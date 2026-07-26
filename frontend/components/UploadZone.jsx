"use client";

import { useRef, useState } from "react";

export default function UploadZone({ file, onFile }) {
  const inputRef = useRef(null);
  const [drag, setDrag] = useState(false);

  const pick = (f) => {
    if (f && f.name.toLowerCase().endsWith(".dxf")) onFile(f);
  };

  return (
    <div
      onClick={() => inputRef.current?.click()}
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        pick(e.dataTransfer.files?.[0]);
      }}
      className={`glass rounded-2xl p-8 text-center cursor-pointer transition-all duration-300 ${
        drag
          ? "border-gold/70 scale-[1.01] shadow-[0_0_44px_-8px_rgba(201,163,92,0.45)]"
          : "hover:border-white/25"
      }`}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".dxf"
        className="hidden"
        onChange={(e) => pick(e.target.files?.[0])}
      />
      {file ? (
        <div className="rise">
          <div className="text-3xl">📐</div>
          <p className="mt-2 font-medium">{file.name}</p>
          <p className="text-fog text-sm mt-1">
            {(file.size / 1024).toFixed(0)} KB — click to swap
          </p>
        </div>
      ) : (
        <>
          <div className="text-3xl opacity-80">⤓</div>
          <p className="mt-2 font-medium">Drop your DXF floor plan</p>
          <p className="text-fog text-sm mt-1">or click to browse</p>
        </>
      )}
    </div>
  );
}
