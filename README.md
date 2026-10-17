# GCSender

A Candle-style G-code sender for GRBL 0.9j and 1.1, aimed at a C3018 desktop CNC mill.
A small local Python server owns the serial port. The browser UI (plain HTML/CSS/JS, no dependencies) talks to it
over HTTP and server-sent events, so reloading or closing the page never interrupts the machine.

See [docs/PLAN.md](docs/PLAN.md) for the full plan, feature inventory and phases.

**Status:** phases 0–3 are complete, verified on the simulator only; the hardware check is still deferred.
- Working: connection and firmware detection (fresh boot or re-attach), live status, and the console with error help.
- Control: Hold, Resume, Reset (with offset and parser-state restore), Unlock and Check.
- Jogging: step only. Each click or key press moves exactly 0.01, 0.05, 0.1, 0.5, 1, 5 or 10 mm. A long press is always one step (no auto-repeat), and there is no hold-to-move mode. Keys are remappable and Esc stops. There is an optional jog box.
- Coordinates: G54–G59 select/zero, G92, and go-to with safe Z. Spindle and coolant controls.
- Probing: Z probe (plate, two-stage), probe-input test for GRBL 0.9.
- Heightmaps: probe a grid, edit values, save/load a library, import/export Candle `.map` files, and compensate a G-code file on the server (download and 3D preview).
- 3D view: grid, axes, tool and trail, heightmap surface and the compensated path, with orbit/pan/zoom and preset views.
- Settings: GRBL `$$` editor with warnings for `$10`/`$13`, and app settings.
- Logs: application log and serial transcripts.

Job upload and streaming come in phase 4.

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

### Options
| Option | Meaning |
|---|---|
| `--port PORT` | Connect at start-up (`sim` = simulator) |
| `--baud N` | Baud rate (default 115200) |
| `--http HOST:PORT` | Listen address (default `127.0.0.1:8080`) |
| `--lan` | Allow other devices on the network. Prints a URL containing the access token |
| `--allow-host NAME` | Extra `Host` name to accept (e.g. a DNS name for the machine) |
| `--simulate 0.9j\|1.1f` | Provide a simulated controller as port `sim` |
| `--sim-no-reset`, `--sim-speed X` | Simulator: no reset when the port opens / time scale |
| `--state-dir DIR` | Settings and serial transcripts (default `~/.gcsender`) |

## Security
The server can move a machine, so:
- It listens on localhost only unless `--lan` is given.
- Every API call needs a random per-run token. The page served to localhost gets it automatically; LAN devices use
  the printed `?t=` URL.
- Requests with a foreign `Host` or `Origin`, and non-JSON form posts, are rejected.

## Development
```sh
uv run pytest            # server + simulator tests
```
Layout: `server/gcsender/` (Python package), `server/tests/`, `web/` (the SPA, served as static files), `docs/`.

Dependencies:
- Runtime: `pyserial`.
- Development: `pytest`.
- uv is configured with `no-build = true` and `exclude-newer = "7 days"`, and `uv.lock` is committed.
