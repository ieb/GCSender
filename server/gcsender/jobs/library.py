"""Job library on disk (``<state>/jobs``) and loaded-job variants."""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyze import Scanner, bounds_of, clean_line
from .estimate import Machine, estimate

MAX_JOB_BYTES = 50 << 20
ID_RE = re.compile(r"^[0-9a-f]{12}$")
M6_RE = re.compile(r"M0*6(?![0-9.])")
PAUSE_RE = re.compile(r"M0*([01])(?![0-9.])")  # M0 program pause, M1 optional stop
MSG_RE = re.compile(r"\(\s*MSG\s*,\s*(.*?)\s*\)", re.IGNORECASE)
MSG_LOOKBACK = 10


def find_pauses(raw: list[str], cmds: list[str]) -> list[dict[str, Any]]:
    """M0/M1 lines with the operator message: a ``(MSG, ...)`` comment on the
    line or on one of the few lines before it (CAM tools put it just before)."""
    out: list[dict[str, Any]] = []
    for i, c in enumerate(cmds):
        m = PAUSE_RE.search(c)
        if not m:
            continue
        message = None
        lo = max(out[-1]["line"] + 1 if out else 0, i - MSG_LOOKBACK)
        for j in range(i, lo - 1, -1):
            mm = MSG_RE.search(raw[j])
            if mm:
                message = mm.group(1)
                break
        out.append({"line": i, "code": f"M{m.group(1)}", "message": message})
    return out


class JobError(ValueError):
    pass


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w .\-()]+", "_", name or "").strip(" .") or "program.nc"
    return name[:120]


@dataclass
class Variant:
    """One streamable text (original, or heightmap-transformed)."""

    kind: str  # original | transformed
    raw: list[str]  # display lines (exact text)
    cmds: list[str]  # what is streamed per line ("" = skipped)
    seconds: list[float] = field(default_factory=list)
    cum: list[float] = field(default_factory=list)  # cumulative seconds *before* line i
    bounds: dict[str, Any] | None = None
    too_long: list[int] = field(default_factory=list)
    m6_lines: list[int] = field(default_factory=list)
    pauses: list[dict[str, Any]] = field(default_factory=list)  # M0/M1 with their (MSG, ...)
    g92: bool = False
    digest: str = ""
    map_digest: str | None = None  # transformed: heightmap it was built from
    stats: dict[str, Any] | None = None  # transformed: transform stats

    @classmethod
    def build(cls, kind: str, text: str, machine: Machine, max_line: int, **extra: Any) -> "Variant":
        raw = text.splitlines()
        cmds = [clean_line(r) for r in raw]
        v = cls(kind, raw, cmds, **extra)
        v.digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        v.too_long = [i for i, c in enumerate(cmds) if len(c) > max_line][:100]
        v.m6_lines = [i for i, c in enumerate(cmds) if M6_RE.search(c)]
        v.pauses = find_pauses(raw, cmds)
        v.seconds = estimate(cmds, machine)
        total = 0.0
        v.cum = []
        for s in v.seconds:
            v.cum.append(total)
            total += s
        v.cum.append(total)
        events = []
        sc = Scanner()
        for i, c in enumerate(cmds):
            events.extend(ev for ev in sc.feed_line(i, c) if ev.kind == "move")
        v.bounds = bounds_of(events)
        v.g92 = sc.m.g92_used
        return v

    @property
    def total(self) -> float:
        return self.cum[-1] if self.cum else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lines": len(self.raw),
            "digest": self.digest,
            "eta": round(self.total, 1),
            "bounds": self.bounds,
            "tooLong": self.too_long,
            "m6Lines": self.m6_lines[:100],
            "pauses": self.pauses[:100],
            "g92": self.g92,
            "mapDigest": self.map_digest,
            "stats": self.stats,
        }


GCODE_EXT = {".nc", ".ngc", ".gcode", ".gc", ".g", ".tap", ".cnc", ".gco", ".ncc", ".nga"}
NOT_GCODE_EXT = {  # Gerber, Excellon, CAD and documents that come with a PCB
    ".gbr", ".ger", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gko", ".gm1", ".gml",
    ".drl", ".xln", ".exc", ".drd", ".kicad_pcb", ".kicad_sch", ".kicad_pro", ".sch", ".brd", ".pdf", ".png",
    ".jpg", ".jpeg", ".svg", ".md", ".json", ".csv", ".html", ".zip", ".rpt", ".pos",
}
MAX_ZIP_FILES = 200
MAX_ZIP_TOTAL = 200 << 20  # uncompressed
# Default order inside a set, for a PCB: back, alignment holes, front, drill, cut-out.
ORDER_HINTS = (
    ("back", "bottom", "b_cu", "b.cu", "bcu", "bot"),
    ("align",),
    ("front", "top", "f_cu", "f.cu", "fcu"),
    ("drill", "drl", "hole"),
    ("cutout", "cut_out", "edge", "outline", "profile"),
)


def looks_like_gcode(text: str) -> bool:
    """Most code lines start with a G-code word (for .txt and unknown extensions)."""
    if re.search(r"^M48\b", text[:4000], re.MULTILINE):
        return False  # an Excellon drill file (its header starts with M48)
    lines = [ln.strip() for ln in text.splitlines()[:400]]
    lines = [ln for ln in lines if ln and not ln.startswith(("(", ";", "%"))]
    if not lines:
        return False
    hits = sum(1 for ln in lines if re.match(r"^(N\d+\s*)?[GMXYZFSTIJKP][-+.\d]", ln, re.IGNORECASE))
    return hits / len(lines) >= 0.8


def _natural(s: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def order_key(path: str) -> tuple[int, list[Any]]:
    """Rank by the first hint found as a word of the file name ("alignment" starts
    with "align"); hints with punctuation (b_cu) match anywhere in the name."""
    base = path.rsplit("/", 1)[-1].lower()
    base = base.rsplit(".", 1)[0] if "." in base else base
    words = [w for w in re.split(r"[^a-z0-9]+", base) if w]
    for rank, hints in enumerate(ORDER_HINTS):
        for h in hints:
            if (h in base) if not h.isalnum() else any(w.startswith(h) for w in words):
                return rank, _natural(path)
    return len(ORDER_HINTS), _natural(path)


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if "\x00" in text:
        raise JobError("this does not look like a G-code text file")
    return text


class JobLibrary:
    """Programs (``jobs/<id>.nc`` + ``.json``) and sets of programs uploaded
    together as a zip (``jobs/sets/<id>.json``: name and file order)."""

    def __init__(self, root: Path | None):
        self.root = root / "jobs" if root is not None else None
        self._mem: dict[str, tuple[dict[str, Any], str]] = {}  # used without a state dir (tests)
        self._mem_sets: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        if self.root is not None:
            (self.root / "sets").mkdir(parents=True, exist_ok=True)

    def _paths(self, job_id: str) -> tuple[Path, Path]:
        if not ID_RE.match(job_id):
            raise JobError("invalid job id")
        assert self.root is not None
        return self.root / f"{job_id}.nc", self.root / f"{job_id}.json"

    def _set_path(self, set_id: str) -> Path:
        if not ID_RE.match(set_id):
            raise JobError("invalid set id")
        assert self.root is not None
        return self.root / "sets" / f"{set_id}.json"

    # -- programs -----------------------------------------------------------------

    def _store(self, meta: dict[str, Any], text: str) -> None:
        if self.root is None:
            self._mem[meta["id"]] = (meta, text)
            return
        nc, js = self._paths(meta["id"])
        nc.write_text(text, encoding="utf-8", newline="")
        js.write_text(json.dumps(meta), encoding="utf-8")

    def _save_meta(self, meta: dict[str, Any]) -> None:
        if self.root is None:
            if meta["id"] in self._mem:
                self._mem[meta["id"]] = (meta, self._mem[meta["id"]][1])
            return
        _, js = self._paths(meta["id"])
        if js.exists():
            js.write_text(json.dumps(meta), encoding="utf-8")

    def add(self, name: str, data: bytes, *, set_id: str | None = None, path: str | None = None) -> dict[str, Any]:
        if len(data) > MAX_JOB_BYTES:
            raise JobError("program larger than 50 MB")
        text = _decode(data)
        meta: dict[str, Any] = {
            "id": secrets.token_hex(6),
            "name": safe_name(name),
            "size": len(data),
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
            "uploaded": time.time(),
        }
        if set_id:
            meta["set"] = set_id
            meta["path"] = path or meta["name"]
        with self._lock:
            self._store(meta, text)
        return meta

    def list(self) -> list[dict[str, Any]]:
        if self.root is None:
            items = [m for m, _ in self._mem.values()]
        else:
            items = []
            for js in self.root.glob("*.json"):
                try:
                    items.append(json.loads(js.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return sorted(items, key=lambda m: m.get("uploaded", 0), reverse=True)

    def get(self, job_id: str) -> tuple[dict[str, Any], str]:
        if self.root is None:
            if job_id not in self._mem:
                raise JobError("no such job")
            return self._mem[job_id]
        nc, js = self._paths(job_id)
        if not nc.exists() or not js.exists():
            raise JobError("no such job")
        return json.loads(js.read_text(encoding="utf-8")), nc.read_text(encoding="utf-8")

    def delete(self, job_id: str) -> None:
        with self._lock:
            try:
                meta, _ = self.get(job_id)
            except JobError:
                meta = None
            if self.root is None:
                self._mem.pop(job_id, None)
            else:
                for p in self._paths(job_id):
                    p.unlink(missing_ok=True)
            if meta and meta.get("set"):
                try:
                    st = self._read_set(meta["set"])
                except JobError:
                    return
                st["files"] = [f for f in st["files"] if f != job_id]
                if st["files"]:
                    self._write_set(st)
                else:
                    self._remove_set_record(st["id"])

    def note_result(self, job_id: str, result: dict[str, Any]) -> None:
        """Remember how the last run of a program ended (shown in the library)."""
        with self._lock:
            try:
                meta, _ = self.get(job_id)
            except JobError:
                return
            meta["lastRun"] = result
            self._save_meta(meta)

    # -- sets ---------------------------------------------------------------------

    def _read_set(self, set_id: str) -> dict[str, Any]:
        if self.root is None:
            if set_id not in self._mem_sets:
                raise JobError("no such set")
            return dict(self._mem_sets[set_id])
        p = self._set_path(set_id)
        if not p.exists():
            raise JobError("no such set")
        return json.loads(p.read_text(encoding="utf-8"))

    def _write_set(self, st: dict[str, Any]) -> None:
        if self.root is None:
            self._mem_sets[st["id"]] = dict(st)
            return
        self._set_path(st["id"]).write_text(json.dumps(st), encoding="utf-8")

    def _remove_set_record(self, set_id: str) -> None:
        if self.root is None:
            self._mem_sets.pop(set_id, None)
        else:
            self._set_path(set_id).unlink(missing_ok=True)

    def _set_records(self) -> list[dict[str, Any]]:
        if self.root is None:
            return [dict(s) for s in self._mem_sets.values()]
        out = []
        for p in (self.root / "sets").glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def _unique_set_name(self, name: str) -> str:
        taken = {s.get("name") for s in self._set_records()}
        if name not in taken:
            return name
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    def sets(self) -> list[dict[str, Any]]:
        """Sets, newest first, each with its programs' metadata in order."""
        metas = {m["id"]: m for m in self.list()}
        out = []
        for st in self._set_records():
            files = [metas[f] for f in st.get("files", []) if f in metas]
            out.append({**st, "files": files})
        return sorted(out, key=lambda s: s.get("uploaded", 0), reverse=True)

    def add_zip(self, name: str, data: bytes) -> dict[str, Any]:
        """Store every G-code file of a zip as one set. Non-G-code is skipped and reported."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise JobError("this is not a valid zip file") from None
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise JobError(f"the zip has more than {MAX_ZIP_FILES} files")
        if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
            raise JobError("the zip expands to more than 200 MB")
        set_id = secrets.token_hex(6)
        accepted: list[tuple[str, bytes]] = []
        skipped: list[dict[str, str]] = []
        for info in infos:
            path = info.filename.replace("\\", "/")
            base = path.rsplit("/", 1)[-1]
            if path.startswith("__MACOSX/") or base.startswith(".") or not base:
                continue  # macOS resource forks, hidden files
            ext = ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""
            if info.flag_bits & 0x1:
                skipped.append({"path": path, "reason": "encrypted"})
                continue
            if info.file_size > MAX_JOB_BYTES:
                skipped.append({"path": path, "reason": "larger than 50 MB"})
                continue
            if ext in NOT_GCODE_EXT:
                skipped.append({"path": path, "reason": "zip inside the zip" if ext == ".zip" else "not G-code"})
                continue
            with zf.open(info) as f:
                raw = f.read(MAX_JOB_BYTES + 1)
            if len(raw) > MAX_JOB_BYTES:
                skipped.append({"path": path, "reason": "larger than 50 MB"})
                continue
            try:
                text = _decode(raw)
            except JobError:
                skipped.append({"path": path, "reason": "not a text file"})
                continue
            if ext not in GCODE_EXT and not looks_like_gcode(text):
                skipped.append({"path": path, "reason": "not G-code"})
                continue
            accepted.append((path, raw))
        if not accepted:
            raise JobError("no G-code files found in the zip" + (f" (skipped: {', '.join(s['path'] for s in skipped[:5])})" if skipped else ""))
        accepted.sort(key=lambda a: order_key(a[0]))
        # A folder that wraps everything (board/...) is noise: show paths below it.
        prefix = ""
        if len(accepted) > 0 and all("/" in p for p, _ in accepted):
            first = accepted[0][0].split("/", 1)[0] + "/"
            if all(p.startswith(first) for p, _ in accepted):
                prefix = first
        stem = re.sub(r"\.zip$", "", name or "set", flags=re.IGNORECASE)
        with self._lock:
            metas = [
                self.add(p[len(prefix):].rsplit("/", 1)[-1], raw, set_id=set_id, path=p[len(prefix):])
                for p, raw in accepted
            ]
            st = {
                "id": set_id,
                "name": self._unique_set_name(safe_name(stem)),
                "source": safe_name(name or "set.zip"),
                "uploaded": time.time(),
                "files": [m["id"] for m in metas],
            }
            self._write_set(st)
        return {"set": {**st, "files": metas}, "skipped": skipped}

    def update_set(self, set_id: str, name: str | None = None, order: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            st = self._read_set(set_id)
            if name is not None:
                name = safe_name(name)
                if name != st["name"]:
                    st["name"] = self._unique_set_name(name)
            if order is not None:
                if not isinstance(order, list) or sorted(order) != sorted(st["files"]):
                    raise JobError("the new order must list exactly the set's files")
                st["files"] = list(order)
            self._write_set(st)
        return next(s for s in self.sets() if s["id"] == set_id)

    def delete_set(self, set_id: str) -> list[str]:
        """Delete a set and its programs; returns the deleted program ids."""
        with self._lock:
            st = self._read_set(set_id)
            for f in st.get("files", []):
                if self.root is None:
                    self._mem.pop(f, None)
                else:
                    for p in self._paths(f):
                        p.unlink(missing_ok=True)
            self._remove_set_record(set_id)
            return list(st.get("files", []))
