"""Test bootstrap — import the plugin with NO protoAgent host present.

The host loads a plugin under a synthetic package; the suite does the same so the modules'
relative imports (``from . import engine``) resolve standalone. Executing ``__init__.py`` is
safe precisely because every host-only import lives inside a function.

Every test runs against a temp plugin store (``DATA_PLUGIN_DIR``), a temp export dir
(``DATA_EXPORT_DIR``) and a temp agent home (``PROTOAGENT_HOME``), with ``data_dirs`` pointing at a
temp folder of small fixture files generated here: CSV, TSV, Parquet, JSON, SQLite and XLSX.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "data"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _mod
    _spec.loader.exec_module(_mod)

SALES = [
    ("2026-07-06", "Monday", "latte", 31, 139.5),
    ("2026-07-07", "Tuesday", "latte", 28, 126.0),
    ("2026-07-11", "Saturday", "mocha", 52, 260.0),
    ("2026-07-12", "Sunday", "drip", 47, 141.0),
    ("2026-07-13", "Monday", "drip", 22, 66.0),
    ("2026-07-18", "Saturday", "latte", 61, 274.5),
    ("2026-07-19", "Sunday", "mocha", 900, 4500.0),  # an outlier for the profile
    ("2026-07-20", "Monday", None, 19, None),
]


def _write_fixtures(d: Path) -> dict[str, Path]:
    import duckdb

    d.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    csv = d / "sales.csv"
    csv.write_text(
        "day,weekday,drink,cups,revenue\n"
        + "\n".join(",".join("" if v is None else str(v) for v in row) for row in SALES)
        + "\n",
        encoding="utf-8",
    )
    out["csv"] = csv
    tsv = d / "staff.tsv"
    tsv.write_text("name\trole\nAda\tbarista\nLin\tmanager\n", encoding="utf-8")
    out["tsv"] = tsv
    pq = d / "sales.parquet"
    c = duckdb.connect()
    c.execute(f"COPY (SELECT * FROM read_csv('{csv}')) TO '{pq}' (FORMAT parquet)")
    c.close()
    out["parquet"] = pq
    js = d / "menu.json"
    js.write_text(json.dumps([{"drink": "latte", "price": 4.5}, {"drink": "mocha", "price": 5.0}]), encoding="utf-8")
    out["json"] = js
    db = d / "shop.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, city TEXT, opened DATE, rent REAL)")
    con.executemany("INSERT INTO stores VALUES (?,?,?,?)", [(1, "Leeds", "2024-01-02", 1200.5), (2, "York", None, 900)])
    con.execute("CREATE TABLE beans (origin TEXT, kg REAL)")
    con.execute("INSERT INTO beans VALUES ('Huila', 12.5)")
    con.commit()
    con.close()
    out["sqlite"] = db
    try:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Budget"
        ws.append(["month", "spend"])
        ws.append(["2026-07", 3100])
        ws.append(["2026-08", 2900.5])
        ws2 = wb.create_sheet("Notes")
        ws2.append(["note"])
        ws2.append(["hello"])
        x = d / "budget.xlsx"
        wb.save(x)
        out["xlsx"] = x
    except ImportError:
        pass
    return out


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    from data import paths, settings

    store = tmp_path / "store"
    exports = tmp_path / "workspace" / "data-exports"
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    monkeypatch.setenv("DATA_PLUGIN_DIR", str(store))
    monkeypatch.setenv("DATA_EXPORT_DIR", str(exports))
    monkeypatch.setenv("PROTOAGENT_HOME", str(agent_home))
    monkeypatch.delenv("PROTOAGENT_BOX_ROOT", raising=False)
    paths.configure("")
    data_dir = tmp_path / "datasets"
    files = _write_fixtures(data_dir)
    settings.configure({"data_dirs": str(data_dir)})
    yield {
        "data_dir": data_dir.resolve(),
        "files": files,
        "tmp": tmp_path,
        "exports": exports,
        "agent_home": agent_home,
    }
    settings.configure(None)


def call(tool, **kw) -> str:
    return tool.invoke(kw)


class FakeRegistry:
    """Stands in for the host's PluginRegistry — records what register() contributes."""

    def __init__(self, config=None):
        self.config = config or {}
        self.plugin_id = "data"
        self.tools, self.skill_dirs = [], []
        self.gaps: dict[str, tuple] = {}

    def register_tools(self, ts):
        self.tools.extend(ts)

    def register_tool(self, t):
        self.tools.append(t)

    def register_skill_dir(self, p):
        self.skill_dirs.append(p)

    def report_setup_gap(self, key, message, *, label=None, action=None):
        if message is None:
            self.gaps.pop(key, None)
        else:
            self.gaps[key] = (message, action)

    def live_config(self):
        return self.config
