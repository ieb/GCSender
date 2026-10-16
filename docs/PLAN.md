# GCSender — Plan (server-first)

A Candle-style G-code sender for a **C3018** desktop mill (ATmega328P + CH340 board) running **GRBL 0.9j**,
with **GRBL 1.1** supported from the start. Feature reference: Denvi/Candle (master @ c04e6f2, v11.x), plus selected bCNC features.

Focus areas, in priority order:
1. Connecting and showing machine state.
2. Jogging, including from the keyboard.
3. Probing (Z-zero, heightmap).
4. Heightmaps: create, save, load and apply.
5. Running G-code with visual progress.
6. Surviving an accidental page reload.

## 0. Decisions

| Topic | Decision |
|---|---|
| Architecture | A **local Python HTTP server** owns the serial port, the controller protocol, jobs, probing and the heightmap. The **SPA** is a pure HTML/CSS/JS UI that talks to it over HTTP and uses **SSE** for the event stream. |
| Web Serial | **Dropped.** There is one sender implementation, on the server. |
| Firmware | GRBL 0.9j (primary) and 1.1, through a capability layer (§2.1). |
| Server dependencies | Python ≥ 3.11 standard library plus **`pyserial`** (runtime). `pytest` as the only dev dependency (mainstream; can be swapped for stdlib `unittest` if you prefer). Managed with uv. `pyproject.toml` sets `[tool.uv] no-build = true` and `exclude-newer = "7 days"`. `uv.lock` is committed; install with `uv sync --locked`. |
| SPA dependencies | **None.** Native ES modules (they work now because the server serves the SPA), raw WebGL, no bundler, no npm. |
| Run | `uv run gcsender [--port /dev/tty.usbserial-xxx] [--http 127.0.0.1:8080]`, then open the printed URL. |
| Units / axes | Metric UI, XYZ only. |
| Licence | Candle is GPL-3.0. We re-implement behaviour and the `candle map v3` heightmap format; no code is copied. |

**Why server-first:**
- **Reloads are harmless.** The server keeps streaming, and the SPA re-attaches via SSE replay or a snapshot.
- **Serial reset stops mattering.** The port is opened once per server run, so the DTR auto-reset only happens at server start.
- **More browsers work.** No Web Serial means Firefox, Safari or a tablet can be used.
- **One source of truth** for what is actually sent, including the heightmap transform.

---

## 1. System overview

```
┌──────────── Browser (any) ─────────────┐        ┌──────────────── gcsender (Python) ─────────────────┐
│ SPA: panels, keyboard jog, console,    │  HTTP  │ http: static SPA + REST API + SSE                  │
│ WebGL visualizer, heightmap editor,    │◄──────►│ events: bus + ring buffer (Last-Event-ID replay)   │
│ G-code parser (display only)           │  SSE   │ controller: protocol, capabilities, machine model  │
└────────────────────────────────────────┘        │ streamer: char-counting, job runner, M6, overrides │
                                                  │ jog / stop / probe / heightmap / transform         │
                                                  │ store: settings, jobs, maps, journal (disk)        │
                                                  │ transport: pyserial  |  simulator (0.9j / 1.1f)    │
                                                  └──────────────────────┬─────────────────────────────┘
                                                                         │ USB serial (CH340)
                                                                    GRBL 0.9j / 1.1  (C3018)
```

---

## 2. GRBL 0.9j vs 1.1 (verified against the grbl/grbl 0.9j source)

| Area | GRBL 1.1 | GRBL 0.9j |
|---|---|---|
| Banner | `Grbl 1.1f ['$' for help]` | `Grbl 0.9j ['$' for help]` |
| Status | `<Idle\|MPos:..\|FS:..\|WCO:..\|Ov:..\|Pn:..>` | `<Idle,MPos:x,y,z,WPos:x,y,z[,Buf:n][,RX:n][,Lim:xyz]>`. Requires `$10` bits 0 and 1 (`$10=3`; `$10=7` adds `Buf:`) |
| States | Idle, Run, Hold:0/1, Jog, Alarm, Door:n, Check, Home, Sleep | Idle, Run, Hold, Home, Alarm, Check, Door. No Jog, no Sleep, no hold sub-state |
| Live feed/spindle, probe pin | `FS:`, `Pn:P` | Not reported |
| Jogging | `$J=` plus cancel `0x85` | None. Use `G91` moves; stop with hold → reset (§5.4) |
| Overrides | Real-time `0x90–0x9D` | None. Software `F` rewrite only, lagging by the planner depth (up to 17 blocks) |
| Sleep / door / coolant toggles | `$SLP`, `0x84`, `0xA0/0xA1` | none / `@` / `M7`–`M9` only |
| Errors / alarms | `error:20`, `ALARM:1` | Text: `error: Expected command letter`, `ALARM: Hard limit` |
| `$G` | `[GC:G0 G54 …]` | `[G0 G54 G17 G21 G90 G94 M0 M5 M9 T0 F0. S0.]` |
| `$#` / PRB | `[PRB:x,y,z:1]`, machine coordinates | Same |
| G-codes | G0–3, G4, G10 L2/L20, G17–19, G20/21, G28/30(.1), G38.2–.5, G40, G43.1/G49, G53–59, G61, G80, G90/91, G91.1, G92(.1), G93/94, M0–2, M30, M3–5, M7–9 | Same set |
| Buffers | RX 128 B, line 80 chars, planner 16 blocks (15 usable) | RX 128 B, line 80 chars, planner 18 blocks (17 usable; 328P default without line numbers) |
| Reset in motion | Alarm | Alarm. **However**, after a feed hold has fully stopped, a reset raises no alarm and keeps the position, but G92 and modal state are cleared |

### 2.1 Capability layer
The version is detected from the banner, or from `$I` when re-attaching with no banner. It produces flags that all logic
and the SPA consult; nothing branches on the version string. Feature availability is reported in `/api/state`, so the
SPA shows or hides controls automatically.

| Flag | 0.9j | 1.1 |
|---|---|---|
| `statusFormat` | comma | pipe; WPos = MPos − cached WCO |
| `jog` | `g91` (hold/reset stop) | `$J` (`0x85` cancel) |
| `overrides` | `software-feed` | `realtime` (feed/rapid/spindle) |
| `probePin`, `liveFeedSpindle`, `holdSubstate` | false | true |
| `sleep`, `door`, `coolantToggle` | false / `@` / M-codes | `$SLP` / `0x84` / `0xA0`, `0xA1` |
| `errorFormat`, `parserStateFormat` | text, bare `[…]` | numeric, `[GC:…]` |
| `laserMode` | false | `$32` |

**C3018 assumptions to confirm** (from your `$I` and `$$` output):
- No limit or home switches (`$22=0`), so machine coordinates are relative to power-on.
- Probe on A5.
- Spindle `M3/M5`, with or without PWM `S`.
- Travel about 300 × 180 × 45 mm.
- CH340 auto-reset when the port opens.

---

## 3. Feature inventory: Candle → GRBL 0.9 → delivery

Legend:
- **0.9:** ✅ = as is, 🟡 = with a workaround, ❌ = impossible on 0.9. "1.1" means it is enabled automatically on 1.1.
- **Phase:** P0–P6 (§8), *Later* = backlog, ✗ = out of scope.

### 3.1 Connection and state
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Serial port and baud selection, connect/disconnect | ✅ | Server: `GET /api/ports`, `POST /api/connect`. Remembers the last port; optional auto-connect at start | P1 |
| Telnet / WebSocket connection types | — | Not needed: the SPA reaches the server over HTTP, optionally from the LAN (§6) | ✗ |
| Status query period | ✅ | Server poll thread, 100–250 ms | P1 |
| Reset on connection (option) | ✅ | Default off; the board resets on open anyway | P1 |
| State panel: WPos/MPos, coloured state | ✅ | Pushed over SSE (`status`) | P1 |
| Parser state (`$G`), spindle/coolant flags overlay | ✅ | Refreshed by the server on Idle transitions | P1 |
| Live feed/spindle | 🟡 | Programmed F/S on 0.9; `FS:` on 1.1 | P1 |
| Console: send, response, history, autocomplete, show UI/program commands | ✅ | `POST /api/command`; all controller I/O is mirrored to SSE `console` | P1 |
| GRBL settings view/edit with descriptions; `$10` check | ✅ | `GET/PUT /api/grbl/settings` | P1 |
| Error/alarm help text | ✅ | Tables for both firmwares | P1 |
| Log window | ✅ | Server log plus a raw serial transcript file; viewable and downloadable | P1 |

### 3.2 Control panel
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Home `$H` | ✅ (needs switches) | Disabled unless `$22=1` | P1 |
| Hold / Resume `!` `~` | ✅ | `POST /api/realtime` | P1 |
| Soft reset, then restore offsets and modal state | ✅ | Server captures `$#`/`$G` beforehand and restores G92 and modal state | P1 |
| Unlock `$X`, Check `$C` | ✅ | | P1 |
| Sleep | ❌ | 1.1 only | P1 (1.1) |
| Door | 🟡 | `@` / `0x84` | P6 |
| Flood/mist | 🟡 | `M8`/`M7`/`M9`; real-time toggles on 1.1 | P2 |

### 3.3 Spindle and overrides
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Spindle on/off, speed field and slider, min/max | ✅ | `M3 S…`/`M5`, in Idle or Hold | P2 |
| Change S during a job | 🟡 | Server injects into the stream | P4 |
| Feed override | 🟡 | 0.9: the server rewrites `F` as lines leave (UI notes the lag). 1.1: real-time | P4 |
| Rapid and spindle override | ❌ | 1.1 real-time only (0.9 S rewrite: Later) | P4 (1.1) |

### 3.4 Jogging
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| X±/Y±/Z± buttons, Stop | 🟡 | `POST /api/jog/step`, `/api/jog/stop` (§5.4) | P2 |
| Step and feed lists (editable), continuous mode | 🟡 | Continuous: `jog/start`, `jog/heartbeat`, `jog/stop` with a dead-man timeout | P2 |
| Keyboard control and remappable keys; diagonals | ✅ | SPA keymap. `blur`/`visibilitychange` immediately sends stop | P2 |
| Bounds-limited continuous travel | 🟡 | Configured machine bounds (no homing on the C3018) | P2 |

### 3.5 Coordinate systems
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Select G54–G59; zero X/Y/Z/all (persistent) | ✅ | `G10 L20 P<n>` | P2 |
| Temporary zero G92; go to zero (safe Z first) | ✅ | | P2 |
| Store/restore offsets around resets | ✅ | Server-side | P2 |

### 3.6 Probing
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Z probe with plate thickness | ✅ | Server routine: `G38.2`, `G10 L20 Z<t>`, retract | P3 |
| Two-stage fast/slow probe *(bCNC)* | ✅ | | P3 |
| Probe test / connected guard | 🟡 | 0.9: short `G38.3` test plus a checklist. 1.1: live `Pn:P` | P3 |
| Tool-length offset G43.1 *(bCNC)* | ✅ | | Later |

### 3.7 Heightmap (computed on the server)
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Border X/Y/W/H; "Auto" from the job bounds | ✅ | The server parses the job and returns its bounds | P3 |
| Probe grid N×M, Zt, Zb, feed; reference origin | ✅ | Server probe routine (serpentine). Each point is pushed over SSE as `probe.point` | P3 |
| Live table and 3D surface while probing | ✅ | SPA renders from events | P3 |
| Bicubic interpolation grid (≥10× probe points) | ✅ | Server (pure Python); the SPA gets the interpolated grid to draw | P3 |
| Edit cells by hand | ✅ | `PATCH /api/heightmap/cells` | P3 |
| Save/open `candle map v3` | ✅ | `GET/PUT /api/heightmap` (text) plus a server library of named maps | P3 |
| Use heightmap → transformed program; save transformed | ✅ | The server transforms the job. The SPA fetches the transformed text to draw it | P3 |
| "Origin" (go to map reference XY) | ✅ | | P3 |
| Re-probe failed or selected points | ✅ | | Later |

### 3.8 Program / job
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Open file (button/drag-drop), recent files | ✅ | Single upload `POST /api/jobs`. The server keeps a job library on disk | P4 |
| Table: #, command, state, response | ✅ | Virtualised table. States come from SSE `job.progress` (sent/acked ranges) plus a per-line errors list | P4 |
| Send / Pause / Stop / Reset | ✅ | `POST /api/job/start|pause|resume|stop` | P4 |
| Send from selected line with parser-state restore | ✅ | Server replays modal state, then safe entry (Z up, XY, spindle plus dwell, plunge) | P4 |
| Start/end commands, M6 pause and tool-change commands, ignore errors | ✅ | Server sender settings. M6 pauses the job and raises an SSE `job.toolchange` prompt | P4 |
| Auto-scroll; elapsed time and ETA; completion notice | ✅ | Server estimator (`$110–$122`); SPA notification and sound | P4 |
| Edit lines with undo/redo; New | ✅ | | Later |
| Canned-cycle expansion G81–G83 *(bCNC)* | n/a | Server-side expansion | Later |

### 3.9 Visualizer (SPA, WebGL)
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Toolpath: solid feed, dashed rapids, red Z moves | ✅ | SPA parser (display only) on the exact text the server sends | P2 basic, P4 full |
| Progress colours (done = grey, in buffer = violet); markers start/end/selected | ✅ | line → vertex-range map | P4 |
| Origin, axes, tool model, live tool position | ✅ | | P2 |
| Orbit/pan/zoom, Top/Front/Left/Iso, Fit, perspective/ortho | ✅ | | P2 |
| Bounds info, machine bounds box, arc segment settings | ✅ | | P4 |
| Raster/grayscale (laser) mode | ✅ | | Later |
| Visual settings (colours, line width, AA) | ✅ | | P6 |

### 3.10 Shell
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Collapsible/resizable panels; layout lock | ✅ | Drag-to-rearrange: Later | P6 |
| Settings (groups, defaults, profiles, import/export); shortcuts editor | ✅ | Machine and sender settings on the server; UI preferences in `localStorage` | P6 |
| User commands panel | ✅ | Buttons → G-code plus a small macro language (`{wx}`, `{prbz}`, `wait idle`), run by the server | P5 |
| Script window (arbitrary JS) / plugins | ❌ | Covered by macros | ✗ |
| Camera with reticle | ✅ | `getUserMedia` in the SPA | Later |
| Theme (light/dark), help page | ✅ | | P6 |

### 3.11 bCNC features considered
| Feature | Phase |
|---|---|
| Two-stage probe; heightmap colour map; software feed override for 0.9 | P3 / P3 / P4 |
| Centre finder; tool-change policy with tool-length re-probe; canned-cycle expansion | Later |
| Orient: 2-marker align, rotate/offset G-code (double-sided PCB); translate/rotate/mirror | Later |
| Autolevel on the fly during sending | ✗ (we pre-transform on the server, which is equivalent and inspectable) |
| CAM, DXF/SVG/STL, generators, rapid optimisation, separate web pendant | ✗ (the SPA already works on a phone or tablet) |

---

## 4. Repository layout

```
pyproject.toml          gcsender package; deps: pyserial; dev: pytest; [tool.uv] no-build, exclude-newer
uv.lock
server/gcsender/
  __main__.py           CLI: --port --baud --http --lan --state-dir --simulate {0.9j,1.1f}
  webserver.py          ThreadingHTTPServer: static files, router, JSON, SSE, auth
  api.py                REST routes
  controller.py         connection, firmware detection, status polling, routing of controller output
  events.py             event bus, ring buffer (ids), per-client queues
  transport.py          pyserial transport (reader thread, exclusive open) + interface
  simulator.py          GRBL 0.9j/1.1f simulator (RX/planner model, probe surface, hold/reset, banner)
  protocol.py           line classification and parsing for both firmwares
  capabilities.py
  machine.py            machine model: state, positions, WCO, modal, offsets, settings
  streamer.py           char-counting queue (127 B), sources, response correlation
  job.py                job runner: start/pause/stop/from-line, M6, overrides, progress, estimator
  jog.py  stop.py  probe.py  macros.py
  gcode/parser.py  gcode/arcs.py  gcode/estimate.py
  heightmap/model.py  heightmap/interp.py  heightmap/mapfile.py  heightmap/transform.py
  store.py              state dir: settings.json, jobs/, maps/, journal.json
  tests/
web/                    served as the SPA
  index.html  css/app.css
  js/api.js  js/events.js            fetch wrapper (token) + EventSource with snapshot/replay
  js/state.js                        client-side store, fed only by server events
  js/gcode/parser.js                 display-only parser → segments with line indices
  js/view/gl.js  js/view/camera.js   WebGL renderer
  js/ui/*.js                         panels
  tests.html                         in-browser unit tests (no framework)
```

---

## 5. Server design

### 5.1 Threads and concurrency
- **Serial reader thread:** reads lines and passes them to `protocol` → `machine` and `streamer` → `events`.
- **Sender thread:** waits on a condition variable and writes queued lines whenever the 127-byte budget allows. Real-time bytes are written immediately under the write lock.
- **Status poll thread:** sends `?` every *n* ms.
- **Jog watchdog thread:** stops continuous jog when heartbeats lapse.
- **HTTP:** `ThreadingHTTPServer`, one thread per request or SSE client.
- One `Controller` lock guards state transitions. CPU-heavy work (parse, transform, estimate) runs in a worker thread so I/O never stalls.

### 5.2 Streamer
- Character counting with a 127-byte budget. Lines are stripped, upper-cased and rejected at ≥ 80 characters.
- Each in-flight entry is tagged `{source: job|console|ui|probe|jog|restore|macro, jobLine?, requestId?}`, and each `ok`/`error` pops the oldest entry. Console requests get their response back on the HTTP call.
- While a job runs, console and UI lines are allowed only when safe (non-motion commands, or when the job is paused). Otherwise the server returns `409`.
- On `error` the job stops, unless "ignore errors" is set. On `ALARM` everything stops and the queue is flushed.

### 5.3 Job runner
- **Upload:**
  - The body is raw text (up to 50 MB).
  - The server stores it, parses it, and computes bounds, ETA and modal snapshots every 1,000 lines (for fast send-from-line).
  - It returns `{jobId, lines, bounds, eta}`.
- **Variants:** `original` and `transformed` (heightmap). The **active variant's text is exactly what is streamed**. The SPA renders the same text, so line indices match 1:1.
- **Progress events** (throttled ~10 Hz): `{sentLine, ackedLine, elapsed, eta, state}`, plus immediate `job.error`, `job.toolchange`, `job.done`.
- **Feed override:** on 0.9 the `F` words are rewritten as lines leave the queue (the UI says it takes effect after up to 17 blocks). On 1.1 the server sends real-time bytes.

### 5.4 Jog and stop
- **Step:**
  - 0.9: `G91 G1 X… F…` then `G90`.
  - 1.1: `$J=G91 G21 X… F…`.
- **Continuous:** `jog/start {vector, feed}` makes the server stream short segments (`feed × 80 ms`, ≤ 3 in flight) while heartbeats keep arriving (every 100 ms, lapse after 300 ms).
  - `jog/stop` or a lapsed heartbeat stops new segments.
  - 0.9: the overrun is bounded by ≤ 3 segments (≈2.4 mm at 600 mm/min).
  - 1.1: uses one long `$J=` move, ended with `0x85` for an exact stop.
- **Hard stop (0.9):**
  1. Send `!`.
  2. Wait for `Hold` with MPos unchanged across two polls.
  3. Send `0x18`; no alarm occurs.
  4. Restore G92 and modal state.

  On 1.1: wait for `Hold:0`, then reset (or `0x85` while jogging).

### 5.5 Probing
- Probe routines are server-side state machines and parse `[PRB:…]`. Each point emits `probe.point`.
- Heightmap probing is an internal job. It survives SPA reloads and can be paused or aborted like any job.
- Z values are stored relative to the first point (Candle semantics).

### 5.6 Heightmap transform
1. Parse the job and linearise arcs (by length/angle settings).
2. Subdivide moves into segments of at most `min(interpStepX, interpStepY)`.
3. For each vertex, add `h(x,y)` (bicubic; clamped at the border, with a warning count).
4. Output a new text with a header comment.
5. Cache the result by `(jobId, mapHash, params)`.

### 5.7 Persistence (state dir, default `~/.gcsender/`)
- `settings.json`: machine, sender, jog, probe and user-command settings.
- `jobs/<id>.nc` plus metadata, and `maps/<name>.map`.
- `journal.json`: active job, variant, sent/acked line, modal/WCS/G92, spindle. Written every ~1 s and on state changes, via an atomic file replace.
- `logs/serial-YYYYMMDD.log`: raw transcript.

---

## 6. HTTP API, events and security

### 6.1 Security (it moves a machine)
- **Binding:** `127.0.0.1` by default. `--lan` binds `0.0.0.0` and prints a warning.
- **Token:**
  - A random token is created at each start-up. It is injected into the served `index.html` (`<meta name="gcs-token">`) and printed on the console for LAN devices.
  - Every non-GET request requires the header `X-GCS-Token`.
  - The SSE stream and data GETs take the token as `?t=`.
- **Request checks:**
  - Mutating calls require `Content-Type: application/json` (or `text/plain` with the token header for uploads).
  - `Origin`, when present, must match the server's own origin.
  - The `Host` header must match the bound host or IP (DNS-rebinding defence).
  - No CORS headers are ever sent.
- The serial port is opened with `exclusive=True`, so no other program can grab it mid-job.

### 6.2 Endpoints
```
GET  /                         SPA (static, token injected)
GET  /api/state                snapshot: connection, caps, machine, modal, offsets, settings, job, heightmap, jog
GET  /api/events?t=…           SSE (see 6.3); honours Last-Event-ID
GET  /api/ports
POST /api/connect              {port, baud}          POST /api/disconnect
POST /api/command              {line} → {response, lines[]}
POST /api/realtime             {op: status|hold|resume|reset|door|jogCancel|ovFeed…}
POST /api/control              {op: home|unlock|check|sleep|flood|mist|coolantOff}
POST /api/spindle              {on, rpm, dir}
POST /api/wcs/select           {g: 54..59}   POST /api/wcs/zero {axes:"XYZ", persistent:true}
POST /api/goto                 {x?,y?,z?, safeZ?}
POST /api/jog/step             {x?,y?,z?, feed}
POST /api/jog/start            {vx,vy,vz, feed} → {jogId}
POST /api/jog/heartbeat        {jogId}        POST /api/jog/stop {jogId?, hard?:bool}
POST /api/probe/z              {depth, feed, fastFeed?, plate, retract}
POST /api/probe/test
GET/PUT  /api/heightmap        JSON model; ?format=map for candle map v3 text
PATCH    /api/heightmap/cells  {cells:[{i,j,z}]}
POST     /api/heightmap/auto   (border from active job)   POST /api/heightmap/probe   POST /api/heightmap/abort
GET      /api/heightmap/library · POST /api/heightmap/library/{name} · GET …/{name}
POST /api/jobs                 raw G-code body, ?name= → {jobId, lines, bounds, eta}
GET  /api/jobs                 library     GET /api/jobs/{id}?variant=original|transformed (text)
POST /api/job/select           {jobId}     POST /api/job/heightmap {enabled}
POST /api/job/start            {fromLine?}  POST /api/job/pause|resume|stop
POST /api/job/override         {feed%, spindle%?, rapid%?}
POST /api/job/toolchange/continue
GET/PUT /api/grbl/settings     $$ parsed / {id: value}
GET/PUT /api/settings          app (server-side) settings
GET/PUT /api/macros            user commands      POST /api/macros/{id}/run
GET  /api/logs/serial          transcript download
```

### 6.3 SSE events
- **Event types:**
  - `snapshot` (on connect when replay is impossible)
  - `status`
  - `state` (connection, alarm, caps)
  - `console` (direction, line, source)
  - `modal`, `offsets`, `settings`
  - `job.progress`, `job.state`, `job.error`, `job.toolchange`, `job.done`
  - `probe.point`, `heightmap.changed`
  - `jog.state`, `log`
- **Replay:** event ids are monotonic. A ring buffer of ~5,000 events (status events coalesced) allows replay via `Last-Event-ID`. If that id is older than the buffer, the server sends `snapshot` instead.
- **Keep-alive:** a comment line every 15 s keeps proxies and sleeping tabs connected.

---

## 7. SPA design
- **State:** a single client store fed **only** by `GET /api/state` plus SSE. User actions are API calls, never optimistic machine-state edits, so a reload reconstructs the identical view.
- **Panels:** State, Control, Jog, Coordinate system, Spindle/Overrides, Heightmap, User commands, Console, Program table, Visualizer, Settings.
- **Keyboard jog:**
  - It is active only when "Keyboard control" is on and no input has focus. Key auto-repeat is ignored.
  - Holding a key → `jog/start` plus heartbeats; releasing it → `jog/stop`.
  - `blur`, `visibilitychange` and `pagehide` → `jog/stop` via `navigator.sendBeacon`.
  - Esc → hard stop.
- **Visualizer:** WebGL 1 lines and points with per-vertex colour, a line → vertex map for progress colouring, and the heightmap surface as a colour-mapped mesh.
- **Reload guard:** a `beforeunload` prompt while a job, probe run or jog is active. This is a courtesy only, since a reload is now harmless.
- **Connection banner:** shown if SSE drops, with auto-reconnect.

---

## 8. Reload and restart behaviour

| Event | Effect | Recovery |
|---|---|---|
| SPA reload / tab closed / browser crash | Job, probe run and console session continue on the server. A continuous jog stops within 300 ms (dead-man) | On load: `GET /api/state` + SSE with `Last-Event-ID`, giving the identical view. Fully automatic |
| Network blip (LAN tablet) | Same as above | EventSource auto-reconnects and replays |
| Server process crash or restart | Streaming stops; GRBL finishes its buffer. Reopening the port **resets the board** (DTR) unless auto-reset is disabled. pyserial `dtr=False` before open is attempted; it may or may not help on the CH340 and must be verified | The journal is read at start-up. **No banner** (no-reset board): re-attach and offer seamless resume from `sent+1`. **Banner**: guided re-zero (WCS from EEPROM; Z re-probe at the map origin), then send from line N with parser-state restore |
| Host reboot / USB unplug | As above, plus position lost | Same guided path |

---

## 9. Phases and acceptance criteria

| Phase | Scope | Acceptance |
|---|---|---|
| **P0 Skeleton & simulator** | uv project, CLI, HTTP static/router/auth, event bus + SSE with replay, transport interface, **GRBL 0.9j/1.1f simulator**, SPA shell with api/events/state modules | `uv run gcsender --simulate 0.9j` serves the SPA; the SSE stream shows simulated status; a reload replays without gaps; token, Origin and Host checks pass in tests |
| **P1 Connect, monitor, console** | Ports/connect, protocol parsing for both firmwares, capabilities, machine model, State/Control panels, console, `$$` editor and `$10` check, error/alarm text, reset with restore, serial transcript | On the real C3018: Idle with MPos/WPos; Hold/Resume/Reset/Unlock; `$$` round-trip; same on the 1.1 simulator |
| **P2 Jog, WCS, spindle, 3D** | Step/continuous jog with dead-man, keyboard map, hard stop, WCS select/zero, G92, go-to, spindle/coolant, WebGL view with live tool and camera controls | Keyboard jog overrun within spec; closing the tab mid-jog stops motion ≤ 300 ms; Esc stop gives no alarm and keeps offsets |
| **P3 Probe & heightmap** | Z probe (single/two-stage), probe test, heightmap create/auto/probe/edit/library, `.map` v3 I/O, bicubic, server transform + cache, SPA surface render, transformed-program fetch | A 5×4 map on the machine; `.map` interop with Candle; a reload mid-probe continues; the transformed job's Z verified on a test PCB |
| **P4 Jobs & streaming** | Upload/library, server parser + estimator, streamer job runner, pause/stop/from-line with restore, start/end commands, M6 flow, overrides, SPA program table + progress colouring + markers + bounds + ETA | A 50k-line job streams without stalls; a reload mid-job shows correct progress; ETA within ±15 % |
| **P5 Recovery & user commands** | Journal, start-up recovery (re-attach and post-reset paths), macros panel and engine, notifications | Killing and restarting the server mid-job on the simulator (reset and no-reset modes) recovers as specified |
| **P6 Polish** | Settings UI and profiles, shortcuts editor, layout (collapse/resize), visual settings, theme, help, door command | Usable on desktop and tablet widths |
| **Later** | Line editing/undo, camera, orient/align, transform G-code, canned cycles, centre finder, TLO re-probe on M6, laser view, S-rewrite override on 0.9, re-probe selected points | — |

---

## 10. Testing
- **Simulator** (`--simulate 0.9j|1.1f`):
  - Models: the 128-byte RX buffer, 80-character line limit, planner depth (18 blocks on 0.9j, 16 on 1.1f) and timing, states, `$10` formats, `$$`/`$#`/`$G`, G38.x against a configurable surface `z(x,y)` with probe fail, hold/reset semantics including the alarm cases, and the banner on open (reset vs no-reset mode).
  - Lets every phase run end-to-end without the machine.
- **pytest:** protocol parsing (fixtures from real 0.9j/1.1 transcripts), streamer budget and correlation, jog dead-man, stop sequence, heightmap interpolation and transform (golden files), `.map` round-trip, the SSE replay/snapshot boundary, and the auth checks.
- **SPA:** `web/tests.html` for the display parser and view math, run in the browser.
- **Hardware checklist per phase:** air-cut first.

---

## 11. Risks and open items
- **Board info:** please share the banner/`$I` and `$$` output to preset `$10`, bounds and rates.
- **DTR reset on server (re)start:** inherent to the CH340 + 328P board. It only matters if the server restarts mid-job; disabling auto-reset (a 10 µF cap RESET–GND) makes that case seamless too. Both paths are supported.
- **0.9 continuous-jog overrun and software feed-override lag** are documented in the UI; neither is a safety control. The hardware E-stop remains the real stop.
- **LAN mode** exposes machine control to anyone with the token; it is off by default.
- **Python performance:** transforming very large jobs in pure Python takes seconds rather than milliseconds. It runs in a worker with progress events; numpy is not needed.
