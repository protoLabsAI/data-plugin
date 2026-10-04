"""Where this plugin keeps its own state, and where exports land.

Places that are all OURS (never a source directory):

* the **store** — ``sources.json`` (the registered sources). On a host it is
  ``sdk.plugin_store(plugin_id="data")``, instance-scoped; ``DATA_PLUGIN_DIR`` overrides it (tests).
* the **runtime dir** — everything the ENGINE can see and print: ``cache/`` (disposable Parquet
  snapshots of SQLite tables and Excel sheets), ``spill/`` (DuckDB's temp dir) and inert DuckDB
  home/secret/extension dirs, under an opaque ``<system temp>/pa-data-<hash>`` (see runtime_dir).
* the **export dir** — ``<agent workspace>/data-exports``: exports are files the operator and the
  agent's other tools are meant to pick up, so they go to the workspace rather than the store.
  ``DATA_EXPORT_DIR`` overrides it (tests); a host without ``infra.paths`` falls back to the store.

And one place that is NOT ours but the operator's: the **default data folder**,
``<agent workspace>/data`` (see :func:`default_data_dir`) — a source folder every agent gets, which
the fence allowlists on top of ``data_dirs``. Exports never land in it (``data-exports`` is its
sibling, and data_export refuses a destination inside it).
"""

from __future__ import annotations

import hashlib
import os
import stat
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


def runtime_dir() -> Path:
    """Every path the ENGINE can see — spill, snapshots, DuckDB's home/secret/extension dirs —
    lives here: ``<system temp>/pa-data-<hash of the store path>``.

    DuckDB will print its settings to anyone's SQL (``duckdb_settings()``, ``current_setting()``,
    or the same through ``query()``), so rather than trying to block every route to them, the
    values themselves reveal nothing: no home dir, no username, no instance layout — just an
    opaque name. The hash is deterministic, so every process of this instance (the server and
    the operator-MCP process) shares one cache. Private (0700) and must be ours; if the name is
    taken by someone else, it falls back to the store (still correct, just less opaque).
    Snapshots here are disposable: a missing one is rebuilt on its next use."""
    store = store_dir()
    name = "pa-data-" + hashlib.sha256(str(store).encode()).hexdigest()[:16]
    p = Path(tempfile.gettempdir()).resolve() / name
    try:
        p.mkdir(mode=0o700, exist_ok=True)
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise OSError("not a plain directory")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise OSError("owned by another user")
        os.chmod(p, 0o700)
    except OSError:
        p = store / "runtime"
        p.mkdir(parents=True, exist_ok=True)
    return p


def _sub(name: str) -> Path:
    p = runtime_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    return _sub("cache")


def spill_dir() -> Path:
    return _sub("spill")


def duckdb_home() -> Path:
    """Inert home/secret/extension dirs, so DuckDB never resolves (or prints) the real ``~``."""
    return _sub("duckdb")


def sources_file() -> Path:
    return store_dir() / "sources.json"


def _workspace() -> Path | None:
    """The agent's workspace — core's ``infra.paths.workspace_dir`` (``<instance root>/workspace``,
    instance-scoped through ``PROTOAGENT_HOME``; ``PROTOAGENT_WORKSPACE`` moves it), the same dir
    the filesystem tools are fenced to. None with no host (tests, standalone)."""
    try:
        from infra.paths import workspace_dir  # host import — lazy

        return Path(workspace_dir(create=True))
    except Exception:  # noqa: BLE001 — no host, or an older one
        return None


def default_data_dir(*, create: bool = False) -> Path | None:
    """The agent's own data folder: ``<agent workspace>/data`` — always allowlisted (unless the
    operator turns ``use_default_folder`` off), so a fresh agent has somewhere to drop files.
    ``DATA_DEFAULT_DIR`` overrides it (tests). None when there's no host to ask. NOT resolved
    here: the fence checks the folder itself isn't a symlink before trusting it."""
    raw = os.environ.get("DATA_DEFAULT_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
    else:
        ws = _workspace()
        if ws is None:
            return None
        p = ws / "data"
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return p


def export_dir() -> Path:
    raw = os.environ.get("DATA_EXPORT_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
    else:
        ws = _workspace()
        # No host (tests) or an older one: the store.
        p = ws / "data-exports" if ws is not None else store_dir() / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()
