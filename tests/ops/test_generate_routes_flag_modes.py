"""#1650 — a flag the current mode never reads is a caller error (exit 2).

The flag × mode matrix `_flags_this_mode_never_reads` implements, from the
read sites in `generate_alertmanager_routes.main()` and its mode handlers::

    flag          --validate  --apply  --output-configmap  render
    -o/--output   never       never    unless --dry-run    unless --dry-run
    --dry-run     never       never    read                read
    --namespace   never       read     read                never
    --configmap   never       read     read                never
    --yes         never       read     never               never

Measured on main before the fix (``d4754fc7``): ``--validate -o <path>``
exited 0 with the file absent and stderr empty — the operator's signal was
byte-identical to "all good". Same class as the ``--base-config`` carrier
#1616 closed; these are the four flags that ticket deliberately left for a
second PR.

⛔ Every "unread" cell here is paired with a CONTROL in a mode that reads the
flag, and the control asserts the flag's EFFECT — otherwise the whole table
is satisfied by a tool that exits 2 on every invocation (#1642). And every
prescription the tool prints is RUN, so a remedy argparse rejects (the #1616
first-cut mistake) or one that lands back on this guard cannot ship.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from factories import make_routing_config, make_tenant_yaml

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")
EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR = 0, 1, 2
GUARD = "is not read in"          # the guard's message shape
# ⛔ Not the CLI default and not a fixture value: a generator that ignored
# the argument and used its default would still print "monitoring".
PROBE_NS, PROBE_CM = "da-probe-ns", "da-probe-cm"


def _gar(argv, stdin=b""):
    return subprocess.run(
        [sys.executable, "-s", str(_GAR), *argv],
        input=stdin, capture_output=True, timeout=180)


def _text(r) -> str:
    return (r.stdout + r.stderr).decode("utf-8", "replace")


@pytest.fixture(scope="module")
def tenant_dir(tmp_path_factory):
    """A tree that PRODUCES routes — controls must reach the output paths."""
    d = tmp_path_factory.mktemp("conf.d")
    (d / "alpha.yaml").write_text(
        make_tenant_yaml("alpha", keys={"mysql_connections": "70"},
                         routing=make_routing_config("alpha")),
        encoding="utf-8")
    return d


@pytest.fixture(scope="module")
def routeless_dir(tmp_path_factory):
    """#1616 lesson: parses fine, declares thresholds, yields NO routes, so
    main() would exit 0 at "No tenants found" before any mode handler runs.
    A check inside a handler is invisible on this tree."""
    d = tmp_path_factory.mktemp("conf.d-routeless")
    (d / "alpha.yaml").write_text(
        "thresholds:\n  mysql_connections:\n    warning: 100\n", encoding="utf-8")
    return d


# ── The unread cells: (id, argv-without-config-dir, flag, mode, prescriptions)
# `prescriptions` are the argv the printed remedy tells the operator to run;
# each is executed and must NOT come back through this guard. `out` in an
# argv is replaced by a per-test path.
_UNREAD = [
    ("o/validate", ["--validate", "-o", "out"], "-o", "--validate",
     [["--validate"], ["-o", "out"]]),
    ("o/apply", ["--apply", "--yes", "-o", "out"], "-o", "--apply",
     [["--apply", "--yes"], ["--output-configmap", "-o", "out"]]),
    ("o/render+dry-run", ["--dry-run", "-o", "out"], "-o", "render",
     [["-o", "out"], ["--dry-run"]]),
    ("o/configmap+dry-run", ["--output-configmap", "--dry-run", "-o", "out"],
     "-o", "--output-configmap",
     [["--output-configmap", "-o", "out"], ["--output-configmap", "--dry-run"]]),
    ("dry-run/validate", ["--validate", "--dry-run"], "--dry-run", "--validate",
     [["--validate"], ["--dry-run"]]),
    ("dry-run/apply", ["--apply", "--yes", "--dry-run"], "--dry-run", "--apply",
     [["--apply", "--yes"], ["--output-configmap", "--dry-run"]]),
    ("namespace/validate", ["--validate", "--namespace", PROBE_NS],
     "--namespace", "--validate",
     [["--validate"], ["--output-configmap", "--namespace", PROBE_NS]]),
    ("namespace/render", ["--namespace", PROBE_NS], "--namespace", "render",
     [[], ["--output-configmap", "--namespace", PROBE_NS]]),
    ("configmap/validate", ["--validate", "--configmap", PROBE_CM],
     "--configmap", "--validate",
     [["--validate"], ["--output-configmap", "--configmap", PROBE_CM]]),
    ("configmap/render", ["--configmap", PROBE_CM], "--configmap", "render",
     [[], ["--output-configmap", "--configmap", PROBE_CM]]),
    ("yes/validate", ["--validate", "--yes"], "--yes", "--validate",
     [["--validate"], ["--apply", "--yes"]]),
    ("yes/output-configmap", ["--output-configmap", "--yes"], "--yes",
     "--output-configmap", [["--output-configmap"]]),
    ("yes/render", ["--yes"], "--yes", "render", [[], ["--apply", "--yes"]]),
    # --validate wins over --apply / --output-configmap (main() exits inside
    # _validate_mode), so flags those modes read are unread here too.
    ("namespace/validate+apply",
     ["--validate", "--apply", "--yes", "--namespace", PROBE_NS],
     "--namespace", "--validate", [["--apply", "--yes", "--namespace", PROBE_NS]]),
    ("yes/validate+output-configmap", ["--validate", "--output-configmap", "--yes"],
     "--yes", "--validate", [["--validate", "--output-configmap"]]),
]


def _argv(base, tmp_path):
    return [str(tmp_path / "out.yaml") if a == "out" else a for a in base]


@pytest.mark.parametrize("cid,argv,flag,mode,prescriptions", _UNREAD,
                         ids=[c[0] for c in _UNREAD])
class TestUnreadFlagIsExit2:

    def test_exit_2_names_flag_and_mode(self, tenant_dir, tmp_path, cid, argv,
                                        flag, mode, prescriptions):
        r = _gar(["--config-dir", str(tenant_dir), *_argv(argv, tmp_path)])
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == EXIT_CALLER_ERROR, _text(r)[:500]
        assert f"{flag}" in err and GUARD in err, err
        assert f"in {mode} mode" in err, (
            f"the message must name the MODE the flag is unread in; got:\n{err}")
        assert not (tmp_path / "out.yaml").exists()
        # Refused before any scan: nothing about the tree was reported.
        assert b"Config files:" not in r.stdout, r.stdout

    def test_every_prescription_is_legal_and_not_this_error(
            self, tenant_dir, tmp_path, cid, argv, flag, mode, prescriptions):
        """Run what the message tells the operator to run. It may still fail
        for a reason of its own (`--apply` has no cluster here), but it must
        not be an argparse rejection and must not land on this guard."""
        for p in prescriptions:
            r = _gar(["--config-dir", str(tenant_dir), *_argv(p, tmp_path)])
            err = r.stderr.decode("utf-8", "replace")
            assert "error: argument" not in err and "not allowed with" not in err, (
                f"prescription {p} is not argparse-legal:\n{err}")
            assert GUARD not in err, (
                f"prescription {p} lands back on the guard:\n{err}")

    def test_fires_on_a_tree_that_yields_no_routes(
            self, routeless_dir, tmp_path, cid, argv, flag, mode, prescriptions):
        """#1616 lesson, pinned per cell: the check runs BEFORE the scan."""
        r = _gar(["--config-dir", str(routeless_dir), *_argv(argv, tmp_path)])
        assert r.returncode == EXIT_CALLER_ERROR, _text(r)[:500]
        assert GUARD in r.stderr.decode("utf-8", "replace")
        assert b"No tenants found" not in r.stdout


class TestRoutelessTreeControl:
    def test_no_routes_and_no_stray_flag_is_still_exit_0(self, routeless_dir):
        """Pairs with `test_fires_on_a_tree_that_yields_no_routes`: the tree
        itself must not be what exits 2."""
        r = _gar(["--config-dir", str(routeless_dir), "--validate"])
        assert r.returncode == EXIT_OK, _text(r)
        assert b"No tenants found" in r.stdout


# ── Controls: the mode that READS the flag honours it ───────────────
class TestReadingModeHonoursTheFlag:

    def test_o_in_render_writes_the_file(self, tenant_dir, tmp_path):
        out = tmp_path / "routes.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "-o", str(out)])
        assert r.returncode == EXIT_OK, _text(r)
        assert out.exists() and b"receivers" in out.read_bytes()

    def test_o_in_output_configmap_writes_the_file(self, tenant_dir, tmp_path):
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap", "-o", str(out)])
        assert r.returncode == EXIT_OK, _text(r)
        assert b"kind: ConfigMap" in out.read_bytes()

    def test_dry_run_in_render_previews(self, tenant_dir):
        r = _gar(["--config-dir", str(tenant_dir), "--dry-run"])
        assert r.returncode == EXIT_OK, _text(r)
        assert b"--- DRY RUN OUTPUT ---" in r.stdout

    def test_dry_run_in_output_configmap_previews(self, tenant_dir):
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap", "--dry-run"])
        assert r.returncode == EXIT_OK, _text(r)
        assert b"--- DRY RUN: ConfigMap YAML ---" in r.stdout

    def test_namespace_and_configmap_in_output_configmap_land_in_the_yaml(
            self, tenant_dir):
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "--namespace", PROBE_NS, "--configmap", PROBE_CM])
        assert r.returncode == EXIT_OK, _text(r)
        assert f"namespace: {PROBE_NS}".encode() in r.stdout, r.stdout[:600]
        assert f"name: {PROBE_CM}".encode() in r.stdout, r.stdout[:600]

    def test_defaults_still_apply_when_the_flags_are_omitted(self, tenant_dir):
        """`default=None` is what makes "supplied" decidable; the documented
        defaults must survive the resolution that follows the guard."""
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap"])
        assert r.returncode == EXIT_OK, _text(r)
        assert b"namespace: monitoring" in r.stdout
        assert b"name: alertmanager-config" in r.stdout

    def test_namespace_configmap_and_yes_in_apply_are_read(self, tenant_dir):
        """No cluster here, so --apply fails AFTER it has used the flags: the
        Target line carries the probe values, no prompt was attempted (--yes),
        and the failure is the cluster, not this guard."""
        r = _gar(["--config-dir", str(tenant_dir), "--apply", "--yes",
                  "--namespace", PROBE_NS, "--configmap", PROBE_CM])
        out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
        assert f"Target: {PROBE_NS}/{PROBE_CM}" in out, out
        assert "Proceed?" not in out
        assert GUARD not in err

    def test_yes_in_apply_is_what_stops_the_prompt(self, tenant_dir):
        """The control's other half: WITHOUT --yes the same invocation stops
        at the confirmation (#1617 shape), so --yes visibly has an effect."""
        r = _gar(["--config-dir", str(tenant_dir), "--apply",
                  "--namespace", PROBE_NS])
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == EXIT_CALLER_ERROR
        assert "interactive confirmation" in err and GUARD not in err, err


class TestSuppliedMeansNotDefault:
    def test_explicit_default_value_still_counts_as_supplied(self, tenant_dir):
        """`--namespace monitoring` under --validate is still a flag nothing
        reads; the predicate is "supplied", not "differs from the default" —
        a wrapper that always passes --namespace is exactly what the ticket
        asked to measure, and it is refused on purpose, with a remedy."""
        r = _gar(["--config-dir", str(tenant_dir), "--validate",
                  "--namespace", "monitoring"])
        assert r.returncode == EXIT_CALLER_ERROR
        assert "--namespace" in r.stderr.decode("utf-8", "replace")

    def test_all_unread_flags_are_reported_in_one_run(self, tenant_dir, tmp_path):
        r = _gar(["--config-dir", str(tenant_dir), "--validate", "--dry-run",
                  "-o", str(tmp_path / "x.yaml"), "--yes", "--namespace", PROBE_NS])
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == EXIT_CALLER_ERROR
        for flag in ("-o/--output", "--dry-run", "--yes", "--namespace"):
            assert f"{flag} is not read" in err, (flag, err)
