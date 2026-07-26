"""Nano Banana (Gemini image) client with a local mock fallback.

If GEMINI_API_KEY is present (ArchiDream/.env or the Cad2Visual root .env)
and the google-genai SDK imports, real calls are made; otherwise a mock
provider produces styled placeholders so the entire product flow is demoable
offline. Every result reports which provider produced it.
"""

import io
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from styles import STYLES, get_style

NANO_MODEL = os.environ.get("NANO_MODEL", "gemini-3-pro-image-preview")

PANO_W, PANO_H = 4096, 2048  # equirect 2:1 delivered to the viewer


def _load_env():
    """Populate os.environ from .env files without requiring python-dotenv.
    ArchiDream/.env is read first so the project-local file wins over the
    Cad2Visual root .env; quoted values are unquoted."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", ".env"),          # ArchiDream/.env
        os.path.join(here, "..", "..", ".env"),   # Cad2Visual root
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(
                    k.strip(), v.strip().strip('"').strip("'"))


_load_env()


QWEN_MODEL = os.environ.get("QWEN_IMAGE_MODEL", "qwen-image-edit-plus-2025-12-15")
# "Nano Banana 2 Lite" = Gemini 3.1 Flash Lite Image. Override the exact id
# with NANO2_MODEL in .env if the model string differs (e.g. a -preview suffix).
NANO2_MODEL = os.environ.get("NANO2_MODEL", "gemini-3.1-flash-lite-image")

# dropdown model id -> underlying Gemini image model id
GEMINI_MODELS = {
    "nano-banana": NANO_MODEL,
    "nano-banana-2-lite": NANO2_MODEL,
}


def _client():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=key)
    except ImportError:
        return None


def _qwen_ready():
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return False
    try:
        import dashscope  # noqa: F401
        return True
    except ImportError:
        return False


# the dropdown: model id -> metadata + availability
def models():
    gem = _client() is not None
    return [
        {"id": "nano-banana", "name": "Nano Banana (Gemini)", "available": gem},
        {"id": "nano-banana-2-lite", "name": "Nano Banana 2 Lite",
         "available": gem},
        {"id": "qwen", "name": "Qwen Image Edit", "available": _qwen_ready()},
    ]


MODEL_IDS = ("nano-banana", "nano-banana-2-lite", "qwen")


def provider_name(model="nano-banana"):
    ok = next((m["available"] for m in models() if m["id"] == model), False)
    return model if ok else "mock"


def _generate(prompt, image, aspect_ratio=None, model=None):
    """One image-edit call. Returns a PIL Image or raises."""
    from google.genai import types
    client = _client()
    config = types.GenerateContentConfig(response_modalities=["IMAGE"],
                                         temperature=0)  # near-deterministic
    if aspect_ratio:
        try:
            config.image_config = types.ImageConfig(aspect_ratio=aspect_ratio)
        except Exception:
            pass  # older SDKs: let the model pick, we resize afterwards
    resp = client.models.generate_content(
        model=model or NANO_MODEL, contents=[prompt, image], config=config)
    for cand in resp.candidates or []:
        for part in cand.content.parts or []:
            data = getattr(part, "inline_data", None)
            if data and data.data:
                return Image.open(io.BytesIO(data.data)).convert("RGB")
    raise RuntimeError(f"no image in response: {resp}")


def _qwen_generate(prompt, image, size=None):
    """One DashScope image-edit call (same pattern as Cad2Visual's
    stylize.py). Takes a PIL image, returns a PIL image or raises."""
    import base64
    import tempfile
    import urllib.request

    import dashscope
    from dashscope import MultiModalConversation
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        image.save(tmp, "PNG")
        tmp_path = tmp.name
    try:
        content = [{"image": "file://" + tmp_path.replace("\\", "/")},
                   {"text": prompt}]

        def call(with_size):
            kwargs = dict(
                api_key=os.environ["DASHSCOPE_API_KEY"], model=QWEN_MODEL,
                messages=[{"role": "user", "content": content}],
                result_format="message", stream=False,
                watermark=False, negative_prompt="", temperature=0)
            if with_size and size:
                kwargs["size"] = size
            return MultiModalConversation.call(**kwargs)

        resp = call(True)
        if resp.status_code != 200 and size:
            resp = call(False)  # size unsupported: retry without
        if resp.status_code != 200:
            raise RuntimeError(f"{resp.code}: {resp.message}")
        for part in resp.output["choices"][0]["message"]["content"]:
            url = part.get("image") if isinstance(part, dict) else None
            if url:
                if url.startswith("data:"):
                    raw = base64.b64decode(url.split(",", 1)[1])
                else:
                    with urllib.request.urlopen(url, timeout=120) as r:
                        raw = r.read()
                return Image.open(io.BytesIO(raw)).convert("RGB")
        raise RuntimeError("no image in qwen response")
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Mock provider — keeps the demo alive without a key / on API failure
# ---------------------------------------------------------------------------

def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mock_stylize(plan_img, style_id):
    s = get_style(style_id) or STYLES[style_id]
    base = plan_img.convert("RGB")
    warm = Image.new("RGB", base.size, _hex_rgb(s["colors"][1]["hex"]))
    out = Image.blend(base, warm, 0.28)
    out = ImageEnhance.Contrast(out).enhance(1.05)
    d = ImageDraw.Draw(out)
    d.text((14, 12), f"PREVIEW  ·  {s['name']}  ·  set GEMINI_API_KEY "
                     "for real renders", fill=_hex_rgb(s["accent"]))
    return out


def _mock_pano(crop_img, style_id):
    s = get_style(style_id) or STYLES[style_id]
    top = _hex_rgb(s["colors"][0]["hex"])
    bottom = _hex_rgb(s["colors"][-1]["hex"])
    pano = Image.new("RGB", (PANO_W, PANO_H))
    px = pano.load()
    for y in range(PANO_H):
        t = y / PANO_H
        row = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
        for x in range(0, PANO_W, 8):
            for dx in range(8):
                if x + dx < PANO_W:
                    px[x + dx, y] = row
    band_h = PANO_H // 2
    band = crop_img.convert("RGB").resize((PANO_W, band_h))
    band = band.filter(ImageFilter.GaussianBlur(6))
    pano.paste(band, (0, (PANO_H - band_h) // 2))
    d = ImageDraw.Draw(pano)
    d.text((PANO_W // 2 - 260, PANO_H // 2 - 12),
           "PREVIEW PANORAMA — set GEMINI_API_KEY for real 360 renders",
           fill=_hex_rgb(s["accent"]))
    return pano


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def region_edit(full_img, polygon, prompt, style_id):
    """Edit a polygonal region of a pano with the proven qwen-image-edit-plus
    path (the same model that generates the panos): crop the polygon's padded
    bounding box, instruction-edit that crop, then paste the polygon interior
    back (feathered) so only the selection changes.

    full_img  — PIL Image (the full pano, 4096×2048)
    polygon   — list of [x, y] pairs in full_img pixel coords (≥3 points)
    prompt    — user's free-text instruction for what to put in the region
    style_id  — used only by the mock fallback tint

    Falls back to a mock tinted overlay when Qwen is unavailable or the call
    fails. Returns (edited_full_img, provider).
    """
    if len(polygon) < 3 or not _qwen_ready():
        return _mock_region_edit(full_img, polygon, prompt, style_id), "mock"

    W, H = full_img.size
    pts = [(int(x), int(y)) for x, y in polygon]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad = 48  # padding gives the model surrounding context to match
    x0, y0 = max(0, min(xs) - pad), max(0, min(ys) - pad)
    x1, y1 = min(W, max(xs) + pad), min(H, max(ys) + pad)
    if x1 - x0 < 16 or y1 - y0 < 16:
        return full_img.copy(), "mock"

    crop = full_img.crop((x0, y0, x1, y1))
    cw, ch = crop.size
    # a clear edit instruction yields a targeted, blended change — the model
    # only sees the cropped region plus its padding as context
    instruction = (
        f"{prompt.strip()}. Apply this change to the image photorealistically, "
        "matching the existing lighting, perspective, materials and colours, "
        "and keep the rest of the scene consistent. No text, no watermark.")
    try:
        edited = _qwen_generate(instruction, crop, size=f"{cw}*{ch}")
        edited = edited.resize((cw, ch), Image.LANCZOS)
    except Exception as exc:
        print(f"  ! qwen region_edit failed, using mock: {exc}")
        return _mock_region_edit(full_img, polygon, prompt, style_id), "mock"

    # paste only the polygon interior back (feathered) — nothing outside moves
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(6))
    out = full_img.copy()
    out.paste(edited, (x0, y0), mask.crop((x0, y0, x1, y1)))
    return out.convert("RGB"), "qwen"


def _mock_region_edit(full_img, polygon, prompt, style_id):
    """Tinted polygon overlay so the flow is demoable without a key."""
    from PIL import ImageDraw as _ID
    s = get_style(style_id) or STYLES.get(style_id, list(STYLES.values())[0])
    r, g, b = _hex_rgb(s["accent"])
    W, H = full_img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = _ID.Draw(overlay)
    pts = [(int(x), int(y)) for x, y in polygon]
    d.polygon(pts, fill=(r, g, b, 90))
    d.line(pts + [pts[0]], fill=(r, g, b, 220), width=6)
    out = full_img.copy().convert("RGBA")
    out = Image.alpha_composite(out, overlay).convert("RGB")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    _ID.Draw(out).text(
        (min(xs) + 8, min(ys) + 8),
        f"PREVIEW: {prompt[:48]}",
        fill=(r, g, b),
    )
    return out


def stylize_top_down(plan_img, prompt, style_id, model="nano-banana"):
    """Stylize the plan render with the chosen model; output is resized to
    match the input exactly so the SVG room hotspots stay aligned.
    Returns (image, provider) — provider is "mock" on any failure so
    callers can avoid caching placeholders as real results."""
    if model == "qwen" and _qwen_ready():
        try:
            w, h = plan_img.size
            out = _qwen_generate(prompt, plan_img, size=f"{w}*{h}")
            return out.resize(plan_img.size, Image.LANCZOS), "qwen"
        except Exception as exc:
            print(f"  ! qwen stylize failed, using mock: {exc}")
    elif model in GEMINI_MODELS and _client():
        try:
            out = _generate(prompt, plan_img, model=GEMINI_MODELS[model])
            return out.resize(plan_img.size, Image.LANCZOS), model
        except Exception as exc:
            print(f"  ! gemini stylize failed, using mock: {exc}")
    return _mock_stylize(plan_img, style_id), "mock"


def room_pano(crop_img, prompt, style_id, model="nano-banana"):
    """Generate an equirect 360 for one room crop with the chosen model.
    Neither provider has a 2:1 preset: Gemini is asked for 16:9, Qwen for
    2048*1024; both are resized to exactly 2:1. Returns (image, provider)."""
    if model == "qwen" and _qwen_ready():
        try:
            out = _qwen_generate(prompt, crop_img, size="2048*1024")
            return out.resize((PANO_W, PANO_H), Image.LANCZOS), "qwen"
        except Exception as exc:
            print(f"  ! qwen pano failed, using mock: {exc}")
    elif model in GEMINI_MODELS and _client():
        try:
            out = _generate(prompt, crop_img, aspect_ratio="16:9",
                            model=GEMINI_MODELS[model])
            return out.resize((PANO_W, PANO_H), Image.LANCZOS), model
        except Exception as exc:
            print(f"  ! gemini pano failed, using mock: {exc}")
    return _mock_pano(crop_img, style_id), "mock"
