"""Firmware capability flags (docs/PLAN.md §2.1).

Everything else consults these flags rather than the version string.
"""

from __future__ import annotations

import re
from typing import Any

CAPS_09: dict[str, Any] = {
    "family": "0.9",
    "statusFormat": "comma",
    "jog": "g91",
    "overrides": "software-feed",
    "probePin": False,
    "liveFeedSpindle": False,
    "holdSubstate": False,
    "sleep": False,
    "door": "@",
    "coolantToggle": False,
    "errorFormat": "text",
    "parserStateFormat": "bare",
    "laserMode": False,
    "plannerBlocks": 17,
    "rxBuffer": 128,
    "lineLength": 80,
}

CAPS_11: dict[str, Any] = {
    "family": "1.1",
    "statusFormat": "pipe",
    "jog": "$J",
    "overrides": "realtime",
    "probePin": True,
    "liveFeedSpindle": True,
    "holdSubstate": True,
    "sleep": True,
    "door": "0x84",
    "coolantToggle": True,
    "errorFormat": "numeric",
    "parserStateFormat": "GC",
    "laserMode": True,
    "plannerBlocks": 15,
    "rxBuffer": 128,
    "lineLength": 80,
}


def for_version(version: str | None) -> dict[str, Any] | None:
    """Capabilities for a version string like '0.9j', '1.1f', '0.9j.20160726'."""
    if not version:
        return None
    if version.startswith("0."):
        caps = dict(CAPS_09)
    elif version.startswith("1."):
        caps = dict(CAPS_11)
    else:
        return None
    m = re.match(r"\d+\.\d+[a-z]?", version)
    caps["version"] = m.group(0) if m else version
    return caps


def for_status_format(fmt: str | None) -> dict[str, Any] | None:
    """Fallback detection from a status report when no banner was seen."""
    if fmt == "0.9":
        caps = dict(CAPS_09)
    elif fmt == "1.1":
        caps = dict(CAPS_11)
    else:
        return None
    caps["version"] = None  # refined later via $I when idle
    return caps
