// Orbit camera with Z up: rotate (drag), pan (shift/right drag), zoom (wheel).

import { add, cross, lookAt, multiply, norm, ortho, perspective, scale, sub } from "./math.js";

const FOV = (30 * Math.PI) / 180;
const DEG = Math.PI / 180;

export const VIEWS = {
  top: { yaw: -90 * DEG, pitch: 89.9 * DEG },
  front: { yaw: -90 * DEG, pitch: 0 },
  left: { yaw: 180 * DEG, pitch: 0 },
  iso: { yaw: -135 * DEG, pitch: 35.264 * DEG },
};

export class OrbitCamera {
  constructor() {
    this.target = [50, 50, 0];
    this.distance = 300;
    this.yaw = VIEWS.iso.yaw;
    this.pitch = VIEWS.iso.pitch;
    this.perspective = true;
  }

  eye() {
    const cp = Math.cos(this.pitch);
    return add(this.target, scale([cp * Math.cos(this.yaw), cp * Math.sin(this.yaw), Math.sin(this.pitch)], this.distance));
  }

  basis() {
    const forward = norm(sub(this.target, this.eye()));
    const right = norm(cross(forward, [0, 0, 1]));
    const up = cross(right, forward);
    return { forward, right, up };
  }

  matrix(aspect) {
    const near = Math.max(0.1, this.distance / 1000);
    const far = this.distance * 20 + 1000;
    let proj;
    if (this.perspective) {
      proj = perspective(FOV, aspect, near, far);
    } else {
      const h = this.distance * Math.tan(FOV / 2);
      proj = ortho(-h * aspect, h * aspect, -h, h, -far, far);
    }
    return multiply(proj, lookAt(this.eye(), this.target, [0, 0, 1]));
  }

  setView(name) {
    const v = VIEWS[name];
    if (!v) return;
    this.yaw = v.yaw;
    this.pitch = v.pitch;
  }

  orbit(dx, dy) {
    this.yaw -= dx * 0.008;
    this.pitch = Math.max(-89.9 * DEG, Math.min(89.9 * DEG, this.pitch + dy * 0.008));
  }

  pan(dx, dy, viewportHeight) {
    const { right, up } = this.basis();
    const unitsPerPixel = (2 * this.distance * Math.tan(FOV / 2)) / Math.max(1, viewportHeight);
    this.target = add(this.target, add(scale(right, -dx * unitsPerPixel), scale(up, dy * unitsPerPixel)));
  }

  zoom(delta) {
    this.distance = Math.max(1, Math.min(20000, this.distance * Math.exp(delta * 0.0015)));
  }

  fit(min, max, aspect) {
    this.target = scale(add(min, max), 0.5);
    const radius = Math.max(5, Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2);
    const fov = aspect < 1 ? 2 * Math.atan(Math.tan(FOV / 2) * aspect) : FOV;
    this.distance = (radius / Math.sin(fov / 2)) * 1.05;
  }
}
