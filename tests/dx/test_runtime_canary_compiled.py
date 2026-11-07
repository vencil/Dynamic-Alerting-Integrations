"""The runtime canary's compiled rules are the compiler's output, and stay so (#3679).

`tests/rulepacks/runtime-canary.compiled.rules.yaml` claims to be what the real
custom-alert compiler emits for `tests/rulepacks/runtime-canary-recipe/`. Before
#3679 that claim lived in a comment, and the bundle fell two compiler changes
behind (#3309's `maintenance` label, #2566's metadata annotations) with every gate
green. These tests pin the gate that now holds it:

  * the committed bundle passes `compile_custom_alerts.py --check` against its
    recipe (the gate is green on main);
  * a bundle that lost one annotation does not (the gate can turn red), and the
    remedy it prints rewrites THAT file, not the shipped pack;
  * the same `--check` invocation is wired into the Makefile target, the
    pre-commit hook and the CI step — a gate nobody runs is not one.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
_LINT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools")
for _p in (_DX, _LINT, _TOOLS):
    sys.path.insert(0, _p)

import compile_custom_alerts as cc  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]
RECIPE_REL = "tests/rulepacks/runtime-canary-recipe"
COMPILED_REL = "tests/rulepacks/runtime-canary.compiled.rules.yaml"
CHECK_ARGS = f"--check --config-dir {RECIPE_REL} --out {COMPILED_REL}"


def _check(out: Path, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["compile", "--check", "--config-dir",
                                      str(_REPO / RECIPE_REL), "--out", str(out)])
    return cc.main()


def test_the_committed_canary_is_the_compilers_output(monkeypatch, capsys):
    assert _check(_REPO / COMPILED_REL, monkeypatch) == cc.EXIT_OK, capsys.readouterr()


def test_a_canary_that_lost_an_annotation_is_refused(tmp_path, monkeypatch, capsys):
    # The acceptance case of #3679: let the canary fall behind once more.
    stale = tmp_path / "canary.yaml"
    shutil.copyfile(_REPO / COMPILED_REL, stale)
    text = stale.read_text(encoding="utf-8")
    without_tier = re.sub(r"(?m)^ +tier: .*\n", "", text, count=1)
    assert without_tier != text, "the compiled canary carries no tier annotation to drop"
    stale.write_text(without_tier, encoding="utf-8")

    assert _check(stale, monkeypatch) == cc.EXIT_VIOLATION
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "content differs" in out
    # The remedy names the compared file, never the shipped pack's make target:
    # following that would rewrite rule-pack-custom-alerts.yaml and leave this red.
    assert str(stale) in out
    assert "make custom-alerts-compile" not in out


def test_the_shipped_pack_keeps_its_make_target_however_it_is_spelled():
    # The Makefile writes it as `$(CURDIR)/…` (absolute), the checks name it
    # relatively or not at all: one file, one command, one header.
    conf = _REPO / cc.DEFAULT_CONFIG_REL
    for out in (_REPO / cc.OUT_REL, Path(cc.OUT_REL)):
        assert cc._regenerate_command(out, conf, _REPO) == "make custom-alerts-compile"


def test_another_file_gets_its_own_repo_relative_command():
    expected = ("bash scripts/ops/with_da_guard.sh python3 scripts/tools/dx/compile_custom_alerts.py "
                f"--config-dir {RECIPE_REL} --out {COMPILED_REL}")
    for out, conf in ((_REPO / COMPILED_REL, _REPO / RECIPE_REL), (Path(COMPILED_REL), Path(RECIPE_REL))):
        assert cc._regenerate_command(out, conf, _REPO) == expected
    # …and the committed canary's header says exactly that.
    assert f"# Run `{expected}`" in (_REPO / COMPILED_REL).read_text(encoding="utf-8")


def _edited_copy(tmp_path: Path, edit) -> Path:
    stale = tmp_path / "canary.yaml"
    text = (_REPO / COMPILED_REL).read_text(encoding="utf-8")
    edited = edit(text)
    assert edited != text, "the edit changed nothing"
    stale.write_text(edited, encoding="utf-8")
    return stale


_DUPLICATE = (
    "- name: custom-alerts\n  rules:\n"
    "  - alert: Custom_threshold__canary_pipeline_heartbeat__gt__w5m__for1m\n"
    "    expr: up{job=\"nonexistent\"} == 0\n"
    "    labels:\n      severity: critical\n")


@pytest.mark.parametrize("label,edit", [
    # Review of #3679: both left the canary green under the rule-identity
    # comparison alone (a duplicate overwrites the first in its map; group
    # fields are not in it).
    ("duplicate rule under an existing name",
     lambda t: t.replace("- name: custom-alerts\n  rules:\n", _DUPLICATE, 1)),
    ("group field",
     lambda t: t.replace("- name: custom-alerts\n  rules:\n",
                         "- name: custom-alerts\n  limit: 1\n  rules:\n", 1)),
    ("header", lambda t: t.replace("DO NOT EDIT", "edit freely", 1)),
])
def test_an_edit_the_rule_comparison_does_not_key_on_is_refused(label, edit, tmp_path, monkeypatch, capsys):
    assert _check(_edited_copy(tmp_path, edit), monkeypatch) == cc.EXIT_VIOLATION, label
    assert "not byte-for-byte" in capsys.readouterr().out


def _precommit_entry(hook_id: str) -> str:
    cfg = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in cfg["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == hook_id:
                return hook["entry"]
    raise AssertionError(f"pre-commit hook {hook_id} not found")


def _ci_runs() -> list[str]:
    wf = yaml.safe_load((_REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    return [step.get("run", "") for job in wf["jobs"].values()
            for step in job.get("steps", [])]


def _make_recipe(target: str) -> str:
    lines = (_REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


@pytest.mark.parametrize("where,text", [
    ("pre-commit", lambda: _precommit_entry("runtime-canary-compile-check")),
    ("make", lambda: _make_recipe("runtime-canary-compile-check")),
    ("ci", lambda: "\n".join(r for r in _ci_runs() if RECIPE_REL in r)),
])
def test_the_gate_is_wired_everywhere(where, text):
    body = text()
    assert "compile_custom_alerts.py" in body, f"{where}: no compiler run"
    assert CHECK_ARGS in body, f"{where}: not `{CHECK_ARGS}`"


def test_the_writer_target_writes_the_checked_file():
    body = _make_recipe("runtime-canary-compile")
    assert f"--config-dir {RECIPE_REL}" in body
    assert f"/{COMPILED_REL}" in body and "--check" not in body
