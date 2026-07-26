"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

/**
 * Fill the style template — the same structure that defines the built-in
 * styles — to create a custom one. On save it's usable everywhere at once.
 */
export default function NewStyleModal({ onClose, onCreated }) {
  const [name, setName] = useState("");
  const [emoji, setEmoji] = useState("🎨");
  const [tagline, setTagline] = useState("");
  const [vibe, setVibe] = useState("");
  const [materials, setMaterials] = useState("");
  const [colors, setColors] = useState([
    { name: "", hex: "#E8DCC8" },
    { name: "", hex: "#C96F4A" },
    { name: "", hex: "#2E2E30" },
  ]);
  const [accent, setAccent] = useState("#c9a35c");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const setColor = (i, patch) =>
    setColors((cs) => cs.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const addColor = () =>
    setColors((cs) =>
      cs.length >= 6 ? cs : [...cs, { name: "", hex: "#888888" }]
    );
  const removeColor = (i) =>
    setColors((cs) => (cs.length <= 2 ? cs : cs.filter((_, j) => j !== i)));

  const list = (s) =>
    s.split(",").map((x) => x.trim()).filter(Boolean);

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      const style = await api.createStyle({
        name,
        emoji,
        tagline,
        vibe: list(vibe),
        materials: list(materials),
        colors,
        accent,
      });
      onCreated(style);
    } catch (e) {
      setErr(String(e.message || e));
      setBusy(false);
    }
  };

  const field =
    "w-full bg-white/5 border border-white/15 rounded-lg px-3 py-2 " +
    "text-ivory outline-none focus:border-white/40 placeholder:text-fog/50";

  return (
    <div className="fixed inset-0 z-50 bg-ink/92 backdrop-blur-md flex items-start justify-center overflow-y-auto py-10 rise">
      <div className="glass rounded-2xl w-full max-w-lg mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="font-display text-2xl">Create a style</h2>
          <button
            onClick={onClose}
            className="btn-ghost rounded-full h-10 w-10 text-lg cursor-pointer"
            aria-label="close"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-4">
          <div className="flex gap-3">
            <div className="w-16">
              <label className="text-fog text-xs">Icon</label>
              <input
                value={emoji}
                onChange={(e) => setEmoji(e.target.value)}
                className={field + " text-center text-xl"}
                maxLength={4}
              />
            </div>
            <div className="flex-1">
              <label className="text-fog text-xs">Name *</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Industrial Loft"
                className={field}
              />
            </div>
          </div>

          <div>
            <label className="text-fog text-xs">Tagline</label>
            <input
              value={tagline}
              onChange={(e) => setTagline(e.target.value)}
              placeholder="Raw concrete, warm edison glow"
              className={field}
            />
          </div>

          <div>
            <label className="text-fog text-xs">
              Vibe — comma separated
            </label>
            <input
              value={vibe}
              onChange={(e) => setVibe(e.target.value)}
              placeholder="Raw, Moody, Urban"
              className={field}
            />
          </div>

          <div>
            <div className="flex items-center justify-between">
              <label className="text-fog text-xs">Palette * (2–6)</label>
              <button
                onClick={addColor}
                className="text-xs text-gold cursor-pointer disabled:opacity-40"
                disabled={colors.length >= 6}
              >
                + add color
              </button>
            </div>
            <div className="flex flex-col gap-2 mt-1.5">
              {colors.map((c, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    type="color"
                    value={c.hex}
                    onChange={(e) => setColor(i, { hex: e.target.value })}
                    className="h-9 w-10 rounded cursor-pointer bg-transparent border border-white/15"
                  />
                  <input
                    value={c.name}
                    onChange={(e) => setColor(i, { name: e.target.value })}
                    placeholder="color name (optional)"
                    className={field + " flex-1"}
                  />
                  <button
                    onClick={() => removeColor(i)}
                    disabled={colors.length <= 2}
                    className="btn-ghost rounded-full h-8 w-8 text-sm cursor-pointer disabled:opacity-30"
                    aria-label="remove color"
                  >
                    −
                  </button>
                </div>
              ))}
            </div>
          </div>

          <div>
            <label className="text-fog text-xs">
              Materials * — comma separated
            </label>
            <input
              value={materials}
              onChange={(e) => setMaterials(e.target.value)}
              placeholder="Concrete, Black Steel, Reclaimed Oak, Leather"
              className={field}
            />
          </div>

          <div className="flex items-center gap-3">
            <label className="text-fog text-xs">Accent</label>
            <input
              type="color"
              value={accent}
              onChange={(e) => setAccent(e.target.value)}
              className="h-9 w-12 rounded cursor-pointer bg-transparent border border-white/15"
            />
            <span className="text-fog text-xs">{accent}</span>
          </div>

          {err && <p className="text-red-400 text-sm">{err}</p>}

          <div className="flex gap-3 mt-1">
            <button
              onClick={submit}
              disabled={busy || !name.trim()}
              className="btn-gold rounded-full px-7 py-3 font-semibold cursor-pointer flex-1"
            >
              {busy ? "Creating…" : "Create style ✦"}
            </button>
            <button
              onClick={onClose}
              className="btn-ghost glass rounded-full px-6 py-3 cursor-pointer"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
