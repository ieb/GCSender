// HTTP API client. The token comes from the page (injected by the server for
// loopback clients) or from a ?t= URL parameter (LAN clients); it is kept in
// sessionStorage and removed from the visible URL.

const TOKEN_KEY = "gcs-token";

function resolveToken() {
  const meta = document.querySelector('meta[name="gcs-token"]');
  let token = meta && meta.content ? meta.content : null;
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("t");
  if (fromUrl) {
    token = fromUrl;
    url.searchParams.delete("t");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  return token || sessionStorage.getItem(TOKEN_KEY);
}

export const token = resolveToken();

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function request(method, path, body) {
  const headers = { "X-GCS-Token": token || "" };
  const init = { method, headers, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, "server unreachable");
  }
  let payload = null;
  const text = await res.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { error: text }; }
  }
  if (!res.ok) throw new ApiError(res.status, (payload && payload.error) || res.statusText);
  return payload;
}

export const api = {
  state: () => request("GET", "/api/state"),
  ports: () => request("GET", "/api/ports"),
  connect: (port, baud) => request("POST", "/api/connect", { port, baud }),
  disconnect: () => request("POST", "/api/disconnect", {}),
  command: (line, source = "console") => request("POST", "/api/command", { line, source }),
  realtime: (op) => request("POST", "/api/realtime", { op }),
  control: (op) => request("POST", "/api/control", { op }),
  grblSettings: () => request("GET", "/api/grbl/settings"),
  grblSettingsRefresh: () => request("POST", "/api/grbl/settings/refresh", {}),
  grblSettingsWrite: (values) => request("PUT", "/api/grbl/settings", { values }),
  appSettings: () => request("GET", "/api/settings"),
  appSettingsWrite: (values) => request("PUT", "/api/settings", values),
  appLog: () => request("GET", "/api/logs/app"),
  jogStep: (delta, feed) => request("POST", "/api/jog/step", { x: delta[0], y: delta[1], z: delta[2], feed }),
  stop: () => request("POST", "/api/stop", {}),
  wcsSelect: (wcs) => request("POST", "/api/wcs/select", { wcs }),
  wcsZero: (axes, persistent) => request("POST", "/api/wcs/zero", { axes, persistent }),
  clearG92: () => request("POST", "/api/wcs/clear-g92", {}),
  goto: (target, safeZ = true) => request("POST", "/api/goto", { ...target, safeZ }),
  spindle: (on, rpm, direction = "cw") => request("POST", "/api/spindle", { on, rpm, direction }),
  coolant: (flags) => request("POST", "/api/coolant", flags),
  transcripts: () => request("GET", "/api/logs/serial/files"),
  probeZ: (opts = {}) => request("POST", "/api/probe/z", opts),
  probeTest: (expect) => request("POST", "/api/probe/test", { expect }),
  heightmap: () => request("GET", "/api/heightmap"),
  heightmapParams: (params) => request("PUT", "/api/heightmap", params),
  heightmapCells: (cells) => request("PATCH", "/api/heightmap/cells", { cells }),
  heightmapClear: () => request("POST", "/api/heightmap/clear", {}),
  heightmapAuto: (gcode, margin = 0) => request("POST", "/api/heightmap/auto", { gcode, margin }),
  heightmapProbe: () => request("POST", "/api/heightmap/probe", {}),
  heightmapAbort: () => request("POST", "/api/heightmap/abort", {}),
  heightmapInterpolated: () => request("GET", "/api/heightmap/interpolated"),
  heightmapImport: (text) => request("POST", "/api/heightmap/import", { text }),
  heightmapLibrary: () => request("GET", "/api/heightmap/library"),
  heightmapSave: (name) => request("POST", "/api/heightmap/library/save", { name }),
  heightmapLoad: (name) => request("POST", "/api/heightmap/library/load", { name }),
  heightmapDelete: (name) => request("POST", "/api/heightmap/library/delete", { name }),
  heightmapOrigin: () => request("POST", "/api/heightmap/origin", {}),
  heightmapTransform: (gcode) => request("POST", "/api/heightmap/transform", { gcode }),
};

export function heightmapExportUrl() {
  return "/api/heightmap/export?" + new URLSearchParams({ t: token || "" }).toString();
}

export function transcriptUrl(date) {
  const p = new URLSearchParams({ t: token || "", date });
  return "/api/logs/serial?" + p.toString();
}

export function eventsUrl(lastId) {
  const p = new URLSearchParams({ t: token || "" });
  if (lastId) p.set("last", lastId);
  return "/api/events?" + p.toString();
}

