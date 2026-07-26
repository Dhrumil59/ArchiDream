"""The four built-in ArchiDream interior styles: UI metadata + prompt
fragments. Users can add their own, persisted to data/custom_styles.json."""

import json
import os
import re

STYLES = {
    "scandinavian": {
        "id": "scandinavian",
        "emoji": "\U0001F90D",  # white heart
        "name": "Scandinavian",
        "tagline": "Nordic light, honest materials",
        "vibe": ["Bright", "Cozy", "Minimal", "Airy"],
        "colors": [
            {"name": "White", "hex": "#F7F4EF"},
            {"name": "Light Oak", "hex": "#D9BC91"},
            {"name": "Soft Beige", "hex": "#E8DCC8"},
            {"name": "Warm Gray", "hex": "#B7B0A6"},
        ],
        "materials": ["Oak Wood", "Linen", "Wool",
                      "Matte Ceramics", "Light Stone"],
        "accent": "#D9BC91",
    },
    "japandi": {
        "id": "japandi",
        "emoji": "\U0001F33F",  # herb
        "name": "Japandi",
        "tagline": "Zen restraint, warm minimalism",
        "vibe": ["Zen", "Calm", "Organic", "Elegant"],
        "colors": [
            {"name": "Cream", "hex": "#F1E9DA"},
            {"name": "Sand", "hex": "#D6C4A5"},
            {"name": "Walnut", "hex": "#6B4A32"},
            {"name": "Charcoal", "hex": "#3B3A38"},
        ],
        "materials": ["Walnut Wood", "Natural Stone", "Textured Plaster",
                      "Linen", "Handmade Ceramics"],
        "accent": "#6B4A32",
    },
    "mediterranean": {
        "id": "mediterranean",
        "emoji": "\U0001F30A",  # wave
        "name": "Mediterranean",
        "tagline": "Sun-washed coastal warmth",
        "vibe": ["Sunny", "Relaxed", "Coastal", "Inviting"],
        "colors": [
            {"name": "White", "hex": "#FAF6EE"},
            {"name": "Terracotta", "hex": "#C96F4A"},
            {"name": "Ocean Blue", "hex": "#2E6E8E"},
            {"name": "Olive Green", "hex": "#7A8450"},
        ],
        "materials": ["Stucco", "Limestone", "Terracotta Tile",
                      "Natural Wood", "Wrought Iron"],
        "accent": "#C96F4A",
    },
    "dark-luxury": {
        "id": "dark-luxury",
        "emoji": "\U0001F311",  # new moon
        "name": "Dark Luxury",
        "tagline": "Moody drama, gilded edges",
        "vibe": ["Luxury", "Moody", "Sophisticated", "Dramatic"],
        "colors": [
            {"name": "Black", "hex": "#121212"},
            {"name": "Charcoal", "hex": "#2E2E30"},
            {"name": "Emerald", "hex": "#1F5B4E"},
            {"name": "Brass Gold", "hex": "#B8924E"},
        ],
        "materials": ["Black Marble", "Brushed Brass", "Dark Walnut",
                      "Velvet", "Smoked Glass"],
        "accent": "#B8924E",
    },
}


_CUSTOM_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data",
    "custom_styles.json"))
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _load_custom():
    try:
        with open(_CUSTOM_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def all_styles():
    """Built-in styles plus user-defined ones (customs never shadow a
    built-in id). Re-read each call so new styles appear without a restart."""
    merged = dict(STYLES)
    for sid, s in _load_custom().items():
        merged.setdefault(sid, s)
    return merged


def get_style(style_id):
    return all_styles().get(style_id)


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "style"


def add_custom(fields):
    """Validate + persist a user-defined style; returns the stored entry.
    Raises ValueError on bad input."""
    name = (fields.get("name") or "").strip()
    if not name:
        raise ValueError("a style name is required")

    colors = [{"name": (c.get("name") or "").strip() or c["hex"].strip(),
               "hex": c["hex"].strip()}
              for c in (fields.get("colors") or [])
              if c.get("hex") and _HEX.match(c["hex"].strip())]
    if len(colors) < 2:
        raise ValueError("give at least 2 valid palette colors (#rrggbb)")

    materials = [m.strip() for m in (fields.get("materials") or []) if m.strip()]
    if not materials:
        raise ValueError("give at least one material")

    vibe = [v.strip() for v in (fields.get("vibe") or []) if v.strip()][:6]
    accent = (fields.get("accent") or "").strip()
    if not _HEX.match(accent):
        accent = colors[1]["hex"] if len(colors) > 1 else colors[0]["hex"]
    emoji = ((fields.get("emoji") or "").strip() or "\U0001F3A8")[:4]

    sid = slugify(name)
    if sid in STYLES:
        sid += "-custom"
    custom = _load_custom()
    base, i = sid, 2
    while sid in custom:
        sid, i = f"{base}-{i}", i + 1

    entry = {
        "id": sid, "emoji": emoji, "name": name,
        "tagline": (fields.get("tagline") or "").strip() or "Custom style",
        "vibe": vibe or ["Custom"],
        "colors": colors, "materials": materials, "accent": accent,
        "custom": True,
    }
    custom[sid] = entry
    os.makedirs(os.path.dirname(_CUSTOM_PATH), exist_ok=True)
    with open(_CUSTOM_PATH, "w", encoding="utf-8") as f:
        json.dump(custom, f, indent=1, ensure_ascii=False)
    return entry


def style_block(style_id):
    """The style fragment appended to every image prompt."""
    s = all_styles()[style_id]
    colors = ", ".join(c["name"] for c in s["colors"])
    return (f"Interior design style: {s['name']} — "
            f"{', '.join(v.lower() for v in s['vibe'])}. "
            f"Color palette strictly limited to: {colors}. "
            f"Materials: {', '.join(s['materials'])}. "
            f"Every surface, furniture piece and textile must follow this "
            f"palette and material set.")
