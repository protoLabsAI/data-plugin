"""The read-only DuckDB engine: a FRESH, locked, in-memory connection per call.

Read-only is enforced by the ENGINE, not by inspecting SQL text:

1. ``autoinstall/autoload_known_extensions`` off — nothing is fetched or loaded;
2. ``temp_directory`` set to this plugin's private ``spill/`` (DuckDB auto-allows its temp dir,
   so it must not be anywhere shared), plus ``memory_limit`` / ``threads``;
3. ``allowed_paths`` = the exact resolved files of the sources that pass the fence right now
   (plus their Parquet snapshots) — and nothing else;
4. ``enable_external_access = false`` — every other file, URL, ATTACH, COPY target, INSTALL and
   LOAD is refused by DuckDB itself;
5. one VIEW per source, created by us, then ``lock_configuration = true`` — so the agent's SQL
   can't SET/RESET any of the above.

On top of that, defence in depth: the agent's SQL must parse (with DuckDB's own parser) to
exactly ONE ``SELECT`` statement — CREATE, ATTACH ':memory:', PRAGMA, EXPLAIN and multi-statement
batches are refused before anything runs. A time cap interrupts the query; a row cap truncates.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths, settings


class QueryError(Exception):
    """A refusal or failure with a model-facing message."""


def q(s: str | Path) -> str:
    """A SQL string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def reader(src: dict) -> str:
    """The table function that reads one source (snapshots read their Parquet cache)."""
    kind = src.get("kind")
    if src.get("cache"):
        return f"read_parquet({q(src['cache'])})"
    path = src["path"]
    if kind == "parquet":
        return f"read_parquet({q(path)})"
    if kind == "json":
        return f"read_json_auto({q(path)})"
    if kind == "tsv":
        return f"read_csv({q(path)}, delim='\\t', header=true)"
    return f"read_csv({q(path)})"


def allowed_files(sources: Iterable[dict]) -> list[str]:
    out: list[str] = []
    for s in sources:
        out.append(str(s["cache"] if s.get("cache") else s["path"]))
    return sorted(set(out))


def open_locked(sources: list[dict], *, extra_paths: Iterable[str | Path] = ()):
    """A locked connection with one view per source. ``extra_paths`` are exact files our OWN
    statement may write (an export target) — never anything the agent names."""
    import duckdb

    conn = duckdb.connect(
        ":memory:", config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
    )
    try:
        conn.execute(f"SET temp_directory={q(paths.spill_dir())}")
        conn.execute(f"SET memory_limit={q(str(settings.cfg().get('memory_limit') or '1GB'))}")
        conn.execute(f"SET threads={max(1, min(4, os.cpu_count() or 1))}")
        allowed = allowed_files(sources) + [str(p) for p in extra_paths]
        conn.execute("SET allowed_paths=[" + ", ".join(q(p) for p in allowed) + "]")
        conn.execute("SET enable_external_access=false")
        for s in sources:
            conn.execute(f"CREATE VIEW {ident(s['name'])} AS SELECT * FROM {reader(s)}")
        conn.execute("SET lock_configuration=true")
    except Exception:
        conn.close()
        raise
    return conn


def guard(conn, sql: str) -> str:
    """The agent's SQL as exactly one SELECT, or raise. Returns it without a trailing ';'."""
    import duckdb

    text = (sql or "").strip()
    if not text:
        raise QueryError("Empty query.")
    try:
        stmts = conn.extract_statements(text)
    except Exception as e:  # noqa: BLE001 — a parse error, said plainly
        raise QueryError(f"SQL didn't parse: {_first_line(e)}") from None
    if len(stmts) != 1:
        raise QueryError(f"One statement per call, please — got {len(stmts)}.")
    st = stmts[0].type
    if st != duckdb.StatementType.SELECT:
        name = getattr(st, "name", str(st))
        raise QueryError(
            f"Read-only: only SELECT queries run here (got {name}). Nothing can be written, attached, "
            "installed or reconfigured — query the connected sources with SELECT / WITH / FROM."
        )
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _first_line(e: BaseException) -> str:
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__


@dataclass
class Result:
    columns: list[str]
    types: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed: float
    notes: list[str] = field(default_factory=list)


def _interruptible(conn, timeout_s: float):
    timer = threading.Timer(max(0.1, float(timeout_s)), conn.interrupt)
    timer.daemon = True
    return timer


def execute(conn, sql: str, *, cap: int, timeout_s: float) -> Result:
    """Run already-guarded SQL with the time and row caps."""
    timer = _interruptible(conn, timeout_s)
    t0 = time.monotonic()
    timer.start()
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        types = [str(d[1]) for d in (cur.description or [])]
        if any("WITH TIME ZONE" in t.upper() for t in types) and not _have_pytz():
            # DuckDB hands TIMESTAMPTZ to Python as a tz-aware datetime, which needs pytz — absent
            # on a lean host. Re-run with those columns as ISO text instead of failing the query.
            cur = conn.execute(_tz_as_text(sql, cols, types))
        rows = cur.fetchmany(cap + 1)
    except Exception as e:  # noqa: BLE001
        if _is_interrupt(e) or time.monotonic() - t0 >= timeout_s:
            raise QueryError(
                f"Query timed out after {timeout_s:g}s — narrow it (filter, aggregate, LIMIT) and try again."
            ) from None
        raise QueryError(_explain(e)) from None
    finally:
        timer.cancel()
    return Result(cols, types, rows[:cap], len(rows) > cap, time.monotonic() - t0)


def _have_pytz() -> bool:
    try:
        import pytz  # noqa: F401
    except ImportError:
        return False
    return True


def _tz_as_text(sql: str, cols: list[str], types: list[str]) -> str:
    tz = [c for c, t in zip(cols, types) if "WITH TIME ZONE" in t.upper()]
    repl = ", ".join(f"strftime({ident(c)}, '%Y-%m-%dT%H:%M:%S%z') AS {ident(c)}" for c in tz)
    return f"SELECT * REPLACE ({repl}) FROM (\n{sql}\n) AS q"


def _is_interrupt(e: BaseException) -> bool:
    return "interrupt" in type(e).__name__.lower() or "interrupted" in str(e).lower()


def _explain(e: BaseException) -> str:
    msg = _first_line(e)
    if "Permission Error" in msg or "disabled by configuration" in msg:
        return (
            f"Refused by the read-only engine: {msg}. Only the connected sources are readable — "
            "use data_sources to see them, data_connect to add a file inside an allowlisted folder."
        )
    return msg


def run_query(sources: list[dict], sql: str, *, cap: int, timeout_s: float) -> Result:
    conn = open_locked(sources)
    try:
        return execute(conn, guard(conn, sql), cap=cap, timeout_s=timeout_s)
    finally:
        conn.close()


def export(sources: list[dict], sql: str, out: Path, fmt: str, *, cap: int, timeout_s: float) -> int:
    """COPY the guarded SELECT to ``out`` (an exact path we chose). Returns rows written."""
    tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
    conn = open_locked(sources, extra_paths=[tmp])
    try:
        body = guard(conn, sql)
        opts = "FORMAT csv, HEADER true" if fmt == "csv" else "FORMAT parquet"
        stmt = f"COPY (SELECT * FROM (\n{body}\n) AS q LIMIT {int(cap)}) TO {q(tmp)} ({opts})"
        timer = _interruptible(conn, timeout_s)
        timer.start()
        try:
            got = conn.execute(stmt).fetchone()
        except Exception as e:  # noqa: BLE001
            if _is_interrupt(e):
                raise QueryError(f"Export timed out after {timeout_s:g}s.") from None
            raise QueryError(_explain(e)) from None
        finally:
            timer.cancel()
    finally:
        conn.close()
    os.replace(tmp, out)
    return int(got[0]) if got and got[0] is not None else 0


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
