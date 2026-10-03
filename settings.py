"""The plugin's config, read at CALL time so a Settings edit applies without a restart.

``register()`` hands us ``registry.live_config`` (falling back to the register-time snapshot on a
host without it); tests call :func:`configure` with a plain dict.
"""

from __future__ import annotations

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


def int_setting(name: str, lo: int, hi: int) -> int:
    try:
        n = int(float(cfg().get(name)))
    except (TypeError, ValueError):
        n = int(DEFAULTS[name])
    return max(lo, min(hi, n))
