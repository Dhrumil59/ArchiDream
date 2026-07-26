"use client";

/**
 * Library — every generation for this project, one card per style@model:
 * the styled top-down, room-360 progress, and the tour video with live
 * render status. This is the "see everything we generated" view.
 */
export default function Library({
  project,
  panos,
  styles,
  models,
  tourJobs,
  onOpenStyle,
  onOpenRoom,
  onMakeTour,
}) {
  const keys = Array.from(
    new Set([
      ...Object.keys(project.stylized || {}),
      ...Object.keys(project.tours || {}),
      ...Object.keys(panos || {}),
    ])
  ).sort();

  if (keys.length === 0) {
    return (
      <div className="glass rounded-2xl p-12 text-center">
        <p className="font-display text-2xl">Nothing generated yet</p>
        <p className="text-fog mt-2 text-sm">
          Pick a style and generate a styled view, room 360s, or a tour video —
          they all collect here.
        </p>
      </div>
    );
  }

  const styleName = (id) => styles.find((s) => s.id === id)?.name || id;
  const modelName = (id) => models.find((m) => m.id === id)?.name || id;

  return (
    <div className="flex flex-col gap-5">
      {keys.map((k) => {
        const [st, md] = k.split("@");
        const accent = styles.find((s) => s.id === st)?.accent || "#c9a35c";
        const stylizedUrl = project.stylized?.[k];
        const tourUrl = project.tours?.[k];
        const roomPanos = panos?.[k] || {};
        const ready = project.rooms.filter((r) => roomPanos[r.id]).length;
        const total = project.rooms.length;
        const busy = tourJobs?.[k] === "rendering";
        const failed = tourJobs?.[k] === "failed";

        return (
          <div key={k} className="glass rounded-2xl overflow-hidden">
            <div className="grid md:grid-cols-[300px_1fr]">
              {/* styled thumbnail */}
              <button
                onClick={() => stylizedUrl && onOpenStyle(st, md)}
                className="relative aspect-[4/3] md:aspect-auto md:h-full bg-black/30 group cursor-pointer"
                title={stylizedUrl ? "Open this style" : "Not generated yet"}
              >
                {stylizedUrl ? (
                  <img
                    src={stylizedUrl}
                    alt={`${styleName(st)} top-down`}
                    className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.02] transition-transform"
                  />
                ) : (
                  <div className="absolute inset-0 grid place-items-center text-fog text-sm">
                    styled view not generated
                  </div>
                )}
              </button>

              {/* details */}
              <div className="p-5 flex flex-col gap-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2.5">
                    <span
                      className="inline-block h-3 w-3 rounded-full"
                      style={{ background: accent }}
                    />
                    <span className="font-display text-xl capitalize">
                      {styleName(st)}
                    </span>
                    <span className="text-fog text-sm">· {modelName(md)}</span>
                  </div>
                  <span
                    className="text-xs rounded-full px-3 py-1 border"
                    style={{
                      borderColor: tourUrl ? accent : "rgba(255,255,255,.15)",
                      color: tourUrl ? accent : "#9d9aa8",
                    }}
                  >
                    {tourUrl
                      ? "Tour ready"
                      : busy
                      ? "Rendering…"
                      : "No tour yet"}
                  </span>
                </div>

                {/* room 360 chips */}
                <div>
                  <p className="text-fog text-xs mb-2">
                    360° rooms — {ready}/{total}
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {project.rooms.map((r) => {
                      const has = !!roomPanos[r.id];
                      return (
                        <button
                          key={r.id}
                          disabled={!has}
                          onClick={() => onOpenRoom(r, k)}
                          className={`text-xs rounded-full px-2.5 py-1 capitalize transition ${
                            has
                              ? "cursor-pointer text-ink font-medium"
                              : "text-fog/50 border border-white/10 cursor-default"
                          }`}
                          style={has ? { background: accent } : undefined}
                          title={has ? "View 360°" : "not generated"}
                        >
                          {r.name}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* tour video */}
                <div className="mt-auto">
                  {tourUrl && !busy ? (
                    <div className="flex flex-col gap-2">
                      <video
                        key={tourUrl}
                        src={tourUrl}
                        controls
                        className="rounded-xl w-full max-w-xl border border-white/10"
                      />
                      <button
                        onClick={() => onMakeTour(st, md)}
                        className="btn-ghost glass rounded-full px-4 py-1.5 text-sm self-start cursor-pointer"
                      >
                        ↻ Re-render
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => onMakeTour(st, md)}
                      disabled={busy || !stylizedUrl || ready === 0}
                      className="btn-gold rounded-full px-6 py-2.5 text-sm font-semibold cursor-pointer"
                      title={
                        !stylizedUrl
                          ? "generate the styled view first"
                          : ready === 0
                          ? "generate at least one room 360 first"
                          : ""
                      }
                    >
                      {busy
                        ? "Rendering… ~2–3 min (in the background)"
                        : "🎬 Make tour video ✦"}
                    </button>
                  )}
                  {failed && (
                    <p className="text-red-400 text-xs mt-2">
                      Render failed — try again.
                    </p>
                  )}
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
