// Help dialog, with the current keyboard bindings.

import { bindings } from "../keymap.js";
import { $ } from "./dom.js";

export function initHelp() {
  const dlg = $("#dlg-help");
  $("#open-help").addEventListener("click", () => {
    const rows = bindings().map(([label, keys]) => {
      const tr = document.createElement("tr");
      const a = document.createElement("td");
      a.textContent = label;
      const b = document.createElement("td");
      if (keys.length) {
        for (const k of keys) {
          const kbd = document.createElement("kbd");
          kbd.textContent = k;
          b.append(kbd);
        }
      } else {
        b.textContent = "—";
        b.className = "muted";
      }
      tr.append(a, b);
      return tr;
    });
    $("#help-keys").replaceChildren(...rows);
    dlg.showModal();
  });
  for (const a of dlg.querySelectorAll(".help-toc a")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      dlg.querySelector(a.getAttribute("href"))?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
}
