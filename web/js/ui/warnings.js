// Settings warnings (e.g. $10 / $13) with one-click fixes.

import { api } from "../api.js";
import { $ } from "./dom.js";

export function initWarnings(store, log) {
  const box = $("#warnings");

  function render() {
    const list = store.state.connection.state === "connected" ? store.state.warnings || [] : [];
    box.hidden = list.length === 0;
    box.replaceChildren(
      ...list.map((w) => {
        const row = document.createElement("div");
        row.className = "warning" + (w.level === "error" ? " error" : "");
        const text = document.createElement("span");
        text.className = "text";
        text.textContent = w.text;
        row.append(text);
        if (w.fix) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = `Set ${w.fix}`;
          btn.title = `Send ${w.fix} to the controller (stored in its EEPROM)`;
          btn.addEventListener("click", async () => {
            btn.disabled = true;
            try {
              const r = await api.command(w.fix, "ui");
              if (r.status !== "ok") log(`${w.fix}: ${r.response}`);
            } catch (e) {
              log(`${w.fix}: ${e.message}`);
            } finally {
              btn.disabled = false;
            }
          });
          row.append(btn);
        }
        return row;
      }),
    );
  }

  store.subscribe((kind) => {
    if (kind === "snapshot" || kind === "warnings" || kind === "connection") render();
  });
  render();
}
