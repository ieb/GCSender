// Program panel: upload/library, start/pause/stop, start-from-line, feed
// override, progress (bar, ETA, virtualised line table) and the error and
// tool-change prompts. The server owns the job; everything here is rebuilt
// from the retained "job" event, so a reload shows the job exactly as it is.

import { api } from "../api.js";
import { $, $$ } from "./dom.js";

const ROW_H = 20;
const ACTIVE = new Set(["starting", "running", "paused", "error", "toolchange"]);

function hms(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
}

function beep() {
  try {
    const ctx = new AudioContext();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.15, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.6);
    o.connect(g).connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + 0.6);
  } catch { /* no audio */ }
}

export function initBottomTabs() {
  for (const t of $$("[data-bottom]")) {
    t.addEventListener("click", () => {
      for (const o of $$("[data-bottom]")) o.classList.toggle("active", o === t);
      for (const b of $$("[data-bottom-body]")) b.hidden = b.dataset.bottomBody !== t.dataset.bottom;
    });
  }
}

export function initJob(store, log, view) {
  const table = $("#job-table");
  const rowsEl = $("#job-table-rows");
  const spacer = $("#job-table-spacer");
  const fromEl = $("#job-from");
  let lines = [];
  let digest = null;
  let selected = null;
  let lastState = null;
  let fetching = null;

  const run = (p, what) => p.catch((e) => log(`${what}: ${e.message}`, "err"));

  // -- library / upload -----------------------------------------------------

  async function refreshLibrary() {
    try {
      const { jobs } = await api.jobs();
      const sel = $("#job-library");
      sel.replaceChildren(new Option("Library…", ""));
      for (const j of jobs) {
        const d = new Date(j.uploaded * 1000);
        sel.add(new Option(`${j.name} · ${j.lines} lines · ${d.toLocaleDateString()} ${d.toLocaleTimeString().slice(0, 5)}`, j.id));
      }
    } catch { /* not connected to the server yet */ }
  }

  async function upload(file) {
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) { log("program larger than 50 MB", "err"); return; }
    log(`uploading ${file.name} (${(file.size / 1024).toFixed(0)} kB)…`);
    try {
      const meta = await api.jobUpload(file);
      log(`uploaded ${meta.name}: ${meta.lines} lines`);
      refreshLibrary();
    } catch (e) {
      log(`upload: ${e.message}`, "err");
    }
  }

  $("#job-file").addEventListener("change", (e) => {
    upload(e.target.files[0]);
    e.target.value = "";
  });
  const panel = $("#panel-program");
  panel.addEventListener("dragover", (e) => { e.preventDefault(); });
  panel.addEventListener("drop", (e) => {
    e.preventDefault();
    upload(e.dataTransfer.files[0]);
  });
  $("#job-library").addEventListener("focus", refreshLibrary);
  $("#job-library").addEventListener("change", (e) => {
    const id = e.target.value;
    e.target.value = "";
    if (id) run(api.jobSelect(id), "load");
  });
  $("#job-unload").addEventListener("click", () => run(api.jobUnload(), "unload"));
  $("#job-heightmap").addEventListener("change", (e) => {
    const on = e.target.checked;
    run(api.jobHeightmap(on).catch((err) => { e.target.checked = !on; throw err; }), "heightmap");
  });

  // -- control ----------------------------------------------------------------

  $("#job-start").addEventListener("click", () => {
    const from = Math.max(1, Math.floor(Number(fromEl.value) || 1)) - 1;
    if (from > 0 && !confirm(`Start from line ${from + 1}?\n\nThe server restores the program state at that line, raises to safe Z, moves to the start XY, starts the spindle (with a dwell) and plunges at the entry feed.`)) return;
    run(api.jobStart(from), "start");
  });
  $("#job-pause").addEventListener("click", () => {
    const st = store.state.job?.state;
    const held = st === "paused" || (st === "running" && store.state.status?.state === "Hold");
    run(held ? api.jobResume() : api.jobPause(), held ? "resume" : "pause");
  });
  $("#job-stop").addEventListener("click", () => run(api.jobStop(), "stop"));
  $("#job-prompt-stop").addEventListener("click", () => run(api.jobStop(), "stop"));
  $("#job-continue").addEventListener("click", () => run(api.jobResume(), "continue"));

  const ovFeed = $("#job-ov-feed");
  ovFeed.addEventListener("input", () => { $("#job-ov-feed-val").textContent = `${ovFeed.value}%`; });
  ovFeed.addEventListener("change", () => run(api.jobOverride("feed", Number(ovFeed.value)), "feed override"));
  ovFeed.addEventListener("dblclick", () => { ovFeed.value = 100; ovFeed.dispatchEvent(new Event("change")); });
  const ovSpindle = $("#job-ov-spindle");
  ovSpindle.addEventListener("input", () => { $("#job-ov-spindle-val").textContent = `${ovSpindle.value}%`; });
  ovSpindle.addEventListener("change", () => run(api.jobOverride("spindle", Number(ovSpindle.value)), "spindle override"));
  $("#job-ov-rapid").addEventListener("change", (e) => run(api.jobOverride("rapid", Number(e.target.value)), "rapid override"));

  // -- table ------------------------------------------------------------------

  function rowClass(i, j, errs) {
    if (errs.has(i)) return "err";
    if (!j) return "";
    if (i < j.startLine) return "done";
    if (i <= (j.executing ?? j.acked)) return "done";
    if (i <= j.acked) return "buffer queued";
    if (i <= j.sent) return "buffer";
    return "";
  }

  function renderRows() {
    const j = store.state.job;
    const errs = new Map((j?.errors || []).map((e) => [e.line, e.response]));
    const first = Math.max(0, Math.floor(table.scrollTop / ROW_H) - 5);
    const last = Math.min(lines.length, first + Math.ceil(table.clientHeight / ROW_H) + 10);
    const from = Math.max(1, Math.floor(Number(fromEl.value) || 1)) - 1;
    const frag = document.createDocumentFragment();
    for (let i = first; i < last; i++) {
      const r = document.createElement("div");
      const cls = rowClass(i, j, errs);
      r.className = `row ${cls}${i === selected ? " sel" : ""}${i === from && from > 0 ? " from" : ""}`;
      r.dataset.line = i;
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = i + 1;
      const c = document.createElement("span");
      c.textContent = lines[i];
      const s = document.createElement("span");
      s.textContent = errs.has(i) ? errs.get(i)
        : cls === "done" && i >= (j?.startLine ?? 0) ? "ok"
        : cls === "buffer queued" ? "queued" : cls === "buffer" ? "sent" : "";
      if (cls === "buffer queued") s.title = "acknowledged: in GRBL's planner, not yet executed";
      r.append(n, c, s);
      frag.append(r);
    }
    rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
    rowsEl.replaceChildren(frag);
  }

  table.addEventListener("scroll", () => requestAnimationFrame(renderRows));
  new ResizeObserver(() => renderRows()).observe(table);
  rowsEl.addEventListener("click", (e) => {
    const r = e.target.closest(".row");
    if (!r) return;
    selected = Number(r.dataset.line);
    if (!ACTIVE.has(store.state.job?.state)) fromEl.value = selected + 1;
    view.setSelected(selected);
    renderRows();
  });
  fromEl.addEventListener("input", renderRows);

  function follow(j) {
    if (!$("#job-autoscroll").checked || !ACTIVE.has(j.state)) return;
    const target = Math.max(0, ((j.executing ?? j.acked) + 1) * ROW_H - table.clientHeight / 3);
    if (Math.abs(table.scrollTop - target) > ROW_H) table.scrollTop = target;
  }

  async function loadText(j) {
    const want = j?.variant?.digest || null;
    if (want === digest) return;
    digest = want;
    selected = null;
    if (!want) {
      lines = [];
      spacer.style.height = "0px";
      view.setProgram(null);
      renderRows();
      return;
    }
    const p = (fetching = api.jobText());
    try {
      const text = await p;
      if (p !== fetching) return; // superseded
      lines = text.split(/\r?\n/);
      if (lines.length && lines[lines.length - 1] === "") lines.pop();
      spacer.style.height = `${lines.length * ROW_H}px`;
      view.setProgram(text);
      const cur = store.state.job;
      if (cur) view.setProgress(cur.executing ?? cur.acked, cur.sent);
      table.scrollTop = 0;
      renderRows();
    } catch (e) {
      digest = null;
      log(`program text: ${e.message}`, "err");
    }
  }

  // -- state ------------------------------------------------------------------

  function update() {
    const j = store.state.job;
    const connected = store.state.connection?.state === "connected";
    const st = j?.state || "idle";
    const active = ACTIVE.has(st);
    const v = j?.variant;
    loadText(j);
    $("#job-name").textContent = j?.job ? `${j.job.name} · ${v?.lines ?? 0} lines${v?.kind === "transformed" ? " · heightmap" : ""}` : "no program";
    $("#job-heightmap").checked = !!j?.heightmap?.enabled;
    $("#job-heightmap").disabled = active || !j?.job;
    $("#job-unload").disabled = active || !j?.job;
    $("#job-library").disabled = active;
    $("#job-file").disabled = false;
    $("#job-start").disabled = !connected || active || !j?.job || store.state.status?.state !== "Idle";
    const pause = $("#job-pause");
    const held = st === "paused" || (st === "running" && store.state.status?.state === "Hold");
    pause.disabled = !(st === "running" || st === "paused");
    pause.textContent = held ? "▶ Resume" : "⏸ Pause";
    $("#job-stop").disabled = !active;
    fromEl.disabled = active;
    if (v) fromEl.max = v.lines;

    // overrides
    const rt = j?.overrideMode === "realtime";
    $("#job-ov-rt").hidden = !rt;
    const ov = j?.override || { feed: 100, rapid: 100, spindle: 100 };
    if (document.activeElement !== ovFeed) { ovFeed.value = ov.feed; $("#job-ov-feed-val").textContent = `${ov.feed}%`; }
    if (document.activeElement !== ovSpindle) { ovSpindle.value = ov.spindle; $("#job-ov-spindle-val").textContent = `${ov.spindle}%`; }
    $("#job-ov-rapid").value = String(ov.rapid);
    $("#job-ov-feed-wrap").title = rt
      ? "Feed override (GRBL 1.1 real-time)"
      : "Feed override (GRBL 0.9: the server rewrites F on lines as they are sent, so it takes effect after the lines already buffered in GRBL, up to ~17 moves)";

    // progress
    const total = v?.lines || 0;
    const doneLines = j ? Math.max(0, (j.executing ?? j.acked) + 1) : 0;
    // The server publishes on change; keep the clocks moving in between.
    const since = (performance.now() - receivedAt) / 1000;
    const elapsed = j?.elapsed !== null && j?.elapsed !== undefined && active ? j.elapsed + since : j?.elapsed;
    const remaining = j?.eta !== null && j?.eta !== undefined && st === "running" ? Math.max(0, j.eta - since) : j?.eta;
    const pct = total ? Math.min(100, (100 * doneLines) / total) : 0;
    const bar = $("#job-bar");
    bar.style.width = `${st === "done" ? 100 : pct}%`;
    bar.className = `fill${st === "done" ? " done" : st === "failed" || st === "error" ? " bad" : ""}`;
    let text = "";
    if (j?.job) {
      if (active) text = `${st} · line ${Math.min(doneLines + 1, total)} / ${total} · ${pct.toFixed(1)}% · elapsed ${hms(elapsed)} · remaining ${hms(remaining)}`;
      else if (st === "ready") text = `ready · estimated ${hms(j.eta)}`;
      else text = `${st}${j.message ? ` · ${j.message}` : ""}${j.elapsed !== null ? ` · ${hms(j.elapsed)}` : ""}`;
      if (j.errorCount) text += ` · ${j.errorCount} error${j.errorCount > 1 ? "s" : ""}`;
    }
    $("#job-progress-text").textContent = text;
    $("#job-tab-badge").textContent = active ? `· ${pct.toFixed(0)}%` : st === "done" ? "· done" : "";

    // prompts
    const prompt = $("#job-prompt");
    if (st === "error" && j.heldError) {
      prompt.hidden = false;
      $("#job-prompt-text").textContent = `Error at line ${j.heldError.line + 1} (${j.heldError.command}): ${j.heldError.response}. The machine is in feed hold.`;
      $("#job-continue").textContent = "Continue";
    } else if (st === "toolchange") {
      const t = j.toolchange?.tool;
      prompt.hidden = false;
      $("#job-prompt-text").textContent = `Tool change at line ${(j.toolchange?.line ?? 0) + 1}: fit tool ${t ?? "?"}. You may jog and probe Z now, then press Continue. The program continues from where the machine is now.`;
      $("#job-continue").textContent = "Continue";
    } else {
      prompt.hidden = true;
    }

    // info: bounds and warnings
    const info = [];
    if (v?.bounds) {
      const [lo, hi] = [v.bounds.min, v.bounds.max];
      info.push(`X ${lo[0].toFixed(2)}…${hi[0].toFixed(2)}  Y ${lo[1].toFixed(2)}…${hi[1].toFixed(2)}  Z ${lo[2].toFixed(2)}…${hi[2].toFixed(2)} mm`);
    }
    if (v?.tooLong?.length) info.push(`⚠ line ${v.tooLong[0] + 1} is too long for GRBL`);
    if (v?.m6Lines?.length) info.push(`${v.m6Lines.length} tool change${v.m6Lines.length > 1 ? "s" : ""}`);
    if (j?.heightmap?.stale) info.push("heightmap changed: recomputed at start");
    if (v?.stats?.warnings?.length) info.push(`⚠ ${v.stats.warnings[0]}`);
    $("#job-info").textContent = info.join(" · ");

    if (j) {
      view.setProgress(j.executing ?? j.acked, j.sent);
      follow(j);
    }
    renderRows();

    if (lastState && ACTIVE.has(lastState) && !active && j?.job) {
      if (st === "done") { beep(); log(`job done: ${j.message || ""}`); }
      else log(`job ${st}${j.message ? `: ${j.message}` : ""}`, st === "failed" ? "err" : undefined);
    }
    if (st === "error" && lastState !== "error") beep();
    if (st === "toolchange" && lastState !== "toolchange") beep();
    lastState = st;
  }

  let receivedAt = performance.now();
  store.subscribe((kind) => {
    if (kind === "job" || kind === "snapshot") receivedAt = performance.now();
    if (kind === "job" || kind === "snapshot" || kind === "status" || kind === "connection") update();
  });
  setInterval(() => { if (ACTIVE.has(store.state.job?.state)) update(); }, 1000);
  refreshLibrary();
  update();
}
