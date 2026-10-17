// State panel (positions, machine state, parser state, alarm) and Control panel.

import { api } from "../api.js";
import { $, $$, fmt } from "./dom.js";
import { registerAction } from "../keymap.js";

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
    const wcsNo = { G54: 1, G55: 2, G56: 3, G57: 4, G58: 5, G59: 6 }[m.wcs || "G54"];
    const motion = {
      G0: "G0: rapid moves (as fast as the machine allows)",
      G1: "G1: straight cutting moves at the programmed feed",
      G2: "G2: clockwise arcs", G3: "G3: counter-clockwise arcs",
      "G38.2": "G38.2: probe towards the work", G80: "G80: motion cancelled",
    }[m.motion] || `${m.motion}: motion mode`;
    const chips = [
      chip(m.wcs || "G54", true, `${m.wcs || "G54"}: the active work coordinate system (number ${wcsNo} of G54–G59). Work positions and zeroing refer to its stored zero.`),
      chip(m.units === "G20" ? "inch" : "mm", true, m.units === "G20" ? "G20: coordinates are in inches" : "G21: coordinates are in millimetres"),
      chip(m.distance === "G91" ? "incremental" : "absolute", m.distance === "G91",
        m.distance === "G91" ? "G91: incremental. Coordinates are distances from the current position" : "G90: absolute. Coordinates are positions relative to work zero"),
      chip(m.plane || "G17", false, { G17: "G17: arcs (G2/G3) are in the XY plane", G18: "G18: arcs are in the XZ plane", G19: "G19: arcs are in the YZ plane" }[m.plane || "G17"]),
      chip(m.motion || "", false, `Motion mode, used by lines without a G word. ${motion}`),
      chip(
        { M3: "spindle CW", M4: "spindle CCW" }[m.spindle] || "spindle off",
        m.spindle === "M3" || m.spindle === "M4",
        { M3: "M3: spindle on, clockwise", M4: "M4: spindle on, counter-clockwise" }[m.spindle] || "M5: spindle off",
      ),
      chip(m.coolant && m.coolant.length ? m.coolant.join(" ") : "coolant off", !!(m.coolant && m.coolant.length),
        m.coolant && m.coolant.length ? `${m.coolant.join(" and ")}: coolant on (M7 mist, M8 flood)` : "M9: coolant off"),
      chip(`F${m.feed ?? 0}`, false, `F: the programmed feed rate for G1/G2/G3 moves (${m.units === "G20" ? "in/min" : "mm/min"})` + (caps.liveFeedSpindle ? "" : ". GRBL 0.9 does not report the live feed")),
      chip(`S${m.speed ?? 0}`, false, "S: the programmed spindle speed (on GRBL 0.9, S1000 = full speed by default)"),
      chip(`T${m.tool ?? 0}`, false, "T: the tool number last selected by the program (GRBL only remembers it)"),
    ];
    if (m.toolLength === "G43.1") chips.push(chip("TLO", true, "G43.1: a tool length offset is active (shown in the Coordinates panel)"));
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

  registerAction("hold", () => api.realtime("hold").catch((e) => log(e.message)));
  registerAction("resume", () => api.realtime("resume").catch((e) => log(e.message)));

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
