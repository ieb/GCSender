// Console: controller transcript (from server events) and command input with
// history, autocomplete, source filters and inline error help.

import { api } from "../api.js";
import { $ } from "./dom.js";

const HISTORY_KEY = "gcs-console-history";
const FILTER_KEY = "gcs-console-filters";
const HISTORY_LIMIT = 100;
const DOM_LIMIT = 2000;
const COMMON = ["$$", "$#", "$G", "$I", "$N", "$X", "$H", "$C", "G0 X0 Y0", "G90", "G91", "G21", "M3 S1000", "M5"];

function lineClass(entry) {
  if (entry.dir === "tx") return "tx";
  if (entry.dir === "rt") return "rt";
  if (entry.dir === "sys" || entry.dir === "local") return "sys";
  if (/^(error|ALARM)/.test(entry.line)) return "err";
  if (entry.line === "ok") return "ok";
  return "";
}

function lineText(entry) {
  if (entry.dir === "rt") return `[${entry.line}]`;
  return entry.line;
}

export function initConsole(store) {
  const out = $("#console-out");
  const form = $("#console-form");
  const input = $("#console-input");
  const datalist = $("#console-history");
  const showSystem = $("#console-show-system");
  const showUi = $("#console-show-ui");
  let history = [];
  try { history = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { history = []; }
  let hIndex = history.length;

  try {
    const f = JSON.parse(localStorage.getItem(FILTER_KEY) || "{}");
    if (typeof f.system === "boolean") showSystem.checked = f.system;
    if (typeof f.ui === "boolean") showUi.checked = f.ui;
  } catch { /* defaults */ }

  function applyFilters() {
    out.classList.toggle("hide-system", !showSystem.checked);
    out.classList.toggle("hide-ui", !showUi.checked);
    localStorage.setItem(FILTER_KEY, JSON.stringify({ system: showSystem.checked, ui: showUi.checked }));
    out.scrollTop = out.scrollHeight;
  }
  showSystem.addEventListener("change", applyFilters);
  showUi.addEventListener("change", applyFilters);
  applyFilters();

  function renderDatalist() {
    const seen = new Set();
    const items = [...history.slice().reverse(), ...COMMON].filter((c) => !seen.has(c) && seen.add(c));
    datalist.replaceChildren(...items.map((c) => Object.assign(document.createElement("option"), { value: c })));
  }
  renderDatalist();

  function node(entry) {
    const div = document.createElement("div");
    const classes = [lineClass(entry)];
    if (entry.src) classes.push(`src-${entry.src}`);
    div.className = classes.filter(Boolean).join(" ");
    div.textContent = lineText(entry);
    if (entry.help) {
      div.title = entry.help;
      const help = document.createElement("div");
      help.className = "help";
      help.textContent = `${entry.title ? entry.title + ": " : ""}${entry.help}`;
      div.append(help);
    }
    return div;
  }

  function atBottom() {
    return out.scrollHeight - out.scrollTop - out.clientHeight < 30;
  }

  function append(entry) {
    const stick = atBottom();
    out.append(node(entry));
    while (out.childElementCount > DOM_LIMIT) out.firstElementChild.remove();
    if (stick) out.scrollTop = out.scrollHeight;
  }

  function renderAll() {
    out.replaceChildren(...store.state.console.slice(-DOM_LIMIT).map(node));
    out.scrollTop = out.scrollHeight;
  }

  function local(text) {
    append({ dir: "local", line: text });
  }

  store.subscribe((kind, _state, payload) => {
    if (kind === "snapshot") renderAll();
    else if (kind === "console") append(payload);
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const line = input.value.trim();
    if (!line) return;
    history = history.filter((h) => h !== line);
    history.push(line);
    if (history.length > HISTORY_LIMIT) history = history.slice(-HISTORY_LIMIT);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
    renderDatalist();
    hIndex = history.length;
    input.value = "";
    try {
      await api.command(line); // the transcript arrives via server events
    } catch (err) {
      local(`${line}: ${err.message}`);
    }
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && hIndex > 0) {
      hIndex -= 1;
      input.value = history[hIndex];
      e.preventDefault();
    } else if (e.key === "ArrowDown" && hIndex < history.length) {
      hIndex += 1;
      input.value = history[hIndex] || "";
      e.preventDefault();
    }
  });

  $("#console-clear").addEventListener("click", () => out.replaceChildren());

  renderAll();
  return local;
}
