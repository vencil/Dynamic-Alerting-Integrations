#!/usr/bin/env python3
"""`configmap_assemble_precheck.py` + the `configmap-assemble` recipe (#1603).

Two properties have to hold together on this customer-run path, and each is
pinned below: the recipe ships every carrier the exporter's scanner reads,
and the pre-check refuses before `kubectl apply` when widening it would make
the exporter reject the ENTIRE config dir (same-stem `.yaml` + `.yml`).

⚠️ The recipe assertions read the shell out of the Makefile and RUN it, so
they cannot drift the way a second copy of the glob would. They deliberately
do not invoke `make` or `kubectl`: what is under test is which files the
selection picks.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRECHECK = REPO / "scripts" / "ops" / "configmap_assemble_precheck.py"
MAKEFILE = REPO / "Makefile"

_TENANT = "tenants:\n  {t}:\n    mysql_connections: 50\n"
_DEFAULTS = "defaults:\n  mysql_connections: 100\n"


def _tree(tmp_path: Path, names, *, tenant_of=None) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir(exist_ok=True)
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    for n in names:
        t = (tenant_of or {}).get(n, Path(n).stem)
        (d / n).write_text(_TENANT.format(t=t), encoding="utf-8")
    return d


def _run(config_dir) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(PRECHECK), "--config-dir", str(config_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )


# ── the recipe's own selection, extracted from the Makefile ───────────

def _recipe_selection_shell() -> str:
    """The `for f in ...` snippet the `configmap-assemble` recipe runs.

    Derived from the Makefile rather than re-typed: a copy here would let
    the recipe narrow again without any assertion noticing.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("\nconfigmap-assemble:")
    end = text.index("\nsharded-assemble:", start)
    body = text[start:end]
    m = re.search(r"\$\(shell (for f in .*?done)\)", body, re.S)
    assert m, "could not find the recipe's selection loop in the Makefile"
    # Makefile escaping: `$$` is a literal `$` to the shell.
    return m.group(1).replace("$$", "$").replace("$(CONFDIR)", '"$CONFDIR"')


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX sh; the recipe runs on Linux/macOS")
class TestRecipeSelection:
    """What `--from-file=` args the recipe produces for a given tree."""

    def _select(self, config_dir: Path) -> set[str]:
        out = subprocess.run(
            ["sh", "-c", _recipe_selection_shell()],
            capture_output=True, text=True,
            env={"CONFDIR": str(config_dir), "PATH": "/usr/bin:/bin"},
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        return {ln.split("=", 1)[1].split("=")[0]
                for ln in out.stdout.split() if ln.startswith("--from-file=")}

    def test_a_yml_carrier_reaches_the_configmap(self, tmp_path):
        """LOWER — this is the whole ticket, at the deployment path."""
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yml"])
        assert self._select(d) == {"_defaults.yaml", "db-a.yaml", "db-b.yml"}

    def test_non_config_extensions_stay_out(self, tmp_path):
        """UPPER — widened to the config suffixes, not to 'any file'."""
        d = _tree(tmp_path, ["db-a.yaml"])
        (d / "notes.txt").write_text("x", encoding="utf-8")
        (d / "db-c.json").write_text("{}", encoding="utf-8")
        (d / "x.yang").write_text("x", encoding="utf-8")
        assert self._select(d) == {"_defaults.yaml", "db-a.yaml"}

    def test_an_empty_dir_emits_no_args_rather_than_a_literal_glob(self, tmp_path):
        """The `[ -e ]` guard: an unmatched glob stays literal in POSIX sh.

        Without it the recipe would hand `kubectl` an argument containing a
        `*`, which is a confusing failure a long way from its cause.
        """
        d = tmp_path / "empty"
        d.mkdir()
        assert self._select(d) == set()


# ── the pre-check ────────────────────────────────────────────────────

class TestPrecheckRefusesTheFailureWideningArms:
    def test_same_stem_pair_is_refused(self, tmp_path):
        d = _tree(tmp_path, ["db-c.yaml", "db-c.yml"],
                  tenant_of={"db-c.yaml": "db-c", "db-c.yml": "db-c"})
        r = _run(d)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "tenant_uniqueness" in r.stderr
        # The operator has to be told the blast radius, not just the rule.
        assert "ENTIRE config dir" in r.stderr

    def test_the_two_spellings_alone_are_not_a_duplicate(self, tmp_path):
        """CONTROL — different stems in different spellings must pass.

        Without this the refusal above could be satisfied by a check that
        simply rejects any tree containing a `.yml`.
        """
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yml"])
        r = _run(d)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_repo_shipped_tree_passes(self):
        """CONTROL on the real tree this target is pointed at by default."""
        r = _run(REPO / "components" / "threshold-exporter" / "config" / "conf.d")
        assert r.returncode == 0, r.stdout + r.stderr


class TestPrecheckNeverTreatsCannotMeasureAsClean:
    def test_missing_dir_is_a_caller_error(self, tmp_path):
        r = _run(tmp_path / "nope")
        assert r.returncode == 2, r.stdout + r.stderr

    def test_empty_dir_is_refused_not_passed(self, tmp_path):
        """A ConfigMap with no tenant keys is indistinguishable from a
        platform that genuinely has none — the silent-zero this family is
        about, on the deployment path."""
        d = tmp_path / "empty"
        d.mkdir()
        r = _run(d)
        assert r.returncode == 1
        assert "no config carrier" in r.stderr

    def test_unparseable_validator_output_is_refused(self, tmp_path, monkeypatch):
        """If the check cannot be run, refuse — do not assume it passed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("precheck", PRECHECK)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["precheck_probe"] = mod
        spec.loader.exec_module(mod)

        d = _tree(tmp_path, ["db-a.yaml"])

        class _R:
            returncode = 0
            stdout = "not json at all"
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R())
        rc = mod.main(["--config-dir", str(d)])
        assert rc == 2

    def test_a_defaults_only_dir_is_allowed(self, tmp_path):
        """⛔ Deliberate, and the boundary a reviewer will ask about: the
        empty-dir arm is about the recipe's globs expanding to NOTHING, not
        about how many tenants a tree ought to have. A `_defaults.yaml`-only
        dir projects faithfully — the ConfigMap says what the tree says —
        so blocking it would be this gate adopting a content policy it
        explicitly declines to own."""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
        r = _run(d)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_hanging_validator_is_refused_not_waited_on(self, tmp_path, monkeypatch):
        """`make configmap-assemble` is customer-run: an unbounded wait there
        is a silent wedge with no output. The timeout has to land in the SAME
        arm as a parse failure — refuse, do not fall through to 'clean'."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("precheck4", PRECHECK)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["precheck_probe4"] = mod
        spec.loader.exec_module(mod)

        assert mod._VALIDATE_TIMEOUT_S > 0, "the hang guard must be bounded"
        d = _tree(tmp_path, ["db-a.yaml"])

        def _hang(*a, **k):
            assert k.get("timeout") == mod._VALIDATE_TIMEOUT_S, (
                "the call must pass its own bound, not rely on a default")
            raise subprocess.TimeoutExpired(cmd="validate_config", timeout=k["timeout"])

        monkeypatch.setattr(mod.subprocess, "run", _hang)
        assert mod.main(["--config-dir", str(d)]) == 2

    def test_a_missing_uniqueness_entry_is_refused(self, tmp_path, monkeypatch):
        """The gate names ONE check; if the validator stops reporting it,
        that is 'could not measure', not 'measured clean'."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("precheck2", PRECHECK)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["precheck_probe2"] = mod
        spec.loader.exec_module(mod)

        d = _tree(tmp_path, ["db-a.yaml"])

        class _R:
            returncode = 0
            stdout = json.dumps([{"check": "yaml_syntax", "status": "pass"}])
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R())
        rc = mod.main(["--config-dir", str(d)])
        assert rc == 2

    def test_other_checks_failing_do_not_block(self, tmp_path, monkeypatch):
        """⛔ Deliberately narrow: adopting the validator's whole verdict
        would turn any unrelated violation into 'cannot deploy'."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("precheck3", PRECHECK)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["precheck_probe3"] = mod
        spec.loader.exec_module(mod)

        d = _tree(tmp_path, ["db-a.yaml"])

        class _R:
            returncode = 1  # the validator failed overall …
            stdout = json.dumps([
                {"check": "schema", "status": "fail", "details": ["boom"]},
                {"check": "tenant_uniqueness", "status": "pass", "details": []},
            ])
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R())
        assert mod.main(["--config-dir", str(d)]) == 0  # … but not on our axis


class TestPrecheckNamesTheCaseResidue:
    def test_uppercase_carrier_is_named_not_dropped_silently(self, tmp_path):
        """Shell has no portable case-insensitive glob, so the recipe cannot
        ship `DB-U.YAML`. That residue is #1588's axis — the point here is
        only that it is LOUD."""
        d = _tree(tmp_path, ["db-a.yaml"])
        (d / "DB-U.YAML").write_text(_TENANT.format(t="DB-U"), encoding="utf-8")
        r = _run(d)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "DB-U.YAML" in r.stderr

    def test_no_warning_when_there_is_no_residue(self, tmp_path):
        """必響對照組 的反面：沒有殘留就不准出聲，否則上面那格沒有鑑別力。"""
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yml"])
        r = _run(d)
        assert r.returncode == 0
        assert "WARN" not in r.stderr
