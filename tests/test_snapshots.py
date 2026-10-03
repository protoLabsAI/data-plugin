"""SQLite and Excel sources are snapshotted to Parquet (read-only open), refreshed on change."""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from data import paths, sources, tools

from conftest import call


def test_sqlite_tables_become_sources_with_types(env):
    out = call(tools.data_connect, path=str(env["files"]["sqlite"]))
    assert "| shop_stores | sqlite | 2 | 4 |" in out and "| shop_beans | sqlite | 1 | 2 |" in out
    schema = call(tools.data_schema, source="shop_stores")
    assert "| id | BIGINT |" in schema and "| rent | DOUBLE |" in schema and "| opened | DATE |" in schema
    q = call(tools.data_query, sql="SELECT city FROM shop_stores WHERE opened IS NULL")
    assert "York" in q
    snap = sources.load()["shop_stores"]["cache"]
    assert snap.startswith(str(paths.cache_dir())) and snap.endswith(".parquet")


def test_sqlite_is_never_written(env):
    db = env["files"]["sqlite"]
    before = (db.read_bytes(), sorted(p.name for p in db.parent.iterdir()))
    call(tools.data_connect, path=str(db))
    call(tools.data_query, sql="SELECT * FROM shop_stores")
    assert (db.read_bytes(), sorted(p.name for p in db.parent.iterdir())) == before


def test_sqlite_snapshot_refreshes_when_the_file_changes(env):
    db = env["files"]["sqlite"]
    call(tools.data_connect, path=str(db))
    assert "| 2 |" in call(tools.data_query, sql="SELECT count(*) AS n FROM shop_stores")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO stores VALUES (3, 'Hull', '2025-05-05', 700)")
    con.commit()
    con.close()
    st = db.stat()
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert "| 3 |" in call(tools.data_query, sql="SELECT count(*) AS n FROM shop_stores")


def test_xlsx_sheets_become_sources(env):
    if "xlsx" not in env["files"]:
        pytest.skip("openpyxl not installed")
    out = call(tools.data_connect, path=str(env["files"]["xlsx"]))
    assert "| budget_budget | xlsx | 2 | 2 |" in out and "| budget_notes | xlsx | 1 | 1 |" in out
    q = call(tools.data_query, sql="SELECT sum(spend) AS total FROM budget_budget")
    assert "6000.5" in q


def test_xlsx_refreshes_on_change(env):
    if "xlsx" not in env["files"]:
        pytest.skip("openpyxl not installed")
    import openpyxl

    x = env["files"]["xlsx"]
    call(tools.data_connect, path=str(x))
    time.sleep(0.01)
    wb = openpyxl.load_workbook(x)
    wb["Budget"].append(["2026-09", 1000])
    wb.save(x)
    assert "7000.5" in call(tools.data_query, sql="SELECT sum(spend) AS total FROM budget_budget")


def test_missing_openpyxl_is_actionable(env, monkeypatch):
    if "xlsx" not in env["files"]:
        pytest.skip("openpyxl not installed")
    import builtins

    real = builtins.__import__

    def no_openpyxl(name, *a, **kw):
        if name == "openpyxl":
            raise ImportError("no")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_openpyxl)
    out = call(tools.data_connect, path=str(env["files"]["xlsx"]))
    assert "openpyxl" in out and "Install dependencies" in out
