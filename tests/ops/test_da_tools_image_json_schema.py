"""The da-tools image gives a verdict on validate-config's `json_schema` row (#2744).

The row calls `lint/check_confd_schema.py` with the Python package
`jsonschema`. The image had neither, so the row was WARN "not checked" for
every customer running the image, whatever their conf.d held.

Three things have to hold for the image to judge, and each is pinned here
without building the image:

  * the Dockerfile installs jsonschema and every package it needs, at the
    versions CI tests;
  * build.sh ships the lint module beside the tool;
  * laid out flat the way build.sh lays the image out, validate-config
    reaches a PASS and a FAIL on that row.

The flat tree is built from build.sh's own TOOL_FILES / REPO_DATA_FILES, so a
file build.sh stops shipping is absent here too.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import requires
from pathlib import Path

import pytest
from packaging.requirements import Requirement

_REPO = Path(__file__).resolve().parents[2]
_TOOLS = _REPO / "scripts" / "tools"
_DOCKERFILE = _REPO / "components" / "da-tools" / "app" / "Dockerfile"
_CONSTRAINTS = _REPO / "requirements" / "ci-constraints.txt"

sys.path.insert(0, str(_TOOLS / "lint"))
import _lint_helpers  # noqa: E402

_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\&;]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pip_install_command() -> str:
    """The builder stage's `RUN pip install …`, continuations joined."""
    text = _DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    runs = [ln for ln in text.splitlines() if re.match(r"\s*RUN\s+pip\s+install\b", ln)]
    assert len(runs) == 1, f"expected one `RUN pip install` in the da-tools Dockerfile: {runs}"
    return runs[0]


def _image_python() -> dict[str, str]:
    """Marker environment of the interpreter the image runs: the Dockerfile's
    `FROM python:X.Y.Z-…`, not the one running this test."""
    tags = set(re.findall(r"^FROM python:(\d+)\.(\d+)\.(\d+)",
                          _DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE))
    assert len(tags) == 1, f"expected one python version across the FROM lines: {tags}"
    major, minor, patch = next(iter(tags))
    return {"extra": "", "python_version": f"{major}.{minor}",
            "python_full_version": f"{major}.{minor}.{patch}"}


def _closure(root: str) -> set[str]:
    """*root* and everything it requires, with no extra, on the image's Python."""
    env = _image_python()
    seen, todo = set(), [root]
    while todo:
        name = _norm(todo.pop())
        if name in seen:
            continue
        seen.add(name)
        for spec in requires(name) or []:
            req = Requirement(spec)
            if req.marker is None or req.marker.evaluate(env):
                todo.append(req.name)
    return seen


def test_the_image_installs_jsonschema_and_everything_it_requires():
    pinned = {_norm(n): v for n, v in _PIN.findall(_pip_install_command())}
    need = _closure("jsonschema")
    assert {"jsonschema", "rpds-py", "referencing"} <= need, f"anti-vacuity: {need}"
    missing = sorted(need - set(pinned))
    assert not missing, (
        f"the da-tools image installs jsonschema without pinning {missing}: pip would "
        f"resolve them at build time to whatever is newest, not what CI tests")


def test_the_rust_extension_must_come_as_a_wheel():
    """rpds-py has no pure-Python fallback. The builder stage has no Rust
    toolchain, and must not grow one because a wheel went missing."""
    named = [_norm(n) for value in re.findall(r"--only-binary[ =](\S+)", _pip_install_command())
             for n in value.split(",")]
    assert "rpds-py" in named or ":all:" in named, (
        f"`pip install` must pass `--only-binary rpds-py`; it names {named}")


def test_build_sh_ships_the_lint_module_the_row_calls():
    assert "lint/check_confd_schema.py" in _lint_helpers.parse_build_sh_tool_paths()


def test_the_build_lint_requires_the_module_wherever_validate_config_ships():
    """validate_config imports it inside `try/except ImportError`, so the
    lint's import scan cannot see the dependency; the pairing is declared."""
    import check_build_completeness as cbc
    assert "check_confd_schema.py" in cbc.REQUIRED_DATA_FILES["validate_config.py"]
    shipped = _lint_helpers.parse_build_sh_tools() - {"check_confd_schema.py"}
    errors = cbc.check_required_data_files(shipped)
    assert any("check_confd_schema.py" in msg for _sev, msg in errors), errors


# ── flat layout, as build.sh assembles the image ────────────────────────────

@pytest.fixture(scope="module")
def flat_image_tree(tmp_path_factory) -> Path:
    flat = tmp_path_factory.mktemp("opt-da-tools")
    for rel in _lint_helpers.parse_build_sh_tool_paths():
        shutil.copy(_TOOLS / rel, flat / os.path.basename(rel))
    for rel in _lint_helpers.parse_build_sh_repo_data_files():
        shutil.copy(_REPO / rel, flat / os.path.basename(rel))
    return flat


_DEFAULTS = "defaults:\n  mysql_connection_usage_percent: \"80\"\n"
_GOOD = "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n"
_BAD = _GOOD + "    _routing:\n      group_by: alertname\n"


def _json_schema_row(flat: Path, tree: Path) -> dict:
    # cwd is the tree-less tmp dir and PYTHONPATH is dropped: nothing may be
    # found through the repo checkout this test runs from.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(flat / "validate_config.py"), "--config-dir", str(tree), "--json"],
        cwd=flat, env=env, capture_output=True, text=True, encoding="utf-8",
        check=False, timeout=300)
    assert proc.stdout.strip(), f"no JSON document (rc={proc.returncode}):\n{proc.stderr}"
    doc = json.loads(proc.stdout)
    rows = {r["check"]: r for r in doc if isinstance(r, dict) and "check" in r}
    assert "json_schema" in rows, sorted(rows)
    return rows["json_schema"]


def _tree(tmp_path: Path, tenant: str) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    (d / "ta.yaml").write_text(tenant, encoding="utf-8")
    return d


@pytest.mark.usefixtures("da_guard_env", "da_read_env")
def test_flat_layout_passes_a_tree_that_fits_the_schema(flat_image_tree, tmp_path):
    row = _json_schema_row(flat_image_tree, _tree(tmp_path, _GOOD))
    assert row["status"] == "pass", row


@pytest.mark.usefixtures("da_guard_env", "da_read_env")
def test_flat_layout_fails_a_value_of_the_wrong_shape(flat_image_tree, tmp_path):
    row = _json_schema_row(flat_image_tree, _tree(tmp_path, _BAD))
    assert row["status"] == "fail", row
    assert any("/tenants/ta/_routing/group_by" in d for d in row["details"]), row


def test_flat_layout_finds_the_schemas_beside_the_module(flat_image_tree):
    """`check_confd_schema` used to count four directories up from `__file__`
    to reach `docs/schemas/`; the image has three ancestors and no repo tree."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = ("import check_confd_schema as c, os\n"
            "for p in (c._DEFAULT_SCHEMA, c._DEFAULT_PLATFORM_SCHEMA, c._DEFAULT_PROFILES_SCHEMA):\n"
            "    print(os.path.dirname(os.path.abspath(p)), os.path.isfile(p))\n"
            "c.default_registry()\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=flat_image_tree, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          check=False, timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines == [f"{flat_image_tree.resolve()} True"] * 3, lines


def test_each_default_is_its_own_schema_in_the_repo_layout():
    import check_confd_schema as ccs
    want = _REPO / "docs" / "schemas"
    assert [Path(p) for p in (ccs._DEFAULT_SCHEMA, ccs._DEFAULT_PLATFORM_SCHEMA,
                              ccs._DEFAULT_PROFILES_SCHEMA)] == [
        want / "tenant-config.schema.json", want / "platform-defaults.schema.json",
        want / "routing-profiles.schema.json"]
