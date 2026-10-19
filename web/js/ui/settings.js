// Settings dialog: GRBL ($$) editor and application settings.

import { api } from "../api.js";
import { initKeymapEditor } from "../keymap.js";
import { $, $$ } from "./dom.js";

export function initTabs(dialog) {
  const tabs = $$(".tab", dialog);
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      for (const t of tabs) t.classList.toggle("active", t === tab);
      for (const body of $$("[data-tab-body]", dialog)) body.hidden = body.dataset.tabBody !== tab.dataset.tab;
    });
  }
}

const APP_FIELDS = [
  "statusInterval", "restoreAfterReset", "resetOnConnect", "autoConnect", "transcriptStatus",
  "jogSteps", "jogFeeds", "safeZ", "jogLimitsEnabled", "spindleMin", "spindleMax",
  "ignoreErrors", "jobStartCommands", "jobEndCommands", "toolChangeMode", "toolChangeCommands", "optionalStop",
  "spindleDwell", "entryPlungeFeed", "backlashEnabled", "backlashX", "backlashY", "backlashZ",
];

export function initSettings(store) {
  const dlg = $("#dlg-settings");
  const body = $("#grbl-settings-body");
  const save = $("#grbl-settings-save");
  const status = $("#grbl-settings-status");
  const note = $("#grbl-settings-note");
  const edits = new Map(); // id -> number
  initTabs(dlg);
  initKeymapEditor();

  $("#open-settings").addEventListener("click", () => {
    edits.clear();
    renderGrbl();
    renderApp();
    status.textContent = "";
    $("#app-settings-status").textContent = "";
    dlg.showModal();
  });

  function flaggedIds() {
    const ids = new Set();
    for (const w of store.state.warnings || []) {
      const m = /^\$(\d+)=/.exec(w.fix || "");
      if (m) ids.add(Number(m[1]));
    }
    return ids;
  }

  function renderGrbl() {
    const list = store.state.grblSettings || [];
    const connected = store.state.connection.state === "connected";
    const state = store.state.status ? store.state.status.state : null;
    const editable = connected && (state === "Idle" || state === "Alarm");
    note.textContent = !connected
      ? "Not connected."
      : list.length === 0
        ? "Settings have not been read yet. Press Reload (requires Idle or Alarm)."
        : "Stored in the controller's EEPROM. Changes can only be written while Idle or in Alarm." +
          (editable ? "" : ` Current state: ${state}.`);
    const flagged = flaggedIds();
    body.replaceChildren(
      ...list.map((s) => {
        const tr = document.createElement("tr");
        tr.dataset.id = s.id;
        if (flagged.has(s.id)) tr.classList.add("flagged");
        const sid = document.createElement("td");
        sid.className = "sid";
        sid.textContent = `$${s.id}`;
        const name = document.createElement("td");
        const n = document.createElement("div");
        n.className = "name";
        n.textContent = s.name;
        const d = document.createElement("div");
        d.className = "desc";
        d.textContent = s.help || s.desc || "";
        name.append(n, d);
        const val = document.createElement("td");
        const input = document.createElement("input");
        const current = edits.has(s.id) ? edits.get(s.id) : s.value;
        if (s.kind === "bool") {
          input.type = "checkbox";
          input.checked = Number(current) === 1;
        } else {
          input.type = "number";
          input.min = "0";
          input.step = s.kind === "float" ? "any" : "1";
          input.value = current;
        }
        input.disabled = !editable;
        input.addEventListener("input", () => {
          const v = input.type === "checkbox" ? (input.checked ? 1 : 0) : Number(input.value);
          if (input.type !== "checkbox" && (input.value === "" || !Number.isFinite(v))) return;
          if (Number(v) === Number(s.value)) edits.delete(s.id);
          else edits.set(s.id, v);
          tr.classList.toggle("changed", edits.has(s.id));
          save.disabled = edits.size === 0;
        });
        if (edits.has(s.id)) tr.classList.add("changed");
        val.append(input);
        if (s.kind === "mask") {
          const bits = document.createElement("div");
          bits.className = "desc mono";
          bits.textContent = Number(s.value).toString(2).padStart(8, "0");
          val.append(bits);
        }
        const unit = document.createElement("td");
        unit.className = "desc";
        unit.textContent = s.unit || "";
        tr.append(sid, name, val, unit);
        return tr;
      }),
    );
    save.disabled = edits.size === 0 || !editable;
  }

  save.addEventListener("click", async () => {
    const values = Object.fromEntries(edits);
    save.disabled = true;
    status.textContent = "Writing…";
    try {
      const r = await api.grblSettingsWrite(values);
      const failed = r.results.filter((x) => x.status !== "ok");
      status.textContent = failed.length
        ? `Failed: ${failed.map((f) => `${f.line} → ${f.response}`).join("; ")}`
        : `Wrote ${r.results.length} setting(s).`;
      for (const x of r.results) if (x.status === "ok") edits.delete(x.id);
    } catch (e) {
      status.textContent = e.message;
    }
    renderGrbl();
  });

  $("#grbl-settings-reload").addEventListener("click", async () => {
    status.textContent = "Reading…";
    try {
      await api.grblSettingsRefresh();
      status.textContent = "Settings read from the controller.";
    } catch (e) {
      status.textContent = e.message;
    }
  });

  // -- application settings ---------------------------------------------------

  function renderApp() {
    const values = store.state.appSettings || {};
    for (const key of APP_FIELDS) {
      const input = $(`#app-${key}`);
      if (input.type === "checkbox") input.checked = !!values[key];
      else input.value = values[key] ?? "";
    }
    const limits = values.jogLimits || [];
    for (const input of $$("#app-jogLimits input")) input.value = limits[Number(input.dataset.i)] ?? "";
  }

  $("#app-settings-save").addEventListener("click", async () => {
    const changes = {};
    for (const key of APP_FIELDS) {
      const input = $(`#app-${key}`);
      if (input.type === "checkbox") changes[key] = input.checked;
      else if (input.type === "number") changes[key] = Number(input.value);
      else changes[key] = input.value;
    }
    changes.jogLimits = $$("#app-jogLimits input")
      .sort((a, b) => a.dataset.i - b.dataset.i)
      .map((i) => Number(i.value));
    try {
      await api.appSettingsWrite(changes);
      $("#app-settings-status").textContent = "Saved.";
    } catch (e) {
      $("#app-settings-status").textContent = e.message;
    }
  });

  store.subscribe((kind) => {
    if (!dlg.open) return;
    if (["grbl.settings", "warnings", "connection", "snapshot"].includes(kind)) renderGrbl();
    if (kind === "status" && store.state.status) {
      // Re-render only when editability changes.
      const s = store.state.status.state;
      if (s !== dlg.dataset.lastState) {
        dlg.dataset.lastState = s;
        renderGrbl();
      }
    }
    if (kind === "app.settings") renderApp();
  });

}
