"""register() contributions, manifest/pyproject/__version__ lockstep, and dependency licences."""

from __future__ import annotations

import re
from importlib import metadata

import pytest
import yaml

import data
from data import settings

from conftest import ROOT, FakeRegistry

TOOL_NAMES = ["data_connect", "data_sources", "data_schema", "data_query", "data_profile", "data_chart", "data_export"]


def test_register_contributes_tools_and_skills(env):
    reg = FakeRegistry({"data_dirs": str(env["data_dir"])})
    data.register(reg)
    assert [t.name for t in reg.tools] == TOOL_NAMES
    assert reg.skill_dirs == ["skills"]
    for skill in ("exploring-a-dataset", "building-a-chart"):
        text = (ROOT / "skills" / skill / "SKILL.md").read_text()
        assert text.startswith(f"---\nname: {skill}\ndescription: ")
    assert "data_dirs" not in reg.gaps


def test_register_reads_live_config_and_flags_missing_data_dirs(env):
    reg = FakeRegistry({})
    data.register(reg)
    assert "data_dirs" in reg.gaps and reg.gaps["data_dirs"][1]["kind"] == "plugin_config"
    reg.config["data_dirs"] = str(env["data_dir"])  # a Settings edit — no re-register needed
    assert settings.cfg()["data_dirs"] == str(env["data_dir"])


def test_versions_in_lockstep():
    man = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    py = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert man["version"] == py == data.__version__
    assert man["id"] == "data" and man["config_section"] == "data"


def test_data_dirs_is_operator_only():
    man = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    fields = {s["key"]: s for s in man["settings"]}
    for key in ("data_dirs", "timeout_s", "memory_limit"):  # scope, run time and memory/spill: operator-only
        assert fields[key].get("spawns") is True, key
    assert set(man["config"]) == set(settings.DEFAULTS) == set(fields)
    assert man["capabilities"]["network"] == []


# Licences every declared runtime dependency must carry — permissive only.
ALLOWED_LICENCES = {"MIT", "BSD", "BSD-3-Clause", "Apache-2.0", "Apache Software License"}


@pytest.mark.parametrize("dist", ["duckdb", "openpyxl"])
def test_dependency_licences_are_permissive(dist):
    man = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    declared = {re.split(r"[<>=!~ ]", e["pkg"])[0] for e in man["requires_pip"]}
    assert dist in declared
    try:
        md = metadata.metadata(dist)
    except metadata.PackageNotFoundError:
        pytest.skip(f"{dist} not installed")
    found = {md.get("License-Expression") or "", md.get("License") or ""}
    found |= {c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")}
    assert any(any(a.lower() in f.lower() for a in ALLOWED_LICENCES) for f in found if f), found
    assert any("MIT" in f for f in found), f"{dist} is expected to be MIT: {found}"
