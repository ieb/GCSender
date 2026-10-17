// Coordinates panel (WCS select, zeroing, G92, go-to) and Spindle & coolant panel.

import { api } from "../api.js";
import { $, $$, fmt } from "./dom.js";

export function initWcs(store, log) {
  const temp = $("#wcs-temp");
  const offsetsEl = $("#wcs-offsets");
  const report = (p) => p.catch((e) => log(e.message));

  for (const b of $$("#wcs-select [data-wcs]")) {
    b.addEventListener("click", () => report(api.wcsSelect(b.dataset.wcs)));
  }
  for (const b of $$("#panel-wcs [data-zero]")) {
    b.addEventListener("click", () => {
      const axes = Object.fromEntries([...b.dataset.zero].map((a) => [a, 0]));
      report(api.wcsZero(axes, !temp.checked));
    });
  }
  $("#g92-clear").addEventListener("click", () => report(api.clearG92()));
  $("#goto-xy0").addEventListener("click", () => report(api.goto({ x: 0, y: 0 })));
  $("#goto-z0").addEventListener("click", () => report(api.goto({ z: 0 }, false)));
  $("#goto-safe").addEventListener("click", () => {
    const z = (store.state.appSettings || {}).safeZ ?? 5;
    report(api.goto({ z }, false));
  });

  function render() {
    const connected = store.state.connection.state === "connected";
    const idle = connected && store.state.status && store.state.status.state === "Idle";
    const wcs = (store.state.modal && store.state.modal.wcs) || null;
    for (const b of $$("#panel-wcs button")) b.disabled = !idle;
    for (const b of $$("#wcs-select [data-wcs]")) b.classList.toggle("active", b.dataset.wcs === wcs);
    const o = store.state.offsets || {};
    const parts = [];
    if (wcs && Array.isArray(o[wcs])) parts.push(`${wcs} ${o[wcs].map((v) => fmt(v)).join(", ")}`);
    if (Array.isArray(o.G92) && o.G92.some((v) => Math.abs(v) > 1e-9)) parts.push(`G92 ${o.G92.map((v) => fmt(v)).join(", ")}`);
    if (typeof o.TLO === "number" && o.TLO) parts.push(`TLO ${fmt(o.TLO)}`);
    const safeZ = (store.state.appSettings || {}).safeZ;
    if (safeZ != null) parts.push(`safe Z ${safeZ}`);
    offsetsEl.textContent = connected ? parts.join(" · ") : "";
  }

  store.subscribe((kind) => {
    if (["snapshot", "connection", "status", "modal", "offsets", "app.settings"].includes(kind)) render();
  });
  render();
}

export function initSpindle(store, log) {
  const rpm = $("#spindle-rpm");
  const slider = $("#spindle-slider");
  const toggle = $("#spindle-toggle");
  const flood = $("#coolant-flood");
  const mist = $("#coolant-mist");
  let userRpm = null; // value the user typed, until the controller confirms it

  const isOn = () => ["M3", "M4"].includes((store.state.modal || {}).spindle);
  const modalSpeed = () => Number((store.state.modal || {}).speed || 0);

  function limits() {
    const app = store.state.appSettings || {};
    return [Number(app.spindleMin ?? 0), Number(app.spindleMax ?? 1000)];
  }

  function wanted() {
    const [lo, hi] = limits();
    const v = Number(rpm.value);
    return Math.max(lo, Math.min(hi, Number.isFinite(v) && v > 0 ? v : hi));
  }

  function send(on) {
    api.spindle(on, wanted()).then(
      () => { userRpm = null; },
      (e) => log(`spindle: ${e.message}`),
    );
  }

  toggle.addEventListener("click", () => send(!isOn()));
  rpm.addEventListener("input", () => {
    userRpm = Number(rpm.value);
    slider.value = rpm.value;
    renderMismatch();
  });
  slider.addEventListener("input", () => {
    rpm.value = slider.value;
    userRpm = Number(slider.value);
    renderMismatch();
  });
  for (const el of [rpm, slider]) {
    el.addEventListener("change", () => {
      if (isOn()) send(true); // apply the new speed to the running spindle
    });
  }

  function setCoolant(el, key) {
    el.addEventListener("change", () => {
      api.coolant({ [key]: el.checked }).catch((e) => {
        el.checked = !el.checked;
        log(`coolant: ${e.message}`);
      });
    });
  }
  setCoolant(flood, "flood");
  setCoolant(mist, "mist");

  function renderMismatch() {
    rpm.classList.toggle("mismatch", isOn() && Number(rpm.value) !== modalSpeed());
  }

  function render() {
    const connected = store.state.connection.state === "connected";
    const [lo, hi] = limits();
    slider.min = String(lo);
    slider.max = String(hi);
    slider.step = String(Math.max(1, Math.round((hi - lo) / 100)));
    if (userRpm === null) {
      const v = modalSpeed() || Number(rpm.value) || hi;
      rpm.value = String(v);
      slider.value = String(v);
    }
    const on = isOn();
    toggle.textContent = on ? "Off" : "On";
    toggle.classList.toggle("active", on);
    toggle.title = on ? "Stop the spindle (M5)" : "Start the spindle clockwise (M3)";
    const coolant = (store.state.modal || {}).coolant || [];
    flood.checked = coolant.includes("M8");
    mist.checked = coolant.includes("M7");
    for (const el of [rpm, slider, toggle, flood, mist]) el.disabled = !connected;
    renderMismatch();
  }

  store.subscribe((kind) => {
    if (["snapshot", "connection", "modal", "app.settings"].includes(kind)) render();
  });
  render();
}
