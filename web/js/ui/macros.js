// User commands: buttons that run server-side macros, and their editor.

import { api } from "../api.js";
import { $ } from "./dom.js";
import { registerAction } from "../keymap.js";

const BLOCKING = new Set(["starting", "running", "paused", "error"]);

export function initMacros(store, log) {
  const box = $("#macros-buttons");
  const status = $("#macro-status");
  const dlg = $("#dlg-macros");
  const list = $("#macro-list");

  function render() {
    const macros = store.state.macros || [];
    const run = store.state.macro || {};
    const connected = store.state.connection?.state === "connected";
    const jobBusy = BLOCKING.has(store.state.job?.state);
    box.replaceChildren(...macros.map((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = m.name;
      b.title = `Run user command “${m.name}” on the server:\n${m.text}`;
      b.classList.toggle("running", run.running === m.id);
      b.disabled = !connected || jobBusy || (!!run.running && run.running !== m.id);
      b.addEventListener("click", () => {
        if (run.running) return;
        api.macroRun(m.id).catch((e) => log(`${m.name}: ${e.message}`));
      });
      return b;
    }));
    status.textContent = run.running ? `running ${run.name}${run.line ? ` · line ${run.line}` : ""}${run.message ? ` · ${run.message}` : ""} (Esc / Stop aborts)`
      : run.message || run.error || "";
    status.classList.toggle("err", !!run.error && !run.running);
  }

  // -- editor -------------------------------------------------------------------

  function item(m = { name: "", text: "" }) {
    const el = document.createElement("div");
    el.className = "macro-item";
    if (m.id) el.dataset.id = m.id;
    const name = document.createElement("input");
    name.className = "m-name";
    name.maxLength = 40;
    name.placeholder = "Button label";
    name.value = m.name;
    const tools = document.createElement("span");
    tools.className = "item-tools";
    const mk = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = title;
      b.addEventListener("click", fn);
      return b;
    };
    tools.append(
      mk("↑", "Move up", () => el.previousElementSibling && list.insertBefore(el, el.previousElementSibling)),
      mk("↓", "Move down", () => el.nextElementSibling && list.insertBefore(el.nextElementSibling, el)),
      mk("✕", "Remove", () => el.remove()),
    );
    const text = document.createElement("textarea");
    text.className = "m-text mono";
    text.rows = Math.min(10, Math.max(3, m.text.split("\n").length + 1));
    text.spellcheck = false;
    text.placeholder = "G-code, one line per row";
    text.value = m.text;
    el.append(name, tools, text);
    return el;
  }

  $("#macros-edit").addEventListener("click", () => {
    list.replaceChildren(...(store.state.macros || []).map((m) => item(m)));
    $("#macro-edit-status").textContent = "";
    dlg.showModal();
  });
  $("#macro-add").addEventListener("click", () => {
    const el = item();
    list.append(el);
    el.querySelector("input").focus();
  });
  $("#macro-save").addEventListener("click", async () => {
    const macros = [...list.children].map((el) => ({
      id: el.dataset.id,
      name: el.querySelector(".m-name").value,
      text: el.querySelector(".m-text").value,
    }));
    try {
      await api.macrosSave(macros);
      $("#macro-edit-status").textContent = "Saved.";
      dlg.close();
    } catch (e) {
      $("#macro-edit-status").textContent = e.message;
    }
  });

  for (let i = 0; i < 9; i++) {
    registerAction(`macro${i + 1}`, () => {
      const b = box.children[i];
      if (b && !b.disabled) b.click();
    });
  }

  store.subscribe((kind) => {
    if (["snapshot", "macros", "macro", "connection", "job"].includes(kind)) render();
  });
  render();
}
