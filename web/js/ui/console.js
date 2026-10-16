// Console: controller transcript (from server events) and command input with history.

import { api } from "../api.js";
import { $ } from "./dom.js";

const HISTORY_KEY = "gcs-console-history";
const HISTORY_LIMIT = 100;
const DOM_LIMIT = 2000;

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
  let history = [];
  try { history = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { history = []; }
  let hIndex = history.length;

  function node(entry) {
    const div = document.createElement("div");
    const cls = lineClass(entry);
    if (cls) div.className = cls;
    div.textContent = lineText(entry);
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
    if (history[history.length - 1] !== line) {
      history.push(line);
      if (history.length > HISTORY_LIMIT) history = history.slice(-HISTORY_LIMIT);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
    }
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
    } else if (e.key === "ArrowDown") {
      hIndex = Math.min(history.length, hIndex + 1);
      input.value = history[hIndex] || "";
      e.preventDefault();
    }
  });

  $("#console-clear").addEventListener("click", () => {
    out.replaceChildren();
  });

  renderAll();
  return local;
}
