// Diagnostics recorder for bug reports. A classic (non-module) script loaded
// first, so it also sees errors raised while the app's modules load.
// Keeps short rings in memory only; nothing leaves the browser until the user
// creates a bug report. Typing in text fields is never recorded.
(function () {
  "use strict";
  const LIMIT = { errors: 100, api: 150, actions: 150, notes: 100 };
  const rings = { errors: [], api: [], actions: [], notes: [] };
  const started = Date.now();

  function push(kind, entry) {
    const ring = rings[kind];
    ring.push(Object.assign({ t: Date.now() }, entry));
    if (ring.length > LIMIT[kind]) ring.splice(0, ring.length - LIMIT[kind]);
  }

  function text(v) {
    if (v instanceof Error) return `${v.name}: ${v.message}`;
    if (typeof v === "string") return v;
    try { return JSON.stringify(v); } catch { return String(v); }
  }

  window.addEventListener("error", (e) => {
    if (e.target && e.target !== window && (e.target.src || e.target.href)) {
      push("errors", { kind: "resource", msg: `failed to load ${e.target.src || e.target.href}` });
      return;
    }
    push("errors", {
      kind: "error",
      msg: e.message,
      where: e.filename ? `${e.filename.replace(location.origin, "")}:${e.lineno}:${e.colno}` : undefined,
      stack: e.error && e.error.stack ? String(e.error.stack).slice(0, 4000) : undefined,
    });
  }, true);

  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    push("errors", { kind: "rejection", msg: text(r), stack: r && r.stack ? String(r.stack).slice(0, 4000) : undefined });
  });

  for (const level of ["error", "warn"]) {
    const orig = console[level].bind(console);
    console[level] = (...args) => {
      try { push("errors", { kind: `console.${level}`, msg: args.map(text).join(" ").slice(0, 4000) }); } catch { /* never break logging */ }
      orig(...args);
    };
  }

  function describe(el) {
    const b = el.closest("button, a, input[type=checkbox], input[type=radio], select, summary, [role=tab], tr[data-line]");
    if (!b) return null;
    const label = (b.textContent || b.value || "").trim().replace(/\s+/g, " ").slice(0, 60);
    return { tag: b.tagName.toLowerCase(), id: b.id || undefined, label: label || undefined, checked: b.type === "checkbox" ? b.checked : undefined };
  }

  document.addEventListener("click", (e) => {
    const d = describe(e.target);
    if (d) push("actions", Object.assign({ kind: "click" }, d));
  }, true);

  document.addEventListener("keydown", (e) => {
    const t = e.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    if (typing && e.key !== "Escape" && e.key !== "Enter") return; // never record what is typed
    const mods = (e.ctrlKey ? "Ctrl+" : "") + (e.altKey ? "Alt+" : "") + (e.metaKey ? "Meta+" : "") + (e.shiftKey ? "Shift+" : "");
    push("actions", { kind: "key", key: mods + (e.key === " " ? "Space" : e.key), repeat: e.repeat || undefined, inField: typing || undefined });
  }, true);

  window.addEventListener("pageshow", (e) => push("notes", { msg: e.persisted ? "page restored from cache" : "page loaded" }));
  document.addEventListener("visibilitychange", () => push("notes", { msg: `page ${document.visibilityState}` }));
  window.addEventListener("online", () => push("notes", { msg: "browser online" }));
  window.addEventListener("offline", () => push("notes", { msg: "browser offline" }));

  window.gcsDiag = {
    started,
    api(entry) { push("api", entry); },
    note(msg, extra) { push("notes", Object.assign({ msg }, extra || {})); },
    snapshot() {
      return {
        started,
        errors: rings.errors.slice(),
        apiCalls: rings.api.slice(),
        actions: rings.actions.slice(),
        notes: rings.notes.slice(),
      };
    },
  };
})();
