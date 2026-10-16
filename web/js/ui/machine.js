// State panel (positions, machine state, modal state, alarm) and Control panel.

import { api } from "../api.js";
import { $, $$, fmt } from "./dom.js";

export function initMachine(store, log) {
  const badge = $("#machine-state");
  const fw = $("#machine-firmware");
  const extra = $("#machine-extra");
  const modal = $("#machine-modal");
  const alarm = $("#machine-alarm");
  const cells = {};
  for (const k of ["wpos", "mpos"]) for (const a of ["x", "y", "z"]) cells[`${k}-${a}`] = $(`#${k}-${a}`);

  function renderStatus() {
    const s = store.state.status;
    const connected = store.state.connection.state === "connected";
    const state = connected && s ? s.state + (s.substate != null ? `:${s.substate}` : "") : connected ? "…" : "Offline";
    badge.textContent = state;
    badge.className = "state-badge" + (connected && s ? ` s-${s.state.toLowerCase()}` : "");
    for (const k of ["wpos", "mpos"]) {
      const v = connected && s ? s[k] : null;
      ["x", "y", "z"].forEach((a, i) => (cells[`${k}-${a}`].textContent = v ? fmt(v[i]) : "—"));
    }
    const bits = [];
    if (connected && s) {
      if (s.fs) bits.push(`F ${s.fs[0]}  S ${s.fs[1]}`);
      if (s.ov) bits.push(`Ov ${s.ov.join("/")}%`);
      if (s.buf != null) bits.push(`planner ${s.buf}`);
      if (s.bf) bits.push(`free planner ${s.bf[0]} · rx ${s.bf[1]}`);
      if (s.pins) bits.push(`pins ${s.pins}`);
    }
    extra.textContent = bits.join(" · ");
  }

  function renderInfo() {
    const caps = store.state.caps;
    fw.textContent = caps ? `GRBL ${caps.version || caps.family + ".x"}` : "";
    modal.textContent = store.state.modal ? store.state.modal.join(" ") : "";
    const a = store.state.alarm;
    alarm.hidden = !a;
    alarm.textContent = a ? `ALARM ${a.text}` : "";
  }

  store.subscribe((kind) => {
    if (kind === "status" || kind === "snapshot" || kind === "connection") renderStatus();
    if (kind === "snapshot" || kind === "caps" || kind === "modal" || kind === "alarm" || kind === "connection") renderInfo();
  });

  for (const btn of $$("#panel-control [data-rt]")) {
    btn.addEventListener("click", () => api.realtime(btn.dataset.rt).catch((e) => log(e.message)));
  }
  for (const btn of $$("#panel-control [data-cmd]")) {
    btn.addEventListener("click", () => api.command(btn.dataset.cmd).catch((e) => log(e.message)));
  }

  renderStatus();
  renderInfo();
}
