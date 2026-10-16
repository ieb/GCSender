// State panel (positions, machine state, parser state, alarm) and Control panel.

import { api } from "../api.js";
import { $, $$, fmt } from "./dom.js";

function settingValue(store, id) {
  const s = (store.state.grblSettings || []).find((x) => x.id === id);
  return s ? s.value : undefined;
}

function chip(text, on, title) {
  const span = document.createElement("span");
  span.className = "chip" + (on ? " on" : "");
  span.textContent = text;
  if (title) span.title = title;
  return span;
}

export function initMachine(store, log) {
  const badge = $("#machine-state");
  const fw = $("#machine-firmware");
  const extra = $("#machine-extra");
  const modalEl = $("#machine-modal");
  const alarm = $("#machine-alarm");
  const cells = {};
  for (const k of ["wpos", "mpos"]) for (const a of ["x", "y", "z"]) cells[`${k}-${a}`] = $(`#${k}-${a}`);
  const btnHome = $("#btn-home");
  const btnUnlock = $("#btn-unlock");
  const btnCheck = $("#btn-check");
  const btnSleep = $("#btn-sleep");

  const connected = () => store.state.connection.state === "connected";

  function renderStatus() {
    const s = store.state.status;
    const conn = store.state.connection.state;
    let label = "Offline";
    if (conn === "connected" && s) label = s.state + (s.substate != null ? `:${s.substate}` : "");
    else if (conn === "connecting") label = "Connecting";
    else if (conn === "unresponsive") label = "No response";
    badge.textContent = label;
    badge.className = "state-badge" + (conn === "connected" && s ? ` s-${s.state.toLowerCase()}` : "");
    for (const k of ["wpos", "mpos"]) {
      const v = conn !== "disconnected" && s ? s[k] : null;
      ["x", "y", "z"].forEach((a, i) => (cells[`${k}-${a}`].textContent = v ? fmt(v[i]) : "—"));
    }
    const bits = [];
    if (connected() && s) {
      if (s.fs) bits.push(`F ${s.fs[0]}  S ${s.fs[1]}`);
      if (s.ov) bits.push(`Ov ${s.ov.join("/")}%`);
      if (s.buf != null) bits.push(`planner ${s.buf}`);
      if (s.bf) bits.push(`free planner ${s.bf[0]} · rx ${s.bf[1]}`);
      if (s.pins) bits.push(`pins ${s.pins}`);
      if (s.wco && s.wco.some((v) => Math.abs(v) > 1e-9)) bits.push(`WCO ${s.wco.map((v) => fmt(v)).join(", ")}`);
    }
    extra.textContent = bits.join(" · ");
    renderControls();
  }

  function renderModal() {
    const m = store.state.modal;
    if (!m || !connected()) {
      modalEl.replaceChildren();
      return;
    }
    const caps = store.state.caps || {};
    const chips = [
      chip(m.wcs || "G54", true, "Active work coordinate system"),
      chip(m.units === "G20" ? "inch" : "mm", true, `${m.units} units`),
      chip(m.distance === "G91" ? "incremental" : "absolute", m.distance === "G91", `${m.distance} distance mode`),
      chip(m.plane || "G17", false, "Arc plane"),
      chip(m.motion || "", false, "Motion mode"),
      chip(
        { M3: "spindle CW", M4: "spindle CCW" }[m.spindle] || "spindle off",
        m.spindle === "M3" || m.spindle === "M4",
        m.spindle,
      ),
      chip(m.coolant && m.coolant.length ? m.coolant.join(" ") : "coolant off", !!(m.coolant && m.coolant.length)),
      chip(`F${m.feed ?? 0}`, false, caps.liveFeedSpindle ? "Programmed feed" : "Programmed feed (GRBL 0.9 reports no live feed)"),
      chip(`S${m.speed ?? 0}`, false, "Programmed spindle speed"),
      chip(`T${m.tool ?? 0}`, false, "Tool"),
    ];
    if (m.toolLength === "G43.1") chips.push(chip("TLO", true, "Tool length offset active"));
    modalEl.replaceChildren(...chips);
  }

  function renderInfo() {
    const caps = store.state.caps;
    const b = store.state.build;
    let text = caps ? `GRBL ${caps.version || caps.family + ".x"}` : "";
    if (b && b.version) text = `GRBL ${b.version}`;
    fw.textContent = text;
    const a = store.state.alarm;
    alarm.hidden = !a;
    if (a) {
      $("#alarm-title").textContent = `ALARM: ${a.title || a.text}`;
      $("#alarm-help").textContent = a.help || "";
    }
    renderModal();
    renderControls();
  }

  function renderControls() {
    const on = connected();
    const s = store.state.status;
    const state = s ? s.state : null;
    const caps = store.state.caps || {};
    for (const b of $$("#panel-control button")) b.disabled = !on;
    const homing = settingValue(store, 22) === 1;
    btnHome.disabled = !on || !homing;
    btnHome.title = homing ? "Homing cycle ($H)" : "Homing is disabled on this machine ($22=0: no home switches)";
    btnUnlock.classList.toggle("attention", state === "Alarm");
    btnCheck.classList.toggle("active", state === "Check");
    btnCheck.disabled = !on || !(state === "Idle" || state === "Check");
    btnSleep.hidden = !caps.sleep;
  }

  store.subscribe((kind) => {
    if (kind === "status" || kind === "snapshot" || kind === "connection") renderStatus();
    if (["snapshot", "caps", "modal", "alarm", "connection", "build", "grbl.settings"].includes(kind)) renderInfo();
  });

  for (const btn of $$("#panel-control [data-rt]")) {
    btn.addEventListener("click", () => api.realtime(btn.dataset.rt).catch((e) => log(e.message)));
  }
  for (const btn of $$("#panel-control [data-ctl]")) {
    btn.addEventListener("click", () => api.control(btn.dataset.ctl).catch((e) => log(e.message)));
  }
  for (const btn of $$("#panel-control [data-cmd]")) {
    btn.addEventListener("click", () => api.command(btn.dataset.cmd, "ui").catch((e) => log(e.message)));
  }

  renderStatus();
  renderInfo();
}
