"""ArchiDream API — upload a DXF, stylize it, generate per-room 360 panos.

Run:  uvicorn app:app --port 8000  (from ArchiDream/backend, venv active)
"""

import asyncio
import glob
import os
import subprocess
import sys
import tempfile
import threading

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image

import nano
import projects
import prompts
import styles as styles_mod
from styles import STYLES

app = FastAPI(title="ArchiDream")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"], allow_headers=["*"],
)

os.makedirs(projects.DATA_DIR, exist_ok=True)
app.mount("/data", StaticFiles(directory=projects.DATA_DIR), name="data")


class StyleBody(BaseModel):
    style: str
    model: str = "nano-banana"
    force: bool = False
    rooms: list[str] | None = None  # tour-video: which 360s to include


class EditBody(BaseModel):
    style: str
    model: str = "nano-banana"
    prompt: str
    polygon: list[list[float]]  # [[x,y], ...] in pano pixel coords


def _check_style(style_id):
    if style_id not in styles_mod.all_styles():
        raise HTTPException(400, f"unknown style '{style_id}'")


def _check_model(model_id):
    if model_id not in nano.MODEL_IDS:
        raise HTTPException(400, f"unknown model '{model_id}'")


@app.get("/api/health")
def health():
    return {"ok": True, "provider": nano.provider_name()}


@app.get("/api/models")
def image_models():
    return nano.models()


@app.get("/api/styles")
def styles():
    return list(styles_mod.all_styles().values())


class StyleCreate(BaseModel):
    name: str
    tagline: str = ""
    vibe: list[str] = []
    colors: list[dict] = []       # [{name, hex}]
    materials: list[str] = []
    accent: str = ""
    emoji: str = ""


@app.post("/api/styles")
def create_style(body: StyleCreate):
    """Add a user-defined style (persisted); usable everywhere at once."""
    try:
        return styles_mod.add_custom(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/projects")
def all_projects():
    return projects.list_projects()


@app.get("/api/projects/{pid}")
def one_project(pid: str):
    p = projects.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    return p


@app.post("/api/projects")
async def upload(file: UploadFile):
    if not (file.filename or "").lower().endswith(".dxf"):
        raise HTTPException(400, "please upload a .dxf file")
    data = await file.read()
    print(f"[upload] file={file.filename!r}  received={len(data)/1048576:.1f}MB",
          flush=True)
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        # create_project runs the extraction engine as a blocking subprocess
        # (several seconds). Off-load it to a worker thread so the single
        # event loop stays free to answer the frontend's concurrent polls
        # (GET /api/projects, /api/styles, /api/models) — otherwise those
        # requests hang and the Next proxy resets them (ECONNRESET ->
        # "Internal Server Error").
        return await asyncio.to_thread(
            projects.create_project, tmp_path, original_name=file.filename)
    except Exception as exc:
        raise HTTPException(500, f"processing failed: {exc}")
    finally:
        os.unlink(tmp_path)


@app.post("/api/projects/{pid}/stylize")
def stylize(pid: str, body: StyleBody):
    _check_style(body.style)
    _check_model(body.model)
    p = projects.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")

    key = f"{body.style}@{body.model}"
    out_dir = os.path.join(projects.pdir(pid), "stylized")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{key}.png")
    mock_path = os.path.join(out_dir, f"{key}.mock.png")
    if os.path.exists(out_path):
        return {"url": f"/data/{pid}/stylized/{key}.png", "key": key,
                "provider": "cache"}
    plan = Image.open(os.path.join(projects.pdir(pid), "plan.png"))
    img, provider = nano.stylize_top_down(
        plan.convert("RGB"), prompts.stylize_prompt(body.style),
        body.style, body.model)
    # mocks are placeholders — never cached under the real key, so the
    # next call retries the real provider once a key/network is back
    path = out_path if provider != "mock" else mock_path
    img.save(path)
    return {"url": f"/data/{pid}/stylized/{os.path.basename(path)}",
            "key": key, "provider": provider}


@app.post("/api/projects/{pid}/rooms/{room_id}/pano")
def pano(pid: str, room_id: str, body: StyleBody):
    _check_style(body.style)
    _check_model(body.model)
    p = projects.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    room = next((r for r in p["rooms"] if r["id"] == room_id), None)
    if not room:
        raise HTTPException(404, f"room '{room_id}' not found")

    key = f"{body.style}@{body.model}"
    out_dir = os.path.join(projects.pdir(pid), "panos", key)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{room_id}.png")
    mock_path = os.path.join(out_dir, f"{room_id}.mock.png")
    if os.path.exists(out_path) and not body.force:
        return {"url": f"/data/{pid}/panos/{key}/{room_id}.png",
                "room": room_id, "key": key, "provider": "cache"}
    if body.model == "nano-banana" and not body.force:
        # pre-dropdown panos were made by nano-banana: honor them as cache
        legacy = os.path.join(projects.pdir(pid), "panos", body.style,
                              f"{room_id}.png")
        if os.path.exists(legacy):
            return {"url": f"/data/{pid}/panos/{body.style}/{room_id}.png",
                    "room": room_id, "key": key, "provider": "cache"}
    crop = projects.crop_room(pid, key, room)
    img, provider = nano.room_pano(
        crop, prompts.pano_prompt(body.style, room["name"], room["area_m2"]),
        body.style, body.model)
    path = out_path if provider != "mock" else mock_path
    img.save(path)
    return {"url": f"/data/{pid}/panos/{key}/{os.path.basename(path)}",
            "room": room_id, "key": key, "provider": provider}


TOUR_DIR = os.path.normpath(os.path.join(
    projects.BACKEND_DIR, "..", "tour", "property-tour"))
FFMPEG_BIN = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Microsoft", "WinGet", "Packages",
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
    "ffmpeg-8.1.2-full_build", "bin")


# ---- tour video: async job model (a render is ~2.5 min; never block) ----
_TOUR_JOBS = {}                  # "pid::key" -> {status, url, error}
_TOUR_LOCK = threading.Lock()    # guards _TOUR_JOBS
_RENDER_LOCK = threading.Lock()  # serialize renders — the workdir is shared


def _tour_out(pid, key):
    return os.path.join(projects.pdir(pid), "tours", f"{key}.mp4")


def _set_job(jk, **kw):
    with _TOUR_LOCK:
        _TOUR_JOBS.setdefault(jk, {}).update(kw)


def _render_tour_worker(pid, key, out_path, rooms=None):
    """Runs off the request thread: compile the HyperFrames tour, render it,
    copy the mp4 into the project. Records status in _TOUR_JOBS.
    `rooms` is the optional list of room ids to include (else all with panos)."""
    jk = f"{pid}::{key}"
    try:
        env = dict(os.environ)
        env["PATH"] = FFMPEG_BIN + os.pathsep + env.get("PATH", "")
        with _RENDER_LOCK:  # one render at a time (compile bakes a shared file)
            r = subprocess.run([sys.executable, "compile_tour.py", pid, key,
                                ",".join(rooms or [])],
                               cwd=TOUR_DIR, capture_output=True, text=True,
                               env=env, timeout=120)
            if r.returncode != 0:
                _set_job(jk, status="failed",
                         error=f"compile failed: {r.stderr[-300:]}")
                return
            r = subprocess.run("npm run render", shell=True, cwd=TOUR_DIR,
                               capture_output=True, text=True, env=env,
                               timeout=900)
            if r.returncode != 0:
                _set_job(jk, status="failed",
                         error=f"render failed: {(r.stdout or '')[-300:]}")
                return
            renders = sorted(glob.glob(os.path.join(TOUR_DIR, "renders",
                             "*.mp4")), key=os.path.getmtime)
            if not renders:
                _set_job(jk, status="failed", error="render produced no mp4")
                return
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(renders[-1], "rb") as src, open(out_path, "wb") as dst:
                dst.write(src.read())
        _set_job(jk, status="done", url=f"/data/{pid}/tours/{key}.mp4",
                 error=None)
    except Exception as exc:                        # noqa: BLE001
        _set_job(jk, status="failed", error=str(exc)[-300:])


@app.post("/api/projects/{pid}/tour-video")
def start_tour_video(pid: str, body: StyleBody):
    """Kick off a HyperFrames tour render in the background, return at once.
    Poll GET /api/projects/{pid}/tour-video?style=&model= for progress."""
    _check_style(body.style)
    _check_model(body.model)
    p = projects.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")

    key = f"{body.style}@{body.model}"
    out_path = _tour_out(pid, key)
    jk = f"{pid}::{key}"

    if os.path.exists(out_path) and not body.force:
        return {"status": "done", "key": key,
                "url": f"/data/{pid}/tours/{key}.mp4", "provider": "cache"}
    if key not in p["stylized"] and body.style not in p["stylized"]:
        raise HTTPException(409, "stylize this style first")
    if not p["panos"].get(key):
        raise HTTPException(409, "generate at least one 360 room first")

    with _TOUR_LOCK:
        cur = _TOUR_JOBS.get(jk)
        if cur and cur.get("status") == "rendering":
            return {"status": "rendering", "key": key}
        _TOUR_JOBS[jk] = {"status": "rendering", "url": None, "error": None}
    threading.Thread(target=_render_tour_worker,
                     args=(pid, key, out_path, body.rooms), daemon=True).start()
    return {"status": "rendering", "key": key}


@app.post("/api/projects/{pid}/rooms/{room_id}/edit-region")
def edit_region(pid: str, room_id: str, body: EditBody):
    """Inpaint a polygonal region of an existing pano using Qwen.

    The existing pano file is overwritten in-place (same path), so the
    sphere viewer just reloads it via a cache-bust query string.
    Returns {url, provider}.
    """
    _check_style(body.style)
    _check_model(body.model)
    p = projects.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    room = next((r for r in p["rooms"] if r["id"] == room_id), None)
    if not room:
        raise HTTPException(404, f"room '{room_id}' not found")
    if len(body.polygon) < 3:
        raise HTTPException(400, "polygon must have at least 3 points")

    # target the pano the user is viewing: {style}@{model}, else the legacy
    # plain-style dir (pre-dropdown panos were made by nano-banana)
    key = f"{body.style}@{body.model}"
    pano_path = os.path.join(projects.pdir(pid), "panos", key, f"{room_id}.png")
    if not os.path.exists(pano_path):
        legacy = os.path.join(projects.pdir(pid), "panos", body.style,
                              f"{room_id}.png")
        if not os.path.exists(legacy):
            raise HTTPException(409, "generate a 360° pano for this room first")
        pano_path = legacy

    full_img = Image.open(pano_path).convert("RGB")
    edited, provider = nano.region_edit(
        full_img, body.polygon, body.prompt, body.style)
    edited.save(pano_path)

    data_rel = os.path.relpath(pano_path, projects.DATA_DIR).replace(os.sep, "/")
    bust = f"?t={os.path.getmtime(pano_path):.0f}"
    return {"url": f"/data/{data_rel}{bust}", "provider": provider}


@app.get("/api/projects/{pid}/tour-video")
def tour_video_status(pid: str, style: str, model: str = "nano-banana"):
    """Progress for one render: rendering | done(+url) | failed(+error) | idle.
    Prefers a live job, then a finished file on disk (survives restarts)."""
    key = f"{style}@{model}"
    job = _TOUR_JOBS.get(f"{pid}::{key}")
    if job and job.get("status") == "rendering":
        return {"status": "rendering", "key": key}
    if os.path.exists(_tour_out(pid, key)):
        return {"status": "done", "key": key,
                "url": f"/data/{pid}/tours/{key}.mp4"}
    if job:
        return {"status": job.get("status", "idle"), "key": key,
                "url": job.get("url"), "error": job.get("error")}
    return {"status": "idle", "key": key}
