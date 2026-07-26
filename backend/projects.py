"""Project store + pipeline orchestration.

One folder per project under ArchiDream/data/<pid>/:
  source.dxf            uploaded drawing
  model.json            semantic model (copied from the engine's output)
  plan.png              deterministic DXF render      (the "Blueprint" view)
  plan_transform.json   world->pixel mapping for plan.png
  rooms.json            per-room pixel polygons/centroids for the UI overlay
  stylized/<style>.png  Nano Banana stylized top-down  (the "Styled" view)
  crops/<style>/<room>.png   room crops fed to the pano call
  panos/<style>/<room>.png   equirect 360s
  project.json          summary/state
"""

import json
import os
import re
import subprocess
import sys

from PIL import Image

import planrender

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(BACKEND_DIR, "..", "data"))
ENGINE = os.path.join(BACKEND_DIR, "engine", "dxf_analyse.py")
ENGINE_OUT = os.path.join(BACKEND_DIR, "engine", "output")

MIN_ROOM_M2 = 1.0  # hide slivers from the UI


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def pdir(pid):
    return os.path.join(DATA_DIR, pid)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def run_engine(dxf_path):
    """Run the extraction pipeline; returns path to the produced model.json."""
    stem = os.path.splitext(os.path.basename(dxf_path))[0]
    env = dict(os.environ, MPLBACKEND="Agg")
    proc = subprocess.run([sys.executable, ENGINE, os.path.abspath(dxf_path)],
                          cwd=os.path.dirname(ENGINE), env=env,
                          capture_output=True, text=True, timeout=600)
    model_path = os.path.join(ENGINE_OUT, stem, f"{stem}-model.json")
    if not os.path.exists(model_path):
        tail = (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-2000:]
        raise RuntimeError(f"engine produced no model.json:\n{tail}")
    return model_path


def build_rooms(model, transform):
    """model.json spaces -> UI rooms with pixel-space geometry."""
    rooms = []
    for s in model.get("spaces", []):
        if (s.get("area_m2") or 0) < MIN_ROOM_M2:
            continue
        poly = s.get("polygon") or []
        if len(poly) < 3:
            continue
        poly_px = [[round(v, 1) for v in
                    planrender.meters_to_px(transform, model, x, y)]
                   for x, y in poly]
        cx, cy = planrender.meters_to_px(transform, model, *s["centroid"])
        xs = [p[0] for p in poly_px]
        ys = [p[1] for p in poly_px]
        name = s.get("purpose") or s.get("name") or s["id"]
        rooms.append({
            "id": s["id"],
            "name": name,
            "area_m2": round(s.get("area_m2") or 0, 1),
            "polygon_px": poly_px,
            "centroid_px": [round(cx, 1), round(cy, 1)],
            "bbox_px": [round(min(xs), 1), round(min(ys), 1),
                        round(max(xs), 1), round(max(ys), 1)],
        })
    return rooms


def create_project(dxf_path, original_name=None):
    """Full ingest: engine -> plan render -> rooms.json -> project.json."""
    stem = os.path.splitext(os.path.basename(original_name or dxf_path))[0]
    pid = slugify(stem)
    root = pdir(pid)
    os.makedirs(root, exist_ok=True)

    stored_dxf = os.path.join(root, "source.dxf")
    if os.path.abspath(dxf_path) != os.path.abspath(stored_dxf):
        with open(dxf_path, "rb") as src, open(stored_dxf, "wb") as dst:
            dst.write(src.read())

    model_path = run_engine(stored_dxf)
    model = _read_json(model_path)
    _write_json(os.path.join(root, "model.json"), model)

    # window the render to the built content (walls + spaces) so elevation/
    # detail views sharing the DXF sheet stay out of the stylize input
    window = None
    pts = [p for w in model.get("walls", []) for p in w.get("polygon", [])]
    pts += [p for s in model.get("spaces", []) for p in s.get("polygon", [])]
    if not pts and model.get("floor_bounds_m"):
        pts = model["floor_bounds_m"]
    if pts:
        ox, oy = model["origin_offset_drawing_units"]
        f = model["to_meters_factor"] or 1.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        pad = 0.8 / f  # ~0.8 m of context beyond the outer walls
        window = (ox + min(xs) / f - pad, oy + min(ys) / f - pad,
                  ox + max(xs) / f + pad, oy + max(ys) / f + pad)

    transform = planrender.render_plan(
        stored_dxf,
        os.path.join(root, "plan.png"),
        os.path.join(root, "plan_transform.json"),
        window=window)

    rooms = build_rooms(model, transform)
    _write_json(os.path.join(root, "rooms.json"), rooms)

    project = {
        "id": pid,
        "name": stem.replace("-", " ").replace("_", " "),
        "width_px": transform["width_px"],
        "height_px": transform["height_px"],
        "rooms": len(rooms),
    }
    _write_json(os.path.join(root, "project.json"), project)
    return get_project(pid)


def list_projects():
    if not os.path.isdir(DATA_DIR):
        return []
    out = []
    for pid in sorted(os.listdir(DATA_DIR)):
        meta = os.path.join(pdir(pid), "project.json")
        if os.path.exists(meta):
            out.append(get_project(pid))
    return out


def get_project(pid):
    root = pdir(pid)
    meta_path = os.path.join(root, "project.json")
    if not os.path.exists(meta_path):
        return None
    project = _read_json(meta_path)
    project["rooms"] = _read_json(os.path.join(root, "rooms.json"))
    project["plan_url"] = f"/data/{pid}/plan.png"

    # keys are "<style>@<model>"; files from before the model dropdown
    # (plain "<style>") were made by the then-only model, nano-banana.
    # ".mock.png" placeholders are never listed as results.
    def key_of(stem):
        return stem if "@" in stem else f"{stem}@nano-banana"

    project["stylized"] = {}
    sdir = os.path.join(root, "stylized")
    if os.path.isdir(sdir):
        # sorted => plain legacy name first, "@model" file wins the key
        for fn in sorted(os.listdir(sdir)):
            if fn.endswith(".png") and not fn.endswith(".mock.png"):
                project["stylized"][key_of(fn[:-4])] = \
                    f"/data/{pid}/stylized/{fn}"

    project["tours"] = {}
    tdir = os.path.join(root, "tours")
    if os.path.isdir(tdir):
        for fn in os.listdir(tdir):
            if fn.endswith(".mp4"):
                project["tours"][fn[:-4]] = f"/data/{pid}/tours/{fn}"

    project["panos"] = {}
    pdir_ = os.path.join(root, "panos")
    if os.path.isdir(pdir_):
        for style in sorted(os.listdir(pdir_)):
            style_dir = os.path.join(pdir_, style)
            if not os.path.isdir(style_dir):
                continue
            # MERGE legacy and @model dirs mapping to the same key —
            # replacing would drop already-generated rooms from the UI
            bucket = project["panos"].setdefault(key_of(style), {})
            bucket.update({
                fn[:-4]: f"/data/{pid}/panos/{style}/{fn}"
                for fn in os.listdir(style_dir)
                if fn.endswith(".png") and not fn.endswith(".mock.png")})
    return project


def room_features(pid, room):
    """Doors/windows of one room from model.json as prompt-ready text.
    Sides are compass words; in the top-down crop, north = up."""
    model = _read_json(os.path.join(pdir(pid), "model.json"))
    sp = next((s for s in model.get("spaces", [])
               if s["id"] == room["id"]), None)
    if not sp:
        return ""
    cx, cy = sp["centroid"]
    names = {s["id"]: (s.get("purpose") or s.get("name") or s["id"])
             for s in model["spaces"]}
    feats = []
    for o in model.get("openings", []):
        c = o.get("connects", [])
        if room["id"] not in c or not o.get("center"):
            continue
        dx, dy = o["center"][0] - cx, o["center"][1] - cy
        side = ("north" if dy > 0 else "south") if abs(dy) >= abs(dx) \
            else ("east" if dx > 0 else "west")
        kind = o.get("type", "opening")
        w = o.get("width_m") or 0
        txt = f"a {w:.1f}m wide {kind} on the {side} wall"
        other = next((x for x in c if x != room["id"]), None)
        if kind != "window" and other and other != "wall":
            if other == "exterior":
                dest = "outside"
            else:
                nm = names.get(other, other)
                dest = "another room" if nm.startswith("space ") else \
                    f"the {nm}"
            txt += f" leading to {dest}"
        feats.append(txt)
    return "; ".join(feats)


def _region(dx, dy):
    ns = "north" if dy > 0.3 else ("south" if dy < -0.3 else "")
    ew = "east" if dx > 0.3 else ("west" if dx < -0.3 else "")
    part = (ns + ew) or "center"
    return "near the center" if part == "center" else f"in the {part} part"


def room_brief(pid, room):
    """Layer-derived facts for one room, prompt-budgeted: dimensions,
    furniture manifest with wall-relative placements, openings, daylight.
    Text disambiguates, the crop image locates — no coordinates here."""
    model = _read_json(os.path.join(pdir(pid), "model.json"))
    sp = next((s for s in model.get("spaces", [])
               if s["id"] == room["id"]), None)
    if not sp:
        return ""
    facts = []

    # 1. shape + size + ceiling (spaces/walls layers)
    xs = [p[0] for p in sp["polygon"]]
    ys = [p[1] for p in sp["polygon"]]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    shape = "rectangular" if sp["area_m2"] >= 0.85 * w * h else "L-shaped"
    ceil = (model.get("defaults") or {}).get("wall_height_m") or 2.7
    facts.append(f"The room is {shape}, about {w:.1f}m east-west by "
                 f"{h:.1f}m north-south, ceiling {ceil:.1f}m.")

    # 2. furniture manifest (movable layer) — identity via text where we
    # have a usable name, placement always wall-relative
    cx, cy = sp["centroid"]
    items = []
    for v in model.get("movable", []):
        if v.get("space") != room["id"] or \
                v.get("category") != "furniture" or not v.get("outline"):
            continue
        bx = [p[0] for p in v["outline"]]
        by = [p[1] for p in v["outline"]]
        iw, ih = max(bx) - min(bx), max(by) - min(by)
        if max(iw, ih) < 0.35:  # decor specks: skip, image shows them
            continue
        icx, icy = (max(bx) + min(bx)) / 2, (max(by) + min(by)) / 2
        gaps = {"north": max(ys) - max(by), "south": min(by) - min(ys),
                "east": max(xs) - max(bx), "west": min(bx) - min(xs)}
        side = min(gaps, key=gaps.get)
        # bbox edges are only guaranteed to BE walls in rectangular rooms;
        # for L-shapes a wrong "against the X wall" fact misleads the model
        place = f"against the {side} wall" \
            if shape == "rectangular" and gaps[side] < 0.45 \
            else _region(icx - cx, icy - cy)
        name = v.get("kind")
        if not name and re.match(r"^[A-Za-z][A-Za-z .\-]{2,}$",
                                 v.get("block", "")):
            name = v["block"].strip()
        label = name or f"a {max(iw, ih):.1f}x{min(iw, ih):.1f}m piece"
        items.append(f"{label} {place}")
    if items:
        facts.append("Furniture in the room (they are visible in the "
                     "top-down input; keep each exactly where shown): "
                     + "; ".join(items[:6]) + ".")

    # 3. openings on their true walls (openings layer)
    feats = room_features(pid, room)
    if feats:
        facts.append(f"Openings: {feats}.")

    # 4. daylight direction (window sides)
    sides = set()
    for o in model.get("openings", []):
        if room["id"] in o.get("connects", []) and \
                o.get("type") == "window" and o.get("center"):
            dx, dy = o["center"][0] - cx, o["center"][1] - cy
            sides.add(("north" if dy > 0 else "south")
                      if abs(dy) >= abs(dx)
                      else ("east" if dx > 0 else "west"))
    if sides:
        facts.append("Natural daylight enters from the "
                     + " and ".join(sorted(sides)) + ".")
    return " ".join(facts)


def building_brief(pid):
    """Whole-plan inventory for the stylize prompt: room purposes + scale
    anchor, so rooms get furnished by function, at the right scale."""
    model = _read_json(os.path.join(pdir(pid), "model.json"))
    rooms = sorted(model.get("spaces", []), key=lambda s: -s["area_m2"])
    def label(s):
        nm = s.get("purpose") or s.get("name") or "room"
        return "utility room" if nm.startswith("space ") else nm

    inv = ", ".join(f'{label(s)} ({s["area_m2"]:.0f} m2)'
                    for s in rooms
                    if (s.get("area_m2") or 0) >= MIN_ROOM_M2)
    fb = model.get("floor_bounds_m")
    size = ""
    if fb:
        (x0, y0), (x1, y1) = fb
        size = f"The floor is about {x1 - x0:.0f}m by {y1 - y0:.0f}m. "
    return (f"{size}Rooms in this plan: {inv}. Furnish every room strictly "
            "according to its function; keep furniture at realistic scale "
            "for these room sizes; leave outdoor or garage areas "
            "unfurnished.")


def crop_room(pid, style_id, room, pad_m=0.6):
    """Crop one room from the stylized top-down (fallback: the plan render)."""
    root = pdir(pid)
    styled = os.path.join(root, "stylized", f"{style_id}.png")
    if not os.path.exists(styled) and "@" in style_id:
        # pre-dropdown file for the same style beats the bare plan render
        legacy = os.path.join(root, "stylized",
                              f'{style_id.split("@")[0]}.png')
        if os.path.exists(legacy):
            styled = legacy
    src = styled if os.path.exists(styled) else os.path.join(root, "plan.png")
    img = Image.open(src).convert("RGB")

    transform = _read_json(os.path.join(root, "plan_transform.json"))
    model = _read_json(os.path.join(root, "model.json"))
    pad = pad_m * planrender.px_per_meter(transform, model)

    x0, y0, x1, y1 = room["bbox_px"]
    box = (max(0, int(x0 - pad)), max(0, int(y0 - pad)),
           min(img.width, int(x1 + pad)), min(img.height, int(y1 + pad)))
    crop = img.crop(box)

    out_dir = os.path.join(root, "crops", style_id)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{room['id']}.png")
    crop.save(out)
    return crop
