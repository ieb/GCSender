// G-code path reader for display (the server is the authority for what is sent).
// Produces segments in program order with their source line, so progress can be
// shown by drawing vertex ranges in different colours.

const WORD = /([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g;
const PLANES = { 17: [0, 1, 2], 18: [2, 0, 1], 19: [1, 2, 0] };

function arcPoints(start, target, vals, mm, plane, cw, tol = 0.02) {
  const [a0, a1, lin] = PLANES[plane];
  const k = (v) => (mm ? v : v * 25.4);
  let off0;
  let off1;
  if (vals.R !== undefined) {
    let r = k(vals.R);
    const x = target[a0] - start[a0];
    const y = target[a1] - start[a1];
    const h2 = 4 * r * r - x * x - y * y;
    if (h2 < 0) return [target];
    let h = -Math.sqrt(h2) / Math.hypot(x, y);
    if (!cw) h = -h;
    if (r < 0) { h = -h; r = -r; }
    off0 = 0.5 * (x - y * h);
    off1 = 0.5 * (y + x * h);
  } else {
    const ijk = ["I", "J", "K"];
    off0 = k(vals[ijk[a0]] || 0);
    off1 = k(vals[ijk[a1]] || 0);
  }
  const c0 = start[a0] + off0;
  const c1 = start[a1] + off1;
  const r0 = -off0;
  const r1 = -off1;
  const rt0 = target[a0] - c0;
  const rt1 = target[a1] - c1;
  const radius = Math.hypot(r0, r1);
  let sweep = Math.atan2(r0 * rt1 - r1 * rt0, r0 * rt0 + r1 * rt1);
  if (cw) { if (sweep >= -1e-9) sweep -= 2 * Math.PI; } else if (sweep <= 1e-9) sweep += 2 * Math.PI;
  if (radius < 1e-9) return [target];
  const theta = tol < radius ? 2 * Math.acos(1 - tol / radius) : Math.PI / 2;
  const n = Math.max(1, Math.min(720, Math.ceil(Math.abs(sweep) / theta)));
  const aStart = Math.atan2(r1, r0);
  const pts = [];
  for (let i = 1; i < n; i++) {
    const a = aStart + (sweep * i) / n;
    const p = [0, 0, 0];
    p[a0] = c0 + radius * Math.cos(a);
    p[a1] = c1 + radius * Math.sin(a);
    p[lin] = start[lin] + ((target[lin] - start[lin]) * i) / n;
    pts.push(p);
  }
  pts.push(target);
  return pts;
}

/**
 * Parse a program. Returns {segments: [{line, a, b, kind}], lineStart: Int32Array(lines + 1), bounds}
 * kind: "rapid" | "feed" | "plunge" (feed moving only in Z).
 */
export function parseProgram(text) {
  const lines = text.split(/\r?\n/);
  const segments = [];
  const lineStart = new Int32Array(lines.length + 1);
  let mm = true;
  let abs = true;
  let plane = 17;
  let motion = 0;
  const pos = [null, null, null];
  const shift = [0, 0, 0];
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (let li = 0; li < lines.length; li++) {
    lineStart[li] = segments.length;
    const code = lines[li].replace(/\([^)]*\)/g, "").split(";")[0].toUpperCase();
    if (!code.trim()) continue;
    const gs = [];
    const vals = {};
    for (const m of code.matchAll(WORD)) {
      const v = Number(m[2]);
      if (m[1] === "G") gs.push(v);
      else vals[m[1]] = v;
    }
    if (gs.includes(20)) mm = false;
    if (gs.includes(21)) mm = true;
    if (gs.includes(90)) abs = true;
    if (gs.includes(91)) abs = false;
    for (const p of [17, 18, 19]) if (gs.includes(p)) plane = p;
    for (const g of gs) if ([0, 1, 2, 3, 80].includes(g) || (g >= 38 && g < 39)) motion = g;
    const axes = {};
    for (const [i, a] of ["X", "Y", "Z"].entries()) if (vals[a] !== undefined) axes[i] = mm ? vals[a] : vals[a] * 25.4;
    const has = Object.keys(axes).length > 0;
    if (gs.includes(92)) {
      for (const i in axes) if (pos[i] !== null) shift[i] = pos[i] - axes[i];
      continue;
    }
    if (gs.some((g) => Math.abs(g - 92.1) < 1e-6)) { shift.fill(0); continue; }
    if (gs.some((g) => [4, 10, 28.1, 30.1].includes(g)) || !has) continue;
    if (gs.some((g) => [28, 30, 53].includes(g)) || (motion >= 38 && motion < 39)) {
      for (const i in axes) pos[i] = null;
      if (gs.includes(28) || gs.includes(30)) pos.fill(null);
      continue;
    }
    if (motion === 80) continue;
    const start = [...pos];
    for (const i in axes) pos[i] = abs ? axes[i] + shift[i] : pos[i] === null ? null : pos[i] + axes[i];
    if (start.some((v) => v === null) || pos.some((v) => v === null)) continue;
    const target = [...pos];
    const pts = motion === 2 || motion === 3 ? arcPoints(start, target, vals, mm, plane, motion === 2) : [target];
    let prev = start;
    for (const p of pts) {
      const kind = motion === 0 ? "rapid" : (Math.abs(p[0] - prev[0]) < 1e-9 && Math.abs(p[1] - prev[1]) < 1e-9 ? "plunge" : "feed");
      segments.push({ line: li, a: prev, b: p, kind });
      for (const q of [prev, p]) for (let i = 0; i < 3; i++) { lo[i] = Math.min(lo[i], q[i]); hi[i] = Math.max(hi[i], q[i]); }
      prev = p;
    }
  }
  lineStart[lines.length] = segments.length;
  return { segments, lineStart, lines: lines.length, bounds: Number.isFinite(lo[0]) ? [lo, hi] : null };
}
