"""Every line that touches DuckDB — runnable IN-PROCESS or as a WORKER in another Python.

Why a worker: on the packaged desktop app the host is a frozen binary with no pip, so a plugin
can't add a compiled dependency to the host process. Plugin deps land in the MANAGED Python
runtime instead (the interpreter ``execute_code`` spawns, ``sdk.managed_python_exe()``), which is
a separate site-packages. So when ``duckdb`` isn't importable in the host, ``engine`` runs this
file under the managed runtime — ``python -I duck.py`` — with one JSON request on stdin and one
JSON reply on stdout. When it IS importable (a source/server install), the same functions run
in-process. One implementation, two transports: the read-only rules below can't drift apart.

This module therefore imports ONLY the standard library and ``duckdb`` — never the host, never
a sibling module (the worker runs with ``-I``: no script dir on ``sys.path``). Everything it
needs (spill/home dirs, memory limit) arrives in the request's ``env``.

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

import base64
import datetime as _dt
import decimal
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


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
    # Only a SQLite/Excel snapshot reads a cache — never trust a `cache` on any other kind.
    if kind in ("sqlite", "xlsx") and src.get("cache"):
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
        snap = s.get("kind") in ("sqlite", "xlsx") and s.get("cache")
        out.append(str(s["cache"] if snap else s["path"]))
    return sorted(set(out))


def first_line(e: BaseException) -> str:
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__


def _connect():
    import duckdb

    return duckdb.connect(
        ":memory:", config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
    )


def open_locked(env: dict, sources: list[dict], *, extra_paths: Iterable[str | Path] = ()):
    """A locked connection with one view per source. ``extra_paths`` are exact files our OWN
    statement may write (an export target) — never anything the agent names.

    ``env``: ``spill`` (temp dir), ``home`` (inert DuckDB home), ``spill_max``, ``memory_limit``,
    ``threads``."""
    conn = _connect()
    step = "configuring the engine"
    try:
        conn.execute(f"SET temp_directory={q(env['spill'])}")
        home = Path(env["home"])
        conn.execute(f"SET home_directory={q(home)}")
        conn.execute(f"SET secret_directory={q(home / 'secrets')}")
        conn.execute(f"SET extension_directory={q(home / 'extensions')}")
        # Spill is DISK: capped on its own, or a big sort/join under a small memory_limit fills
        # the drive within the time cap. Past this the query fails instead.
        conn.execute(f"SET max_temp_directory_size={q(env['spill_max'])}")
        conn.execute(f"SET memory_limit={q(env['memory_limit'])}")
        conn.execute(f"SET threads={max(1, int(env.get('threads') or 1))}")
        allowed = allowed_files(sources) + [str(p) for p in extra_paths]
        conn.execute("SET allowed_paths=[" + ", ".join(q(p) for p in allowed) + "]")
        conn.execute("SET enable_external_access=false")
        for s in sources:
            step = f"opening `{s['name']}`"
            conn.execute(f"CREATE VIEW {ident(s['name'])} AS SELECT * FROM {reader(s)}")
        conn.execute("SET lock_configuration=true")
    except Exception as e:  # noqa: BLE001 — a corrupt file or bad setting is a refusal, never a crash
        conn.close()
        raise QueryError(f"Failed {step}: {first_line(e)}") from None
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
        raise QueryError(f"SQL didn't parse: {first_line(e)}") from None
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


def _interruptible(conn, timeout_s: float):
    timer = threading.Timer(max(0.1, float(timeout_s)), conn.interrupt)
    timer.daemon = True
    return timer


def execute(conn, sql: str, *, cap: int, timeout_s: float) -> dict:
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
    return {
        "columns": cols,
        "types": types,
        "rows": rows[:cap],
        "truncated": len(rows) > cap,
        "elapsed": time.monotonic() - t0,
    }


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
    msg = first_line(e)
    if "Permission Error" in msg or "disabled by configuration" in msg:
        return (
            f"Refused by the read-only engine: {msg}. Only the connected sources are readable — "
            "use data_sources to see them, data_connect to add a file inside an allowlisted folder."
        )
    return msg


# ── the three operations ─────────────────────────────────────────────────────


def run_query(env: dict, sources: list[dict], sql: str, *, cap: int, timeout_s: float) -> dict:
    conn = open_locked(env, sources)  # raises QueryError, never a raw duckdb error
    try:
        return execute(conn, guard(conn, sql), cap=cap, timeout_s=timeout_s)
    finally:
        conn.close()


def export(env: dict, sources: list[dict], sql: str, tmp: str, fmt: str, *, cap: int, timeout_s: float) -> int:
    """COPY the guarded SELECT to ``tmp`` (an exact path the caller chose). Returns rows written.
    A failed/interrupted COPY leaves no half-written file."""
    tmp_p = Path(tmp)
    conn = open_locked(env, sources, extra_paths=[tmp_p])
    try:
        body = guard(conn, sql)
        opts = "FORMAT csv, HEADER true" if fmt == "csv" else "FORMAT parquet"
        stmt = f"COPY (SELECT * FROM (\n{body}\n) AS q LIMIT {int(cap)}) TO {q(tmp_p)} ({opts})"
        timer = _interruptible(conn, timeout_s)
        timer.start()
        try:
            got = conn.execute(stmt).fetchone()
        except Exception as e:  # noqa: BLE001
            tmp_p.unlink(missing_ok=True)
            if _is_interrupt(e):
                raise QueryError(f"Export timed out after {timeout_s:g}s.") from None
            raise QueryError(_explain(e)) from None
        finally:
            timer.cancel()
    finally:
        conn.close()
    return int(got[0]) if got and got[0] is not None else 0


def csv_to_parquet(env: dict, tmp_csv: str, tmp_pq: str, columns: dict[str, str] | None, null: str) -> None:
    """Snapshot step: the staged CSV (written by the caller from SQLite/Excel) → Parquet. Tries the
    declared column types first, then DuckDB's own sniffing, then all-VARCHAR."""
    conn = _connect()
    try:
        conn.execute(f"SET temp_directory={q(env['spill'])}")
        base = f"read_csv({q(tmp_csv)}, header=true, nullstr={q(null)}"
        spec = None
        if columns:
            spec = "{" + ", ".join(f"{q(k)}: {q(v)}" for k, v in columns.items()) + "}"
        for attempt in ([f"{base}, columns={spec})"] if spec else []) + [f"{base})", f"{base}, all_varchar=true)"]:
            try:
                conn.execute(f"COPY (SELECT * FROM {attempt}) TO {q(tmp_pq)} (FORMAT parquet)")
                return
            except Exception:  # noqa: BLE001 — declared types the data doesn't honour: loosen
                continue
        raise QueryError("no read of the staged rows produced a snapshot")
    finally:
        conn.close()


def dispatch(req: dict) -> Any:
    """Run one request ``{"op": ..., "env": {...}, ...}``. Raises QueryError."""
    op = req.get("op")
    env = req.get("env") or {}
    if op == "query":
        return run_query(env, req["sources"], req["sql"], cap=int(req["cap"]), timeout_s=float(req["timeout_s"]))
    if op == "export":
        return export(
            env,
            req["sources"],
            req["sql"],
            req["tmp"],
            req["fmt"],
            cap=int(req["cap"]),
            timeout_s=float(req["timeout_s"]),
        )
    if op == "snapshot":
        return csv_to_parquet(env, req["tmp_csv"], req["tmp_pq"], req.get("columns"), req["null"])
    if op == "ping":
        import duckdb

        return duckdb.__version__
    raise QueryError(f"unknown op {op!r}")


# ── the wire: values survive the process boundary with their Python types ────
# Rows carry dates, Decimals, timedeltas, bytes, UUIDs, nested lists/structs. JSON alone would
# flatten them to strings and the formatting code downstream (cell, json_safe) would then render
# them differently on the two transports, so each non-JSON value travels as a tagged object.


def encode(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return {"$t": "dec", "v": str(v)}
    if isinstance(v, _dt.datetime):
        return {"$t": "dtm", "v": v.isoformat()}
    if isinstance(v, _dt.date):
        return {"$t": "date", "v": v.isoformat()}
    if isinstance(v, _dt.time):
        return {"$t": "time", "v": v.isoformat()}
    if isinstance(v, _dt.timedelta):
        return {"$t": "td", "v": [v.days, v.seconds, v.microseconds]}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$t": "bytes", "v": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, uuid.UUID):
        return {"$t": "uuid", "v": str(v)}
    if isinstance(v, tuple):
        return {"$t": "tuple", "v": [encode(x) for x in v]}
    if isinstance(v, list):
        return [encode(x) for x in v]
    if isinstance(v, dict):
        return {"$t": "dict", "v": [[encode(k), encode(x)] for k, x in v.items()]}
    return str(v)


def decode(v: Any) -> Any:
    if isinstance(v, list):
        return [decode(x) for x in v]
    if not isinstance(v, dict):
        return v
    t, x = v.get("$t"), v.get("v")
    if t == "dec":
        return decimal.Decimal(x)
    if t == "dtm":
        return _dt.datetime.fromisoformat(x)
    if t == "date":
        return _dt.date.fromisoformat(x)
    if t == "time":
        return _dt.time.fromisoformat(x)
    if t == "td":
        return _dt.timedelta(days=x[0], seconds=x[1], microseconds=x[2])
    if t == "bytes":
        return base64.b64decode(x)
    if t == "uuid":
        return uuid.UUID(x)
    if t == "tuple":
        return tuple(decode(i) for i in x)
    if t == "dict":
        return {decode(k): decode(i) for k, i in x}
    return v


def main() -> int:
    """Worker entry: one JSON request on stdin → ``{"ok": result}`` or ``{"error": msg}`` on stdout.
    Exit 0 either way; a non-zero exit means the worker itself broke (stderr says how)."""
    req = json.loads(sys.stdin.read() or "{}")
    try:
        reply = {"ok": encode(dispatch(req))}
    except QueryError as e:
        reply = {"error": str(e)}
    except ImportError as e:
        reply = {"missing": first_line(e)}
    sys.stdout.write(json.dumps(reply))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
