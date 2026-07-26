"use client";

/**
 * The interactive floor-plan stage.
 * Both images (blueprint render + stylized) share identical pixel dimensions,
 * and room geometry arrives in that same pixel space — so one SVG with
 * viewBox = image size overlays perfectly at any display size.
 */
export default function PlanStage({
  project,
  accent,
  view, // "plan" | "styled"
  stylizedUrl,
  stylizing,
  panos, // { roomId: url } for the active style
  generating, // Set of roomIds currently generating
  onRoom, // click on room or its 360 badge
}) {
  const { width_px: w, height_px: h, rooms } = project;
  const fontSize = Math.max(20, w / 55);
  const badgeR = Math.max(18, w / 52);

  return (
    <div
      className="relative w-full rounded-2xl overflow-hidden border border-white/10 bg-ink-2"
      style={{ aspectRatio: `${w} / ${h}` }}
    >
      <img
        src={project.plan_url}
        alt="blueprint"
        className="absolute inset-0 w-full h-full transition-opacity duration-700"
        style={{ opacity: view === "plan" ? 1 : 0 }}
      />
      {stylizedUrl && (
        <img
          src={stylizedUrl}
          alt="stylized"
          className="absolute inset-0 w-full h-full transition-opacity duration-700"
          style={{ opacity: view === "styled" ? 1 : 0 }}
        />
      )}

      {/* stylize-in-progress veil */}
      {stylizing && (
        <div className="absolute inset-0 shimmer bg-ink/40 backdrop-blur-[2px] flex items-center justify-center">
          <div className="glass rounded-full px-6 py-3 flex items-center gap-3">
            <span className="h-4 w-4 rounded-full border-2 border-white/20 border-t-gold spin-slow" />
            <span className="text-sm">painting your dream…</span>
          </div>
        </div>
      )}

      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="absolute inset-0 w-full h-full"
        style={{ "--accent": accent }}
      >
        {rooms.map((r) => {
          const [cx, cy] = r.centroid_px;
          const ready = !!panos[r.id];
          const busy = generating.has(r.id);
          return (
            <g key={r.id} className="room-group" onClick={() => onRoom(r)}>
              <polygon
                className="room-poly"
                points={r.polygon_px.map((p) => p.join(",")).join(" ")}
              />
              {/* hover label */}
              <g className="room-label">
                <text
                  x={cx}
                  y={ready || busy ? cy - badgeR * 1.7 : cy}
                  textAnchor="middle"
                  fontSize={fontSize}
                  fontWeight="600"
                  fill="#f3efe7"
                  stroke="#0b0b10"
                  strokeWidth={fontSize / 5}
                  paintOrder="stroke"
                >
                  {r.name}
                  <tspan fontSize={fontSize * 0.72} fontWeight="400">
                    {"  "}
                    {r.area_m2} m²
                  </tspan>
                </text>
              </g>

              {/* 360 badge */}
              {(ready || busy) && (
                <g
                  className="pano-badge"
                  style={{ transformOrigin: `${cx}px ${cy}px` }}
                >
                  {ready && (
                    <circle
                      cx={cx}
                      cy={cy}
                      r={badgeR}
                      fill="none"
                      stroke={accent}
                      strokeWidth="2"
                    >
                      <animate
                        attributeName="r"
                        from={badgeR}
                        to={badgeR * 1.9}
                        dur="1.9s"
                        repeatCount="indefinite"
                      />
                      <animate
                        attributeName="opacity"
                        from="0.7"
                        to="0"
                        dur="1.9s"
                        repeatCount="indefinite"
                      />
                    </circle>
                  )}
                  <circle
                    cx={cx}
                    cy={cy}
                    r={badgeR}
                    fill={ready ? "#0b0b10d9" : "#0b0b1080"}
                    stroke={ready ? accent : "#ffffff30"}
                    strokeWidth="2.5"
                  />
                  {busy ? (
                    <circle
                      cx={cx}
                      cy={cy}
                      r={badgeR * 0.62}
                      fill="none"
                      stroke={accent}
                      strokeWidth="3"
                      strokeDasharray={`${badgeR} ${badgeR * 3}`}
                      strokeLinecap="round"
                    >
                      <animateTransform
                        attributeName="transform"
                        type="rotate"
                        from={`0 ${cx} ${cy}`}
                        to={`360 ${cx} ${cy}`}
                        dur="1s"
                        repeatCount="indefinite"
                      />
                    </circle>
                  ) : (
                    <text
                      x={cx}
                      y={cy + badgeR * 0.28}
                      textAnchor="middle"
                      fontSize={badgeR * 0.78}
                      fontWeight="700"
                      fill={accent}
                    >
                      360°
                    </text>
                  )}
                </g>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
