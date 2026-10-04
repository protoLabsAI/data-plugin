"""A source swapped between the per-query fence check and DuckDB opening it is refused.

The fence resolves and checks every source before each query; the engine then opens the
already-resolved path. Swapping that path for a symlink (into the agent home, say) in the gap
used to be read. Each source's identity (dev + inode, plain regular file) is snapshotted when it
passes the fence and re-``lstat``-ed after the engine is done; any change discards the result.
Runs in-process and, with ``DATA_TEST_WORKER=1``, through the worker (conftest).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from data import engine, settings, tools

from conftest import call

SECRET = "TOPSECRET"


@pytest.fixture
def secret(env) -> Path:
    home = env["agent_home"] / "config"
    home.mkdir(parents=True, exist_ok=True)
    f = home / "good.csv"
    f.write_text(f"day,weekday,drink,cups,revenue\n{SECRET},x,y,1,2\n")
    return f


def _swap_before_engine(monkeypatch, swap):
    """Run ``swap()`` right before the engine runs (after the fence check passed)."""
    real = engine.call
    done = []

    def hooked(op, **kw):
        if op in ("query", "export") and not done:
            done.append(1)
            swap()
        return real(op, **kw)

    monkeypatch.setattr(engine, "call", hooked)


def _to_symlink(path: Path, target: Path):
    def swap():
        os.replace(path, path.with_suffix(".bak"))
        path.symlink_to(target)

    return swap


@pytest.mark.parametrize("tool", ["query", "chart_free_query", "export"])
def test_swap_to_symlink_between_check_and_execute_is_refused(env, secret, monkeypatch, tool):
    src = env["files"]["csv"]
    assert call(tools.data_connect, path=str(src)).startswith("Connected ")
    _swap_before_engine(monkeypatch, _to_symlink(src, secret))
    if tool == "export":
        out = call(tools.data_export, sql="SELECT * FROM sales", format="csv", filename="leak")
        assert not (env["exports"] / "leak.csv").exists()
        assert not [p for p in env["exports"].iterdir()] if env["exports"].exists() else True
    elif tool == "query":
        out = call(tools.data_query, sql="SELECT * FROM sales")
    else:
        out = call(tools.data_query, sql="SELECT day FROM sales WHERE day = 'TOPSECRET'")
    assert SECRET not in out, out
    assert engine.CHANGED in out, out


def test_swap_to_another_regular_file_is_refused(env, secret, monkeypatch):
    src = env["files"]["csv"]
    assert call(tools.data_connect, path=str(src)).startswith("Connected ")

    def swap():
        tmp = src.with_suffix(".new")
        tmp.write_text(secret.read_text())  # a different inode at the same path
        os.replace(tmp, src)

    _swap_before_engine(monkeypatch, swap)
    out = call(tools.data_query, sql="SELECT * FROM sales")
    assert SECRET not in out and engine.CHANGED in out, out


def test_swap_during_the_read_is_refused(env, secret, monkeypatch):
    """The swap lands while the engine runs (after it opened the original): result discarded."""
    src = env["files"]["csv"]
    assert call(tools.data_connect, path=str(src)).startswith("Connected ")
    real = engine.call

    def hooked(op, **kw):
        got = real(op, **kw)
        if op == "query":
            _to_symlink(src, secret)()
        return got

    monkeypatch.setattr(engine, "call", hooked)
    out = call(tools.data_query, sql="SELECT count(*) AS n FROM sales")
    assert engine.CHANGED in out, out


def test_untouched_sources_still_query(env, monkeypatch):
    calls = []
    real = engine.call
    monkeypatch.setattr(engine, "call", lambda op, **kw: calls.append(op) or real(op, **kw))
    assert call(tools.data_connect, path=str(env["files"]["csv"])).startswith("Connected ")
    out = call(tools.data_query, sql="SELECT sum(cups) AS c FROM sales")
    assert "1160" in out and "query" in calls


def test_snapshot_origin_swapped_before_query_is_refused(env, secret, monkeypatch):
    """SQLite sources read a Parquet snapshot — the origin's identity is checked too."""
    db = env["files"]["sqlite"]
    assert call(tools.data_connect, path=str(db)).startswith("Connected ")
    other = env["agent_home"] / "checkpoints.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE stores (id INTEGER, city TEXT, opened DATE, rent REAL)")
    con.execute(f"INSERT INTO stores VALUES (9, '{SECRET}', NULL, 0)")
    con.commit()
    con.close()
    _swap_before_engine(monkeypatch, _to_symlink(db, other))
    out = call(tools.data_query, sql="SELECT * FROM shop_stores")
    assert SECRET not in out and engine.CHANGED in out, out


def test_sources_without_a_recorded_identity_are_refused(env):
    srcs = [{"name": "sales", "kind": "csv", "path": str(env["files"]["csv"])}]
    with pytest.raises(engine.QueryError, match="changed during the query"):
        engine.run_query(srcs, "SELECT 1 FROM sales", cap=1, timeout_s=10)


def test_identity_is_not_trusted_from_sources_json(env):
    from data import sources

    assert call(tools.data_connect, path=str(env["files"]["csv"])).startswith("Connected ")
    srcs = sources.load()
    assert all("ident" not in s for s in srcs.values())
    settings.configure({"data_dirs": str(env["data_dir"])})
    ok, _ = sources.usable()
    assert ok and all(s["ident"] for s in ok)
