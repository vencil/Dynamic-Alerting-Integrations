#!/usr/bin/env python3
"""Tests for check_devcontainer_dep_parity.py (#1254).

Covers three faces:
  1. Parsing — pip command tokenization, JSONC comment stripping, ci.yml job
     location. These are where a silent wrong-parse would make the gate pass
     vacuously.
  2. Verdicts — drift / ok / caller-error exit codes (dev-rules #13).
  3. Live repo — the real tree is clean AND a mutant (drop a package from the
     real devcontainer.json) must turn it red. Without the mutant half, an
     always-empty parse would look identical to "no drift".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_DIR = REPO_ROOT / "scripts" / "tools" / "lint"
if str(_LINT_DIR) not in sys.path:
    sys.path.insert(0, str(_LINT_DIR))

import check_devcontainer_dep_parity as mod  # noqa: E402

SCRIPT = _LINT_DIR / "check_devcontainer_dep_parity.py"

# A ci.yml skeleton whose job matches the full-suite detector.
_CI_TEMPLATE = """\
name: CI
on: [push]
jobs:
  aggregation-gate:
    name: Python Tests (3.13)
    steps:
      - run: echo "no tests here"
  python-tests-run:
    name: Python Tests — run (3.13)
    steps:
      - name: Install
        run: pip install {packages} -c requirements/ci-constraints.txt
      - name: Run
        run: pytest tests/ --tb=short -n auto --cov --cov-report=xml
"""

_DEVCONTAINER_TEMPLATE = """\
{{
  // a comment mentioning https://example.com must not break parsing
  "name": "vibe",
  "postCreateCommand": "pip3 install --user {packages} -c requirements/ci-constraints.txt && curl -Lo ./kind https://example.com/kind"
}}
"""


def _write_pair(tmp_path: Path, ci_packages: str, dev_packages: str):
    ci = tmp_path / "ci.yml"
    dev = tmp_path / "devcontainer.json"
    ci.write_text(_CI_TEMPLATE.format(packages=ci_packages), encoding="utf-8")
    dev.write_text(
        _DEVCONTAINER_TEMPLATE.format(packages=dev_packages), encoding="utf-8"
    )
    return ci, dev


def _run(ci: Path, dev: Path, *extra: str) -> int:
    return mod.main(["--ci", str(ci), "--devcontainer", str(dev), *extra])


# ── 1. Parsing ───────────────────────────────────────────────────────


class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("PyYAML", "pyyaml"),
        ("pytest_cov", "pytest-cov"),
        ("promql.parser", "promql-parser"),
        ("hypothesis==6.156.9", "hypothesis"),
        ("pytest>=8,<9", "pytest"),
        ("requests[security]", "requests"),
    ])
    def test_pep503_equivalence(self, raw, expected):
        assert mod.normalize(raw) == expected


class TestPackagesFromPipCommand:
    def test_basic(self):
        assert mod.packages_from_pip_command("pip install a b c") == {"a", "b", "c"}

    def test_flag_values_are_not_packages(self):
        # Regression guard: `-c requirements/ci-constraints.txt` must not
        # yield a bogus "requirements/ci-constraints.txt" package.
        pkgs = mod.packages_from_pip_command(
            "pip3 install --user pyyaml -c requirements/ci-constraints.txt"
        )
        assert pkgs == {"pyyaml"}

    def test_stops_at_command_chain(self):
        pkgs = mod.packages_from_pip_command(
            "pip install pyyaml && curl -Lo ./kind https://example.com/kind"
        )
        assert pkgs == {"pyyaml"}

    def test_python_dash_m_pip_form(self):
        assert mod.packages_from_pip_command("python -m pip install foo") == {"foo"}

    def test_multiple_installs_are_unioned(self):
        pkgs = mod.packages_from_pip_command(
            "pip install a && echo hi && pip install b"
        )
        assert pkgs == {"a", "b"}

    def test_non_pip_command_yields_nothing(self):
        assert mod.packages_from_pip_command("npm install left-pad") == set()


class TestDevcontainerParsing:
    def test_comment_with_url_survives(self, tmp_path):
        _, dev = _write_pair(tmp_path, "a", "pyyaml")
        assert mod.devcontainer_packages(dev) == {"pyyaml"}

    def test_missing_file_is_caller_error(self, tmp_path):
        with pytest.raises(mod.ParityError, match="not found"):
            mod.devcontainer_packages(tmp_path / "nope.json")

    def test_missing_postcreatecommand_is_caller_error(self, tmp_path):
        p = tmp_path / "devcontainer.json"
        p.write_text('{"name": "x"}', encoding="utf-8")
        with pytest.raises(mod.ParityError, match="postCreateCommand"):
            mod.devcontainer_packages(p)

    def test_list_form_postcreatecommand(self, tmp_path):
        p = tmp_path / "devcontainer.json"
        p.write_text(
            json.dumps({"postCreateCommand": ["pip", "install", "foo"]}),
            encoding="utf-8",
        )
        assert mod.devcontainer_packages(p) == {"foo"}

    def test_malformed_json_is_caller_error(self, tmp_path):
        p = tmp_path / "devcontainer.json"
        p.write_text("{ not json", encoding="utf-8")
        with pytest.raises(mod.ParityError, match="cannot parse"):
            mod.devcontainer_packages(p)


class TestCiParsing:
    def test_picks_full_suite_job_not_aggregation_gate(self, tmp_path):
        ci, _ = _write_pair(tmp_path, "pytest pyyaml", "pytest pyyaml")
        assert mod.ci_packages(ci) == {"pytest", "pyyaml"}

    def test_no_matching_job_is_caller_error(self, tmp_path):
        ci = tmp_path / "ci.yml"
        ci.write_text(
            "jobs:\n  a:\n    steps:\n      - run: echo hi\n", encoding="utf-8"
        )
        with pytest.raises(mod.ParityError, match="no job"):
            mod.ci_packages(ci)

    def test_matching_job_with_no_pip_is_caller_error(self, tmp_path):
        """Fail-closed: a parse that finds zero packages must NOT pass."""
        ci = tmp_path / "ci.yml"
        ci.write_text(
            "jobs:\n  a:\n    steps:\n"
            "      - run: pytest tests/ --cov --cov-report=xml\n",
            encoding="utf-8",
        )
        with pytest.raises(mod.ParityError, match="vacuously"):
            mod.ci_packages(ci)

    def test_malformed_yaml_is_caller_error(self, tmp_path):
        ci = tmp_path / "ci.yml"
        ci.write_text("jobs: [unclosed\n", encoding="utf-8")
        with pytest.raises(mod.ParityError, match="cannot parse"):
            mod.ci_packages(ci)


# ── 2. Verdicts ──────────────────────────────────────────────────────


class TestVerdicts:
    def test_parity_holds_exits_zero(self, tmp_path):
        ci, dev = _write_pair(tmp_path, "pyyaml pytest", "pyyaml pytest mkdocs-material")
        assert _run(ci, dev) == 0

    def test_missing_package_exits_one(self, tmp_path):
        ci, dev = _write_pair(tmp_path, "pyyaml croniter", "pyyaml")
        assert _run(ci, dev) == 1

    def test_name_normalization_prevents_false_drift(self, tmp_path):
        """`PyYAML` in CI and `pyyaml` in devcontainer are the SAME package."""
        ci, dev = _write_pair(tmp_path, "PyYAML pytest_cov", "pyyaml pytest-cov")
        assert _run(ci, dev) == 0

    def test_devcontainer_extras_are_allowed(self, tmp_path):
        ci, dev = _write_pair(tmp_path, "pytest", "pytest mkdocs-material extra-thing")
        assert _run(ci, dev) == 0

    def test_caller_error_exits_two(self, tmp_path):
        ci, _ = _write_pair(tmp_path, "pytest", "pytest")
        assert _run(ci, tmp_path / "missing.json") == 2

    def test_json_output_shape_on_drift(self, tmp_path, capsys):
        ci, dev = _write_pair(tmp_path, "pyyaml croniter", "pyyaml")
        rc = _run(ci, dev, "--json")
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["status"] == "drift"
        assert payload["missing"] == ["croniter"]

    def test_json_output_on_caller_error(self, tmp_path, capsys):
        ci, _ = _write_pair(tmp_path, "pytest", "pytest")
        rc = _run(ci, tmp_path / "missing.json", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert payload["status"] == "error"


# ── 3. Live repo + mutant ────────────────────────────────────────────


class TestLiveRepo:
    def test_real_repo_is_clean(self):
        rc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert rc.returncode == 0, (
            f"real repo has devcontainer dep drift:\n{rc.stdout}\n{rc.stderr}"
        )

    def test_mutant_real_devcontainer_turns_red(self, tmp_path):
        """Drop one CI package from the REAL devcontainer.json → must fail.

        Guards against the always-green failure mode: if the parse silently
        returned an empty CI set, `test_real_repo_is_clean` would still pass.
        """
        real = REPO_ROOT / ".devcontainer" / "devcontainer.json"
        text = real.read_text(encoding="utf-8")
        assert " croniter " in text, "fixture assumption broke: croniter not declared"
        mutant = tmp_path / "devcontainer.json"
        mutant.write_text(text.replace(" croniter ", " ", 1), encoding="utf-8")

        rc = mod.main([
            "--devcontainer", str(mutant),
            "--ci", str(REPO_ROOT / ".github" / "workflows" / "ci.yml"),
        ])
        assert rc == 1


class TestCliContract:
    def test_help_exits_zero(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert r.returncode == 0

    def test_unknown_flag_exits_two(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--nope"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert r.returncode == 2
