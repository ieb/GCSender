// WebGL 1 line renderer: batches of coloured line segments, drawn on demand.

const VS = `
attribute vec3 aPos;
attribute vec4 aColor;
uniform mat4 uMvp;
uniform vec3 uOffset;
varying vec4 vColor;
void main() {
  vColor = aColor;
  gl_Position = uMvp * vec4(aPos + uOffset, 1.0);
}`;

const FS = `
precision mediump float;
varying vec4 vColor;
void main() { gl_FragColor = vColor; }`;

function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
  return s;
}

// A batch of GL_LINES built from [x,y,z] pairs with an RGBA colour per segment.
export class LineBatch {
  constructor() {
    this.positions = [];
    this.colors = [];
  }
  line(a, b, color) {
    this.positions.push(a[0], a[1], a[2], b[0], b[1], b[2]);
    this.colors.push(...color, ...color);
  }
  polyline(points, color) {
    for (let i = 1; i < points.length; i++) this.line(points[i - 1], points[i], color);
  }
  get count() {
    return this.positions.length / 3;
  }
}

export class Renderer {
  constructor(canvas) {
    const gl = canvas.getContext("webgl", { antialias: true, alpha: true, premultipliedAlpha: false });
    if (!gl) throw new Error("WebGL unavailable");
    this.gl = gl;
    this.canvas = canvas;
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    this.prog = prog;
    this.aPos = gl.getAttribLocation(prog, "aPos");
    this.aColor = gl.getAttribLocation(prog, "aColor");
    this.uMvp = gl.getUniformLocation(prog, "uMvp");
    this.uOffset = gl.getUniformLocation(prog, "uOffset");
    this.buffers = new Map(); // name -> {pos, col, count}
  }

  upload(name, batch) {
    const gl = this.gl;
    let b = this.buffers.get(name);
    if (!b) {
      b = { pos: gl.createBuffer(), col: gl.createBuffer(), count: 0, offset: [0, 0, 0], visible: true };
      this.buffers.set(name, b);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, b.pos);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(batch.positions), gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, b.col);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(batch.colors), gl.DYNAMIC_DRAW);
    b.count = batch.count;
  }

  setOffset(name, offset) {
    const b = this.buffers.get(name);
    if (b) b.offset = offset;
  }

  setVisible(name, visible) {
    const b = this.buffers.get(name);
    if (b) b.visible = visible;
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    return [w, h];
  }

  draw(mvp, order) {
    const gl = this.gl;
    const [w, h] = this.resize();
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(this.prog);
    gl.uniformMatrix4fv(this.uMvp, false, mvp);
    for (const name of order) {
      const b = this.buffers.get(name);
      if (!b || !b.count || !b.visible) continue;
      gl.uniform3fv(this.uOffset, b.offset);
      gl.bindBuffer(gl.ARRAY_BUFFER, b.pos);
      gl.enableVertexAttribArray(this.aPos);
      gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, b.col);
      gl.enableVertexAttribArray(this.aColor);
      gl.vertexAttribPointer(this.aColor, 4, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, b.count);
    }
  }
}
