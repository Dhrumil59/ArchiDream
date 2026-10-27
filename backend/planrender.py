"""Deterministic DXF -> PNG renderer with an exact world->pixel transform.

The engine's own render_png() lets the ezdxf drawing backend control the axes
aspect, so the mapping between drawing coordinates and pixels is unknowable.
Here we pin the figure so the axes fill it edge-to-edge and the pixel canvas
has the same aspect ratio as the world window — the transform becomes a pure
linear map we can persist to plan_transform.json and reuse for room hotspots.
"""

import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import ezdxf
from ezdxf import recover
from ezdxf.math import Vec3

DPI = 100
TARGET_WIDTH_PX = 1600
MARGIN_FRAC = 0.02  # same 2% breathing room the engine uses


def _read_dxf(dxf_path):
    try:
        doc = ezdxf.readfile(dxf_path)
    except (ezdxf.DXFStructureError, ezdxf.DXFError):
        doc, _auditor = recover.readfile(dxf_path)
    return doc


def _extents(doc, msp):
    ext_min = doc.header.get("$EXTMIN")
    ext_max = doc.header.get("$EXTMAX")
    if ext_min and ext_max and Vec3(ext_max).x >= Vec3(ext_min).x:
        return ((Vec3(ext_min).x, Vec3(ext_min).y),
                (Vec3(ext_max).x, Vec3(ext_max).y))
    from ezdxf.bbox import extents as bbox_extents
    box = bbox_extents(msp, fast=True)
    return ((box.extmin.x, box.extmin.y), (box.extmax.x, box.extmax.y))


def render_plan(dxf_path, out_png, out_transform, window=None):
    """Render the model space to out_png and write the pixel transform.

    window: optional (x0, y0, x1, y1) in drawing units — e.g. the detected
    floor region, so elevation/detail views sharing the sheet are excluded.
    Returns the transform dict: world window (drawing units) + canvas size.
    """
    doc = _read_dxf(dxf_path)
    msp = doc.modelspace()
    if window:
        (xmin, ymin), (xmax, ymax) = (window[0], window[1]), (window[2],
                                                              window[3])
    else:
        (xmin, ymin), (xmax, ymax) = _extents(doc, msp)
    mx = (xmax - xmin) * MARGIN_FRAC
    my = (ymax - ymin) * MARGIN_FRAC
    wx0, wx1 = xmin - mx, xmax + mx
    wy0, wy1 = ymin - my, ymax + my
    ww, wh = wx1 - wx0, wy1 - wy0

    width_px = TARGET_WIDTH_PX
    height_px = max(64, round(width_px * wh / ww))

    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    fig = plt.figure(dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    config = Configuration(background_policy=BackgroundPolicy.WHITE)
    Frontend(ctx, MatplotlibBackend(ax), config=config).draw_layout(
        msp, finalize=True)
    # finalize() resizes the whole FIGURE to the auto-fit data aspect and pins
    # aspect='equal' — both must be re-asserted or the px transform is garbage.
    fig.set_size_inches(width_px / DPI, height_px / DPI)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    # canvas aspect == window aspect, so 'auto' is undistorted AND edge-to-edge
    ax.set_aspect("auto")
    ax.set_axis_off()
    ax.set_facecolor("white")  # margins outside the drawn background rect
    fig.savefig(out_png, dpi=DPI)
    plt.close(fig)

    from PIL import Image
    with Image.open(out_png) as check:
        got_w, got_h = check.size
    # matplotlib rounds inches*dpi, so the file can land a pixel off the
    # requested size. The axes fill the figure and aspect is 'auto', so the
    # window maps edge-to-edge onto whatever was actually written — take the
    # real size as the transform and the mapping stays exact. Anything beyond
    # rounding means the figure was resized under us and the transform would
    # be garbage, so still fail loudly.
    if abs(got_w - width_px) > 2 or abs(got_h - height_px) > 2:
        raise RuntimeError(f"render size {(got_w, got_h)} != expected "
                           f"{(width_px, height_px)}")
    width_px, height_px = got_w, got_h

    transform = {
        "width_px": width_px, "height_px": height_px,
        "world": {"x0": wx0, "y0": wy0, "x1": wx1, "y1": wy1},
    }
    with open(out_transform, "w", encoding="utf-8") as f:
        json.dump(transform, f, indent=1)
    return transform


def world_to_px(transform, x, y):
    """Drawing-unit point -> pixel point (y flipped: image origin is top-left)."""
    w = transform["world"]
    px = (x - w["x0"]) / (w["x1"] - w["x0"]) * transform["width_px"]
    py = (w["y1"] - y) / (w["y1"] - w["y0"]) * transform["height_px"]
    return px, py


def meters_to_px(transform, model_meta, xm, ym):
    """model.json meter-space point -> pixel point.

    model.json coordinates are meters = (drawing - origin_offset) * to_meters,
    so drawing = origin_offset + meters / to_meters.
    """
    ox, oy = model_meta["origin_offset_drawing_units"]
    f = model_meta["to_meters_factor"] or 1.0
    return world_to_px(transform, ox + xm / f, oy + ym / f)


def px_per_meter(transform, model_meta):
    f = model_meta["to_meters_factor"] or 1.0
    w = transform["world"]
    return transform["width_px"] / (w["x1"] - w["x0"]) / f


def debug_overlay(plan_png, rooms, out_png):
    """Draw room polygons + centroids over the plan render for a visual check."""
    from PIL import Image, ImageDraw
    img = Image.open(plan_png).convert("RGB")
    drw = ImageDraw.Draw(img, "RGBA")
    for room in rooms:
        pts = [tuple(p) for p in room["polygon_px"]]
        if len(pts) >= 3:
            drw.polygon(pts, fill=(80, 200, 120, 70), outline=(20, 160, 90, 255))
        cx, cy = room["centroid_px"]
        r = 6
        drw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(230, 60, 60, 255))
        drw.text((cx + 8, cy - 6), room["name"], fill=(200, 30, 30, 255))
    img.save(out_png)
