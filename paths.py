"""Where this plugin keeps its own state, and where exports land.

Three places, all OURS (never a source directory):

* the **store** — ``sources.json`` (the registered sources) plus ``cache/`` (Parquet snapshots of
  SQLite tables and Excel sheets) and ``spill/`` (DuckDB's private temp dir). On a host it is
  ``sdk.plugin_store(plugin_id="data")``, instance-scoped; ``DATA_PLUGIN_DIR`` overrides it (tests).
* the **export dir** — ``<agent workspace>/data-exports``: exports are files the operator and the
  agent's other tools are meant to pick up, so they go to the workspace rather than the store.
  ``DATA_EXPORT_DIR`` overrides it (tests); a host without ``infra.paths`` falls back to the store.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_STORE: str = ""


def configure(store_dir: str = "") -> None:
    """The host's plugin store (``sdk.plugin_store``), or '' to fall back."""
    global _STORE
    _STORE = str(store_dir or "")


def store_dir() -> Path:
    raw = os.environ.get("DATA_PLUGIN_DIR", "").strip() or _STORE
    p = Path(raw).expanduser() if raw else Path(tempfile.gettempdir()) / "protoagent-data-plugin"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def cache_dir() -> Path:
    p = store_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def spill_dir() -> Path:
    p = store_dir() / "spill"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sources_file() -> Path:
    return store_dir() / "sources.json"


def export_dir() -> Path:
    raw = os.environ.get("DATA_EXPORT_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
    else:
        try:
            from infra.paths import workspace_dir  # host import — lazy

            p = Path(workspace_dir(create=True)) / "data-exports"
        except Exception:  # noqa: BLE001 — no host (tests) or an older one: the store
            p = store_dir() / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()
