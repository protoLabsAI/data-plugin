"""The default data folder — ``<agent workspace>/data``, always allowlisted, the ONE carve-out from
the agent-home refusal.

The host's ``infra.paths`` is stood in for by a fake module whose ``workspace_dir`` is
``$PROTOAGENT_HOME/workspace`` (what core resolves for an instance), so the default folder really
sits INSIDE the temp agent home here, next to a config, a secrets file, SQLite stores and a
memory/ folder that must all stay refused.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

import data
from data import fence, paths, settings, tools

from conftest import FakeRegistry, call

CSV = "day,cups\nMonday,31\nSaturday,52\nSunday,47\n"


@pytest.fixture
def home(env, monkeypatch):
    """A populated agent home with a host-shaped workspace; ``data_dirs`` empty."""
    agent_home: Path = env["agent_home"].resolve()
    (agent_home / "config").mkdir()
    (agent_home / "config" / "config.yaml").write_text("model: x\n")
    (agent_home / "config" / "settings.csv").write_text("key,value\nmodel,x\n")
    (agent_home / "secrets.yaml").write_text("auth:\n  token: s3cret\n")
    for db in ("checkpoints.db", "knowledge.db"):
        con = sqlite3.connect(agent_home / db)
        con.execute("CREATE TABLE t (secret TEXT)")
        con.execute("INSERT INTO t VALUES ('s3cret')")
        con.commit()
        con.close()
    (agent_home / "memory").mkdir()
    (agent_home / "memory" / "facts.csv").write_text("fact\ns3cret\n")

    def workspace_dir(*, create: bool = False) -> Path:
        ws = Path(os.environ["PROTOAGENT_HOME"]) / "workspace"
        if create:
            ws.mkdir(parents=True, exist_ok=True)
        return ws.resolve()

    infra = types.ModuleType("infra")
    infra_paths = types.ModuleType("infra.paths")
    infra_paths.workspace_dir = workspace_dir
    infra.paths = infra_paths
    monkeypatch.setitem(sys.modules, "infra", infra)
    monkeypatch.setitem(sys.modules, "infra.paths", infra_paths)
    monkeypatch.delenv("DATA_EXPORT_DIR", raising=False)
    monkeypatch.delenv("DATA_DEFAULT_DIR", raising=False)
    settings.configure({})
    return {
        **env,
        "agent_home": agent_home,
        "default": agent_home / "workspace" / "data",
        "exports": agent_home / "workspace" / "data-exports",
    }


def _drop(home, name="coffee.csv", text=CSV) -> Path:
    home["default"].mkdir(parents=True, exist_ok=True)
    f = home["default"] / name
    f.write_text(text)
    return f


def _connected(out: str) -> bool:
    return out.startswith("Connected ")


def _refused(out: str) -> bool:
    """Refused by the fence: it resolves out of every allowlisted folder, or into the home."""
    return (
        not _connected(out)
        and "s3cret" not in out
        and ("outside the allowlisted data folders" in out or "agent's home" in out)
    )


# ── created, allowlisted, readable ──────────────────────────────────────────


def test_register_creates_the_folder_and_clears_the_gap(home):
    assert not home["default"].exists()
    reg = FakeRegistry({})
    data.register(reg)
    assert home["default"].is_dir()
    assert "data_dirs" not in reg.gaps  # no data_dirs needed — the default folder is there


def test_resolved_through_the_workspace(home):
    root, note = fence.default_root()
    assert root == home["default"] and note is None
    assert paths.export_dir() == home["exports"]
    allowed, _ = fence.roots("")
    assert allowed == [home["default"]]


def test_file_in_default_folder_is_connectable_and_readable(home):
    f = _drop(home)
    out = call(tools.data_connect, path=str(f))
    assert _connected(out), out
    out = call(tools.data_query, sql="SELECT weekday_total FROM (SELECT sum(cups) AS weekday_total FROM coffee)")
    assert "130" in out


def test_default_folder_itself_is_connectable(home):
    _drop(home)
    _drop(home, "beans.csv", "origin,kg\nHuila,12.5\n")
    out = call(tools.data_connect, path=str(home["default"]))
    assert _connected(out) and "coffee" in out and "beans" in out, out


def test_sqlite_in_default_folder_is_readable(home):
    home["default"].mkdir(parents=True)
    con = sqlite3.connect(home["default"] / "shop.db")
    con.execute("CREATE TABLE stores (city TEXT)")
    con.execute("INSERT INTO stores VALUES ('Leeds')")
    con.commit()
    con.close()
    out = call(tools.data_connect, path=str(home["default"] / "shop.db"))
    assert _connected(out), out
    assert "Leeds" in call(tools.data_query, sql="SELECT city FROM shop_stores")


def test_data_sources_names_the_default_folder(home, env):
    settings.configure({"data_dirs": str(env["data_dir"])})
    out = call(tools.data_sources)
    assert (
        f"Drop CSV, Excel, Parquet, JSON or SQLite files into `{home['default']}`" in out
        and "or add folders in Settings ▸ Plugins ▸ Data Analyst ▸ Data folders" in out
    ), out
    assert f"`{env['data_dir']}`" in out  # data_dirs listed too


# ── the rest of the agent home stays refused ────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    ["config/settings.csv", "config/config.yaml", "secrets.yaml", "checkpoints.db", "knowledge.db", "memory/facts.csv"],
)
def test_rest_of_agent_home_refused_even_when_allowlisted(home, rel):
    # Worst case: the operator allowlisted the folders holding them. Still refused — only the
    # default data folder is carved out.
    settings.configure({"data_dirs": "\n".join(str(home["agent_home"] / d) for d in ("config", "memory"))})
    target = home["agent_home"] / rel
    out = call(tools.data_connect, path=str(target))
    assert not _connected(out), out
    assert "agent's home" in out or "credentials" in out or "outside the allowlisted" in out, out


@pytest.mark.parametrize("rel", ["config", "memory", "."])
def test_agent_home_folders_refused(home, rel):
    settings.configure({"data_dirs": "\n".join(str(home["agent_home"] / d) for d in ("config", "memory"))})
    out = call(tools.data_connect, path=str((home["agent_home"] / rel).resolve()))
    assert not _connected(out), out


@pytest.mark.parametrize("rel", ["memory/facts.csv", "checkpoints.db", "config/settings.csv"])
def test_symlink_from_default_folder_into_agent_home_refused(home, rel):
    home["default"].mkdir(parents=True)
    link = home["default"] / ("link" + Path(rel).suffix)
    link.symlink_to(home["agent_home"] / rel)
    out = call(tools.data_connect, path=str(link))
    assert _refused(out), out
    # …and walking the folder skips it too
    out = call(tools.data_connect, path=str(home["default"]))
    assert not _connected(out), out


def test_dir_symlink_from_default_folder_into_agent_home_refused(home):
    home["default"].mkdir(parents=True)
    (home["default"] / "mem").symlink_to(home["agent_home"] / "memory", target_is_directory=True)
    out = call(tools.data_connect, path=str(home["default"] / "mem"))
    assert not _connected(out), out
    out = call(tools.data_connect, path=str(home["default"] / "mem" / "facts.csv"))
    assert _refused(out), out


def test_symlink_from_default_folder_to_outside_is_an_outside_path(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.csv").write_text(CSV)
    home["default"].mkdir(parents=True)
    link = home["default"] / "x.csv"
    link.symlink_to(outside / "x.csv")
    out = call(tools.data_connect, path=str(link))
    assert not _connected(out) and "outside the allowlisted data folders" in out, out
    settings.configure({"data_dirs": str(outside)})  # now it's under data_dirs → readable
    out = call(tools.data_connect, path=str(link))
    assert _connected(out), out


def test_connected_file_rechecked_at_query_time(home):
    f = _drop(home)
    assert _connected(call(tools.data_connect, path=str(f)))
    f.unlink()
    f.symlink_to(home["agent_home"] / "memory" / "facts.csv")  # swapped for a link into the home
    out = call(tools.data_query, sql="SELECT * FROM coffee")
    assert _refused(out) and "`coffee` skipped" in out, out


def test_credential_names_and_hardlinks_still_refused_in_default_folder(home):
    f = _drop(home, "secrets.json", '[{"a": 1}]')
    out = call(tools.data_connect, path=str(f))
    assert not _connected(out) and "credentials" in out, out
    g = _drop(home)
    os.link(g, home["default"] / "again.csv")
    out = call(tools.data_connect, path=str(g))
    assert not _connected(out) and "hardlinked" in out, out


def test_default_folder_that_is_a_symlink_is_not_trusted(home):
    ws = home["agent_home"] / "workspace"
    ws.mkdir(parents=True)
    (ws / "data").symlink_to(home["agent_home"], target_is_directory=True)  # would alias the home
    root, note = fence.default_root()
    assert root is None and "not a plain directory" in note
    out = call(tools.data_connect, path=str(ws / "data" / "memory" / "facts.csv"))
    assert not _connected(out) and "s3cret" not in out, out
    out = call(tools.data_connect, path=str(home["agent_home"] / "memory" / "facts.csv"))
    assert not _connected(out), out


def test_default_folder_that_contains_a_home_is_not_trusted(home, monkeypatch):
    monkeypatch.setenv("DATA_DEFAULT_DIR", str(home["agent_home"].parent))
    root, note = fence.default_root()
    assert root is None and "contains a home dir" in note


# ── use_default_folder: false ───────────────────────────────────────────────


def test_use_default_folder_false_disables_it(home):
    f = _drop(home)
    settings.configure({"use_default_folder": False})
    assert fence.default_root() == (None, None)
    out = call(tools.data_connect, path=str(f))
    assert not _connected(out) and "No data folders are allowlisted" in out, out
    assert str(home["default"]) not in call(tools.data_sources)
    reg = FakeRegistry({"use_default_folder": False})
    data.register(reg)
    assert "data_dirs" in reg.gaps  # nothing to read from → the setup gap is back


@pytest.mark.parametrize("val", ["false", "off", "0", False])
def test_use_default_folder_false_spellings(home, val):
    settings.configure({"use_default_folder": val})
    assert settings.flag("use_default_folder") is False


def test_use_default_folder_false_refuses_the_carve_out_even_via_data_dirs(home):
    f = _drop(home)
    settings.configure({"use_default_folder": False, "data_dirs": str(home["default"])})
    out = call(tools.data_connect, path=str(f))
    assert not _connected(out) and "agent's home" in out, out


# ── exports never land in the default folder ────────────────────────────────


def test_export_lands_beside_not_inside_the_default_folder(home):
    f = _drop(home)
    assert _connected(call(tools.data_connect, path=str(f)))
    out = call(tools.data_export, sql="SELECT * FROM coffee", format="csv", filename="out")
    assert f"→ {home['exports'] / 'out.csv'}" in out, out
    assert sorted(p.name for p in home["default"].iterdir()) == ["coffee.csv"]


@pytest.mark.parametrize("enabled", [True, False])
def test_export_dir_inside_default_folder_refused(home, monkeypatch, enabled):
    f = _drop(home)
    assert _connected(call(tools.data_connect, path=str(f)))
    settings.configure({"use_default_folder": enabled, "data_dirs": "" if enabled else str(home["default"].parent)})
    monkeypatch.setenv("DATA_EXPORT_DIR", str(home["default"] / "exports"))
    out = call(tools.data_export, sql="SELECT 1 AS a", format="csv", filename="x")
    assert "exports never write into source folders" in out, out
    assert not (home["default"] / "exports" / "x.csv").exists()


# ── the setup gap follows a Settings save (no restart) ──────────────────────


class ReloadingHost(FakeRegistry):
    """Core's Save & apply: a NEW registry whose ``config`` is the saved values, while
    ``live_config()`` still serves the old ones — register() runs before the commit."""

    def __init__(self, new, live, gaps):
        super().__init__(new)
        self._live = live
        self.gaps = gaps  # one process-wide gap store across reloads

    def live_config(self):
        return self._live[0]


def test_saving_a_folder_clears_the_gap_without_a_restart(home, env):
    gaps: dict = {}
    live = [{"use_default_folder": False}]
    data.register(ReloadingHost({"use_default_folder": False}, live, gaps))
    assert "data_dirs" in gaps  # nothing to read: default off, no data_dirs

    saved = {"use_default_folder": False, "data_dirs": str(env["data_dir"])}
    data.register(ReloadingHost(saved, live, gaps))  # register() during the reload — live is still old
    assert "data_dirs" not in gaps
    live[0] = saved  # the reload commits

    data.register(ReloadingHost({"use_default_folder": False}, live, gaps))  # folder removed again
    assert "data_dirs" in gaps
    data.register(ReloadingHost({}, live, gaps))  # default folder back on → cleared
    assert "data_dirs" not in gaps


def test_tools_resync_the_gap_from_the_live_config(home, env):
    """A reload that reuses the plugin bundle never re-runs register(): the next tool call fixes it."""
    gaps: dict = {}
    live = [{"use_default_folder": False}]
    data.register(ReloadingHost({"use_default_folder": False}, live, gaps))
    assert "data_dirs" in gaps
    live[0] = {"use_default_folder": False, "data_dirs": str(env["data_dir"])}
    call(tools.data_sources)
    assert "data_dirs" not in gaps
    live[0] = {"use_default_folder": False}
    call(tools.data_connect, path=str(env["data_dir"]))
    assert "data_dirs" in gaps


def test_gap_only_when_default_off_and_no_data_dirs(home, env):
    for conf, raised in [
        ({}, False),
        ({"data_dirs": str(env["data_dir"])}, False),
        ({"use_default_folder": False, "data_dirs": str(env["data_dir"])}, False),
        ({"use_default_folder": False}, True),
    ]:
        reg = FakeRegistry(conf)
        data.register(reg)
        assert ("data_dirs" in reg.gaps) is raised, conf
