"""QA-panel round on v0.1.0: the DESCRIBE/SUMMARIZE claim settled, and the four minors pinned."""

from __future__ import annotations

import duckdb
import pytest

from data import engine, tools

from conftest import call


@pytest.fixture
def connected(env):
    assert "Connected 1 source" in call(tools.data_connect, path=str(env["files"]["csv"]))
    return env


# ── the "majors": DESCRIBE / SUMMARIZE pass guard() ─────────────────────────


@pytest.mark.parametrize("sql", ["DESCRIBE SELECT 1 AS a", "SUMMARIZE SELECT 1 AS a"])
def test_duckdb_parses_describe_and_summarize_as_select(sql):
    """The claim was that guard() rejects them. DuckDB parses both as SELECT statements, so
    they pass the single-SELECT guard. Pinned here, and through the guard itself."""
    conn = duckdb.connect()
    try:
        assert conn.extract_statements(sql)[0].type == duckdb.StatementType.SELECT
        assert engine.guard(conn, sql) == sql
    finally:
        conn.close()


def test_data_schema_returns_real_output_through_guard(connected):
    out = call(tools.data_schema, source="sales")
    assert "| column | type |" in out and "| weekday |" in out and "Sample:" in out
    assert "Read-only" not in out  # not a guard refusal


def test_data_profile_returns_real_output_through_guard(connected):
    out = call(tools.data_profile, source="sales")
    assert "| column | type | nulls | ≈distinct |" in out and "| revenue |" in out
    assert "Read-only" not in out


# ── _fields: only transform-DERIVED names are exempt ─────────────────────────


def test_fields_keeps_query_fields_when_a_transform_is_present():
    spec = {
        "transform": [{"calculate": "datum.revenue / datum.orders", "as": "ticket"}],
        "mark": "point",
        "encoding": {"x": {"field": "ticket"}, "y": {"field": "revenue"}, "color": {"field": "wekday"}},
    }
    # `ticket` is derived; `revenue` and the typo `wekday` must still be checked against the query
    assert tools._fields(spec) == {"revenue", "wekday"}


def test_fields_inherits_derived_names_into_layers_and_handles_aggregate_lists():
    spec = {
        "transform": [{"aggregate": [{"op": "sum", "field": "revenue", "as": "total"}], "groupby": ["weekday"]}],
        "layer": [
            {"mark": "bar", "encoding": {"x": {"field": "weekday"}, "y": {"field": "total"}}},
            {
                "transform": [{"fold": ["a", "b"], "as": ["key", "value"]}],
                "mark": "line",
                "encoding": {"x": {"field": "key"}, "y": {"field": "value"}, "detail": {"field": "store"}},
            },
        ],
    }
    assert tools._fields(spec) == {"weekday", "store"}


def test_fields_trusts_a_pivot_node():
    spec = {"transform": [{"pivot": "k", "value": "v"}], "mark": "bar", "encoding": {"x": {"field": "anything"}}}
    assert tools._fields(spec) == set()


def test_a_chart_with_a_misspelled_field_beside_a_transform_is_caught(connected, monkeypatch):
    import json

    monkeypatch.setattr(tools, "_service", lambda: None)
    spec = {
        "transform": [{"calculate": "datum.revenue * 2", "as": "double"}],
        "mark": "bar",
        "encoding": {"x": {"field": "wekday"}, "y": {"field": "double"}},
    }
    out = call(tools.data_chart, sql="SELECT weekday, revenue FROM sales", vega_lite_spec=json.dumps(spec))
    assert "wekday" in out and "not in the query's columns" in out


# ── export leaves no half-written file; dead code gone ───────────────────────


def test_a_failed_export_unlinks_its_part_file(connected):
    out = call(tools.data_export, sql="SELECT error('boom') AS x FROM sales", format="csv")
    assert "boom" in out
    leftovers = [p.name for p in connected["exports"].iterdir()] if connected["exports"].exists() else []
    assert not [n for n in leftovers if n.endswith(".part")], leftovers


def test_result_has_no_unused_notes_field():
    assert "notes" not in engine.Result.__dataclass_fields__
