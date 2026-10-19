// Bug reports: describe a problem and save a report on the server with the
// machine, job, log and browser state; list and download earlier reports.

import { bugReports, ApiError, isViewOnly } from "../api.js";
import { $ } from "./dom.js";

const VISIBLE = [
  "banner", "warnings", "link-text", "conn-info", "machine-state", "machine-firmware", "probe-status", "hm-progress",
  "hm-transform-info", "macro-status", "job-name", "job-progress-text", "job-recovery", "job-prompt-text", "job-info",
];
const STORAGE_PREFIXES = ["gcs-"];

function visibleText() {
  const out = {};
  for (const id of VISIBLE) {
    const el = document.getElementById(id);
    if (!el) continue;
    const hidden = el.hidden || el.closest("[hidden]") !== null;
    const text = (el.innerText || el.textContent || "").trim().slice(0, 2000);
    if (text) out[id] = hidden ? `(hidden) ${text}` : text;
  }
  const open = [...document.querySelectorAll("dialog[open]")].map((d) => d.id);
  out.openDialogs = open;
  out.activeTabs = [...document.querySelectorAll(".tab.active, [role=tab][aria-selected=true]")].map((t) => t.textContent.trim());
  return out;
}

function storage() {
  const out = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (!STORAGE_PREFIXES.some((p) => k.startsWith(p)) || /token/i.test(k)) continue;
    const v = localStorage.getItem(k);
    try { out[k] = JSON.parse(v); } catch { out[k] = v; }
  }
  return out;
}

function webgl() {
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    if (!gl) return { available: false };
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    return {
      available: true,
      vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
      renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
    };
  } catch (e) {
    return { available: false, error: String(e) };
  }
}

function collect(store) {
  const s = store.state;
  const state = {};
  for (const [k, v] of Object.entries(s)) state[k] = k === "console" ? undefined : v;
  return {
    capturedAt: new Date().toISOString(),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    page: location.pathname + location.hash, // never the query (it may carry a token)
    userAgent: navigator.userAgent,
    language: navigator.language,
    platform: navigator.platform,
    online: navigator.onLine,
    viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
    screen: { width: screen.width, height: screen.height },
    touch: matchMedia("(pointer: coarse)").matches,
    pageAgeSeconds: window.gcsDiag ? Math.round((Date.now() - window.gcsDiag.started) / 1000) : null,
    webgl: webgl(),
    visible: visibleText(),
    storage: storage(),
    store: state,
    console: s.console.slice(-300),
    ...(window.gcsDiag ? window.gcsDiag.snapshot() : { diagnostics: "unavailable" }),
  };
}

function fmtSize(n) {
  return n > 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} kB`;
}

export function initBugReport(store) {
  const dlg = $("#dlg-bugreport");
  const desc = $("#bug-desc");
  const status = $("#bug-status");
  const createBtn = $("#bug-create");
  const list = $("#bug-list");
  const remote = $("#bug-remote");
  $("#bug-archive").href = bugReports.archiveUrl();

  // Server link and connection changes, for the report's timeline.
  let lastLink = null;
  let lastConn = null;
  store.subscribe((_kind, s) => {
    if (s.link !== lastLink) {
      lastLink = s.link;
      window.gcsDiag?.note(`server link ${s.link}`);
    }
    const c = s.connection && s.connection.state;
    if (c !== lastConn) {
      lastConn = c;
      window.gcsDiag?.note(`connection ${c}`, s.connection.error ? { error: s.connection.error } : undefined);
    }
  });

  function row(r) {
    const tr = document.createElement("tr");
    const when = document.createElement("td");
    when.textContent = new Date(r.created * 1000).toLocaleString();
    const title = document.createElement("td");
    title.className = "bug-title";
    title.textContent = r.title;
    title.title = r.title;
    const st = document.createElement("td");
    const sum = r.summary || {};
    st.textContent = [sum.machineState, sum.job && sum.job.state].filter(Boolean).join(" · ") || "—";
    const size = document.createElement("td");
    size.textContent = fmtSize(r.size || 0);
    const act = document.createElement("td");
    const dl = document.createElement("a");
    dl.className = "button-like";
    dl.href = bugReports.url(r.id);
    dl.download = `gcsender-bugreport-${r.id}.zip`;
    dl.textContent = "Download";
    dl.title = `Download report ${r.id} as a zip`;
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "Delete";
    del.title = `Delete report ${r.id} from the server`;
    del.addEventListener("click", async () => {
      if (!confirm(`Delete the bug report from ${when.textContent}?`)) return;
      try {
        render((await bugReports.remove(r.id)).reports);
      } catch (e) {
        status.textContent = `cannot delete: ${e.message}`;
      }
    });
    del.hidden = isViewOnly();
    act.append(dl, " ", del);
    tr.append(when, title, st, size, act);
    return tr;
  }

  function render(reports) {
    if (!reports.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "muted";
      td.textContent = "No reports yet.";
      tr.append(td);
      list.replaceChildren(tr);
    } else {
      list.replaceChildren(...reports.map(row));
    }
  }

  function renderAccess(a) {
    remote.replaceChildren();
    if (!a || !a.reportsToken) return;
    if (!a.lan) {
      remote.textContent = "Remote download: start the server with --lan to allow it from another computer.";
      return;
    }
    const host = location.hostname === "localhost" || location.hostname === "127.0.0.1" ? a.hostname : location.hostname;
    const code = document.createElement("code");
    code.textContent = `http://${host}:${a.port}/api/bugreports/archive?t=${a.reportsToken}`;
    remote.append("Remote download (read-only reports token): ", code);
  }

  async function refresh() {
    try {
      const body = await bugReports.list();
      render(body.reports);
      renderAccess(body.access);
    } catch (e) {
      status.textContent = `cannot list reports: ${e.message}`;
    }
  }

  $("#open-bugreport").addEventListener("click", () => {
    status.textContent = "";
    dlg.showModal();
    desc.focus();
    refresh();
  });

  createBtn.addEventListener("click", async () => {
    const text = desc.value.trim();
    if (!text) {
      status.textContent = "Please describe what happened first.";
      desc.focus();
      return;
    }
    createBtn.disabled = true;
    status.textContent = "Saving…";
    try {
      const r = await bugReports.create(text, collect(store), $("#bug-program").checked);
      status.textContent = `Saved report ${r.id}.`;
      desc.value = "";
      refresh();
    } catch (e) {
      status.textContent = e instanceof ApiError && e.status === 0
        ? "The server is unreachable, so the report cannot be saved. Try again when the link is back."
        : `Cannot save the report: ${e.message}`;
    } finally {
      createBtn.disabled = false;
    }
  });
}
