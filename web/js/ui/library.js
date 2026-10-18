// Program library: single programs and sets (a zip of a PCB's programs,
// uploaded together and kept in order). Pick a set, then the program to load.

import { api } from "../api.js";
import { $ } from "./dom.js";

const ACTIVE = new Set(["starting", "running", "paused", "error", "toolchange"]);
const SINGLE = "__single";

function when(t) {
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function lastRunText(r) {
  if (!r) return { text: "—", cls: "muted", title: "Not run yet" };
  const base = { done: "✓ done", stopped: "■ stopped", failed: "✗ failed" }[r.state] || r.state;
  const at = when(r.at);
  const from = r.startLine ? ` (from line ${r.startLine + 1})` : "";
  const line = r.state !== "done" && r.lastLine >= 0 ? ` at line ${r.lastLine + 1}` : "";
  return {
    text: `${base}${line} · ${at}`,
    cls: r.state === "done" ? "ok" : "warn",
    title: `${r.message || base}${from}${r.heightmap ? " · with heightmap" : ""} · ${new Date(r.at * 1000).toLocaleString()}`,
  };
}

function btn(label, title, onClick, cls) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.title = title;
  if (cls) b.className = cls;
  b.addEventListener("click", onClick);
  return b;
}

export function initLibrary(store, log) {
  const dlg = $("#dlg-library");
  const setsEl = $("#lib-sets");
  const filesEl = $("#lib-files");
  const titleEl = $("#lib-set-title");
  const statusEl = $("#lib-status");
  let data = { jobs: [], sets: [] };
  let current = null; // set id or SINGLE
  const listeners = new Set();

  const jobActive = () => {
    const st = store.state.job?.state || "idle";
    return ACTIVE.has(st) || st === "interrupted";
  };

  function singles() {
    return data.jobs.filter((j) => !j.set);
  }

  /** The set a program belongs to, and the next program in it. */
  function placeOf(jobId) {
    for (const s of data.sets) {
      const i = s.files.findIndex((f) => f.id === jobId);
      if (i >= 0) return { set: s, index: i, next: s.files[i + 1] || null };
    }
    return null;
  }

  async function refresh() {
    try {
      data = await api.jobs();
    } catch { return data; /* not connected to the server yet */ }
    if (current && current !== SINGLE && !data.sets.some((s) => s.id === current)) current = null;
    if (dlg.open) render();
    for (const fn of listeners) fn();
    return data;
  }

  function renderSets() {
    const items = data.sets.map((s) => {
      const b = btn("", `${s.name}: ${s.files.length} programs, uploaded ${new Date(s.uploaded * 1000).toLocaleString()} from ${s.source || "a zip"}`, () => { current = s.id; render(); }, "lib-set");
      const done = s.files.filter((f) => f.lastRun?.state === "done").length;
      b.innerHTML = "";
      const name = document.createElement("b");
      name.textContent = s.name;
      const meta = document.createElement("span");
      meta.className = "muted small";
      meta.textContent = `${s.files.length} files · ${done}/${s.files.length} done · ${when(s.uploaded)}`;
      b.append(name, meta);
      b.classList.toggle("active", s.id === current);
      return b;
    });
    if (singles().length) {
      const b = btn("", "Programs uploaded on their own, newest first", () => { current = SINGLE; render(); }, "lib-set");
      const name = document.createElement("b");
      name.textContent = "Single files";
      const meta = document.createElement("span");
      meta.className = "muted small";
      meta.textContent = `${singles().length} programs`;
      b.append(name, meta);
      b.classList.toggle("active", current === SINGLE);
      items.push(b);
    }
    if (!items.length) {
      const p = document.createElement("p");
      p.className = "muted small";
      p.textContent = "Nothing uploaded yet. Upload a G-code file, or a zip of a board's programs.";
      items.push(p);
    }
    setsEl.replaceChildren(...items);
  }

  function renderFiles() {
    const set = data.sets.find((s) => s.id === current);
    const files = set ? set.files : current === SINGLE ? singles() : [];
    const loadedId = store.state.job?.job?.id;
    const busy = jobActive();
    $("#lib-rename").hidden = !set;
    $("#lib-delete-set").hidden = !set;
    titleEl.textContent = set ? set.name : current === SINGLE ? "Single files" : "Choose a set";
    const rows = files.map((f, i) => {
      const tr = document.createElement("tr");
      if (f.id === loadedId) tr.className = "loaded";
      const n = document.createElement("td");
      n.textContent = set ? String(i + 1) : "";
      n.className = "muted";
      const name = document.createElement("td");
      name.className = "mono";
      // Long CAM names break after _ - . / only.
      for (const part of (set ? f.path || f.name : f.name).split(/(?<=[_.\-/])/)) {
        name.append(part, document.createElement("wbr"));
      }
      name.title = `${f.name} · ${(f.size / 1024).toFixed(0)} kB · uploaded ${new Date(f.uploaded * 1000).toLocaleString()}`;
      const lines = document.createElement("td");
      lines.textContent = `${f.lines}`;
      lines.className = "num muted";
      const last = document.createElement("td");
      const lr = lastRunText(f.lastRun);
      last.textContent = lr.text;
      last.className = `small ${lr.cls}`;
      last.title = lr.title;
      const act = document.createElement("td");
      act.className = "lib-actions";
      const load = btn(f.id === loadedId ? "Loaded" : "Load", busy ? "A job is running: wait for it to finish" : `Load ${f.name} into the Program panel`, async () => {
        try {
          await api.jobSelect(f.id);
          dlg.close();
        } catch (e) { statusEl.textContent = `load: ${e.message}`; }
      }, f.id === loadedId ? "" : "primary");
      load.disabled = busy || f.id === loadedId;
      act.append(load);
      if (set) {
        const move = (d) => async () => {
          const order = set.files.map((x) => x.id);
          const j = i + d;
          [order[i], order[j]] = [order[j], order[i]];
          try {
            await api.jobSetUpdate(set.id, { order });
            await refresh();
          } catch (e) { statusEl.textContent = e.message; }
        };
        const up = btn("↑", "Move up: run earlier in this set", move(-1));
        up.disabled = i === 0;
        const down = btn("↓", "Move down: run later in this set", move(1));
        down.disabled = i === files.length - 1;
        act.append(up, down);
      }
      const del = btn("🗑", `Delete ${f.name} from the library`, async () => {
        if (!confirm(`Delete ${f.name}${set ? ` from ${set.name}` : ""}?`)) return;
        try {
          await api.jobDelete(f.id);
          await refresh();
        } catch (e) { statusEl.textContent = e.message; }
      });
      del.disabled = busy && f.id === loadedId;
      act.append(del);
      tr.append(n, name, lines, last, act);
      return tr;
    });
    filesEl.replaceChildren(...rows);
  }

  function render() {
    if (!current) current = data.sets[0]?.id || (singles().length ? SINGLE : null);
    renderSets();
    renderFiles();
  }

  async function open(setId) {
    statusEl.textContent = "";
    if (setId) current = setId;
    else {
      const loaded = store.state.job?.job;
      if (loaded) current = loaded.set || SINGLE;
    }
    await refresh();
    render();
    if (!dlg.open) dlg.showModal();
  }

  async function upload(file) {
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) { log("file larger than 50 MB", "err"); return; }
    log(`uploading ${file.name} (${(file.size / 1024).toFixed(0)} kB)…`);
    try {
      const r = await api.jobUpload(file);
      if (r.set) {
        const skipped = r.skipped?.length ? ` · skipped ${r.skipped.map((s) => `${s.path} (${s.reason})`).join(", ")}` : "";
        log(`uploaded set ${r.set.name}: ${r.set.files.map((f) => f.path).join(", ")}${skipped}`);
        await open(r.set.id);
        statusEl.textContent = `Uploaded ${r.set.files.length} programs${r.skipped?.length ? `; skipped ${r.skipped.length} other files (see the console)` : ""}. Check the order, then load the first.`;
      } else {
        log(`uploaded ${r.name}: ${r.lines} lines`);
        refresh();
      }
    } catch (e) {
      log(`upload: ${e.message}`, "err");
      if (dlg.open) statusEl.textContent = `upload: ${e.message}`;
    }
  }

  $("#lib-upload").addEventListener("change", (e) => {
    upload(e.target.files[0]);
    e.target.value = "";
  });
  $("#lib-rename").addEventListener("click", async () => {
    const set = data.sets.find((s) => s.id === current);
    if (!set) return;
    const name = prompt("Name of this set", set.name);
    if (!name || name.trim() === set.name) return;
    try {
      await api.jobSetUpdate(set.id, { name: name.trim() });
      await refresh();
    } catch (e) { statusEl.textContent = e.message; }
  });
  $("#lib-delete-set").addEventListener("click", async () => {
    const set = data.sets.find((s) => s.id === current);
    if (!set || !confirm(`Delete the set ${set.name} and its ${set.files.length} programs?`)) return;
    try {
      data = await api.jobSetDelete(set.id);
      current = null;
      render();
      for (const fn of listeners) fn();
    } catch (e) { statusEl.textContent = e.message; }
  });

  // Keep the dialog and the "next program" hint current as jobs change.
  let lastMetaKey = null;
  store.subscribe((kind, s) => {
    if (kind !== "job" && kind !== "snapshot") return;
    const j = s.job;
    const key = `${j?.job?.id}|${j?.state}|${j?.job?.lastRun?.at}`;
    if (key === lastMetaKey) return;
    lastMetaKey = key;
    refresh();
  });

  return { open, upload, refresh, placeOf, onChange: (fn) => listeners.add(fn) };
}
