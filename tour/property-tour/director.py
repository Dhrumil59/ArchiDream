"""IBM Granite (watsonx.ai) as the tour's DIRECTOR (not author).

Given the property facts, Granite returns a silent-video shot-plan: the room
ORDER, one short evocative CAPTION per room, and a closing TAGLINE.  It never
writes HTML or touches pixels — compile_tour.py executes the plan and
HyperFrames renders it deterministically.

`direct()` returns None (→ deterministic fallback in the compiler) whenever
credentials are missing or the call errors/times out, so the video always
renders.

Credentials are read from ArchiDream/.env, then the Cad2Visual root .env:
    WATSONX_API_KEY   — IBM Cloud API key
    WATSONX_PROJECT_ID — watsonx.ai project ID
    WATSONX_URL        — regional endpoint, e.g. https://us-south.ml.cloud.ibm.com

Model: ibm/granite-4-h-small
"""
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))

GRANITE_MODEL = "ibm/granite-4-h-small"

SYSTEM = """You direct a short, SILENT property tour video with on-screen text \
only (no narration). You get facts about a home, its interior style, and the \
exact rooms that will appear. Decide three things:

1. order   - the narrative order to visit the rooms. Aim for a walkthrough: \
arrival / shared living spaces first, private rooms (bed, bath) later. Use \
ONLY the given room ids.
2. captions - for EACH room id, one short evocative caption: max 6 words, no \
numbers, no emojis, grounded in the room and the style's mood.
3. tagline - one closing line for the whole home: max 8 words, no emojis.

Match the tone to the interior style. Never invent rooms or features that the \
facts do not imply. Respond with EXACTLY one JSON object and nothing else:
{"order": ["id", ...], "captions": {"id": "text", ...}, "tagline": "text"}"""


def _load_creds():
    """Read WATSONX_* keys from .env files into os.environ (if not already set).
    ArchiDream/.env wins over the Cad2Visual root .env."""
    for rel in (("..", "..", ".env"), ("..", "..", "..", ".env")):
        path = os.path.normpath(os.path.join(HERE, *rel))
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    os.environ.setdefault(
                        k.strip(), v.strip().strip('"').strip("'"))
        except FileNotFoundError:
            continue


_load_creds()


def _creds_ready():
    return all(os.environ.get(k) for k in (
        "WATSONX_API_KEY", "WATSONX_PROJECT_ID", "WATSONX_URL"))


def _first_json(text):
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except ValueError:
                continue
    return None


def _validate(plan, ids):
    if not isinstance(plan, dict):
        return None
    order = [i for i in plan.get("order", []) if i in ids]
    order += [i for i in ids if i not in order]   # keep any Granite dropped
    if not order:
        return None
    caps_in = plan.get("captions") or {}
    captions = {i: str(caps_in[i]).strip()[:56]
                for i in ids if str(caps_in.get(i, "")).strip()}
    tagline = str(plan.get("tagline", "")).strip()[:70]
    return {"order": order, "captions": captions, "tagline": tagline}


def _call(facts, ids):
    if not _creds_ready():
        return None

    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference
    from ibm_watsonx_ai.foundation_models.schema import TextChatParameters

    creds = Credentials(
        api_key=os.environ["WATSONX_API_KEY"],
        url=os.environ["WATSONX_URL"],
    )
    model = ModelInference(
        model_id=GRANITE_MODEL,
        credentials=creds,
        project_id=os.environ["WATSONX_PROJECT_ID"],
        params=TextChatParameters(max_tokens=900, temperature=0.7),
        validate=False,   # skip server-side model-list lookup on older SDK
    )

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",   "content": (
            json.dumps(facts, ensure_ascii=False)
            + "\n\nRoom ids to order (use exactly these): "
            + json.dumps(ids)
        )},
    ]
    resp = model.chat(messages=messages)
    content = resp["choices"][0]["message"]["content"]
    return _validate(_first_json(content), ids)


def direct(facts, ids, timeout=25):
    """Shot-plan dict, or None to fall back to the deterministic cut.

    Runs in a daemon thread so a slow/hung call never stalls the render.
    Returns None silently on any failure — compile_tour.py handles the fallback.
    """
    box = {}

    def work():
        try:
            box["plan"] = _call(facts, ids)
        except Exception as exc:          # noqa: BLE001
            box["err"] = repr(exc)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("plan")
