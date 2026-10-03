"""Read-only enforcement: the ENGINE refuses writes/escapes, and the statement guard refuses
anything but one SELECT — each checked through the agent's own tool."""

from __future__ import annotations

import pytest

from data import engine, sources, tools

from conftest import call


@pytest.fixture
def connected(env):
    out = call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "Connected 1 source" in out, out
    return env


def test_select_runs(connected):
    out = call(tools.data_query, sql="SELECT weekday, sum(revenue) AS rev FROM sales GROUP BY 1 ORDER BY 2 DESC")
    assert "| weekday | rev |" in out and "Sunday" in out


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE x AS SELECT 1",
        "INSERT INTO sales VALUES (1,2,3,4,5)",
        "DROP VIEW sales",
        "COPY sales TO '/tmp/pwned.csv'",
        "ATTACH ':memory:' AS m",
        "ATTACH '/tmp/evil.db' AS e",
        "INSTALL sqlite",
        "LOAD httpfs",
        "SET enable_external_access=true",
        "RESET lock_configuration",
        "PRAGMA enable_profiling",
        "EXPLAIN SELECT 1",
        "EXPORT DATABASE '/tmp/x'",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP VIEW sales",
    ],
)
def test_non_select_is_refused(connected, sql):
    out = call(tools.data_query, sql=sql)
    assert "Read-only" in out or "One statement" in out or "didn't parse" in out, out
    assert "|" not in out.splitlines()[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_text('{secret}')",
        "SELECT * FROM read_csv('{sibling}')",
        "SELECT * FROM glob('{dir}/*')",
        "SELECT * FROM read_csv('https://example.com/x.csv')",
        "SELECT * FROM read_parquet('{dir}/../store/sources.json')",
    ],
)
def test_reads_outside_the_connected_sources_are_refused_by_the_engine(connected, sql):
    d = connected["data_dir"]
    (d / ".env").write_text("SECRET=1\n")
    (d / "other.csv").write_text("a\n1\n")  # inside data_dirs but NOT connected
    q = sql.format(secret=d / ".env", sibling=d / "other.csv", dir=d)
    out = call(tools.data_query, sql=q)
    assert "Refused by the read-only engine" in out, out
    assert "SECRET" not in out


def test_engine_lock_holds_even_without_the_statement_guard(connected):
    """Defence in depth: bypass guard() and the locked connection still refuses."""
    ok, _ = sources.usable()
    conn = engine.open_locked(ok)
    try:
        for stmt in (
            "SET enable_external_access=true",
            "SET allowed_paths=['/etc/passwd']",
            "COPY (SELECT 1) TO '/tmp/data-plugin-pwned.csv'",
            "INSTALL sqlite",
            "LOAD sqlite",
            "SELECT * FROM read_csv('/etc/passwd')",
        ):
            with pytest.raises(Exception):
                conn.execute(stmt).fetchall()
    finally:
        conn.close()


def test_source_files_are_never_written(connected):
    before = connected["files"]["csv"].read_bytes()
    call(tools.data_query, sql=f"COPY sales TO '{connected['files']['csv']}'")
    call(tools.data_query, sql="SELECT * FROM sales")
    assert connected["files"]["csv"].read_bytes() == before
