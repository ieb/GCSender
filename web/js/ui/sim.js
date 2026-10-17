// Simulator panel (only when the server runs with --simulate): virtual
// surface for probing, forced probe input, and a readout of the surface under
// the tool. Settings live on the server (retained "sim" event).

import { api } from "../api.js";
import { $, $$, fmt } from "./dom.js";

// Same formula as SurfaceParams.__call__ on the server.
export function surfaceZ(p, x, y) {
  let z = p.base + p.tiltX * x + p.tiltY * y;
  z += p.ampX * Math.sin((2 * Math.PI * x) / p.periodX) + p.ampY * Math.cos((2 * Math.PI * y) / p.periodY);
  if (p.bump) {
    const d2 = (x - p.bumpX) ** 2 + (y - p.bumpY) ** 2;
    z += p.bump * Math.exp(-d2 / (2 * p.bumpRadius ** 2));
  }
  return z;
}

export function initSim(store, log) {
  const panel = $("#panel-sim");
  const form = $("#sim-form");
  const preset = $("#sim-preset");
  const readout = $("#sim-readout");
  const pins = $$("#sim-pin [data-pin]");
  let edited = false;
  const report = (p) => p.catch((e) => log(`simulator: ${e.message}`));

  form.addEventListener("input", () => { edited = true; });

  function values() {
    const out = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      out[el.name] = Number(el.value) / Number(el.dataset.scale || 1);
    }
    return out;
  }

  $("#sim-apply").addEventListener("click", () => report(api.simSurface(values()).then(() => { edited = false; })));
  $("#sim-preset-apply").addEventListener("click", () => {
    if (preset.value) report(api.simSurface({ preset: preset.value }).then(() => { edited = false; }));
  });
  for (const b of pins) {
    b.addEventListener("click", () => {
      const forced = { auto: null, touching: true, open: false }[b.dataset.pin];
      report(api.simProbe(forced));
    });
  }
  $("#sim-goto").addEventListener("click", () => {
    const s = store.state.sim;
    const st = store.state.status;
    if (!s || !s.surface || !st || !st.mpos) return;
    const z = surfaceZ(s.surface, st.mpos[0], st.mpos[1]) + 5;
    report(api.command(`G53 G0 Z${z.toFixed(3)}`, "ui"));
  });

  function updateVisibility() {
    const available = !!(store.state.sim && store.state.sim.surface);
    panel.toggleAttribute("data-unavailable", !available);
    const activeTab = document.querySelector("[data-left-tab].active");
    panel.hidden = !available || !activeTab || activeTab.dataset.leftTab !== "probe";
  }
  function render() {
    updateVisibility();
    const s = store.state.sim;
    if (!s || !s.surface) return;
    if (preset.options.length !== s.presets.length) {
      preset.replaceChildren(...s.presets.map((name) => new Option(name, name)));
    }
    if (!edited) {
      for (const el of form.elements) {
        if (!el.name || document.activeElement === el) continue;
        const v = s.surface[el.name] * Number(el.dataset.scale || 1);
        el.value = String(Number(v.toFixed(4)));
      }
    }
    const mode = s.probeForced === true ? "touching" : s.probeForced === false ? "open" : "auto";
    for (const b of pins) b.classList.toggle("active", b.dataset.pin === mode);
    renderReadout();
  }

  function renderReadout() {
    const s = store.state.sim;
    const st = store.state.status;
    const idle = st && st.state === "Idle" && store.state.connection.state === "connected";
    $("#sim-goto").disabled = !idle;
    if (!s || !s.surface || !st || !st.mpos) {
      readout.textContent = "";
      return;
    }
    const z = surfaceZ(s.surface, st.mpos[0], st.mpos[1]);
    const gap = st.mpos[2] - z;
    readout.textContent = `surface under tool: machine Z ${fmt(z)} · ` + (gap > 0.0005 ? `tool ${fmt(gap)} mm above` : "tool touching");
  }

  store.subscribe((kind) => {
    if (kind === "snapshot" || kind === "sim") render();
    else if (kind === "status" || kind === "connection") renderReadout();
  });
  render();
}
