"""
dxf_analyse.py — generic DXF extraction + analysis pipeline.

Usage:
    python dxf_analyse.py [path/to/file.dxf]

Produces (next to the input file):
    <name>.png          full rendering via ezdxf's drawing add-on
    <name>-data.json    lossless structured export of every entity
    <name>-report.html  human-readable summary report

Design goals:
    - Lossless: every entity in every layout (model + paper space) and every
      block definition is serialized with its full geometry and transform
      (position, scale, rotation), plus layer/color/linetype properties.
    - Unit-aware: reads $INSUNITS and sanity-checks it against the drawing
      extents (files frequently declare the wrong unit).
    - Generic: nothing is keyed to a specific file's layer names or language.
      Room detection prefers real closed geometry (hatches, closed polylines,
      wall-segment loops) over text heuristics; text labels only *name* rooms.
"""

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

import ezdxf
from ezdxf import recover
from ezdxf import path as ezpath
from ezdxf.math import Vec3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

MIN_ROOM_AREA_M2 = 0.8     # ignore cells smaller than this (m²)
MAX_ROOM_FRACTION = 0.85   # a "room" larger than this fraction of the whole
                           # drawing footprint is the outer boundary, not a room
CURVE_FLATTEN_TOL = 0.01   # sagitta tolerance when flattening curves
LOOP_GAP_TOL = 1e-4        # edgeminer: max gap between wall segment endpoints
                           # (relative to drawing size, scaled at runtime)

# Optional hints for naming rooms: a text inside a room polygon matching this
# is preferred over arbitrary text (dimensions, notes). Extend freely; if no
# text matches, the room is still detected and gets a generic name.
ROOM_NAME_HINT = re.compile(
    r"(bed|bath|kitchen|dining|living|hall|garage|porch|corridor|entry|toilet|"
    r"lounge|office|store|room|closet|laundry|estancia|recamara|recámara|baño|"
    r"bano|cocina|comedor|sala|pasillo|terraza|bodega|patio|wc|zimmer|küche|"
    r"chambre|cuisine|salon)", re.I)

# INSUNITS code -> (name, meters per unit)
INSUNITS = {
    0: ("unitless", None), 1: ("inches", 0.0254), 2: ("feet", 0.3048),
    3: ("miles", 1609.344), 4: ("millimeters", 0.001), 5: ("centimeters", 0.01),
    6: ("meters", 1.0), 7: ("kilometers", 1000.0), 8: ("microinches", 2.54e-8),
    9: ("mils", 2.54e-5), 10: ("yards", 0.9144), 11: ("angstroms", 1e-10),
    12: ("nanometers", 1e-9), 13: ("microns", 1e-6), 14: ("decimeters", 0.1),
}

# ═══════════════════════════════════════════════════════════════════════════
# SMALL GEOMETRY HELPERS (no external deps)
# ═══════════════════════════════════════════════════════════════════════════

def vec(v):
    """Vec3/tuple -> [x, y, z] (z omitted when 0)."""
    v = Vec3(v)
    return [v.x, v.y] if abs(v.z) < 1e-12 else [v.x, v.y, v.z]


def polygon_area(pts):
    """Shoelace area of a 2D point list (absolute value)."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(px, py, pts):
    """Ray-casting point-in-polygon test."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i][0], pts[i][1]
        xj, yj = pts[j][0], pts[j][1]
        if (yi > py) != (yj > py):
            x_cross = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# ═══════════════════════════════════════════════════════════════════════════
# UNITS
# ═══════════════════════════════════════════════════════════════════════════

def resolve_units(doc, extent_span):
    """Return unit info dict. Trust $INSUNITS unless it yields an implausible
    building size, in which case guess a unit that puts the drawing span in a
    plausible 2–2000 m range (architectural drawings)."""
    code = doc.header.get("$INSUNITS", 0)
    name, to_m = INSUNITS.get(code, (f"code-{code}", None))
    info = {"insunits_code": code, "declared_unit": name,
            "unit": name, "to_meters": to_m, "guessed": False}
    span = max(extent_span) if extent_span else 0.0
    if span <= 0:
        return info
    # architectural drawings: 2–200 m span. Deliberately narrow — files
    # routinely declare the wrong unit (e.g. inches for a mm drawing, which
    # a loose upper bound would wave through as a 280 m building)
    declared_span_m = span * to_m if to_m else None
    if declared_span_m is None or not (2.0 <= declared_span_m <= 200.0):
        for cand_code in (6, 4, 5, 1, 2):  # m, mm, cm, in, ft
            cname, cfactor = INSUNITS[cand_code]
            if 2.0 <= span * cfactor <= 200.0:
                info.update(unit=cname, to_meters=cfactor, guessed=True,
                            note=(f"declared unit '{name}' gives an implausible "
                                  f"drawing span; '{cname}' assumed instead"))
                break
    return info


# ═══════════════════════════════════════════════════════════════════════════
# ENTITY SERIALIZATION (lossless-by-intent)
# ═══════════════════════════════════════════════════════════════════════════

def flatten_2d(entity):
    """Flattened [x, y] vertex list for any curve-ish entity, or None."""
    try:
        p = ezpath.make_path(entity)
        return [[v.x, v.y] for v in p.flattening(CURVE_FLATTEN_TOL)]
    except Exception:
        return None


def wcs(e, v):
    """OCS point -> WCS. Entities with a non-default extrusion vector store
    their coordinates in a local system; consumers need world coordinates."""
    try:
        return vec(e.ocs().to_wcs(Vec3(v)))
    except Exception:
        return vec(v)


def note_ocs(out, e):
    """For OCS curve entities with a non-default extrusion: record it and add
    WCS flattened geometry, since the raw points/angles are local-space."""
    d = e.dxf
    ext = Vec3(d.extrusion) if d.hasattr("extrusion") else Vec3(0, 0, 1)
    if not ext.isclose(Vec3(0, 0, 1)):
        out["extrusion"] = vec(ext)
        pts = flatten_2d(e)  # make_path yields WCS coordinates
        if pts:
            out["flattened_wcs"] = pts


def common_props(e):
    d = e.dxf
    out = {"type": e.dxftype(), "handle": e.dxf.handle,
           "layer": d.layer if d.hasattr("layer") else "0"}
    if d.hasattr("color") and d.color != 256:      # 256 = BYLAYER
        out["color_aci"] = d.color
    if d.hasattr("true_color"):
        out["true_color"] = d.true_color
    if d.hasattr("linetype") and d.linetype.upper() != "BYLAYER":
        out["linetype"] = d.linetype
    if d.hasattr("lineweight") and d.lineweight not in (-1, -2, -3):
        out["lineweight"] = d.lineweight
    return out


def serialize_entity(e):
    """Type-specific geometry; falls back to raw dxf attribs for rare types."""
    t = e.dxftype()
    out = common_props(e)
    d = e.dxf
    try:
        if t == "LINE":
            out["start"], out["end"] = vec(d.start), vec(d.end)
            out["length"] = (Vec3(d.end) - Vec3(d.start)).magnitude

        elif t == "ARC":
            out.update(center=wcs(e, d.center), radius=d.radius,
                       start_angle=d.start_angle, end_angle=d.end_angle)
            span = (d.end_angle - d.start_angle) % 360 or 360
            out["length"] = math.radians(span) * d.radius
            note_ocs(out, e)

        elif t == "CIRCLE":
            out.update(center=wcs(e, d.center), radius=d.radius,
                       length=2 * math.pi * d.radius)

        elif t == "ELLIPSE":
            out.update(center=vec(d.center), major_axis=vec(d.major_axis),
                       ratio=d.ratio, start_param=d.start_param,
                       end_param=d.end_param)

        elif t == "LWPOLYLINE":
            # (x, y, start_width, end_width, bulge) — bulge preserves arcs
            out["points"] = [list(p) for p in e.get_points()]
            out["closed"] = bool(e.closed)
            pts = flatten_2d(e)
            if pts:
                out["length"] = sum(math.hypot(pts[i+1][0]-pts[i][0],
                                               pts[i+1][1]-pts[i][1])
                                    for i in range(len(pts)-1))
                if e.closed:
                    out["area"] = polygon_area(pts)
            note_ocs(out, e)

        elif t == "POLYLINE":
            out["points"] = [vec(v.dxf.location) for v in e.vertices]
            out["closed"] = bool(e.is_closed)
            note_ocs(out, e)

        elif t == "SPLINE":
            out["degree"] = d.degree
            out["control_points"] = [vec(p) for p in e.control_points]
            out["knots"] = list(e.knots)
            out["closed"] = bool(e.closed)
            out["flattened"] = flatten_2d(e)

        elif t in ("TEXT", "ATTRIB", "ATTDEF"):
            out["text"] = d.text
            insert = d.insert if d.hasattr("insert") else None
            align = d.align_point if d.hasattr("align_point") else None
            out["position"] = wcs(e, align or insert or (0, 0))
            out["height"] = d.height if d.hasattr("height") else None
            out["rotation"] = d.rotation if d.hasattr("rotation") else 0.0
            if t == "ATTRIB":
                out["tag"] = d.tag

        elif t == "MTEXT":
            out["text"] = e.plain_text()
            out["raw_text"] = e.text
            out["position"] = vec(d.insert)
            out["height"] = d.char_height
            out["rotation"] = d.rotation if d.hasattr("rotation") else 0.0

        elif t == "INSERT":
            out.update(block=d.name, position=wcs(e, d.insert),
                       x_scale=d.xscale, y_scale=d.yscale, z_scale=d.zscale,
                       rotation=d.rotation)
            ext = Vec3(d.extrusion)
            if not ext.isclose(Vec3(0, 0, 1)):
                out["extrusion"] = vec(ext)
            # full block-space -> WCS transform (rows of a 4x4, row-vector
            # convention: p_wcs = p_block @ M). This is the authoritative
            # placement; scale/rotation above are raw OCS values.
            m = e.matrix44()
            out["transform"] = [list(m.get_row(i)) for i in range(4)]
            attribs = {a.dxf.tag: a.dxf.text for a in e.attribs}
            if attribs:
                out["attributes"] = attribs

        elif t == "HATCH":
            out.update(pattern=d.pattern_name, solid=bool(e.has_solid_fill),
                       scale=d.pattern_scale if d.hasattr("pattern_scale") else 1.0)
            boundaries = []
            try:
                for p in ezpath.from_hatch(e):
                    pts = [[v.x, v.y] for v in p.flattening(CURVE_FLATTEN_TOL)]
                    if len(pts) >= 3:
                        boundaries.append({"points": pts,
                                           "area": polygon_area(pts)})
            except Exception:
                pass
            out["boundaries"] = boundaries
            out["area"] = sum(b["area"] for b in boundaries[:1]) if boundaries else None

        elif t == "DIMENSION":
            out["dim_type"] = d.dimtype
            out["text_override"] = d.text  # "<>" means measured value
            try:
                out["measurement"] = e.get_measurement()
            except Exception:
                out["measurement"] = None
            if d.hasattr("defpoint"):
                out["defpoint"] = vec(d.defpoint)

        elif t == "POINT":
            out["position"] = vec(d.location)

        elif t == "SOLID" or t == "TRACE":
            out["points"] = [wcs(e, d.vtx0), wcs(e, d.vtx1),
                             wcs(e, d.vtx2), wcs(e, d.vtx3)]

        elif t == "LEADER":
            out["vertices"] = [vec(v) for v in e.vertices]

        elif t == "VIEWPORT":
            out.update(center=vec(d.center), width=d.width, height=d.height,
                       view_center=vec(d.view_center_point),
                       view_height=d.view_height)

        else:
            # generic fallback: dump every dxf attribute this entity carries
            raw = {}
            for key, value in e.dxfattribs().items():
                if key in ("handle", "owner", "layer", "color", "linetype"):
                    continue
                raw[key] = vec(value) if isinstance(value, Vec3) else \
                    (value if isinstance(value, (int, float, str, bool)) else str(value))
            out["attribs"] = raw
            pts = flatten_2d(e)
            if pts:
                out["flattened"] = pts
    except Exception as exc:
        out["serialization_error"] = f"{type(exc).__name__}: {exc}"
    return out


def serialize_layout(layout):
    return [serialize_entity(e) for e in layout]


# ═══════════════════════════════════════════════════════════════════════════
# TABLES: LAYERS / BLOCKS
# ═══════════════════════════════════════════════════════════════════════════

def serialize_layers(doc):
    layers = []
    for lyr in doc.layers:
        d = lyr.dxf
        layers.append({
            "name": d.name,
            "color_aci": d.color,
            "true_color": d.true_color if d.hasattr("true_color") else None,
            "linetype": d.linetype,
            "lineweight": d.lineweight,
            "is_on": lyr.is_on(),
            "is_frozen": lyr.is_frozen(),
            "is_locked": lyr.is_locked(),
            "plot": bool(d.plot),
        })
    return layers


def serialize_blocks(doc):
    blocks = {}
    for blk in doc.blocks:
        if blk.name.lower().startswith(("*model_space", "*paper_space")):
            continue
        blocks[blk.name] = {
            "base_point": vec(blk.block.dxf.base_point),
            "entities": serialize_layout(blk),
        }
    return blocks


# ═══════════════════════════════════════════════════════════════════════════
# SEMANTIC MODEL (<name>-model.json)
#   Turns raw strokes into building objects a 3D tool can extrude directly:
#   walls (centerline + thickness + openings), rooms (true polygons),
#   furniture (block footprints with transform applied).
#   All coordinates in METERS, origin at the floor-plan bbox corner.
# ═══════════════════════════════════════════════════════════════════════════

DOOR_BLOCK_KW = ("PUERTA", "DOOR", "TUR", "PORTE")
WINDOW_BLOCK_KW = ("VENTANA", "WINDOW", "VI-", "FENSTER", "FENETRE")
# NOTE: matching doors by LAYER name as well was tried and reverted — on sets
# with anonymised blocks it seals frames and hardware, not just the doorway,
# and costs more rooms than it recovers. Anonymous door blocks are handled by
# _gap_barriers instead.
WALL_THICKNESS_M = (0.05, 0.45)   # plausible wall thickness range
OPENING_WIDTH_M = (0.45, 3.5)     # plausible door/window opening widths
# How wide a hole the stranded-room rescue is willing to treat as a hole.
# A gap wider than this is open space, not a hole. Raising it to 5.0 was tried
# for a 4.3 m undrawn terrace opening and changed nothing — the region escapes
# by several routes at once, so widening the ceiling only finds more ways out.
LEAK_SEAL_MAX_W_M = 3.0
DEFAULT_WALL_HEIGHT_M = 2.7


WALL_LAYER_KW = ("WALL", "MURO", "MUR", "WAND", "PARET", "PARED", "STRUCT",
                 "PAREDES", "MUROS")
# window assemblies enclose space too — a glass wall drawn on a VENTANAS
# layer is still a wall for room topology (One-story-house lesson)
WINDOW_LAYER_KW = ("VENTANA", "WINDOW", "GLAZ", "FENSTER", "JANELA", "JENDELA")
NON_WALL_LAYER_KW = ("EJE", "AXIS", "GRID", "DIM", "COTA", "TEXT", "PROYEC",
                     "NIVEL", "LEVEL", "SYMB", "HATCH", "ANNO",
                     "PISO", "FLOOR", "FINISH", "TILE", "CEILING", "CEIL",
                     "ROOF", "FURN", "MOBIL", "VEGETA", "TREE", "PLANT",
                     "COLUMNA", "COLUMN", "ACOTAM", "WINDOW", "DOOR",
                     "VENTANA", "PUERTA")


def _entity_segments(e):
    """Straight segments of a serialized LINE/polyline entity."""
    if e["type"] == "LINE":
        return [((e["start"][0], e["start"][1]), (e["end"][0], e["end"][1]))]
    if e["type"] in ("LWPOLYLINE", "POLYLINE"):
        pts = e.get("flattened_wcs") or [(p[0], p[1]) for p in e.get("points", [])]
        if e.get("closed") and len(pts) > 2:
            pts = pts + [pts[0]]
        return [((pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1]))
                for i in range(len(pts) - 1)]
    return []


def _polyline_wall_score(ents_on_layer):
    """Score how 'wall-like' a layer's LWPOLYLINE entities look.
    Wall polylines have two signatures:
      (a) Open U/L-shaped traces: short segments with right-angle turns
          (each polyline draws one face-and-ends of a wall section).
      (b) Closed thin rectangles: area << perimeter² / 4π (very elongated).
    Returns a float 0..1; > 0.35 strongly suggests a wall layer."""
    polys = [e for e in ents_on_layer
             if e["type"] in ("LWPOLYLINE", "POLYLINE")]
    if not polys:
        return 0.0
    scores = []
    for e in polys:
        pts_raw = e.get("flattened_wcs") or [(p[0], p[1])
                                              for p in e.get("points", [])]
        pts = [(p[0], p[1]) for p in pts_raw]
        n = len(pts)
        if n < 2:
            continue
        # right-angle count
        right = 0
        for i in range(1, n - 1):
            ax, ay = pts[i][0] - pts[i-1][0], pts[i][1] - pts[i-1][1]
            bx, by = pts[i+1][0] - pts[i][0], pts[i+1][1] - pts[i][1]
            la, lb = math.hypot(ax, ay), math.hypot(bx, by)
            if la > 0 and lb > 0:
                dot = (ax * bx + ay * by) / (la * lb)
                if abs(dot) < 0.18:   # ~80–100 degrees counts as right-angle
                    right += 1
        right_ratio = right / max(n - 2, 1)
        # elongation for closed polys
        if e.get("closed") and n >= 3:
            perim = sum(math.hypot(pts[(i+1) % n][0] - pts[i][0],
                                   pts[(i+1) % n][1] - pts[i][1])
                        for i in range(n))
            area = polygon_area(pts)
            elongation = (4 * math.pi * area / perim ** 2) if perim > 0 else 1
            scores.append(max(right_ratio, 1 - elongation))
        else:
            scores.append(right_ratio)
    return sum(scores) / len(scores) if scores else 0.0


def _wall_layer_strategies(model_ents):
    """Candidate wall-layer sets, most specific first. Which one is right
    differs per file: some put walls on MURO-*/WALL-* layers, some put a
    minor 'wall projection' layer there while the real walls sit on layer
    '0'. The caller tries each strategy and keeps the one that encloses the
    most space — self-validation instead of a magic ratio.

    Extra strategy for LWPOLYLINE-dominant drawings (Revit/ArchiCAD exports,
    polyline-trace conventions): score layers by their polyline wall-likeness
    in addition to line length, and add those layers as a dedicated strategy
    before the generic fallback so the self-validation loop picks them up."""
    lengths = defaultdict(float)
    layer_ents = defaultdict(list)
    for e in model_ents:
        layer_ents[e["layer"]].append(e)
        for (x1, y1), (x2, y2) in _entity_segments(e):
            lengths[e["layer"]] += math.hypot(x2 - x1, y2 - y1)
    if not lengths:
        return []

    # keyword-named wall layers
    kw = {l for l in lengths if any(k in l.upper() for k in WALL_LAYER_KW)}
    # glass walls: window-layer linework participates in wall detection
    kw |= {l for l in lengths
           if any(k in l.upper() for k in WINDOW_LAYER_KW)}

    # high-length layers (existing heuristic)
    top = max(lengths.values())
    heavy = {l for l, v in lengths.items()
             if v >= 0.4 * top
             and not any(k in l.upper() for k in NON_WALL_LAYER_KW)}

    # LWPOLYLINE wall-likeness score per layer
    poly_scores = {l: _polyline_wall_score(ents)
                   for l, ents in layer_ents.items()
                   if not any(k in l.upper() for k in NON_WALL_LAYER_KW)}
    poly_wall = {l for l, s in poly_scores.items() if s >= 0.30}

    strategies = []
    # Strategy 1: keyword-named layers only (most precise signal)
    if kw:
        strategies.append(kw)
    # Strategy 2: keyword + poly-wall layers combined
    # (needed when the kw layer alone misses walls drawn on a secondary layer)
    combined = kw | poly_wall
    if combined != kw and combined:
        strategies.append(combined)
    # Strategy 3: keyword + heavy-length + poly-wall (broadest)
    broadest = kw | heavy | poly_wall
    if broadest not in (kw, combined):
        strategies.append(broadest)
    # Fallback: at least something
    if not strategies:
        strategies.append({max(lengths, key=lengths.get)})
    return strategies


def _wall_candidates(model_ents, floor_bbox, wall_layers):
    """Two tiers of wall-face segments inside the plan region:
    primary  — on the given wall layers: trusted, paired into walls directly;
    secondary — any other layer except annotation-ish ones: used only as
               collinear evidence to extend walls found from the primary tier
               (drawings routinely put parts of a wall on layer '0' or draw
               one face as a polyline)."""
    xmin, ymin, xmax, ymax = floor_bbox

    primary, secondary = [], []
    for e in model_ents:
        if e["type"] not in ("LINE", "LWPOLYLINE", "POLYLINE"):
            continue
        lyr = e["layer"].upper()
        if e["layer"] not in wall_layers \
                and any(k in lyr for k in NON_WALL_LAYER_KW):
            continue
        for (x1, y1), (x2, y2) in _entity_segments(e):
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            if not (xmin <= mx <= xmax and ymin <= my <= ymax):
                continue
            seg = ((x1, y1), (x2, y2))
            (primary if e["layer"] in wall_layers else secondary).append(seg)
    return primary, secondary


def _oriented_box(pts):
    """Minimum-area oriented bounding box of a closed path.
    Returns (short_side, long_side, u, v, u_range, v_mid) where u is the unit
    vector along the long side. Rotating calipers over the edge directions —
    the minimum-area box always shares an edge with the hull, and boundary
    paths here are already near-convex bands."""
    best = None
    for i in range(len(pts) - 1):
        dx, dy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        L = math.hypot(dx, dy)
        if L < 1e-9:
            continue
        ux, uy = dx / L, dy / L
        us = [p[0] * ux + p[1] * uy for p in pts]
        vs = [-p[0] * uy + p[1] * ux for p in pts]
        w, h = max(vs) - min(vs), max(us) - min(us)
        if best is None or w * h < best[0]:
            best = (w * h, (ux, uy), (-uy, ux), min(us), max(us),
                    min(vs), max(vs))
    if best is None:
        return None
    _, u, v, u0, u1, v0, v1 = best
    across, along = v1 - v0, u1 - u0
    if along < across:   # long side runs across: swap roles
        u, v = v, u
        u0, u1, v0, v1 = v0, v1, u0, u1
        across, along = along, across
    return across, along, u, v, (u0, u1), (v0, v1)


def _hatch_wall_faces(model_ents, to_m):
    """Wall faces recovered from poche: solid/pattern HATCH bands.

    Architectural sets routinely draw walls as a filled band (poche) rather
    than as two drawn faces — the fill IS the wall, and its boundary path
    carries no LINE geometry at all, so wall detection never sees it. This
    turns every hatch boundary path shaped like a wall (a band whose width
    falls in the plausible wall-thickness range, elongated, and long enough
    to be structural) back into its two long edges.

    Emitting faces rather than finished runs is deliberate: they flow through
    the same pairing, extension, opening-detection and gap-sealing as drawn
    linework, so a poche wall behaves exactly like a drawn one. Material and
    furniture fills also produce bands, so the caller keeps these only when
    they enclose more area."""
    min_t, max_t = WALL_THICKNESS_M[0] / to_m, WALL_THICKNESS_M[1] / to_m
    min_len = 0.6 / to_m
    faces = []
    for e in model_ents:
        if e["type"] != "HATCH":
            continue
        for b in (e.get("boundaries") or []):
            pts = b.get("points") or []
            if len(pts) < 3:
                continue
            box = _oriented_box(pts)
            if not box:
                continue
            across, along, u, v, (u0, u1), (v0, v1) = box
            if not (min_t <= across <= max_t and along >= min_len):
                continue
            if along < 2.5 * across:      # a blob, not a band
                continue
            for off in (v0, v1):
                faces.append((
                    (u[0] * u0 + v[0] * off, u[1] * u0 + v[1] * off),
                    (u[0] * u1 + v[0] * off, u[1] * u1 + v[1] * off),
                ))
    return faces


def _parallel_groups(segs, min_len):
    """Group segments into parallel families, projected onto a shared axis.

    Yields (d, n, projected) where `projected` is the family's segments as
    (offset along n, lo, hi along d), sorted by offset. Shared by wall pairing
    and single-line wall detection so both see identical geometry."""
    ANG_TOL = 1.0  # degrees: segments within this are considered parallel

    items = []  # (angle 0..180, x1, y1, x2, y2)
    for (x1, y1), (x2, y2) in segs:
        ln = math.hypot(x2 - x1, y2 - y1)
        if ln < min_len:
            continue
        items.append((math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0,
                      x1, y1, x2, y2))
    if not items:
        return

    # cluster by angle (single-link on the sorted list, with 0/180 wraparound)
    items.sort()
    clusters = [[items[0]]]
    for it in items[1:]:
        if it[0] - clusters[-1][-1][0] <= ANG_TOL:
            clusters[-1].append(it)
        else:
            clusters.append([it])
    if len(clusters) > 1 and (180.0 - clusters[-1][-1][0]) + clusters[0][0][0] <= ANG_TOL:
        clusters[0] = clusters.pop() + clusters[0]

    for cl in clusters:
        # one shared direction per cluster so offsets are comparable even
        # when individual segments deviate by fractions of a degree
        # (unwrap angles relative to the first so 179.8 and 0.2 average to 180)
        base = cl[0][0]
        unwrapped = [c[0] - 180 if c[0] - base > 90
                     else c[0] + 180 if base - c[0] > 90 else c[0] for c in cl]
        a = math.radians(sum(unwrapped) / len(unwrapped))
        d = (math.cos(a), math.sin(a))
        n = (-d[1], d[0])
        projected = []
        for _, x1, y1, x2, y2 in cl:
            off = (x1 * n[0] + y1 * n[1] + x2 * n[0] + y2 * n[1]) / 2
            t1, t2 = x1 * d[0] + y1 * d[1], x2 * d[0] + y2 * d[1]
            projected.append((off, min(t1, t2), max(t1, t2)))
        projected.sort()
        yield d, n, projected


def _group_pieces(pieces, to_m):
    """Group wall pieces into runs: same direction and nearly the same
    centerline offset and thickness -> union of intervals along the wall.
    Single-link merge (no fixed buckets: pieces of one wall must not split
    across a rounding boundary)."""
    tol = 0.04 / to_m
    runs = []
    for d, n, mid, t, lo, hi in sorted(pieces, key=lambda p: (p[0], p[2])):
        for r in runs:
            if r["d"] == d and abs(r["mid"] - mid) <= tol \
                    and abs(r["t"] - t) <= tol:
                k = len(r["intervals"])
                r["mid"] = (r["mid"] * k + mid) / (k + 1)
                r["t"] = max(r["t"], t)
                r["intervals"].append((lo, hi))
                break
        else:
            runs.append({"d": d, "n": n, "mid": mid, "t": t,
                         "intervals": [(lo, hi)]})

    for r in runs:
        iv = sorted(r["intervals"])
        merged = [list(iv[0])]
        for lo, hi in iv[1:]:
            if lo <= merged[-1][1] + tol:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        r["intervals"] = merged
    return runs


def _pair_walls(segs, to_m):
    """Pair parallel line segments into walls with thickness.
    Returns wall runs: dict(direction, normal, offset, thickness, intervals)."""
    min_t, max_t = WALL_THICKNESS_M[0] / to_m, WALL_THICKNESS_M[1] / to_m
    min_overlap = 0.25 / to_m

    pieces = []  # (d, n, mid_offset, thickness, t_lo, t_hi)
    for d, n, projected in _parallel_groups(segs, min_overlap):
        for i, (off1, a1, b1) in enumerate(projected):
            for off2, a2, b2 in projected[i + 1:]:
                t = off2 - off1
                if t > max_t:
                    break
                if t < min_t:
                    continue
                lo, hi = max(a1, a2), min(b1, b2)
                if hi - lo >= min_overlap:
                    pieces.append((d, n, off1 + t / 2, t, lo, hi))

    return _group_pieces(pieces, to_m)


def _run_spans_along(d, n, off, runs, tol):
    """Extents of existing runs that lie along this line, projected onto it.

    Compared geometrically (not by identity of the direction tuple) because
    runs come from their own angle clustering and may carry a fractionally
    different d. Each run contributes its FULL extent, gaps included, so a
    doorway inside a run stays 'covered' and never gets walled shut."""
    spans = []
    for r in runs:
        rd, rn, rmid = r["d"], r["n"], r["mid"]
        if abs(rd[0] * n[0] + rd[1] * n[1]) > 0.02:   # not parallel to us
            continue
        rlo, rhi = r["intervals"][0][0], r["intervals"][-1][1]
        ax = rd[0] * rlo + rn[0] * rmid
        ay = rd[1] * rlo + rn[1] * rmid
        bx = rd[0] * rhi + rn[0] * rmid
        by = rd[1] * rhi + rn[1] * rmid
        if abs((ax * n[0] + ay * n[1]) - off) > tol:  # different offset
            continue
        s1, s2 = ax * d[0] + ay * d[1], bx * d[0] + by * d[1]
        spans.append((min(s1, s2), max(s1, s2)))
    return spans


def _single_line_runs(segs, runs, to_m):
    """Wall runs for wall-layer linework drawn as a SINGLE line.

    _pair_walls needs two parallel faces. Production architectural sets
    routinely mix conventions: interior partitions drawn double-line (two
    faces a stud-width apart) while the outer envelope — or a party wall, or
    a glazed facade — is drawn as one line, the line itself being the wall.
    Those segments pair with nothing, so the envelope never enters the wall
    network and the partition floods straight out of every room touching it.

    We work per interval rather than per segment: a long envelope line often
    pairs with something over a short stretch (a partition butting into it),
    and rejecting the whole line for that would leave most of the envelope
    missing. So we subtract the stretches an existing run already carries and
    promote whatever remains, if it is long enough to be structural.

    Thickness is the median of the paired walls in the same drawing, so the
    sealed line matches the file's own drafting scale. The caller keeps the
    result only when it encloses more area, so fully double-line plans are
    left untouched."""
    max_t = WALL_THICKNESS_M[1] / to_m
    # structural, not a jamb/return/detail tick
    min_len = 0.8 / to_m

    # give a bare line the thickness this drawing uses for its real walls
    t_nom = 0.10 / to_m
    if runs:
        ts = sorted(r["t"] for r in runs)
        t_nom = ts[len(ts) // 2]

    pieces = []
    for d, n, projected in _parallel_groups(segs, min_len):
        for off, lo, hi in projected:
            blocked = _run_spans_along(d, n, off, runs, max_t)
            for a, b in _clip_intervals([lo, hi], blocked):
                if b - a >= min_len:
                    pieces.append((d, n, off, t_nom, a, b))

    return _group_pieces(pieces, to_m)


def _clip_intervals(iv, blocked):
    """Subtract blocked ranges from interval [lo, hi]."""
    out = [iv]
    for plo, phi in blocked:
        nxt = []
        for lo, hi in out:
            if phi <= lo or plo >= hi:
                nxt.append([lo, hi])
                continue
            if lo < plo:
                nxt.append([lo, plo])
            if hi > phi:
                nxt.append([phi, hi])
        out = nxt
    return out


def _extend_runs(runs, secondary, to_m, classify):
    """Extend wall runs with collinear segments from non-wall layers.
    A segment counts as evidence only if it lies on the run's centerline or
    on one of its two face planes (within tolerance) and is parallel to it —
    furniture/detail lines elsewhere in the room never qualify. Gaps that
    classify as door/window are protected: threshold or floor lines drawn
    across a doorway must not wall it shut."""
    if not runs or not secondary:
        return runs
    tol = 0.04 / to_m
    min_len = 0.15 / to_m
    min_ow, max_ow = OPENING_WIDTH_M[0] / to_m, OPENING_WIDTH_M[1] / to_m
    for r in runs:
        d, n, mid, t = r["d"], r["n"], r["mid"], r["t"]

        protected = []
        for (a_lo, a_hi), (b_lo, b_hi) in zip(r["intervals"], r["intervals"][1:]):
            gap = b_lo - a_hi
            if min_ow <= gap <= max_ow:
                s = (a_hi + b_lo) / 2
                cx, cy = d[0] * s + n[0] * mid, d[1] * s + n[1] * mid
                if classify(cx, cy, gap) in ("door", "window"):
                    protected.append((a_hi, b_lo))
        run_lo = r["intervals"][0][0] - 0.5 / to_m
        run_hi = r["intervals"][-1][1] + 0.5 / to_m
        added = []
        for (x1, y1), (x2, y2) in secondary:
            sx, sy = x2 - x1, y2 - y1
            ln = math.hypot(sx, sy)
            if ln < min_len:
                continue
            # parallel? (cross product of unit directions ~ 0)
            if abs((sx * d[1] - sy * d[0]) / ln) > 0.02:
                continue
            off1 = x1 * n[0] + y1 * n[1]
            off2 = x2 * n[0] + y2 * n[1]
            off = (off1 + off2) / 2
            dist = abs(off - mid)
            on_center = dist <= tol
            on_face = abs(dist - t / 2) <= tol
            if not (on_center or on_face):
                continue
            t1 = x1 * d[0] + y1 * d[1]
            t2 = x2 * d[0] + y2 * d[1]
            lo, hi = min(t1, t2), max(t1, t2)
            if hi < run_lo or lo > run_hi:  # far beyond the run: unrelated
                continue
            added.extend(_clip_intervals([lo, hi], protected))
        if added:
            iv = sorted([list(v) for v in r["intervals"]] + added)
            merged = [list(iv[0])]
            for lo, hi in iv[1:]:
                if lo <= merged[-1][1] + tol:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            r["intervals"] = merged
    return runs


def _supplement_perimeter_runs(runs, secondary, floor_bbox, to_m):
    """Add wall runs for perimeter edges that primary-layer pairing missed.

    Some DXF files split the outer perimeter across layers: the inner walls
    sit on a named layer (e.g. 'paredes') while one or more outer boundary
    walls are only on layer '0'. This function scans secondary-tier segments
    for long axis-aligned pairs (within wall thickness) that are NOT already
    covered by an existing run and that span a substantial fraction of the
    floor region — these are almost certainly outer perimeter walls.

    Unlike a bbox-proximity check (which fails when the floor_bbox is padded),
    we measure whether a candidate segment is 'outermost' in its axis direction
    relative to all existing runs, and whether it spans enough of the floor."""
    if not secondary or not runs:
        return runs

    min_len = 3.0 / to_m
    min_t, max_t = WALL_THICKNESS_M[0] / to_m, WALL_THICKNESS_M[1] / to_m
    floor_span_x = floor_bbox[2] - floor_bbox[0]
    floor_span_y = floor_bbox[3] - floor_bbox[1]

    # Existing run centerlines keyed by orientation bucket: angle → list of mids
    run_mids_h = []  # horizontal runs (d≈(1,0)) → their y-value (= n·mid)
    run_mids_v = []  # vertical runs (d≈(0,1)) → their x-value
    for r in runs:
        d, n, mid = r["d"], r["n"], r["mid"]
        if abs(d[0]) > 0.9:   # horizontal
            run_mids_h.append(mid / n[1] if abs(n[1]) > 0.5 else mid)
        elif abs(d[1]) > 0.9: # vertical
            run_mids_v.append(mid / n[0] if abs(n[0]) > 0.5 else mid)

    def already_covered(axis, offset):
        """True if an existing run already sits near this offset."""
        tol = max_t
        mids = run_mids_h if axis == "h" else run_mids_v
        return any(abs(m - offset) <= tol for m in mids)

    # Collect axis-aligned secondary segments long enough to be perimeter walls
    h_segs = []  # (y_offset, x_lo, x_hi)
    v_segs = []  # (x_offset, y_lo, y_hi)
    for (x1, y1), (x2, y2) in secondary:
        ln = math.hypot(x2 - x1, y2 - y1)
        if ln < min_len:
            continue
        if abs(y2 - y1) / ln < 0.05:  # horizontal
            h_segs.append(((y1 + y2) / 2, min(x1, x2), max(x1, x2)))
        elif abs(x2 - x1) / ln < 0.05:  # vertical
            v_segs.append(((x1 + x2) / 2, min(y1, y2), max(y1, y2)))

    def try_pair_segs(seg_list, axis, min_span_frac):
        """Try to form new wall runs from unpaired parallel segments."""
        # Filter: only segments not already covered by existing runs
        uncov = [(off, lo, hi) for off, lo, hi in seg_list
                 if not already_covered(axis, off)]
        if len(uncov) < 2:
            return
        uncov.sort()
        for i, (off1, lo1, hi1) in enumerate(uncov):
            for off2, lo2, hi2 in uncov[i + 1:]:
                t = abs(off2 - off1)
                if t > max_t:
                    break
                if t < min_t:
                    continue
                overlap = min(hi1, hi2) - max(lo1, lo2)
                span = floor_span_x if axis == "h" else floor_span_y
                if overlap < span * min_span_frac:
                    continue
                mid_off = (off1 + off2) / 2
                lo, hi = max(lo1, lo2), min(hi1, hi2)
                if axis == "h":
                    d = (1.0, 0.0)
                    n = (0.0, 1.0) if mid_off >= 0 else (0.0, -1.0)
                    run_mid = mid_off * abs(n[1])
                else:
                    d = (0.0, 1.0)
                    n = (1.0, 0.0) if mid_off >= 0 else (-1.0, 0.0)
                    run_mid = mid_off * abs(n[0])
                # Recompute mid correctly as n · centrepoint
                # (normal is perpendicular to d, mid = x*n[0]+y*n[1] at centre)
                if axis == "h":
                    run_mid = mid_off   # y*1 for n=(0,1); y*(-1) for n=(0,-1)
                    if n[1] < 0:
                        run_mid = -mid_off
                else:
                    run_mid = mid_off   # x*1 for n=(1,0); x*(-1) for n=(-1,0)
                    if n[0] < 0:
                        run_mid = -mid_off
                runs.append({"d": d, "n": n, "mid": run_mid, "t": t,
                             "intervals": [[lo, hi]]})
                run_mids_h.append(mid_off) if axis == "h" else run_mids_v.append(mid_off)
                break

    # Require new runs to span ≥ 30% of the floor in their direction
    try_pair_segs(h_segs, "h", 0.30)
    try_pair_segs(v_segs, "v", 0.30)

    return runs


def _seal_perimeter_gaps(runs, secondary, to_m):
    """Close an open side of the built perimeter with a SINGLE boundary line.

    _pair_walls and _supplement_perimeter_runs both need two parallel faces
    (within wall thickness) to form a wall. Some plans draw one enclosing
    side as a single line — a glazed or open facade whose only linework is
    the plot-boundary edge, often on layer '0'. With that side absent from
    the network the space partition floods straight out through it and the
    whole interior collapses to 'exterior' (the One-story-house lesson:
    kitchen/dining/living/family all leak, only the fully-walled corner
    survives).

    We take the axis-aligned bbox of the EXISTING runs — the footprint built
    from trusted walls, NOT the padded floor region — so we never seal in a
    garden or yard beyond the house. For any of the four sides not already
    carried by a run, we look for a long axis-aligned secondary segment lying
    along that side; if one spans most of it, we add it as a thin sealing run.
    A single line is enough to seal the flood without being a paired wall."""
    if not secondary or not runs:
        return runs

    # World extent of every run + per-side coverage spans (from world
    # endpoints, so the sign of n is irrelevant at this pre-normalize stage).
    xs, ys, h_runs, v_runs = [], [], [], []
    for r in runs:
        d, n, mid = r["d"], r["n"], r["mid"]
        lo, hi = r["intervals"][0][0], r["intervals"][-1][1]
        ax, ay = d[0] * lo + n[0] * mid, d[1] * lo + n[1] * mid
        bx, by = d[0] * hi + n[0] * mid, d[1] * hi + n[1] * mid
        xs += [ax, bx]; ys += [ay, by]
        if abs(d[0]) > 0.9:      # horizontal run
            h_runs.append(((ay + by) / 2, min(ax, bx), max(ax, bx)))
        elif abs(d[1]) > 0.9:    # vertical run
            v_runs.append(((ax + bx) / 2, min(ay, by), max(ay, by)))
    if not xs:
        return runs
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    tol = max(WALL_THICKNESS_M[1] / to_m, 0.35 / to_m)
    min_len = 2.0 / to_m

    # Long axis-aligned secondary segments (any untrusted layer, incl. '0').
    h_segs, v_segs = [], []
    for (x1, y1), (x2, y2) in secondary:
        ln = math.hypot(x2 - x1, y2 - y1)
        if ln < min_len:
            continue
        if abs(y2 - y1) / ln < 0.05:
            h_segs.append(((y1 + y2) / 2, min(x1, x2), max(x1, x2)))
        elif abs(x2 - x1) / ln < 0.05:
            v_segs.append(((x1 + x2) / 2, min(y1, y2), max(y1, y2)))

    def covered(spans, offset, lo, hi):
        total = 0.0
        for off, a, b in spans:
            if abs(off - offset) <= tol:
                total += max(0.0, min(hi, b) - max(lo, a))
        return total >= 0.6 * (hi - lo)

    def best_seg(segs, offset, lo, hi):
        pick, best_ov = None, 0.6 * (hi - lo)
        for off, a, b in segs:
            if abs(off - offset) > tol:
                continue
            ov = min(hi, b) - max(lo, a)
            if ov > best_ov:
                best_ov, pick = ov, (off, max(lo, a), min(hi, b))
        return pick

    seal_t = 0.12 / to_m
    new = []
    for offset in (xmin, xmax):          # vertical sides (east / west)
        if covered(v_runs, offset, ymin, ymax):
            continue
        seg = best_seg(v_segs, offset, ymin, ymax)
        if seg:
            off, lo, hi = seg
            new.append({"d": (0.0, 1.0), "n": (1.0, 0.0), "mid": off,
                        "t": seal_t, "intervals": [[lo, hi]]})
    for offset in (ymin, ymax):          # horizontal sides (north / south)
        if covered(h_runs, offset, xmin, xmax):
            continue
        seg = best_seg(h_segs, offset, xmin, xmax)
        if seg:
            off, lo, hi = seg
            new.append({"d": (1.0, 0.0), "n": (0.0, 1.0), "mid": off,
                        "t": seal_t, "intervals": [[lo, hi]]})
    return runs + new


def _normalize_run(r):
    """Flip a run's direction so equivalent runs are directly comparable
    (d pointing toward +x, or +y when vertical)."""
    d = r["d"]
    if d[0] < -1e-9 or (abs(d[0]) <= 1e-9 and d[1] < 0):
        r["d"] = (-d[0], -d[1])
        r["n"] = (-r["n"][0], -r["n"][1])
        r["mid"] = -r["mid"]
        r["intervals"] = sorted([-hi, -lo] for lo, hi in r["intervals"])
    return r


def _merge_runs(runs, to_m):
    """Stage-1 cleanup: merge near-coincident parallel runs into one wall.
    Double/triple-traced walls (finish lines, projection layers) otherwise
    export as 2-4 stacked walls at nearly the same place. Runs merge when
    parallel and their thickness strips overlap. Openings survive by
    majority: a gap kept by most members stays a gap."""
    if not runs:
        return runs
    runs = [_normalize_run(r) for r in runs]
    max_t = WALL_THICKNESS_M[1] / to_m
    min_ow, max_ow = OPENING_WIDTH_M[0] / to_m, OPENING_WIDTH_M[1] / to_m
    tol = 0.06 / to_m

    parent = list(range(len(runs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            a, b = runs[i], runs[j]
            if abs(a["d"][0] * b["d"][0] + a["d"][1] * b["d"][1]) < 0.999:
                continue
            if abs(a["mid"] - b["mid"]) > (a["t"] + b["t"]) / 2 + tol:
                continue
            a_lo, a_hi = a["intervals"][0][0], a["intervals"][-1][1]
            b_lo, b_hi = b["intervals"][0][0], b["intervals"][-1][1]
            if min(a_hi, b_hi) - max(a_lo, b_lo) < -0.3 / to_m:
                continue  # no overlap along the wall
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

    groups = defaultdict(list)
    for i in range(len(runs)):
        groups[find(i)].append(runs[i])

    merged = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        main = max(members, key=lambda r: r["intervals"][-1][1]
                   - r["intervals"][0][0])
        lo_edge = min(r["mid"] - r["t"] / 2 for r in members)
        hi_edge = max(r["mid"] + r["t"] / 2 for r in members)
        new = {"d": main["d"], "n": main["n"],
               "mid": (lo_edge + hi_edge) / 2,
               "t": min(hi_edge - lo_edge, max_t)}
        # union of member intervals
        iv = sorted([list(v) for r in members for v in r["intervals"]])
        uni = [iv[0]]
        for lo, hi in iv[1:]:
            if lo <= uni[-1][1] + tol:
                uni[-1][1] = max(uni[-1][1], hi)
            else:
                uni.append([lo, hi])
        # gap-majority: re-open spans that most members leave open (a real
        # passage traced only in some layers must not be walled shut by one
        # continuous finish line)
        gaps = []
        for r in members:
            for (a1, ah), (b1, bh) in zip(r["intervals"], r["intervals"][1:]):
                if min_ow <= b1 - ah <= max_ow:
                    gaps.append((ah, b1))
        for ga, gb in gaps:
            n_gap = n_cover = 0
            for r in members:
                ext_lo, ext_hi = r["intervals"][0][0], r["intervals"][-1][1]
                if not (ext_lo - tol <= ga and gb <= ext_hi + tol):
                    continue  # member doesn't reach this span: abstains
                if any(lo - tol <= ga and gb <= hi + tol
                       for lo, hi in r["intervals"]):
                    n_cover += 1
                else:
                    n_gap += 1
            if n_gap > n_cover:
                uni = [seg for piece in uni
                       for seg in _clip_intervals(piece, [(ga, gb)])]
                uni.sort()
        new["intervals"] = uni or [[lo_edge, hi_edge]]
        merged.append(new)
    return merged


def _filter_stub_runs(runs, to_m):
    """Drop phantom 'walls': paired parallel edges of furniture and fixtures
    (call-center pods, sanitary blocks). A real short wall piece (door jamb,
    corner return) touches other walls; furniture fragments float alone or
    are blobs (shorter than twice their own thickness)."""
    def length(r):
        return r["intervals"][-1][1] - r["intervals"][0][0]

    long_runs = [r for r in runs if length(r) >= 1.2 / to_m]

    def connected(r):
        d, n, mid = r["d"], r["n"], r["mid"]
        for s in (r["intervals"][0][0], r["intervals"][-1][1]):
            px, py = d[0] * s + n[0] * mid, d[1] * s + n[1] * mid
            for o in long_runs:
                if o is r:
                    continue
                off = px * o["n"][0] + py * o["n"][1]
                if abs(off - o["mid"]) > o["t"] / 2 + 0.25 / to_m:
                    continue
                so = px * o["d"][0] + py * o["d"][1]
                if o["intervals"][0][0] - 0.25 / to_m <= so \
                        <= o["intervals"][-1][1] + 0.25 / to_m:
                    return True
        return False

    kept = []
    for r in runs:
        ln = length(r)
        if ln >= 1.2 / to_m:
            kept.append(r)
        elif ln < 2 * r["t"] and ln < 0.5 / to_m:
            continue  # blob, not a wall
        elif connected(r):
            kept.append(r)
    return kept


def _classify_opening(cx, cy, width, model_ents, to_m):
    """Returns (kind, evidence_handle). door if a door-swing arc or door
    block is nearby; window if a window block or annotation is nearby;
    else generic 'opening'."""
    near = 1.5 / to_m
    for e in model_ents:
        if e["type"] == "ARC":
            r = e.get("radius", 0)
            ax, ay = e["center"][0], e["center"][1]
            if 0.5 * width <= r <= 1.4 * width \
                    and math.hypot(ax - cx, ay - cy) <= 1.4 * width:
                return "door", e["handle"]
        elif e["type"] == "INSERT":
            bx, by = e["position"][0], e["position"][1]
            if math.hypot(bx - cx, by - cy) <= near:
                name = e["block"].upper()
                if any(k in name for k in DOOR_BLOCK_KW):
                    return "door", None
                if any(k in name for k in WINDOW_BLOCK_KW):
                    return "window", None
        elif e["type"] in ("TEXT", "MTEXT"):
            tx, ty = e["position"][0], e["position"][1]
            if math.hypot(tx - cx, ty - cy) <= near:
                t = e.get("text", "")
                if re.match(r"\s*V\s*[=\d]", t):
                    return "window", None
                if re.match(r"\s*P\s*[=\d]", t):
                    return "door", None
    return "opening", None


def _block_bbox(block_name, blocks, _cache=None, _depth=0):
    """Local-space bbox of a block definition (recursive, cached)."""
    if _cache is None:
        _cache = {}
    if block_name in _cache:
        return _cache[block_name]
    if _depth > 4 or block_name not in blocks:
        return None
    xs, ys = [], []

    def add(x, y):
        xs.append(x); ys.append(y)

    for e in blocks[block_name]["entities"]:
        t = e["type"]
        if t == "LINE":
            add(*e["start"][:2]); add(*e["end"][:2])
        elif t in ("ARC", "CIRCLE"):
            cx, cy = e["center"][:2]; r = e["radius"]
            add(cx - r, cy - r); add(cx + r, cy + r)
        elif t == "LWPOLYLINE":
            for p in e["points"]:
                add(p[0], p[1])
        elif t == "POLYLINE":
            for p in e["points"]:
                add(p[0], p[1])
        elif t == "SPLINE" and e.get("flattened"):
            for p in e["flattened"]:
                add(p[0], p[1])
        elif t == "SOLID":
            for p in e["points"]:
                add(p[0], p[1])
        elif t == "INSERT":
            sub = _block_bbox(e["block"], blocks, _cache, _depth + 1)
            T = e.get("transform")
            if sub and T:  # full matrix: raw scale/rotation would miss the
                for bx in (sub[0], sub[2]):     # nested insert's own OCS
                    for by in (sub[1], sub[3]):
                        add(bx * T[0][0] + by * T[1][0] + T[3][0],
                            bx * T[0][1] + by * T[1][1] + T[3][1])
    bb = (min(xs), min(ys), max(xs), max(ys)) if xs else None
    _cache[block_name] = bb
    return bb


# ═══════════════════════════════════════════════════════════════════════════
# SEMANTIC MODEL (<name>-model.json) — layered, with ownership ledger
#   L1 structure  : walls (paired parallel faces, cross-layer extension)
#   L2 spaces     : grid partition of the wall network — every m² belongs to
#                   exactly one space; labels only NAME spaces
#   L3 openings   : gaps in walls, classified, with space connectivity
#   L4 fixed      : residual linework per space (counters, built-ins) +
#                   hatch floor finishes
#   L5 movable    : block inserts with true outlines (convex hulls)
#   L6 overhead   : dashed/projection linework (beams, roof lines)
#   L7 annotation : texts and dimensions (verification, not geometry)
#   coverage      : the ledger — every entity claimed or explicitly unclaimed
#   All coordinates in METERS, origin at the floor-plan region corner.
# ═══════════════════════════════════════════════════════════════════════════

import numpy as np

DASHED_LT_KW = ("DASH", "HID", "CENTER", "PHANTOM", "DOT", "DIV")
OVERHEAD_LAYER_KW = ("PROYEC", "OVERHEAD", "CEIL", "ROOF")
DRAWABLE = ("LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE",
            "SPLINE", "SOLID", "TRACE", "LEADER")


def _floor_region(segs, to_m):
    """Cluster wall segments spatially; return bbox of the dominant cluster
    (the floor plan) + margin. Elevation/detail views drawn beside the plan
    fall into separate clusters and are excluded."""
    if not segs:
        return None
    link = 2.0 / to_m
    mids = []
    for (x1, y1), (x2, y2) in segs:
        mids.append(((x1 + x2) / 2, (y1 + y2) / 2, math.hypot(x2 - x1, y2 - y1)))
    parent = list(range(len(mids)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    buckets = defaultdict(list)
    for i, (mx, my, _) in enumerate(mids):
        buckets[(int(mx // link), int(my // link))].append(i)
    for (bx, by), idxs in buckets.items():
        for other in ((bx + dx, by + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            for j in buckets.get(other, ()):
                union(idxs[0], j)
        for j in idxs[1:]:
            union(idxs[0], j)

    lengths = defaultdict(float)
    for i, (_, _, ln) in enumerate(mids):
        lengths[find(i)] += ln
    best = max(lengths, key=lengths.get)
    xs, ys = [], []
    for i, ((x1, y1), (x2, y2)) in enumerate(segs):
        if find(i) == best:
            xs += [x1, x2]
            ys += [y1, y2]
    m = 2.5 / to_m
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _endpoint_openings(runs, model_ents, to_m):
    """Doorways at wall-run ENDS (e.g. at a corridor corner) leave no gap
    inside any single run, so interval-based opening detection misses them
    and the space partition leaks through. Find end-to-end gaps of plausible
    opening width that have door/window evidence nearby; they become sealing
    barriers for the partition plus proper opening records."""
    min_ow, max_ow = OPENING_WIDTH_M[0] / to_m, OPENING_WIDTH_M[1] / to_m
    ends = []
    for ri, r in enumerate(runs):
        d, n, mid = r["d"], r["n"], r["mid"]
        for s in (r["intervals"][0][0], r["intervals"][-1][1]):
            ends.append((d[0] * s + n[0] * mid, d[1] * s + n[1] * mid, ri))
    extras, seen = [], set()
    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            x1, y1, ra = ends[i]
            x2, y2, rb = ends[j]
            if ra == rb:
                continue
            gap = math.hypot(x2 - x1, y2 - y1)
            if not (min_ow <= gap <= max_ow):
                continue
            # geometry must look like a doorway — otherwise pairs of
            # unrelated run ends produce phantom barriers that slice rooms:
            # (i) the gap continues a wall direction (door in a wall line), or
            # (ii) the gap runs perpendicular between two parallel walls
            #      (a corridor mouth)
            u = ((x2 - x1) / gap, (y2 - y1) / gap)
            da, db = runs[ra]["d"], runs[rb]["d"]
            par_a = abs(u[0] * da[0] + u[1] * da[1])
            par_b = abs(u[0] * db[0] + u[1] * db[1])
            runs_parallel = abs(da[0] * db[0] + da[1] * db[1]) > 0.97
            if not (par_a > 0.97 or par_b > 0.97
                    or (runs_parallel and par_a < 0.26 and par_b < 0.26)):
                continue
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            kind, evidence = _classify_opening(cx, cy, gap, model_ents, to_m)
            if kind != "door":  # only swing-arc/door evidence is trustworthy
                continue        # enough to justify sealing the partition here
            if any(math.hypot(cx - e["center"][0], cy - e["center"][1])
                   < 0.5 / to_m for e in extras):
                continue  # same physical doorway found via another end pair
            extras.append({"kind": kind, "evidence": evidence,
                           "p1": (x1, y1), "p2": (x2, y2),
                           "width": gap, "center": (cx, cy)})
    return extras


def _arc_endpoints(e):
    """World endpoints of a serialized ARC."""
    if e.get("flattened_wcs"):
        return e["flattened_wcs"][0][:2], e["flattened_wcs"][-1][:2]
    cx, cy = e["center"][:2]
    r = e["radius"]
    a0 = math.radians(e.get("start_angle", 0.0))
    a1 = math.radians(e.get("end_angle", 360.0))
    return ([cx + r * math.cos(a0), cy + r * math.sin(a0)],
            [cx + r * math.cos(a1), cy + r * math.sin(a1)])


def _gap_barriers(runs, to_m):
    """Seal every plausible-width gap in a wall run with a thin barrier segment.
    This is the fallback when there is no door-swing arc or named door block —
    common for files that use anonymous block references (A$Cxxxxxxxx) or draw
    doors without arcs. A thin raster barrier seals the gap in the flood-fill
    without deleting the opening from the semantic model.

    A gap is an opening along the wall direction (d). To seal it we place a
    segment PERPENDICULAR to the wall (along n) at the midpoint of the gap.
    Both endpoints share the same d-coordinate (the gap midpoint), and differ
    only in their n-offset, spanning the full wall thickness. This creates a
    clean door-threshold cut that properly seals the flood-fill leak."""
    min_w = OPENING_WIDTH_M[0] / to_m
    max_w = OPENING_WIDTH_M[1] / to_m
    barriers = []
    for r in runs:
        d, n, mid = r["d"], r["n"], r["mid"]
        for i in range(len(r["intervals"]) - 1):
            a_hi = r["intervals"][i][1]
            b_lo = r["intervals"][i + 1][0]
            gap = b_lo - a_hi
            if not (min_w <= gap <= max_w):
                continue
            # Midpoint of the gap along the wall direction
            s_mid = (a_hi + b_lo) / 2
            # Barrier spans the full wall thickness in the normal direction,
            # with a small extra margin so the rasterizer catches it cleanly.
            hw = r["t"] / 2 + 0.15 / to_m
            # Both points share the same d-component → purely perpendicular cut
            p1 = (d[0] * s_mid + n[0] * (mid - hw),
                  d[1] * s_mid + n[1] * (mid - hw))
            p2 = (d[0] * s_mid + n[0] * (mid + hw),
                  d[1] * s_mid + n[1] * (mid + hw))
            barriers.append((p1, p2))
    return barriers


def _door_barriers(model_ents, blocks, floor_bbox, to_m, cache, runs):
    """Thin sealing segments derived from door geometry. A thin barrier only
    changes the space topology when it bridges two walls — exactly what a
    door does.
    - door-swing ARCs: only radii whose endpoint lands ON a wall — that is
      the closed-door position spanning the doorway. The open-position
      radius (endpoint free in the room) is dropped: with door widths close
      to corridor widths it would slice corridors apart.
    - door blocks: the edges of their placed outline hull"""

    def near_wall(px, py):
        for r in runs:
            d, n, mid, t = r["d"], r["n"], r["mid"], r["t"]
            if abs(px * n[0] + py * n[1] - mid) > t / 2 + 0.12 / to_m:
                continue
            s = px * d[0] + py * d[1]
            lo = r["intervals"][0][0]
            hi = r["intervals"][-1][1]
            if lo - 0.12 / to_m <= s <= hi + 0.12 / to_m:
                return True
        return False

    barriers = []
    r_lo, r_hi = 0.4 / to_m, 1.6 / to_m
    xmin, ymin, xmax, ymax = floor_bbox
    for e in model_ents:
        if e["type"] == "ARC":
            r = e.get("radius", 0)
            cx, cy = e["center"][:2]
            if not (r_lo <= r <= r_hi and xmin <= cx <= xmax
                    and ymin <= cy <= ymax):
                continue
            for p in _arc_endpoints(e):
                if near_wall(p[0], p[1]):
                    barriers.append(((cx, cy), tuple(p)))
        elif e["type"] == "INSERT":
            # Block NAME only, deliberately: a door layer also carries frames,
            # thresholds and hardware whose hulls would seal far more than the
            # doorway. Anonymous door blocks are recovered by _gap_barriers.
            if not any(k in e["block"].upper() for k in DOOR_BLOCK_KW):
                continue
            T = e.get("transform")
            pts = _block_pts(e["block"], blocks, cache)
            if not T or not pts:
                continue
            wpts = [(x * T[0][0] + y * T[1][0] + T[3][0],
                     x * T[0][1] + y * T[1][1] + T[3][1]) for x, y in pts]
            cx = sum(p[0] for p in wpts) / len(wpts)
            cy = sum(p[1] for p in wpts) / len(wpts)
            if not (xmin <= cx <= xmax and ymin <= cy <= ymax):
                continue
            hull = _convex_hull(wpts)
            barriers += [(tuple(hull[i]), tuple(hull[(i + 1) % len(hull)]))
                         for i in range(len(hull))]
    return barriers


def _micro_bridges(runs, to_m):
    """Seal drafting joints: gaps between wall ends narrower than any real
    opening (~0.42 m) cannot be doors or passages — they are imprecision in
    the drawing (frame-to-wall joints, layer seams). Returns thin barrier
    segments (endpoint-to-endpoint and endpoint-to-wall-face)."""
    MAXG = 0.42 / to_m
    ends = []
    for r in runs:
        d, n, mid = r["d"], r["n"], r["mid"]
        for s in (r["intervals"][0][0], r["intervals"][-1][1]):
            ends.append((d[0] * s + n[0] * mid, d[1] * s + n[1] * mid, r))
    bridges = []
    for i in range(len(ends)):
        x1, y1, r1 = ends[i]
        # endpoint -> endpoint
        for j in range(i + 1, len(ends)):
            x2, y2, r2 = ends[j]
            if r1 is r2:
                continue
            g = math.hypot(x2 - x1, y2 - y1)
            if 0.01 / to_m < g <= MAXG:
                bridges.append(((x1, y1), (x2, y2)))
        # endpoint -> another run's face
        for r2 in runs:
            if r2 is r1:
                continue
            d2, n2, mid2, t2 = r2["d"], r2["n"], r2["mid"], r2["t"]
            off = x1 * n2[0] + y1 * n2[1]
            gap = abs(off - mid2) - t2 / 2
            if not (0.01 / to_m < gap <= MAXG):
                continue
            s = x1 * d2[0] + y1 * d2[1]
            if r2["intervals"][0][0] <= s <= r2["intervals"][-1][1]:
                side = 1 if off > mid2 else -1
                fx = d2[0] * s + n2[0] * (mid2 + side * t2 / 2)
                fy = d2[1] * s + n2[1] * (mid2 + side * t2 / 2)
                bridges.append(((x1, y1), (fx, fy)))
    return bridges


def _flood(open_mask, seed_mask):
    """Vectorized 4-neighbor flood fill on a boolean grid."""
    f = seed_mask & open_mask
    if not f.any():
        return f
    prev = -1
    while True:
        cur = int(f.sum())
        if cur == prev:
            return f
        prev = cur
        g = f.copy()
        g[1:, :] |= f[:-1, :]
        g[:-1, :] |= f[1:, :]
        g[:, 1:] |= f[:, :-1]
        g[:, :-1] |= f[:, 1:]
        f = g & open_mask


def _partition(runs, floor_bbox, to_m, barriers=()):
    """Rasterize walls (full run extent — openings sealed) and split the open
    area into exterior + interior connected components (the spaces).
    `barriers` are extra thin segments (doorways) to seal; components below
    room size merge into their largest reachable neighbor."""
    # (see _outdoor_components for why the biggest component is not always a room)
    xmin, ymin, xmax, ymax = floor_bbox
    span = max(xmax - xmin, ymax - ymin)
    cell = max(span / 800.0, 0.02 / to_m)
    min_cells = MIN_ROOM_AREA_M2 / ((cell * to_m) ** 2)
    nx = int((xmax - xmin) / cell) + 3
    ny = int((ymax - ymin) / cell) + 3
    wall = np.zeros((ny, nx), dtype=bool)

    step = cell * 0.6

    def raster(d, n, mid, t, lo, hi):
        ss = np.arange(lo, hi + step, step)
        oo = np.arange(-t / 2 - cell * 0.6, t / 2 + cell * 0.6 + step, step)
        X = d[0] * ss[None, :] + n[0] * (mid + oo[:, None])
        Y = d[1] * ss[None, :] + n[1] * (mid + oo[:, None])
        ix = ((X - xmin) / cell).astype(int)
        iy = ((Y - ymin) / cell).astype(int)
        ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
        wall[iy[ok], ix[ok]] = True

    for r in runs:
        raster(r["d"], r["n"], r["mid"], r["t"],
               r["intervals"][0][0], r["intervals"][-1][1])
    for (x1, y1), (x2, y2) in barriers:
        ln = math.hypot(x2 - x1, y2 - y1)
        if ln <= 0:
            continue
        d = ((x2 - x1) / ln, (y2 - y1) / ln)
        n = (-d[1], d[0])
        mid = x1 * n[0] + y1 * n[1]
        lo = x1 * d[0] + y1 * d[1]
        raster(d, n, mid, cell * 2, lo, lo + ln)

    # dilate 1 cell: seals pinholes from rasterization
    w = wall.copy()
    w[1:, :] |= wall[:-1, :]
    w[:-1, :] |= wall[1:, :]
    w[:, 1:] |= wall[:, :-1]
    w[:, :-1] |= wall[:, 1:]
    wall = w

    open_ = ~wall
    border = np.zeros_like(open_)
    border[0, :] = border[-1, :] = True
    border[:, 0] = border[:, -1] = True
    exterior = _flood(open_, border)

    labels = np.zeros((ny, nx), dtype=np.int32)
    remaining = open_ & ~exterior
    comp_id = 0
    while remaining.any() and comp_id < 80:
        seed_idx = np.argwhere(remaining)[0]
        seed = np.zeros_like(remaining)
        seed[seed_idx[0], seed_idx[1]] = True
        comp = _flood(remaining, seed)
        comp_id += 1
        labels[comp] = comp_id
        remaining &= ~comp

    # merge sub-room fragments into their nearest neighbor: door-swing areas
    # cut off by sealing barriers belong to a real space (real wall cavities
    # stay isolated — a 3-cell growth cannot cross a full wall)
    if min_cells:
        sizes = {c: int((labels == c).sum()) for c in range(1, comp_id + 1)}
        for c in sorted(sizes, key=sizes.get):
            if sizes[c] >= min_cells:
                continue
            mask = labels == c
            grown = mask.copy()
            for _ in range(6):  # must out-grow barrier thickness (~4 cells)
                                # but stay below wall thickness (~7+ cells)
                g = grown.copy()
                g[1:, :] |= grown[:-1, :]
                g[:-1, :] |= grown[1:, :]
                g[:, 1:] |= grown[:, :-1]
                g[:, :-1] |= grown[:, 1:]
                grown = g
            neigh = set(np.unique(labels[grown])) - {0, c}
            if neigh:
                target = max(neigh, key=lambda t2: sizes.get(t2, 0))
                labels[mask] = target
                sizes[target] += sizes[c]
            else:
                labels[mask] = 0  # true void (wall cavity)
            sizes[c] = 0
    return labels, exterior, wall, (xmin, ymin, cell, comp_id)


def _rdp(pts, eps):
    """Douglas-Peucker polyline simplification."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-12
        dmax, imax = 0.0, -1
        for k in range(i + 1, j):
            dist = abs((pts[k][0] - ax) * dy - (pts[k][1] - ay) * dx) / L
            if dist > dmax:
                dmax, imax = dist, k
        if dmax > eps:
            keep[imax] = True
            stack.append((i, imax))
            stack.append((imax, j))
    return [p for p, k in zip(pts, keep) if k]


def _mask_outline(mask, xmin, ymin, cell):
    """Boundary polygon of a grid component via matplotlib contouring."""
    fig, ax = plt.subplots()
    try:
        cs = ax.contour(mask.astype(float), levels=[0.5])
        segs = [s for lvl in cs.allsegs for s in lvl]
    except Exception:
        segs = []
    plt.close(fig)
    if not segs:
        return None
    path = max(segs, key=len)
    pts = [(xmin + float(c) * cell, ymin + float(r) * cell) for c, r in path]
    # contour paths are closed loops (first == last): a straight RDP baseline
    # would be zero-length and collapse everything. Split at the point
    # farthest from the start and simplify the two halves.
    if len(pts) > 3 and math.hypot(pts[0][0] - pts[-1][0],
                                   pts[0][1] - pts[-1][1]) < cell:
        pts = pts[:-1]
    far = max(range(len(pts)),
              key=lambda i: (pts[i][0] - pts[0][0]) ** 2
                            + (pts[i][1] - pts[0][1]) ** 2)
    if far < 2:
        return pts
    a = _rdp(pts[:far + 1], cell * 1.5)
    b = _rdp(pts[far:] + [pts[0]], cell * 1.5)
    return a[:-1] + b[:-1]


def _convex_hull(pts):
    """Andrew monotone chain; returns hull points CCW."""
    pts = sorted(set((round(x, 6), round(y, 6)) for x, y in pts))
    if len(pts) < 3:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def _block_pts(name, blocks, cache, depth=0):
    """All 2D geometry points of a block definition, block-local coords."""
    if name in cache:
        return cache[name]
    if depth > 4 or name not in blocks:
        return []
    pts = []
    for e in blocks[name]["entities"]:
        t = e["type"]
        if t == "LINE":
            pts += [e["start"][:2], e["end"][:2]]
        elif t in ("ARC", "CIRCLE"):
            cx, cy = e["center"][:2]
            r = e["radius"]
            a0 = math.radians(e.get("start_angle", 0.0))
            a1 = math.radians(e.get("end_angle", 360.0))
            if a1 <= a0:
                a1 += 2 * math.pi
            k = 10
            pts += [[cx + r * math.cos(a0 + (a1 - a0) * i / k),
                     cy + r * math.sin(a0 + (a1 - a0) * i / k)]
                    for i in range(k + 1)]
        elif t == "LWPOLYLINE":
            pts += [[p[0], p[1]] for p in (e.get("flattened_wcs")
                                           or e.get("points", []))]
        elif t == "POLYLINE":
            pts += [[p[0], p[1]] for p in e.get("points", [])]
        elif t == "SPLINE":
            pts += [[p[0], p[1]] for p in (e.get("flattened")
                                           or e.get("control_points", []))]
        elif t in ("SOLID", "TRACE"):
            pts += [[p[0], p[1]] for p in e["points"]]
        elif t == "INSERT":
            T = e.get("transform")
            if T:
                pts += [[x * T[0][0] + y * T[1][0] + T[3][0],
                         x * T[0][1] + y * T[1][1] + T[3][1]]
                        for x, y in _block_pts(e["block"], blocks, cache,
                                               depth + 1)]
    if len(pts) > 4000:
        pts = pts[::len(pts) // 4000 + 1]
    cache[name] = pts
    return pts


def _entity_points(e):
    """Flattened 2D world points of a serialized drawable entity."""
    t = e["type"]
    if t == "LINE":
        return [e["start"][:2], e["end"][:2]]
    if t in ("ARC", "CIRCLE"):
        if e.get("flattened_wcs"):
            return [p[:2] for p in e["flattened_wcs"]]
        cx, cy = e["center"][:2]
        r = e["radius"]
        a0 = math.radians(e.get("start_angle", 0.0))
        a1 = math.radians(e.get("end_angle", 360.0))
        if a1 <= a0:
            a1 += 2 * math.pi
        k = 12
        return [[cx + r * math.cos(a0 + (a1 - a0) * i / k),
                 cy + r * math.sin(a0 + (a1 - a0) * i / k)]
                for i in range(k + 1)]
    if t == "LWPOLYLINE":
        return [p[:2] for p in (e.get("flattened_wcs") or e.get("points", []))]
    if t == "POLYLINE":
        return [p[:2] for p in e.get("points", [])]
    if t == "SPLINE":
        return [p[:2] for p in (e.get("flattened") or e.get("control_points", []))]
    if t in ("SOLID", "TRACE"):
        return [p[:2] for p in e.get("points", [])]
    if t == "LEADER":
        return [p[:2] for p in e.get("vertices", [])]
    if t == "ELLIPSE" and e.get("flattened"):
        return [p[:2] for p in e["flattened"]]
    return []


def _claim_wall_entities(runs, model_ents, floor_bbox, to_m):
    """Post-hoc ownership: an entity is claimed by the wall layer if its
    segments lie on a wall run's face/centerline within its coverage."""
    tol = 0.05 / to_m
    claimed = set()
    xmin, ymin, xmax, ymax = floor_bbox
    for e in model_ents:
        if e["type"] not in ("LINE", "LWPOLYLINE", "POLYLINE"):
            continue
        segs = _entity_segments(e)
        if not segs:
            continue
        n_on = 0
        for (x1, y1), (x2, y2) in segs:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            if not (xmin <= mx <= xmax and ymin <= my <= ymax):
                continue
            sx, sy = x2 - x1, y2 - y1
            ln = math.hypot(sx, sy) or 1e-12
            for r in runs:
                d, n, mid, t = r["d"], r["n"], r["mid"], r["t"]
                if abs((sx * d[1] - sy * d[0]) / ln) > 0.03:
                    continue
                off = mx * n[0] + my * n[1]
                dist = abs(off - mid)
                if not (dist <= tol or abs(dist - t / 2) <= tol):
                    continue
                s = mx * d[0] + my * d[1]
                if any(lo - tol <= s <= hi + tol for lo, hi in r["intervals"]):
                    n_on += 1
                    break
        if n_on and n_on >= max(1, len(segs) // 2):
            claimed.add(e["handle"])
    return claimed


def _hint_runs(hints, primary, secondary, to_m):
    """Vision-proposed wall segments (hints.json, drawing units) promoted to
    wall runs — but ONLY where real vector geometry supports them: the hint
    selects and groups existing linework, it never invents coordinates.
    Returns (runs, n_unsupported)."""
    runs, unsupported = [], 0
    segs = list(primary) + list(secondary)
    for h in hints.get("wall_segments", []):
        ax, ay = h["a"]
        bx, by = h["b"]
        L = math.hypot(bx - ax, by - ay)
        if L < (0.2 if h.get("exact") else 0.4) / to_m:
            continue
        d = ((bx - ax) / L, (by - ay) / L)
        n = (-d[1], d[0])
        off0 = ax * n[0] + ay * n[1]
        s_lo = min(ax * d[0] + ay * d[1], bx * d[0] + by * d[1])
        s_hi = max(ax * d[0] + ay * d[1], bx * d[0] + by * d[1])

        # "exact" hints are vectors selected BY ID from the drawing itself
        # (Set-of-Mark rescue) — no snap search needed, they are the truth
        if h.get("exact"):
            runs.append({"d": d, "n": n, "mid": off0,
                         "t": h.get("thickness_m", 0.12) / to_m,
                         "intervals": [[s_lo, s_hi]]})
            continue

        band = 0.5 / to_m  # how far the hint may be off the real wall

        offs, cover = [], 0.0
        for (x1, y1), (x2, y2) in segs:
            sx, sy = x2 - x1, y2 - y1
            ln = math.hypot(sx, sy)
            # only wall-like linework may support a hint: long straight
            # segments. Short furniture strokes must not legitimize a
            # hallucinated wall through a wardrobe.
            if ln < 0.7 / to_m:
                continue
            if abs((sx * d[1] - sy * d[0]) / ln) > 0.09:
                continue  # not parallel to the hint
            om = ((x1 + x2) / 2) * n[0] + ((y1 + y2) / 2) * n[1]
            if abs(om - off0) > band:
                continue
            t1 = x1 * d[0] + y1 * d[1]
            t2 = x2 * d[0] + y2 * d[1]
            lo, hi = max(min(t1, t2), s_lo), min(max(t1, t2), s_hi)
            if hi <= lo:
                continue
            offs.append(om)
            cover += hi - lo
        if cover < 0.45 * L or not offs:
            unsupported += 1
            continue
        spread = max(offs) - min(offs)
        t = spread if WALL_THICKNESS_M[0] / to_m <= spread \
            <= WALL_THICKNESS_M[1] / to_m else \
            (h.get("thickness_m", 0.15) / to_m)
        runs.append({"d": d, "n": n,
                     "mid": sum(offs) / len(offs),
                     "t": t, "intervals": [[s_lo, s_hi]]})
    return runs, unsupported


def _leak_points(start_ix, start_iy, exterior, wallgrid, gx, gy, cell):
    """Where does a failed room's area escape to the outside? BFS from the
    label's cell through the exterior flood to the grid border, then find
    pinch points along the path — narrow passages squeezed between walls.
    Those are the gaps a wall (or door) should close. Returns up to 3
    [x, y] points in drawing units, nearest-to-room first."""
    from collections import deque

    ny, nx = exterior.shape
    if not (0 <= start_ix < nx and 0 <= start_iy < ny):
        return []
    # nudge onto the exterior flood if the label sits on a wall cell
    if not exterior[start_iy, start_ix]:
        found = False
        for r in range(1, 25):
            for dx2, dy2 in ((r, 0), (-r, 0), (0, r), (0, -r),
                             (r, r), (-r, -r), (r, -r), (-r, r)):
                ix2, iy2 = start_ix + dx2, start_iy + dy2
                if 0 <= ix2 < nx and 0 <= iy2 < ny and exterior[iy2, ix2]:
                    start_ix, start_iy, found = ix2, iy2, True
                    break
            if found:
                break
        if not found:
            return []

    # BFS to the border
    parent = {}
    q = deque([(start_ix, start_iy)])
    seen = {(start_ix, start_iy)}
    end = None
    while q:
        ix2, iy2 = q.popleft()
        if ix2 in (0, nx - 1) or iy2 in (0, ny - 1):
            end = (ix2, iy2)
            break
        for dx2, dy2 in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx2, ny2 = ix2 + dx2, iy2 + dy2
            if (nx2, ny2) in seen or not (0 <= nx2 < nx and 0 <= ny2 < ny):
                continue
            if not exterior[ny2, nx2]:
                continue
            seen.add((nx2, ny2))
            parent[(nx2, ny2)] = (ix2, iy2)
            q.append((nx2, ny2))
    if end is None:
        return []
    path = [end]
    while path[-1] in parent:
        path.append(parent[path[-1]])
    path.reverse()  # room -> border

    # pinch detection: corridor width perpendicular to travel direction
    MAXW = int(LEAK_SEAL_MAX_W_M / cell)
    pinches = []
    step = max(1, len(path) // 400)
    for i in range(3, len(path) - 3, step):
        dx2 = path[i + 3][0] - path[i - 3][0]
        dy2 = path[i + 3][1] - path[i - 3][1]
        L = math.hypot(dx2, dy2) or 1
        px, py = -dy2 / L, dx2 / L
        widths = []
        for sgn in (1, -1):
            w = None
            for k in range(1, MAXW):
                jx = int(round(path[i][0] + sgn * px * k))
                jy = int(round(path[i][1] + sgn * py * k))
                if not (0 <= jx < nx and 0 <= jy < ny):
                    break
                if wallgrid[jy, jx]:
                    w = k
                    break
            widths.append(w)
        if widths[0] is not None and widths[1] is not None:
            pinches.append((i, path[i]))

    # cluster consecutive pinch indices, keep each cluster's midpoint
    out = []
    cluster = []
    last_i = None
    for i, cellpos in pinches + [(None, None)]:
        if last_i is not None and (i is None or i - last_i > 8):
            mid = cluster[len(cluster) // 2]
            out.append([gx + mid[0] * cell, gy + mid[1] * cell])
            cluster = []
        if i is not None:
            cluster.append(cellpos)
            last_i = i
    return out[:3]


def _outdoor_components(labels, meta):
    """Component ids that are the OUTDOORS wrapped in, not rooms.

    Once the wall network is complete enough, a site boundary, a plot fence
    or the drawing's own sheet border can close the region *around* the
    building. The flood then reports the yard as an ordinary enclosed
    component — and because it is usually the largest thing on the sheet it
    would both dominate the room list and, worse, make 'encloses the most
    area' pick the strategy that produced it.

    The test is containment plus shape, and both halves are needed:

      - it swallows two or more other components whole. Rooms never nest:
        two rooms' boxes may overlap, but one room does not contain two
        other rooms outright. This alone would still catch an open-plan
        floor wrapped around a service core, hence the second half.
      - it is shaped like surroundings rather than like a room: either it
        spans most of the plan in both directions (the yard around a
        building), or it fills only a small fraction of its own bounding box
        (the sliver network left between unclosed wall stubs). A real room,
        even an L- or U-shaped one, is a compact solid that does not span
        the whole sheet.

    Note a label inside is NOT taken as proof it is a room: on messy plans a
    room name routinely lands in the leftover region, and reporting that
    label as stranded is far better than publishing a 143 m² 'bathroom'."""
    _gx, _gy, _cell, n_comp = meta
    if n_comp < 3:
        return set()
    ny, nx = labels.shape
    boxes, fill = {}, {}
    for cid in range(1, n_comp + 1):
        rows, cols = np.nonzero(labels == cid)
        if not rows.size:
            continue
        x0, y0 = int(cols.min()), int(rows.min())
        x1, y1 = int(cols.max()), int(rows.max())
        boxes[cid] = (x0, y0, x1, y1)
        area = max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
        fill[cid] = rows.size / area

    out = set()
    for cid, (x0, y0, x1, y1) in boxes.items():
        spans = (x1 - x0) >= 0.7 * nx and (y1 - y0) >= 0.7 * ny
        sliver = fill[cid] < 0.35
        if not (spans or sliver):
            continue
        contained = sum(1 for o, (a0, b0, a1, b1) in boxes.items()
                        if o != cid and a0 > x0 and b0 > y0
                        and a1 < x1 and b1 < y1)
        if contained >= 2:
            out.add(cid)
    return out


def _leak_seals(start_ix, start_iy, exterior, wallgrid, gx, gy, cell):
    """Barrier segments (drawing units) that close the pinch-point gaps a
    leaked room's area uses to reach the exterior — the deterministic seal
    the vision rescue would otherwise place by hand. Same BFS-to-border +
    pinch analysis as _leak_points, but returns a wall-to-wall segment across
    each gap instead of just the midpoint, ready to drop into the partition's
    `barriers`. This is what lets an open-plan public zone behind a front door
    (SALA/DINING/KITCHEN) get enclosed without any AI in the loop."""
    from collections import deque

    ny, nx = exterior.shape
    if not (0 <= start_ix < nx and 0 <= start_iy < ny):
        return []
    if not exterior[start_iy, start_ix]:
        found = False
        for r in range(1, 25):
            for dx2, dy2 in ((r, 0), (-r, 0), (0, r), (0, -r),
                             (r, r), (-r, -r), (r, -r), (-r, r)):
                ix2, iy2 = start_ix + dx2, start_iy + dy2
                if 0 <= ix2 < nx and 0 <= iy2 < ny and exterior[iy2, ix2]:
                    start_ix, start_iy, found = ix2, iy2, True
                    break
            if found:
                break
        if not found:
            return []

    parent = {}
    q = deque([(start_ix, start_iy)])
    seen = {(start_ix, start_iy)}
    end = None
    while q:
        ix2, iy2 = q.popleft()
        if ix2 in (0, nx - 1) or iy2 in (0, ny - 1):
            end = (ix2, iy2)
            break
        for dx2, dy2 in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx2, ny2 = ix2 + dx2, iy2 + dy2
            if (nx2, ny2) in seen or not (0 <= nx2 < nx and 0 <= ny2 < ny):
                continue
            if not exterior[ny2, nx2]:
                continue
            seen.add((nx2, ny2))
            parent[(nx2, ny2)] = (ix2, iy2)
            q.append((nx2, ny2))
    if end is None:
        return []
    path = [end]
    while path[-1] in parent:
        path.append(parent[path[-1]])
    path.reverse()  # room -> border

    MAXW = int(LEAK_SEAL_MAX_W_M / cell)
    segs, last = [], -99
    step = max(1, len(path) // 400)
    for i in range(3, len(path) - 3, step):
        dx2 = path[i + 3][0] - path[i - 3][0]
        dy2 = path[i + 3][1] - path[i - 3][1]
        L = math.hypot(dx2, dy2) or 1
        px, py = -dy2 / L, dx2 / L  # perpendicular to travel
        w = [None, None]
        for si, sgn in enumerate((1, -1)):
            for k in range(1, MAXW):
                jx = int(round(path[i][0] + sgn * px * k))
                jy = int(round(path[i][1] + sgn * py * k))
                if not (0 <= jx < nx and 0 <= jy < ny):
                    break
                if wallgrid[jy, jx]:
                    w[si] = k
                    break
        # a real hole: walls on BOTH sides within corridor width -> seal it
        if w[0] is not None and w[1] is not None and i - last > 8:
            cx, cy = path[i]
            x1 = gx + (cx + px * (w[0] + 3)) * cell
            y1 = gy + (cy + py * (w[0] + 3)) * cell
            x2 = gx + (cx - px * (w[1] + 3)) * cell
            y2 = gy + (cy - py * (w[1] + 3)) * cell
            segs.append(((x1, y1), (x2, y2)))
            last = i
    return segs[:3]


def _is_overhead(e, layer_linetypes):
    lt = (e.get("linetype") or layer_linetypes.get(e["layer"], "") or "").upper()
    lyr = e["layer"].upper()
    return (any(k in lt for k in DASHED_LT_KW)
            or any(k in lyr for k in OVERHEAD_LAYER_KW))


def build_semantic_model(model_ents, blocks, units, extents, layers_table,
                         hints=None):
    to_m = units["to_meters"] or 1.0
    layer_linetypes = {l["name"]: l.get("linetype", "") for l in layers_table}
    hints = hints or {}
    hint_stats = {"applied": 0, "unsupported": 0}

    # ---- L1+L2: walls and space partition, self-validated ----
    # Try each wall-layer strategy end-to-end (region -> pairing -> door
    # sealing -> partition) and keep the one that encloses the most floor
    # area. Wrong layer choices produce either almost no walls or phantom
    # walls everywhere — both enclose little coherent space.
    huge = (-1e18, -1e18, 1e18, 1e18)
    block_cache = {}
    fallback_bbox = (extents[0][0], extents[0][1], extents[1][0], extents[1][1])
    classify = lambda cx, cy, w: _classify_opening(cx, cy, w,
                                                   model_ents, to_m)[0]
    best = None
    hatch_faces_all = _hatch_wall_faces(model_ents, to_m)
    # room-name label positions: used both to tell the yard from a real room
    # while scoring, and to auto-seal stranded rooms further down
    room_texts = [(e["position"][0], e["position"][1]) for e in model_ents
                  if e["type"] in ("TEXT", "MTEXT")
                  and ROOM_NAME_HINT.search((e.get("text") or "").strip())]
    for wall_layers in (_wall_layer_strategies(model_ents) or [set()]):
        primary_all, _ = _wall_candidates(model_ents, huge, wall_layers)
        floor_bbox = _floor_region(primary_all, to_m) or fallback_bbox
        primary_drawn, secondary = _wall_candidates(model_ents, floor_bbox,
                                                    wall_layers)
        hatch_faces = [((x1, y1), (x2, y2))
                       for (x1, y1), (x2, y2) in hatch_faces_all
                       if floor_bbox[0] <= (x1 + x2) / 2 <= floor_bbox[2]
                       and floor_bbox[1] <= (y1 + y2) / 2 <= floor_bbox[3]]
        # Two more axes of the same self-validation: whether poche hatch bands
        # count as wall faces, and whether unpaired wall-layer linework is
        # promoted to single-line walls. Files that draw every wall as a
        # double line score identically on all of them and keep the plain
        # pass; files that draw their envelope as poche or as one line only
        # enclose their rooms once that source is included.
        for use_hatch in ((False, True) if hatch_faces else (False,)):
            primary = primary_drawn + (hatch_faces if use_hatch else [])
            for use_singles in (False, True):
                runs = _pair_walls(primary, to_m)
                if use_singles:
                    extra = _single_line_runs(primary, runs, to_m)
                    if not extra:
                        continue  # identical to the pass already scored
                    runs = runs + extra
                runs = _extend_runs(runs, secondary, to_m, classify)
                runs = _supplement_perimeter_runs(runs, secondary, floor_bbox,
                                                  to_m)
                runs = _seal_perimeter_gaps(runs, secondary, to_m)
                if hints.get("wall_segments"):
                    h_runs, h_bad = _hint_runs(hints, primary, secondary, to_m)
                    runs = runs + h_runs
                    hint_stats["applied"] = len(h_runs)
                    hint_stats["unsupported"] = h_bad
                end_openings = _endpoint_openings(runs, model_ents, to_m)
                barriers = [(e["p1"], e["p2"]) for e in end_openings]
                barriers += _door_barriers(model_ents, blocks, floor_bbox,
                                           to_m, block_cache, runs)
                barriers += _micro_bridges(runs, to_m)
                # doorway seals confirmed externally (atlas ops / vision
                # voting): a barrier splits the spaces like a door swing does,
                # without inventing a wall — drawing units, as wall_segments
                barriers += [(tuple(s["a"]), tuple(s["b"]))
                             for s in hints.get("door_barriers", [])]
                # Fallback: seal any remaining plausible-width gaps that door
                # detection missed (anonymous blocks, no swing arcs, etc.)
                gap_b = _gap_barriers(runs, to_m)
                gap_b_set = {(round(p1[0],3), round(p1[1],3),
                              round(p2[0],3), round(p2[1],3))
                             for p1, p2 in barriers}
                barriers += [(p1, p2) for p1, p2 in gap_b
                             if (round(p1[0],3), round(p1[1],3),
                                 round(p2[0],3), round(p2[1],3))
                             not in gap_b_set]
                labels, exterior, wallgrid, meta = \
                    _partition(runs, floor_bbox, to_m, barriers=barriers)
                # score on ROOM area only: sealing the yard or the sheet
                # border encloses a lot of area without finding any room, and
                # must not be able to win the comparison
                scored = labels > 0
                for cid in _outdoor_components(labels, meta):
                    scored &= labels != cid
                enclosed_m2 = float(scored.sum()) * (meta[2] * to_m) ** 2
                if best is None or enclosed_m2 > best["score"]:
                    best = {"score": enclosed_m2, "layers": wall_layers,
                            "floor_bbox": floor_bbox, "runs": runs,
                            "end_openings": end_openings, "labels": labels,
                            "exterior": exterior, "wallgrid": wallgrid,
                            "meta": meta, "barriers": barriers,
                            "single_line_walls": use_singles,
                            "hatch_walls": use_hatch}

    # ── auto-seal labeled leaks ──────────────────────────────────────────
    # A room whose NAME text is stranded in the exterior flood is a room the
    # partition lost through an unsealed gap (open-plan public zones behind a
    # front door, or a perimeter drawn with gaps). Deterministically seal the
    # pinch points its area uses to escape — the same gaps the vision rescue
    # would close — and re-partition. Only fires when a ROOM_NAME label is
    # actually stranded, so plans that already resolve cleanly are untouched;
    # kept only if it encloses strictly more area than before.
    def _room_cells(labels_x, meta_x):
        """Enclosed cells that are actually rooms — the outdoors, if a seal
        happened to wrap it, must not count as progress."""
        m = labels_x > 0
        for cid in _outdoor_components(labels_x, meta_x):
            m &= labels_x != cid
        return float(m.sum())

    if room_texts:
        runs_b, fb_b = best["runs"], best["floor_bbox"]
        base_area = _room_cells(best["labels"], best["meta"])
        barriers_cur = list(best["barriers"])
        cur = {k: best[k] for k in ("labels", "exterior", "wallgrid", "meta")}
        for _ in range(5):
            labels_c, ext_c, wall_c, meta_c = (cur["labels"], cur["exterior"],
                                               cur["wallgrid"], cur["meta"])
            gxb, gyb, cellb = meta_c[0], meta_c[1], meta_c[2]
            nyb, nxb = labels_c.shape
            seals = []
            for lx, ly in room_texts:
                ix, iy = int((lx - gxb) / cellb), int((ly - gyb) / cellb)
                if 0 <= ix < nxb and 0 <= iy < nyb and ext_c[iy, ix]:
                    seals += _leak_seals(ix, iy, ext_c, wall_c, gxb, gyb, cellb)
            if not seals:
                break
            barriers_cur = barriers_cur + seals
            l2, e2, w2, m2 = _partition(runs_b, fb_b, to_m, barriers=barriers_cur)
            cur = {"labels": l2, "exterior": e2, "wallgrid": w2, "meta": m2}
        if _room_cells(cur["labels"], cur["meta"]) > base_area:
            best.update(cur)

    floor_bbox = best["floor_bbox"]
    runs = _filter_stub_runs(_merge_runs(best["runs"], to_m), to_m)
    end_openings = best["end_openings"]
    labels, exterior, wallgrid = best["labels"], best["exterior"], best["wallgrid"]
    gx, gy, cell, n_comp = best["meta"]
    ny, nx = labels.shape
    ox, oy = floor_bbox[0], floor_bbox[1]

    def M(x, y):
        return [round((x - ox) * to_m, 4), round((y - oy) * to_m, 4)]

    claimed_wall = _claim_wall_entities(runs, model_ents, floor_bbox, to_m)

    def cell_of(x, y):
        ix, iy = int((x - gx) / cell), int((y - gy) / cell)
        if not (0 <= ix < nx and 0 <= iy < ny):
            return "outside"
        if exterior[iy, ix]:
            return "exterior"
        if wallgrid[iy, ix]:
            return "wall"
        return int(labels[iy, ix])

    def sp_of(c):
        return c if isinstance(c, str) else comp_to_space.get(c, "void")

    def probe(cx, cy, nx_, ny_, start):
        """Space on one side of a wall: step outward past the dilated wall
        cells until we land in open space."""
        for k in range(6):
            c = cell_of(cx + nx_ * (start + k * cell * 2),
                        cy + ny_ * (start + k * cell * 2))
            if c != "wall":
                return sp_of(c)
        return "wall"

    min_cells = MIN_ROOM_AREA_M2 / ((cell * to_m) ** 2)
    outdoors = _outdoor_components(labels, best["meta"])
    spaces, comp_to_space, voids = [], {}, 0
    for cid in range(1, n_comp + 1):
        if cid in outdoors:
            continue          # the yard/site the flood got sealed into
        mask = labels == cid
        count = int(mask.count_nonzero()) if hasattr(mask, "count_nonzero") \
            else int(mask.sum())
        if count < min_cells:
            voids += 1
            continue
        outline = _mask_outline(mask, gx, gy, cell)
        rows, cols = np.nonzero(mask)
        cx = gx + float(cols.mean()) * cell
        cy = gy + float(rows.mean()) * cell
        sid = f"S{len(spaces) + 1}"
        comp_to_space[cid] = sid
        spaces.append({
            "id": sid,
            "name": None,        # filled from labels below
            "labels": [],
            "area_m2": round(count * (cell * to_m) ** 2, 3),
            "centroid": M(cx, cy),
            "polygon": [M(x, y) for x, y in (outline or [])],
        })

    # name spaces from text labels inside them. A text's insertion point is
    # the corner of its box and often sits on a wall or in a door-swing
    # area — if it lands outside every space, snap to the nearest space
    # within a small ring search.
    warnings = []
    failed_labels = []
    for e in model_ents:
        if e["type"] not in ("TEXT", "MTEXT") or not e.get("text", "").strip():
            continue
        x, y = e["position"][0], e["position"][1]
        txt = e["text"].strip()
        c = cell_of(x, y)
        if not (isinstance(c, int) and c in comp_to_space):
            for radius in (0.2, 0.4, 0.7, 1.0):
                for k in range(8):
                    a = k * math.pi / 4
                    c2 = cell_of(x + radius / to_m * math.cos(a),
                                 y + radius / to_m * math.sin(a))
                    if isinstance(c2, int) and c2 in comp_to_space:
                        c = c2
                        break
                if isinstance(c, int) and c in comp_to_space:
                    break
        if isinstance(c, int) and c in comp_to_space:
            sp = next(s for s in spaces if s["id"] == comp_to_space[c])
            sp["labels"].append(txt)
        elif ROOM_NAME_HINT.search(txt):
            warnings.append(f"room label '{txt}' matched no space — possible "
                            f"leak through an unsealed opening")
            failed_labels.append((txt, x, y))
    def looks_like_name(t):
        """A label usable as a room name: not a dimension (P=0.90X2.15),
        level mark (NPT +0.10), or measurement string."""
        t = t.strip()
        return (0 < len(t) <= 30
                and "=" not in t and "±" not in t
                and not t.upper().startswith(("NPT", "N.P.T"))
                and not re.fullmatch(r"[\d\s.,xX×+\-]+", t))

    # for every failed label, locate WHERE its area escapes to the exterior
    # (rescue.py aims vision questions at these exact gaps)
    leaks = []
    for txt, lx, ly in failed_labels:
        pts = _leak_points(int((lx - gx) / cell), int((ly - gy) / cell),
                           exterior, wallgrid, gx, gy, cell)
        if pts:  # drawing units -> model meters, like everything in the JSON
            leaks.append({"label": txt,
                          "points": [[round((p[0] - ox) * to_m, 3),
                                      round((p[1] - oy) * to_m, 3)]
                                     for p in pts]})

    for i, sp in enumerate(spaces):
        hint_labels = [t for t in sp["labels"] if ROOM_NAME_HINT.search(t)]
        if not hint_labels:  # no keyword hit: any clean label beats "space S7"
            hint_labels = [t for t in sp["labels"] if looks_like_name(t)]
        seen = set()
        names = [h.strip().lower() for h in hint_labels
                 if not (h.strip().lower() in seen or seen.add(h.strip().lower()))]
        sp["name"] = "/".join(names) if names else f"space {sp['id']}"
    spaces.sort(key=lambda s: -s["area_m2"])

    # ---- L1 walls + L3 openings (with connectivity) ----
    min_ow, max_ow = OPENING_WIDTH_M[0] / to_m, OPENING_WIDTH_M[1] / to_m
    walls, openings = [], []
    run_wall = []  # (run, wall_id) for the object-opening pass below
    claimed_arcs = set()
    for i, r in enumerate(sorted(runs, key=lambda r: -(r["intervals"][-1][1]
                                                       - r["intervals"][0][0]))):
        d, n, mid, t = r["d"], r["n"], r["mid"], r["t"]

        def pt(s):
            return (d[0] * s + n[0] * mid, d[1] * s + n[1] * mid)

        lo, hi = r["intervals"][0][0], r["intervals"][-1][1]
        wid = f"W{i + 1}"
        wall_openings = []
        for (a_lo, a_hi), (b_lo, b_hi) in zip(r["intervals"], r["intervals"][1:]):
            gap = b_lo - a_hi
            if not (min_ow <= gap <= max_ow):
                continue
            s = (a_hi + b_lo) / 2
            cx, cy = pt(s)
            kind, evidence = _classify_opening(cx, cy, gap, model_ents, to_m)
            if evidence:
                claimed_arcs.add(evidence)
            sides = [probe(cx, cy, n[0], n[1], t / 2 + 3 * cell),
                     probe(cx, cy, -n[0], -n[1], t / 2 + 3 * cell)]
            oid = f"O{len(openings) + 1}"
            op = {"id": oid, "wall": wid, "type": kind,
                  "at_m": round((a_hi - lo) * to_m, 3),
                  "width_m": round(gap * to_m, 3),
                  "center": M(cx, cy),
                  "dir": [round(d[0], 4), round(d[1], 4)],
                  "connects": sides}
            openings.append(op)
            wall_openings.append(oid)
        run_wall.append((r, wid))
        (sx0, sy0), (ex0, ey0) = pt(lo), pt(hi)
        hx, hy = n[0] * t / 2, n[1] * t / 2
        walls.append({
            "id": wid,
            "start": M(sx0, sy0), "end": M(ex0, ey0),
            "length_m": round((hi - lo) * to_m, 3),
            "thickness_m": round(t * to_m, 3),
            "polygon": [M(sx0 - hx, sy0 - hy), M(ex0 - hx, ey0 - hy),
                        M(ex0 + hx, ey0 + hy), M(sx0 + hx, sy0 + hy)],
            "openings": wall_openings,
        })

    # endpoint doorways (sealed as barriers above) are openings too
    for e2 in end_openings:
        if e2["evidence"]:
            claimed_arcs.add(e2["evidence"])
        (x1, y1), (x2, y2) = e2["p1"], e2["p2"]
        ln = math.hypot(x2 - x1, y2 - y1) or 1e-12
        pn = (-(y2 - y1) / ln, (x2 - x1) / ln)
        cx, cy = e2["center"]
        openings.append({
            "id": f"O{len(openings) + 1}", "wall": None, "type": e2["kind"],
            "at_m": None, "width_m": round(e2["width"] * to_m, 3),
            "center": M(cx, cy),
            "dir": [round((x2 - x1) / ln, 4), round((y2 - y1) / ln, 4)],
            "connects": [probe(cx, cy, pn[0], pn[1], 3 * cell),
                         probe(cx, cy, -pn[0], -pn[1], 3 * cell)],
        })

    # an opening that connects no space on either side is noise from the
    # endpoint pairing — drop it (and its references in walls)
    openings = [o for o in openings
                if any(str(c).startswith("S") or c == "exterior"
                       for c in o["connects"])]
    kept_ids = {o["id"] for o in openings}
    for w in walls:
        w["openings"] = [i for i in w["openings"] if i in kept_ids]

    # ---- L5: movable (block inserts, true outlines) ----
    movable = []
    door_window_objs = []  # (category, hull in drawing units) — openings pass
    claimed_inserts = set()
    cache = block_cache
    seen_inserts = set()
    for e in model_ents:
        if e["type"] != "INSERT":
            continue
        T = e.get("transform")
        pts = _block_pts(e["block"], blocks, cache)
        if not T or not pts:
            continue
        wpts = [(x * T[0][0] + y * T[1][0] + T[3][0],
                 x * T[0][1] + y * T[1][1] + T[3][1]) for x, y in pts]
        cx = sum(p[0] for p in wpts) / len(wpts)
        cy = sum(p[1] for p in wpts) / len(wpts)
        if not (floor_bbox[0] <= cx <= floor_bbox[2]
                and floor_bbox[1] <= cy <= floor_bbox[3]):
            continue
        claimed_inserts.add(e["handle"])
        key = (e["block"], round(cx, 2), round(cy, 2))
        if key in seen_inserts:
            continue
        seen_inserts.add(key)
        hull = _convex_hull(wpts)
        hxs = [p[0] for p in hull]
        hys = [p[1] for p in hull]
        if hull and max(max(hxs) - min(hxs),
                        max(hys) - min(hys)) > 6.0 / to_m:
            hull = None  # scattered geometry (block reused in elevations)
        det = T[0][0] * T[1][1] - T[0][1] * T[1][0]
        rot = math.degrees(math.atan2(T[0][1], T[0][0])) % 360
        up = e["block"].upper()
        category = ("door" if any(k in up for k in DOOR_BLOCK_KW) else
                    "window" if any(k in up for k in WINDOW_BLOCK_KW) else
                    "furniture")
        if category == "furniture":
            # name told us nothing (anonymous/localized blocks): a block
            # containing a door-sized swing arc IS a door
            scale = math.sqrt(abs(det)) or 1.0
            for be in blocks.get(e["block"], {}).get("entities", ()):
                if be["type"] != "ARC":
                    continue
                r_w = be.get("radius", 0) * scale
                span = (be.get("end_angle", 0) - be.get("start_angle", 0)) % 360
                if 0.4 / to_m <= r_w <= 1.6 / to_m and 45 <= span <= 190:
                    category = "door"
                    break
        c = cell_of(cx, cy)
        if category in ("door", "window") and hull:
            door_window_objs.append((category, hull))
        movable.append({
            "id": f"M{len(movable) + 1}",
            "block": e["block"], "category": category,
            "space": sp_of(c),
            "position": M(cx, cy),
            "rotation_deg": round(rot, 2),
            "mirrored": det < 0,
            "outline": [M(x, y) for x, y in hull] if hull else None,
        })

    # ---- L3b: openings from door/window OBJECTS ----
    # Many drawings never leave a measurable gap in the wall linework — the
    # door/window block or swing arc is then the only evidence an opening
    # exists. Project each such object onto its nearest wall run.
    def nearest_run(px, py, max_face_dist):
        found, found_s = None, None
        bestd = max_face_dist
        for r2, wid2 in run_wall:
            d2, n2, mid2, t2 = r2["d"], r2["n"], r2["mid"], r2["t"]
            face_dist = abs(px * n2[0] + py * n2[1] - mid2) - t2 / 2
            s = px * d2[0] + py * d2[1]
            lo2 = r2["intervals"][0][0]
            hi2 = r2["intervals"][-1][1]
            if lo2 - 0.3 / to_m <= s <= hi2 + 0.3 / to_m \
                    and face_dist < bestd:
                bestd = face_dist
                found, found_s = (r2, wid2), s
        return found, found_s

    object_candidates = [(cat, hull, None) for cat, hull in door_window_objs]
    for e in model_ents:  # door-swing arcs drawn directly (not in a block)
        if e["type"] == "ARC" and e["handle"] not in claimed_arcs:
            r0 = e.get("radius", 0)
            if 0.4 / to_m <= r0 <= 1.6 / to_m:
                cx0, cy0 = e["center"][:2]
                if floor_bbox[0] <= cx0 <= floor_bbox[2] \
                        and floor_bbox[1] <= cy0 <= floor_bbox[3]:
                    p0, p1 = _arc_endpoints(e)
                    object_candidates.append(
                        ("door", [[cx0, cy0], list(p0), list(p1)], "arc"))

    wall_by_id = {w["id"]: w for w in walls}
    for cat, hull_u, kind_src in object_candidates:
        hcx = sum(p[0] for p in hull_u) / len(hull_u)
        hcy = sum(p[1] for p in hull_u) / len(hull_u)
        rr, _ = nearest_run(hcx, hcy, 0.6 / to_m)
        if not rr:
            continue
        r2, wid2 = rr
        d2, n2, mid2, t2 = r2["d"], r2["n"], r2["mid"], r2["t"]
        if kind_src == "arc":
            # hinge + the swing endpoint lying on this wall span the doorway
            hinge, p0, p1 = hull_u
            on_wall = min((p0, p1), key=lambda p: abs(
                p[0] * n2[0] + p[1] * n2[1] - mid2))
            if abs(on_wall[0] * n2[0] + on_wall[1] * n2[1] - mid2) \
                    > t2 / 2 + 0.15 / to_m:
                continue  # neither endpoint touches this wall: not its door
            s0 = hinge[0] * d2[0] + hinge[1] * d2[1]
            s1 = on_wall[0] * d2[0] + on_wall[1] * d2[1]
            width = abs(s1 - s0)
            s = (s0 + s1) / 2
        else:
            ss = [p[0] * d2[0] + p[1] * d2[1] for p in hull_u]
            width = max(ss) - min(ss)
            s = (max(ss) + min(ss)) / 2
        if not (min_ow <= width <= max_ow):
            continue
        ocx = d2[0] * s + n2[0] * mid2
        ocy = d2[1] * s + n2[1] * mid2
        cm = M(ocx, ocy)
        if any(math.hypot(cm[0] - o["center"][0],
                          cm[1] - o["center"][1]) < 0.6 for o in openings):
            continue  # already recorded via a wall gap or endpoint pair
        connects = [probe(ocx, ocy, n2[0], n2[1], t2 / 2 + 3 * cell),
                    probe(ocx, ocy, -n2[0], -n2[1], t2 / 2 + 3 * cell)]
        if not any(str(c2).startswith("S") or c2 == "exterior"
                   for c2 in connects):
            continue
        oid = f"O{len(openings) + 1}"
        openings.append({
            "id": oid, "wall": wid2, "type": cat,
            "at_m": round((s - r2["intervals"][0][0]) * to_m, 3),
            "width_m": round(width * to_m, 3),
            "center": cm,
            "dir": [round(d2[0], 4), round(d2[1], 4)],
            "connects": connects,
        })
        wall_by_id[wid2]["openings"].append(oid)

    # ---- L4/L6: residual linework -> fixed (per space) or overhead ----
    fixed, overhead, finishes = [], [], []
    claimed_other, outside, unclaimed = set(), 0, []
    for e in model_ents:
        t = e["type"]
        h = e["handle"]
        if t == "HATCH":
            b = e.get("boundaries") or []
            if b and b[0]["points"]:
                pts0 = b[0]["points"]
                cx = sum(p[0] for p in pts0) / len(pts0)
                cy = sum(p[1] for p in pts0) / len(pts0)
                c = cell_of(cx, cy)
                finishes.append({
                    "space": sp_of(c),
                    "pattern": e.get("pattern"),
                    "area_m2": round((e.get("area") or 0) * to_m * to_m, 3),
                })
                claimed_other.add(h)
            continue
        if t in ("TEXT", "MTEXT", "DIMENSION"):
            claimed_other.add(h)  # annotation layer, collected below
            continue
        if t == "INSERT":
            if h not in claimed_inserts:
                unclaimed.append({"handle": h, "type": t, "layer": e["layer"]})
            continue
        if t not in DRAWABLE:
            unclaimed.append({"handle": h, "type": t, "layer": e["layer"]})
            continue
        if h in claimed_wall or h in claimed_arcs:
            continue
        pts = _entity_points(e)
        if not pts:
            unclaimed.append({"handle": h, "type": t, "layer": e["layer"]})
            continue
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        if not (floor_bbox[0] <= mx <= floor_bbox[2]
                and floor_bbox[1] <= my <= floor_bbox[3]):
            outside += 1
            continue
        entry_pts = [M(x, y) for x, y in _rdp([(p[0], p[1]) for p in pts],
                                              0.01 / to_m)]
        if _is_overhead(e, layer_linetypes):
            overhead.append({"layer": e["layer"], "type": t,
                             "points": entry_pts})
            claimed_other.add(h)
        else:
            c = cell_of(mx, my)
            fixed.append({
                "space": sp_of(c),
                "layer": e["layer"], "type": t,
                "points": entry_pts,
            })
            claimed_other.add(h)

    # ---- L7: annotation ----
    annotation = []
    for e in model_ents:
        if e["type"] in ("TEXT", "MTEXT") and e.get("text", "").strip():
            x, y = e["position"][0], e["position"][1]
            if floor_bbox[0] <= x <= floor_bbox[2] \
                    and floor_bbox[1] <= y <= floor_bbox[3]:
                annotation.append({"text": e["text"].strip(),
                                   "position": M(x, y),
                                   "layer": e["layer"]})
        elif e["type"] == "DIMENSION":
            annotation.append({"dimension": e.get("measurement"),
                               "text": e.get("text_override"),
                               "layer": e["layer"]})

    # ---- coverage ledger ----
    in_region = sum(1 for e in model_ents if e["type"] in DRAWABLE + ("INSERT",
                    "HATCH", "TEXT", "MTEXT", "DIMENSION"))
    coverage = {
        "total_entities_modelspace": len(model_ents),
        "claimed": {
            "structure_walls": len(claimed_wall),
            "openings_evidence": len(claimed_arcs),
            "movable_blocks": len(claimed_inserts),
            "fixed_residual": len(fixed),
            "overhead": len(overhead),
            "finishes": len(finishes),
            "annotation": len(annotation),
        },
        "outside_floor_region": outside,
        "void_cells_dropped": voids,
        "unclaimed_count": len(unclaimed),
        "unclaimed_sample": unclaimed[:40],
        "warnings": warnings,
    }

    if hints:
        coverage["hints"] = hint_stats
    if leaks:
        coverage["leaks"] = leaks
    return {
        "units": "meters",
        "origin_offset_drawing_units": [round(ox, 4), round(oy, 4)],
        "to_meters_factor": to_m,
        "defaults": {"wall_height_m": DEFAULT_WALL_HEIGHT_M, "assumed": True},
        "floor_bounds_m": [M(floor_bbox[0], floor_bbox[1]),
                           M(floor_bbox[2], floor_bbox[3])],
        "walls": walls,
        "spaces": spaces,
        "openings": openings,
        "fixed": fixed,
        "movable": movable,
        "overhead": overhead,
        "finishes": finishes,
        "annotation": annotation,
        "coverage": coverage,
    }


SPACE_COLORS = ["#bfdbfe", "#bbf7d0", "#fde68a", "#fbcfe8", "#ddd6fe",
                "#a5f3fc", "#fed7aa", "#d9f99d", "#e5e7eb", "#fecaca"]


def render_model_debug(model, out_png):
    """Overlay plot of the layered model for visual verification."""
    fig, ax = plt.subplots(figsize=(15, 11))
    ax.set_aspect("equal")
    for i, s in enumerate(model["spaces"]):
        if not s["polygon"]:
            continue
        pts = s["polygon"] + [s["polygon"][0]]
        ax.fill([p[0] for p in pts], [p[1] for p in pts],
                color=SPACE_COLORS[i % len(SPACE_COLORS)], alpha=0.7,
                linewidth=0)
        ax.text(s["centroid"][0], s["centroid"][1],
                f'{s["name"]}\n{s["area_m2"]} m²',
                ha="center", va="center", fontsize=8, color="#1f2328")
    for f in model["fixed"]:
        pts = f["points"]
        if len(pts) >= 2:
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    color="#92400e", linewidth=0.5)
    for o in model["overhead"]:
        pts = o["points"]
        if len(pts) >= 2:
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    color="#9ca3af", linewidth=0.6, linestyle="--")
    for w in model["walls"]:
        pts = w["polygon"] + [w["polygon"][0]]
        ax.fill([p[0] for p in pts], [p[1] for p in pts], color="#374151")
    for m in model["movable"]:
        if m["outline"]:
            pts = m["outline"] + [m["outline"][0]]
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    color="#15803d", linewidth=0.8)
    for o in model["openings"]:
        c = {"door": "#dc2626", "window": "#0891b2",
             "opening": "#d97706"}[o["type"]]
        cx, cy = o["center"]
        if o.get("dir"):  # draw the opening as its actual span along the wall
            dx, dy = o["dir"]
            hw = o["width_m"] / 2
            ax.plot([cx - dx * hw, cx + dx * hw],
                    [cy - dy * hw, cy + dy * hw],
                    color=c, linewidth=4, solid_capstyle="butt", zorder=5)
        else:
            ax.plot(cx, cy, "o", color=c, markersize=6)
    cov = model["coverage"]
    ax.set_title("Layered model — spaces (fill), walls (dark), fixed (brown), "
                 "movable (green), overhead (dashed) | "
                 f"unclaimed: {cov['unclaimed_count']}, "
                 f"warnings: {len(cov['warnings'])}")
    plt.tight_layout()
    plt.savefig(out_png, dpi=150)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════════
# RENDERING (ezdxf drawing add-on: real layer colors, blocks, text, hatches)
# ═══════════════════════════════════════════════════════════════════════════

def render_png(doc, msp, out_png, extents=None):
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    fig = plt.figure(figsize=(16, 12))
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend).draw_layout(msp, finalize=True)
    if extents:  # clamp to drawing extents: stray far-away entities otherwise
        (xmin, ymin), (xmax, ymax) = extents  # zoom the auto-fit out to nothing
        mx, my = (xmax - xmin) * 0.02, (ymax - ymin) * 0.02
        ax.set_xlim(xmin - mx, xmax + mx)
        ax.set_ylim(ymin - my, ymax + my)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ═══════════════════════════════════════════════════════════════════════════

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_table(headers, rows):
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
                for r in rows)
    return (f'<table class="tbl"><thead><tr>{h}</tr></thead>'
            f'<tbody>{b}</tbody></table>')


def build_report(data, png_name):
    src = data["source"]["file"]
    s = data["summary"]
    units = data["units"]
    unit = units["unit"]

    sections = []

    def sec(title, content):
        sections.append(f"<section><h2>{esc(title)}</h2>{content}</section>")

    cards = [
        (s["total_entities"], "Entities (all layouts)"),
        (len(data["layers"]), "Layers"),
        (len(data["blocks"]), "Block Definitions"),
        (len(data["rooms"]), "Rooms Detected"),
        (f'{s["total_room_area_m2"]:.1f} m²' if s["total_room_area_m2"]
         else "—", "Total Room Area"),
        (unit + (" (guessed)" if units["guessed"] else ""), "Drawing Unit"),
    ]
    card_html = "".join(
        f'<div class="card"><span class="num">{esc(n)}</span><br>{esc(l)}</div>'
        for n, l in cards)
    sec("Summary", f'<div class="cards">{card_html}</div>')

    if units.get("note"):
        sec("Units", f'<p class="warn">&#9888; {esc(units["note"])}</p>')

    sec("Rendering", f'<img src="{esc(png_name)}" style="max-width:100%;'
                     f'border:1px solid #e5e7eb;border-radius:8px">')

    if data["rooms"]:
        rows = [(r["name"], f'{r["area_m2"]:.2f}',
                 ", ".join(r.get("labels", [])) or "—")
                for r in data["rooms"]]
        total = sum(r["area_m2"] or 0 for r in data["rooms"])
        rows.append(("TOTAL", f"{total:.2f}", ""))
        sec("Spaces", make_table(["Name", "Area (m²)", "Labels Inside"], rows))

    for layout_name, ents in data["layouts"].items():
        counts = Counter(e["type"] for e in ents)
        sec(f"Layout: {layout_name} ({len(ents)} entities)",
            make_table(["Entity Type", "Count"], counts.most_common()))

    layer_rows = [(l["name"], l["color_aci"], l["linetype"],
                   "off" if not l["is_on"] else ("frozen" if l["is_frozen"] else "on"))
                  for l in data["layers"]]
    sec("Layers", make_table(["Name", "ACI Color", "Linetype", "State"],
                             sorted(layer_rows)))

    if data["blocks"]:
        block_rows = [(name, len(b["entities"]),
                       s["block_insert_counts"].get(name, 0))
                      for name, b in sorted(data["blocks"].items())]
        sec("Block Definitions", make_table(
            ["Block", "Entities Inside", "Times Inserted"], block_rows))

    texts = [(e["layer"], e["text"],
              round(e["position"][0], 2), round(e["position"][1], 2))
             for e in data["layouts"].get("Model", [])
             if e["type"] in ("TEXT", "MTEXT") and e.get("text", "").strip()]
    if texts:
        sec("Text Annotations (model space)",
            make_table(["Layer", "Text", "X", "Y"], texts))

    dims = [(e["layer"], e.get("measurement"), e.get("text_override") or "<>")
            for ents in data["layouts"].values() for e in ents
            if e["type"] == "DIMENSION"]
    if dims:
        sec("Dimensions", make_table(["Layer", "Measured", "Text"], dims))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>DXF Report — {esc(src)}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;font-size:14px;
       line-height:1.6;background:#fff;color:#1f2328;max-width:960px;
       margin:0 auto;padding:24px 20px 48px}}
  h1{{font-size:22px;margin-bottom:4px}}
  .sub{{color:#57606a;font-size:13px;margin-bottom:28px}}
  h2{{font-size:15px;font-weight:600;margin:28px 0 10px;
     border-bottom:1px solid #e5e7eb;padding-bottom:6px}}
  .cards{{display:flex;flex-wrap:wrap;gap:12px}}
  .card{{background:#f7f8fa;border:1px solid #e5e7eb;border-radius:8px;
        padding:14px 16px;text-align:center;min-width:110px;flex:1;
        font-size:12px;color:#57606a}}
  .num{{font-size:22px;font-weight:700;color:#3b82d4;display:block}}
  .warn{{background:#fff8e6;border:1px solid #f0d58c;border-radius:6px;
        padding:8px 12px}}
  table.tbl{{width:100%;border-collapse:collapse;font-size:13px}}
  .tbl th{{background:#f7f8fa;text-align:left;padding:6px 10px;
          border:1px solid #e5e7eb;font-weight:600}}
  .tbl td{{padding:5px 10px;border:1px solid #e5e7eb;vertical-align:top}}
  .tbl tbody tr:nth-child(even){{background:#fafbfc}}
</style></head><body>
<h1>DXF Report — {esc(src)}</h1>
<p class="sub">DXF {esc(data["source"]["dxf_version"])} · ezdxf {ezdxf.__version__}</p>
{"".join(sections)}
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def main(dxf_path):
    stem = os.path.splitext(os.path.basename(dxf_path))[0]
    out_dir = os.path.join(OUTPUT_DIR, stem)  # one folder per input file
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, stem)
    out_png, out_json, out_html = base + ".png", base + "-data.json", base + "-report.html"

    print(f"Reading {dxf_path} ...")
    try:
        doc = ezdxf.readfile(dxf_path)
        recovered = False
    except (ezdxf.DXFStructureError, ezdxf.DXFError):
        doc, auditor = recover.readfile(dxf_path)
        recovered = True
        print(f"  file needed recovery ({len(auditor.errors)} errors fixed)")

    msp = doc.modelspace()

    # extents: prefer header, fall back to computing from entities
    ext_min = doc.header.get("$EXTMIN")
    ext_max = doc.header.get("$EXTMAX")
    if not ext_min or not ext_max or Vec3(ext_max).x < Vec3(ext_min).x:
        xs, ys = [], []
        for e in msp:
            pts = flatten_2d(e)
            if pts:
                xs += [p[0] for p in pts]
                ys += [p[1] for p in pts]
        if not xs:
            print("  drawing appears empty"); return
        extents = ((min(xs), min(ys)), (max(xs), max(ys)))
    else:
        extents = ((Vec3(ext_min).x, Vec3(ext_min).y),
                   (Vec3(ext_max).x, Vec3(ext_max).y))
    span = (extents[1][0] - extents[0][0], extents[1][1] - extents[0][1])

    units = resolve_units(doc, span)
    print(f"  units: {units['unit']}"
          + (" (guessed -- declared "
             f"{units['declared_unit']} is implausible)" if units["guessed"] else ""))

    print("  serializing layouts ...")
    layouts = {layout.name: serialize_layout(layout) for layout in doc.layouts}

    print("  serializing blocks ...")
    blocks = serialize_blocks(doc)

    print("  rendering PNG ...")
    try:
        render_png(doc, msp, out_png, extents)
    except Exception as exc:
        print(f"  ! rendering failed: {exc}")

    layers_table = serialize_layers(doc)

    print("  building semantic model ...")
    hints = None
    hints_path = os.path.join(out_dir, "hints.json")
    if os.path.exists(hints_path):
        try:
            with open(hints_path, encoding="utf-8") as f:
                hints = json.load(f)
            print(f"  using hints.json "
                  f"({len(hints.get('wall_segments', []))} wall hints)")
        except Exception as exc:
            print(f"  ! hints.json unreadable: {exc}")
    model = build_semantic_model(layouts.get("Model", []), blocks,
                                 units, extents, layers_table, hints)
    spaces = model["spaces"]
    total_room_area = sum(s["area_m2"] for s in spaces)

    insert_counts = Counter(e["block"] for ents in layouts.values()
                            for e in ents if e["type"] == "INSERT")

    data = {
        "source": {
            "file": os.path.basename(dxf_path),
            "dxf_version": doc.dxfversion,
            "recovered": recovered,
            "ezdxf_version": ezdxf.__version__,
        },
        "units": units,
        "extents": {"min": list(extents[0]), "max": list(extents[1]),
                    "span": list(span)},
        "summary": {
            "total_entities": sum(len(v) for v in layouts.values()),
            "entities_per_layout": {k: len(v) for k, v in layouts.items()},
            "entity_type_counts": dict(Counter(
                e["type"] for v in layouts.values() for e in v)),
            "block_insert_counts": dict(insert_counts),
            "total_room_area_m2": round(total_room_area, 3),
        },
        "layers": layers_table,
        "blocks": blocks,
        "rooms": [{"name": s["name"], "area_m2": s["area_m2"],
                   "labels": s["labels"]} for s in spaces],
        "layouts": layouts,
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)

    # re-apply cached vision semantics (refine/semantics.py) so identities
    # survive re-extraction; geometry is never touched by this
    sem_path = os.path.join(out_dir, "semantics.json")
    if os.path.exists(sem_path):
        try:
            with open(sem_path, encoding="utf-8") as f:
                sem = json.load(f)
            for mid, info in (sem.get("movable") or {}).items():
                for m in model["movable"]:
                    if m["id"] == mid:
                        m["kind"] = info["kind"]
                        m["kind_confidence"] = info["confidence"]
                        m["kind_source"] = info.get("source", "vision")
            if sem.get("fixed_clusters"):
                model["fixed_groups"] = [
                    {"id": c["id"], "space": c["space"], "kind": c["kind"],
                     "confidence": c["confidence"],
                     "fixed_indices": c["indices"]}
                    for c in sem["fixed_clusters"]]
            for sid, info in (sem.get("spaces") or {}).items():
                for s in model["spaces"]:
                    if s["id"] == sid:
                        s["purpose"] = info["purpose"]
                        s["purpose_source"] = info.get("source", "vision")
            for sid, nm in (sem.get("names") or {}).items():
                for s in model["spaces"]:
                    if s["id"] == sid:
                        s["name"] = nm
            print("  applied cached semantics.json")
        except Exception as exc:
            print(f"  ! semantics merge failed: {exc}")

    out_model = base + "-model.json"
    with open(out_model, "w", encoding="utf-8") as f:
        json.dump(model, f, indent=1, ensure_ascii=False)
    out_model_png = base + "-model.png"
    try:
        render_model_debug(model, out_model_png)
    except Exception as exc:
        print(f"  ! model debug render failed: {exc}")
    cov = model["coverage"]
    print(f"    walls: {len(model['walls'])}, openings: {len(model['openings'])}, "
          f"movable: {len(model['movable'])}, fixed: {len(model['fixed'])}, "
          f"overhead: {len(model['overhead'])}")
    print(f"    ledger: unclaimed={cov['unclaimed_count']}, "
          f"outside_region={cov['outside_floor_region']}, "
          f"warnings={len(cov['warnings'])}")
    for w in cov["warnings"]:
        print(f"      ! {w}")

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(build_report(data, os.path.basename(out_png)))

    print(f"\n  Spaces detected: {len(spaces)}")
    for s in spaces:
        print(f'    {s["name"]:<32} {s["area_m2"]:>8.2f} m2')
    print(f"\n  JSON   : {out_json}")
    print(f"  Model  : {out_model}  (+ {os.path.basename(out_model_png)})")
    print(f"  Report : {out_html}")
    print(f"  PNG    : {out_png}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        # no argument: process every DXF in the input/ folder
        input_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "input")
        os.makedirs(input_dir, exist_ok=True)
        dxf_files = [os.path.join(input_dir, f) for f in os.listdir(input_dir)
                     if f.lower().endswith(".dxf")]
        if not dxf_files:
            print(f"No .dxf files found in {input_dir}")
            sys.exit(1)
        for p in dxf_files:
            main(p)
