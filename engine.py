"""The read-only query engine: a FRESH, locked, in-memory DuckDB connection per call.

The DuckDB code itself — the read-only rules, the SELECT guard, the caps — lives in ``duck.py``.
This module picks WHERE it runs:

* **in-process**, when ``duckdb`` is importable in the host (a source/server install, where
  ``plugin install-deps`` pips it into the host's environment);
* otherwise as a **worker in the managed Python runtime** (``sdk.managed_python_exe()``) — the
  packaged desktop app, whose frozen host can't take a compiled dependency, installs plugin deps
  there instead. One JSON request in, one JSON reply out, a fresh process per call (the same
  isolation a fresh connection already gave).

Either way the caller sees the same ``Result`` and the same ``QueryError`` messages.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import duck, paths, settings
from .duck import QueryError, allowed_files, guard, ident, q, reader  # noqa: F401 — re-exported

_first_line = duck.first_line

#: Seconds a worker gets on top of the query's own time cap: interpreter start + ``import duckdb``.
WORKER_SLACK_S = 30.0

INSTALL_HINT = (
    "Data Analyst needs the `duckdb` Python package — use Install dependencies "
    "(Settings ▸ Plugins ▸ Data Analyst), or `plugin install-deps data`."
)
RUNTIME_HINT = (
    "Data Analyst runs DuckDB in the managed Python runtime on the desktop app, and it isn't "
    "provisioned yet — install it under Settings ▸ Tools (Python runtime), then use Install "
    "dependencies on Data Analyst."
)


def env() -> dict:
    """What the DuckDB side needs from this plugin's paths and settings."""
    home = paths.duckdb_home()
    return {
        "spill": str(paths.spill_dir()),
        "home": str(home),
        "spill_max": settings.SPILL_MAX,
        "memory_limit": settings.memory_limit(),
        "threads": max(1, min(4, os.cpu_count() or 1)),
    }


def in_process() -> bool:
    """True when ``duckdb`` imports in THIS process (checked per call — a just-run Install
    dependencies takes effect without a restart)."""
    try:
        return importlib.util.find_spec("duckdb") is not None
    except (ImportError, ValueError):
        return False


def worker_python() -> str | None:
    """The interpreter a worker runs under: the managed Python runtime, else — on a source run
    only — this process's own interpreter. None on a frozen app with no runtime provisioned."""
    try:
        from graph import sdk  # host import — lazy

        exe = sdk.managed_python_exe()
    except Exception:  # noqa: BLE001 — no host (tests) or a host without the seam
        exe = None
    if exe:
        return str(exe)
    if not getattr(sys, "frozen", False):
        return sys.executable
    return None


def _worker_env() -> dict[str, str]:
    # Scrubbed like execute_code's child: no gateway keys or auth tokens, and no PYTHONHOME /
    # PYTHONPATH from a frozen parent pointing the runtime at the wrong stdlib.
    keep = ("PATH", "TMPDIR", "TEMP", "TMP", "SystemRoot", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
    out = {k: os.environ[k] for k in keep if k in os.environ}
    out["PYTHONUNBUFFERED"] = "1"
    out["PYTHONIOENCODING"] = "utf-8"
    return out


def call(op: str, *, timeout_s: float, **req: Any) -> Any:
    """Run one DuckDB operation where duckdb lives. Raises ``QueryError`` with a model-facing
    message for every failure — a refusal, a timeout, or a missing/broken engine."""
    payload = {"op": op, "env": env(), "timeout_s": float(timeout_s), **req}
    if in_process():
        return duck.dispatch(json.loads(json.dumps(payload)))
    exe = worker_python()
    if exe is None:
        raise QueryError(RUNTIME_HINT)
    try:
        proc = subprocess.run(
            [exe, "-I", str(Path(duck.__file__).resolve())],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=float(timeout_s) + WORKER_SLACK_S,
            env=_worker_env(),
        )
    except subprocess.TimeoutExpired:
        raise QueryError(
            f"Query timed out after {timeout_s:g}s — narrow it (filter, aggregate, LIMIT) and try again."
        ) from None
    except OSError as e:
        raise QueryError(f"Couldn't start the query engine ({exe}): {_first_line(e)}") from None
    try:
        reply = json.loads(proc.stdout or "")
    except ValueError:
        err = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        raise QueryError(f"The query engine crashed: {err[-1][:300] if err else f'exit {proc.returncode}'}") from None
    if "missing" in reply:
        raise QueryError(INSTALL_HINT)
    if "error" in reply:
        raise QueryError(str(reply["error"]))
    return duck.decode(reply.get("ok"))


@dataclass
class Result:
    columns: list[str]
    types: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed: float


def open_locked(sources: list[dict], *, extra_paths: Iterable[str | Path] = ()):
    """An in-process locked connection (tests, and callers that know duckdb is importable)."""
    return duck.open_locked(env(), sources, extra_paths=extra_paths)


def referenced(sources: list[dict], sql: str) -> list[dict]:
    """The sources ``sql`` names (as a whole word, any case) — the only ones a query needs a view for.

    Creating a view binds its reader, which sniffs the file, so a view per CONNECTED source cost
    every query ~2 s at the 200-file cap. Narrowing also narrows ``allowed_paths``: a query can
    reach only the sources it names. Over-matching (a name inside a string literal) is harmless;
    a source the SQL doesn't name simply isn't there, and DuckDB says so."""
    low = (sql or "").lower()
    return [s for s in sources if re.search(r"(?<![a-z0-9_])" + re.escape(s["name"].lower()) + r"(?![a-z0-9_])", low)]


CHANGED = "a source changed during the query — refused"


def _read_paths(sources: list[dict]) -> list[tuple[str, list[int] | None]]:
    """(each file the engine will open, its identity when it passed the fence). A snapshot
    source reads its Parquet cache, but the origin's identity is checked too."""
    out = [(str(s["path"]), s.get("ident")) for s in sources]
    out += [(str(s["cache"]), s.get("cache_ident")) for s in sources if s.get("cache")]
    return out


def _unchanged(expected: list[tuple[str, list[int] | None]]) -> bool:
    from . import fence

    return all(want is not None and fence.identity(p) == want for p, want in expected)


def checked_call(op: str, sources: list[dict], **req: Any) -> Any:
    """``call`` for an op that reads ``sources`` — and then proves it read what the fence
    checked. Every source file is re-``lstat``-ed after the engine is done: same dev + inode,
    still a plain regular file (not a symlink). A file swapped between the fence check and
    DuckDB opening it (or during the read) fails that, and the result is DISCARDED, never
    returned. Sources with no recorded identity (not from ``sources.usable``) are refused."""
    expected = _read_paths(sources)
    if not _unchanged(expected):
        raise QueryError(CHANGED)
    got = call(op, sources=sources, **req)
    if not _unchanged(expected):
        raise QueryError(CHANGED)
    return got


def run_query(sources: list[dict], sql: str, *, cap: int, timeout_s: float) -> Result:
    got = checked_call("query", referenced(sources, sql), sql=sql, cap=int(cap), timeout_s=float(timeout_s))
    rows = [tuple(r) for r in got["rows"]]
    return Result(got["columns"], got["types"], rows, bool(got["truncated"]), float(got["elapsed"]))


def export(sources: list[dict], sql: str, out: Path, fmt: str, *, cap: int, timeout_s: float) -> int:
    """COPY the guarded SELECT to ``out`` (an exact path we chose). Returns rows written."""
    tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        n = checked_call(
            "export",
            referenced(sources, sql),
            sql=sql,
            tmp=str(tmp),
            fmt=fmt,
            cap=int(cap),
            timeout_s=float(timeout_s),
        )
    except QueryError:
        tmp.unlink(missing_ok=True)  # a failed/interrupted COPY leaves no half-written file
        raise
    os.replace(tmp, out)
    return int(n or 0)


# ── value shaping ────────────────────────────────────────────────────────────


def json_safe(v: Any) -> Any:
    """A value Vega-Lite (JSON) can carry: dates → ISO strings, Decimals → float, NaN → null."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)[:32].hex()
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    return str(v)


def cell(v: Any, width: int = 60) -> str:
    if v is None:
        return "∅"
    if isinstance(v, float):
        s = f"{v:.6g}" if abs(v) < 1e15 else f"{v:.4e}"
    elif isinstance(v, decimal.Decimal):
        s = f"{float(v):.6g}"
    else:
        s = str(json_safe(v)) if not isinstance(v, str) else v
    s = s.replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= width else s[: width - 1] + "…"


def md_table(columns: list[str], rows: Iterable[Iterable[Any]], width: int = 60) -> str:
    if not columns:
        return "(no columns)"
    out = ["| " + " | ".join(cell(c, width) for c in columns) + " |", "|" + "---|" * len(columns)]
    for r in rows:
        out.append("| " + " | ".join(cell(x, width) for x in r) + " |")
    return "\n".join(out)
