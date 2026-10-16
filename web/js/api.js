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
  command: (line) => request("POST", "/api/command", { line }),
  realtime: (op) => request("POST", "/api/realtime", { op }),
};

export function eventsUrl(lastId) {
  const p = new URLSearchParams({ t: token || "" });
  if (lastId) p.set("last", lastId);
  return "/api/events?" + p.toString();
}
