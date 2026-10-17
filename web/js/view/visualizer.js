// 3D view: work-coordinate grid and axes, jog-limit box, tool model at the
// live position and a trail of where the tool has been.

import { api } from "../api.js";
import { $, $$, fmt } from "../ui/dom.js";
import { OrbitCamera } from "./camera.js";
import { LineBatch, Renderer } from "./gl.js";
import { parseProgram } from "./gcode.js";

const TRAIL_MAX = 5000;
const DARK = window.matchMedia("(prefers-color-scheme: dark)");

function palette() {
  return DARK.matches
    ? { grid: [1, 1, 1, 0.07], grid10: [1, 1, 1, 0.16], bounds: [0.6, 0.7, 0.8, 0.5], tool: [1, 0.6, 0.15, 1], trail: [0.45, 0.65, 1, 0.9] }
    : { grid: [0, 0, 0, 0.07], grid10: [0, 0, 0, 0.18], bounds: [0.3, 0.35, 0.45, 0.55], tool: [0.95, 0.5, 0.05, 1], trail: [0.2, 0.35, 0.85, 0.9] };
}
const NOOP_VIEW = { setHeightmap() {}, setPreview() {}, setProgram() {}, setProgress() {}, setSelected() {} };
// Toolpath colours (Candle-like): feed, rapid, plunge; progress tints done / in GRBL's buffer.
const PATH = {
  feed: [0.1, 0.45, 0.85, 0.95],
  rapid: [0.55, 0.55, 0.55, 0.45],
  plunge: [0.85, 0.2, 0.2, 0.95],
  done: [0.6, 0.6, 0.6, 0.55],
  buffer: [0.6, 0.25, 0.85, 1],
  selected: [1, 0.75, 0, 1],
};

// Blue (low) -> green -> red (high).
function heat(t, alpha = 1) {
  t = Math.max(0, Math.min(1, t));
  const r = Math.max(0, Math.min(1, 2 * t - 0.5));
  const g = 1 - Math.abs(2 * t - 1) * 0.8;
  const b = Math.max(0, Math.min(1, 1.5 - 2 * t));
  return [r, g * 0.85, b, alpha];
}

const AXIS = { x: [0.86, 0.15, 0.15, 1], y: [0.1, 0.65, 0.2, 1], z: [0.15, 0.35, 0.9, 1] };

function toolBatch(color, diameter = 3, angleDeg = 15, length = 25) {
  const b = new LineBatch();
  const r = diameter / 2;
  const coneH = Math.min(length, r / Math.tan(((angleDeg / 2) * Math.PI) / 180));
  const n = 16;
  const ring = (z) => Array.from({ length: n + 1 }, (_, i) => {
    const a = (i / n) * 2 * Math.PI;
    return [r * Math.cos(a), r * Math.sin(a), z];
  });
  const low = ring(coneH);
  const high = ring(length);
  b.polyline(low, color);
  b.polyline(high, color);
  for (let i = 0; i < n; i += 4) {
    b.line([0, 0, 0], low[i], color);
    b.line(low[i], high[i], color);
  }
  return b;
}

export function initVisualizer(store) {
  const canvas = $("#gl");
  const statusEl = $("#vis-status");
  let renderer;
  try {
    renderer = new Renderer(canvas);
  } catch {
    $("#vis-fallback").hidden = false;
    return NOOP_VIEW;
  }
  const cam = new OrbitCamera();
  const trail = []; // machine coordinates
  let program = null; // loaded job: {segments, lineStart, bounds, lines}
  let progress = { acked: -1, sent: -1 };
  let wco = [0, 0, 0];
  let tool = null; // work coordinates
  let fitted = false;
  let frame = 0;
  let staticKey = "";

  function request() {
    if (!frame) frame = requestAnimationFrame(draw);
  }

  function draw() {
    frame = 0;
    const aspect = canvas.clientWidth / Math.max(1, canvas.clientHeight);
    renderer.draw(cam.matrix(aspect), ["grid", "bounds", "hm-surface", "hm-points", "program", "program-sel", "preview", "markers", "axes", "trail", "tool"]);
  }

  function limitsWork() {
    const app = store.state.appSettings || {};
    if (!app.jogLimitsEnabled || !Array.isArray(app.jogLimits)) return null;
    const l = app.jogLimits;
    return [[l[0] - wco[0], l[2] - wco[1], l[4] - wco[2]], [l[1] - wco[0], l[3] - wco[1], l[5] - wco[2]]];
  }

  function extent() {
    const min = [0, 0, 0];
    const max = [100, 100, 0];
    const grow = (p) => { for (let i = 0; i < 3; i++) { min[i] = Math.min(min[i], p[i]); max[i] = Math.max(max[i], p[i]); } };
    if (tool) grow(tool);
    for (const m of trail) grow([m[0] - wco[0], m[1] - wco[1], m[2] - wco[2]]);
    const lim = limitsWork();
    if (lim) { grow(lim[0]); grow(lim[1]); }
    if (hmBox) { grow(hmBox[0]); grow(hmBox[1]); }
    if (previewBox) { grow(previewBox[0]); grow(previewBox[1]); }
    if (program?.bounds) { grow(program.bounds[0]); grow(program.bounds[1]); }
    return { min, max };
  }

  function rebuildStatic() {
    const pal = palette();
    const { min, max } = extent();
    const g0 = [Math.floor(Math.min(min[0], 0) / 10) * 10 - 10, Math.floor(Math.min(min[1], 0) / 10) * 10 - 10];
    const g1 = [Math.ceil(Math.max(max[0], 0) / 10) * 10 + 10, Math.ceil(Math.max(max[1], 0) / 10) * 10 + 10];
    const key = [g0, g1, limitsWork(), DARK.matches].join("|");
    if (key === staticKey) return;
    staticKey = key;
    const grid = new LineBatch();
    for (let x = g0[0]; x <= g1[0]; x += 10) grid.line([x, g0[1], 0], [x, g1[1], 0], x % 50 === 0 ? pal.grid10 : pal.grid);
    for (let y = g0[1]; y <= g1[1]; y += 10) grid.line([g0[0], y, 0], [g1[0], y, 0], y % 50 === 0 ? pal.grid10 : pal.grid);
    renderer.upload("grid", grid);

    const axes = new LineBatch();
    axes.line([0, 0, 0], [15, 0, 0], AXIS.x);
    axes.line([0, 0, 0], [0, 15, 0], AXIS.y);
    axes.line([0, 0, 0], [0, 0, 15], AXIS.z);
    renderer.upload("axes", axes);

    const bounds = new LineBatch();
    const lim = limitsWork();
    if (lim) {
      const [a, b] = lim;
      const c = (i) => [i & 1 ? b[0] : a[0], i & 2 ? b[1] : a[1], i & 4 ? b[2] : a[2]];
      for (const [i, j] of [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]]) {
        bounds.line(c(i), c(j), pal.bounds);
      }
    }
    renderer.upload("bounds", bounds);
  }

  function rebuildTrail() {
    const b = new LineBatch();
    const pts = trail.map((m) => [m[0] - wco[0], m[1] - wco[1], m[2] - wco[2]]);
    b.polyline(pts, palette().trail);
    renderer.upload("trail", b);
  }

  function fit() {
    const { min, max } = extent();
    cam.fit(min, [max[0], max[1], Math.max(max[2], 25)], canvas.clientWidth / Math.max(1, canvas.clientHeight));
    request();
  }

  // -- heightmap and preview ----------------------------------------------------

  let hmBox = null;
  let previewBox = null;
  let surfaceDigest = null;

  function setHeightmap(m) {
    const pts = new LineBatch();
    if (!m) {
      hmBox = null;
      renderer.upload("hm-points", pts);
      renderer.upload("hm-surface", pts);
      request();
      return;
    }
    const p = m.params;
    hmBox = [[p.x, p.y, Math.min(0, m.min ?? 0)], [p.x + p.w, p.y + p.h, Math.max(0, m.max ?? 0)]];
    const lo = m.min ?? 0;
    const span = Math.max(1e-6, (m.max ?? 0) - lo);
    const border = DARK.matches ? [1, 1, 1, 0.45] : [0, 0, 0, 0.45];
    pts.polyline([[p.x, p.y, 0], [p.x + p.w, p.y, 0], [p.x + p.w, p.y + p.h, 0], [p.x, p.y + p.h, 0], [p.x, p.y, 0]], border);
    const s = Math.max(0.4, Math.min(p.w, p.h) / 60);
    for (let i = 0; i < p.ny; i++) {
      for (let j = 0; j < p.nx; j++) {
        const x = p.x + (p.w * j) / (p.nx - 1);
        const y = p.y + (p.h * i) / (p.ny - 1);
        const v = m.values[i][j];
        const z = v ?? 0;
        const c = v === null ? [0.55, 0.55, 0.55, 0.8] : heat((v - lo) / span);
        pts.line([x - s, y, z], [x + s, y, z], c);
        pts.line([x, y - s, z], [x, y + s, z], c);
      }
    }
    // reference origin: a small ring
    const ring = [];
    for (let k = 0; k <= 12; k++) ring.push([p.originX + s * 1.5 * Math.cos(k * Math.PI / 6), p.originY + s * 1.5 * Math.sin(k * Math.PI / 6), 0]);
    pts.polyline(ring, [0.86, 0.15, 0.15, 1]);
    renderer.upload("hm-points", pts);
    if (m.complete && m.digest !== surfaceDigest) {
      surfaceDigest = m.digest;
      api.heightmapInterpolated().then((r) => {
        if (r.digest !== surfaceDigest) return;
        const g = r.grid;
        const ny = g.length;
        const nx = g[0].length;
        const b = new LineBatch();
        const at = (i, j) => [p.x + (p.w * j) / (nx - 1), p.y + (p.h * i) / (ny - 1), g[i][j]];
        const col = (z) => heat((z - lo) / span, 0.75);
        for (let i = 0; i < ny; i++) for (let j = 1; j < nx; j++) b.line(at(i, j - 1), at(i, j), col(g[i][j]));
        for (let j = 0; j < nx; j++) for (let i = 1; i < ny; i++) b.line(at(i - 1, j), at(i, j), col(g[i][j]));
        renderer.upload("hm-surface", b);
        request();
      }, () => {});
    } else if (!m.complete) {
      surfaceDigest = null;
      renderer.upload("hm-surface", new LineBatch());
    }
    staticKey = "";
    rebuildStatic();
    request();
  }

  function setPreview(text) {
    const b = new LineBatch();
    previewBox = null;
    if (text) {
      const parsed = parseProgram(text);
      previewBox = parsed.bounds;
      const rc = DARK.matches ? [0.7, 0.7, 0.7, 0.35] : [0.4, 0.4, 0.4, 0.35];
      for (const sg of parsed.segments) b.line(sg.a, sg.b, sg.kind === "rapid" ? rc : [0.1, 0.55, 0.3, 0.95]);
    }
    renderer.upload("preview", b);
    staticKey = "";
    rebuildStatic();
    if (text) fit();
    request();
  }

  renderer.upload("tool", toolBatch(palette().tool));
  renderer.setVisible("tool", false);
  rebuildStatic();

  // -- live data ---------------------------------------------------------------

  let trailDirty = false;
  function onStatus() {
    const s = store.state.status;
    const connected = store.state.connection.state === "connected";
    if (!s || !connected || !s.mpos) {
      renderer.setVisible("tool", false);
      statusEl.textContent = "";
      request();
      return;
    }
    const newWco = s.wco || (s.wpos ? s.mpos.map((m, i) => m - s.wpos[i]) : [0, 0, 0]);
    const wcoChanged = newWco.some((v, i) => Math.abs(v - wco[i]) > 1e-6);
    wco = newWco;
    tool = s.mpos.map((m, i) => m - wco[i]);
    renderer.setOffset("tool", tool);
    renderer.setVisible("tool", true);
    const last = trail[trail.length - 1];
    if (!last || Math.hypot(last[0] - s.mpos[0], last[1] - s.mpos[1], last[2] - s.mpos[2]) > 0.005) {
      trail.push([...s.mpos]);
      if (trail.length > TRAIL_MAX) trail.splice(0, trail.length - TRAIL_MAX);
      trailDirty = true;
    }
    if (trailDirty || wcoChanged) {
      trailDirty = false;
      rebuildTrail();
      rebuildStatic();
    }
    const m = store.state.modal || {};
    statusEl.textContent =
      `${s.state}${m.wcs ? "  " + m.wcs : ""}\n` +
      `X ${fmt(tool[0])}  Y ${fmt(tool[1])}  Z ${fmt(tool[2])}`;
    if (!fitted) {
      fitted = true;
      fit();
    }
    request();
  }

  store.subscribe((kind) => {
    if (kind === "status" || kind === "snapshot" || kind === "connection") onStatus();
    if (kind === "app.settings" || kind === "snapshot") {
      rebuildStatic();
      request();
    }
  });

  // -- interaction ---------------------------------------------------------------

  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 1 || e.button === 2 };
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    if (drag.pan) cam.pan(dx, dy, canvas.clientHeight);
    else cam.orbit(dx, dy);
    request();
  });
  const endDrag = () => { drag = null; };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    cam.zoom(e.deltaY);
    request();
  }, { passive: false });

  for (const b of $$("[data-view]")) {
    b.addEventListener("click", () => {
      cam.setView(b.dataset.view);
      fit();
    });
  }
  $("#view-fit").addEventListener("click", fit);
  const persp = $("#view-persp");
  persp.classList.add("active");
  persp.addEventListener("click", () => {
    cam.perspective = !cam.perspective;
    persp.classList.toggle("active", cam.perspective);
    request();
  });
  $("#view-trail-clear").addEventListener("click", () => {
    trail.length = 0;
    rebuildTrail();
    request();
  });

  new ResizeObserver(request).observe(canvas);
  DARK.addEventListener("change", () => {
    staticKey = "";
    renderer.upload("tool", toolBatch(palette().tool));
    rebuildStatic();
    rebuildTrail();
    request();
  });
  request();
  // -- the loaded program, its progress and the selected line ------------------

  function setProgram(text) {
    const b = new LineBatch();
    const m = new LineBatch();
    program = null;
    if (text) {
      program = parseProgram(text);
      for (const sg of program.segments) b.line(sg.a, sg.b, PATH[sg.kind]);
      const segs = program.segments;
      if (segs.length) {
        const s = 1.2;
        const first = segs[0].a;
        const last = segs[segs.length - 1].b;
        const ring = (c, col) => {
          const pts = [];
          for (let k = 0; k <= 16; k++) pts.push([c[0] + s * Math.cos(k * Math.PI / 8), c[1] + s * Math.sin(k * Math.PI / 8), c[2]]);
          m.polyline(pts, col);
        };
        ring(first, [0.1, 0.7, 0.2, 1]); // start: green ring
        ring(last, [0.86, 0.15, 0.15, 1]); // end: red ring
      }
    }
    renderer.upload("program", b);
    renderer.upload("markers", m);
    renderer.upload("program-sel", new LineBatch());
    applyProgress();
    staticKey = "";
    rebuildStatic();
    if (text) fit();
    request();
  }

  // Vertex index where line i starts (2 vertices per segment).
  const vAt = (i) => (program ? 2 * program.lineStart[Math.max(0, Math.min(program.lines, i))] : 0);

  function applyProgress() {
    if (!program) return;
    const total = 2 * program.segments.length;
    const done = vAt(progress.acked + 1);
    const sent = Math.max(done, vAt(progress.sent + 1));
    renderer.setRanges("program", [
      { first: 0, count: done, tint: PATH.done },
      { first: done, count: sent - done, tint: PATH.buffer },
      { first: sent, count: total - sent, tint: null },
    ]);
  }

  function setProgress(acked, sent) {
    if (progress.acked === acked && progress.sent === sent) return;
    progress = { acked, sent };
    applyProgress();
    request();
  }

  function setSelected(line) {
    const b = new LineBatch();
    if (program && line !== null && line >= 0) {
      for (let k = program.lineStart[line]; k < program.lineStart[line + 1]; k++) {
        const sg = program.segments[k];
        b.line(sg.a, sg.b, PATH.selected);
      }
    }
    renderer.upload("program-sel", b);
    request();
  }

  return { setHeightmap, setPreview, setProgram, setProgress, setSelected };
}
