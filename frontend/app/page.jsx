"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import StylePicker from "@/components/StylePicker";
import UploadZone from "@/components/UploadZone";

const STEPS = [
  "Reading DXF geometry…",
  "Detecting walls…",
  "Flood-filling rooms…",
  "Tracing doors & windows…",
  "Preparing your canvas…",
];

export default function Home() {
  const router = useRouter();
  const [styles, setStyles] = useState([]);
  const [demos, setDemos] = useState([]);
  const [file, setFile] = useState(null);
  const [style, setStyle] = useState(null);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState(0);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.styles().then(setStyles).catch(() => {});
    api.projects().then(setDemos).catch(() => {});
  }, []);

  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => setStep((s) => (s + 1) % STEPS.length), 2600);
    return () => clearInterval(t);
  }, [busy]);

  const generate = async () => {
    if (!file || !style) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.upload(file);
      router.push(`/project/${project.id}?style=${style}`);
    } catch (e) {
      setError(String(e.message || e));
      setBusy(false);
    }
  };

  return (
    <main className="max-w-6xl mx-auto px-6 pb-24">
      {/* nav */}
      <nav className="flex items-center justify-between py-7">
        <div className="font-display text-2xl tracking-tight">
          Archi<span className="text-gold">Dream</span>
        </div>
        <span className="text-fog text-sm hidden sm:block">
          blueprint → dream home
        </span>
      </nav>

      {/* hero */}
      <header className="text-center mt-14 mb-16 rise">
        <h1 className="font-display text-5xl sm:text-6xl leading-[1.06] tracking-tight">
          Walk through your home
          <br />
          <span className="text-gold-2 italic">before it exists.</span>
        </h1>
        <p className="text-fog mt-6 max-w-xl mx-auto leading-relaxed">
          Upload a CAD floor plan, choose an interior soul, and ArchiDream
          renders it into a living space you can explore room by room — in
          360°.
        </p>
      </header>

      {/* upload */}
      <section className="max-w-2xl mx-auto rise" style={{ animationDelay: "0.1s" }}>
        <UploadZone file={file} onFile={setFile} />
      </section>

      {/* styles */}
      <section className="mt-14 rise" style={{ animationDelay: "0.2s" }}>
        <h2 className="font-display text-2xl mb-6 text-center">
          Choose the soul of your home
        </h2>
        <StylePicker styles={styles} selected={style} onSelect={setStyle} />
      </section>

      {/* CTA */}
      <section className="text-center mt-12">
        <button
          onClick={generate}
          disabled={!file || !style || busy}
          className="btn-gold rounded-full px-10 py-4 font-semibold text-base cursor-pointer"
        >
          {busy ? "Dreaming…" : "Bring it to life ✦"}
        </button>
        {error && <p className="text-red-400 mt-4 text-sm">{error}</p>}
      </section>

      {/* demos */}
      {demos.length > 0 && (
        <section className="mt-20 rise" style={{ animationDelay: "0.3s" }}>
          <h2 className="text-fog text-sm uppercase tracking-widest text-center mb-6">
            or explore a ready home
          </h2>
          <div className="flex flex-wrap justify-center gap-4">
            {demos.map((d) => (
              <button
                key={d.id}
                onClick={() =>
                  router.push(`/project/${d.id}?style=${style || "scandinavian"}`)
                }
                className="btn-ghost glass rounded-xl px-5 py-3 text-left cursor-pointer"
              >
                <span className="font-medium capitalize">{d.name}</span>
                <span className="text-fog text-sm block">
                  {d.rooms.length} rooms
                </span>
              </button>
            ))}
          </div>
        </section>
      )}

      {/* processing overlay */}
      {busy && (
        <div className="fixed inset-0 z-50 bg-ink/92 backdrop-blur-md flex items-center justify-center">
          <div className="text-center">
            <div className="mx-auto h-14 w-14 rounded-full border-2 border-white/15 border-t-gold spin-slow" />
            <p className="font-display text-2xl mt-8">{STEPS[step]}</p>
            <p className="text-fog text-sm mt-2">
              parsing usually takes under a minute
            </p>
          </div>
        </div>
      )}
    </main>
  );
}
