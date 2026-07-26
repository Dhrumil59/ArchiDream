"use client";

export default function StylePicker({ styles, selected, onSelect, compact, onAdd }) {
  return (
    <div
      className={
        compact
          ? "flex flex-wrap gap-2"
          : "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5"
      }
    >
      {styles.map((s) =>
        compact ? (
          <button
            key={s.id}
            onClick={() => onSelect(s.id)}
            style={{ "--accent": s.accent }}
            className={`seg rounded-full px-4 py-1.5 text-sm border transition-all cursor-pointer ${
              selected === s.id
                ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_18%,transparent)] text-ivory"
                : "border-white/12 text-fog hover:border-white/30 hover:text-ivory"
            }`}
          >
            {s.emoji} {s.name}
          </button>
        ) : (
          <div
            key={s.id}
            onClick={() => onSelect(s.id)}
            style={{ "--accent": s.accent }}
            className={`style-card glass rounded-2xl p-5 ${
              selected === s.id ? "selected" : ""
            }`}
          >
            <div className="text-3xl">{s.emoji}</div>
            <h3 className="font-display text-xl mt-3">{s.name}</h3>
            <p className="text-fog text-sm mt-0.5">{s.tagline}</p>

            <div className="flex flex-wrap gap-1.5 mt-4">
              {s.vibe.map((v) => (
                <span
                  key={v}
                  className="text-[10px] uppercase tracking-wider text-fog border border-white/10 rounded-full px-2 py-0.5"
                >
                  {v}
                </span>
              ))}
            </div>

            <div className="flex gap-2.5 mt-5">
              {s.colors.map((c) => (
                <span
                  key={c.name}
                  title={c.name}
                  className="swatch h-6 w-6 rounded-full border border-white/20"
                  style={{ background: c.hex }}
                />
              ))}
            </div>

            <p className="text-xs text-fog/80 mt-4 leading-relaxed">
              {s.materials.join(" · ")}
            </p>
          </div>
        )
      )}

      {onAdd &&
        (compact ? (
          <button
            onClick={onAdd}
            className="seg rounded-full px-4 py-1.5 text-sm border border-dashed border-white/25 text-fog hover:text-ivory hover:border-white/50 transition-all cursor-pointer"
          >
            ＋ New style
          </button>
        ) : (
          <button
            onClick={onAdd}
            className="glass rounded-2xl p-5 border border-dashed border-white/20 grid place-items-center text-fog hover:text-ivory hover:border-white/40 transition-all cursor-pointer min-h-[180px]"
          >
            <div className="text-center">
              <div className="text-3xl">＋</div>
              <div className="font-display text-lg mt-2">New style</div>
              <p className="text-fog text-xs mt-1">fill the template</p>
            </div>
          </button>
        ))}
    </div>
  );
}
