// Display preferences (this browser only): theme, touch size, layout lock and
// the 3D view's colours and options. Settings → Display edits them.

import { $ } from "./ui/dom.js";

const KEY = "gcs-display";

export const DEFAULTS = {
  theme: "auto",
  layoutLock: false,
  largeTouch: "auto",
  showGrid: true,
  showRapids: true,
  showTrail: true,
  trailLength: 5000,
  toolDiameter: 3,
  toolAngle: 15,
  antialias: true,
  colFeed: "#1a73d9",
  colRapid: "#8c8c8c",
  colPlunge: "#d93333",
  colDone: "#999999",
  colBuffer: "#9940d9",
  colSelected: "#ffbf00",
  colTool: "#f2800d",
  colTrail: "#3359d9",
};

const listeners = new Set();
let prefs = load();

function load() {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) || "{}");
    return { ...DEFAULTS, ...(p && typeof p === "object" ? p : {}) };
  } catch {
    return { ...DEFAULTS };
  }
}

export function display() {
  return prefs;
}

export function onDisplayChange(fn) {
  listeners.add(fn);
}

export function setDisplay(changes) {
  prefs = { ...prefs, ...changes };
  localStorage.setItem(KEY, JSON.stringify(prefs));
  apply();
  for (const fn of listeners) fn(prefs);
}

const DARK = window.matchMedia("(prefers-color-scheme: dark)");

export function isDark() {
  return prefs.theme === "dark" || (prefs.theme !== "light" && DARK.matches);
}
DARK.addEventListener("change", () => { for (const fn of listeners) fn(prefs); });

// "#rrggbb" -> [r, g, b, a] in 0..1
export function rgba(hex, alpha = 1) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return [0.5, 0.5, 0.5, alpha];
  const n = parseInt(m[1], 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, alpha];
}

function apply() {
  const root = document.documentElement;
  if (prefs.theme === "light" || prefs.theme === "dark") root.dataset.theme = prefs.theme;
  else delete root.dataset.theme;
  const touch = prefs.largeTouch === "on" || (prefs.largeTouch !== "off" && window.matchMedia("(pointer: coarse)").matches);
  root.classList.toggle("touch", touch);
  root.classList.toggle("layout-locked", !!prefs.layoutLock);
}

export function initDisplayEditor() {
  const form = $("#display-form");
  function render() {
    for (const el of form.elements) {
      if (!el.name || !(el.name in prefs)) continue;
      if (el.type === "checkbox") el.checked = !!prefs[el.name];
      else el.value = prefs[el.name];
    }
  }
  form.addEventListener("change", (e) => {
    const el = e.target;
    if (!el.name) return;
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.type === "number") {
      v = Number(el.value);
      if (!Number.isFinite(v) || (el.min && v < Number(el.min)) || (el.max && v > Number(el.max))) {
        $("#display-status").textContent = `${el.name}: out of range`;
        render();
        return;
      }
    }
    setDisplay({ [el.name]: v });
    $("#display-status").textContent = el.name === "antialias" ? "Reload the page to apply." : "";
  });
  $("#display-reset").addEventListener("click", () => {
    setDisplay({ ...DEFAULTS });
    render();
    $("#display-status").textContent = "Defaults restored.";
  });
  render();
  apply();
}
