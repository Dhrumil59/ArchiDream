"""Compile tour-data.json + tour-template.tpl -> index.html.

Stage 1 of the tour pipeline: gathers per-property data (rooms.json,
project.json, style palette) into tour-data.json, then bakes the
HyperFrames composition. The room sweeps are generated dynamically — one
per included 360 — so the video scales to however many rooms you pick.

Usage: python compile_tour.py [project_id] [style_key] [room_ids_csv]
  room_ids_csv  optional comma-separated room ids to include. Empty/absent
                => every room that has a pano. Rooms without a pano are
                always skipped.
"""
import json
import os
import shutil
import sys

import director

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data"))

PALETTES = {  # mirror of backend/styles.py (colors + accent + display name)
    "scandinavian": ("Scandinavian", ["#F7F4EF", "#D9BC91", "#E8DCC8", "#B7B0A6"]),
    "japandi": ("Japandi", ["#F1E9DA", "#D6C4A5", "#6B4A32", "#3B3A38"]),
    "mediterranean": ("Mediterranean", ["#FAF6EE", "#C96F4A", "#2E6E8E", "#7A8450"]),
    "dark-luxury": ("Dark Luxury", ["#121212", "#2E2E30", "#1F5B4E", "#B8924E"]),
}


def style_palette(style):
    """(display name, 4 palette hexes) for built-in OR user-defined styles."""
    if style in PALETTES:
        return PALETTES[style]
    try:
        custom = json.load(open(os.path.join(DATA, "custom_styles.json"),
                                encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        custom = {}
    s = custom.get(style)
    if s:
        hexes = [c["hex"] for c in s["colors"]] or ["#c9a35c"]
        return s["name"], (hexes * 4)[:4]
    return style.replace("-", " ").title(), \
        ["#c9a35c", "#b9b3a6", "#3B3A38", "#F1E9DA"]

# ---- shot timing (seconds) --------------------------------------------------
PANO_START = 12.0     # intro (opening card + plan->styled dive) is fixed
HERO_DUR = 5.5        # first room lingers a touch longer
ROOM_DUR = 4.0        # every other room
PULLBACK_DUR = 3.5
CLOSING_DUR = 4.0
SWEEP_X = 700         # horizontal pan amplitude (px) inside the 2700px pano

PANO_SECTION = """\
      <section id="p{i}" class="clip" data-start="{start}" \
data-duration="{dur}" data-track-index="1">
        <div class="fill"></div>
        <div id="p{i}-pano" class="pano"><img id="p{i}-img" \
src="{pano}" alt="" /></div>
      </section>
      <section id="p{i}lt" class="clip" data-start="{lt_start}" \
data-duration="{lt_dur}" data-track-index="2">
        <div class="lt" id="p{i}-lt"><div class="name">{name}</div>{sub}</div>
      </section>"""

PANO_TWEEN = """\
      tl.fromTo("#p{i}-pano", {{ x: {xf} }}, {{ x: {xt}, duration: {dur}, \
ease: "none" }}, {start});
      tl.from("#p{i}-lt", {{ x: -60, opacity: 0, duration: 0.7, \
ease: "power3.out" }}, {name_at});"""


def _fmt(x):
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_data(pid, style_key, include_ids=None):
    root = os.path.join(DATA, pid)
    rooms = json.load(open(os.path.join(root, "rooms.json"), encoding="utf-8"))
    project = json.load(open(os.path.join(root, "project.json"), encoding="utf-8"))
    style = style_key.split("@")[0]
    pano_dir = os.path.join(root, "panos", style_key)

    # rooms that HAVE a pano (checking the keyed dir, then the legacy dir)
    have = []
    for r in rooms:
        p = os.path.join(pano_dir, f'{r["id"]}.png')
        if not os.path.exists(p):
            p = os.path.join(root, "panos", style, f'{r["id"]}.png')
        if os.path.exists(p):
            have.append((r, p))
    if include_ids:
        want = set(include_ids)
        have = [t for t in have if t[0]["id"] in want]
    have.sort(key=lambda t: -t[0]["area_m2"])  # biggest first; hero = [0]
    if not have:
        raise SystemExit("no rooms with a pano to include")

    total = round(sum(r["area_m2"] for r in rooms))
    beds = sum(1 for r in rooms if "bed" in r["name"].lower())
    baths = sum(1 for r in rooms if "bath" in r["name"].lower())
    name, palette = style_palette(style)

    # Granite directs the silent cut: room order + a caption per room + tagline.
    # Falls back to deterministic biggest-first when Granite is unavailable.
    ids = [r["id"] for r, _ in have]
    facts = {
        "property": {"name": project["name"].title(), "total_m2": total,
                     "rooms": len(rooms)},
        "style": {"name": name, "palette": palette},
        "rooms": [{"id": r["id"], "name": r["name"].replace("/", " / "),
                   "area_m2": round(r["area_m2"])} for r, _ in have],
    }
    plan = director.direct(facts, ids)
    if plan:
        rank = {rid: n for n, rid in enumerate(plan["order"])}
        have.sort(key=lambda t: rank.get(t[0]["id"], 999))
        captions = plan["captions"]
        tagline = plan["tagline"] or "Walk through your home before it exists."
    else:
        captions = {}
        tagline = "Walk through your home before it exists."
    print("director:", "Granite shot-plan" if plan else "deterministic fallback")

    hero = have[0][0]
    plan_w = project["width_px"]
    plan_h = project["height_px"]
    stylized = os.path.join(root, "stylized", f"{style_key}.png")
    if not os.path.exists(stylized):
        stylized = os.path.join(root, "stylized", f"{style}.png")

    return {
        "title": project["name"].title(),
        "stats": f"{total} m² · {len(rooms)} rooms"
                 + (f" · {beds} bed" if beds else "")
                 + (f" / {baths} bath" if baths else ""),
        "style_name": name,
        "palette": palette,
        "plan_px": [plan_w, plan_h],
        "plan_img": os.path.join(root, "plan.png"),
        "styled_img": stylized,
        "hero": {
            "polygon": " ".join(f'{p[0]},{p[1]}' for p in hero["polygon_px"]),
            "cx_pct": round(hero["centroid_px"][0] / plan_w * 100, 2),
            "cy_pct": round(hero["centroid_px"][1] / plan_h * 100, 2),
        },
        "rooms": [
            {"id": r["id"], "label": r["name"].replace("/", " · ").title(),
             "caption": captions.get(r["id"], ""),
             "area": round(r["area_m2"]), "pano": p}
            for r, p in have
        ],
        "tagline": tagline,
    }


def main():
    pid = sys.argv[1] if len(sys.argv) > 1 else "simple-house"
    style_key = sys.argv[2] if len(sys.argv) > 2 else "mediterranean@nano-banana"
    include = None
    if len(sys.argv) > 3 and sys.argv[3].strip():
        include = [x for x in sys.argv[3].split(",") if x.strip()]

    d = build_data(pid, style_key, include)
    json.dump(d, open(os.path.join(HERE, "tour-data.json"), "w",
                      encoding="utf-8"), indent=1)

    # stage media locally (renderer serves the project dir)
    media = os.path.join(HERE, "media")
    os.makedirs(media, exist_ok=True)

    def stage(src, name):
        shutil.copy2(src, os.path.join(media, name))
        return f"media/{name}"

    d["plan_img"] = stage(d["plan_img"], "plan.png")
    d["styled_img"] = stage(d["styled_img"], "styled.png")
    for i, r in enumerate(d["rooms"]):
        r["pano"] = stage(r["pano"], f"pano{i}.png")

    # ---- build the dynamic room-sweep shots + their tweens ----
    sections, tweens = [], []
    t = PANO_START
    for i, r in enumerate(d["rooms"]):
        dur = HERO_DUR if i == 0 else ROOM_DUR
        name_line = r["label"] + (f" · {r['area']} m²" if r["area"] >= 3 else "")
        cap = (r.get("caption") or "").strip()
        sub = f'<div class="sub">{_esc(cap)}</div>' if cap else ""
        sections.append(PANO_SECTION.format(
            i=i, start=_fmt(t), dur=_fmt(dur),
            lt_start=_fmt(t + 0.6), lt_dur=_fmt(max(1.6, dur - 0.8)),
            pano=r["pano"], name=_esc(name_line), sub=sub))
        xf, xt = (0, -SWEEP_X) if i % 2 == 0 else (-SWEEP_X, 0)
        tweens.append(PANO_TWEEN.format(
            i=i, xf=xf, xt=xt, dur=_fmt(dur),
            start=_fmt(t), name_at=_fmt(t + 0.7)))
        t += dur

    pano_end = t
    pullback_start = pano_end
    closing_start = pano_end + PULLBACK_DUR
    total_dur = closing_start + CLOSING_DUR

    flat = {
        "TITLE": d["title"], "STATS": d["stats"], "TAGLINE": d["tagline"],
        "STYLE_NAME": d["style_name"],
        "PLAN_W": str(d["plan_px"][0]), "PLAN_H": str(d["plan_px"][1]),
        "PLAN_IMG": d["plan_img"], "STYLED_IMG": d["styled_img"],
        "HERO_POLY": d["hero"]["polygon"],
        "HERO_CX": str(d["hero"]["cx_pct"]), "HERO_CY": str(d["hero"]["cy_pct"]),
        "DOT0": d["palette"][0], "DOT1": d["palette"][1],
        "DOT2": d["palette"][2], "DOT3": d["palette"][3],
        "PANO_SECTIONS": "\n".join(sections),
        "PANO_TWEENS": "\n".join(tweens),
        "PULLBACK_START": _fmt(pullback_start),
        "CLOSING_START": _fmt(closing_start),
        "TITLE_AT": _fmt(closing_start + 0.2),
        "TAG_AT": _fmt(closing_start + 0.7),
        "DOTS_AT": _fmt(closing_start + 1.3),
        "STYLE_AT": _fmt(closing_start + 1.6),
        "MARK_AT": _fmt(closing_start + 2.0),
        "TOTAL": _fmt(total_dur),
    }
    tpl = open(os.path.join(HERE, "tour-template.tpl"), encoding="utf-8").read()
    for k, v in flat.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    open(os.path.join(HERE, "index.html"), "w", encoding="utf-8").write(tpl)
    print(f"compiled index.html for {pid} / {style_key} "
          f"({len(d['rooms'])} rooms, {_fmt(total_dur)}s)")


if __name__ == "__main__":
    main()
