---
workflow: general-video
flow: automation
storyboard: no
message: "Walk through your home before it exists"
aspect: 1920x1080
length: 35s
audience: home buyers / clients of ArchiDream
---

## Intent

A ~35 s property-marketing tour for one ArchiDream project in one chosen
interior style — realistic, calm, premium real-estate feel. This project
is the reusable TEMPLATE: every per-property value (names, areas, room
list, image paths, palette) must be injected from a sibling
`tour-data.json` so backend code can render any project/style later.
Pilot: project `simple-house`, style `mediterranean@nano-banana`.

## Assets

- ../../data/simple-house/plan.png — DXF line render (1600x2279), shot 2 start
- ../../data/simple-house/stylized/mediterranean@nano-banana.png — styled
  top-down, pixel-aligned with plan.png — shots 2/3/7
- ../../data/simple-house/panos/mediterranean@nano-banana/S1.png — 4096x2048
  equirect, kitchen/dining/living 35 m2 — shot 4
- ../../data/simple-house/panos/mediterranean@nano-banana/S2.png — bedroom
  13 m2 — shot 5
- ../../data/simple-house/panos/mediterranean@nano-banana/S5.png — bathroom
  2 m2 — shot 6
- ../../data/simple-house/rooms.json — room names, areas m2, pixel polygons
  + centroids on plan.png coordinates (for the S1 glow outline in shot 3)

## Customizations

- Shot list (fixed order for the pilot):
  1. Opening card 3 s: "Simple House" + "68 m2 · 3 rooms · 2 bed / 1 bath"
  2. Reveal 4 s: plan.png -> stylized crossfade under a slow zoom-in
     (images are pixel-aligned; this is the signature shot)
  3. Room dive 3 s: zoom continues toward S1's region; S1 polygon glows
     as an outline overlay (polygon from rooms.json)
  4. S1 sweep 6 s: horizontal pan across the equirect pano;
     lower-third "Kitchen · Dining · Living — 35 m2"
  5. S2 sweep 4 s: lower-third "Bedroom — 13 m2"
  6. S5 sweep 2.5 s: lower-third "Bathroom"
  7. Pullback 3 s: stylized top-down, slow zoom-out
  8. Closing card 3.5 s: message line + "ArchiDream" wordmark + style
     badge: "Mediterranean" + palette dots #FAF6EE #C96F4A #2E6E8E #7A8450
- Pano sweep = translate a horizontally-doubled strip of the equirect (or
  three.js sphere yaw if more reliable in the runtime).
- Background music: gentle warm track full duration, fade out; if no
  local licensed track, render silent + document an ffmpeg mux step.

## Notes

- Brand: ink background #101014, ivory text #F4F0E6, gold accent #c9a35c,
  thin serif display type — matches the ArchiDream frontend.
- The IBM Granite "director" (watsonx.ai) emits the shot-plan JSON at render
  time; the FastAPI endpoint renders on demand; shots stay data-driven and
  order-flexible via tour-data.json.
- Room names/areas must never be hardcoded in the composition HTML.
