"""The plugin's config, read at CALL time so a Settings edit applies without a restart.

``register()`` hands us ``registry.live_config`` (falling back to the register-time snapshot on a
host without it); tests call :func:`configure` with a plain dict.
"""

from __future__ import annotations

import re
from typing import Any, Callable

DEFAULTS: dict[str, Any] = {
    "data_dirs": "",
    "row_cap": 200,
    "chart_row_cap": 5000,
    "timeout_s": 20,
    "memory_limit": "1GB",
    "export_row_cap": 1_000_000,
}

_SOURCE: Callable[[], dict] | None = None


def configure(source: Callable[[], dict] | dict | None) -> None:
    global _SOURCE
    if isinstance(source, dict):
        snap = dict(source)
        _SOURCE = lambda: snap  # noqa: E731
    else:
        _SOURCE = source


def cfg() -> dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        got = _SOURCE() if _SOURCE else {}
    except Exception:  # noqa: BLE001 — a broken host read must not break a tool
        got = {}
    for k, v in (got or {}).items():
        if v is not None and v != "":
            out[k] = v
    return out


# Hard ceilings the engine never exceeds, whatever a setting says. `memory_limit` and `timeout_s`
# are operator-only (`spawns: true`), but a host older than that marker — or a hand-edited YAML —
# still can't push a query past these: memory spills to disk, and spill is capped separately.
MEMORY_MIN_MB = 64
MEMORY_MAX_MB = 8 * 1024
TIMEOUT_MAX_S = 120
SPILL_MAX = "1GB"  # DuckDB max_temp_directory_size: a query that would spill more fails instead
_UNITS = {"": 1, "b": 1 / 2**20, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1, "mib": 1, "gb": 1024, "gib": 1024}
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$", re.IGNORECASE)


def memory_limit() -> str:
    """The validated ``memory_limit`` as DuckDB syntax (``"<n>MB"``), clamped to sane bounds.

    Anything unparseable (``"lots"``) falls back to the default instead of reaching DuckDB, where a
    bad value made every query fail with a ParserException."""
    raw = str(cfg().get("memory_limit") or "")
    m = _MEM_RE.match(raw)
    unit = (m.group(2) or "").lower() if m else ""
    if not m or unit not in _UNITS:
        m, unit = _MEM_RE.match(str(DEFAULTS["memory_limit"])), "gb"
    mb = float(m.group(1)) * _UNITS[unit]
    return f"{int(max(MEMORY_MIN_MB, min(MEMORY_MAX_MB, mb)))}MB"


def timeout_s() -> float:
    return float(int_setting("timeout_s", 1, TIMEOUT_MAX_S))


def int_setting(name: str, lo: int, hi: int) -> int:
    try:
        n = int(float(cfg().get(name)))
    except (TypeError, ValueError):
        n = int(DEFAULTS[name])
    return max(lo, min(hi, n))
