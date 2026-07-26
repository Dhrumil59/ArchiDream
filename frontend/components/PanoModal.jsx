"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { api } from "@/lib/api";

/**
 * Room window — three modes:
 *   "view"   : 360° sphere viewer (drag/zoom)
 *   "edit"   : flat-panel pano with polygon-draw + prompt for region inpaint
 *   "empty"  : no pano yet → generate CTA
 *
 * Props:
 *   projectId, roomId, title, url, accent, busy, style
 *   onGenerate(force) → Promise
 *   onEdited(newUrl)  → called after a successful region edit
 *   onClose()
 */
export default function PanoModal({
  projectId,
  roomId,
  title,
  url,
  accent,
  busy,
  style,
  model,
  onGenerate,
  onEdited,
  onClose,
}) {
  const mountRef = useRef(null);
  const [mode, setMode] = useState("view"); // "view" | "edit"
  const [err, setErr] = useState(null);

  // ── polygon draw state ──────────────────────────────────────────────────
  const [points, setPoints] = useState([]);   // [[x,y],...] in image coords
  const [closed, setClosed] = useState(false);
  const [editPrompt, setEditPrompt] = useState("");
  const [editBusy, setEditBusy] = useState(false);
  const imgRef = useRef(null);   // the flat pano <img>
  const svgRef = useRef(null);   // the overlay <svg>

  // reset polygon when switching modes
  useEffect(() => {
    if (mode === "edit") {
      setPoints([]);
      setClosed(false);
      setEditPrompt("");
      setErr(null);
    }
  }, [mode]);

  // Escape always closes
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        if (mode === "edit") setMode("view");
        else onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, mode]);

  // ── 360 sphere viewer (only mounted in "view" mode while url exists) ────
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount || !url || mode !== "view") return;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(mount.clientWidth, mount.clientHeight);
    mount.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(
      75, mount.clientWidth / mount.clientHeight, 0.1, 100
    );
    const geo = new THREE.SphereGeometry(50, 64, 48);
    geo.scale(-1, 1, 1);
    const tex = new THREE.TextureLoader().load(url);
    tex.colorSpace = THREE.SRGBColorSpace;
    const sphere = new THREE.Mesh(
      geo, new THREE.MeshBasicMaterial({ map: tex })
    );
    scene.add(sphere);

    let lon = 0, lat = 0, down = null, lastMove = 0, raf;
    const onDown = (e) => { down = { x: e.clientX, y: e.clientY, lon, lat }; mount.style.cursor = "grabbing"; };
    const onMove = (e) => {
      if (!down) return;
      lon = down.lon - (e.clientX - down.x) * 0.18;
      lat = Math.max(-85, Math.min(85, down.lat + (e.clientY - down.y) * 0.18));
      lastMove = performance.now();
    };
    const onUp = () => { down = null; mount.style.cursor = "grab"; };
    const onWheel = (e) => {
      e.preventDefault();
      camera.fov = Math.max(35, Math.min(95, camera.fov + e.deltaY * 0.04));
      camera.updateProjectionMatrix();
    };
    const onResize = () => {
      camera.aspect = mount.clientWidth / mount.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(mount.clientWidth, mount.clientHeight);
    };
    mount.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    mount.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("resize", onResize);
    mount.style.cursor = "grab";

    const animate = () => {
      raf = requestAnimationFrame(animate);
      if (!down && performance.now() - lastMove > 2500) lon += 0.035;
      const phi = THREE.MathUtils.degToRad(90 - lat);
      const theta = THREE.MathUtils.degToRad(lon);
      camera.lookAt(
        Math.sin(phi) * Math.cos(theta),
        Math.cos(phi),
        Math.sin(phi) * Math.sin(theta)
      );
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(raf);
      mount.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      mount.removeEventListener("wheel", onWheel);
      window.removeEventListener("resize", onResize);
      tex.dispose(); geo.dispose(); sphere.material.dispose();
      renderer.dispose(); renderer.domElement.remove();
    };
  }, [url, mode]);

  // ── polygon helpers ──────────────────────────────────────────────────────

  /** The rendered image content box inside the element, accounting for the
   *  object-contain letterbox (the pano is 2:1 so it rarely fills the box). */
  const getImgRect = () => {
    const img = imgRef.current;
    if (!img) return null;
    const r = img.getBoundingClientRect();
    const ar = 4096 / 2048; // pano aspect
    let w = r.width, h = r.height, left = r.left, top = r.top;
    if (r.width / r.height > ar) {
      w = r.height * ar;              // element wider → letterbox L/R
      left = r.left + (r.width - w) / 2;
    } else {
      h = r.width / ar;              // element taller → letterbox T/B
      top = r.top + (r.height - h) / 2;
    }
    return { left, top, width: w, height: h };
  };

  /** Pointer event → pano pixel coords (4096×2048); null if outside the image. */
  const toPanoCoords = (e) => {
    const r = getImgRect();
    if (!r) return null;
    const rx = (e.clientX - r.left) / r.width;
    const ry = (e.clientY - r.top) / r.height;
    if (rx < 0 || rx > 1 || ry < 0 || ry > 1) return null; // clicked the letterbox
    return [Math.round(rx * 4096), Math.round(ry * 2048)];
  };

  /** Screen-space distance (px) between a pointer and a stored pano point. */
  const screenDist = (e, pt) => {
    const r = getImgRect();
    if (!r) return Infinity;
    const sx = (pt[0] / 4096) * r.width + r.left;
    const sy = (pt[1] / 2048) * r.height + r.top;
    return Math.hypot(e.clientX - sx, e.clientY - sy);
  };

  const handleSvgClick = (e) => {
    if (closed || editBusy) return;
    e.stopPropagation();
    // close polygon if near first point
    if (points.length >= 3 && screenDist(e, points[0]) < 14) {
      setClosed(true);
      return;
    }
    const pt = toPanoCoords(e);
    if (pt) setPoints((p) => [...p, pt]);
  };

  const handleUndo = () => {
    if (closed) { setClosed(false); return; }
    setPoints((p) => p.slice(0, -1));
  };

  const handleClearPoly = () => { setPoints([]); setClosed(false); };

  /** SVG points in pano user units — the overlay's viewBox is 0 0 4096 2048,
   *  and preserveAspectRatio="xMidYMid meet" letterboxes it exactly like the
   *  object-contain image, so the overlay aligns with the pano. */
  const svgPts = points.map(([x, y]) => `${x},${y}`).join(" ");

  // ── region edit submit ───────────────────────────────────────────────────
  const submitEdit = async () => {
    if (!closed || !editPrompt.trim() || editBusy) return;
    setEditBusy(true);
    setErr(null);
    try {
      const { url: newUrl, provider } = await api.editRegion(
        projectId, roomId, style, model, editPrompt.trim(), points
      );
      onEdited(newUrl);
      setMode("view");
      setPoints([]);
      setClosed(false);
      setEditPrompt("");
      if (provider === "mock") {
        setErr("Preview only — the Qwen edit couldn't run (check DASHSCOPE_API_KEY). Showing a placeholder tint, not a real edit.");
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setEditBusy(false);
    }
  };

  const runGenerate = async (force) => {
    setErr(null);
    try { await onGenerate(force); }
    catch (e) { setErr(String(e.message || e)); }
  };

  // ── render ───────────────────────────────────────────────────────────────
  return (
    <div className="fixed inset-0 z-50 bg-ink/92 backdrop-blur-md flex flex-col rise">

      {/* ── header ── */}
      <div className="flex items-center justify-between px-6 py-4 shrink-0">
        <div className="flex items-center gap-3">
          <span className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: accent }} />
          <span className="font-display text-xl capitalize">{title}</span>
          {url && mode === "view" && (
            <span className="text-fog text-sm">drag to look · scroll to zoom</span>
          )}
          {mode === "edit" && (
            <span className="text-fog text-sm">
              {!closed
                ? points.length === 0
                  ? "click to place polygon points — click near start to close"
                  : `${points.length} point${points.length > 1 ? "s" : ""} — keep clicking, or click near start to close`
                : "polygon closed — describe your edit below"}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {url && mode === "view" && (
            <>
              <button onClick={() => setMode("edit")}
                className="btn-ghost glass rounded-full px-4 py-1.5 text-sm cursor-pointer"
                title="Paint-edit a region of this pano">
                ✏️ Edit region
              </button>
              <button onClick={() => runGenerate(true)} disabled={busy}
                className="btn-ghost glass rounded-full px-4 py-1.5 text-sm cursor-pointer">
                {busy ? "Dreaming… ~40 s" : "↻ Regenerate"}
              </button>
            </>
          )}
          {mode === "edit" && (
            <>
              {points.length > 0 && (
                <button onClick={handleUndo}
                  className="btn-ghost glass rounded-full px-3 py-1.5 text-sm cursor-pointer"
                  title="Undo last point">
                  ← Undo
                </button>
              )}
              {points.length > 0 && (
                <button onClick={handleClearPoly}
                  className="btn-ghost glass rounded-full px-3 py-1.5 text-sm cursor-pointer"
                  title="Clear polygon">
                  ✕ Clear
                </button>
              )}
              <button onClick={() => setMode("view")}
                className="btn-ghost glass rounded-full px-4 py-1.5 text-sm cursor-pointer">
                ← Back to 360°
              </button>
            </>
          )}
          <button onClick={onClose}
            className="btn-ghost rounded-full h-10 w-10 text-lg cursor-pointer"
            aria-label="close">✕</button>
        </div>
      </div>

      {err && <p className="text-red-400 text-sm px-6 pb-2">{err}</p>}

      {/* ── body ── */}
      {!url ? (
        /* no pano yet */
        <div className="flex-1 mx-4 mb-4 rounded-2xl overflow-hidden grid place-items-center border border-white/10">
          <div className="text-center">
            <p className="font-display text-3xl">No 360° view for this room yet</p>
            <p className="text-fog mt-3 text-sm">one click renders this room in your chosen style</p>
            <button onClick={() => runGenerate(false)} disabled={busy}
              className="btn-gold rounded-full px-9 py-3.5 mt-8 font-semibold cursor-pointer">
              {busy ? "Dreaming… ~40 s" : "Generate 360° ✦"}
            </button>
          </div>
        </div>

      ) : mode === "view" ? (
        /* 360 sphere */
        <div ref={mountRef}
             className="flex-1 mx-4 mb-4 rounded-2xl overflow-hidden" />

      ) : (
        /* ── flat-panel polygon editor ── */
        <div className="flex-1 mx-4 mb-4 flex flex-col gap-3 min-h-0">
          {/* pano image + svg overlay */}
          <div className="relative flex-1 rounded-2xl overflow-hidden border border-white/10 bg-black min-h-0 cursor-crosshair"
               onClick={handleSvgClick}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img ref={imgRef} src={url} alt="pano"
                 className="w-full h-full object-contain select-none"
                 draggable={false} />
            {/* polygon overlay svg — same coordinate space as img display */}
            <svg ref={svgRef}
                 className="absolute inset-0 w-full h-full pointer-events-none"
                 viewBox="0 0 4096 2048"
                 preserveAspectRatio="xMidYMid meet">
              {/* filled polygon when closed */}
              {closed && points.length >= 3 && (
                <polygon
                  points={svgPts}
                  fill={accent + "40"}
                  stroke={accent}
                  strokeWidth="10"
                />
              )}
              {/* open polyline while drawing */}
              {!closed && points.length >= 2 && (
                <polyline
                  points={svgPts}
                  fill="none"
                  stroke={accent}
                  strokeWidth="10"
                  strokeDasharray="24 16"
                />
              )}
              {/* vertices */}
              {points.map(([x, y], i) => (
                <circle
                  key={i}
                  cx={x}
                  cy={y}
                  r={i === 0 && !closed ? 34 : 22}
                  fill={i === 0 ? accent : "#fff"}
                  stroke={accent}
                  strokeWidth="6"
                  opacity={i === 0 ? 0.9 : 0.75}
                />
              ))}
            </svg>

            {/* instruction badge */}
            {!closed && points.length === 0 && (
              <div className="absolute bottom-3 left-1/2 -translate-x-1/2 glass rounded-full px-4 py-2 text-sm text-fog pointer-events-none">
                click to start drawing the region to edit
              </div>
            )}
            {!closed && points.length >= 3 && (
              <div className="absolute bottom-3 left-1/2 -translate-x-1/2 glass rounded-full px-4 py-2 text-sm pointer-events-none"
                   style={{ color: accent }}>
                click near the first point (●) to close the polygon
              </div>
            )}
          </div>

          {/* prompt + submit — only visible once polygon is closed */}
          {closed && (
            <div className="flex gap-3 pb-1 shrink-0">
              <input
                type="text"
                value={editPrompt}
                onChange={(e) => setEditPrompt(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submitEdit()}
                placeholder="describe what to put here — e.g. 'a terracotta reading chair'"
                className="flex-1 bg-white/5 border border-white/15 rounded-full px-5 py-3 text-ivory outline-none focus:border-white/40 placeholder:text-fog/50"
                disabled={editBusy}
                autoFocus
              />
              <button
                onClick={submitEdit}
                disabled={editBusy || !editPrompt.trim()}
                className="btn-gold rounded-full px-7 py-3 font-semibold cursor-pointer shrink-0"
              >
                {editBusy ? "Painting… ~30 s" : "Apply edit ✦"}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
