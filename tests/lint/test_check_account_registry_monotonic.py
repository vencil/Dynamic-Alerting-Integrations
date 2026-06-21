"""Tests for check_account_registry_monotonic.py — #609 / ADR-021 monotonic guard.

Pinned contracts
----------------
1. **decrease → fail** (the headline): lowering next_account_id across a commit
   (the git-revert / hand-edit leak class) exits 1 under --ci. Pre-commit mode
   (staged-vs-HEAD) AND CI mode (--base) both catch it.
2. **increase → pass** / **equal → pass**: a monotonic or no-change registry is
   clean.
3. **new-file → pass**: a registry absent at the baseline (Day-0) has nothing to
   regress against.
4. **corrupt → fail-closed**: a present-but-unparseable registry blocks the gate
   (exit 1) even WITHOUT --ci — a registry we cannot trust must never pass.
5. **not-tracked → pass**: the default in-repo registry path is (currently) not
   committed; the gate gracefully no-ops on the real repo tree.

The fixture is a real on-disk git repo (the lint shells out to
``git show :<path>`` / ``git show <rev>:<path>``; mocking git would not exercise
the staged-vs-committed blob resolution the gate depends on).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_account_registry_monotonic as mono  # noqa: E402

# The registry path the fixtures write/stage. Kept short; the module default
# (the real conf.d path) is exercised separately by the live-tree test.
REL = "conf.d/_account_registry.yaml"


def _registry(next_id: int, allocations: dict | None = None) -> str:
    """Render a minimal valid registry document at the given high-water mark."""
    lines = ["schema_version: v1", f"next_account_id: {next_id}", "allocations:"]
    if allocations:
        for tenant, aid in allocations.items():
            lines.append(f"  {tenant}: {aid}")
    else:
        lines[-1] = "allocations: {}"
    return "\n".join(lines) + "\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with helpers to seed/commit/stage the registry file.

    Yields an object exposing:
      - .path                      → repo Path
      - .write(text)               → write the registry working-copy
      - .stage()                   → git add the registry
      - .commit(msg)               → git commit -am
      - .seed_committed(text, msg) → write + add + commit in one step

    PROJECT_ROOT (and thus every `git -C` the module runs) is monkeypatched to
    this repo.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "conf.d").mkdir()

    def _git(*args, check=True):
        # subprocess-timeout: ignore — local git on a tiny fixture repo.
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=check
        )

    _git("init", "--quiet", "--initial-branch=main")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")

    reg_path = root / REL

    class _Repo:
        path = root

        def write(self, text: str) -> None:
            reg_path.write_text(text, encoding="utf-8")

        def stage(self) -> None:
            _git("add", REL)

        def commit(self, msg: str = "change") -> None:
            _git("commit", "--quiet", "-m", msg)

        def seed_committed(self, text: str, msg: str = "seed") -> None:
            self.write(text)
            self.stage()
            self.commit(msg)

        def git(self, *args):
            return _git(*args)

    # Hermetic env: the CI runner sets GITHUB_BASE_REF (= the PR's base branch)
    # for the real Lint job, but this fixture is a standalone temp repo with no
    # such remote ref. Clear both diff-base envs so a test drives --base
    # explicitly (or exercises the no-base staged-vs-HEAD default) rather than
    # inheriting CI's ambient base — which would resolve to a non-existent
    # `origin/main` HERE and make the lint exit 2 (caller error) instead of the
    # behaviour under test. (Host passes without this; CI sets the var.)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("LINT_DIFF_BASE", raising=False)
    monkeypatch.setattr(mono, "PROJECT_ROOT", root)
    return _Repo()


# ---------------------------------------------------------------------------
# evaluate() — core decision table (pre-commit mode: staged vs HEAD)
# ---------------------------------------------------------------------------
class TestPreCommitMode:
    def test_decrease_flagged(self, repo):
        """Headline: a revert/edit lowering the counter → status 'decrease'."""
        repo.seed_committed(_registry(1003, {"a": 1000, "b": 1001, "c": 1002}))
        # Simulate `git revert` of c's onboarding: counter back to 1002, c gone.
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        res = mono.evaluate(REL, base=None)
        assert res["status"] == "decrease"
        assert res["previous"] == 1003 and res["current"] == 1002

    def test_increase_passes(self, repo):
        repo.seed_committed(_registry(1001, {"a": 1000}))
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        assert mono.evaluate(REL, base=None)["status"] == "ok"

    def test_equal_passes(self, repo):
        """A commit that touches the file but not the counter is fine."""
        repo.seed_committed(_registry(1002, {"a": 1000, "b": 1001}))
        # Re-stage identical counter (e.g. a comment/allocation-comment churn).
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        assert mono.evaluate(REL, base=None)["status"] == "ok"

    def test_new_file_passes(self, repo):
        """No baseline blob (file is new in this change) → Day-0, pass."""
        repo.write(_registry(1000))
        repo.stage()  # staged but never committed → no HEAD:<path>
        assert mono.evaluate(REL, base=None)["status"] == "absent"


# ---------------------------------------------------------------------------
# evaluate() — CI / PR mode (base vs HEAD)
# ---------------------------------------------------------------------------
class TestCIMode:
    def test_decrease_across_pr_flagged(self, repo):
        """A revert committed on the branch lowers HEAD's counter vs the base."""
        repo.seed_committed(_registry(1003, {"a": 1000, "b": 1001, "c": 1002}), "base")
        repo.git("tag", "base-ref")
        # Commit the revert directly (CI compares committed HEAD vs base).
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        repo.commit("revert c onboarding")
        res = mono.evaluate(REL, base="base-ref")
        assert res["status"] == "decrease"
        assert res["previous"] == 1003 and res["current"] == 1002

    def test_increase_across_pr_passes(self, repo):
        repo.seed_committed(_registry(1001, {"a": 1000}), "base")
        repo.git("tag", "base-ref")
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        repo.commit("onboard b")
        assert mono.evaluate(REL, base="base-ref")["status"] == "ok"


# ---------------------------------------------------------------------------
# parse_next_account_id() — fail-closed corruption handling
# ---------------------------------------------------------------------------
class TestParse:
    def test_blank_primes_at_floor(self):
        # Blank/whitespace == brand-new file → floor (1000), not an error.
        assert mono.parse_next_account_id("") == 1000
        assert mono.parse_next_account_id("   \n\t ") == 1000

    def test_valid_counter(self):
        assert mono.parse_next_account_id(_registry(1042)) == 1042

    def test_malformed_yaml_raises(self):
        with pytest.raises(mono.RegistryParseError):
            mono.parse_next_account_id("next_account_id: [unterminated\n")

    def test_missing_counter_raises(self):
        with pytest.raises(mono.RegistryParseError):
            mono.parse_next_account_id("schema_version: v1\nallocations: {}\n")

    def test_non_integer_counter_raises(self):
        with pytest.raises(mono.RegistryParseError):
            mono.parse_next_account_id("next_account_id: not-a-number\n")

    def test_bool_counter_rejected(self):
        # bool is an int subclass — `true` must NOT silently read as 1.
        with pytest.raises(mono.RegistryParseError):
            mono.parse_next_account_id("next_account_id: true\n")

    def test_non_mapping_root_raises(self):
        with pytest.raises(mono.RegistryParseError):
            mono.parse_next_account_id("- just\n- a\n- list\n")


# ---------------------------------------------------------------------------
# main() — exit codes + fail-closed end-to-end
# ---------------------------------------------------------------------------
class TestMainExitCodes:
    def test_decrease_ci_exits_1(self, repo, cli_argv, capsys):
        repo.seed_committed(_registry(1003, {"a": 1000, "b": 1001, "c": 1002}))
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        cli_argv("check_account_registry_monotonic.py", "--ci", "--registry-path", REL)
        assert mono.main() == mono.EXIT_VIOLATION
        assert "decreased" in capsys.readouterr().err

    def test_decrease_without_ci_exits_0(self, repo, cli_argv):
        """Report-only mode does not fail the build on a decrease (only --ci)."""
        repo.seed_committed(_registry(1003, {"a": 1000, "b": 1001, "c": 1002}))
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        cli_argv("check_account_registry_monotonic.py", "--registry-path", REL)
        assert mono.main() == mono.EXIT_OK

    def test_increase_ci_exits_0(self, repo, cli_argv):
        repo.seed_committed(_registry(1001, {"a": 1000}))
        repo.write(_registry(1002, {"a": 1000, "b": 1001}))
        repo.stage()
        cli_argv("check_account_registry_monotonic.py", "--ci", "--registry-path", REL)
        assert mono.main() == mono.EXIT_OK

    def test_corrupt_fail_closed_exits_1_even_without_ci(self, repo, cli_argv, capsys):
        """A present-but-unparseable registry fail-closes regardless of --ci."""
        repo.seed_committed(_registry(1001, {"a": 1000}))
        # Stage a malformed counter (e.g. a botched hand-edit).
        repo.write("schema_version: v1\nnext_account_id: oops\nallocations: {}\n")
        repo.stage()
        cli_argv("check_account_registry_monotonic.py", "--registry-path", REL)
        assert mono.main() == mono.EXIT_VIOLATION
        assert "cannot validate" in capsys.readouterr().err

    def test_new_file_exits_0(self, repo, cli_argv):
        repo.write(_registry(1000))
        repo.stage()
        cli_argv("check_account_registry_monotonic.py", "--ci", "--registry-path", REL)
        assert mono.main() == mono.EXIT_OK

    def test_unresolvable_base_is_caller_error(self, repo, cli_argv, capsys):
        repo.seed_committed(_registry(1001, {"a": 1000}))
        cli_argv(
            "check_account_registry_monotonic.py",
            "--ci",
            "--registry-path",
            REL,
            "--base",
            "origin/does-not-exist",
        )
        assert mono.main() == mono.EXIT_CALLER_ERROR
        assert "does not resolve" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# live repo tree — default path is not tracked → graceful no-op
# ---------------------------------------------------------------------------
def test_live_tree_default_path_no_ops():
    """On the real repo, the default registry path is not committed (runtime
    state), so the gate must gracefully PASS rather than error."""
    res = mono.evaluate(mono.DEFAULT_REGISTRY_REL, base=None)
    assert res["status"] == "absent"
