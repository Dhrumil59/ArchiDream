"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import StylePicker from "@/components/StylePicker";
import PlanStage from "@/components/PlanStage";
import PanoModal from "@/components/PanoModal";
import Library from "@/components/Library";
import TourComposer from "@/components/TourComposer";
import NewStyleModal from "@/components/NewStyleModal";

function ProjectView() {
  const { id } = useParams();
  const router = useRouter();
  const search = useSearchParams();

  const [project, setProject] = useState(null);
  const [styles, setStyles] = useState([]);
  const [models, setModels] = useState([]);
  const [style, setStyle] = useState(search.get("style") || "scandinavian");
  const [model, setModel] = useState(search.get("model") || "nano-banana");
  const [view, setView] = useState("styled");
  const [stylizing, setStylizing] = useState(false);
  const [panos, setPanos] = useState({}); // "style@model" -> {roomId: url}
  const [generating, setGenerating] = useState(new Set());
  const [open, setOpen] = useState(null); // {room, url}
  const [newStyle, setNewStyle] = useState(false);
  const [error, setError] = useState(null);
  const stylizeStarted = useRef(new Set());

  const styleDef = styles.find((s) => s.id === style);
  const accent = styleDef?.accent || "#c9a35c";
  const key = `${style}@${model}`;
  const stylizedUrl = project?.stylized?.[key];
  const stylePanos = panos[key] || {};
  const modelDef = models.find((m) => m.id === model);
  const mockMode = modelDef && !modelDef.available;

  useEffect(() => {
    api.styles().then(setStyles).catch(() => {});
    api.models().then(setModels).catch(() => {});
    api
      .project(id)
      .then((p) => {
        setProject(p);
        setPanos(p.panos || {});
      })
      .catch((e) => setError(String(e.message || e)));
  }, [id]);

  // ensure the active style+model top-down exists (server caches per key)
  useEffect(() => {
    if (!project || project.stylized?.[key]) return;
    if (stylizeStarted.current.has(key)) return;
    stylizeStarted.current.add(key);
    setStylizing(true);
    api
      .stylize(project.id, style, model)
      .then(({ url, provider }) => {
        setProject((p) => ({
          ...p,
          stylized: { ...p.stylized, [key]: url },
        }));
        // a mock result is not cached server-side: allow a retry later
        if (provider === "mock") stylizeStarted.current.delete(key);
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setStylizing(false));
  }, [project, style, model, key]);

  // per-room generation, triggered from the room modal
  const generateRoom = useCallback(
    async (room, force = false) => {
      setGenerating((g) => new Set(g).add(room.id));
      try {
        const bust = force ? `?t=${Date.now()}` : "";
        const { url } = await api.pano(project.id, room.id, style, model,
                                       force);
        const fresh = url + bust;
        setPanos((p) => ({
          ...p,
          [key]: { ...(p[key] || {}), [room.id]: fresh },
        }));
        return fresh;
      } finally {
        setGenerating((g) => {
          const next = new Set(g);
          next.delete(room.id);
          return next;
        });
      }
    },
    [project, style, model, key]
  );

  const onRoom = (room) => setOpen({ room });

  const onEdited = useCallback((newUrl) => {
    // capture roomId at call-time from the open ref to avoid stale closure
    const rid = open?.room?.id;
    if (!rid) return;
    setPanos((p) => ({
      ...p,
      [key]: { ...(p[key] || {}), [rid]: newUrl },
    }));
  }, [key, open]);

  // per-(style@model) tour render jobs, polled to completion in the background
  const [tourJobs, setTourJobs] = useState({}); // key -> "rendering" | "failed"
  const pollers = useRef({}); // key -> interval id

  const pollTour = useCallback(
    (st, md) => {
      const k = `${st}@${md}`;
      clearInterval(pollers.current[k]);
      pollers.current[k] = setInterval(async () => {
        try {
          const s = await api.tourStatus(project.id, st, md);
          if (s.status === "done") {
            clearInterval(pollers.current[k]);
            delete pollers.current[k];
            const fresh = s.url + `?t=${Date.now()}`;
            setProject((p) => ({ ...p, tours: { ...p.tours, [k]: fresh } }));
            setTourJobs((j) => {
              const n = { ...j };
              delete n[k];
              return n;
            });
          } else if (s.status === "failed") {
            clearInterval(pollers.current[k]);
            delete pollers.current[k];
            setTourJobs((j) => ({ ...j, [k]: "failed" }));
            setError(s.error || "tour render failed");
          }
        } catch {
          /* transient — keep polling */
        }
      }, 3000);
    },
    [project]
  );

  // render one style's tour; returns immediately, then polls in the background
  const makeTour = useCallback(
    async (st = style, md = model, roomIds = null) => {
      const k = `${st}@${md}`;
      const force = !!project?.tours?.[k];
      setError(null);
      setTourJobs((j) => ({ ...j, [k]: "rendering" }));
      try {
        const res = await api.tourVideo(project.id, st, md, force, roomIds);
        if (res.status === "done") {
          // cache-bust: the mp4 keeps the same path, so force a fresh fetch
          const fresh = res.url + `?t=${Date.now()}`;
          setProject((p) => ({ ...p, tours: { ...p.tours, [k]: fresh } }));
          setTourJobs((j) => {
            const n = { ...j };
            delete n[k];
            return n;
          });
        } else {
          pollTour(st, md); // "rendering" — poll until done/failed
        }
      } catch (e) {
        setError(String(e.message || e));
        setTourJobs((j) => {
          const n = { ...j };
          delete n[k];
          return n;
        });
      }
    },
    [project, style, model, pollTour]
  );

  useEffect(
    () => () => Object.values(pollers.current).forEach(clearInterval),
    []
  );

  // if this style's tour is still rendering on the server (navigated away and
  // back, or reloaded mid-render), resume showing progress and polling
  useEffect(() => {
    if (!project) return;
    let cancelled = false;
    api
      .tourStatus(project.id, style, model)
      .then((s) => {
        if (cancelled || s.status !== "rendering" || pollers.current[key])
          return;
        setTourJobs((j) => ({ ...j, [key]: "rendering" }));
        pollTour(style, model);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [project?.id, style, model, key, pollTour]);

  // open a room's 360 for a specific style@model (from the Library)
  const openRoomFor = useCallback((room, k) => {
    const [st, md] = k.split("@");
    setStyle(st);
    setModel(md);
    setView("styled");
    setOpen({ room });
  }, []);

  if (error && !project) {
    return (
      <main className="max-w-3xl mx-auto px-6 py-24 text-center">
        <p className="font-display text-3xl">Something broke.</p>
        <p className="text-fog mt-3">{error}</p>
        <button
          onClick={() => router.push("/")}
          className="btn-gold rounded-full px-8 py-3 mt-8 cursor-pointer"
        >
          Back home
        </button>
      </main>
    );
  }

  if (!project) {
    return (
      <main className="max-w-5xl mx-auto px-6 py-24">
        <div className="shimmer rounded-2xl h-[60vh]" />
      </main>
    );
  }

  const readyCount = project.rooms.filter((r) => stylePanos[r.id]).length;

  return (
    <main className="max-w-6xl mx-auto px-6 pb-24">
      <nav className="flex items-center justify-between py-6">
        <button
          onClick={() => router.push("/")}
          className="font-display text-2xl tracking-tight cursor-pointer"
        >
          Archi<span className="text-gold">Dream</span>
        </button>
        <div className="flex items-center gap-3">
          {mockMode && (
            <span className="text-xs border border-amber-400/40 text-amber-300 rounded-full px-3 py-1">
              preview mode — add {model === "qwen" ? "DASHSCOPE" : "GEMINI"}
              _API_KEY for real renders
            </span>
          )}
          <span className="text-fog text-sm capitalize hidden sm:block">
            {project.name}
          </span>
        </div>
      </nav>

      {/* controls */}
      <div className="flex flex-wrap items-center justify-between gap-4 mb-5 rise">
        {/* view toggle */}
        <div className="glass rounded-full p-1 flex">
          {[
            ["plan", "Blueprint"],
            ["styled", "Dream"],
            ["library", "Library"],
          ].map(([v, label]) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`seg rounded-full px-5 py-2 text-sm cursor-pointer ${
                view === v
                  ? "bg-ivory text-ink font-semibold"
                  : "text-fog hover:text-ivory"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-3">
          {/* image model dropdown */}
          <label className="glass rounded-full px-4 py-2 flex items-center gap-2 text-sm">
            <span className="text-fog">Model</span>
            <select
              value={model}
              onChange={(e) => {
                setModel(e.target.value);
                setView("styled");
              }}
              className="bg-transparent text-ivory outline-none cursor-pointer"
            >
              {(models.length
                ? models
                : [{ id: "nano-banana", name: "Nano Banana (Gemini)", available: true }]
              ).map((m) => (
                <option key={m.id} value={m.id} className="bg-ink">
                  {m.name}
                  {m.available ? "" : " (preview)"}
                </option>
              ))}
            </select>
          </label>

          <StylePicker
            styles={styles}
            selected={style}
            onSelect={(s) => {
              setStyle(s);
              setView("styled");
            }}
            onAdd={() => setNewStyle(true)}
            compact
          />
        </div>
      </div>

      {/* stage */}
      <div className="rise" style={{ animationDelay: "0.08s" }}>
        {view === "library" ? (
          <Library
            project={project}
            panos={panos}
            styles={styles}
            models={models}
            tourJobs={tourJobs}
            onOpenStyle={(st, md) => {
              setStyle(st);
              setModel(md);
              setView("styled");
            }}
            onOpenRoom={openRoomFor}
            onMakeTour={makeTour}
          />
        ) : (
          <PlanStage
            project={project}
            accent={accent}
            view={stylizedUrl ? view : "plan"}
            stylizedUrl={stylizedUrl}
            stylizing={stylizing}
            panos={stylePanos}
            generating={generating}
            onRoom={onRoom}
          />
        )}
      </div>

      {/* footer actions (hidden in Library — each card carries its own) */}
      {view !== "library" && (
        <div className="flex flex-col items-center mt-8 gap-3">
          <p className="text-fog/80 text-sm">
            click any room to enter it — {readyCount}/{project.rooms.length}{" "}
            <span style={{ color: accent }}>360°</span> rooms generated
          </p>
          {stylizedUrl && (
            <TourComposer
              rooms={project.rooms}
              panoUrls={stylePanos}
              resetKey={key}
              accent={accent}
              status={tourJobs[key]}
              tourUrl={project.tours?.[key]}
              onMake={(ids) => makeTour(style, model, ids)}
            />
          )}
          {error && <p className="text-red-400 text-sm">{error}</p>}
        </div>
      )}

      {open && (
        <PanoModal
          projectId={id}
          roomId={open.room.id}
          title={open.room.name}
          url={stylePanos[open.room.id]}
          accent={accent}
          busy={generating.has(open.room.id)}
          style={style}
          model={model}
          onGenerate={(force) => generateRoom(open.room, force)}
          onEdited={onEdited}
          onClose={() => setOpen(null)}
        />
      )}

      {newStyle && (
        <NewStyleModal
          onClose={() => setNewStyle(false)}
          onCreated={(s) => {
            setStyles((prev) =>
              prev.some((x) => x.id === s.id) ? prev : [...prev, s]
            );
            setStyle(s.id);
            setView("styled");
            setNewStyle(false);
          }}
        />
      )}
    </main>
  );
}

export default function ProjectPage() {
  return (
    <Suspense fallback={null}>
      <ProjectView />
    </Suspense>
  );
}
