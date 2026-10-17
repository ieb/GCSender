// Remappable keyboard shortcuts (stored in localStorage, keyed by KeyboardEvent.code).

import { $ } from "./ui/dom.js";

const KEY = "gcs-keymap";

export const ACTIONS = [
  ["x+", "Jog X+"], ["x-", "Jog X−"], ["y+", "Jog Y+"], ["y-", "Jog Y−"], ["z+", "Jog Z+"], ["z-", "Jog Z−"],
  ["step+", "Larger step"], ["step-", "Smaller step"], ["feed+", "Faster jog feed"], ["feed-", "Slower jog feed"],
  ["stop", "Stop all motion"],
  ["hold", "Feed hold"], ["resume", "Resume (cycle start)"], ["jobPause", "Job: pause / resume"],
  ["viewFit", "View: fit"], ["viewTop", "View: top"], ["viewIso", "View: iso"], ["console", "Go to the console"],
  ...Array.from({ length: 9 }, (_, i) => [`macro${i + 1}`, `User command ${i + 1}`]),
];

export const DEFAULTS = {
  "x+": ["ArrowRight", "Numpad6"],
  "x-": ["ArrowLeft", "Numpad4"],
  "y+": ["ArrowUp", "Numpad8"],
  "y-": ["ArrowDown", "Numpad2"],
  "z+": ["PageUp", "Numpad9"],
  "z-": ["PageDown", "Numpad3"],
  "step+": ["NumpadAdd", "Equal"],
  "step-": ["NumpadSubtract", "Minus"],
  "feed+": ["NumpadMultiply", "BracketRight"],
  "feed-": ["NumpadDivide", "BracketLeft"],
  stop: ["Escape"],
  // Unbound by default: assign in Settings → Keyboard.
  hold: [], resume: [], jobPause: [], viewFit: [], viewTop: [], viewIso: [], console: [],
  ...Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`macro${i + 1}`, []])),
};

// Modules register what an action does; jog.js dispatches key presses.
const handlers = new Map();
export function registerAction(action, fn) {
  handlers.set(action, fn);
}
export function runAction(action) {
  const fn = handlers.get(action);
  if (fn) fn();
  return !!fn;
}
export function bindings() {
  return ACTIONS.map(([action, label]) => [label, (map[action] || []).map(keyLabel)]);
}

export const AXIS_ACTIONS = {
  "x+": [1, 0, 0], "x-": [-1, 0, 0], "y+": [0, 1, 0], "y-": [0, -1, 0], "z+": [0, 0, 1], "z-": [0, 0, -1],
};

let map = load();

function load() {
  try {
    const stored = JSON.parse(localStorage.getItem(KEY) || "null");
    if (stored && typeof stored === "object") return { ...DEFAULTS, ...stored };
  } catch { /* fall through */ }
  return { ...DEFAULTS };
}

function save() {
  localStorage.setItem(KEY, JSON.stringify(map));
}

export function actionFor(code) {
  for (const [action, codes] of Object.entries(map)) if (codes.includes(code)) return action;
  return null;
}

function keyLabel(code) {
  return code.replace(/^Key/, "").replace(/^Digit/, "").replace(/^Arrow/, "Arrow ");
}

// Settings dialog "Keyboard" tab: click an action, press a key to add it.
export function initKeymapEditor() {
  const body = $("#keymap-body");
  const status = $("#keymap-status");
  let capturing = null;

  function render() {
    body.replaceChildren(
      ...ACTIONS.map(([action, label]) => {
        const tr = document.createElement("tr");
        const name = document.createElement("td");
        name.className = "name";
        name.textContent = label;
        const keys = document.createElement("td");
        for (const code of map[action] || []) {
          const k = document.createElement("kbd");
          k.textContent = keyLabel(code);
          k.title = `Click to remove ${keyLabel(code)} from “${label}”`;
          k.addEventListener("click", () => {
            if (action === "stop" && map.stop.length === 1) {
              status.textContent = "Stop needs at least one key.";
              return;
            }
            map[action] = map[action].filter((c) => c !== code);
            save();
            render();
          });
          keys.append(k);
        }
        const tools = document.createElement("td");
        const add = document.createElement("button");
        add.type = "button";
        add.textContent = capturing === action ? "Press a key…" : "Add key";
        add.title = `Assign another key to “${label}”: click, then press the key (Backspace cancels)`;
        add.addEventListener("click", () => {
          capturing = action;
          status.textContent = `Press the key for “${label}” (Backspace cancels).`;
          render();
        });
        tools.append(add);
        tr.append(name, keys, tools);
        return tr;
      }),
    );
  }

  document.addEventListener(
    "keydown",
    (e) => {
      if (!capturing) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.code !== "Backspace") {
        for (const a of Object.keys(map)) map[a] = map[a].filter((c) => c !== e.code); // unique binding
        map[capturing] = [...(map[capturing] || []), e.code];
        save();
        status.textContent = `Assigned ${keyLabel(e.code)}.`;
      } else {
        status.textContent = "";
      }
      capturing = null;
      render();
    },
    true,
  );

  $("#keymap-reset").addEventListener("click", () => {
    map = { ...DEFAULTS };
    save();
    status.textContent = "Defaults restored.";
    render();
  });

  render();
}
