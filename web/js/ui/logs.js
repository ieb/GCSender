// Logs dialog: application log records and serial transcript downloads.

import { api, transcriptUrl } from "../api.js";
import { $ } from "./dom.js";
import { initTabs } from "./settings.js";

function time(t) {
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

export function initLogs() {
  const dlg = $("#dlg-logs");
  const body = $("#app-log-body");
  const list = $("#transcript-list");
  initTabs(dlg);

  async function loadAppLog() {
    try {
      const { records } = await api.appLog();
      body.replaceChildren(
        ...records.reverse().map((r) => {
          const tr = document.createElement("tr");
          for (const [cls, text] of [["", time(r.t)], [`lvl-${r.level}`, r.level], ["", r.name], ["", r.msg]]) {
            const td = document.createElement("td");
            if (cls) td.className = cls;
            td.textContent = text;
            tr.append(td);
          }
          return tr;
        }),
      );
    } catch (e) {
      body.replaceChildren();
      const tr = document.createElement("tr");
      tr.textContent = e.message;
      body.append(tr);
    }
  }

  async function loadTranscripts() {
    try {
      const { files } = await api.transcripts();
      if (!files.length) {
        const li = document.createElement("li");
        li.textContent = "No transcripts yet.";
        list.replaceChildren(li);
        return;
      }
      list.replaceChildren(
        ...files.map((f) => {
          const li = document.createElement("li");
          const a = document.createElement("a");
          a.href = transcriptUrl(f.date);
          a.download = `serial-${f.date}.log`;
          a.textContent = `${f.date.slice(0, 4)}-${f.date.slice(4, 6)}-${f.date.slice(6)}`;
          li.append(a, ` (${(f.size / 1024).toFixed(1)} KiB)`);
          return li;
        }),
      );
    } catch (e) {
      list.replaceChildren(Object.assign(document.createElement("li"), { textContent: e.message }));
    }
  }

  $("#open-logs").addEventListener("click", () => {
    loadAppLog();
    loadTranscripts();
    dlg.showModal();
  });
  $("#app-log-refresh").addEventListener("click", loadAppLog);
}
