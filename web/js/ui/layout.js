// Layout: collapsible left-column panels and draggable dividers between the
// columns and between the 3D view and the program/console. Kept per browser
// in localStorage; "Lock the layout" (Settings → Display) disables changes.

import { $, $$ } from "./dom.js";

const KEY = "gcs-layout";
const INTERACTIVE = "button, input, select, label, a, textarea, output";

function load() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "{}");
    return { collapsed: {}, leftW: null, bottomH: null, ...(v && typeof v === "object" ? v : {}) };
  } catch {
    return { collapsed: {}, leftW: null, bottomH: null };
  }
}

export function initLayout() {
  const state = load();
  const save = () => localStorage.setItem(KEY, JSON.stringify(state));
  const locked = () => document.documentElement.classList.contains("layout-locked");
  const layout = $(".layout");
  const main = $(".col-main");

  // -- collapsible panels ------------------------------------------------------
  for (const panel of $$(".col-left > .panel")) {
    const h2 = panel.querySelector(":scope > h2");
    if (!h2 || !panel.id) continue;
    panel.classList.add("collapsible");
    if (state.collapsed[panel.id]) panel.classList.add("collapsed");
    h2.addEventListener("click", (e) => {
      if (e.target.closest(INTERACTIVE) || locked()) return;
      const on = panel.classList.toggle("collapsed");
      if (on) state.collapsed[panel.id] = true;
      else delete state.collapsed[panel.id];
      save();
    });
    h2.title = h2.title || "Click to collapse or expand";
  }

  // -- dividers ----------------------------------------------------------------
  // Saved sizes are clamped to the current window, so a size dragged on a large
  // screen never squeezes the 3D view or the program on a smaller one.
  const applySizes = () => {
    const w = layout.clientWidth;
    if (state.leftW) layout.style.setProperty("--left-w", `${Math.min(state.leftW, Math.round(w * 0.55))}px`);
    else layout.style.removeProperty("--left-w");
    const h = main.clientHeight;
    const tabs = main.querySelector(".bottom-tabs");
    const room = h - 260 /* 3D view minimum */ - (tabs ? tabs.offsetHeight : 32) - 36 /* gaps + divider */;
    if (state.bottomH && h > 0) {
      let px = Math.max(140, Math.min(state.bottomH, room));
      main.style.setProperty("--bottom-h", `${px}px`);
      // Measure: if the open bottom panel still pokes out of the column, shrink by that much.
      const panel = main.querySelector(".bottom-panel:not([hidden])");
      const over = panel ? Math.ceil(panel.getBoundingClientRect().bottom - main.getBoundingClientRect().bottom) : 0;
      if (over > 0 && getComputedStyle(layout).gridTemplateColumns.split(" ").length > 1) {
        px = Math.max(140, px - over);
        main.style.setProperty("--bottom-h", `${px}px`);
      }
    } else main.style.removeProperty("--bottom-h");
  };
  // Re-apply whenever the space changes (window size, the top bar wrapping, …).
  let resizeQueued = false;
  new ResizeObserver(() => {
    if (resizeQueued) return;
    resizeQueued = true;
    requestAnimationFrame(() => { resizeQueued = false; applySizes(); });
  }).observe(layout);

  function drag(el, onMove) {
    el.addEventListener("pointerdown", (e) => {
      if (locked() || e.button !== 0) return;
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      const move = (ev) => { onMove(ev); applySizes(); };
      const up = () => {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        el.removeEventListener("pointercancel", up);
        el.classList.remove("dragging");
        save();
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
    });
  }

  const left = $("#split-left");
  drag(left, (e) => {
    const r = layout.getBoundingClientRect();
    state.leftW = Math.round(Math.max(220, Math.min(r.width * 0.55, e.clientX - r.left - 10)));
  });
  left.addEventListener("dblclick", () => { if (!locked()) { state.leftW = null; applySizes(); save(); } });

  const bottom = $("#split-bottom");
  drag(bottom, (e) => {
    const r = main.getBoundingClientRect();
    state.bottomH = Math.round(Math.max(140, Math.min(r.height - 180, r.bottom - e.clientY - 30)));
  });
  bottom.addEventListener("dblclick", () => { if (!locked()) { state.bottomH = null; applySizes(); save(); } });

  applySizes();
}
