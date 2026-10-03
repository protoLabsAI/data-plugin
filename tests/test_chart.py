"""data_chart: the payload handed to core's ``artifact.show`` service, and its degrade path."""

from __future__ import annotations

import json
import sys
import types

import pytest

from data import settings, tools

from conftest import call

REF = '\n\x00COMPONENT{"kind":"artifact-ref"}'


class FakeSdk(types.ModuleType):
    def __init__(self, show=None):
        super().__init__("graph.sdk")
        self.calls = []
        self._show = show

    def service(self, name):
        self.calls.append(("service", name))
        return self._show


@pytest.fixture
def shown(monkeypatch):
    got = []

    def show(kind, code, title=""):
        got.append({"kind": kind, "code": code, "title": title})
        return {
            "ok": True,
            "id": "a1",
            "version": 1,
            "message": "Created vega-lite artifact a1 — rendered OK.",
            "ref": REF,
        }

    sdk = FakeSdk(show)
    graph = types.ModuleType("graph")
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    return got, sdk


SPEC = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weekday", "type": "nominal", "sort": "-y"},
        "y": {"field": "rev", "type": "quantitative"},
    },
}
SQL = "SELECT weekday, sum(revenue) AS rev, min(day) AS first_day FROM sales GROUP BY 1"


def test_chart_payload_shape(env, shown):
    got, sdk = shown
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_chart, sql=SQL, vega_lite_spec=json.dumps(SPEC), title="Best weekdays")
    assert sdk.calls == [("service", "artifact.show")]
    assert len(got) == 1 and got[0]["kind"] == "vega-lite" and got[0]["title"] == "Best weekdays"
    spec = json.loads(got[0]["code"])
    assert spec["mark"] == "bar" and spec["title"] == "Best weekdays"
    assert spec["$schema"].endswith("vega-lite/v6.json")
    values = spec["data"]["values"]
    assert {v["weekday"] for v in values} == {"Monday", "Tuesday", "Saturday", "Sunday"}
    assert all(isinstance(v["first_day"], str) for v in values)  # dates → ISO strings, JSON-safe
    assert "url" not in json.dumps(spec["data"])
    assert out.startswith("Created vega-lite artifact a1")
    assert out.endswith(REF)  # the chip tail goes LAST, verbatim
    assert "4 rows × 3 cols" in out


def test_spec_data_and_urls_are_replaced(env, shown):
    got, _ = shown
    call(tools.data_connect, path=str(env["files"]["csv"]))
    spec = dict(SPEC, data={"url": "https://evil.example/x.csv"})
    spec["layer"] = [{"mark": "rule", "data": {"url": "file:///etc/passwd"}, "encoding": {}}]
    out = call(tools.data_chart, sql=SQL, vega_lite_spec=json.dumps(spec), title="")
    sent = got[0]["code"]
    assert "evil.example" not in sent and "/etc/passwd" not in sent
    assert "replaced the spec's `data`" in out and "dropped 1 external data URL" in out


def test_spec_dict_is_accepted_and_bad_specs_refused(env, shown):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "must be a JSON object" in call(tools.data_chart, sql=SQL, vega_lite_spec="[1,2]", title="")
    assert "isn't valid JSON" in call(tools.data_chart, sql=SQL, vega_lite_spec="{mark:", title="")
    assert "needs a `mark`" in call(tools.data_chart, sql=SQL, vega_lite_spec='{"encoding": {}}', title="")


def test_unknown_fields_are_flagged(env, shown):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    spec = {
        "mark": "bar",
        "encoding": {"x": {"field": "wkday", "type": "nominal"}, "y": {"field": "rev", "type": "quantitative"}},
    }
    out = call(tools.data_chart, sql=SQL, vega_lite_spec=json.dumps(spec), title="")
    assert "not in the query's columns: wkday" in out


def test_chart_row_cap_asks_for_aggregation(env, shown):
    got, _ = shown
    settings.configure({"data_dirs": str(env["data_dir"]), "chart_row_cap": 3})
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_chart, sql="SELECT * FROM sales", vega_lite_spec=json.dumps(SPEC), title="")
    assert "too many to chart" in out and "Aggregate in SQL" in out and not got


def test_chart_query_is_read_only_too(env, shown):
    got, _ = shown
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_chart, sql="COPY sales TO '/tmp/x.csv'", vega_lite_spec=json.dumps(SPEC), title="")
    assert "Read-only" in out and not got


def test_no_service_degrades_with_the_spec(env, monkeypatch):
    sdk = FakeSdk(None)
    graph = types.ModuleType("graph")
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    call(tools.data_connect, path=str(env["files"]["csv"]))
    out = call(tools.data_chart, sql=SQL, vega_lite_spec=json.dumps(SPEC), title="T")
    assert out.startswith("Chart NOT rendered") and "enable the Artifact plugin" in out
    assert '"mark":"bar"' in out and '"values"' not in out


def test_older_host_without_service_degrades(env, monkeypatch):
    graph = types.ModuleType("graph")
    graph.sdk = types.ModuleType("graph.sdk")  # no .service attribute
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", graph.sdk)
    call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "Chart NOT rendered" in call(tools.data_chart, sql=SQL, vega_lite_spec=json.dumps(SPEC), title="")
