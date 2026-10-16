# GCSender

A Candle-style G-code sender for GRBL 0.9j and 1.1, aimed at a C3018 desktop CNC mill.
A small local Python server owns the serial port. The browser UI (plain HTML/CSS/JS, no dependencies) talks to it
over HTTP and server-sent events, so reloading or closing the page never interrupts the machine.

See [docs/PLAN.md](docs/PLAN.md) for the full plan, feature inventory and phases.

**Status:** phases 0–1 are complete, verified on the simulator only; the hardware check is still deferred.
- Working: connection and firmware detection (fresh boot or re-attach), live status, and the console with error help.
- Control: Hold, Resume, Reset (with offset and parser-state restore), Unlock and Check.
- Settings: GRBL `$$` editor with warnings for `$10`/`$13`, and app settings.
- Logs: application log and serial transcripts.

Jogging, probing, heightmaps and jobs come in later phases.

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
