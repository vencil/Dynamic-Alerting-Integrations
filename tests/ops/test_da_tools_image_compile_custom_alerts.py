"""`da-tools compile-custom-alerts` in the image: a customer's conf.d → rule pack (#4225).

ADR-024 Capability B compiles a tree's `_custom_alerts` into one vectorised
rule set per shape. Until #4225 only this repository could run the compiler
(`make custom-alerts-compile`, pinned to its own conf.d): the image shipped
neither the command nor its modules, so a customer had no way to turn their
own declarations into rules.

Pinned here without building the image, on a flat tree laid out from
build.sh's own TOOL_FILES / REPO_DATA_FILES plus entrypoint.py (the
Dockerfile copies both into one WORKDIR), run from outside the repository
with PYTHONPATH dropped, on a copy of the recipe example tree:

  * the command dispatches and writes a pack Prometheus' own rule loader
    (promtool) accepts;
  * `--check` is 0 on that pack and 1 once the pack drifts from the tree —
    the customer adding a recipe as much as the pack being edited;
  * every command it prints for the reader to run (the drift remedy, the
    pack's header) is the image's `da-tools compile-custom-alerts`, never a
    repository path that does not exist there; `--help` says both flags are
    required, as they are;
  * outside the repository there is no default to fall back on, so a run
    without --out is a named 2, never a write into the image.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_TOOLS = _REPO / "scripts" / "tools"
_ENTRYPOINT = _REPO / "components" / "da-tools" / "app" / "entrypoint.py"
_EXAMPLE_TREE = _REPO / "rule-packs" / "recipes" / "examples" / "conf.d"

sys.path.insert(0, str(_TOOLS / "lint"))
import _lint_helpers  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_read_env")


@pytest.fixture(scope="module")
def image(tmp_path_factory) -> Path:
    app = tmp_path_factory.mktemp("app")
    for rel in _lint_helpers.parse_build_sh_tool_paths():
        shutil.copy(_TOOLS / rel, app / os.path.basename(rel))
    for rel in _lint_helpers.parse_build_sh_repo_data_files():
        shutil.copy(_REPO / rel, app / os.path.basename(rel))
    shutil.copy(_ENTRYPOINT, app / "entrypoint.py")
    return app


@pytest.fixture
def work(tmp_path) -> Path:
    shutil.copytree(_EXAMPLE_TREE, tmp_path / "conf.d")
    return tmp_path


def _da_tools(image: Path, work: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, str(image / "entrypoint.py"), "compile-custom-alerts", *args],
        cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
        check=False, timeout=300)


def test_the_image_compiles_a_customer_tree_into_a_pack_prometheus_loads(image, work):
    done = _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml")
    assert done.returncode == 0, done.stderr
    groups = yaml.safe_load((work / "pack.yaml").read_text(encoding="utf-8"))["groups"]
    assert any(r.get("alert") for g in groups for r in g["rules"]), groups
    promtool = shutil.which("promtool")
    if promtool is None:
        pytest.skip("promtool not on PATH: the rule loader's verdict is not checked")
    checked = subprocess.run([promtool, "check", "rules", "pack.yaml"], cwd=work,
                             capture_output=True, text=True, encoding="utf-8",
                             check=False, timeout=120)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_check_is_0_in_sync_and_1_once_the_pack_drifts(image, work):
    assert _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml").returncode == 0
    in_sync = _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml", "--check")
    assert in_sync.returncode == 0, in_sync.stderr
    pack = work / "pack.yaml"
    doc = yaml.safe_load(pack.read_text(encoding="utf-8"))
    doc["groups"] = doc["groups"][:-1]
    pack.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    drifted = _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml", "--check")
    assert drifted.returncode == 1, drifted.stdout + drifted.stderr


@pytest.mark.parametrize("args", [
    ("--config-dir", "conf.d"),
    ("--config-dir", "conf.d", "--check"),
], ids=["write", "check"])
def test_outside_the_repository_out_is_required(image, work, args):
    before = sorted(p.name for p in image.iterdir())
    done = _da_tools(image, work, *args)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "Pass both --config-dir and --out" in done.stderr
    assert sorted(p.name for p in image.iterdir() if p.name != "__pycache__") == [
        n for n in before if n != "__pycache__"]


_NEW_SHAPE = """tenants:
  shop-z:
    _custom_alerts:
      - recipe: threshold
        name: backlog_high
        metric: shop_z_backlog_depth
        op: ">"
        window: 5m
        threshold: "50:warning"
"""


def test_check_turns_red_when_the_customer_adds_a_recipe(image, work):
    assert _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml").returncode == 0
    (work / "conf.d" / "shop-z.yaml").write_text(_NEW_SHAPE, encoding="utf-8")
    drifted = _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml", "--check")
    assert drifted.returncode == 1, drifted.stdout + drifted.stderr
    said = drifted.stdout + drifted.stderr
    assert "da-tools compile-custom-alerts --config-dir" in said, said
    assert "scripts/" not in said and "with_da_guard" not in said, said


def test_the_pack_header_names_the_image_command(image, work):
    assert _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml").returncode == 0
    head = (work / "pack.yaml").read_text(encoding="utf-8").split("groups:", 1)[0]
    assert "da-tools compile-custom-alerts --config-dir <tree> --out <this file>" in head, head


def test_help_in_the_image_says_both_flags_are_required(image, work):
    done = _da_tools(image, work, "--help")
    assert done.returncode == 0, done.stderr
    assert "required here" in done.stdout and "default: components/" not in done.stdout


def test_the_disk_prerequisite_names_the_image_byo_check(image, work):
    """The example tree has a disk-fill recipe: the notice names the command
    the image has, not the repository script."""
    done = _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml")
    assert done.returncode == 0, done.stderr
    assert "`da-tools byo-check prometheus`" in done.stderr, done.stderr
    assert "byo_check.py" not in done.stderr


def test_refusing_to_empty_a_pack_offers_the_image_command(image, work, tmp_path):
    assert _da_tools(image, work, "--config-dir", "conf.d", "--out", "pack.yaml").returncode == 0
    bare = work / "bare"
    bare.mkdir()
    (bare / "_defaults.yaml").write_text(
        "defaults:\n  mysql_connection_usage_percent: \"80\"\n", encoding="utf-8")
    (bare / "ta.yaml").write_text(
        "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n", encoding="utf-8")
    refused = _da_tools(image, work, "--config-dir", "bare", "--out", "pack.yaml")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    said = refused.stdout + refused.stderr
    assert "da-tools compile-custom-alerts --config-dir bare --out pack.yaml --allow-empty" in said, said
