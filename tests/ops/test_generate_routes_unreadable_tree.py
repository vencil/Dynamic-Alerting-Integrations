"""#1460 — an unreadable tenant file must not yield a SMALLER success.

Measured on main before the fix (``d4754fc7``): ``conf.d/`` with two tenants,
one of them with an unterminated quoted scalar::

    $ generate-routes --config-dir conf.d --validate
      WARN: skip unparseable alpha.yaml: while scanning a quoted scalar ...
    Found 1 tenant(s) with routing config: beta
    Validation: 1 route(s), 1 receiver(s), 1 inhibit rule(s)
    OK: all configs valid
    rc=0

— with ``--strict`` too. The stdout tail and the exit code were byte-identical
to the all-valid run; the only difference was a number in the middle with no
baseline to compare it against. Inside the shipped pre-commit hook (which
prints hook output only on failure) not even the WARN reached the screen.

The rule shared with #1405 / #1420 (config-diff): an input that could not be
parsed makes the verdict UNTRUSTWORTHY; it does not make a smaller success.

⛔ Every assertion here checks the MESSAGE and the COUNTS as well as the exit
code: after #1616 / #1617 this tool reaches rc=1 and rc=2 for several
unrelated reasons, so ``rc == 1`` alone is satisfied by causes these tests
never meant to pin (#1642).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from factories import make_routing_config, make_tenant_yaml
from generate_alertmanager_routes import (
    TenantTree,
    load_tenant_configs,
    load_tenant_tree,
)

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")
EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR = 0, 1, 2

# An unterminated quoted scalar — the exact shape in the ticket ("take one
# quote out of a tenant file").
_BROKEN_QUOTE = 'tenants:\n  alpha:\n    mysql_connections: "70\n'


def _gar(argv):
    return subprocess.run(
        [sys.executable, "-s", str(_GAR), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=180)


def _good_tenant(name: str) -> str:
    return make_tenant_yaml(name, keys={"mysql_connections": "70"},
                            routing=make_routing_config(name))


@pytest.fixture
def broken_tree(tmp_path):
    """alpha.yaml does not parse; beta.yaml is a full routing tenant."""
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "alpha.yaml").write_text(_BROKEN_QUOTE, encoding="utf-8")
    (d / "beta.yaml").write_text(_good_tenant("beta"), encoding="utf-8")
    return d


@pytest.fixture
def good_tree(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "alpha.yaml").write_text(_good_tenant("alpha"), encoding="utf-8")
    (d / "beta.yaml").write_text(_good_tenant("beta"), encoding="utf-8")
    return d


# ── The structured record (the thing that did not exist) ─────────────
class TestTheRecordIsStructural:
    """The consumer must derive counts from data, never from the WARN text."""

    def test_unparseable_tenant_file_is_recorded_with_its_name(self, broken_tree):
        tree = load_tenant_tree(str(broken_tree))
        assert isinstance(tree, TenantTree)
        names = [f for f, _ in tree.tenant_file_errors]
        assert names == ["alpha.yaml"], tree.tenant_file_errors
        _, reason = tree.tenant_file_errors[0]
        assert "failed to parse" in reason and "fix:" in reason, reason
        assert tree.files_read == 1
        assert [f for f, _ in tree.files_skipped] == ["alpha.yaml"]

    def test_control_clean_tree_has_no_record(self, good_tree):
        tree = load_tenant_tree(str(good_tree))
        assert tree.tenant_file_errors == []
        assert tree.files_skipped == []
        assert tree.files_read == 2

    def test_the_tuple_form_keeps_its_shape(self, broken_tree):
        """Dozens of callers unpack five positionals; the record rides on the
        tree form only, so none of them changes."""
        tup = load_tenant_configs(str(broken_tree))
        assert len(tup) == 5
        assert tup == load_tenant_tree(str(broken_tree)).as_tuple()

    def test_a_non_mapping_top_level_is_the_same_class(self, tmp_path):
        """A file that parses to a list is dropped through the same helper
        as one that does not parse — #1447 made the two report alike, and
        this keeps them refused alike."""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "alpha.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        (d / "beta.yaml").write_text(_good_tenant("beta"), encoding="utf-8")
        tree = load_tenant_tree(str(d))
        assert [f for f, _ in tree.tenant_file_errors] == ["alpha.yaml"]
        assert tree.files_read == 1, "a dropped file is not a read file"


# ── --validate ────────────────────────────────────────────────────────
class TestValidateRefusesAnUnreadableTree:

    @pytest.mark.parametrize("extra", [[], ["--strict"]],
                             ids=["plain", "strict"])
    def test_validate_exits_1_and_names_the_file_on_stdout(self, broken_tree, extra):
        r = _gar(["--config-dir", str(broken_tree), "--validate", *extra])
        assert r.returncode == EXIT_VIOLATION, (r.stdout, r.stderr)
        # The name is on STDOUT: pre-commit shows a failing hook's output,
        # but a reader who only has stdout (a CI log filter, `2>/dev/null`)
        # must still see WHICH file.
        assert "alpha.yaml" in r.stdout, r.stdout
        assert "Config files: 1 read, 1 skipped" in r.stdout, r.stdout
        assert "OK: all configs valid" not in r.stdout, (
            "the smaller success is exactly the defect")
        # The cause and the remedy are on stderr with the FAIL framing.
        assert "FAIL:" in r.stderr and "alpha.yaml" in r.stderr, r.stderr
        assert "could not be read" in r.stderr, r.stderr
        # The pre-existing WARN line is kept.
        assert "WARN: skip unparseable alpha.yaml" in r.stderr, r.stderr

    def test_control_both_valid_is_ok_with_counts(self, good_tree):
        r = _gar(["--config-dir", str(good_tree), "--validate"])
        assert r.returncode == EXIT_OK, (r.stdout, r.stderr)
        assert "Config files: 2 read, 0 skipped" in r.stdout, r.stdout
        assert r.stdout.rstrip().endswith("OK: all configs valid"), r.stdout

    def test_the_failure_is_on_the_exit_code_not_only_on_stderr(self, broken_tree):
        """The pre-commit shape: `pre-commit` decides pass/fail from the exit
        code and shows output only on failure. A tool that shouts on stderr
        and exits 0 is `Passed` with the shout discarded — that is the
        original symptom, so the code is the assertion here, and the
        message is checked separately above."""
        r = subprocess.run(
            [sys.executable, "-s", str(_GAR), "--config-dir", str(broken_tree),
             "--validate"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        assert r.returncode == EXIT_VIOLATION

    def test_a_tree_that_is_only_the_broken_file_is_not_no_tenants_found(
            self, tmp_path):
        """⛔ Before the fix this printed `No tenants found in config
        directory.` and exited 0 — the early exit ran before anything looked
        at what had been skipped."""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "alpha.yaml").write_text(_BROKEN_QUOTE, encoding="utf-8")
        r = _gar(["--config-dir", str(d), "--validate"])
        assert r.returncode == EXIT_VIOLATION, (r.stdout, r.stderr)
        assert "No tenants found" not in r.stdout, r.stdout
        assert "Config files: 0 read, 1 skipped (alpha.yaml)" in r.stdout, r.stdout


# ── Render / --output-configmap: never emit from a partial tree ───────
class TestWritersRefuseAnUnreadableTree:

    def test_render_mode_exits_1_and_writes_nothing(self, broken_tree, tmp_path):
        out = tmp_path / "routes.yaml"
        r = _gar(["--config-dir", str(broken_tree), "-o", str(out)])
        assert r.returncode == EXIT_VIOLATION, (r.stdout, r.stderr)
        assert not out.exists(), "a fragment from a partial tree was written"
        assert "alpha.yaml" in r.stdout and "1 read, 1 skipped" in r.stdout, r.stdout

    def test_output_configmap_exits_1_and_emits_no_configmap(self, broken_tree):
        """A partial ConfigMap applied to the cluster is the worst outcome
        here, so the GitOps writer refuses too."""
        r = _gar(["--config-dir", str(broken_tree), "--output-configmap"])
        assert r.returncode == EXIT_VIOLATION, (r.stdout, r.stderr)
        assert "kind: ConfigMap" not in r.stdout, r.stdout
        assert "alpha.yaml" in r.stdout, r.stdout

    def test_control_render_mode_writes_the_file_and_prints_counts(
            self, good_tree, tmp_path):
        out = tmp_path / "routes.yaml"
        r = _gar(["--config-dir", str(good_tree), "-o", str(out)])
        assert r.returncode == EXIT_OK, (r.stdout, r.stderr)
        assert out.exists()
        assert "Config files: 2 read, 0 skipped" in r.stdout, r.stdout


# ── Boundary: policy files keep their --strict-gated treatment ─────────
class TestPolicyFilesKeepTheirOwnGate:
    """`_domain_policy.yaml` already had its record (policy_file_errors) and
    its own gate (`--strict`, ADR-007); #1460 does not re-decide that. It IS
    counted as a skipped file — the accounting line is about files, and a
    skipped file is a skipped file — but it is not a tenant_file_error."""

    def test_broken_policy_file_is_counted_but_not_a_tenant_error(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_domain_policy.yaml").write_text("domain_policies: [", encoding="utf-8")
        (d / "beta.yaml").write_text(_good_tenant("beta"), encoding="utf-8")
        tree = load_tenant_tree(str(d))
        assert [f for f, _ in tree.files_skipped] == ["_domain_policy.yaml"]
        assert tree.tenant_file_errors == []
        r = _gar(["--config-dir", str(d), "--validate"])
        assert r.returncode == EXIT_OK, (r.stdout, r.stderr)
        assert "Config files: 1 read, 1 skipped (_domain_policy.yaml)" in r.stdout
        r = _gar(["--config-dir", str(d), "--validate", "--strict"])
        assert r.returncode == EXIT_VIOLATION, "the ADR-007 gate is unchanged"
