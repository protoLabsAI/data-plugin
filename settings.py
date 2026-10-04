"""The plugin's config, read at CALL time so a Settings edit applies without a restart.

``register()`` hands us ``registry.live_config`` (falling back to the register-time snapshot on a
host without it); tests call :func:`configure` with a plain dict.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

DEFAULTS: dict[str, Any] = {
    "data_dirs": "",
    "use_default_folder": True,
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


def merged(conf: dict | None) -> dict[str, Any]:
    """DEFAULTS ⊕ ``conf``'s set (non-blank) values."""
    out = dict(DEFAULTS)
    for k, v in (conf or {}).items():
        if v is not None and v != "":
            out[k] = v
    return out


def cfg() -> dict[str, Any]:
    try:
        got = _SOURCE() if _SOURCE else {}
    except Exception:  # noqa: BLE001 — a broken host read must not break a tool
        got = {}
    return merged(got)


# Hard ceilings the engine never exceeds, whatever a setting says. `memory_limit` and `timeout_s`
# are operator-only (`spawns: true`), but a host older than that marker — or a hand-edited YAML —
# still can't push a query past these: memory spills to disk, and spill is capped separately.
MEMORY_MIN_MB = 64
MEMORY_MAX_MB = 8000  # 8GB in DuckDB's decimal units
TIMEOUT_MAX_S = 120
SPILL_MAX = "1GB"  # DuckDB max_temp_directory_size: a query that would spill more fails instead
# DuckDB's size syntax: a number + an optional unit (K/M/G/T, with or without B, decimal or the
# binary KiB/MiB/GiB/TiB). A bare number is MB here (the setting's documented unit).
_UNITS_MB = {
    "": 1, "b": 1 / 1e6, "byte": 1 / 1e6, "bytes": 1 / 1e6,
    "k": 1e-3, "kb": 1e-3, "m": 1, "mb": 1, "g": 1e3, "gb": 1e3, "t": 1e6, "tb": 1e6,
    "kib": 1024 / 1e6, "mib": 2**20 / 1e6, "gib": 2**30 / 1e6, "tib": 2**40 / 1e6,
}  # fmt: skip
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$", re.IGNORECASE)
_log = logging.getLogger("protoagent.plugins.data")
_WARNED: set[str] = set()


def _clamp_note(note: str) -> None:
    """Log a clamp/reset once per distinct note (it recurs on every call while the setting stands)."""
    if note not in _WARNED:
        _WARNED.add(note)
        _log.warning("[data] %s", note)


def _memory() -> tuple[str, str | None]:
    raw = str(cfg().get("memory_limit") or "").strip()
    m = _MEM_RE.match(raw)
    unit = (m.group(2) or "").lower() if m else ""
    if not m or unit not in _UNITS_MB:
        return "1000MB", f"memory_limit {raw!r} isn't a size (e.g. 512MB, 2G) — using 1GB"
    mb = float(m.group(1)) * _UNITS_MB[unit]
    if mb < MEMORY_MIN_MB:
        return f"{MEMORY_MIN_MB}MB", f"memory_limit {raw} clamped to {MEMORY_MIN_MB}MB (min)"
    if mb > MEMORY_MAX_MB:
        return "8GB", f"memory_limit {raw} clamped to 8GB (max)"
    return f"{int(round(mb))}MB", None


def memory_limit() -> str:
    """The validated ``memory_limit`` as DuckDB syntax, clamped to [64MB, 8GB]. Accepts DuckDB's
    size syntax (``512MB``, ``512M``, ``2G``, ``2GB``, ``1TB``, ``1.5GiB``); a value that isn't a
    size uses 1GB rather than reaching DuckDB, where it failed every query. A clamp is reported
    (log + :func:`clamp_notes`), never silent."""
    val, note = _memory()
    if note:
        _clamp_note(note)
    return val


def _timeout() -> tuple[float, str | None]:
    raw = cfg().get("timeout_s")
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULTS["timeout_s"]), f"timeout_s {raw!r} isn't a number — using {DEFAULTS['timeout_s']}s"
    if n > TIMEOUT_MAX_S:
        return float(TIMEOUT_MAX_S), f"timeout clamped to {TIMEOUT_MAX_S}s (max)"
    if n < 1:
        return 1.0, "timeout clamped to 1s (min)"
    return n, None


def timeout_s() -> float:
    val, note = _timeout()
    if note:
        _clamp_note(note)
    return val


def clamp_notes() -> list[str]:
    """What the current settings were clamped or reset to — appended to tool results, so the
    agent (and the operator reading along) knows the engine isn't running on the value set."""
    out = [n for _, n in (_memory(), _timeout()) if n]
    for n in out:
        _clamp_note(n)
    return out


def flag(name: str, conf: dict | None = None) -> bool:
    """A boolean setting — a real bool, or the strings a YAML hand-edit / form round-trip gives.
    Read from ``conf`` (merged over DEFAULTS) when given, else the live config."""
    v = (merged(conf) if conf is not None else cfg()).get(name)
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def int_setting(name: str, lo: int, hi: int) -> int:
    try:
        n = int(float(cfg().get(name)))
    except (TypeError, ValueError):
        n = int(DEFAULTS[name])
    return max(lo, min(hi, n))


# ── the "no data folders" setup gap ─────────────────────────────────────────
# register() reports it, but it must also follow a Settings save: core re-runs register() on
# Save & apply, yet BEFORE it commits the new config — so inside register() ``live_config()``
# still returns the OLD values (protoAgent server/agent_init.py ``_reload_langgraph_agent``).
# register() therefore judges the gap from its registry's fresh ``config`` snapshot, and the
# tools re-sync it from the live config whenever they compute the allowlist (a reload that
# reuses the plugin bundle never calls register() at all).

GAP_KEY = "data_dirs"
GAP_MESSAGE = (
    "Data Analyst has no data folders — the default data folder is off and Data folders is empty. "
    "Add the folders the agent may read, or turn the default folder back on."
)
_GAP: Callable[..., Any] | None = None
_GAP_STATE: bool | None = None  # last reported: True = gap raised, False = cleared


def set_gap_reporter(fn: Callable[..., Any] | None) -> None:
    global _GAP, _GAP_STATE
    _GAP, _GAP_STATE = (fn if callable(fn) else None), None


def sync_gap(has_folders: bool) -> None:
    """Raise or clear the setup gap — only when its state changes (it's called per tool call)."""
    global _GAP_STATE
    want = not has_folders
    if _GAP is None or _GAP_STATE is want:
        return
    try:
        _GAP(
            GAP_KEY,
            GAP_MESSAGE if want else None,
            action={"kind": "plugin_config", "fields": ["data_dirs", "use_default_folder"]},
        )
        _GAP_STATE = want
    except Exception:  # noqa: BLE001 — a banner must never break a tool
        _log.exception("[data] reporting the setup gap failed")
