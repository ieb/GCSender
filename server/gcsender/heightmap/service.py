"""Server-side owner of the working heightmap: edits, library, events, transform cache."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Any

from ..events import EventBus
from .model import HeightMap, MapError, MapLibrary, MapParams
from .transform import bounds, transform

CACHE_SIZE = 4


class HeightmapService:
    def __init__(self, bus: EventBus, library: MapLibrary):
        self.bus = bus
        self.library = library
        hm, name = library.load_current()
        self.hm = hm or HeightMap()
        self.name: str | None = name
        self.dirty = False
        self.locked_by: str | None = None  # "probing" while a probe run owns the map
        self._lock = threading.RLock()
        self._cache: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self.publish()

    # ------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {**self.hm.to_json(), "name": self.name, "dirty": self.dirty}

    def publish(self) -> None:
        self.bus.publish("heightmap", self.state(), retain=True)

    def _changed(self, dirty: bool = True) -> None:
        self.dirty = dirty
        self.library.save_current(self.hm, self.name)
        self.publish()

    def _check_unlocked(self) -> None:
        if self.locked_by:
            raise MapError(f"the heightmap is in use ({self.locked_by})")

    # ------------------------------------------------------------- edits

    def set_params(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._check_unlocked()
            reset = self.hm.set_params(changes)
            self._changed()
            return {"reset": reset}

    def set_cells(self, cells: list[dict[str, Any]]) -> None:
        with self._lock:
            self._check_unlocked()
            for c in cells:
                if not isinstance(c, dict):
                    raise MapError("cells must be objects {i, j, z}")
                i, j = c.get("i"), c.get("j")
                if not isinstance(i, int) or not isinstance(j, int) or isinstance(i, bool) or isinstance(j, bool):
                    raise MapError("cell indices must be integers")
                self.hm.set_cell(i, j, c.get("z"))
            self._changed()

    def clear_values(self) -> None:
        with self._lock:
            self._check_unlocked()
            self.hm.reset_values()
            self._changed()

    def auto_border(self, text: str, margin: float = 0.0) -> dict[str, Any]:
        """Set the border to the program's cutting extent (plus margin)."""
        b = bounds(text)
        if b is None:
            raise MapError("no moves with known positions found in the program")
        x0, y0 = b["min"][0] - margin, b["min"][1] - margin
        w = max(b["max"][0] - b["min"][0] + 2 * margin, 1.0)
        h = max(b["max"][1] - b["min"][1] + 2 * margin, 1.0)
        changes: dict[str, Any] = {"x": round(x0, 3), "y": round(y0, 3), "w": round(w, 3), "h": round(h, 3)}
        p = self.hm.params
        ox, oy = p.originX, p.originY
        if not (x0 <= ox <= x0 + w and y0 <= oy <= y0 + h):
            changes.update(originX=round(x0, 3), originY=round(y0, 3))
        result = self.set_params(changes)
        return {**result, "bounds": b, "border": changes}

    # --------------------------------------------- probing (Prober only)

    def begin_probe(self, owner: str) -> MapParams:
        with self._lock:
            self._check_unlocked()
            self.locked_by = owner
            self.hm.reset_values()
            self.name = None
            self._changed()
            return self.hm.params

    def record(self, i: int, j: int, z: float) -> None:
        with self._lock:
            self.hm.set_cell(i, j, round(z, 4))
            self._changed()

    def end_probe(self) -> None:
        with self._lock:
            self.locked_by = None
            self.publish()

    # ----------------------------------------------------------- library

    def save(self, name: str) -> None:
        with self._lock:
            self.library.save(name, self.hm)
            self.name = name
            self._changed(dirty=False)

    def load(self, name: str) -> None:
        with self._lock:
            self._check_unlocked()
            self.hm = self.library.load(name)
            self.name = name
            self._changed(dirty=False)

    def delete(self, name: str) -> None:
        with self._lock:
            self.library.delete(name)
            if self.name == name:
                self.name = None
            self.publish()

    def import_text(self, text: str, name: str | None = None) -> None:
        with self._lock:
            self._check_unlocked()
            self.hm = HeightMap.from_map_text(text)
            self.name = name
            self._changed()

    def export_text(self) -> str:
        with self._lock:
            return self.hm.to_map_text()

    def interpolated(self) -> dict[str, Any]:
        with self._lock:
            if not self.hm.complete:
                raise MapError("the heightmap has unprobed points")
            return {"digest": self.hm.digest(), "params": self.state()["params"], "grid": self.hm.interpolated_grid()}

    # --------------------------------------------------------- transform

    def transform(self, text: str, arc_tolerance: float = 0.01) -> dict[str, Any]:
        with self._lock:
            hm = self.hm
            if not hm.complete:
                raise MapError("the heightmap has unprobed points; probe or edit it first")
            key = (hashlib.sha256(text.encode()).hexdigest(), hm.digest(), arc_tolerance)
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        # Transform outside the lock: it is CPU work on immutable inputs.
        snapshot = HeightMap(MapParams(**hm.to_json()["params"]), [list(r) for r in hm.values])
        try:
            result = transform(text, snapshot, arc_tolerance)
        except ValueError as e:
            raise MapError(str(e)) from None
        with self._lock:
            self._cache[key] = result
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result
