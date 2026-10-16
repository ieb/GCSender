// Small DOM helpers shared by the UI modules.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function fmt(v, digits = 3) {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) : "—";
}

export function showBanner(text, kind = "warn") {
  const el = $("#banner");
  if (!text) {
    el.hidden = true;
    return;
  }
  el.textContent = text;
  el.className = "banner" + (kind === "error" ? " error" : "");
  el.hidden = false;
}
