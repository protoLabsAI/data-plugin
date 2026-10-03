"""data — Data Analyst: local-first data analysis over your own files (ADR 0116).

Point the agent at local CSV / TSV / Parquet / JSON / Excel / SQLite files inside the operator's
allowlisted ``data_dirs``; it explores them with READ-ONLY SQL on an embedded DuckDB (querying the
files in place — no server, no import step), profiles them, exports results to the workspace, and
charts them in the console's Artifact panel as live Vega-Lite charts. The model writes a few lines
of SQL plus a small spec; the plugin inlines the rows and hands them to the panel through core's
``artifact.show`` service (``graph.sdk.service``) — the artifact plugin is never imported.

``register(registry)`` is the only place plugin code runs (ADR 0018). Host-only imports stay
inside functions so the suite runs with no protoAgent present.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.data")

__version__ = "0.1.0"


def _host_store(registry) -> str:
    """The host's instance-scoped plugin dir (sdk.plugin_store), or '' on older hosts / tests."""
    try:
        from graph import sdk  # host import — lazy

        return str(sdk.plugin_store(plugin_id=getattr(registry, "plugin_id", None) or "data"))
    except Exception:  # noqa: BLE001 — fall back to paths.py's own default
        return ""


def register(registry) -> None:
    # Imported here, not at module top: pytest imports a rootdir __init__.py without a parent
    # package, where a top-level relative import can't resolve.
    from . import fence, paths, settings, tools

    try:
        paths.configure(_host_store(registry))
        live = getattr(registry, "live_config", None)
        settings.configure(live if callable(live) else (lambda: dict(getattr(registry, "config", None) or {})))
        roots, notes = fence.roots(settings.cfg().get("data_dirs"))
        for n in notes:
            log.warning("[data] %s", n)
        gap = getattr(registry, "report_setup_gap", None)
        if callable(gap):  # a config reload re-runs register(), which clears or re-raises it
            gap(
                "data_dirs",
                None
                if roots
                else "Data Analyst has no data folders yet — add the folders the agent may read in its settings.",
                action={"kind": "plugin_config", "fields": ["data_dirs"]},
            )
    except Exception:
        log.exception("[data] configuring failed")

    try:
        import duckdb  # noqa: F401 — the one hard dependency; say so instead of failing every call
    except ImportError:
        gap = getattr(registry, "report_setup_gap", None)
        if callable(gap):
            gap(
                "duckdb",
                "Data Analyst needs the `duckdb` Python package — use Install dependencies.",
                action={"kind": "install_deps"},
            )
    registry.register_tools(tools.TOOLS)
    try:
        registry.register_skill_dir("skills")
    except Exception:
        log.exception("[data] registering skills failed")
