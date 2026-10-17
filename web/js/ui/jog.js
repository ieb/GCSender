// Jog panel: step jogging only.
//
// Every click or key press moves exactly the selected step. Deliberate presses
// may queue up behind each other, but holding a key or mouse button is always
// exactly one step: keyboard auto-repeat is ignored, and a key counts again
// only after it has been released.

import { api } from "../api.js";
import { actionFor, AXIS_ACTIONS } from "../keymap.js";
import { $, $$ } from "./dom.js";

const KB_KEY = "gcs-jog-keyboard";
const STEP_KEY = "gcs-jog-step";
const FEED_KEY = "gcs-jog-feed";
const DEFAULT_STEPS = [0.01, 0.05, 0.1, 0.5, 1, 5, 10];

function parseList(text, fallback) {
  const nums = String(text || "")
    .split(",")
    .map((x) => Number(x.trim()))
    .filter((n) => Number.isFinite(n) && n > 0);
  return nums.length ? nums : fallback;
}

function isTyping(el) {
  return el && (el.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName));
}

export function initJog(store, log) {
  const stepsEl = $("#jog-steps");
  const feedSel = $("#jog-feed");
  const kb = $("#jog-keyboard");
  const info = $("#jog-info");
  const buttons = $$("#panel-jog [data-jog]");
  let steps = DEFAULT_STEPS;
  let step = Number(localStorage.getItem(STEP_KEY)) || 1;

  kb.checked = localStorage.getItem(KB_KEY) === "1";
  kb.addEventListener("change", () => localStorage.setItem(KB_KEY, kb.checked ? "1" : "0"));

  // -- step selector (segmented buttons) and feed list -----------------------

  function renderSteps() {
    if (!steps.includes(step)) step = steps.includes(1) ? 1 : steps[0];
    stepsEl.replaceChildren(
      ...steps.map((v) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = String(v);
        b.title = `${v} mm per click or key press`;
        b.classList.toggle("active", v === step);
        b.addEventListener("click", () => setStep(v));
        return b;
      }),
    );
    renderInfo();
  }

  function setStep(v) {
    step = v;
    localStorage.setItem(STEP_KEY, String(v));
    for (const b of stepsEl.children) b.classList.toggle("active", Number(b.textContent) === v);
    renderInfo();
  }

  function renderOptions() {
    const app = store.state.appSettings || {};
    steps = parseList(app.jogSteps, DEFAULT_STEPS);
    const feeds = parseList(app.jogFeeds, [100, 300, 1000]);
    const wantFeed = localStorage.getItem(FEED_KEY) || feedSel.value || "300";
    feedSel.replaceChildren(...feeds.map((v) => new Option(`${v}`, String(v))));
    feedSel.value = [...feedSel.options].some((o) => o.value === wantFeed) ? wantFeed : String(feeds[0]);
    renderSteps();
  }
  feedSel.addEventListener("change", () => {
    localStorage.setItem(FEED_KEY, feedSel.value);
    renderInfo();
  });

  function cycleStep(dir) {
    const i = Math.min(steps.length - 1, Math.max(0, steps.indexOf(step) + dir));
    setStep(steps[i]);
  }
  function cycleFeed(dir) {
    const i = Math.min(feedSel.options.length - 1, Math.max(0, feedSel.selectedIndex + dir));
    feedSel.selectedIndex = i;
    feedSel.dispatchEvent(new Event("change"));
  }

  let infoTimer = 0;
  function renderInfo(message) {
    clearTimeout(infoTimer);
    info.textContent = message || `${step} mm per click or key press at ${feedSel.value} mm/min`;
    info.classList.toggle("warn", !!message);
    if (message) infoTimer = setTimeout(() => renderInfo(), 3000);
  }

  // -- jogging ---------------------------------------------------------------

  function jog(dir) {
    api.jogStep(dir.map((c) => c * step), Number(feedSel.value)).then(
      () => renderInfo(),
      (e) => {
        renderInfo(e.message);
        log(`jog: ${e.message}`);
      },
    );
  }

  function stopAll() {
    api.stop().catch((e) => log(`stop: ${e.message}`));
  }

  for (const b of buttons) {
    const dir = b.dataset.jog.split(",").map(Number);
    // "click" fires once per press-and-release, however long the button is held.
    b.addEventListener("click", () => jog(dir));
  }
  $("#jog-stop").addEventListener("click", stopAll);

  const held = new Set(); // keys pressed and not yet released
  window.addEventListener("keyup", (e) => held.delete(e.code));
  window.addEventListener("blur", () => held.clear());

  window.addEventListener("keydown", (e) => {
    if (document.querySelector("dialog[open]")) return;
    const action = actionFor(e.code);
    if (!action) return;
    const connected = store.state.connection.state === "connected";
    if (action === "stop") {
      // The stop key works everywhere (even while typing), except in dialogs.
      if (connected) {
        e.preventDefault();
        stopAll();
      }
      return;
    }
    if (!kb.checked || !connected || isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    // A held key is one step: ignore auto-repeat, including platforms/remote
    // desktops that send repeats without the repeat flag.
    if (e.repeat || held.has(e.code)) return;
    held.add(e.code);
    if (AXIS_ACTIONS[action]) jog(AXIS_ACTIONS[action]);
    else if (action === "step+") cycleStep(1);
    else if (action === "step-") cycleStep(-1);
    else if (action === "feed+") cycleFeed(1);
    else if (action === "feed-") cycleFeed(-1);
  });

  // -- state -------------------------------------------------------------------

  function renderEnabled() {
    const on = store.state.connection.state === "connected";
    for (const b of buttons) b.disabled = !on;
    $("#jog-stop").disabled = !on;
  }

  store.subscribe((kind) => {
    if (kind === "snapshot" || kind === "app.settings") renderOptions();
    if (kind === "snapshot" || kind === "connection") renderEnabled();
  });
  renderOptions();
  renderEnabled();
}
