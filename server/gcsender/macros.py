"""User commands (macros), run by the server (docs/PLAN.md §3.10).

A macro is a named block of G-code lines with a small template language:

* ``{expr}`` is replaced by the value of an arithmetic expression over
  variables: ``wx wy wz`` (work position), ``mx my mz`` (machine position),
  ``prbx prby prbz`` (last probe contact, machine coordinates) and
  ``safez`` (the Safe Z setting). Operators ``+ - * /``, parentheses and
  ``min() max() abs()``. Example: ``G0 Z{max(safez, wz + 2)}``.
* A line that uses a position variable first waits until the machine is
  Idle, so the value is where the machine actually is.
* ``wait idle`` waits until every queued move has finished.
* Blank lines and comments (``;`` or ``( )``) are skipped.

Lines are sent one at a time and acknowledged before the next; an error ends
the macro, which is finished once the machine is Idle again. While a macro runs the controller is busy (``macro``): console,
jog, probe and job start are refused. Stop (or Reset) aborts it.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .errors import ControllerError

log = logging.getLogger(__name__)

MAX_MACROS = 60
MAX_NAME = 40
MAX_TEXT = 4000
TEMPLATE = re.compile(r"\{([^{}]*)\}")
POSITION_VARS = {"wx", "wy", "wz", "mx", "my", "mz", "prbx", "prby", "prbz"}
VARS = POSITION_VARS | {"safez"}
FUNCS: dict[str, Callable[..., float]] = {"min": min, "max": max, "abs": abs}

DEFAULT_MACROS = [
    {"name": "Z up 5 mm", "text": "; raise Z by 5 mm from where it is\nG91 G0 Z5\nG90"},
    {"name": "Park", "text": "; safe Z (or stay higher), then work X0 Y0\nG90 G0 Z{max(safez, wz)}\nG0 X0 Y0"},
    {"name": "Spindle warm-up", "text": "; 30 s warm-up in two steps\nM3 S300\nG4 P10\nM3 S700\nG4 P20\nM5"},
]


class MacroError(ValueError):
    pass


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def evaluate(expr: str, variables: dict[str, float | None]) -> float:
    """Evaluate a template expression with a whitelist of AST nodes."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        raise MacroError(f"cannot read {{{expr}}}") from None

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise MacroError(f"unknown variable '{node.id}' (known: {', '.join(sorted(VARS))})")
            v = variables[node.id]
            if v is None:
                raise MacroError(f"'{node.id}' is not known yet")
            return float(v)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            x = ev(node.operand)
            return -x if isinstance(node.op, ast.USub) else x
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if b == 0:
                raise MacroError("division by zero")
            return a / b
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS
            and node.args and not node.keywords
        ):
            return float(FUNCS[node.func.id](*[ev(a) for a in node.args]))
        raise MacroError(f"not allowed in {{{expr}}}")

    return ev(tree)


def expand(line: str, variables: dict[str, float | None]) -> str:
    return TEMPLATE.sub(lambda m: _fmt(evaluate(m.group(1), variables)), line)


def uses_position(line: str) -> bool:
    return any(set(re.findall(r"[a-z]+", m.group(1))) & POSITION_VARS for m in TEMPLATE.finditer(line))


def strip_comment(line: str) -> str:
    """Remove ``( )`` and ``;`` comments, but not parentheses inside ``{...}``."""
    out = []
    depth = 0  # inside {...}
    comment = False
    for c in line:
        if comment:
            comment = c != ")"
            continue
        if depth == 0 and c == ";":
            break
        if depth == 0 and c == "(":
            comment = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        out.append(c)
    return "".join(out).strip()


def validate(macros: Any) -> list[dict[str, str]]:
    if not isinstance(macros, list) or len(macros) > MAX_MACROS:
        raise MacroError(f"macros must be a list of at most {MAX_MACROS}")
    out = []
    seen = set()
    for m in macros:
        if not isinstance(m, dict):
            raise MacroError("each macro needs a name and text")
        name = str(m.get("name", "")).strip()
        text = str(m.get("text", ""))
        if not name or len(name) > MAX_NAME:
            raise MacroError(f"a macro name must be 1-{MAX_NAME} characters")
        if len(text) > MAX_TEXT:
            raise MacroError(f"'{name}' is longer than {MAX_TEXT} characters")
        if any(ord(c) > 0x7E or (ord(c) < 0x20 and c not in "\n\r\t") for c in text):
            raise MacroError(f"'{name}' may contain only printable ASCII")
        for raw in text.splitlines():
            code = strip_comment(raw)
            if not code or code.lower() == "wait idle":
                continue
            for expr in TEMPLATE.findall(code):
                evaluate(expr, dict.fromkeys(VARS, 1.0))  # syntax and names only
            if code in ("?", "!", "~"):
                raise MacroError(f"'{name}': real-time commands are not allowed in macros")
        mid = str(m.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{8}", mid) or mid in seen:
            mid = secrets.token_hex(4)
        seen.add(mid)
        out.append({"id": mid, "name": name, "text": text.replace("\r\n", "\n")})
    return out


class MacroStore:
    def __init__(self, path: Path | None):
        self.path = path
        self._macros: list[dict[str, str]] | None = None

    def list(self) -> list[dict[str, str]]:
        if self._macros is None:
            data = None
            if self.path is not None and self.path.exists():
                try:
                    data = validate(json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, ValueError) as e:
                    log.warning("ignoring unreadable macros file %s: %s", self.path, e)
            self._macros = data if data is not None else validate(DEFAULT_MACROS)
        return list(self._macros)

    def save(self, macros: Any) -> list[dict[str, str]]:
        clean = validate(macros)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        self._macros = clean
        return list(clean)

    def get(self, mid: str) -> dict[str, str]:
        for m in self.list():
            if m["id"] == mid:
                return m
        raise MacroError("no such macro")


class MacroRunner:
    def __init__(self, controller: Any, store: MacroStore):
        self.ctl = controller
        self.store = store
        self._abort = False
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": None, "name": None, "line": None, "error": None, "message": None}
        controller.stop_hooks.append(self._on_stop)
        self._publish_list()
        self._publish()

    def _publish(self) -> None:
        self.ctl.bus.publish("macro", dict(self.state), retain=True)

    def _publish_list(self) -> None:
        self.ctl.bus.publish("macros", self.store.list(), retain=True)

    def save(self, macros: Any) -> list[dict[str, str]]:
        out = self.store.save(macros)
        self._publish_list()
        return out

    def _on_stop(self) -> None:
        self._abort = True

    def variables(self) -> dict[str, float | None]:
        st = self.ctl.model.status or {}
        w = st.get("wpos") or [None] * 3
        m = st.get("mpos") or [None] * 3
        prb = self.ctl.model.offsets.get("PRB")
        p = prb.get("value") if isinstance(prb, dict) else None
        p = p or [None] * 3
        return {
            "wx": w[0], "wy": w[1], "wz": w[2],
            "mx": m[0], "my": m[1], "mz": m[2],
            "prbx": p[0], "prby": p[1], "prbz": p[2],
            "safez": float(self.ctl.settings.get("safeZ")),
        }

    def run(self, mid: str) -> dict[str, Any]:
        macro = self.store.get(mid)
        if self.ctl.connection.get("state") != "connected":
            raise ControllerError("not connected")
        if self.ctl.busy:
            raise ControllerError(f"busy: {self.ctl.busy}")
        if self._thread is not None and self._thread.is_alive():
            raise ControllerError("a macro is already running")
        self._abort = False
        self.ctl.busy = "macro"
        self.state = {"running": mid, "name": macro["name"], "line": None, "error": None, "message": None}
        self._publish()
        self._thread = threading.Thread(target=self._run, args=(macro,), name="macro", daemon=True)
        self._thread.start()
        return dict(self.state)

    def _wait_idle(self, timeout: float = 600.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._abort:
                raise MacroError("stopped")
            st = self.ctl.wait_status(lambda s: True, timeout=1.0)
            streamer = self.ctl._streamer
            if st is not None and st["state"] == "Idle" and (streamer is None or not streamer.inflight):
                return
            if st is not None and st["state"] == "Alarm":
                raise MacroError("the machine is in alarm")
        raise MacroError("timed out waiting for Idle")

    def _run(self, macro: dict[str, str]) -> None:
        try:
            for n, raw in enumerate(macro["text"].splitlines(), 1):
                if self._abort:
                    raise MacroError("stopped")
                code = strip_comment(raw)
                if not code:
                    continue
                self.state["line"] = n
                if code.lower() == "wait idle":
                    self._wait_idle()
                    continue
                if uses_position(code):
                    self._wait_idle()
                line = expand(code, self.variables())
                self.state["message"] = line
                self._publish()
                r = self.ctl.command(line, source="macro", timeout=600)
                if r["status"] != "ok":
                    raise MacroError(f"line {n} ({line}): {r['response']}")
            self._wait_idle()  # done means the machine has finished the macro's moves
            self.state.update(running=None, message=f"'{macro['name']}' done")
        except (MacroError, ControllerError) as e:
            self.state.update(running=None, error=str(e), message=f"'{macro['name']}' stopped: {e}")
            log.info("macro %s: %s", macro["name"], e)
        except Exception as e:  # pragma: no cover - defensive
            log.exception("macro crashed")
            self.state.update(running=None, error=f"internal error: {e}")
        finally:
            self.ctl.busy = None
            self._publish()
