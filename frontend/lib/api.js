// Central fetch client. Every call gets:
//   • a timeout (via AbortController) so nothing spins forever
//   • a human-readable error — FastAPI's `detail`, or a friendly note for a
//     network/proxy failure or a timeout.
const GEN = 180000; // image generation / upload — allow for slow AI
const FAST = 20000; // quick metadata + status calls

async function req(url, opts = {}, timeout = FAST) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetch(url, { ...opts, signal: ctrl.signal });
  } catch (e) {
    if (e.name === "AbortError")
      throw new Error("This is taking longer than expected — please try again.");
    throw new Error("Couldn't reach the server — check it's running, then retry.");
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

const jpost = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  styles: () => req("/api/styles"),
  createStyle: (fields) => req("/api/styles", jpost(fields)),
  models: () => req("/api/models"),
  projects: () => req("/api/projects"),
  project: (id) => req(`/api/projects/${id}`),

  upload: (file) => {
    const form = new FormData();
    form.append("file", file);
    return req("/api/projects", { method: "POST", body: form }, GEN);
  },

  stylize: (id, style, model) =>
    req(`/api/projects/${id}/stylize`, jpost({ style, model }), GEN),

  pano: (id, roomId, style, model, force = false) =>
    req(`/api/projects/${id}/rooms/${roomId}/pano`,
        jpost({ style, model, force }), GEN),

  editRegion: (id, roomId, style, model, prompt, polygon) =>
    req(`/api/projects/${id}/rooms/${roomId}/edit-region`,
        jpost({ style, model, prompt, polygon }), GEN),

  // async job: returns fast with {status:"rendering"|"done"}, then poll below
  tourVideo: (id, style, model, force = false, rooms = null) =>
    req(`/api/projects/${id}/tour-video`, jpost({ style, model, force, rooms })),

  tourStatus: (id, style, model) =>
    req(
      `/api/projects/${id}/tour-video?style=${encodeURIComponent(
        style
      )}&model=${encodeURIComponent(model)}`
    ),
};
