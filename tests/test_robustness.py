"""Review fixes for v0.1.0: a bad file or setting is a refusal (never a crash, never left
registered), spill is capped, and the registry's cache paths aren't trusted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data import engine, paths, settings, sources, tools

from conftest import call


@pytest.fixture
def connected(env):
    assert "Connected 1 source" in call(tools.data_connect, path=str(env["files"]["csv"]))
    return env


def _bad_parquet(env) -> Path:
    f = env["data_dir"] / "bad.parquet"
    f.write_bytes(b"junk")
    return f


def test_a_corrupt_file_is_refused_and_never_registered(env):
    out = call(tools.data_connect, path=str(_bad_parquet(env)))
    assert "Nothing connected" in out and "bad.parquet skipped" in out
    assert "bad" not in sources.load()
    # and a later query is unaffected
    call(tools.data_connect, path=str(env["files"]["csv"]))
    assert "| weekday |" in call(tools.data_query, sql="SELECT weekday FROM sales LIMIT 1")


def test_a_folder_with_one_corrupt_file_connects_the_rest(env):
    _bad_parquet(env)
    out = call(tools.data_connect, path=str(env["data_dir"]))
    assert "Connected" in out and "bad.parquet skipped" in out
    assert "| sales |" in out and "bad" not in sources.load()


def test_a_poisoned_registry_entry_is_a_refusal_not_a_crash(env):
    """An entry that was registered before the fix (or a file corrupted after connect)."""
    bad = _bad_parquet(env)
    sources.save({"bad": {"kind": "parquet", "path": str(bad)}})
    out = call(tools.data_query, sql="SELECT * FROM bad")
    assert out.startswith("Failed opening `bad`")
    assert "| x |" in call(tools.data_query, sql="SELECT 1 AS x")  # queries not naming it still run


@pytest.mark.parametrize(
    ("raw", "want"),
    [("lots", "1024MB"), ("", "1024MB"), ("512MB", "512MB"), ("2 gb", "2048MB"), ("1", "64MB"), ("999GB", "8192MB")],
)
def test_memory_limit_is_validated_and_clamped(env, raw, want):
    settings.configure({"data_dirs": str(env["data_dir"]), "memory_limit": raw})
    assert settings.memory_limit() == want


def test_an_invalid_memory_limit_never_breaks_queries(connected):
    settings.configure({"data_dirs": str(connected["data_dir"]), "memory_limit": "lots", "timeout_s": "nope"})
    assert "| x |" in call(tools.data_query, sql="SELECT 1 AS x")


def test_timeout_is_clamped(env):
    settings.configure({"data_dirs": str(env["data_dir"]), "timeout_s": 100000})
    assert settings.timeout_s() == settings.TIMEOUT_MAX_S


def test_spill_is_capped_on_disk(env):
    conn = engine.open_locked([])
    try:
        got = conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    finally:
        conn.close()
    assert got.replace(" ", "").upper().startswith(("1.0GIB", "1GIB", "1GB", "953.6MIB", "1000.0MB", "0.9GIB"))


def test_a_query_that_would_spill_past_the_cap_fails_cleanly(connected, monkeypatch):
    """64 MB of memory and a hash table far bigger than that: it spills, hits the spill cap
    (shrunk to 32 MB here so the test is quick) and comes back as a refusal, with the disk bounded."""
    monkeypatch.setattr(settings, "SPILL_MAX", "32MB")
    settings.configure({"data_dirs": str(connected["data_dir"]), "memory_limit": "64MB", "timeout_s": 60})
    out = call(
        tools.data_query,
        sql="SELECT count(*) AS n FROM (SELECT DISTINCT md5(i::VARCHAR) || repeat('x', 100) AS s FROM range(3000000) t(i))",
    )
    assert "| n |" not in out, out[:300]  # it did NOT complete past the caps
    assert "max_temp_directory_size" in out or "Out of Memory" in out or "temp" in out.lower(), out[:300]
    spill = paths.spill_dir()
    assert sum(f.stat().st_size for f in spill.rglob("*") if f.is_file()) < 40 * 2**20


@pytest.mark.parametrize(
    "sql",
    ["SELECT * FROM duckdb_settings()", "SELECT current_setting('temp_directory')", "SELECT sql FROM duckdb_views()"],
)
def test_introspection_of_internal_paths_is_refused(connected, sql):
    assert "isn't available here" in call(tools.data_query, sql=sql)


def test_a_cache_path_on_a_csv_source_is_dropped(env, tmp_path):
    """sources.json is a file: a `cache` it carries must never reach allowed_paths."""
    secret = tmp_path / "elsewhere.parquet"
    import duckdb

    duckdb.connect().execute(f"COPY (SELECT 'leak' AS s) TO '{secret}' (FORMAT parquet)")
    csv_src = env["files"]["csv"]
    raw = {"sales": {"kind": "csv", "path": str(csv_src), "cache": str(secret)}}
    paths.sources_file().write_text(json.dumps({"sources": raw}))
    assert "cache" not in sources.load()["sales"]
    out = call(tools.data_query, sql="SELECT * FROM sales LIMIT 1")
    assert "leak" not in out and "| weekday" in out


def test_a_snapshot_cache_outside_the_plugin_cache_is_dropped(env, tmp_path):
    db = env["files"]["sqlite"]
    rogue = tmp_path / "rogue.parquet"
    rogue.write_bytes(b"x")
    raw = {"t": {"kind": "sqlite", "path": str(db), "table": "stores", "cache": str(rogue), "sig": [0, 0]}}
    paths.sources_file().write_text(json.dumps({"sources": raw}))
    assert "cache" not in sources.load()["t"]
    ok, _ = sources.usable()
    [t] = [s for s in ok if s["name"] == "t"]
    assert Path(t["cache"]).resolve().parent == paths.cache_dir()  # re-snapshotted into OUR cache


def test_unknown_kinds_are_dropped_from_the_registry(env):
    paths.sources_file().write_text(json.dumps({"sources": {"x": {"kind": "exe", "path": "/bin/sh"}}}))
    assert sources.load() == {}
