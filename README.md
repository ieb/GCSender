# GCSender

A Candle-style G-code sender for GRBL 0.9j and 1.1, aimed at a C3018 desktop CNC mill.
A small local Python server owns the serial port. The browser UI (plain HTML/CSS/JS, no dependencies) talks to it
over HTTP and server-sent events, so reloading or closing the page never interrupts the machine.

See [docs/PLAN.md](docs/PLAN.md) for the full plan, feature inventory and phases.

**Status:** phases 0–6 are complete and verified on the simulator and on the C3018 (GRBL 0.9j): two PCBs cut without problems.
- Working: connection and firmware detection (fresh boot or re-attach), live status, and the console with error help.
- Control: Hold, Resume, Door, Reset (with offset and parser-state restore), Unlock and Check.
- Jogging: step only. Each click or key press moves exactly 0.01, 0.05, 0.1, 0.5, 1, 5 or 10 mm. A long press is always one step (no auto-repeat), and there is no hold-to-move mode. Keys are remappable and Esc stops. There is an optional jog box.
- Coordinates: G54–G59 select/zero, G92, and go-to with safe Z. Spindle and coolant controls.
- Probing: Z probe (plate, two-stage), probe-input test (open) for GRBL 0.9, and Go XY0 from the probe panel.
- Heightmaps: probe a grid, edit values, save/load a library, import/export Candle `.map` files, and compensate a G-code file on the server (download and 3D preview).
- Jobs: upload (button or drag-drop) to a server-side library, then Start / Pause / Stop, start from any line (safe entry), M6 tool-change pause (jog and probe allowed), error hold with Continue/Stop, start/end commands, and feed override (software on 0.9, real-time feed/rapid/spindle on 1.1). The server streams the job, so a page reload shows the job exactly where it is. Run the heightmap-compensated version with one checkbox.
- Recovery: a journal lets a restarted server (or a reconnect after a lost USB link) resume an interrupted job: seamlessly when the board was not reset, or after a guided re-zero with a safe entry when it was.
- User commands: buttons for server-run macros with `{wx}`/`{prbz}`-style values, simple arithmetic and `wait idle`.
- Notifications: desktop notifications and a marked tab title when a job finishes or needs you while the tab is in the background (🔔 in the top bar).
- 3D view: grid, axes, tool and trail, the program (feed, rapids, plunges) coloured by progress, start/end markers and the selected line, the heightmap surface, and the compensated path, with orbit/pan/zoom and preset views.
- Settings: GRBL `$$` editor with warnings for `$10`/`$13` and a `$$` backup download; app settings with defaults, named profiles and export/import; remappable shortcuts (including hold, resume, job pause, views and user commands 1–9).
- Display: light/dark/system theme, toolpath colours and 3D options, collapsible panels and draggable dividers (with a layout lock), larger touch targets on tablets, and a Help dialog. Works on desktop and tablet widths.
- Logs: application log and serial transcripts.

All planned phases are done and checked on the machine; what remains is the *Later* backlog in [docs/PLAN.md](docs/PLAN.md).

**Jogging is step only, by design.** Continuous (hold-to-move) jogging is deliberately not provided: GRBL 0.9 cannot cancel a jog,
so any overrun would go into the workpiece. Separate presses queue and run in order; holding a key or button never produces more than one step.

## Requirements
- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- A modern browser (any; Web Serial is **not** used)

## Run
```sh
uv sync --locked

# With the built-in simulator (no machine needed):
uv run gcsender --simulate 0.9j            # or 1.1f; add --sim-no-reset to emulate a board without DTR auto-reset

# With the real machine:
uv run gcsender --port /dev/tty.usbserial-XXXX     # or pick the port in the UI
```
Then open the printed URL (default <http://127.0.0.1:8080/>).

### Using it from another computer (LAN)
Create a user once, then start the server with `--lan`; other computers open `http://<host>:8080/` and log in:

```sh
uv run gcsender --add-user ian        # asks for a password (8+ characters); run again to change it
uv run gcsender --lan --port /dev/tty.usbserial-XXXX
```

| Option | Meaning |
|---|---|
| `--add-user NAME` | Create a user or set a new password (ends that user's sessions). With `--password-stdin` the password is read from stdin |
| `--remove-user NAME` | Delete a user; their sessions end at once |
| `--list-users` | Users and how many sessions each has |

These change `<state-dir>/users.json` and take effect on a running server. A login lasts until **Log out**, 7 days
without use, or 30 days. The browser on the server's own computer needs no login.

**Only the server's own computer controls the machine.** Browsers on other computers are *view only* (a 👁 badge):
they see everything live, can upload programs and zips (which wait in the library rather than replacing what is
loaded) and rename or reorder sets, and can report a problem. Connecting, jogging, zeroing, probing, running jobs,
user commands and settings are refused by the server. Opening GCSender on the server's computer through its LAN
address also counts as local. The rule depends on where a request comes from, not on the credential, so the API
token used from another computer is view only too. Do not put GCSender behind a reverse proxy on the same computer:
every request would then look local.

### Options
| Option | Meaning |
|---|---|
| `--port PORT` | Connect at start-up (`sim` = simulator) |
| `--baud N` | Baud rate (default 115200) |
| `--http HOST:PORT` | Listen address (default `127.0.0.1:8080`) |
| `--lan` | Allow other devices on the network. Prints a URL containing the access token |
| `--token TOKEN` | Use this API token instead of a random one at each start, so bookmarks and scripts keep working. 16–256 of `A-Z a-z 0-9 - _ . ~` |
| `--token-file FILE` | Read the token from a file (keeps it out of `ps` and shell history). The `GCSENDER_TOKEN` environment variable also works; precedence: `--token`, `--token-file`, `GCSENDER_TOKEN`, random |
| `--allow-host NAME` | Extra `Host` name to accept (e.g. a DNS name for the machine) |
| `--simulate 0.9j\|1.1f` | Provide a simulated controller as port `sim` |
| `--sim-no-reset`, `--sim-speed X` | Simulator: no reset when the port opens / time scale |
| `--sim-surface SPEC` | Simulator: the virtual workpiece that the probe touches, e.g. `preset=bent` or `base=-25,tiltX=0.005,bump=0.4` |

### Probing in the simulator
The simulator has a virtual workpiece surface. Its probe input closes when the tool tip reaches the surface, so `G38.2`, Z probing and heightmaps work without hardware.
- **Default:** about 30 mm below where the simulator starts, in machine coordinates, with a gentle 0.3 mm warp.
- **Where to change it:** the **Simulator · virtual surface** panel in the *Probe & heightmap* tab (shown only with `--simulate`), or `--sim-surface` at start-up.
- **What you can set:** height, tilt X/Y, warp amplitude/period in X and Y, and a bump (height, radius, centre).
- **Presets:** `gentle`, `flat`, `tilted`, `bent`, `bump`.
- **Probe input:** `auto` (follows the surface), `touching` (as if the clip is held to the plate) or `open`.
- **Readout:** the surface height under the tool. **5 mm above surface** moves there, ready for *Probe Z*.

A quick tour: *5 mm above surface* → *Probe Z* → set a map border → *Probe map*. Add `--sim-speed 10` to make it ten times faster.

### Running a job
1. **Open…** (or drop a file on the Program panel) uploads it to the server's library (`<state-dir>/jobs`); **Library…** reloads earlier uploads.
   **A board as a set:** upload the `.zip` of a PCB's programs. Every G-code file in it is kept together as a *set*
   named after the zip (a second upload of the same name becomes "name (2)", so nothing clashes). Gerber, Excellon,
   notes and `__MACOSX` files are skipped. The set is ordered back → align → front → drill → cut-out (by words in the
   file names), then by name; fix it with ↑ ↓ in **Library…**, where you can also rename or delete it. Each program
   shows how its last run ended. When one finishes, **Next: …** in the Program panel loads the next one.
2. Tick **heightmap** to stream the compensated program instead (recomputed at Start if the map has changed).
3. **Start** runs from line 1, or from the **from line** number (click a table row to pick one). Starting mid-program restores the modal state, raises to safe Z, moves to the start XY, starts the spindle with a dwell, and plunges at no more than the *entry plunge feed*.
4. Progress in the table and 3D view: grey is done, violet is in GRBL's buffers. The ETA comes from a model of GRBL's planner using your `$110–$122` and `$11`, corrected as the job runs.
5. **On an M6** the job waits for the machine to stop, runs the *tool-change commands* (default `M5`), and asks. You can jog and probe Z, then **Continue**. **On an M0** (program pause; many CAM tools write a tool change as `(MSG, Change tool…)` + `M0`) the job pauses the same way and shows the message. **M1** (optional stop) does too, unless turned off. **On an error** the machine is feed-held and you choose Continue or Stop. Both are set in *Settings → Application → Jobs*.

The simulator moves at constant speed (no acceleration), so its run times are shorter than the estimate. On the real machine the estimate includes acceleration.

### If the server stops mid-job
The server keeps a journal (`<state-dir>/journal.json`). When it starts again (or the USB link comes back) the job shows as **interrupted** with a recovery card:
- **The board was not reset** (it re-attached): the machine finishes the moves it had buffered. The server then finds the line whose end matches where the machine stopped and offers **Resume here**, which continues with no extra moves.
- **The board was reset** (the usual case when the port reopens on a CH340 board): the position is lost. Re-establish work zero (XY reference, Probe Z; with a heightmap, at the map origin), tick the two boxes, then **Resume with safe entry** from the suggested line (or any line).
- **Discard** forgets the interrupted run; the program stays loaded.

In simulate mode, **Simulate server restart** (Probe & heightmap tab) tries this without killing anything. Start with `--sim-no-reset` to see the re-attach path.

### User commands
*Machine* tab → **User commands** → *edit*. Each command is G-code, one line per row. `{expr}` inserts a value:
- **Variables:** `wx wy wz` (work position), `mx my mz` (machine position), `prbx prby prbz` (last probe contact) and `safez`.
- **Arithmetic:** `+ - * /` and `min max abs`, e.g. `G0 Z{max(safez, wz + 2)}`.

`wait idle` waits for the moves to finish. Esc / Stop aborts a running command.

### Reporting a problem
Press **🐞 Report**, say what you did and what went wrong, and press **Create report**. A zip is saved in
`<state-dir>/bugreports` (newest 100 are kept). It contains:
- the machine state, `$$`, offsets, the job and its progress, and the commands in GRBL's buffer;
- recent events and status updates, every server thread's stack, and the application log (`<state-dir>/logs/gcsender.log`);
- the last 5000 lines of the serial transcript, your settings, user commands, journal and heightmap;
- optionally the loaded program;
- from the browser: errors, failed requests, the buttons and keys pressed (never what is typed into fields) and what was on screen.

Tokens are never included. The same dialog lists earlier reports to download or delete.

**Fetching reports from another computer.** Start the server with `--lan`. The dialog (and the start-up output) shows
a URL containing a separate **reports token**. That token is kept in `<state-dir>/reports-token` (delete the file to
change it) and can *only* read bug reports, so it can be shared without giving control of the machine:

```sh
curl -o reports.zip "http://<host>:8080/api/bugreports/archive?t=<reports-token>"            # all reports
curl -H "X-GCS-Token: <reports-token>" http://<host>:8080/api/bugreports                     # list (JSON)
curl -o r.zip -H "X-GCS-Token: <reports-token>" http://<host>:8080/api/bugreports/<id>       # one report
```
| `--state-dir DIR` | Settings and serial transcripts (default `~/.gcsender`) |

## Security
The server can move a machine, so:
- It listens on localhost only unless `--lan` is given.
- **Other computers log in** with a user name and password (scrypt-hashed in `users.json`). The server then sets an
  HttpOnly, SameSite=Strict session cookie: the page never holds a key, and scripts in it cannot read the cookie.
  Changes made with a session must carry the `X-GCS-Request: 1` header, which another website cannot send.
  Failed logins from one address are slowed down after 5 attempts.
- **The API token** (random per run, or fixed with `--token` / `--token-file` / `GCSENDER_TOKEN`) is for the page on
  the server's own computer (embedded automatically) and for scripts, in the `X-GCS-Token` header. From another
  computer it is accepted only in that header, never in a `?t=` URL, and it is no longer printed. Anyone with it can
  move the machine: if you fix it, use a long random value kept in a file readable only by you (`chmod 600`).
- Requests with a foreign `Host` or `Origin`, and non-JSON form posts, are rejected.
- The bug-reports token (see *Reporting a problem*) only allows `GET /api/bugreports…`.

## Development
```sh
uv run pytest            # server + simulator tests
```
Layout: `server/gcsender/` (Python package), `server/tests/`, `web/` (the SPA, served as static files), `docs/`.

Dependencies:
- Runtime: `pyserial`.
- Development: `pytest`.
- uv is configured with `no-build = true` and `exclude-newer = "7 days"`, and `uv.lock` is committed.
