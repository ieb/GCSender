"""Heightmap model, Candle ``.map`` (``candle map v3``) I/O and interpolation.

Semantics follow Candle so maps are interchangeable:

* The map covers the border rectangle ``x, y, w, h`` in *work* coordinates
  (mm). ``nx`` x ``ny`` probe points span it evenly; row 0 is the minimum Y.
* Every value is the probed Z relative to the reference probe taken at the
  map origin (``originX, originY``), so the origin reads 0.
* ``interpNx``/``interpNy`` are point *counts* of the interpolation grid; the
  spacing used to subdivide moves is ``w / (interpNx - 1)``.
* Interpolation is Candle's Catmull-Rom bicubic, identical in interior cells.
  Two deliberate differences: in edge cells the missing neighbour is
  extrapolated linearly (``2*p0 - p1``) instead of duplicated, so a tilted
  board is reproduced exactly up to the border (Candle errs by ~14 um on a
  0.6 mm / 30 mm tilt), and queries outside the border are clamped to it
  rather than extrapolated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SIGNATURE = "candle map v3"
NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")


class MapError(ValueError):
    pass


@dataclass
class MapParams:
    x: float = 0.0
    y: float = 0.0
    w: float = 50.0
    h: float = 30.0
    originX: float = 0.0
    originY: float = 0.0
    nx: int = 5
    ny: int = 4
    zBottom: float = -1.0
    zTop: float = 1.0
    feed: float = 20.0
    interpType: int = 0  # 0 = bicubic (the only type Candle offers)
    interpNx: int = 41
    interpNy: int = 31

    def validate(self) -> None:
        for k in ("x", "y", "w", "h", "originX", "originY", "zBottom", "zTop", "feed"):
            v = getattr(self, k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                raise MapError(f"{k} must be a number")
        for k in ("nx", "ny", "interpNx", "interpNy", "interpType"):
            if not isinstance(getattr(self, k), int) or isinstance(getattr(self, k), bool):
                raise MapError(f"{k} must be an integer")
        if self.w <= 0 or self.h <= 0:
            raise MapError("border width and height must be positive")
        if not (2 <= self.nx <= 100 and 2 <= self.ny <= 100):
            raise MapError("grid must be 2..100 points in each direction")
        if not (2 <= self.interpNx <= 1000 and 2 <= self.interpNy <= 1000):
            raise MapError("interpolation grid must be 2..1000 points in each direction")
        if self.zTop <= self.zBottom:
            raise MapError("Z top must be above Z bottom")
        if self.feed <= 0:
            raise MapError("probe feed must be positive")
        if self.interpType != 0:
            raise MapError("only bicubic interpolation (type 0) is supported")

    def grid_shape_key(self) -> tuple:
        return (self.x, self.y, self.w, self.h, self.nx, self.ny)

    def point(self, i: int, j: int) -> tuple[float, float]:
        """Work XY of probe point row i (Y), column j (X)."""
        return (self.x + self.w * j / (self.nx - 1), self.y + self.h * i / (self.ny - 1))

    @property
    def step(self) -> tuple[float, float]:
        """Interpolation spacing used to subdivide moves (mm)."""
        return (self.w / (self.interpNx - 1), self.h / (self.interpNy - 1))

    @classmethod
    def from_dict(cls, d: dict[str, Any], base: "MapParams | None" = None) -> "MapParams":
        p = MapParams(**asdict(base)) if base else MapParams()
        for k, v in d.items():
            if k not in cls.__dataclass_fields__:
                raise MapError(f"unknown parameter {k!r}")
            if k in ("nx", "ny", "interpNx", "interpNy", "interpType") and isinstance(v, float) and v.is_integer():
                v = int(v)
            setattr(p, k, v)
        p.validate()
        return p


@dataclass
class HeightMap:
    params: MapParams = field(default_factory=MapParams)
    values: list[list[float | None]] = field(default_factory=list)  # [row i (Y)][col j (X)]

    def __post_init__(self) -> None:
        if not self.values:
            self.reset_values()

    def reset_values(self) -> None:
        self.values = [[None] * self.params.nx for _ in range(self.params.ny)]

    # -------------------------------------------------------------- state

    @property
    def complete(self) -> bool:
        return all(v is not None for row in self.values for v in row)

    @property
    def probed(self) -> int:
        return sum(v is not None for row in self.values for v in row)

    def digest(self) -> str:
        blob = json.dumps({"p": asdict(self.params), "v": self.values}, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def to_json(self) -> dict[str, Any]:
        vals = [v for row in self.values for v in row if v is not None]
        return {
            "params": asdict(self.params),
            "values": self.values,
            "complete": self.complete,
            "probed": self.probed,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "digest": self.digest(),
        }

    def set_params(self, changes: dict[str, Any]) -> bool:
        """Apply parameter changes; returns True if probe data was reset."""
        new = MapParams.from_dict(changes, self.params)
        reset = new.grid_shape_key() != self.params.grid_shape_key()
        self.params = new
        if reset:
            self.reset_values()
        return reset

    def set_cell(self, i: int, j: int, z: float | None) -> None:
        if not (0 <= i < self.params.ny and 0 <= j < self.params.nx):
            raise MapError(f"cell ({i}, {j}) is outside the grid")
        if z is not None and (isinstance(z, bool) or not isinstance(z, (int, float)) or not math.isfinite(z)):
            raise MapError("cell value must be a number or null")
        self.values[i][j] = None if z is None else float(z)

    # -------------------------------------------------------- interpolation

    def height(self, x: float, y: float) -> float:
        """Bicubic height at work (x, y), clamped to the border. Requires a complete map."""
        p = self.params
        x = min(max(x, p.x), p.x + p.w)
        y = min(max(y, p.y), p.y + p.h)
        return bicubic(self.values, p.x, p.y, p.w, p.h, x, y)

    def inside(self, x: float, y: float, tol: float = 1e-6) -> bool:
        p = self.params
        return p.x - tol <= x <= p.x + p.w + tol and p.y - tol <= y <= p.y + p.h + tol

    def interpolated_grid(self) -> list[list[float]]:
        """Values on the interpNx x interpNy grid (for display); needs a complete map."""
        p = self.params
        out = []
        for i in range(p.interpNy):
            y = p.y + p.h * i / (p.interpNy - 1)
            out.append([round(self.height(p.x + p.w * j / (p.interpNx - 1), y), 5) for j in range(p.interpNx)])
        return out

    # ---------------------------------------------------------- .map file

    def to_map_text(self) -> str:
        p = self.params
        lines = [
            SIGNATURE,
            ";".join(_num(v) for v in (p.x, p.y, p.w, p.h)),
            ";".join(_num(v) for v in (p.originX, p.originY)),
            ";".join([str(p.nx), str(p.ny), _num(p.zBottom), _num(p.zTop), _num(p.feed)]),
            ";".join([str(p.interpType), str(p.interpNx), str(p.interpNy)]),
        ]
        for row in self.values:
            lines.append(";".join("nan" if v is None else _num(v, 4) for v in row))
        return "\r\n".join(lines) + "\r\n"

    @classmethod
    def from_map_text(cls, text: str) -> "HeightMap":
        lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        if not lines or lines[0].lstrip("\ufeff") != SIGNATURE:
            raise MapError(f"not a Candle heightmap (expected first line {SIGNATURE!r})")
        try:
            border = [_parse(v) for v in lines[1].split(";")]
            origin = [_parse(v) for v in lines[2].split(";")]
            grid = [_parse(v) for v in lines[3].split(";")]
            interp = [_parse(v) for v in lines[4].split(";")]
            params = MapParams(
                x=border[0], y=border[1], w=border[2], h=border[3],
                originX=origin[0], originY=origin[1],
                nx=int(grid[0]), ny=int(grid[1]), zBottom=grid[2], zTop=grid[3], feed=grid[4],
                interpType=int(interp[0]), interpNx=int(interp[1]), interpNy=int(interp[2]),
            )
        except (IndexError, ValueError) as e:
            raise MapError(f"malformed heightmap header: {e}") from None
        params.validate()
        values: list[list[float | None]] = []
        for i in range(params.ny):
            try:
                cells = lines[5 + i].split(";")
                row = []
                for j in range(params.nx):
                    v = _parse(cells[j])
                    row.append(None if math.isnan(v) else v)
            except (IndexError, ValueError):
                raise MapError(f"heightmap row {i} is missing or malformed") from None
            values.append(row)
        return cls(params, values)


def _num(v: float, decimals: int = 3) -> str:
    s = f"{v:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _parse(s: str) -> float:
    s = s.strip()
    if s.lower() in ("nan", "-nan", ""):
        return math.nan
    return float(s.replace(",", "."))  # tolerate a decimal comma from localised Candle builds


# ------------------------------------------------------------ interpolation


def _cubic(p: list[float], t: float) -> float:
    # Catmull-Rom, identical to Candle's Interpolation::cubicInterpolate.
    return p[1] + 0.5 * t * (p[2] - p[0] + t * (2.0 * p[0] - 5.0 * p[1] + 4.0 * p[2] - p[3] + t * (3.0 * (p[1] - p[2]) + p[3] - p[0])))


def bicubic(values: list[list[float | None]], bx: float, by: float, bw: float, bh: float, x: float, y: float) -> float:
    rows = len(values)
    cols = len(values[0])
    sx = bw / (cols - 1)
    sy = bh / (rows - 1)
    x -= bx
    y -= by
    ix = min(max(int(x / sx), 0), cols - 2)
    iy = min(max(int(y / sy), 0), rows - 2)
    tx = x / sx - ix
    ty = y / sy - iy

    def four(get, i: int, n: int) -> list[float]:
        # Neighbours i-1..i+2; beyond the edge, extrapolate linearly.
        mid = [get(i), get(i + 1)]
        if any(v is None for v in mid):
            raise MapError("heightmap is incomplete")
        lo = get(i - 1) if i > 0 else 2 * mid[0] - mid[1]
        hi = get(i + 2) if i < n - 2 else 2 * mid[1] - mid[0]
        if lo is None or hi is None:
            raise MapError("heightmap is incomplete")
        return [lo, mid[0], mid[1], hi]

    def row_value(r: int) -> float:
        row = values[r]
        return _cubic(four(lambda c: row[c], ix, cols), tx)

    return _cubic(four(row_value, iy, rows), ty)


# ------------------------------------------------------------- persistence


class MapLibrary:
    """Named ``.map`` files in ``<state>/maps`` plus the current working map."""

    def __init__(self, root: Path | None):
        self.root = root
        if root is not None:
            (root / "maps").mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name) or name.startswith("."):
            raise MapError("map names may use letters, digits, space, '_', '-' and '.' (max 64)")
        if self.root is None:
            raise MapError("no state directory")
        return self.root / "maps" / f"{name}.map"

    def names(self) -> list[dict[str, Any]]:
        if self.root is None:
            return []
        out = []
        for f in sorted((self.root / "maps").glob("*.map")):
            out.append({"name": f.stem, "modified": f.stat().st_mtime, "size": f.stat().st_size})
        return out

    def save(self, name: str, hm: HeightMap) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(hm.to_map_text(), encoding="utf-8", newline="")
        tmp.replace(path)

    def load(self, name: str) -> HeightMap:
        path = self._path(name)
        if not path.exists():
            raise MapError(f"no map named {name!r}")
        return HeightMap.from_map_text(path.read_text(encoding="utf-8"))

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.exists():
            raise MapError(f"no map named {name!r}")
        path.unlink()

    # The working map survives server restarts.
    def save_current(self, hm: HeightMap, name: str | None) -> None:
        if self.root is None:
            return
        cur = self.root / "heightmap-current.map"
        tmp = cur.with_suffix(".tmp")
        tmp.write_text(hm.to_map_text(), encoding="utf-8", newline="")
        tmp.replace(cur)
        (self.root / "heightmap-current.json").write_text(json.dumps({"name": name}), encoding="utf-8")

    def load_current(self) -> tuple[HeightMap | None, str | None]:
        if self.root is None:
            return None, None
        cur = self.root / "heightmap-current.map"
        if not cur.exists():
            return None, None
        try:
            hm = HeightMap.from_map_text(cur.read_text(encoding="utf-8"))
            meta = json.loads((self.root / "heightmap-current.json").read_text(encoding="utf-8"))
            return hm, meta.get("name")
        except (OSError, ValueError):
            return None, None
