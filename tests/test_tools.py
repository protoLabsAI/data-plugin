"""Connect / sources / schema / profile output, and the row + time caps."""

from __future__ import annotations

import time

import pytest

from data import settings, sources, tools

from conftest import call


def test_connect_folder_registers_every_kind(env):
    out = call(tools.data_connect, path=str(env["data_dir"]))
    for name in ("sales", "sales_2", "staff", "menu", "shop_stores", "shop_beans"):
        assert f"| {name} |" in out, out
    if "xlsx" in env["files"]:
        assert "| budget_budget |" in out and "| budget_notes |" in out
    listing = call(tools.data_sources)
    assert "| sales | csv | 8 | 5 |" in listing
    assert "| shop_stores | sqlite | 2 | 4 |" in listing


def test_connect_names_a_single_file_and_reconnect_reuses_it(env):
    out = call(tools.data_connect, path=str(env["files"]["csv"]), name="Coffee Sales!")
    assert "| coffee_sales |" in out
    again = call(tools.data_connect, path=str(env["files"]["csv"]), name="other")
    assert "| coffee_sales |" in again  # same file → same source, refreshed
    assert list(sources.load()) == ["coffee_sales"]


def test_registry_persists_across_processes(env):
    call(tools.data_connect, path=str(env["files"]["parquet"]))
    raw = (env["tmp"] / "store" / "sources.json").read_text()
    assert '"kind": "parquet"' in raw


def test_unsupported_type(env):
    f = env["data_dir"] / "notes.txt"
    f.write_text("hi")
    assert "unsupported file type" in call(tools.data_connect, path=str(f))


def test_schema(env):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_schema, source="sales")
    assert "5 columns" in out
    assert "| day | DATE |" in out and "| cups | BIGINT |" in out and "| revenue | DOUBLE |" in out
    assert "Sample:" in out and "latte" in out


def test_schema_unknown_source(env):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "No connected source 'nope'" in call(tools.data_schema, source="nope")


def test_profile(env):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_profile, source="sales")
    assert out.startswith("`sales` — 8 rows, 5 columns")
    lines = {ln.split("|")[1].strip(): ln for ln in out.splitlines() if ln.startswith("| ") and "---" not in ln}
    assert "1 (12.5%)" in lines["drink"]  # one null
    assert "top: latte (3)" in lines["drink"]
    assert "μ" in lines["cups"] and "q1" in lines["cups"]
    assert lines["cups"].rstrip(" |").endswith("1")  # the 900-cup day is the IQR outlier
    assert "| ≈distinct |" in out


def test_query_row_cap_truncates(env):
    settings.configure({"data_dirs": str(env["data_dir"]), "row_cap": 3})
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_query, sql="SELECT * FROM sales")
    assert "TRUNCATED at the 3-row cap" in out
    assert len([ln for ln in out.splitlines() if ln.startswith("| ") and "---" not in ln]) == 4  # header + 3


def test_query_time_cap_interrupts(env):
    settings.configure({"data_dirs": str(env["data_dir"]), "timeout_s": 1})
    call(tools.data_connect, path=str(env["files"]["csv"]))
    t0 = time.monotonic()
    out = call(
        tools.data_query, sql="SELECT count(*) FROM range(1000000000) a, range(100000) b WHERE a.range % 7 = b.range"
    )
    assert "timed out after 1s" in out
    assert time.monotonic() - t0 < 15


def test_query_error_is_reported_plainly(env):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_query, sql="SELECT nope FROM sales")
    assert "nope" in out and "Traceback" not in out


def test_query_with_no_sources(env):
    assert "No usable data sources" in call(tools.data_query, sql="SELECT 1")


def test_no_sources_points_at_the_allowlisted_folders(env, monkeypatch):
    """The operator allowlisted a folder FOR the agent: with nothing connected yet, say where it
    is, so the agent can connect it without asking the operator for a path."""
    out = call(tools.data_sources)
    assert "allowlisted data folders" in out and str(env["data_dir"].resolve()) in out
    monkeypatch.setattr(settings, "cfg", lambda: {"data_dirs": ""})
    assert "No data folders are allowlisted yet" in call(tools.data_sources)


@pytest.mark.parametrize(
    "raw,want", [("Sales 2026.csv", "sales_2026_csv"), ("2026", "t_2026"), ("order", "order_data")]
)
def test_sanitize(raw, want):
    assert sources.sanitize(raw) == want


def test_timestamptz_columns_render_without_pytz(env, monkeypatch):
    """A TIMESTAMPTZ column needs pytz to become a Python datetime; a lean host may not have it."""
    from data import engine

    monkeypatch.setattr(engine.duck, "_have_pytz", lambda: False)
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_query, sql="SELECT day::TIMESTAMPTZ AS ts, cups FROM sales ORDER BY ts LIMIT 1")
    assert "| ts | cups |" in out and "2026-07-06T" in out
