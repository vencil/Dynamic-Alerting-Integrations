#!/usr/bin/env python3
"""test_gitops_check.py — gitops_check.py pytest comprehensive test suite.

Validates:
  1. check_local() — local conf.d structure: _defaults.yaml presence, every
     file read through `da-guard parse-tree`, metric / tenant counts; a
     `--dir` that is no readable directory is a caller error (exit 2).
  2. main() — `local` human / JSON output and exit codes; the removed
     `repo` / `sidecar` subcommands and `--ci` (#3376, #3348) are argparse
     errors (exit 2).
  3. Data models, the bilingual selector, timestamps.
"""

import errno
import json
import os
import shutil
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
from _platform_fs import symlink_or_skip, unprivileged_sandbox  # noqa: E402
from _go_refusal import refuses  # noqa: E402  (#3206)
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


def _as_run(result):
    """What `_lib_exitcodes.run` does with main()'s answer (#3216): a Report's
    document is written and its status is the exit, as SystemExit."""
    raise SystemExit(emit_result(result))

_REPO = Path(__file__).resolve().parents[2]


def run_main(argv):
    """Run emit_result(gc.main()) under the given sys.argv and return the pytest
    ExceptionInfo for the expected SystemExit. Callers assert on
    exc_info.value.code and read capsys themselves, so assertion order
    (code-first vs output-first) stays explicit at the call site.
    """
    with patch("sys.argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            _as_run(gc.main())
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
        with open(os.path.join(tmpdir, "_defaults.yaml"), "w", encoding="utf-8") as f:
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

        with open(os.path.join(tmpdir, "db-a.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(db_a, f)
        with open(os.path.join(tmpdir, "db-b.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(db_b, f)

        yield tmpdir


class TestLocalDirMissingIsCallerError:
    """#3211 (owner-approved rc 1 → 2): `local --dir` naming no directory."""

    @pytest.mark.parametrize("as_json", [False, True])
    def test_rc_2_and_one_error_line(self, tmp_path, as_json):
        missing = str(tmp_path / "does-not-exist")
        argv = [sys.executable, str(Path(gc.__file__)), "local", "--dir", missing]
        if as_json:
            argv.append("--json")
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              timeout=120)
        assert proc.returncode == EXIT_CALLER_ERROR, proc.stderr
        assert "Traceback" not in proc.stderr
        assert proc.stderr.count("ERROR:") == 1, proc.stderr
        # named before da-guard is asked — not da-guard failing on no tree
        assert f"ERROR: --dir {missing}: {os.strerror(errno.ENOENT)}" in proc.stderr, proc.stderr
        if as_json:
            doc = json.loads(proc.stdout)
            assert (doc["status"], doc["reason"]) == ("caller_error", "dir_not_found")
            # dev-rules §13: the report's own keys stay, zeroed
            assert (doc["overall_status"], doc["checks"]) == ("", []), doc
            assert doc["timestamp"], doc

    def test_a_file_is_not_a_directory(self, tmp_path):
        """`--dir` naming a regular file: the same caller error, named before
        da-guard is asked (not da-guard failing on a non-tree)."""
        f = tmp_path / "conf.yaml"
        f.write_text("tenants: {}\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(Path(gc.__file__)), "local", "--dir", str(f),
                               "--json"], capture_output=True, text=True, encoding="utf-8",
                              timeout=120)
        assert proc.returncode == EXIT_CALLER_ERROR, proc.stderr
        assert json.loads(proc.stdout)["reason"] == "dir_not_found", proc.stdout


class TestLocalDirNotAccessibleIsCallerError:
    """#3241: `local --dir` under a parent the caller cannot enter —
    `Path.is_dir` raises EACCES there; a named caller error (rc 2 unchanged),
    not `internal_error`."""

    @pytest.mark.parametrize("err, reason", [
        (errno.EACCES, "dir_not_accessible"),
        (errno.ENAMETOOLONG, "dir_not_accessible"),
        (errno.EIO, "dir_not_accessible"),
        (errno.ENOENT, "dir_not_found"),
        (errno.ENOTDIR, "dir_not_found"),
        (errno.ELOOP, "dir_not_accessible"),  # #3392: `ls` lists a loop
    ])
    def test_errno_decides_the_reason(self, monkeypatch, err, reason):
        """By errno, not by which errnos this Python's `Path.is_dir` swallows
        (3.13 raises EACCES, 3.14 returns False)."""
        from _lib_exitcodes import CallerError

        def _stat(path, *a, **kw):
            raise OSError(err, os.strerror(err), str(path))
        monkeypatch.setattr(gc.os, "stat", _stat)
        with pytest.raises(CallerError) as exc_info:
            gc.check_local("/locked/conf.d")
        monkeypatch.undo()
        assert exc_info.value.reason == reason
        assert "/locked/conf.d" in str(exc_info.value)
        if reason == "dir_not_accessible":
            assert os.strerror(err) in str(exc_info.value)

    def test_a_python_whose_is_dir_swallows_eacces(self, monkeypatch):
        """3.14's `Path.is_dir` answers False on EACCES: still named."""
        from _lib_exitcodes import CallerError

        def _stat(path, *a, **kw):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        monkeypatch.setattr(Path, "is_dir", lambda self, *a, **kw: False)
        monkeypatch.setattr(gc.os, "stat", _stat)
        with pytest.raises(CallerError) as exc_info:
            gc.check_local("/locked/conf.d")
        monkeypatch.undo()
        assert exc_info.value.reason == "dir_not_accessible"

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX permission bits")
    def test_real_locked_parent(self, tmp_path):
        """The real EACCES, through the CLI: a non-root caller under a parent
        it cannot enter. Root re-runs as nobody via setpriv, from a fresh
        system-temp dir nobody can reach (pytest's own tmp root is 0700)."""
        prefix = []
        base = tmp_path
        if os.geteuid() == 0:
            setpriv = shutil.which("setpriv")
            if setpriv is None:
                pytest.skip("root without setpriv: no uid that EACCES applies to")
            prefix = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups"]
            script = Path(gc.__file__).resolve()
            if not (all(p.stat().st_mode & 0o001 for p in script.parents)
                    and script.stat().st_mode & 0o004):
                pytest.skip("checkout not readable by nobody: the CLI could not even start")
            base = Path(tempfile.mkdtemp())
            base.chmod(0o755)
        locked = base / "locked"
        (locked / "conf.d").mkdir(parents=True)
        locked.chmod(0o700 if prefix else 0o000)
        # nobody cannot write the parent's coverage data (root's pytest tmp is
        # 0700): with COVERAGE_PROCESS_START exported (tests/conftest.py, under
        # --cov) the child's coverage .pth would print a Traceback of its own.
        env = {k: v for k, v in os.environ.items()
               if not (prefix and k.startswith("COVERAGE_PROCESS_"))}
        try:
            proc = subprocess.run(
                [*prefix, sys.executable, str(Path(gc.__file__)), "local",
                 "--dir", str(locked / "conf.d"), "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=120, env=env)
        finally:
            locked.chmod(0o755)
            if base is not tmp_path:
                shutil.rmtree(base)
        assert proc.returncode == EXIT_CALLER_ERROR, proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        doc = json.loads(proc.stdout)
        assert (doc["status"], doc["reason"]) == ("caller_error", "dir_not_accessible"), doc


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
        """No directory to check is the caller's input, not a fail (#3211)."""
        from _lib_exitcodes import CallerError
        with pytest.raises(CallerError) as exc_info:
            gc.check_local("/nonexistent/path/xyz")
        assert exc_info.value.reason == "dir_not_found"
        assert "/nonexistent/path/xyz" in str(exc_info.value)

    def test_local_missing_defaults_yaml(self, config_dir):
        """Missing _defaults.yaml (required)."""
        result = gc.check_local(config_dir)

        assert result.check == "local"
        assert result.status == "fail"
        assert "missing" in result.message.lower()
        assert "_defaults.yaml" in result.message

    def test_local_invalid_defaults_yaml(self, config_dir):
        """Invalid YAML in _defaults.yaml: the tree cannot be served, so the
        shared refusal names it and the check stops (#3206)."""
        bad_yaml = "key: value\n  bad indentation:\n not aligned:"
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            f.write(bad_yaml)

        with refuses("_defaults.yaml"):
            gc.check_local(config_dir)

    def test_local_invalid_tenant_yaml(self, config_dir):
        """Invalid YAML in tenant file."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        # Create bad tenant file
        with open(os.path.join(config_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
            f.write("invalid: yaml\n  bad indent:")

        with refuses("db-a.yaml"):
            gc.check_local(config_dir)

    def test_local_multiple_parse_errors(self, config_dir):
        """Multiple tenant files with YAML errors."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        # Create multiple bad tenant files with actual syntax errors
        for fname in ["db-a.yaml", "db-b.yaml", "db-c.yaml"]:
            with open(os.path.join(config_dir, fname), "w", encoding="utf-8") as f:
                # Use invalid YAML: unclosed quotes
                f.write("key: 'unclosed quote\nanother: value")

        with refuses("db-a.yaml", "db-b.yaml", "db-c.yaml"):
            gc.check_local(config_dir)

    def test_local_empty_tenant_files(self, config_dir):
        """Only _defaults.yaml, no tenant files."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        result = gc.check_local(config_dir)

        assert result.status == "pass"
        assert result.details["tenant_files"] == 0
        assert result.details["total_metrics"] == 0

    def test_local_ignores_reserved_files(self, config_dir):
        """`_`-prefixed control files are not tenant carriers.

        ⛔ Renamed from `test_local_ignores_hidden_files`, which called a
        `_`-prefixed file "hidden". Go's name rules make reserved
        (`_` prefix, class defaults / platform) and hidden (`.` prefix) two
        DELIBERATELY orthogonal properties, and this reader treats them differently: it
        drops reserved names and COUNTS dot-prefixed ones. Under the old
        name, anyone grepping for coverage of the hidden axis found this
        green test and stopped — see the SCOPE block further down, which
        records that the hidden axis has no assertion at all.
        """
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        # Create reserved-prefix file (should be ignored)
        with open(os.path.join(config_dir, "_hidden.yaml"), "w", encoding="utf-8") as f:
            yaml.dump({"hidden": True}, f)

        result = gc.check_local(config_dir)

        assert result.status == "pass"
        assert result.details["tenant_files"] == 0

    def test_local_ignores_non_yaml_files(self, config_dir):
        """Non-.yaml files are ignored."""
        defaults = {"threshold": 100}
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        # Create non-yaml file
        with open(os.path.join(config_dir, "README.md"), "w", encoding="utf-8") as f:
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
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        # Create tenant file with only internal keys
        with open(os.path.join(config_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
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
# The oracle is the exporter's scanner: `scanDirHierarchical` (`config_hierarchy.go`) lowercases
# the entry name and accepts BOTH `.yaml` and `.yml`.
#
# ⚠️ SCOPE. These pin the extension-SPELLING axis only. The other three axes
# are NOT covered, and none of them has a ticket of its own behind it, so
# the disclosure has to carry itself:
#   * Recursion: `check_local` is flat by construction (`base.iterdir()`).
#     That is `test_confd_enumeration_contract.py`'s axis; nested carriers do
#     get a `WARN` on stderr, so this one at least speaks.
#   * Hidden names: closed by #2055 — `check_local` now skips dot-prefixed
#     entries as the exporter's walker does. Pinned by
#     `TestCheckLocalHiddenEntries` below, not by this class; the fixtures
#     here still contain no dot-prefixed name.
#   * Entries `is_file()` drops (a directory named `notes.yml/`, a broken
#     symlink) are still silently skipped rather than named. ⚠️ #1607's
#     closing comment verifies `operator_generate`
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

            # The shared refusal (#3206) must name the carrier it could not
            # parse, for either spelling.
            with refuses(f"db-b{ext}"):
                gc.check_local(str(root))

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
        the docstring of the (since deleted, #3206)
        `has_yaml_extension` says this argument gets touched —
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
            drop the reserved (`_`) term          -> 4 / 12   (`_defaults.yaml`
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
        """FLOOR, derived: one carrier per member of `SUFFIXES`.

        The two tests above hard-code `.yaml` and `.yml`, so they stop
        covering the floor the day the exporter grows a third spelling and
        the shared name table follows it. This one reads the set
        instead of restating it, so it widens on its own.

        It reads the set from the shared name table
        (`_confd_name_table.config_suffixes`), which Go owns — pkg/confdname's
        matrix test asserts every cell; no Python rule restates it (#3206).

        ⚠️ Floor only: satisfied by a reader that is too WIDE, which is what
        the counterexample test above is for.
        """
        from _confd_name_table import config_suffixes  # noqa: PLC0415
        SUFFIXES = config_suffixes()  # Go's table (#3206)

        # ⛔ Anti-vacuity: an empty `SUFFIXES` would make the assertion
        # below `0 == 0`. The set is not this test's to guard, so say who
        # does rather than pretending a floor here would be independent.
        assert len(SUFFIXES) >= 2, (
            f"SUFFIXES collapsed to {SUFFIXES!r}; the shared "
            f"classification matrix is the thing that should have gone red "
            f"first, so fix that before touching this line"
        )

        root = tmp_path / "confd"
        root.mkdir()
        Path(root, "_defaults.yaml").write_text(
            "global_threshold: 100\n", encoding="utf-8")
        for i, suffix in enumerate(SUFFIXES):
            Path(root, f"t{i}{suffix}").write_text(
                "".join(f"k{j}: '{j}'\n" for j in range(i + 1)),
                encoding="utf-8")

        result = gc.check_local(str(root))
        expected_metrics = sum(range(1, len(SUFFIXES) + 1))

        assert result.status == "pass"
        assert (result.details["tenant_files"],
                result.details["total_metrics"]) == (
                    len(SUFFIXES), expected_metrics), (
            f"one carrier was written per member of SUFFIXES "
            f"({SUFFIXES!r}) and gitops-check counted "
            f"tenant_files={result.details['tenant_files']} "
            f"total_metrics={result.details['total_metrics']}, expected "
            f"{(len(SUFFIXES), expected_metrics)}"
        )


class TestCheckLocalHiddenEntries:
    """#2055: `.`-prefixed names are skipped by the exporter's walker, so
    `check_local` must neither count them nor fail on them.

    ⛔ The last test is the CONTROL: the same broken body under a visible
    name must still fail, or "passes with a broken hidden file" is equally
    satisfied by a check that never parses anything.
    """

    _BROKEN = "k1: [unclosed\n"

    @staticmethod
    def _root(tmp_path):
        root = tmp_path / "confd"
        root.mkdir()
        Path(root, "_defaults.yaml").write_text(
            "global_threshold: 100\n", encoding="utf-8")
        Path(root, "acme.yaml").write_text("k1: '1'\n", encoding="utf-8")
        return root

    def test_hidden_file_is_not_a_tenant_file(self, tmp_path):
        root = self._root(tmp_path)
        Path(root, ".hidden.yaml").write_text(
            "k1: '1'\nk2: '2'\n", encoding="utf-8")

        result = gc.check_local(str(root))

        assert result.status == "pass"
        assert (result.details["tenant_files"],
                result.details["total_metrics"]) == (1, 1), result.details

    def test_broken_hidden_leftover_does_not_fail(self, tmp_path):
        root = self._root(tmp_path)
        Path(root, ".acme.yaml").write_text(self._BROKEN, encoding="utf-8")

        result = gc.check_local(str(root))

        assert result.status == "pass", result.details
        assert result.details["tenant_files"] == 1

    def test_control_broken_visible_file_still_fails(self, tmp_path):
        root = self._root(tmp_path)
        Path(root, "bad.yaml").write_text(self._BROKEN, encoding="utf-8")

        with refuses("bad.yaml"):
            gc.check_local(str(root))


# ── 5. CheckResult & GitOpsReport Tests ────────────────────────────────────

class TestDataModels:
    """Test CheckResult and GitOpsReport data structures."""

    def test_check_result_creation(self):
        """CheckResult dataclass initialization."""
        result = gc.CheckResult(
            check="local",
            status="pass",
            message="Test message",
            details={"url": "https://github.com/test/repo.git"}
        )

        assert result.check == "local"
        assert result.status == "pass"
        assert result.message == "Test message"
        assert result.details["url"] == "https://github.com/test/repo.git"

    def test_gitops_report_to_dict(self):
        """GitOpsReport.to_dict() serialization."""
        check = gc.CheckResult(
            check="local",
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
        assert data["checks"][0]["check"] == "local"
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

    @pytest.mark.parametrize("extra", [(), ("--json",)])
    @patch("gitops_check.check_local")
    def test_local_fail_with_parse_errors(self, mock_check_local, capsys, extra):
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

        exc_info = run_main(["gitops-check", "local", "--dir", "/tmp/config", *extra])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        if extra:  # #3216: the document goes out and the exit is still the verdict
            assert json.loads(captured.out)["overall_status"] == "fail"
        else:
            assert "✗" in captured.out

    @patch("gitops_check.check_local")
    def test_no_parse_tree_under_json_is_rc_2_with_the_report(self, mock_check_local, capsys):
        """ADR-036 step 4: no usable parse-tree is a caller error — under
        --json the report saying so goes out and the exit is 2 (#3216)."""
        mock_check_local.side_effect = gc.ParseTreeError("no da-guard")
        exc_info = run_main(["gitops-check", "local", "--dir", "/tmp/config", "--json"])
        assert exc_info.value.code == EXIT_CALLER_ERROR
        doc = json.loads(capsys.readouterr().out)
        assert doc["checks"][0]["details"]["reason"] == "da_guard_failed"


class TestMainEdgeCases:
    """Test main() edge cases and error handling."""

    def test_no_subcommand(self, capsys):
        """No subcommand → print help and exit caller error."""
        exc_info = run_main(["gitops-check"])

        assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_invalid_subcommand(self, capsys):
        """Invalid subcommand → argparse error."""
        run_main(["gitops-check", "invalid"])


    @pytest.mark.parametrize("argv, refusal", [
        (["repo", "--url", "https://example.com/r.git"], "invalid choice: 'repo'"),
        (["sidecar"], "invalid choice: 'sidecar'"),
        (["local", "--dir", "{conf}", "--ci"], "unrecognized arguments: --ci"),
    ], ids=["repo", "sidecar", "local --ci"])
    def test_removed_subcommands_and_ci_are_caller_errors(self, argv, refusal,
                                                          valid_config_dir, capsys):
        """#3376, #3348: `repo`, `sidecar` and `--ci` checked GitOps Native
        Mode, refused since #1349; they are gone, so argparse refuses them
        (exit 2) rather than run a check that could only fail. The refusal
        text is asserted, not only the 2: without kubectl `sidecar` was 2
        too, and `--ci` against a valid conf.d is the only way its absence
        shows."""
        argv = [a.replace("{conf}", valid_config_dir) for a in argv]
        exc_info = run_main(["gitops-check", *argv])
        assert exc_info.value.code == EXIT_CALLER_ERROR
        assert refusal in capsys.readouterr().err

    def test_local_missing_required_dir(self):
        """local subcommand without --dir → error."""
        run_main(["gitops-check", "local"])


# ── 8. Output Format Tests ─────────────────────────────────────────────────

class TestOutputFormatting:
    """Test output formatting and symbols."""


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
        with open(os.path.join(actual_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(defaults, f)

        # Create symlink
        link_dir = os.path.join(config_dir, "link")
        symlink_or_skip(actual_dir, link_dir)

        result = gc.check_local(link_dir)

        assert result.status == "pass"

    def test_local_names_a_file_parse_tree_cannot_read(self, config_dir):
        """ADR-036 step 4 (#2906): the tree is listed and read by `da-guard
        parse-tree`, so a file it cannot read — at any depth, here one that
        is not UTF-8 — is named (by the shared refusal, #3206), never skipped."""
        with open(os.path.join(config_dir, "_defaults.yaml"), "w", encoding="utf-8") as f:
            f.write("defaults:\n  threshold: 100\n")
        os.makedirs(os.path.join(config_dir, "team"))
        with open(os.path.join(config_dir, "team", "bad.yaml"), "wb") as f:
            f.write(b"tenants:\n  t\xff: {}\n")

        with refuses("team/bad.yaml") as err:
            gc.check_local(config_dir)
        assert "invalid_utf8: not valid UTF-8 at byte 12" in err.getvalue()

    def test_local_names_a_dir_it_cannot_list(self):
        """#3277: `--dir` itself cannot be listed (mode 000 to this uid). The
        refusal is da-guard's for the root (`walk_error`, rc 2) — never a
        report that `_defaults.yaml` is missing from a tree nobody read."""
        with unprivileged_sandbox(str(_REPO)) as (box, run):
            conf = Path(box) / "conf.d"
            conf.mkdir()
            (conf / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
            (conf / "_defaults.yaml").chmod(0o644)
            conf.chmod(0)
            probe = run([sys.executable, "-c", "import os, sys; os.listdir(sys.argv[1])",
                         str(conf)])
            assert probe.returncode != 0, "the uid can list it: the case would be vacuous"
            proc = run([sys.executable,
                        str(Path(box) / "scripts" / "tools" / "ops" / "gitops_check.py"),
                        "local", "--dir", str(conf), "--json"])
        assert proc.returncode == EXIT_CALLER_ERROR, proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "(walk_error)" in proc.stderr, proc.stderr
        assert "_defaults.yaml" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
        doc = json.loads(proc.stdout)
        assert (doc["status"], doc["reason"]) == ("caller_error", "conf_d_not_servable"), doc
        assert doc["refused_paths"] == [{"path": ".", "verdict": "unservable"}], doc


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
