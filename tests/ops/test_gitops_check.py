#!/usr/bin/env python3
"""test_gitops_check.py — gitops_check.py pytest comprehensive test suite.

Validates:
  1. check_repo() — Git repository accessibility via git ls-remote
     - Successful repo check with branch verification
     - Branch not found → fail
     - Repository access failure → fail
     - Timeout handling
     - Command not found (git missing)

  2. check_local() — Local configuration structure validation
     - Directory exists with _defaults.yaml and valid YAML files
     - Missing directory → fail
     - Missing _defaults.yaml → fail
     - Invalid YAML in _defaults.yaml → fail
     - Invalid YAML in tenant files → fail with parse error details
     - Empty directory (only _defaults.yaml) → pass
     - Multiple tenant files with rule counting
     - Directory scan with OSError → fail

  3. check_sidecar() — K8s git-sync sidecar deployment
     - kubectl available: full check with secret and sidecar
     - kubectl not available → warn
     - Secret present + sidecar present → pass
     - Secret present, sidecar missing → warn
     - Secret missing, sidecar present → warn
     - Both missing → warn

  4. Main entry points
     - repo subcommand with JSON output
     - local subcommand with JSON output
     - sidecar subcommand with JSON output
     - --ci mode: warn → exit 0, fail → exit 1
     - normal mode: warn/fail → exit 1
     - No action specified → exit 1

  5. BilingualText & Output
     - Human-readable output with symbols (✓, ⚠, ✗)
     - JSON format with format_json_report
     - Language detection (_h() selector)

  6. Edge cases
     - Empty YAML file
     - YAML with syntax error
     - Large tenant configurations
     - Multiple parse errors
"""

import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from datetime import datetime

import pytest
import yaml

import gitops_check as gc
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


def run_main(argv):
    """Run gc.main() under the given sys.argv and return the pytest
    ExceptionInfo for the expected SystemExit. Callers assert on
    exc_info.value.code and read capsys themselves, so assertion order
    (code-first vs output-first) stays explicit at the call site.
    """
    with patch("sys.argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            gc.main()
    return exc_info


# ── Fixtures ──────────────────────────────────────────────────────────────

# config_dir (a bare temp dir) is provided by the shared tests/conftest.py
# fixture of the same name; only the seeded valid_config_dir is file-local.
@pytest.fixture
def valid_config_dir():
    """Temporary directory with valid _defaults.yaml and tenant files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create _defaults.yaml
        defaults = {
            "global_threshold": 100,
            "alert_enabled": True,
        }
        with open(os.path.join(tmpdir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create tenant files (Dynamic Alerting flat key-value format)
        db_a = {
            "mysql_connections": "80",
            "mysql_threads_running": "75",
            "container_memory": "85",
            "_routing": {"receiver_type": "slack"},
        }
        db_b = {
            "redis_memory_usage": "80",
            "redis_connected_clients": "500",
            "_metadata": {"owner": "sre-team"},
        }

        with open(os.path.join(tmpdir, "db-a.yaml"), "w") as f:
            yaml.dump(db_a, f)
        with open(os.path.join(tmpdir, "db-b.yaml"), "w") as f:
            yaml.dump(db_b, f)

        yield tmpdir


# ── 1. _run_cmd Tests ──────────────────────────────────────────────────────

class TestRunCmd:
    """Test _run_cmd() subprocess execution."""

    def test_successful_command(self):
        """Successful command execution."""
        success, stdout, stderr = gc._run_cmd(["echo", "hello"])
        assert success is True
        assert "hello" in stdout
        assert stderr == ""

    def test_failed_command(self):
        """Failed command (non-zero exit)."""
        success, stdout, stderr = gc._run_cmd(["sh", "-c", "exit 42"])
        assert success is False

    def test_command_not_found(self):
        """Command not found → FileNotFoundError."""
        success, stdout, stderr = gc._run_cmd(["nonexistent_binary_xyz"])
        assert success is False
        assert "not found" in stderr or "Command not found" in stderr

    def test_timeout(self):
        """Command timeout."""
        success, stdout, stderr = gc._run_cmd(
            ["sleep", "100"],
            timeout=1
        )
        assert success is False
        assert "timeout" in stderr or "TimeoutExpired" in str(stderr)

    def test_stderr_capture(self):
        """Capture stderr from failed command."""
        success, stdout, stderr = gc._run_cmd(
            ["sh", "-c", "echo 'error message' >&2; exit 1"]
        )
        assert success is False
        assert "error message" in stderr


# ── 2. check_repo() Tests ──────────────────────────────────────────────────

class TestCheckRepo:
    """Test check_repo() Git repository validation."""

    @patch("gitops_check._run_cmd")
    def test_repo_success(self, mock_run_cmd):
        """Successful repo check with branch found."""
        mock_run_cmd.side_effect = [
            (True, "abc123\trefs/heads/main\nxyz789\trefs/heads/develop", ""),  # ls-remote
            (True, "", ""),  # git archive (path verified)
        ]

        result = gc.check_repo("https://github.com/test/repo.git", "main")

        assert result.check == "repo"
        assert result.status == "pass"
        assert "accessible" in result.message.lower()
        assert result.details["url"] == "https://github.com/test/repo.git"
        assert result.details["branch"] == "main"
        assert result.details["branch_found"] is True
        assert result.details["branch_count"] == 2
        assert result.details["config_path_verified"] is True

    @patch("gitops_check._run_cmd")
    def test_repo_branch_not_found(self, mock_run_cmd):
        """Branch doesn't exist in repo."""
        mock_run_cmd.return_value = (True, "abc123\trefs/heads/main\nxyz789\trefs/heads/develop", "")

        result = gc.check_repo("https://github.com/test/repo.git", "nonexistent")

        assert result.check == "repo"
        assert result.status == "fail"
        assert "not found" in result.message.lower()
        assert result.details["url"] == "https://github.com/test/repo.git"

    @patch("gitops_check._run_cmd")
    def test_repo_access_failure(self, mock_run_cmd):
        """Repository not accessible."""
        mock_run_cmd.return_value = (False, "", "Connection refused")

        result = gc.check_repo("https://github.com/test/repo.git", "main")

        assert result.check == "repo"
        assert result.status == "fail"
        assert "cannot access" in result.message.lower()
        assert "Connection refused" in result.message

    @patch("gitops_check._run_cmd")
    def test_repo_with_custom_branch_and_path(self, mock_run_cmd):
        """Custom branch and path parameters."""
        mock_run_cmd.side_effect = [
            (True, "abc123\trefs/heads/develop\n", ""),  # ls-remote
            (False, "", "not supported"),  # git archive fails (best-effort)
        ]

        result = gc.check_repo(
            "git@github.com:test/repo.git",
            branch="develop",
            path="config/"
        )

        assert result.status == "pass"
        assert result.details["branch"] == "develop"
        assert result.details["config_path"] == "config/"
        assert result.details["config_path_verified"] is False

    @patch("gitops_check._run_cmd")
    def test_repo_git_command_failure(self, mock_run_cmd):
        """git ls-remote command not available."""
        mock_run_cmd.return_value = (False, "", "git: command not found")

        result = gc.check_repo("https://github.com/test/repo.git")

        assert result.status == "fail"
        assert result.details["url"] == "https://github.com/test/repo.git"

    @patch("gitops_check._run_cmd")
    def test_repo_empty_output(self, mock_run_cmd):
        """Empty output from git ls-remote."""
        mock_run_cmd.return_value = (True, "", "")  # single return ok: fails at branch check before archive call

        result = gc.check_repo("https://github.com/test/repo.git", "main")

        assert result.status == "fail"
        assert "not found" in result.message.lower()

    @patch("gitops_check._run_cmd")
    def test_repo_path_verified(self, mock_run_cmd):
        """Path verification succeeds via git archive."""
        mock_run_cmd.side_effect = [
            (True, "abc123\trefs/heads/main\n", ""),  # ls-remote
            (True, "", ""),  # git archive succeeds
        ]
        result = gc.check_repo("https://github.com/test/repo.git", "main", "conf.d")
        assert result.details["config_path_verified"] is True
        assert "verified" in result.message.lower()

    @patch("gitops_check._run_cmd")
    def test_repo_path_not_verifiable(self, mock_run_cmd):
        """Path verification fails gracefully (server doesn't support git archive)."""
        mock_run_cmd.side_effect = [
            (True, "abc123\trefs/heads/main\n", ""),  # ls-remote
            (False, "", "remote does not support archive"),  # git archive fails
        ]
        result = gc.check_repo("https://github.com/test/repo.git", "main", "conf.d")
        assert result.status == "pass"  # not a hard failure
        assert result.details["config_path_verified"] is False


# ── 3. check_local() Tests ──────────────────────────────────────────────────

class TestCheckLocal:
    """Test check_local() local configuration validation."""

    def test_local_success(self, valid_config_dir):
        """Valid local configuration directory."""
        result = gc.check_local(valid_config_dir)

        assert result.check == "local"
        assert result.status == "pass"
        assert "valid" in result.message.lower()
        assert result.details["directory"] == valid_config_dir
        assert result.details["defaults_file"] == "present"
        assert result.details["tenant_files"] == 2
        assert result.details["total_metrics"] == 5  # 3 from db-a + 2 from db-b

    def test_local_directory_not_found(self):
        """Directory does not exist."""
        result = gc.check_local("/nonexistent/path/xyz")

        assert result.check == "local"
        assert result.status == "fail"
        assert "not found" in result.message.lower()
        assert result.details["directory"] == "/nonexistent/path/xyz"

    def test_local_missing_defaults_yaml(self, config_dir):
        """Missing _defaults.yaml (required)."""
        result = gc.check_local(config_dir)

        assert result.check == "local"
        assert result.status == "fail"
        assert "missing" in result.message.lower()
        assert "_defaults.yaml" in result.message

    def test_local_invalid_defaults_yaml(self, config_dir):
        """Invalid YAML in _defaults.yaml."""
        bad_yaml = "key: value\n  bad indentation:\n not aligned:"
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            f.write(bad_yaml)

        result = gc.check_local(config_dir)

        assert result.check == "local"
        assert result.status == "fail"
        assert "invalid" in result.message.lower()

    def test_local_invalid_tenant_yaml(self, config_dir):
        """Invalid YAML in tenant file."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create bad tenant file
        with open(os.path.join(config_dir, "db-a.yaml"), "w") as f:
            f.write("invalid: yaml\n  bad indent:")

        result = gc.check_local(config_dir)

        assert result.check == "local"
        assert result.status == "fail"
        assert "invalid" in result.message.lower()
        assert "parse_errors" in result.details
        assert len(result.details["parse_errors"]) == 1
        assert result.details["parse_errors"][0]["file"] == "db-a.yaml"

    def test_local_multiple_parse_errors(self, config_dir):
        """Multiple tenant files with YAML errors."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create multiple bad tenant files with actual syntax errors
        for fname in ["db-a.yaml", "db-b.yaml", "db-c.yaml"]:
            with open(os.path.join(config_dir, fname), "w") as f:
                # Use invalid YAML: unclosed quotes
                f.write("key: 'unclosed quote\nanother: value")

        result = gc.check_local(config_dir)

        assert result.status == "fail"
        assert "3 yaml files" in result.message.lower()
        assert len(result.details["parse_errors"]) == 3

    def test_local_empty_tenant_files(self, config_dir):
        """Only _defaults.yaml, no tenant files."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        result = gc.check_local(config_dir)

        assert result.status == "pass"
        assert result.details["tenant_files"] == 0
        assert result.details["total_metrics"] == 0

    def test_local_ignores_reserved_files(self, config_dir):
        """`_`-prefixed control files are not tenant carriers.

        ⛔ Renamed from `test_local_ignores_hidden_files`, which called a
        `_`-prefixed file "hidden". `_lib_confd` makes `is_reserved_name`
        (`_` prefix) and `is_hidden_name` (`.` prefix) two DELIBERATELY
        orthogonal predicates, and this reader treats them differently: it
        drops reserved names and COUNTS dot-prefixed ones. Under the old
        name, anyone grepping for coverage of the hidden axis found this
        green test and stopped — see the SCOPE block further down, which
        records that the hidden axis has no assertion at all.
        """
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create reserved-prefix file (should be ignored)
        with open(os.path.join(config_dir, "_hidden.yaml"), "w") as f:
            yaml.dump({"hidden": True}, f)

        result = gc.check_local(config_dir)

        assert result.status == "pass"
        assert result.details["tenant_files"] == 0

    def test_local_ignores_non_yaml_files(self, config_dir):
        """Non-.yaml files are ignored."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create non-yaml file
        with open(os.path.join(config_dir, "README.md"), "w") as f:
            f.write("# Config")

        result = gc.check_local(config_dir)

        assert result.status == "pass"
        assert result.details["tenant_files"] == 0

    def test_local_counts_metric_keys(self, valid_config_dir):
        """Count metric keys (excluding _ prefix internal keys)."""
        result = gc.check_local(valid_config_dir)

        assert result.details["tenant_files"] == 2
        assert result.details["total_metrics"] == 5
        # db-a: mysql_connections, mysql_threads_running, container_memory (3)
        # db-b: redis_memory_usage, redis_connected_clients (2)

    def test_local_tenant_file_only_internal_keys(self, config_dir):
        """Tenant file with only internal (_-prefixed) keys has 0 metrics."""
        defaults = {"mysql_connections": 80}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create tenant file with only internal keys
        with open(os.path.join(config_dir, "db-a.yaml"), "w") as f:
            yaml.dump({"_routing": {"receiver_type": "slack"}, "_metadata": {"owner": "team"}}, f)

        result = gc.check_local(config_dir)

        assert result.status == "pass"
        assert result.details["tenant_files"] == 1
        assert result.details["total_metrics"] == 0


# ── 3b. The extension-SPELLING axis (#1603) ────────────────────────────────
#
# `check_local` is what `da-tools gitops-check local` runs, i.e. what an
# operator points at their GitOps repo to answer "is this ready?". Its
# defaults resolver takes `_defaults.yaml` OR `_defaults.yml` while its
# tenant scan used to take `.yaml` only, so one invocation answered the
# same question two ways: it found the defaults carrier and then reported
# `pass` with `tenant_files: 0`, rc=0, stderr empty, on a tree the
# exporter is serving tenants out of.
#
# The oracle is the exporter's scanner: `config_hierarchy.go:195` lowercases
# the entry name and accepts BOTH `.yaml` and `.yml`.
#
# ⚠️ SCOPE. These pin the extension-SPELLING axis only. The other three axes
# are NOT covered, and none of them has an open ticket standing behind it, so
# the disclosure has to carry itself:
#   * Recursion: `check_local` is flat by construction (`base.iterdir()`).
#     That is `test_confd_enumeration_contract.py`'s axis; nested carriers do
#     get a `WARN` on stderr, so this one at least speaks.
#   * Hidden names: this reader COUNTS `.hidden.yaml` as a tenant file while
#     the exporter skips dot-prefixed entries (`config_hierarchy.go:181,190`).
#     Measured on the tree before this change and unchanged by it, so it is
#     pre-existing — but it is also the same shape as the bug fixed here: the
#     shared predicates say `is_hidden_name(".secret.yaml") is True` and
#     `config_stem(".secret.yaml") == ""` (this file carries no tenant id),
#     while the loop two lines below counts it as tenant #2. This module
#     imports three of the four name predicates and not `is_hidden_name`.
#     ⛔ Closing it DELETES tenants that count today, so it is a separate
#     behaviour change; the fixtures below therefore contain no dot-prefixed
#     name at all, rather than pinning today's answer for them.
#     ⚠️ #1339 (the family ticket) is closed, so nothing is tracking this.
#   * Entries `is_file()` drops (a directory named `notes.yml/`, a broken
#     symlink) are still silently skipped rather than named. ⚠️ #1607 is
#     closed (state_reason=COMPLETED) — its closing comment verifies `operator_generate`
#     and `custom_alerts/loader` on main and does not mention this tool, and
#     measured here it still drops both shapes with rc=0 and an empty stderr.
#     This reader is a residual of a ticket that reads as finished.

class TestCheckLocalExtensionSpelling:
    """`.yml` carriers must count exactly as `.yaml` ones do — no more."""

    @staticmethod
    def _seed(root, ext):
        """One conf.d whose every carrier uses `ext`. Bodies never change."""
        Path(root, f"_defaults{ext}").write_text(
            "global_threshold: 100\n", encoding="utf-8")
        Path(root, f"db-a{ext}").write_text(
            "mysql_connections: '80'\n"
            "mysql_threads_running: '75'\n"
            "_routing:\n  receiver_type: slack\n", encoding="utf-8")
        Path(root, f"db-b{ext}").write_text(
            "redis_memory_usage: '80'\n", encoding="utf-8")

    def test_local_agrees_across_extension_spellings(self, tmp_path):
        """FLOOR. Two trees, same bytes, different extension → same answer.

        ⛔ Asserted as an EQUALITY between the two runs rather than as
        "`.yml` is accepted". The second is satisfied by a reader that takes
        `.yml` and drops `.yaml`, and it stops meaning anything the day the
        exporter grows a third spelling; the equality keeps saying the right
        thing in both cases.
        """
        lower, alt = tmp_path / "yaml", tmp_path / "yml"
        lower.mkdir()
        alt.mkdir()
        self._seed(lower, ".yaml")
        self._seed(alt, ".yml")

        a = gc.check_local(str(lower))
        b = gc.check_local(str(alt))

        def comparable(result):
            # `details["directory"]` echoes the input path, which differs
            # between the two trees by construction.
            details = {k: v for k, v in (result.details or {}).items()
                       if k != "directory"}
            return result.status, result.message, details

        # ⛔ Vacuity guard FIRST: two trees it cannot read at all would also
        # compare equal.
        #
        # ⛔ And it is asserted on BOTH sides, not just the control. Blind
        # review measured that gutting `comparable()` down to `(status,)` and
        # narrowing the scan back to `.yaml` leaves this test GREEN: all of
        # its discriminating power lived inside that one helper, which is
        # also the cheapest way to make a legitimate-but-inconvenient failure
        # go away. Pinning the two counts directly means the equality can be
        # weakened without silently disarming the test.
        for label, r in (("`.yaml`", a), ("`.yml`", b)):
            assert (r.details.get("tenant_files"),
                    r.details.get("total_metrics")) == (2, 3), (
                f"the {label} tree reported "
                f"tenant_files={r.details.get('tenant_files')} "
                f"total_metrics={r.details.get('total_metrics')}, not (2, 3) — "
                f"the equality below would prove nothing"
            )
        assert comparable(a) == comparable(b), (
            f"gitops-check reports a different readiness for a conf.d whose "
            f"only difference is `.yaml` vs `.yml`, both of which the "
            f"exporter serves:\n  .yaml: {comparable(a)}\n  .yml : "
            f"{comparable(b)}"
        )

    def test_local_names_an_unparseable_yml_carrier(self, tmp_path):
        """FLOOR, second observable: a broken `.yml` is NAMED, not skipped.

        The count-equality above goes red if a `.yml` tenant disappears; this
        goes red if one is dropped from the FAILURE path instead, which is the
        same silence wearing a green exit code.
        """
        for ext in (".yaml", ".yml"):
            root = tmp_path / ext.lstrip(".")
            root.mkdir()
            Path(root, "_defaults.yaml").write_text(
                "global_threshold: 100\n", encoding="utf-8")
            Path(root, f"db-b{ext}").write_text(
                "mysql_connections: [unclosed\n", encoding="utf-8")

            result = gc.check_local(str(root))

            assert result.status == "fail", (
                f"a conf.d holding an unparseable `db-b{ext}` was reported "
                f"{result.status!r}"
            )
            assert [e["file"] for e in result.details["parse_errors"]] == [
                f"db-b{ext}"], (
                f"the report must name the carrier it could not parse; got "
                f"{result.details.get('parse_errors')!r}"
            )

    def test_local_does_not_count_a_json_or_reserved_carrier(self, tmp_path):
        """CEILING, by counterexample — that is all a ceiling can be.

        ⛔ The name says `.json` and reserved rather than "a carrier the
        exporter does not serve", because you cannot enumerate the complement
        of an accept-set: this pins TWO counterexamples, not a rule. In
        particular it does NOT cover dot-prefixed names, which this reader
        counts and the exporter skips (see the SCOPE block above) — a name
        promising the general claim would have been read as covering that.

        ⛔ Blind review of the sibling fix (#1663) measured that over-widening
        a call site to `(".yaml", ".yml", ".json")` — a single-token edit, and
        `has_yaml_extension`'s own docstring says this argument gets touched —
        left the whole suite green while the tool began reading carriers the
        exporter never loads. Re-measured for THIS tool on the base this
        change sits on (`daf747fb`): with the widening applied to
        `check_local` and this file's four new tests absent, the CI-exact
        suite is 15905 passed / 185 skipped / rc=0 — nothing anywhere was
        watching that direction.

        The two counts are a FINGERPRINT, not two loose bounds: each file
        contributes a distinct number of metric keys (1 / 2 / 4 / 8), so
        `total_metrics` identifies exactly WHICH files were counted. Measured,
        each with the edit that produces it:

            accept `.json` too                    -> 3 / 7
            narrow back to `.yaml` only           -> 1 / 1
            drop the `is_reserved_name(...)` term -> 4 / 12   (`_defaults.yaml`
                                                    is `_`-prefixed too, so it
                                                    joins as well)
            keep it but narrow it to `_defaults`  -> 3 / 11

        Every one of them fails this assertion.
        """
        root = tmp_path / "confd"
        root.mkdir()
        Path(root, "_defaults.yaml").write_text(
            "global_threshold: 100\n", encoding="utf-8")
        # 1 key — plain tenant carrier.
        Path(root, "alpha.yaml").write_text("k1: '1'\n", encoding="utf-8")
        # 2 keys — the spelling this change widened to.
        Path(root, "beta.yml").write_text(
            "k1: '1'\nk2: '2'\n", encoding="utf-8")
        # 4 keys — parses fine as YAML (JSON is a YAML subset), and that is
        # the point: nothing but the extension rule keeps it out.
        Path(root, "gamma.json").write_text(
            '{"k1": "1", "k2": "2", "k3": "3", "k4": "4"}\n', encoding="utf-8")
        # 8 keys — `_`-prefixed control file, never a tenant carrier.
        Path(root, "_profiles.yaml").write_text(
            "".join(f"k{i}: '{i}'\n" for i in range(8)), encoding="utf-8")

        result = gc.check_local(str(root))

        assert result.status == "pass"
        assert (result.details["tenant_files"], result.details["total_metrics"]) \
            == (2, 3), (
            f"gitops-check counted a different set of carriers than the two "
            f"the exporter serves here (alpha.yaml + beta.yml); got "
            f"tenant_files={result.details['tenant_files']} "
            f"total_metrics={result.details['total_metrics']}"
        )

    def test_local_counts_every_spelling_the_shared_set_names(self, tmp_path):
        """FLOOR, derived: one carrier per member of `CONFIG_SUFFIXES`.

        The two tests above hard-code `.yaml` and `.yml`, so they stop
        covering the floor the day the exporter grows a third spelling and
        `_lib_confd.CONFIG_SUFFIXES` follows it. This one reads the set
        instead of restating it, so it widens on its own.

        ⛔ It does NOT re-implement `has_yaml_extension` — that would be the
        predicate checking itself. It uses the shared CONSTANT, whose
        agreement with the exporter's scanner is pinned elsewhere
        (`tests/shared/confd_name_classification_matrix.json`, asserted from
        the Go side by `confd_name_classification_parity_test.go` and from
        the Python side by `tests/shared/test_confd_name_classification_parity.py`).

        ⚠️ Floor only: satisfied by a reader that is too WIDE, which is what
        the counterexample test above is for.
        """
        from _lib_confd import CONFIG_SUFFIXES  # noqa: PLC0415

        # ⛔ Anti-vacuity: an empty `CONFIG_SUFFIXES` would make the assertion
        # below `0 == 0`. The set is not this test's to guard, so say who
        # does rather than pretending a floor here would be independent.
        assert len(CONFIG_SUFFIXES) >= 2, (
            f"CONFIG_SUFFIXES collapsed to {CONFIG_SUFFIXES!r}; the shared "
            f"classification matrix is the thing that should have gone red "
            f"first, so fix that before touching this line"
        )

        root = tmp_path / "confd"
        root.mkdir()
        Path(root, "_defaults.yaml").write_text(
            "global_threshold: 100\n", encoding="utf-8")
        for i, suffix in enumerate(CONFIG_SUFFIXES):
            Path(root, f"t{i}{suffix}").write_text(
                "".join(f"k{j}: '{j}'\n" for j in range(i + 1)),
                encoding="utf-8")

        result = gc.check_local(str(root))
        expected_metrics = sum(range(1, len(CONFIG_SUFFIXES) + 1))

        assert result.status == "pass"
        assert (result.details["tenant_files"],
                result.details["total_metrics"]) == (
                    len(CONFIG_SUFFIXES), expected_metrics), (
            f"one carrier was written per member of CONFIG_SUFFIXES "
            f"({CONFIG_SUFFIXES!r}) and gitops-check counted "
            f"tenant_files={result.details['tenant_files']} "
            f"total_metrics={result.details['total_metrics']}, expected "
            f"{(len(CONFIG_SUFFIXES), expected_metrics)}"
        )


# ── 4. check_sidecar() Tests ───────────────────────────────────────────────

class TestCheckSidecar:
    """Test check_sidecar() K8s deployment validation."""

    @patch("gitops_check._run_cmd")
    def test_sidecar_kubectl_not_available(self, mock_run_cmd):
        """kubectl not available → warning."""
        mock_run_cmd.return_value = (False, "", "kubectl: command not found")

        result = gc.check_sidecar()

        assert result.check == "sidecar"
        assert result.status == "warn"
        assert "kubectl not available" in result.message.lower()
        assert result.details["namespace"] == "monitoring"

    @patch("gitops_check._run_cmd")
    def test_sidecar_full_pass(self, mock_run_cmd):
        """Secret and sidecar both present → pass."""
        def run_cmd_side_effect(cmd, timeout=10):
            if cmd[0:2] == ["kubectl", "version"]:
                return (True, "Client Version: v1.20.0", "")
            elif "git-sync-credentials" in cmd:
                return (True, "git-sync-credentials   Opaque   1   2h", "")
            elif "threshold-exporter" in cmd:
                return (True, "threshold-exporter git-sync", "")
            return (False, "", "unknown")

        mock_run_cmd.side_effect = run_cmd_side_effect

        result = gc.check_sidecar()

        assert result.status == "pass"
        assert "sidecar deployed" in result.message.lower()
        assert result.details["git_sync_secret"] == "present"
        assert result.details["sidecar_present"] is True

    @patch("gitops_check._run_cmd")
    def test_sidecar_secret_present_no_sidecar(self, mock_run_cmd):
        """Secret present but sidecar missing → warn."""
        def run_cmd_side_effect(cmd, timeout=10):
            if cmd[0:2] == ["kubectl", "version"]:
                return (True, "Client Version: v1.20.0", "")
            elif "git-sync-credentials" in cmd:
                return (True, "git-sync-credentials   Opaque   1   2h", "")
            elif "threshold-exporter" in cmd:
                return (True, "threshold-exporter", "")
            return (False, "", "unknown")

        mock_run_cmd.side_effect = run_cmd_side_effect

        result = gc.check_sidecar()

        assert result.status == "warn"
        assert "partial" in result.message.lower()
        assert result.details["git_sync_secret"] == "present"
        assert result.details["sidecar_present"] is False

    @patch("gitops_check._run_cmd")
    def test_sidecar_no_secret_with_sidecar(self, mock_run_cmd):
        """Sidecar present but secret missing → warn."""
        def run_cmd_side_effect(cmd, timeout=10):
            if cmd[0:2] == ["kubectl", "version"]:
                return (True, "Client Version: v1.20.0", "")
            elif "git-sync-credentials" in cmd:
                return (False, "", "secret not found")
            elif "threshold-exporter" in cmd:
                return (True, "threshold-exporter git-sync", "")
            return (False, "", "unknown")

        mock_run_cmd.side_effect = run_cmd_side_effect

        result = gc.check_sidecar()

        assert result.status == "warn"
        assert "partial" in result.message.lower()
        assert result.details["git_sync_secret"] == "missing"
        assert result.details["sidecar_present"] is True

    @patch("gitops_check._run_cmd")
    def test_sidecar_both_missing(self, mock_run_cmd):
        """Both secret and sidecar missing → warn."""
        def run_cmd_side_effect(cmd, timeout=10):
            if cmd[0:2] == ["kubectl", "version"]:
                return (True, "Client Version: v1.20.0", "")
            elif "git-sync-credentials" in cmd:
                return (False, "", "secret not found")
            elif "threshold-exporter" in cmd:
                return (False, "", "deployment not found")
            return (False, "", "unknown")

        mock_run_cmd.side_effect = run_cmd_side_effect

        result = gc.check_sidecar()

        assert result.status == "warn"
        assert "not deployed" in result.message.lower()
        assert result.details["git_sync_secret"] == "missing"

    @patch("gitops_check._run_cmd")
    def test_sidecar_custom_namespace(self, mock_run_cmd):
        """Custom namespace parameter."""
        def run_cmd_side_effect(cmd, timeout=10):
            if cmd[0:2] == ["kubectl", "version"]:
                return (True, "Client Version: v1.20.0", "")
            elif "custom-ns" in cmd:
                # Verify namespace is passed correctly
                assert "custom-ns" in cmd
                return (True, "", "")
            return (False, "", "")

        mock_run_cmd.side_effect = run_cmd_side_effect

        result = gc.check_sidecar(namespace="custom-ns")

        assert result.details["namespace"] == "custom-ns"


# ── 5. CheckResult & GitOpsReport Tests ────────────────────────────────────

class TestDataModels:
    """Test CheckResult and GitOpsReport data structures."""

    def test_check_result_creation(self):
        """CheckResult dataclass initialization."""
        result = gc.CheckResult(
            check="repo",
            status="pass",
            message="Test message",
            details={"url": "https://github.com/test/repo.git"}
        )

        assert result.check == "repo"
        assert result.status == "pass"
        assert result.message == "Test message"
        assert result.details["url"] == "https://github.com/test/repo.git"

    def test_gitops_report_to_dict(self):
        """GitOpsReport.to_dict() serialization."""
        check = gc.CheckResult(
            check="repo",
            status="pass",
            message="OK",
            details={"url": "test"}
        )
        report = gc.GitOpsReport(
            overall_status="pass",
            checks=[check],
            timestamp="2024-01-01T00:00:00Z"
        )

        data = report.to_dict()

        assert data["overall_status"] == "pass"
        assert len(data["checks"]) == 1
        assert data["checks"][0]["check"] == "repo"
        assert data["checks"][0]["details"]["url"] == "test"
        assert data["timestamp"] == "2024-01-01T00:00:00Z"

    def test_gitops_report_empty_details(self):
        """CheckResult with None details → empty dict in report."""
        check = gc.CheckResult(
            check="local",
            status="fail",
            message="No details",
            details=None
        )
        report = gc.GitOpsReport(
            overall_status="fail",
            checks=[check],
            timestamp="2024-01-01T00:00:00Z"
        )

        data = report.to_dict()
        assert data["checks"][0]["details"] == {}


# ── 6. Bilingual text (_h) Tests ───────────────────────────────────────────

class TestBilingualText:
    """Test _h() bilingual text selector."""

    def test_h_returns_english_by_default(self):
        """_h() returns English when _LANG is not 'zh'."""
        with patch.object(gc, "_LANG", "en"):
            result = gc._h("中文", "English")
            assert result == "English"

    def test_h_returns_chinese_when_zh(self):
        """_h() returns Chinese when _LANG is 'zh'."""
        with patch.object(gc, "_LANG", "zh"):
            result = gc._h("中文", "English")
            assert result == "中文"


# ── 7. Main CLI Tests ──────────────────────────────────────────────────────

class TestMainRepo:
    """Test main() with repo subcommand."""

    @patch("gitops_check.check_repo")
    def test_repo_subcommand_human_output(self, mock_check_repo, capsys):
        """repo subcommand with human-readable output."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="pass",
            message="Git repository accessible, branch main exists",
            details={
                "url": "https://github.com/test/repo.git",
                "branch": "main",
                "config_path": "configs/",
            }
        )

        exc_info = run_main(["gitops-check", "repo", "--url", "https://github.com/test/repo.git"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "accessible" in captured.out.lower()

    @patch("gitops_check.check_repo")
    def test_repo_subcommand_json_output(self, mock_check_repo, capsys):
        """repo subcommand with --json output."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="pass",
            message="OK",
            details={"url": "https://github.com/test/repo.git"}
        )

        exc_info = run_main(["gitops-check", "repo", "--url", "https://github.com/test/repo.git", "--json"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["overall_status"] == "pass"
        assert data["checks"][0]["check"] == "repo"

    @patch("gitops_check.check_repo")
    def test_repo_fail_exit_code(self, mock_check_repo):
        """repo subcommand with fail status → exit 1."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="fail",
            message="Cannot access repo",
            details={}
        )

        exc_info = run_main(["gitops-check", "repo", "--url", "https://github.com/test/repo.git"])

        assert exc_info.value.code == 1

    @patch("gitops_check.check_repo")
    def test_repo_warn_normal_mode_exit_1(self, mock_check_repo):
        """repo subcommand with warn status in normal mode → exit 1."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="warn",
            message="Warning",
            details={}
        )

        exc_info = run_main(["gitops-check", "repo", "--url", "https://github.com/test/repo.git"])

        assert exc_info.value.code == 1

    @patch("gitops_check.check_repo")
    def test_repo_warn_ci_mode_exit_0(self, mock_check_repo):
        """repo subcommand with warn status in --ci mode → exit 0."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="warn",
            message="Warning",
            details={}
        )

        exc_info = run_main(["gitops-check", "repo", "--url", "https://github.com/test/repo.git", "--ci"])

        assert exc_info.value.code == 0

    @patch("gitops_check.check_repo")
    def test_repo_fail_ci_mode_exit_1(self, mock_check_repo):
        """repo subcommand with fail status in --ci mode → exit 1."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="fail",
            message="Failure",
            details={}
        )

        exc_info = run_main(["gitops-check", "repo", "--url", "https://github.com/test/repo.git", "--ci"])

        assert exc_info.value.code == 1


class TestMainLocal:
    """Test main() with local subcommand."""

    @patch("gitops_check.check_local")
    def test_local_subcommand_pass(self, mock_check_local, capsys):
        """local subcommand with pass status."""
        mock_check_local.return_value = gc.CheckResult(
            check="local",
            status="pass",
            message="Configuration structure valid, 2 tenant files, 10 alerts",
            details={
                "directory": "/tmp/config",
                "defaults_file": "present",
                "tenant_files": 2,
                "total_metrics": 10,
            }
        )

        exc_info = run_main(["gitops-check", "local", "--dir", "/tmp/config"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out

    @patch("gitops_check.check_local")
    def test_local_subcommand_json(self, mock_check_local, capsys):
        """local subcommand with --json output."""
        mock_check_local.return_value = gc.CheckResult(
            check="local",
            status="pass",
            message="OK",
            details={"directory": "/tmp/config"}
        )

        exc_info = run_main(["gitops-check", "local", "--dir", "/tmp/config", "--json"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["checks"][0]["check"] == "local"

    @patch("gitops_check.check_local")
    def test_local_fail_with_parse_errors(self, mock_check_local, capsys):
        """local subcommand with parse errors."""
        mock_check_local.return_value = gc.CheckResult(
            check="local",
            status="fail",
            message="2 YAML files are invalid",
            details={
                "directory": "/tmp/config",
                "parse_errors": [
                    {"file": "db-a.yaml", "error": "syntax error"},
                    {"file": "db-b.yaml", "error": "mapping error"},
                ]
            }
        )

        exc_info = run_main(["gitops-check", "local", "--dir", "/tmp/config"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "✗" in captured.out


class TestMainSidecar:
    """Test main() with sidecar subcommand."""

    @patch("gitops_check.check_sidecar")
    def test_sidecar_subcommand_pass(self, mock_check_sidecar, capsys):
        """sidecar subcommand with pass status."""
        mock_check_sidecar.return_value = gc.CheckResult(
            check="sidecar",
            status="pass",
            message="git-sync sidecar deployed, secret exists",
            details={
                "namespace": "monitoring",
                "git_sync_secret": "present",
                "sidecar_present": True,
            }
        )

        exc_info = run_main(["gitops-check", "sidecar"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out

    @patch("gitops_check.check_sidecar")
    def test_sidecar_warn_json(self, mock_check_sidecar, capsys):
        """sidecar subcommand with warn and --json."""
        mock_check_sidecar.return_value = gc.CheckResult(
            check="sidecar",
            status="warn",
            message="Partial git-sync configuration",
            details={
                "namespace": "monitoring",
                "git_sync_secret": "missing",
                "sidecar_present": True,
            }
        )

        exc_info = run_main(["gitops-check", "sidecar", "--json"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["overall_status"] == "warn"

    @patch("gitops_check.check_sidecar")
    def test_sidecar_custom_namespace(self, mock_check_sidecar, capsys):
        """sidecar subcommand with custom namespace."""
        mock_check_sidecar.return_value = gc.CheckResult(
            check="sidecar",
            status="warn",
            message="kubectl not available",
            details={"namespace": "custom-ns"}
        )

        exc_info = run_main(["gitops-check", "sidecar", "--namespace", "custom-ns"])

        # Verify check_sidecar was called with custom namespace
        mock_check_sidecar.assert_called_once_with("custom-ns")


class TestMainEdgeCases:
    """Test main() edge cases and error handling."""

    def test_no_subcommand(self, capsys):
        """No subcommand → print help and exit caller error."""
        exc_info = run_main(["gitops-check"])

        assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_invalid_subcommand(self, capsys):
        """Invalid subcommand → argparse error."""
        run_main(["gitops-check", "invalid"])

    def test_repo_missing_required_url(self):
        """repo subcommand without --url → error."""
        run_main(["gitops-check", "repo"])

    def test_local_missing_required_dir(self):
        """local subcommand without --dir → error."""
        run_main(["gitops-check", "local"])


# ── 8. Output Format Tests ─────────────────────────────────────────────────

class TestOutputFormatting:
    """Test output formatting and symbols."""

    @patch("gitops_check.check_repo")
    def test_pass_symbol_in_output(self, mock_check_repo, capsys):
        """Pass status displays ✓ symbol."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="pass",
            message="OK",
            details={}
        )

        run_main(["gitops-check", "repo", "--url", "test"])

        captured = capsys.readouterr()
        assert "✓" in captured.out

    @patch("gitops_check.check_repo")
    def test_warn_symbol_in_output(self, mock_check_repo, capsys):
        """Warn status displays ⚠ symbol."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="warn",
            message="Warning",
            details={}
        )

        run_main(["gitops-check", "repo", "--url", "test"])

        captured = capsys.readouterr()
        assert "⚠" in captured.out

    @patch("gitops_check.check_repo")
    def test_fail_symbol_in_output(self, mock_check_repo, capsys):
        """Fail status displays ✗ symbol."""
        mock_check_repo.return_value = gc.CheckResult(
            check="repo",
            status="fail",
            message="Failed",
            details={}
        )

        run_main(["gitops-check", "repo", "--url", "test"])

        captured = capsys.readouterr()
        assert "✗" in captured.out

    @patch("gitops_check.check_local")
    def test_details_in_output(self, mock_check_local, capsys):
        """Details are printed in human-readable output."""
        mock_check_local.return_value = gc.CheckResult(
            check="local",
            status="pass",
            message="OK",
            details={
                "directory": "/path/to/config",
                "defaults_file": "present",
                "tenant_files": 2,
                "total_metrics": 10,
            }
        )

        run_main(["gitops-check", "local", "--dir", "/path/to/config"])

        captured = capsys.readouterr()
        assert "directory" in captured.out
        assert "/path/to/config" in captured.out
        assert "tenant_files" in captured.out


# ── 9. Integration Tests ───────────────────────────────────────────────────

class TestIntegration:
    """End-to-end integration tests."""

    def test_local_check_end_to_end(self, valid_config_dir, capsys):
        """Full local check without mocks."""
        exc_info = run_main(["gitops-check", "local", "--dir", valid_config_dir])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "tenant files" in captured.out.lower()
        assert "metrics" in captured.out.lower()

    def test_local_check_json_output_valid(self, valid_config_dir, capsys):
        """Local check with JSON output."""
        exc_info = run_main(["gitops-check", "local", "--dir", valid_config_dir, "--json"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["overall_status"] == "pass"
        assert data["checks"][0]["check"] == "local"
        assert data["checks"][0]["details"]["tenant_files"] == 2

    @patch("gitops_check._run_cmd")
    def test_repo_check_with_custom_parameters(self, mock_run_cmd, capsys):
        """Repo check with custom branch and path."""
        mock_run_cmd.return_value = (True, "abc\trefs/heads/develop\n", "")

        exc_info = run_main(["gitops-check", "repo", "--url", "git@github.com:test/repo.git", "--branch", "develop", "--path", "config/"])

        assert exc_info.value.code == 0


# ── 10. Error Recovery Tests ───────────────────────────────────────────────

class TestErrorRecovery:
    """Test error recovery and edge cases."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="os.symlink requires admin privileges on Windows; the "
               "test verifies we follow symlinks correctly, which is a "
               "Linux/macOS-relevant code path. Verified to pass on "
               "Linux CI runners.",
    )
    def test_local_with_symlink_directory(self, config_dir):
        """Local check with symlink directory."""
        # Create actual config dir
        actual_dir = os.path.join(config_dir, "actual")
        os.makedirs(actual_dir)

        defaults = {"threshold": 100}
        with open(os.path.join(actual_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Create symlink
        link_dir = os.path.join(config_dir, "link")
        os.symlink(actual_dir, link_dir)

        result = gc.check_local(link_dir)

        assert result.status == "pass"

    def test_local_with_permission_error(self, config_dir):
        """Local check when directory is not readable."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w") as f:
            yaml.dump(defaults, f)

        # Mock Path.iterdir to raise OSError. (Earlier versions used
        # os.listdir; the pathlib refactor switched to Path.iterdir,
        # which raises identically on permission errors.)
        with patch("pathlib.Path.iterdir",
                    side_effect=OSError("Permission denied")):
            result = gc.check_local(config_dir)

        assert result.status == "fail"
        assert "cannot scan" in result.message.lower()

    def test_run_cmd_with_unicode_error(self):
        """_run_cmd handles unicode in error messages."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("錯誤信息")
            success, stdout, stderr = gc._run_cmd(["test"])

        assert success is False
        assert "錯誤" in stderr or "error" in stderr.lower()


# ── 11. Timestamp Tests ────────────────────────────────────────────────────

class TestTimestamps:
    """Test timestamp generation in reports."""

    @patch("gitops_check.check_local")
    def test_report_has_valid_iso_timestamp(self, mock_check_local, capsys):
        """Report contains valid ISO 8601 timestamp."""
        mock_check_local.return_value = gc.CheckResult(
            check="local",
            status="pass",
            message="OK",
            details={}
        )

        run_main(["gitops-check", "local", "--dir", "/tmp", "--json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        timestamp = data["timestamp"]

        # Validate ISO 8601 format (timezone-aware)
        assert "T" in timestamp
        # Parse to verify it's a valid datetime
        datetime.fromisoformat(timestamp)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
