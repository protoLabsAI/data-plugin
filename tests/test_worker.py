"""The out-of-process engine — how the packaged desktop app runs DuckDB.

The frozen desktop host can't import a compiled dependency, and its installer REFUSES a hard
``scope: host`` dep outright ("needs duckdb as a HOST-scoped dep, which a frozen app cannot
satisfy") — which is exactly how v0.1.0 failed to install from the Analyst archetype. duckdb is
a runtime dep now, and ``engine`` runs each operation in the managed Python runtime via
``duck.py``. These tests pin the transport; the whole suite also runs through it in CI
(``DATA_TEST_WORKER=1``).
"""

from __future__ import annotations

import datetime as dt
import decimal
import io
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from conftest import FakeRegistry

ROOT = Path(__file__).resolve().parent.parent
from data import engine as _engine  # noqa: E402 — conftest registered the package

_REAL_WORKER_PYTHON = _engine.worker_python  # before DATA_TEST_WORKER's fixture swaps it


@pytest.fixture
def worker(monkeypatch):
    from data import engine

    monkeypatch.setattr(engine, "in_process", lambda: False)
    monkeypatch.setattr(engine, "worker_python", lambda: sys.executable)
    return engine


def test_duckdb_is_not_host_scoped():
    """A hard host-scoped dep is uninstallable on the desktop app (graph.plugins.installer.
    _refuse_frozen_host_scoped) — the bug this module exists for."""
    man = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    for entry in man["requires_pip"]:
        spec = entry if isinstance(entry, str) else entry["pkg"]
        if spec.startswith("duckdb"):
            assert isinstance(entry, str) or entry.get("scope", "runtime") == "runtime"
            assert not (isinstance(entry, dict) and entry.get("optional"))
            break
    else:
        pytest.fail("duckdb is not declared")


def test_duck_imports_only_stdlib_and_duckdb():
    """The worker runs with ``python -I duck.py``: no sibling module, no host on sys.path."""
    src = (ROOT / "duck.py").read_text()
    assert "from . " not in src and "from .duck" not in src
    assert "from graph" not in src and "import graph" not in src and "infra" not in src


_TYPED = """
SELECT 1.25::DECIMAL(10,2) AS dec, DATE '2026-07-06' AS d, TIMESTAMP '2026-07-06 10:11:12.5' AS ts,
       TIME '10:11:12' AS t, INTERVAL 3 DAY + INTERVAL 5 SECOND AS iv, '\\x01\\xff'::BLOB AS b,
       '4b7d2c3e-0000-4000-8000-000000000001'::UUID AS u, [1, 2, NULL] AS l, {'a': 1, 'b': 'x'} AS s,
       NULL AS n, 'nan'::DOUBLE AS nan_, 7 AS i, 'text' AS txt, true AS flag
FROM sales LIMIT 1
"""


def test_values_cross_the_process_boundary_with_their_types(env, worker):
    from data import engine

    from data import fence

    path = env["files"]["csv"]
    srcs = [{"name": "sales", "kind": "csv", "path": str(path), "ident": fence.identity(path)}]
    remote = engine.run_query(srcs, _TYPED, cap=5, timeout_s=20)

    import duckdb  # the same query in-process is the oracle

    conn = engine.open_locked(srcs)
    try:
        local = conn.execute(_TYPED).fetchall()
    finally:
        conn.close()
    got, want = remote.rows[0], local[0]
    assert len(got) == len(want)
    for g, w in zip(got, want):
        if isinstance(w, float) and w != w:
            assert isinstance(g, float) and g != g
        else:
            assert g == w and type(g) is type(w), (g, w)
    assert isinstance(got[0], decimal.Decimal) and isinstance(got[1], dt.date) and isinstance(got[6], uuid.UUID)
    assert duckdb.__version__


def test_refusals_keep_their_message_through_the_worker(env, worker):
    from data import engine

    from data import fence

    path = env["files"]["csv"]
    srcs = [{"name": "sales", "kind": "csv", "path": str(path), "ident": fence.identity(path)}]
    with pytest.raises(engine.QueryError, match="only SELECT"):
        engine.run_query(srcs, "CREATE TABLE x AS SELECT 1", cap=5, timeout_s=20)
    with pytest.raises(engine.QueryError, match="Refused by the read-only engine"):
        engine.run_query(srcs, f"SELECT * FROM read_text('{env['tmp']}/nope')", cap=5, timeout_s=20)


def test_frozen_app_without_a_runtime_says_how_to_provision_it(monkeypatch):
    from data import engine

    monkeypatch.setattr(engine, "in_process", lambda: False)
    monkeypatch.setattr(engine, "worker_python", _REAL_WORKER_PYTHON)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert engine.worker_python() is None  # no host sdk here → no managed runtime
    with pytest.raises(engine.QueryError, match="managed Python runtime"):
        engine.call("ping", timeout_s=5)


def _fake_run(stdout="", stderr="", rc=0, exc=None):
    def run(*a, **k):
        if exc:
            raise exc
        return subprocess.CompletedProcess(a[0], rc, stdout=stdout, stderr=stderr)

    return run


def test_a_runtime_without_duckdb_names_install_deps(monkeypatch, worker):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=json.dumps({"missing": "No module named 'duckdb'"})))
    with pytest.raises(worker.QueryError, match="Install dependencies"):
        worker.call("ping", timeout_s=5)


def test_worker_main_reports_a_missing_duckdb(monkeypatch):
    from data import duck

    def boom(req):
        raise ImportError("No module named 'duckdb'")

    monkeypatch.setattr(duck, "dispatch", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"op": "ping"}'))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert duck.main() == 0
    assert json.loads(out.getvalue()) == {"missing": "No module named 'duckdb'"}


def test_a_crashed_worker_surfaces_its_last_stderr_line(monkeypatch, worker):
    monkeypatch.setattr(subprocess, "run", _fake_run(stderr="Traceback …\n  File x\nMemoryError: boom\n", rc=1))
    with pytest.raises(worker.QueryError, match="crashed: MemoryError: boom"):
        worker.call("ping", timeout_s=5)


def test_a_hung_worker_is_a_timeout(monkeypatch, worker):
    monkeypatch.setattr(subprocess, "run", _fake_run(exc=subprocess.TimeoutExpired("py", 1)))
    with pytest.raises(worker.QueryError, match="timed out after 5s"):
        worker.call("ping", timeout_s=5)


def test_the_worker_env_carries_no_secrets(monkeypatch, worker):
    seen = {}

    def run(argv, **k):
        seen.update(k["env"])
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": "1"}), stderr="")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PYTHONPATH", "/frozen/pyz")
    monkeypatch.setattr(subprocess, "run", run)
    worker.call("ping", timeout_s=5)
    assert "OPENAI_API_KEY" not in seen and "PYTHONPATH" not in seen
    assert seen["argv"][1] == "-I" and seen["argv"][2].endswith("duck.py")


@pytest.mark.parametrize(
    "frozen, exe, gap",
    [(True, None, "managed Python runtime"), (False, "SELF", "Install dependencies"), (True, "/rt/python3", None)],
)
def test_register_reports_the_right_duckdb_gap(monkeypatch, frozen, exe, gap):
    import data
    from data import engine

    monkeypatch.setattr(engine, "in_process", lambda: False)
    monkeypatch.setattr(engine, "worker_python", lambda: sys.executable if exe == "SELF" else exe)
    reg = FakeRegistry({"data_dirs": ""})
    data.register(reg)
    if gap is None:
        assert "duckdb" not in reg.gaps  # a managed runtime missing duckdb is the host's deps banner
    else:
        assert gap in reg.gaps["duckdb"][0]
