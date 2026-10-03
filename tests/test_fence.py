"""The data_dirs allowlist: symlink / '..' / hardlink escapes, credential names, the agent home,
too-broad roots, an empty allowlist — and re-validation at query time."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data import fence, settings, tools

from conftest import call


def test_empty_data_dirs_refuses_every_connect(env):
    settings.configure({"data_dirs": ""})
    out = call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "No data folders are allowlisted" in out and "operator" in out


def test_outside_file_is_refused(env, tmp_path):
    out_file = tmp_path / "elsewhere.csv"
    out_file.write_text("a\n1\n")
    out = call(tools.data_connect, path=str(out_file))
    assert "outside the allowlisted data folders" in out


def test_symlink_escape_is_refused(env, tmp_path):
    target = tmp_path / "secret-outside.csv"
    target.write_text("pw\nhunter2\n")
    link = env["data_dir"] / "innocent.csv"
    link.symlink_to(target)
    out = call(tools.data_connect, path=str(link))
    assert "outside the allowlisted" in out
    # and a folder connect skips it rather than following it out
    out = call(tools.data_connect, path=str(env["data_dir"]))
    assert "innocent" not in out.split("Query them")[0]


def test_dotdot_escape_is_refused(env, tmp_path):
    (tmp_path / "up.csv").write_text("a\n1\n")
    out = call(tools.data_connect, path=str(env["data_dir"] / ".." / "up.csv"))
    assert "outside the allowlisted" in out


def test_symlinked_folder_escape_is_refused(env, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "x.csv").write_text("a\n1\n")
    (env["data_dir"] / "linkdir").symlink_to(outside, target_is_directory=True)
    out = call(tools.data_connect, path=str(env["data_dir"] / "linkdir"))
    assert "outside the allowlisted" in out


def test_hardlink_is_refused(env, tmp_path):
    src = tmp_path / "hl-src.csv"
    src.write_text("a\n1\n")
    hl = env["data_dir"] / "hl.csv"
    os.link(src, hl)
    out = call(tools.data_connect, path=str(hl))
    assert "hardlinked" in out


@pytest.mark.parametrize("name", [".env", "secrets.yaml", "id_rsa", "server.pem", "credentials.json"])
def test_credential_names_are_refused(env, name):
    f = env["data_dir"] / name
    f.write_text("a\n1\n")
    reason, _ = fence.file_problem(f, [env["data_dir"]])
    assert reason and "credentials" in reason


def test_credential_dir_is_refused(env):
    d = env["data_dir"] / ".aws"
    d.mkdir()
    (d / "costs.csv").write_text("a\n1\n")
    out = call(tools.data_connect, path=str(d / "costs.csv"))
    assert "credentials directory" in out


def test_agent_home_is_refused_even_inside_an_allowlisted_dir(env):
    inside = env["agent_home"] / "exports"
    inside.mkdir()
    (inside / "chats.csv").write_text("a\n1\n")
    settings.configure({"data_dirs": str(inside)})
    out = call(tools.data_connect, path=str(inside / "chats.csv"))
    assert "agent's home" in out


@pytest.mark.parametrize("which", ["root", "home", "agent_home_parent"])
def test_too_broad_roots_are_ignored(env, which):
    raw = {"root": "/", "home": str(Path.home()), "agent_home_parent": str(env["agent_home"].parent)}[which]
    roots, notes = fence.roots(raw)
    assert roots == [] and "too broad" in notes[0]


def test_relative_and_missing_roots_are_ignored(env):
    roots, notes = fence.roots("relative/dir, /no/such/dir")
    assert roots == [] and len(notes) == 2


def test_folder_connect_skips_hidden_and_credentials(env):
    (env["data_dir"] / ".env").write_text("SECRET=1\n")
    out = call(tools.data_connect, path=str(env["data_dir"]))
    assert "sales" in out and "SECRET" not in out


def test_removing_the_allowlist_entry_cuts_off_connected_sources(env):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "Sunday" in call(tools.data_query, sql="SELECT * FROM sales")
    settings.configure({"data_dirs": ""})
    out = call(tools.data_query, sql="SELECT * FROM sales")
    assert "No usable data sources" in out and "skipped" in out


def test_a_file_swapped_for_an_outside_symlink_is_dropped_at_query_time(env, tmp_path):
    call(tools.data_connect, path=str(env["files"]["csv"]))
    outside = tmp_path / "swap.csv"
    outside.write_text("pw\nhunter2\n")
    env["files"]["csv"].unlink()
    env["files"]["csv"].symlink_to(outside)
    out = call(tools.data_query, sql="SELECT * FROM sales")
    assert "hunter2" not in out and "outside the allowlisted" in out
