// Left-column tabs, Z probe panel and heightmap panel.
//
// Everything runs on the server; this module only edits parameters, starts
// and aborts routines, and renders the retained "probe" and "heightmap"
// events, so a reload mid-probe shows the run continuing.

import { api, heightmapExportUrl } from "../api.js";
import { $, $$, fmt } from "./dom.js";

const TAB_KEY = "gcs-left-tab";
const PARAMS = ["x", "y", "w", "h", "originX", "originY", "nx", "ny", "zTop", "zBottom", "feed", "interpNx", "interpNy"];
const INTS = new Set(["nx", "ny", "interpNx", "interpNy"]);

function readFile(input) {
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return Promise.resolve(null);
  return file.text().then((text) => ({ name: file.name, text }));
}

export function initLeftTabs() {
  const tabs = $$("[data-left-tab]");
  function select(name) {
    localStorage.setItem(TAB_KEY, name);
    for (const t of tabs) t.classList.toggle("active", t.dataset.leftTab === name);
    for (const p of $$(".col-left [data-group]")) p.hidden = p.dataset.group !== name || p.hasAttribute("data-unavailable");
  }
  for (const t of tabs) t.addEventListener("click", () => select(t.dataset.leftTab));
  select(localStorage.getItem(TAB_KEY) || "machine");
  return select;
}

export function initProbe(store, log) {
  const status = $("#probe-status");
  const inputs = $$("#probe-form [data-setting]");

  // Inputs are app settings, saved on change so the server uses the same values.
  for (const el of inputs) {
    el.addEventListener("change", () => {
      const value = el.type === "checkbox" ? el.checked : Number(el.value);
      api.appSettingsWrite({ [el.dataset.setting]: value }).catch((e) => {
        log(`probe settings: ${e.message}`);
        render();
      });
    });
  }

  function say(text, error = false) {
    status.textContent = text;
    status.classList.toggle("error", error);
  }

  $("#probe-z").addEventListener("click", () => {
    const app = store.state.appSettings || {};
    const ok = confirm(
      `Probe Z: the tool moves DOWN up to ${app.probeDepth} mm until it touches.\n\n` +
        "• The probe clip is attached and the plate/work is under the tool\n" +
        "• You tested the probe input (Test: touching) since connecting\n\nContinue?",
    );
    if (!ok) return;
    api.probeZ().catch((e) => say(e.message, true));
  });
  for (const [id, expect] of [["#probe-test-open", "open"], ["#probe-test-closed", "closed"]]) {
    $(id).addEventListener("click", () => {
      say("testing…");
      api.probeTest(expect).then(
        (r) => say(r.message, !r.ok),
        (e) => say(e.message, true),
      );
    });
  }

  function render() {
    const app = store.state.appSettings || {};
    for (const el of inputs) {
      if (document.activeElement === el) continue;
      const v = app[el.dataset.setting];
      if (el.type === "checkbox") el.checked = !!v;
      else if (v !== undefined) el.value = v;
    }
    const p = store.state.probe || {};
    const idle = store.state.connection.state === "connected" && (store.state.status || {}).state === "Idle";
    const running = p.state === "running";
    for (const id of ["#probe-z", "#probe-test-open", "#probe-test-closed"]) $(id).disabled = !idle || running;
    if (p.kind === "z") {
      if (running) say("probing Z…");
      else if (p.state === "done" && p.result) {
        const c = p.result.contact || [];
        say(`touched at machine Z ${fmt(c[2])}` + (p.result.wcs ? `; ${p.result.wcs} Z set so the contact reads ${p.result.plate}` : ""));
      } else if (p.state === "failed" || p.state === "aborted") say(p.message || p.state, true);
    }
  }

  store.subscribe((kind) => {
    if (["snapshot", "app.settings", "probe", "status", "connection"].includes(kind)) render();
  });
  render();
}

export function initHeightmap(store, log, view) {
  const form = $("#hm-form");
  const table = $("#hm-table");
  const progress = $("#hm-progress");
  const lib = $("#hm-lib");
  const tinfo = $("#hm-transform-info");
  const download = $("#hm-transform-download");
  const preview = $("#hm-preview");
  let edited = false; // form has unsaved edits
  let lastPoint = null;
  let transformed = null; // {name, gcode}

  const report = (p) => p.catch((e) => log(`heightmap: ${e.message}`));
  const hm = () => store.state.heightmap;

  // -- parameters ---------------------------------------------------------------

  form.addEventListener("input", () => { edited = true; });
  function formValues() {
    const out = {};
    for (const k of PARAMS) {
      const v = Number(form.elements[k].value);
      out[k] = INTS.has(k) ? Math.round(v) : v;
    }
    return out;
  }
  $("#hm-apply").addEventListener("click", () => {
    const m = hm();
    const v = formValues();
    const p = m ? m.params : {};
    const reshapes = ["x", "y", "w", "h", "nx", "ny"].some((k) => p[k] !== v[k]);
    if (m && m.probed && reshapes && !confirm("Changing the border or grid clears the probed values. Continue?")) return;
    api.heightmapParams(v).then(() => { edited = false; render(); }, (e) => log(`heightmap: ${e.message}`));
  });
  $("#hm-auto-file").addEventListener("change", async (e) => {
    const f = await readFile(e.target);
    if (!f) return;
    const m = hm();
    if (m && m.probed && !confirm("Setting the border clears the probed values. Continue?")) return;
    api.heightmapAuto(f.text, Number($("#hm-margin").value) || 0).then(
      (r) => { edited = false; log(`heightmap border from ${f.name}: X${r.border.x} Y${r.border.y} W${r.border.w} H${r.border.h}`); },
      (err) => log(`heightmap: ${err.message}`),
    );
  });

  // -- probing --------------------------------------------------------------------

  $("#hm-probe").addEventListener("click", () => {
    const m = hm();
    if (!m) return;
    const p = m.params;
    const ok = confirm(
      `Probe a ${p.nx}×${p.ny} heightmap?\n\n` +
        `• Work Z0 must be set near the surface (use Z probe first)\n` +
        `• Travel at Z ${p.zTop}, probing down to Z ${p.zBottom} at ${p.feed} mm/min\n` +
        `• The origin (${p.originX}, ${p.originY}) is probed first as the reference\n` +
        (m.probed ? "• The current values will be replaced\n" : "") +
        "\nContinue?",
    );
    if (ok) report(api.heightmapProbe());
  });
  $("#hm-abort").addEventListener("click", () => report(api.heightmapAbort()));
  $("#hm-origin").addEventListener("click", () => report(api.heightmapOrigin()));
  $("#hm-clear").addEventListener("click", () => {
    if (confirm("Clear all probed values?")) report(api.heightmapClear());
  });

  // -- table ------------------------------------------------------------------------

  table.addEventListener("change", (e) => {
    const input = e.target.closest("input");
    if (!input) return;
    const raw = input.value.trim();
    const z = raw === "" ? null : Number(raw);
    if (z !== null && !Number.isFinite(z)) {
      renderTable();
      return;
    }
    report(api.heightmapCells([{ i: Number(input.dataset.i), j: Number(input.dataset.j), z }]));
  });

  function renderTable() {
    const m = hm();
    if (!m) {
      table.replaceChildren();
      return;
    }
    const { nx, ny } = m.params;
    const head = document.createElement("tr");
    head.append(document.createElement("th"));
    for (let j = 0; j < nx; j++) {
      const th = document.createElement("th");
      th.textContent = fmt(m.params.x + (m.params.w * j) / (nx - 1), 1);
      head.append(th);
    }
    const rows = [head];
    for (let i = ny - 1; i >= 0; i--) { // highest Y on top, like the machine seen from above
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.textContent = fmt(m.params.y + (m.params.h * i) / (ny - 1), 1);
      tr.append(th);
      for (let j = 0; j < nx; j++) {
        const v = m.values[i][j];
        const td = document.createElement("td");
        const input = document.createElement("input");
        input.dataset.i = i;
        input.dataset.j = j;
        input.value = v === null ? "" : v.toFixed(3);
        input.placeholder = "—";
        input.title = `row ${i}, column ${j}`;
        td.classList.toggle("empty", v === null);
        td.classList.toggle("last", !!lastPoint && lastPoint[0] === i && lastPoint[1] === j);
        td.append(input);
        tr.append(td);
      }
      rows.push(tr);
    }
    table.replaceChildren(...rows);
  }

  // -- library / import / export ---------------------------------------------------

  function refreshLibrary() {
    api.heightmapLibrary().then((r) => {
      const current = lib.value;
      lib.replaceChildren(...r.maps.map((m) => new Option(m.name, m.name)));
      if (!r.maps.length) lib.append(new Option("(no saved maps)", ""));
      if ([...lib.options].some((o) => o.value === current)) lib.value = current;
    }, () => {});
  }
  $("#hm-load").addEventListener("click", () => {
    if (!lib.value) return;
    if (hm() && hm().dirty && !confirm("The current map has unsaved changes. Load anyway?")) return;
    report(api.heightmapLoad(lib.value));
  });
  $("#hm-delete").addEventListener("click", () => {
    if (lib.value && confirm(`Delete the saved map “${lib.value}”?`)) report(api.heightmapDelete(lib.value).then(refreshLibrary));
  });
  $("#hm-save").addEventListener("click", () => {
    const name = $("#hm-save-name").value.trim();
    if (!name) return;
    report(api.heightmapSave(name).then(refreshLibrary));
  });
  $("#hm-import").addEventListener("change", async (e) => {
    const f = await readFile(e.target);
    if (f) report(api.heightmapImport(f.text).then(() => log(`imported heightmap ${f.name}`)));
  });
  $("#hm-export").addEventListener("click", () => { $("#hm-export").href = heightmapExportUrl(); });

  // -- transform ----------------------------------------------------------------------

  $("#hm-transform-file").addEventListener("change", async (e) => {
    const f = await readFile(e.target);
    if (!f) return;
    tinfo.textContent = "compensating…";
    try {
      const r = await api.heightmapTransform(f.text);
      transformed = { name: f.name.replace(/(\.[^.]*)?$/, "-hm$1"), gcode: r.gcode, digest: hm() && hm().digest };
      const s = r.stats;
      tinfo.textContent =
        `${f.name}: ${s.lines_in} → ${s.lines_out} lines, ${s.segments} compensated segments` +
        (s.warnings.length ? `\n⚠ ${s.warnings.join("\n⚠ ")}` : "");
      if (download.href.startsWith("blob:")) URL.revokeObjectURL(download.href);
      download.href = URL.createObjectURL(new Blob([r.gcode], { type: "text/plain" }));
      download.download = transformed.name;
      download.textContent = `Download ${transformed.name}`;
      download.hidden = false;
      view.setPreview(preview.checked ? r.gcode : null);
    } catch (err) {
      tinfo.textContent = err.message;
    }
  });
  preview.addEventListener("change", () => view.setPreview(preview.checked && transformed ? transformed.gcode : null));

  // -- render ---------------------------------------------------------------------------

  function render() {
    const m = hm();
    const p = store.state.probe || {};
    const probing = p.state === "running" && p.kind === "map";
    if (m && !edited) {
      for (const k of PARAMS) {
        if (document.activeElement !== form.elements[k]) form.elements[k].value = m.params[k];
      }
    }
    if (m) {
      $("#hm-name").textContent = `${m.name || "unsaved"}${m.dirty ? " •" : ""} · ${m.probed}/${m.params.nx * m.params.ny}` +
        (m.min !== null ? ` · ${fmt(m.min)}…${fmt(m.max)} mm` : "");
    }
    const idle = store.state.connection.state === "connected" && (store.state.status || {}).state === "Idle";
    $("#hm-probe").disabled = !idle || p.state === "running";
    $("#hm-abort").disabled = !probing;
    $("#hm-origin").disabled = !idle || p.state === "running";
    for (const el of [$("#hm-apply"), $("#hm-clear"), $("#hm-load"), $("#hm-import").parentElement]) {
      if ("disabled" in el) el.disabled = probing;
    }
    table.classList.toggle("locked", probing);
    for (const input of $$("input", table)) input.disabled = probing;
    if (p.kind === "map") {
      progress.classList.toggle("error", p.state === "failed");
      if (probing) progress.textContent = `probing point ${Math.min((p.index || 0) + 1, p.total || 0)} of ${p.total} — safe to reload this page`;
      else if (p.state === "done") progress.textContent = `probed ${p.result ? p.result.points : ""} points`;
      else progress.textContent = p.message || "";
    }
    if (transformed && m && transformed.digest !== m.digest) {
      tinfo.textContent = "The heightmap changed: compensate the file again.";
      download.hidden = true;
      transformed = null;
      view.setPreview(null);
    }
  }

  let prevValues = null;
  store.subscribe((kind) => {
    if (kind === "heightmap" || kind === "snapshot") {
      const m = hm();
      // Highlight the most recently probed cell while a run is in progress.
      if (m && prevValues && (store.state.probe || {}).state === "running") {
        lastPoint = null;
        m.values.forEach((row, i) => row.forEach((v, j) => {
          if (v !== null && (!prevValues[i] || prevValues[i][j] === null)) lastPoint = [i, j];
        }));
      }
      prevValues = m ? m.values : null;
      renderTable();
      view.setHeightmap(m);
    }
    if (["snapshot", "heightmap", "probe", "status", "connection"].includes(kind)) render();
  });
  refreshLibrary();
  renderTable();
  render();
  if (hm()) view.setHeightmap(hm());
}
