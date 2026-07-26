"use client";

import { useEffect, useState } from "react";

/**
 * Pick which generated 360s go into the tour video, then render.
 * Every room that has a 360 starts selected; untick to leave it out.
 * Rooms without a 360 aren't shown (nothing to include).
 */
export default function TourComposer({
  rooms,
  panoUrls,
  resetKey,
  accent,
  status,
  tourUrl,
  onMake,
}) {
  const available = rooms.filter((r) => panoUrls[r.id]);
  const [sel, setSel] = useState(
    () => new Set(available.map((r) => r.id))
  );

  // switching style@model: reset the selection to "all available"
  useEffect(() => {
    setSel(new Set(rooms.filter((r) => panoUrls[r.id]).map((r) => r.id)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  // a newly-generated 360 joins the selection automatically
  useEffect(() => {
    setSel((s) => {
      const n = new Set(s);
      available.forEach((r) => n.add(r.id));
      return n.size === s.size ? s : n;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [available.length]);

  if (available.length === 0) {
    return (
      <p className="text-fog/70 text-sm">
        Generate at least one room 360 to make a tour video.
      </p>
    );
  }

  const busy = status === "rendering";
  const toggle = (id) =>
    setSel((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const count = sel.size;
  const noun = count === 1 ? "room" : "rooms";

  return (
    <div className="flex flex-col items-center gap-3 w-full">
      <p className="text-fog/70 text-xs">
        rooms in the tour — tap to include or leave out
      </p>
      <div className="flex flex-wrap gap-1.5 justify-center max-w-2xl">
        {available.map((r) => {
          const on = sel.has(r.id);
          return (
            <button
              key={r.id}
              onClick={() => toggle(r.id)}
              className={`text-xs rounded-full px-3 py-1.5 capitalize transition cursor-pointer ${
                on ? "text-ink font-medium" : "text-fog border border-white/15"
              }`}
              style={on ? { background: accent } : undefined}
              title={on ? "in the video — tap to exclude" : "tap to include"}
            >
              {on ? "✓ " : ""}
              {r.name}
            </button>
          );
        })}
      </div>
      <button
        onClick={() => onMake(Array.from(sel))}
        disabled={busy || count === 0}
        className="btn-gold rounded-full px-9 py-3.5 font-semibold cursor-pointer"
      >
        {busy
          ? "Rendering… ~2–3 min (in the background)"
          : tourUrl
          ? `↻ Re-render tour · ${count} ${noun}`
          : `🎬 Make tour video · ${count} ${noun} ✦`}
      </button>
      {status === "failed" && (
        <p className="text-red-400 text-sm">Render failed — try again.</p>
      )}
      {tourUrl && !busy && (
        <video
          key={tourUrl}
          src={tourUrl}
          controls
          className="mt-2 rounded-2xl w-full max-w-3xl border border-white/10"
        />
      )}
    </div>
  );
}
