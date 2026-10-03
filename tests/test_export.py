"""data_export writes ONLY into the workspace's data-exports folder."""

from __future__ import annotations

import pytest

from data import settings, tools

from conftest import call


@pytest.fixture
def connected(env):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    return env


def test_csv_round_trip(connected):
    out = call(tools.data_export, sql="SELECT weekday, cups FROM sales ORDER BY cups", format="csv", filename="cups")
    f = connected["exports"] / "cups.csv"
    assert f"→ {f.resolve()}" in out and "Exported 8 rows" in out
    text = f.read_text()
    assert text.splitlines()[0] == "weekday,cups" and "Monday,19" in text
    assert not [p for p in connected["exports"].iterdir() if p.name.startswith(".")]  # no temp left


def test_parquet_round_trip(connected):
    import duckdb

    call(tools.data_export, sql="SELECT * FROM sales", format="parquet", filename="all.parquet")
    f = connected["exports"] / "all.parquet"
    assert duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{f}')").fetchone()[0] == 8


@pytest.mark.parametrize("name", ["../evil", "sub/x", "..\\x", ".hidden", "/abs/path", "a..b"])
def test_filename_traversal_refused(connected, name):
    out = call(tools.data_export, sql="SELECT 1 AS a", format="csv", filename=name)
    assert "bare file name" in out
    assert not any(connected["exports"].iterdir()) if connected["exports"].exists() else True


def test_bad_format(connected):
    assert 'format must be "csv" or "parquet"' in call(tools.data_export, sql="SELECT 1", format="xlsx")


def test_export_query_is_read_only(connected):
    out = call(tools.data_export, sql="COPY sales TO '/tmp/x.csv'", format="csv", filename="x")
    assert "Read-only" in out


def test_export_row_cap(connected):
    settings.configure({"data_dirs": str(connected["data_dir"]), "export_row_cap": 3})
    out = call(tools.data_export, sql="SELECT * FROM sales", format="csv", filename="few")
    assert "Exported 3 rows (capped at 3 rows)" in out


def test_never_into_a_data_folder(connected, monkeypatch):
    inside = connected["data_dir"] / "exports"
    monkeypatch.setenv("DATA_EXPORT_DIR", str(inside))
    out = call(tools.data_export, sql="SELECT * FROM sales", format="csv", filename="x")
    assert "exports never write into source folders" in out
    assert not (inside / "x.csv").exists()
    assert sorted(p.name for p in connected["data_dir"].iterdir() if p.is_file()) == sorted(
        p.name for p in connected["files"].values()
    )
