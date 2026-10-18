# GCSender — Plan (server-first)

A Candle-style G-code sender for a **C3018** desktop mill (ATmega328P + CH340 board) running **GRBL 0.9j**,
with **GRBL 1.1** supported from the start. Feature reference: Denvi/Candle (master @ c04e6f2, v11.x), plus selected bCNC features.

Focus areas, in priority order:
1. Connecting and showing machine state.
2. Jogging in precise steps (0.01–10 mm), from buttons or the keyboard. **Step jogging only — no continuous (hold-to-move) jogging** (§5.4).
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
| Jogging | `$J=` plus cancel `0x85` | None. Step jogs are plain `G91` moves; stop with hold → reset (§5.4) |
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

**The user's C3018** (from `docs/responses.txt`):
- Firmware: `$I` reports `[0.9j.20180122:]`.
- `$10=3`, so status reports carry both MPos and WPos. This is what GCSender needs.
- `$13=0` (mm). `$20=$21=$22=0`: no soft limits, no hard limits, no homing. Machine coordinates are therefore relative to power-on.
- Steps per mm: `$100–$102=400`.
- Max rates: `$110–$112=1500` mm/min.
- Accelerations: `$120–$122=10` mm/s².
- `$130–$132=200` mm. This does not match the real C3018 travel (~300 × 180 × 45). It is harmless while soft limits are off, but jog bounds (P2) must use the app's own machine-bounds setting, not these values.
- The simulator defaults to exactly these settings.

Still assumed, not yet confirmed:
- Probe on A5.
- Spindle `M3/M5`, with or without PWM `S`.
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
| Door | 🟡 | `@` / `0x84`: Control panel **Door** button (feed hold reported as Door; Resume continues) | P6 ✅ |
| Flood/mist | 🟡 | `M8`/`M7`/`M9` in Idle; real-time toggles while running on 1.1 | P2 ✅ |

### 3.3 Spindle and overrides
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Spindle on/off, speed field and slider, min/max | 🟡 | `M3 S…`/`M5` in Idle. In Hold only on 1.1 (`0x9E`): 0.9 blocks the parser on spindle changes until the planner empties | P2 ✅ |
| Change S during a job | 🟡 | Server injects into the stream | P4 |
| Feed override | 🟡 | 0.9: the server rewrites `F` as lines leave (UI notes the lag). 1.1: real-time | P4 ✅ |
| Rapid and spindle override | ❌ | 1.1 real-time only (0.9 S rewrite: Later) | P4 ✅ (1.1) |

### 3.4 Jogging
**Decision: step jogging only.** A click or key press moves exactly the selected step. Deliberate presses may queue, but a long press on a key or mouse button is always exactly one step. Continuous (hold-to-move) jogging is deliberately **not provided**, from Candle, bCNC or anywhere else. On a 0.9 machine any overrun past the intended point hits the workpiece and can damage the tool (§5.4).

| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| X±/Y±/Z± and XY-diagonal buttons, Stop | ✅ | `POST /api/jog/step`, `POST /api/stop` (§5.4) | P2 ✅ |
| Step selector 0.01 / 0.05 / 0.1 / 0.5 / 1 / 5 / 10 mm; feed list | ✅ | Segmented buttons in the Jog panel. Both lists are editable in settings | P2 ✅ |
| Keyboard control and remappable keys | ✅ | One key press = one step; auto-repeat is ignored, and a key counts again only after release. Keys can cycle the step and feed sizes | P2 ✅ |
| Continuous / hold-to-move jog (Candle "continuous" step) | — | **Excluded by design**: overrun risk | ✗ |
| Jog box | ✅ | Optional machine-coordinate box. A step that would leave it is refused, never shortened | P2 ✅ |

### 3.5 Coordinate systems
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Select G54–G59; zero X/Y/Z/all (persistent) | ✅ | `G10 L20 P<n>` | P2 ✅ |
| Temporary zero G92; go to zero (safe Z first) | ✅ | | P2 ✅ |
| Store/restore offsets around resets | ✅ | Server-side | P1 ✅ |

### 3.6 Probing
| Feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Z probe with plate thickness | ✅ | Server routine: `G38.2`, `G10 L20 Z<t>` (corrected for overshoot past the trigger), retract | P3 ✅ |
| Two-stage fast/slow probe *(bCNC)* | ✅ | | P3 ✅ |
| Probe test / connected guard | 🟡 | Both firmwares: "Test: open" / "Test: touching" via a 0.1 mm upward `G38.3`, which alarms without moving if the input is already triggered; the alarm is cleared with `$X`. 1.1 also shows live `Pn:P` | P3 ✅ |
| Tool-length offset G43.1 *(bCNC)* | ✅ | | Later |

### 3.7 Heightmap (computed on the server)
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Border X/Y/W/H; "Auto" from the job bounds | ✅ | The server reads a G-code file and uses the extent of its cutting moves (rapids excluded), plus a margin. Until P4 the file is picked in the panel | P3 ✅ |
| Probe grid N×M, Zt, Zb, feed; reference origin | ✅ | Server probe routine (origin first, then serpentine). Each point updates the retained `heightmap` event | P3 ✅ |
| Live table and 3D surface while probing | ✅ | SPA renders from events; survives a reload mid-run | P3 ✅ |
| Bicubic interpolation grid (≥10× probe points) | ✅ | Server (pure Python); the SPA gets the interpolated grid to draw | P3 ✅ |
| Edit cells by hand | ✅ | `PATCH /api/heightmap/cells` | P3 ✅ |
| Save/open `candle map v3` | ✅ | Import/export plus a server library of named maps; the working map survives server restarts | P3 ✅ |
| Use heightmap → transformed program; save transformed | ✅ | The server transforms the program; the SPA downloads it and draws it. Until P4 (jobs) the file is picked in the panel; in P4 the job's `transformed` variant is streamed | P3 ✅ |
| "Origin" (go to map reference XY) | ✅ | | P3 ✅ |
| Re-probe failed or selected points | ✅ | | Later |

### 3.8 Program / job
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Open file (button/drag-drop), recent files | ✅ | Single upload `POST /api/jobs` (raw text, ≤ 50 MB). The server keeps a job library on disk (`<state>/jobs`) | P4 ✅ |
| Table: #, command, state, response | ✅ | Virtualised table (50k+ lines). Row states from the retained `job` event: done (executed), queued (acknowledged, in GRBL's planner), sent (awaiting ack), error | P4 ✅ |
| Send / Pause / Stop / Reset | ✅ | `POST /api/job/start|pause|resume|stop`; Stop is the safe stop (hold, reset, restore; no alarm) | P4 ✅ |
| Send from selected line with parser-state restore | ✅ | Server replays modal state, then safe entry (Z up, XY, spindle plus dwell, plunge at ≤ `entryPlungeFeed`). Refused if the program used G92 before the line or the position is unknown | P4 ✅ |
| Start/end commands, M6 pause and tool-change commands, ignore errors | ✅ | Server sender settings. M6 (never sent to GRBL) waits for Idle, runs the tool-change commands and pauses in `toolchange`; jog and probe are allowed until Continue. An error feed-holds and asks (Continue/Stop) unless "ignore errors"; an ALARM always ends the job. **M0/M1** (bug report 20261017-185144-b289): not sent to GRBL, which would feed-hold where it cannot jog, with no explanation in the UI. The job pauses in `toolchange` (kind `M0`/`M1`) showing the nearest preceding `(MSG, …)` text; M1 is skipped when *Pause at M1* is off. Progress is published by a ticker while the runner is blocked, so the UI never shows a stale line | P4 ✅ |
| Auto-scroll; elapsed time and ETA; completion notice | ✅ | Server estimator (GRBL planner model: `$11`, `$110–$122`, junction deviation, trapezoids); the remaining time is corrected by the measured ratio. SPA tone and console notice | P4 ✅ |
| Edit lines with undo/redo; New | ✅ | | Later |
| Canned-cycle expansion G81–G83 *(bCNC)* | n/a | Server-side expansion | Later |

### 3.9 Visualizer (SPA, WebGL)
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Toolpath: solid feed, dashed rapids, red Z moves | ✅ | SPA parser (display only, arcs linearised) on the exact text the server sends. Rapids are drawn faint rather than dashed (WebGL 1 has no line stipple) | P4 ✅ |
| Progress colours (done = grey, in buffer = violet); markers start/end/selected | ✅ | line → vertex-range map; progress recolours by drawing ranges with a tint (no re-upload) | P4 ✅ |
| Origin, axes, tool model, live tool position | ✅ | Plus a grid, the jog box and a tool trail | P2 ✅ |
| Orbit/pan/zoom, Top/Front/Left/Iso, Fit, perspective/ortho | ✅ | | P2 ✅ |
| Bounds info, machine bounds box, arc segment settings | ✅ | Bounds shown in the program panel; the jog box is drawn (P2) | P4 ✅ |
| Raster/grayscale (laser) mode | ✅ | | Later |
| Visual settings (colours, line width, AA) | ✅ | Settings → Display (per browser): toolpath, progress, selection, tool and trail colours; grid / rapids / trail visibility, trail length, tool model size, antialiasing. Line width is not offered: WebGL draws 1-px lines in most browsers | P6 ✅ |

### 3.10 Shell
| Candle feature | 0.9 | Implementation | Phase |
|---|---|---|---|
| Collapsible/resizable panels; layout lock | ✅ | Left-column panels collapse from their title; dividers between the columns and above the program/console drag (double-click resets); sizes are clamped to the window; all kept per browser. Drag-to-rearrange: Later | P6 ✅ |
| Settings (groups, defaults, profiles, import/export); shortcuts editor | ✅ | Machine and sender settings on the server; UI preferences in `localStorage`. **Defaults** (session keys kept), named **profiles** (settings + user commands, `<state>/profiles`), **export/import** JSON (all-or-nothing, unknown keys reported), **Back up $$** as a text file. Shortcuts: jog, step, feed, stop plus hold, resume, job pause, views, console and user commands 1–9 (unbound by default) | P6 ✅ |
| User commands panel | ✅ | Buttons → G-code plus a small macro language (`{wx}`, `{prbz}`, `{max(safez, wz + 2)}`, `wait idle`), run by the server. Expressions are parsed with a whitelist (numbers, `+ - * /`, `min max abs`, named variables), never `eval`. A line using a position waits for Idle first; an error or Stop ends the macro. Stored in `<state>/macros.json` | P5 ✅ |
| Script window (arbitrary JS) / plugins | ❌ | Covered by macros | ✗ |
| Camera with reticle | ✅ | `getUserMedia` in the SPA | Later |
| Theme (light/dark), help page | ✅ | Theme: follow the system, light or dark, applied before first paint. Help dialog with workflow, recovery, safety, troubleshooting and the live key bindings | P6 ✅ |
| Bug reports (not in Candle) | ✅ | **🐞 Report**: the user describes the problem; the server saves a zip in `<state>/bugreports` (newest 100, ≤ 500 MB): summary, controller/streamer internals (in-flight lines), retained state, job + journal, `$$`/offsets/modal, recent events and status polls, thread stacks, application log tail (`logs/gcsender.log`, rotating), last 5000 serial-transcript lines, settings/macros/heightmap files, optionally the program (trimmed above 5 MB), and the browser's diagnostics (`js/diag.js`: errors, API calls, clicks and keys outside fields, link changes, on-screen messages, preferences). Each section is captured independently. List, download one, download all, delete. A separate persistent **reports token** reads reports only, for remote retrieval over `--lan` | P6 ✅ |

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
- **Progress events:** one retained `job` event (throttled ~10 Hz, immediate on state changes) carrying `{state, sent, acked, executing, errors, heldError, toolchange, override, elapsed, eta}`. States: `idle, ready, starting, running, paused, error, toolchange, done, stopped, failed`. Being retained, it is what a reloaded SPA sees, so there are no separate one-shot events to miss.
- **Executing line:** GRBL acknowledges a line when it enters the planner, up to 17 moves before it runs. The runner replays the planner timeline (each acknowledged line starts when the previous one ends and takes its estimated time; the clock stops during a hold; never more than the planner depth behind). The ETA, the "done" colouring and the "stopped at line" message use that line, not the last `ok`.
- **Pipelining:** lines are sent whenever they fit in the RX budget, without waiting for the previous `ok`. Measured on the simulator: 50,004 lines at ~670 lines/s sustained (above what 115200 baud can carry); at real-time speed the planner stays full (17/17) throughout. `ok` lines from the job are not echoed to the console; errors are, with the failing command.
- **Feed override:** on 0.9 the `F` words are rewritten as lines leave the queue (the UI says it takes effect after up to 17 blocks). On 1.1 the server sends real-time bytes.

### 5.4 Jog and stop
**Jogging is step only, by design.** Every click or key press moves exactly the selected distance (0.01, 0.05, 0.1, 0.5, 1, 5 or 10 mm by default) and nothing more:
- **No continuous jog.** There is no hold-to-move mode, no `jog/start`/`heartbeat`, and no dead-man is needed. GRBL 0.9 has no jog cancel, so a hold-to-move jog must keep its braking distance queued and coasts on after release. With the C3018's 10 mm/s² acceleration that is several millimetres even at moderate feeds, and that overrun lands in the workpiece.
- **A long press is one jog.** On the keyboard, auto-repeat is ignored (`event.repeat`), and a key must be released before it counts again; this also covers platforms and remote desktops that send repeats without the flag. On the mouse, a step fires on `click`, which happens once per press-and-release however long the button is held. Holding a key or button can never produce a fast, uncontrollable stream of jogs.
- **Deliberate presses queue.** Separate presses made while earlier steps are moving are appended and run in order, so five presses of 1 mm move exactly 5 mm. Steps queue only behind other jog steps; while a program, probe or console motion is running, jogging is refused.
- **Exact or nothing.** With the jog box enabled, a step that would leave it is refused, never shortened. Queued steps are checked against the position at the end of the queue.
- **Commands:**
  - 0.9: `G91 G21 G1 X… F…` (always millimetres), then one line that restores the previous units, distance mode, motion mode (G0/G1) and feed. The parser state is read with `$G` first.
  - 1.1: `$J=G91 G21 X… F…` (no modal side effects).
  - The feed is capped per axis at `$110–$112`. The step happens at the selected jog feed; the distance is exact regardless of feed.
  - Resolution: at `$100–$102=400` steps/mm, the smallest step (0.01 mm) is 4 motor steps, so every default size is a whole number of motor steps. Backlash in the C3018's lead screws is not compensated (GRBL 0.9 has no backlash setting), so direction reversals on small steps can lose some of the movement.
- **Hard stop (0.9):**
  1. Send `!`.
  2. Wait for `Hold` with MPos unchanged across two polls.
  3. Send `0x18`; no alarm occurs.
  4. Restore G92 and modal state.

  On 1.1: wait for `Hold:0`, then reset (or `0x85` during a `$J` step). `POST /api/stop` (Esc, ■) does this. Use it to abandon a long step, e.g. 10 mm at a slow feed. A 0.9 step's restore line is parsed before the motion finishes, so the restored parser state is never left in G91.

### 5.5 Probing
- **Routines run on the server** (`probe.py`) and parse `[PRB:…]`. Progress goes out on the retained `probe` event, and map values on the retained `heightmap` event, so a reload mid-run shows it continuing.
- **Busy while running:** the controller refuses console, UI and jog commands (409). Hold and Resume work. Stop (Esc / ■ / Abort) aborts: hold → settle → reset → restore, with no alarm; the partial map is kept.
- **Z probe:** `G91 G38.2 Z-depth F fast`. Optionally back off and touch again at the slow feed. Then `G10 L20 P<active> Z` so the *contact point* reads the plate thickness; the overshoot past the trigger is corrected using `PRB` versus MPos. Then retract and restore the user's parser state. No contact within the depth puts GRBL in ALARM; the message says to check the probe, then Unlock.
- **Probe input test (needed on 0.9, which has no pin report):** `G91 G38.3 Z0.1`. GRBL alarms immediately, without moving, if the input is already triggered, so an alarm means "closed"; it is then cleared with `$X`. Otherwise the tool moves 0.1 mm up and back. "Test: open" (nothing touching) and "Test: touching" (clip held to the plate) together prove the wiring.
- **Heightmap run (Candle semantics):** G21 G90 in work coordinates. Raise to Z top; probe the map origin first as the reference; then the grid in serpentine order (row 0 = minimum Y), raising to Z top between points. Each value = `PRB.z − reference`, so the origin reads 0. Work Z0 must be set near the surface first (use the Z probe).
- **`.map` format (`candle map v3`, verified against Candle's source):** signature; `x;y;w;h`; `originX;originY`; `nx;ny;zBottom;zTop;feed`; `interpType;interpNx;interpNy`; then `ny` rows of `nx` values (row 0 = minimum Y, `nan` = unprobed), CRLF. The interpolation figures are point *counts*.

### 5.6 Heightmap transform
1. Read the program (`gcode.py`), tracking units, distance mode, plane, motion mode, the program's own G92 shift and the position.
2. Linearise arcs (G17/18/19, IJK or R, GRBL geometry) with chord error ≤ `arcTolerance` (default 0.01 mm). Subdivide every G0/G1/arc piece so it is no longer than the interpolation spacing `min(w/(interpNx−1), h/(interpNy−1))`.
3. For each vertex, add `h(x,y)`. Rapids are compensated too (as Candle does).
   - Interpolation is Candle's Catmull-Rom bicubic, identical in interior cells. In edge cells the missing neighbour is extrapolated linearly instead of duplicated, so a tilted board is exact up to the border; Candle errs by about 14 µm on a 0.6 mm/30 mm tilt.
   - Outside the border, the height is clamped to the edge, with a warning count.
4. Output rules:
   - Every emitted motion line has an explicit `G0`/`G1`.
   - Coordinates are written in the program's own units and distance mode. In G91, increments are computed from the *rounded* emitted positions, so rounding never drifts.
   - Lines that must not be changed are copied as-is: G53, G28/G30, G38.x, G10/G92, comments, and M/S/T. A copied motion line that relied on a modal motion word gets it made explicit.
   - A move whose start is unknown (the first XY move) gets only its end point compensated. A move whose end is unknown (e.g. the initial `G0 Z5`) is copied and counted.
5. The output starts with a header comment. `line_map[k]` gives each output line's source line, for P4 progress.
6. The result is cached by `(sha256(program), mapDigest, arcTolerance)` (4 entries).

### 5.7 Persistence (state dir, default `~/.gcsender/`)
- `settings.json`: machine, sender, jog, probe and user-command settings.
- `jobs/<id>.nc` plus metadata, and `maps/<name>.map`.
- `journal.json`: active job, variant digest, sent/acked/executing line, `reserved` (the journal is written *before* streaming past it, 64 lines ahead, so the last line sent is always ≤ `reserved`), last MPos/WPos, offsets and modal state. Written every ~1 s, on state changes and ahead of streaming, via a temp file, fsync and an atomic replace. Removed when a job ends (done/stopped/failed); kept while `interrupted`.
- `macros.json`: user commands.
- `logs/serial-YYYYMMDD.log`: raw transcript.
- `logs/gcsender.log` (+ `.1`, `.2`, 2 MB each): application log.
- `bugreports/<id>.zip` + `<id>.json` (list entry); `reports-token`: the read-only token for remote report downloads.

---

## 6. HTTP API, events and security

### 6.1 Security (it moves a machine)
- **Binding:** `127.0.0.1` by default. `--lan` binds `0.0.0.0` and prints a warning.
- **LAN login (`auth.py`):** users in `<state>/users.json` (scrypt n=2^14, r=8, p=1; managed with `--add-user` / `--remove-user` / `--list-users`, reloaded when the file changes). `POST /api/login {username, password}` sets `gcs_session_<port>` (cookies are not per-port), HttpOnly, SameSite=Strict, Path=/; only its SHA-256 is stored (`sessions.json`, survives restarts). Sessions end on logout, after 7 days idle, 30 days after login, when the user is removed or their password changes. Mutating requests on a session need `X-GCS-Request: 1` (CSRF: a custom header forces a CORS pre-flight that is never answered). Failed logins: 5 free per address, then 2 s doubling up to 15 min. `GET /api/session` tells the SPA whether to show the login; a 401 later re-opens it. The SPA never receives a key: `index.html` carries the token only for loopback clients, and `?t=` in a URL is stripped and ignored.
- **Token:**
  - A random token is created at each start-up unless one is given with `--token`, `--token-file` or `$GCSENDER_TOKEN` (in that order; 16–256 URL-safe characters, validated because it is placed in URLs and the HTML; must differ from the reports token). It is injected into the served `index.html` (`<meta name="gcs-token">`) and printed on the console for LAN devices.
  - Every non-GET request requires the header `X-GCS-Token`.
  - The SSE stream and data GETs take the token as `?t=`, from loopback clients only (LAN pages use the session cookie; scripts use the header).
- **Request checks:**
  - Mutating calls require `Content-Type: application/json` (or `text/plain` with the token header for uploads).
  - `Origin`, when present, must match the server's own origin.
  - The `Host` header must match the bound host or IP (DNS-rebinding defence).
  - No CORS headers are ever sent.
- **Reports token:** a second, persistent token (`<state>/reports-token`) accepted only for `GET /api/bugreports…`, so reports can be fetched remotely without the control token.
- The serial port is opened with `exclusive=True`, so no other program can grab it mid-job.

### 6.2 Endpoints
```
GET  /                         SPA (static, token injected)
GET  /api/state                snapshot: connection, caps, machine, modal, offsets, settings, job, heightmap
GET  /api/events?t=…           SSE (see 6.3); honours Last-Event-ID
GET  /api/ports
POST /api/connect              {port, baud}          POST /api/disconnect
POST /api/command              {line} → {response, lines[]}
POST /api/realtime             {op: status|hold|resume|reset|door|jogCancel|ovFeed…}
POST /api/control              {op: home|unlock|check|sleep|flood|mist|coolantOff}
POST /api/spindle              {on, rpm, dir}
POST /api/wcs/select           {g: 54..59}   POST /api/wcs/zero {axes:"XYZ", persistent:true}
POST /api/goto                 {x?,y?,z?, safeZ?}
POST /api/jog/step             {x?,y?,z?, feed}   one exact step; queues behind earlier jog steps, 409 behind other motion
POST /api/stop                 hold → settle → reset → restore (1.1 step: 0x85)
POST /api/probe/z              {depth?, fastFeed?, slowFeed?, twoStage?, backoff?, plate?, retract?, setZero?} (defaults from settings) → runs in background
POST /api/probe/test           {expect: open|closed} → {triggered, ok, message}
GET  /api/probe                routine status (also the retained `probe` event)
GET/PUT  /api/heightmap        JSON model / parameters (border or grid change clears values)
PATCH    /api/heightmap/cells  {cells:[{i,j,z|null}]}      POST /api/heightmap/clear
POST     /api/heightmap/auto   {gcode, margin}   POST /api/heightmap/probe   POST /api/heightmap/abort   POST /api/heightmap/origin
GET      /api/heightmap/interpolated            GET /api/heightmap/export (.map)   POST /api/heightmap/import {text}
GET      /api/heightmap/library  POST /api/heightmap/library/save|load|delete {name}
POST     /api/heightmap/transform {gcode} → {gcode, stats}  (P4 uses it for the job's transformed variant)
POST /api/jobs                 raw G-code body, ?name= → {id, name, lines, size} (selected when no job runs)
GET  /api/jobs                 library     DELETE /api/jobs/{id}
GET  /api/job                  state       GET /api/job/text (active variant, exactly what is streamed)
POST /api/job/select           {jobId}     POST /api/job/unload     POST /api/job/heightmap {enabled}
POST /api/job/start            {fromLine?}  POST /api/job/pause|resume|stop  (resume = Continue after an error or tool change)
POST /api/job/override         {kind: feed|rapid|spindle, value: %}
POST /api/job/recover          {action: seamless|entry|discard, line?, rezeroed?}
GET/PUT /api/macros            {macros: [{id, name, text}]}       POST /api/macros/{id}/run
POST /api/sim/restart-server   (--simulate only) emulate a server crash + restart; the simulator keeps running
GET/PUT /api/grbl/settings     $$ parsed / {id: value}
GET/PUT /api/settings          app (server-side) settings
POST /api/settings/reset       GET /api/settings/export (file)   POST /api/settings/import {doc, macros}
GET/POST /api/settings/profiles {name}   POST /api/settings/profiles/load {name, macros}   DELETE /api/settings/profiles/{name}
GET  /api/grbl/settings/export $$ backup (text)
GET  /api/logs/serial          transcript download
POST /api/bugreports           {description, client, includeProgram}   GET /api/bugreports (list + remote access info)
GET  /api/bugreports/{id}      zip    GET /api/bugreports/archive (all, zip of zips)    DELETE /api/bugreports/{id}
```

### 6.3 SSE events
- **Event types:**
  - `snapshot` (on connect when replay is impossible)
  - `status`
  - `state` (connection, alarm, caps)
  - `console` (direction, line, source)
  - `modal`, `offsets`, `settings`
  - `job.progress`, `job.state`, `job.error`, `job.toolchange`, `job.done`
  - `probe` (routine status), `heightmap` (map, retained)
  - `log`
- **Replay:** event ids are monotonic. A ring buffer of ~5,000 events (status events coalesced) allows replay via `Last-Event-ID`. If that id is older than the buffer, the server sends `snapshot` instead.
- **Keep-alive:** a comment line every 15 s keeps proxies and sleeping tabs connected.

---

## 7. SPA design
- **State:** a single client store fed **only** by `GET /api/state` plus SSE. User actions are API calls, never optimistic machine-state edits, so a reload reconstructs the identical view.
- **Panels:** State, Control, Jog, Coordinate system, Spindle/Overrides, Heightmap, User commands, Console, Program table, Visualizer, Settings.
- **Keyboard jog (step only):**
  - It is active only when "keyboard" is on in the Jog panel and no input has focus.
  - Each key press sends one `jog/step` of the selected size. Auto-repeat is ignored and a key counts again only after release, so holding a key moves one step. Separate presses queue.
  - Keys also cycle the step size and jog feed. All keys are remappable.
  - Esc → hard stop (always active, even while typing).
- **Visualizer:** WebGL 1 lines and points with per-vertex colour, a line → vertex map for progress colouring, and the heightmap surface as a colour-mapped mesh.
- **Reload guard:** a `beforeunload` prompt while a job or probe run is active. This is a courtesy only, since a reload is now harmless.
- **Connection banner:** shown if SSE drops, with auto-reconnect.

---

## 8. Reload and restart behaviour

| Event | Effect | Recovery |
|---|---|---|
| SPA reload / tab closed / browser crash | Job, probe run and console session continue on the server. A jog step in progress simply completes (it is a bounded move) | On load: `GET /api/state` + SSE with `Last-Event-ID`, giving the identical view. Fully automatic |
| Network blip (LAN tablet) | Same as above | EventSource auto-reconnects and replays |
| Server process crash or restart | Streaming stops; GRBL finishes its buffer. Reopening the port **resets the board** (DTR) unless auto-reset is disabled. pyserial `dtr=False` before open is attempted; it may or may not help on the CH340 and must be verified | The journal is read at start-up and the job comes back as `interrupted` with a recovery card. **No banner** (re-attached): wait for Idle, then match the machine's work position to a line end between the last acknowledged line and the journal's `reserved` line (earliest match, so at worst a short stretch is re-cut, never skipped). Offer **Resume here** (seamless: modal state restored, spindle re-started with a dwell only if it stopped, no motion) or a safe-entry resume. **Banner** (reset): position and parser state are lost. Guided re-zero (XY reference; Z probe, at the map origin with a heightmap), confirmed by two checkboxes, then a safe-entry resume from the line that was executing at the last journal write (≤ 1 s stale, so it errs towards re-cutting) |
| Host reboot / USB unplug | As above, plus position lost | Same guided path. A lost link inside a running server (unplug, Disconnect) interrupts the job in the same way and recovers when the port is reopened |

---

## 9. Phases and acceptance criteria

| Phase | Scope | Acceptance |
|---|---|---|
| **P0 Skeleton & simulator** | uv project, CLI, HTTP static/router/auth, event bus + SSE with replay, transport interface, **GRBL 0.9j/1.1f simulator**, SPA shell with api/events/state modules | `uv run gcsender --simulate 0.9j` serves the SPA; the SSE stream shows simulated status; a reload replays without gaps; token, Origin and Host checks pass in tests |
| **P1 Connect, monitor, console** ✅ (simulator) | Ports/connect, protocol parsing for both firmwares, capabilities, machine model, State/Control panels, console, `$$` editor and `$10` check, error/alarm text, reset with restore, serial transcript | On the real C3018: Idle with MPos/WPos; Hold/Resume/Reset/Unlock; `$$` round-trip; same on the 1.1 simulator. **Hardware check deferred** (no machine access yet). Verified on the 0.9j and 1.1f simulators, configured with the user's `$$` |
| **P2 Jog, WCS, spindle, 3D** ✅ (simulator) | Step-only jog (no continuous mode), keyboard map, hard stop, WCS select/zero, G92, go-to, spindle/coolant, WebGL view with live tool and camera controls | Each step moves exactly its size, for every default size; a held key or button is one step; deliberate presses queue and sum exactly; a step that would leave the jog box (checked at the end of the queue) or that would follow non-jog motion is refused; Esc stop gives no alarm and keeps offsets. Verified on the 0.9j/1.1f simulators (tests in `test_phase2.py`). **Hardware check deferred** |
| **P3 Probe & heightmap** ✅ (simulator) | Z probe (single/two-stage), probe test, heightmap create/auto/probe/edit/library, `.map` v3 I/O, bicubic, server transform + cache, SPA surface render, transformed-program fetch | A 5×4 map on the machine; `.map` interop with Candle; a reload mid-probe continues; the transformed job's Z verified on a test PCB. Simulator: a 5×4 map matches the simulated warped surface within 2 µm; a reload mid-run continues (browser-verified); `.map` files in Candle's layout read and write; transform verified by an independent interpreter (lines, arcs, G91, G20, G92). **Hardware check deferred**, and `.map` interop to be confirmed with a file saved by Candle itself |
| **P4 Jobs & streaming** ✅ (simulator) | Upload/library, server parser + estimator, streamer job runner, pause/stop/from-line with restore, start/end commands, M6 flow, overrides, SPA program table + progress colouring + markers + bounds + ETA | A 50k-line job streams without stalls; a reload mid-job shows correct progress; ETA within ±15 %. Simulator: 50,004 lines streamed at ~670 lines/s with the planner full at real-time speed; a reload mid-job shows the same line, colours and ETA (browser-verified); ETA within ±15 % of the simulated run with the estimator given the simulator's constant-speed model (tests in `test_phase4.py`). **Hardware check deferred**: the ±15 % ETA on the real machine (acceleration, junctions) |
| **P5 Recovery & user commands** ✅ (simulator) | Journal, start-up recovery (re-attach and post-reset paths), macros panel and engine, notifications | Killing and restarting the server mid-job on the simulator (reset and no-reset modes) recovers as specified. Simulator: no-reset restart matched the stop position to the exact line end and resumed seamlessly with every line sent (browser-verified, transcript checked); reset restart required the re-zero confirmation and resumed with a safe entry; lost link inside the server; missing job file; discard (tests in `test_phase5.py`). Notifications: desktop notification + tab title while the tab is hidden. **Hardware check deferred** |
| **P6 Polish** ✅ | Settings UI and profiles, shortcuts editor, layout (collapse/resize), visual settings, theme, help, door command | Usable on desktop and tablet widths. Checked in the browser at 1440×900, 1024×768 and 768×1024 (touch emulation: larger targets; one scrolling column under 900 px) and 390 px (no horizontal scrolling); tests in `test_phase6.py`. Every button explains itself in a tooltip (what it sends or does, e.g. G54–G59 with their stored zeros, `$G`/`$#`/`$$`/`$I`, the State-panel mode chips); `test_ui_tooltips.py` fails if a button without one is added |
| **Later** | Line editing/undo, camera, orient/align, transform G-code, canned cycles, centre finder, TLO re-probe on M6, laser view, S-rewrite override on 0.9, re-probe selected points | — |

---

## 10. Testing
- **Simulator** (`--simulate 0.9j|1.1f`):
  - Models: the 128-byte RX buffer, 80-character line limit, planner depth (18 blocks on 0.9j, 16 on 1.1f) and timing, states, `$10` formats, `$$`/`$#`/`$G`, G38.x against a configurable virtual surface `z(x,y)` (height, tilt, warp, bump, and presets; set with `--sim-surface` or the simulate-only Simulator panel / `GET /api/sim`, `PUT /api/sim/surface`, `POST /api/sim/probe {forced}`) with probe fail, hold/reset semantics including the alarm cases, and the banner on open (reset vs no-reset mode).
  - Lets every phase run end-to-end without the machine.
- **pytest:** protocol parsing (fixtures from real 0.9j/1.1 transcripts), streamer budget and correlation, exact jog steps, stop sequence, heightmap interpolation and transform (golden files), `.map` round-trip, the SSE replay/snapshot boundary, and the auth checks.
- **SPA:** `web/tests.html` for the display parser and view math, run in the browser.
- **Hardware checklist per phase:** air-cut first.

---

## 11. Risks and open items
- **Deferred hardware checks.** Run these on the C3018 when it is available, before relying on P2+ features:
  - P1 acceptance: connect, Idle with MPos/WPos, Hold/Resume/Reset/Unlock, `$$` round-trip.
  - Does opening the port reset the board (banner seen)?
  - Does Hold → Reset leave the position intact with no alarm on the real 0.9j?
  - P3: probe input test open/touching (does the 0.9j board alarm on an already-triggered probe and clear with `$X`?); Z probe with and without a plate against a known height; a 5×4 map on a PCB blank; load the `.map` in Candle and a Candle `.map` here; cut a shallow test isolation pattern with the compensated file.
  - P4: air-cut a job of a few thousand short segments: no stutter (planner starvation) at 115200 baud; ETA against the actual time (±15 %); Pause/Resume and Stop mid-move (no alarm, position kept); an M6 pause, jog, Continue; start from a mid-program line (air) and check the entry moves; 0.9 software feed override takes effect after the buffered moves.
  - P5: air-cut, then kill the server (`kill -9`) mid-job and start it again: does the CH340 board reset on open even with `dtr=False`? Without a reset, is the matched line right and is the seamless resume clean? With a reset, follow the re-zero steps and resume (air). Unplug USB mid-job and replug. Run each default macro.
  - P2: measure 0.01/0.1/1/10 mm steps with a dial indicator (also checks `$100–$102`); a held key or mouse button moves one step only; five quick presses move exactly five steps; Esc mid-move (no alarm, offsets kept, position unchanged against a reference mark); `G10 L20` zeroing; go-to with safe Z; `M3 S…` speed scaling against `spindleMax` (0.9j build default 1000); keyboard jog in Chrome/Safari/Firefox.
- **DTR reset on server (re)start:** inherent to the CH340 + 328P board. It only matters if the server restarts mid-job; disabling auto-reset (a 10 µF cap RESET–GND) makes that case seamless too. Both paths are supported.
- **Software feed-override lag** on 0.9 is documented in the UI; it is not a safety control. Continuous jogging, the other overrun risk, is excluded by design (§5.4). The hardware E-stop remains the real stop.
- **LAN mode** exposes machine control to anyone with the token; it is off by default.
- **Python performance:** transforming very large jobs in pure Python takes seconds rather than milliseconds. It runs in a worker with progress events; numpy is not needed.
