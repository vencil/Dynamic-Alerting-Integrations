#!/usr/bin/env python3
"""`configmap_assemble.py` + the `configmap-assemble` recipe.

This customer-run path used to hold THREE independent enumerations of "which
files ship" — the script's own, and two `$(shell …)` glob loops on the recipe
— so the same question had three answers. It now holds one, and the script is
both the gate and the producer. Four properties are pinned below, each able to
go red on its own:

* the recipe delegates selection entirely (no glob came back);
* the exporter's own case-insensitive predicate decides what ships (#1792);
* a file name that cannot be a ConfigMap key is refused BY NAME, and nothing
  re-parses a file name on its way to `kubectl` (#1796);
* the repo's development sample tree is refused unless explicitly allowed
  (#1797);

plus the pre-existing one this file was written for: refuse before
`kubectl apply` when the artifact would make the exporter reject the ENTIRE
config dir (same-stem `.yaml` + `.yml`).

⚠️ `kubectl` is NOT on the CI runners (only `nightly-image-scan.yaml` and
`image-ref-resolve.yaml` mention it), so the oracle for the producer half is a
PATH shim that dumps its own `sys.argv` as JSON. That is also the BETTER
oracle: it can assert that each `--from-file=key=path` arrived as one argv
element, byte for byte, which a real `kubectl` would silently absorb.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ops" / "configmap_assemble.py"
MAKEFILE = REPO / "Makefile"
SAMPLE_TREE = REPO / "components" / "threshold-exporter" / "config" / "conf.d"

sys.path.insert(0, str(REPO / "scripts" / "tools"))
import _lib_tenant_uniqueness as tu  # noqa: E402

# ⛔ The "could not measure" arms below patch `subprocess.run` on the stdlib
# module object, which is what `_lib_tenant_uniqueness` calls. #1794 moved the
# measurement out of this script into that shared module (the sibling producer
# `assemble_config_dir` has to ask the identical question).
# ⚠️ The script itself calls `subprocess.run` again now, for `kubectl`, so a
# blanket patch reaches BOTH calls. Every arm below either returns before the
# kubectl call or dispatches on argv — see `_fake_run`.

_TENANT = "tenants:\n  {t}:\n    mysql_connections: 50\n"
_DEFAULTS = "defaults:\n  mysql_connections: 100\n"

# A shim that records exactly what argv it was handed and prints a plausible
# manifest. `KUBECTL_SHIM_FAIL` turns it into a failing kubectl.
_SHIM = """#!{python}
import json, os, sys
dump = os.environ.get("KUBECTL_SHIM_ARGV")
if dump:
    with open(dump, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh)
fail = os.environ.get("KUBECTL_SHIM_FAIL")
if fail:
    sys.stderr.write(fail + "\\n")
    sys.exit(3)
sys.stdout.write("apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: stub\\n")
"""


@pytest.fixture(scope="session")
def kubectl_shim(tmp_path_factory) -> Path:
    """A directory holding a fake `kubectl`, to be prepended to `PATH`."""
    if sys.platform == "win32":  # pragma: no cover - CI runs POSIX
        pytest.skip("the shim relies on a POSIX shebang")
    d = tmp_path_factory.mktemp("kubectl-shim")
    exe = d / "kubectl"
    exe.write_text(_SHIM.format(python=sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    return d


def _tree(tmp_path: Path, names, *, tenant_of=None, at="conf.d") -> Path:
    d = tmp_path / at
    d.mkdir(exist_ok=True, parents=True)
    (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
    for n in names:
        t = (tenant_of or {}).get(n, Path(n).stem)
        (d / n).write_text(_TENANT.format(t=t), encoding="utf-8")
    return d


def _out(tmp_path: Path) -> Path:
    """An output path under the test's own tmp dir, NOT the repo's `.build`."""
    return tmp_path / "build" / "threshold-config.yaml"


def _run(config_dir, output, *, shim: Path | None = None,
         env_extra: dict | None = None, path: str | None = None,
         extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Never inherit the opt-in: #1797's refusal has to be measurable.
    env.pop("ALLOW_SAMPLE_CONFDIR", None)
    if path is not None:
        env["PATH"] = path
    if shim is not None:
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT),
         "--config-dir", str(config_dir), "--output", str(output),
         *(extra_args or [])],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=300,
    )


def _import_script(probe: str):
    """Load the script as a module, for the in-process monkeypatch arms."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(probe, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[probe] = mod
    spec.loader.exec_module(mod)
    return mod


# ── the recipe: it must not have grown a selection of its own ────────

def _recipe_body() -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("\nconfigmap-assemble:")
    end = text.index("\nsharded-assemble:", start)
    return text[start:end]


class TestTheRecipeIsInertToMakeAndToTheShell:
    """The successor to the old `TestRecipeSelection`, and to the four-string
    blocklist that replaced it.

    ⛔ That blocklist (`$(shell`, `*.yaml`, `*.yml`, `basename`) was an
    ENUMERATION OF SPELLINGS, and blind review walked through it: rebuilding
    the producer out of `$(foreach …)` + `$(wildcard …)` + `$(notdir …)`
    brought #1792 and #1796 both back with all 43 tests green. Adding those
    three words would have been the second version of the same mistake.

    The two properties below are DERIVED instead, from the two layers that
    can each touch a file name here, and between them nothing is enumerated:

    * make — the recipe needs no make expansion at all, so `$(`/`${` may not
      appear in it. Every make-level producer needs one, whatever it spells
      itself. ⛔ Comment lines are INSIDE this, not excluded from it: make
      expands a recipe line before `#` ever reaches the shell, so a function
      call in a comment RUNS. (Blind review measured that too: a `@#` line
      holding `$(shell touch …)` created the file during `make -n`, and the
      old test excluded `@#` lines by construction.)
    * the shell — the expansion is exactly ONE command and it is the script
      call, so there is no second command in which a glob, a `kubectl` or a
      `basename` could live.
    """

    def test_the_recipe_calls_the_script(self):
        """必響對照組: without this both properties below are vacuous — an
        empty recipe has no `$(` and no extra command either."""
        body = _recipe_body()
        assert "scripts/ops/configmap_assemble.py" in body, body
        assert "--config-dir" in body, body

    def test_the_recipe_needs_no_make_expansion_not_even_in_a_comment(self):
        """make layer. `$(CONFDIR)` used to be here; it is `"$$CONFDIR"` now
        (a SHELL variable read from the environment), which leaves this
        recipe with nothing for make to expand — so any `$(` is either a
        producer or a comment that is about to run."""
        body = _recipe_body()
        offenders = [ln for ln in body.splitlines()
                     if ln.startswith("\t") and ("$(" in ln or "${" in ln)]
        assert not offenders, (
            "a make expansion in this recipe (comments included — make "
            "expands them before `#` reaches the shell, so they RUN). If a "
            "make variable is genuinely needed, `export` it and read it as "
            f"`$$NAME`:\n" + "\n".join(offenders))

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                        reason="needs GNU make to expand the recipe")
    def test_the_expansion_is_exactly_the_script_call_and_nothing_else(self):
        """shell layer, asked of make's OWN expansion rather than of the
        source text: whatever the recipe is written as, this is what runs.

        ⚠️ `make -n` strips the `@` / `-` prefixes, so "can the gate fail the
        target" is NOT answerable here — `TestTheGateCanActuallyFailTheTarget`
        runs the real thing for that.
        """
        r = subprocess.run(["make", "-n", "configmap-assemble"], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        # Join the `\`-continuations make prints verbatim, then drop the
        # comment lines (they are the recipe's own prose; the test above is
        # what keeps them inert).
        text = r.stdout.replace("\\\n", " ")
        commands = [" ".join(ln.split()) for ln in text.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
        assert commands == [
            'python3 ./scripts/ops/configmap_assemble.py '
            '--config-dir "$CONFDIR" --output .build/threshold-config.yaml'
        ], r.stdout

    def test_the_recipe_writes_the_scripts_documented_default(self):
        """The two must not drift: the docs and the `--help` promise
        `.build/threshold-config.yaml`, and the recipe is what actually runs."""
        mod = _import_script("cma_default_probe")
        assert mod.DEFAULT_OUTPUT == REPO / ".build" / "threshold-config.yaml"
        assert ".build/threshold-config.yaml" in _recipe_body()

    def test_the_makefile_default_confdir_is_the_sample_tree(self):
        """#1797 in one line: the built-in default IS the tree the script
        refuses. If someone repoints `CONFDIR`, the refusal stops being the
        thing that protects this path and this test says so."""
        m = re.search(r"^CONFDIR := (.+)$", MAKEFILE.read_text(encoding="utf-8"),
                      re.M)
        assert m, "CONFDIR assignment not found"
        assert (REPO / m.group(1).strip()).resolve() == SAMPLE_TREE.resolve()


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                    reason="needs GNU make + POSIX sh; the recipe runs on Linux/macOS")
class TestTheGateCanActuallyFailTheTarget:
    """⛔ The load-bearing wire: the gate must be able to STOP the target.

    Everything else here tests the halves separately — what the script does,
    and what the recipe looks like. Neither notices if the script stops being
    able to fail `make configmap-assemble` (prefix the line with `-`, append
    `|| true`…). Blind review measured that hole: with `-@python3 …` in place
    of `@python3 …` the whole module stayed green.
    """

    def test_a_same_stem_pair_stops_before_the_artifact_is_written(self, tmp_path):
        d = _tree(tmp_path, ["db-c.yaml", "db-c.yml"],
                  tenant_of={"db-c.yaml": "db-c", "db-c.yml": "db-c"})
        artifact = REPO / ".build" / "threshold-config.yaml"
        before = artifact.stat().st_mtime_ns if artifact.exists() else None

        r = subprocess.run(["make", "configmap-assemble", f"CONFDIR={d}"],
                           cwd=REPO, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)

        assert r.returncode != 0, (
            "the recipe ran to completion on a tree the exporter would reject "
            f"in full\nstdout: {r.stdout}\nstderr: {r.stderr}")
        assert "tenant_uniqueness" in r.stderr, r.stderr
        after = artifact.stat().st_mtime_ns if artifact.exists() else None
        assert after == before, "the target wrote its artifact anyway"


# ── the gate ─────────────────────────────────────────────────────────

class TestRefusesTheFailureWideningArms:
    def test_same_stem_pair_is_refused(self, tmp_path):
        d = _tree(tmp_path, ["db-c.yaml", "db-c.yml"],
                  tenant_of={"db-c.yaml": "db-c", "db-c.yml": "db-c"})
        r = _run(d, _out(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "tenant_uniqueness" in r.stderr
        # The operator has to be told the blast radius, not just the rule.
        assert "ENTIRE config dir" in r.stderr

    def test_the_two_spellings_alone_are_not_a_duplicate(self, tmp_path, kubectl_shim):
        """CONTROL — different stems in different spellings must pass.

        Without this the refusal above could be satisfied by a check that
        simply rejects any tree containing a `.yml`.
        """
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_the_repo_sample_tree_is_refused_without_the_opt_in(self, tmp_path):
        """#1797. The successor to `test_repo_shipped_tree_passes`: that tree
        is still legal (see the CONTROL below), but "legal" was never the
        question — `kubectl apply` of an artifact built from it replaces the
        live ConfigMap with `db-a` / `db-b`."""
        r = _run(SAMPLE_TREE, _out(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "SAMPLE" in r.stderr, r.stderr
        assert "ALLOW_SAMPLE_CONFDIR=1" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_the_repo_sample_tree_is_legal_when_explicitly_allowed(
            self, tmp_path, kubectl_shim):
        """CONTROL on the real tree, and the half the refusal above must not
        be allowed to hide: the block is about INTENT, not about the tree
        being malformed. Without this arm the refusal would also be satisfied
        by the sample tree having gone invalid."""
        r = _run(SAMPLE_TREE, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"ALLOW_SAMPLE_CONFDIR": "1"})
        assert r.returncode == 0, r.stdout + r.stderr
        assert _out(tmp_path).exists()


class TestTheSampleTreeBlockCannotBeWalkedAround:
    """#1797, the paths an operator reaches the same directory by."""

    def test_a_symlink_to_the_sample_tree_is_refused(self, tmp_path):
        link = tmp_path / "conf.d"
        try:
            link.symlink_to(SAMPLE_TREE, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("this filesystem/user cannot create symlinks")
        r = _run(link, _out(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "SAMPLE" in r.stderr, r.stderr

    def test_a_dot_prefixed_relative_path_is_refused(self, tmp_path):
        rel = "./" + SAMPLE_TREE.relative_to(REPO).as_posix() + "/."
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT),
             "--config-dir", rel, "--output", str(_out(tmp_path))],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300,
            env={k: v for k, v in os.environ.items()
                 if k != "ALLOW_SAMPLE_CONFDIR"},
        )
        assert r.returncode == 1, r.stdout + r.stderr
        assert "SAMPLE" in r.stderr, r.stderr

    def test_a_customers_own_tree_is_unaffected(self, tmp_path, kubectl_shim):
        """必響對照組: the block must key on THAT directory, not on "a tree
        that happens to hold a tenant called db-a"."""
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_subdirectory_of_the_sample_tree_is_refused(self, tmp_path):
        """⛔ The question is "is this the repo's sample material", and path
        EQUALITY is only one spelling of it. `examples/` sits inside that
        very tree, holds the same demonstration tenants, and assembled seven
        of them with rc 0 — a `kubectl apply` away from replacing a live
        ConfigMap with reference templates, which is exactly what #1797 is."""
        sub = SAMPLE_TREE / "examples"
        assert sub.is_dir(), f"{sub} is the fixture; it must exist in-repo"
        r = _run(sub, _out(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "SAMPLE" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_a_tree_outside_the_repo_ending_in_the_same_names_is_not(
            self, tmp_path, kubectl_shim):
        """誤紅對照組 for the widening above: containment is on the RESOLVED
        path, so a customer whose own tree happens to end in the same
        components must still build. Without this arm the block could be
        satisfied by matching on the path's tail."""
        mirror = tmp_path / "components" / "threshold-exporter" / "config"
        d = _tree(mirror, ["db-a.yaml"], at="conf.d")
        assert d.as_posix().endswith(
            SAMPLE_TREE.relative_to(REPO).as_posix()), d
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr


class TestNeverTreatsCannotMeasureAsClean:
    def test_missing_dir_is_a_caller_error(self, tmp_path):
        r = _run(tmp_path / "nope", _out(tmp_path))
        assert r.returncode == 2, r.stdout + r.stderr

    def test_empty_dir_is_refused_not_passed(self, tmp_path):
        """A ConfigMap with no tenant keys is indistinguishable from a
        platform that genuinely has none — the silent-zero this family is
        about, on the deployment path."""
        d = tmp_path / "empty"
        d.mkdir()
        r = _run(d, _out(tmp_path))
        assert r.returncode == 1
        assert "no config carrier" in r.stderr

    def test_unparseable_validator_output_is_refused(self, tmp_path, monkeypatch):
        """If the check cannot be run, refuse — do not assume it passed."""
        mod = _import_script("cma_probe1")
        d = _tree(tmp_path, ["db-a.yaml"])

        class _R:
            returncode = 0
            stdout = "not json at all"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        rc = mod.main(["--config-dir", str(d), "--output", str(_out(tmp_path))])
        assert rc == 2

    def test_a_defaults_only_dir_is_allowed(self, tmp_path, kubectl_shim):
        """⛔ Deliberate, and the boundary a reviewer will ask about: the
        empty-dir arm is about the selection finding NOTHING, not about how
        many tenants a tree ought to have. A `_defaults.yaml`-only dir
        projects faithfully — the ConfigMap says what the tree says — so
        blocking it would be this gate adopting a content policy it
        explicitly declines to own."""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_non_pass_verdict_is_refused_not_read_as_clean(self, tmp_path, monkeypatch):
        """`tenant_uniqueness` also returns `warn`, whose own details say it is
        a limit on what was checked. Only an affirmative `pass` is a pass."""
        mod = _import_script("cma_probe5")
        d = _tree(tmp_path, ["db-a.yaml"])

        class _R:
            returncode = 0
            stdout = json.dumps([{
                "check": "tenant_uniqueness", "status": "warn",
                "details": ["1 file(s) could not be read and were not examined"],
                "suggested_action": "fix the unreadable file(s) and re-run",
            }])
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        assert mod.main(["--config-dir", str(d),
                         "--output", str(_out(tmp_path))]) == 2

    def test_a_dotfile_is_not_a_carrier(self, tmp_path):
        """The exporter skips `.`-prefixed entries, so a CI config sitting in
        a mis-pointed `--config-dir` is not a carrier. Counting it made the
        empty-dir guard pass for exactly the mis-pointing that guard exists
        for."""
        d = tmp_path / "repo-root"
        d.mkdir()
        (d / ".gitlab-ci.yml").write_text("stages: []\n", encoding="utf-8")
        (d / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
        r = _run(d, _out(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "no config carrier" in r.stderr

    def test_a_hanging_validator_is_refused_not_waited_on(self, tmp_path, monkeypatch):
        """`make configmap-assemble` is customer-run: an unbounded wait there
        is a silent wedge with no output. The timeout has to land in the SAME
        arm as a parse failure — refuse, do not fall through to 'clean'."""
        mod = _import_script("cma_probe4")
        assert tu._VALIDATE_TIMEOUT_S > 0, "the hang guard must be bounded"
        d = _tree(tmp_path, ["db-a.yaml"])

        def _hang(*a, **k):
            assert k.get("timeout") == tu._VALIDATE_TIMEOUT_S, (
                "the call must pass its own bound, not rely on a default")
            raise subprocess.TimeoutExpired(cmd="validate_config", timeout=k["timeout"])

        monkeypatch.setattr(subprocess, "run", _hang)
        assert mod.main(["--config-dir", str(d),
                         "--output", str(_out(tmp_path))]) == 2

    def test_a_missing_uniqueness_entry_is_refused(self, tmp_path, monkeypatch):
        """The gate names ONE check; if the validator stops reporting it,
        that is 'could not measure', not 'measured clean'."""
        mod = _import_script("cma_probe2")
        d = _tree(tmp_path, ["db-a.yaml"])

        class _R:
            returncode = 0
            stdout = json.dumps([{"check": "yaml_syntax", "status": "pass"}])
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        rc = mod.main(["--config-dir", str(d), "--output", str(_out(tmp_path))])
        assert rc == 2

    def test_other_checks_failing_do_not_block(self, tmp_path, monkeypatch):
        """⛔ Deliberately narrow: adopting the validator's whole verdict
        would turn any unrelated violation into 'cannot deploy'."""
        mod = _import_script("cma_probe3")
        d = _tree(tmp_path, ["db-a.yaml"])
        validator = json.dumps([
            {"check": "schema", "status": "fail", "details": ["boom"]},
            {"check": "tenant_uniqueness", "status": "pass", "details": []},
        ])

        class _Validator:
            returncode = 1  # the validator failed overall …
            stdout = validator
            stderr = ""

        class _Kubectl:
            returncode = 0
            stdout = "apiVersion: v1\nkind: ConfigMap\n"
            stderr = ""

        def _fake_run(cmd, *a, **k):
            # ⚠️ Dispatch, because the script now shells out twice: patching
            # `subprocess.run` blanket-style would otherwise hand the
            # validator's JSON to the kubectl call as well.
            return _Kubectl() if cmd[0] == "kubectl" else _Validator()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert mod.main(["--config-dir", str(d),
                         "--output", str(_out(tmp_path))]) == 0  # … not our axis


class TestSpeaksForTheArtifactNotTheTree:
    """The ConfigMap key plane is flat. Both halves of that are pinned here."""

    def test_a_nested_duplicate_does_not_block_a_shippable_artifact(
            self, tmp_path, kubectl_shim):
        """A second declaration under a subdirectory never reaches the
        ConfigMap through this path, so refusing the build over it blocks a
        deployable artifact — and tells the operator something untrue about
        what the exporter would do with the thing being built."""
        d = _tree(tmp_path, ["db-a.yaml"])
        sub = d / "examples"
        sub.mkdir()
        (sub / "db-a.yaml").write_text(_TENANT.format(t="db-a"), encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_the_subtree_that_will_not_ship_is_named(self, tmp_path, kubectl_shim):
        """The other half: flat selection is only honest if it says so."""
        d = _tree(tmp_path, ["db-a.yaml"])
        sub = d / "region-eu"
        sub.mkdir()
        (sub / "db-b.yaml").write_text(_TENANT.format(t="db-b"), encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "db-b.yaml" in r.stderr, r.stderr

    def test_a_flat_tree_says_nothing_about_nesting(self, tmp_path, kubectl_shim):
        """必響對照組: without this the assertion above has no discrimination."""
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0
        assert "region-eu" not in r.stderr
        assert "WARN" not in r.stderr


# ── #1792: the case axis, now on the producer ────────────────────────

def _from_file_args(argv: list[str]) -> list[str]:
    return [a for a in argv if a.startswith("--from-file=")]


def _keys(argv: list[str]) -> set[str]:
    return {a[len("--from-file="):].split("=", 1)[0] for a in _from_file_args(argv)}


def _argv_of(shim_dump: Path) -> list[str]:
    return json.loads(shim_dump.read_text(encoding="utf-8"))


class TestSelectionFollowsTheExportersOwnPredicate:
    def test_an_uppercase_carrier_reaches_the_configmap(self, tmp_path, kubectl_shim):
        """#1792, the whole ticket. The exporter's scanner accepts
        `DB-A.YAML`; the recipe's shell glob matched with case-SENSITIVE
        fnmatch, so that tenant was dropped on EVERY platform."""
        dump = tmp_path / "argv.json"
        d = _tree(tmp_path, ["db-a.yaml", "DB-U.YAML"],
                  tenant_of={"DB-U.YAML": "db-u"})
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)})
        assert r.returncode == 0, r.stdout + r.stderr
        assert _keys(_argv_of(dump)) == {"_defaults.yaml", "db-a.yaml",
                                         "DB-U.YAML"}

    def test_non_config_extensions_stay_out(self, tmp_path, kubectl_shim):
        """UPPER — widened to the config suffixes in ANY casing, not to
        'any file'."""
        dump = tmp_path / "argv.json"
        d = _tree(tmp_path, ["db-a.yaml"])
        (d / "notes.txt").write_text("x", encoding="utf-8")
        (d / "db-c.json").write_text("{}", encoding="utf-8")
        (d / "x.yang").write_text("x", encoding="utf-8")
        (d / "README.MD").write_text("# x\n", encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)})
        assert r.returncode == 0, r.stdout + r.stderr
        assert _keys(_argv_of(dump)) == {"_defaults.yaml", "db-a.yaml"}

    def test_a_hidden_backup_does_not_reach_the_configmap(self, tmp_path, kubectl_shim):
        """The exporter skips `.`-prefixed entries, so an editor backup is not
        something it reads — and widening the casing must not smuggle one in
        as a key (it would shadow nothing, but it WOULD ship a stale tenant)."""
        dump = tmp_path / "argv.json"
        d = _tree(tmp_path, ["db-a.yaml"])
        (d / ".db-a.YAML.bak.YAML").write_text(_TENANT.format(t="stale"),
                                               encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)})
        assert r.returncode == 0, r.stdout + r.stderr
        assert _keys(_argv_of(dump)) == {"_defaults.yaml", "db-a.yaml"}

    def test_two_casings_of_one_tenant_are_now_refused(self, tmp_path):
        """⛔ BEHAVIOUR CHANGE, pinned deliberately. `db-a.yaml` +
        `DB-A.YAML` both declaring `db-a` used to be green: the glob shipped
        only the lowercase one and the gate only ever looked at what the glob
        shipped. Both ship now, so the exporter would reject the ENTIRE dir —
        and the gate says so instead of the deploy discovering it."""
        d = _tree(tmp_path, ["db-a.yaml", "DB-A.YAML"],
                  tenant_of={"db-a.yaml": "db-a", "DB-A.YAML": "db-a"})
        r = _run(d, _out(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "tenant_uniqueness" in r.stderr
        assert not _out(tmp_path).exists()


# ── #1796: names that cannot be keys, and argv that is not re-parsed ─

class TestFileNamesThatCannotBeConfigMapKeys:
    """⛔ Refuse BY NAME. The two wrong answers are both worse: dropping the
    file loses a tenant behind a green light, and handing it to `kubectl`
    produces a message that never says which file."""

    @pytest.mark.parametrize("name,fragment", [
        ("db b.yaml", "' '"),
        ("db-a (copy).yaml", "'('"),
    ])
    def test_a_name_that_can_never_be_a_key_is_named(self, tmp_path, name,
                                                     fragment, kubectl_shim):
        d = _tree(tmp_path, ["db-a.yaml"])
        (d / name).write_text(_TENANT.format(t="db-x"), encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 1, r.stdout + r.stderr
        assert name in r.stderr, r.stderr
        assert fragment in r.stderr, r.stderr
        assert "[-._a-zA-Z0-9]+" in r.stderr, r.stderr
        assert not _out(tmp_path).exists(), "the artifact was written anyway"

    def test_shell_metacharacters_are_shown_verbatim_not_evaluated(
            self, tmp_path, kubectl_shim):
        """The other failure mode of the old recipe: make pasted `$(shell …)`
        output back for the shell to parse, so `$(id)` and backticks were
        EVALUATED. Here they must survive as bytes, inside a refusal that
        names the file."""
        d = _tree(tmp_path, ["db-a.yaml"])
        hostile = "db-$(id)-`id`.yaml"
        (d / hostile).write_text(_TENANT.format(t="db-x"), encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 1, r.stdout + r.stderr
        assert hostile in r.stderr, r.stderr
        assert "uid=" not in r.stdout + r.stderr, "the name was evaluated"
        assert not _out(tmp_path).exists()

    def test_a_legal_name_with_dots_and_dashes_is_not_refused(
            self, tmp_path, kubectl_shim):
        """必響對照組: the rule is k8s's `[-._a-zA-Z0-9]+`, not "looks
        simple". Without this the refusal above is satisfied by a check that
        rejects anything unusual."""
        d = _tree(tmp_path, ["db-a.v2.legacy_1.yaml"],
                  tenant_of={"db-a.v2.legacy_1.yaml": "db-a"})
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_the_rule_is_transcribed_from_the_k8s_authority(self):
        """The predicate is a copy of someone else's definition, so pin it
        against that definition rather than against examples."""
        mod = _import_script("cma_key_probe")
        assert mod.configmap_key_problem("db-a.yaml") is None
        assert mod.configmap_key_problem("_defaults.yml") is None
        assert mod.configmap_key_problem("A-1_2.3") is None
        assert mod.configmap_key_problem("..") is not None
        assert mod.configmap_key_problem(".") is not None
        assert mod.configmap_key_problem("..hidden.yaml") is not None
        assert mod.configmap_key_problem("a/b.yaml") is not None
        # `len()` in Go counts BYTES, so the bound is on the UTF-8 encoding.
        assert mod.configmap_key_problem("a" * 253) is None
        assert mod.configmap_key_problem("a" * 254) is not None

    def test_the_bound_is_bytes_not_characters(self):
        """⛔ The byte-vs-character axis had NOTHING on it: blind review
        replaced `len(name.encode(...))` with `len(name)` and all 44 tests
        stayed green, because every fixture was ASCII where the two agree.
        Go's `len()` counts bytes, so a name the API server refuses at 257
        bytes is only 131 characters long."""
        mod = _import_script("cma_key_bytes_probe")
        multibyte = "å" * 126 + ".yaml"
        assert len(multibyte) == 131 and len(multibyte.encode("utf-8")) == 257
        why = mod.configmap_key_problem(multibyte)
        assert why is not None and "bytes" in why, why

    def test_the_end_anchor_is_end_of_text_not_end_of_line(self):
        """Python's `$` also matches just BEFORE a trailing newline, so
        transcribing Go's `$` as `$` copied the character and lost the
        meaning: `db-a.yaml\\n` was accepted as a key."""
        mod = _import_script("cma_key_anchor_probe")
        assert mod.configmap_key_problem("db-a.yaml\n") is not None

    def test_a_name_the_filesystem_can_hold_but_utf8_cannot(self, tmp_path,
                                                            kubectl_shim):
        """⛔ The refusal has to REACH the operator. The byte-length check ran
        first and encoded the name to UTF-8, so a file whose name is not
        valid UTF-8 — ordinary on POSIX, where a name is bytes — ended the
        run in a `UnicodeEncodeError` traceback instead of the by-name
        refusal this whole class is about. `os.fsencode` counts the same
        bytes without raising."""
        d = _tree(tmp_path, ["db-a.yaml"])
        try:
            with open(os.path.join(os.fsencode(str(d)), b"db-\xff.yaml"),
                      "wb") as fh:
                fh.write(b"tenants: {}\n")
        except (OSError, ValueError):  # pragma: no cover - exotic filesystem
            pytest.skip("this filesystem will not hold a non-UTF-8 name")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "Traceback" not in r.stderr, r.stderr
        assert "UnicodeEncodeError" not in r.stderr, r.stderr
        assert "udcff" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()


class TestTheFromFileSourceKubectlHasToParse:
    """#1796's other half: the FILE NAME was checked, the source string built
    out of it was not."""

    def test_an_equals_sign_in_the_config_dir_is_named(self, tmp_path,
                                                       kubectl_shim):
        """`env=prod/`, and a Jenkins matrix axis directory (`axis=value/`),
        are ordinary directory names. `--from-file=key=path` then carries two
        `=`, which `ParseFileSource` refuses with "key names or file paths
        cannot contain '='" — an accusation against the FILE NAMES, which are
        innocent, so the operator goes looking in the wrong place."""
        d = _tree(tmp_path / "env=prod", ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "env=prod" in r.stderr, r.stderr
        assert "--from-file" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_the_transcription_matches_parsefilesource(self):
        """Pinned against kubectl's rule (count the `=`), not against the two
        directory names above."""
        mod = _import_script("cma_source_probe")
        assert mod.from_file_source_problem("db-a.yaml=/srv/db-a.yaml") is None
        assert mod.from_file_source_problem("db-a.yaml=/s=v/db-a.yaml") is not None

    def test_an_ordinary_path_still_builds(self, tmp_path, kubectl_shim):
        """必響對照組: without this the refusal above is satisfied by a check
        that rejects every `--from-file` argument."""
        d = _tree(tmp_path / "env-prod", ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr


class TestAConfigNamedEntryNothingCanBeReadFromIsNamed:
    """`is_file()` is a SILENT filter, and this module's own docstring calls
    dropping a carrier silently "how a tenant disappears with a green light"."""

    def test_a_broken_symlink_and_a_config_named_directory_are_named(
            self, tmp_path, kubectl_shim):
        d = _tree(tmp_path, ["db-a.yaml"])
        try:
            (d / "db-gone.yaml").symlink_to(tmp_path / "nowhere.yaml")
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("this filesystem/user cannot create symlinks")
        (d / "db-dir.yaml").mkdir()
        dump = tmp_path / "argv.json"
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)})
        # Non-blocking on purpose: a tree that deploys today must keep
        # deploying. What may not happen is silence.
        assert r.returncode == 0, r.stdout + r.stderr
        assert "db-gone.yaml" in r.stderr, r.stderr
        assert "db-dir.yaml" in r.stderr, r.stderr
        assert "broken symlink" in r.stderr, r.stderr
        # …and they really are absent from the artifact, which is why being
        # silent about them was the defect.
        assert _keys(_argv_of(dump)) == {"_defaults.yaml", "db-a.yaml"}

    def test_a_clean_tree_says_nothing(self, tmp_path, kubectl_shim):
        """必響對照組: without this the WARN above could be unconditional."""
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "WARN" not in r.stderr, r.stderr


class TestAnArtifactTooLargeToBeAConfigMap:
    """The green light this gate could still hand out: `kubectl create
    --dry-run=client` does not apply the size rule, so the refusal landed at
    `kubectl apply`, on the whole object, naming no file."""

    def test_carriers_over_the_data_limit_are_refused_and_the_largest_named(
            self, tmp_path, kubectl_shim):
        mod = _import_script("cma_size_probe")
        limit = mod._MAX_CONFIGMAP_BYTES
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(_DEFAULTS, encoding="utf-8")
        body = _TENANT.format(t="db-a") + "#" * (limit // 2)
        for n in ("db-a.yaml", "db-b.yaml", "db-c.yaml"):
            (d / n).write_text(body.replace("db-a", Path(n).stem),
                               encoding="utf-8")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 1, r.stdout + r.stderr
        assert str(limit) in r.stderr, r.stderr
        assert "db-a.yaml" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_the_limit_is_the_k8s_one_and_a_tree_under_it_still_builds(
            self, tmp_path, kubectl_shim):
        """必響對照組 + the pin: 1 MiB is `core.MaxSecretSize`, the bound
        `ValidateConfigMap` applies to the SUM of the `data` values. A tree
        just under it must build, or the gate is a tenant-count policy."""
        mod = _import_script("cma_size_probe2")
        limit = mod._MAX_CONFIGMAP_BYTES
        assert limit == 1024 * 1024
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "db-a.yaml").write_text(
            _TENANT.format(t="db-a") + "#" * (limit - 200), encoding="utf-8")
        assert sum(p.stat().st_size for p in d.iterdir()) < limit
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim")
class TestArgvReachesKubectlUnparsed:
    """#1796's core. A real `kubectl` cannot answer this — it would absorb a
    split argument silently — so the oracle is a shim that reports its argv."""

    def test_every_from_file_arg_is_one_argv_element(self, tmp_path, kubectl_shim):
        """The directory holds a space AND parens, which is what killed the
        old recipe: `$(shell …)` output was pasted back unquoted, so this
        path became three words and `basename` ate the extension."""
        dump = tmp_path / "argv.json"
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yml"],
                  at="my confs (v2)/conf.d")
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)})
        assert r.returncode == 0, r.stdout + r.stderr

        argv = _argv_of(dump)
        args = _from_file_args(argv)
        on_disk = sorted(p for p in d.iterdir() if p.is_file())
        assert len(args) == len(on_disk) == 3, argv

        seen = {}
        for a in args:
            key, path = a[len("--from-file="):].split("=", 1)
            seen[key] = path
        # Byte for byte against what is actually on disk — the key keeps its
        # extension and the path keeps its spaces and parens.
        assert seen == {p.name: str(p) for p in on_disk}, argv
        assert " " in str(d) and "(" in str(d), "the fixture lost its point"

    def test_the_rest_of_the_command_line_is_what_the_recipe_used_to_write(
            self, tmp_path, kubectl_shim):
        dump = tmp_path / "argv.json"
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)})
        assert r.returncode == 0, r.stdout + r.stderr
        argv = _argv_of(dump)[1:]  # drop argv[0], the shim's own path
        fixed = [a for a in argv if not a.startswith("--from-file=")]
        assert fixed == ["create", "configmap", "threshold-config",
                         "-n", "monitoring", "--dry-run=client", "-o", "yaml"]

    def test_name_and_namespace_are_overridable(self, tmp_path, kubectl_shim):
        dump = tmp_path / "argv.json"
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ARGV": str(dump)},
                 extra_args=["--namespace", "obs", "--name", "thr-cfg"])
        assert r.returncode == 0, r.stdout + r.stderr
        argv = _argv_of(dump)
        assert "thr-cfg" in argv and "obs" in argv, argv

    def test_the_artifact_is_kubectls_own_stdout(self, tmp_path, kubectl_shim):
        d = _tree(tmp_path, ["db-a.yaml"])
        out = _out(tmp_path)
        r = _run(d, out, shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert out.read_text(encoding="utf-8").startswith("apiVersion: v1")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim")
class TestKubectlIsAHardPrerequisite:
    def test_a_missing_kubectl_is_a_caller_error_not_a_crash(self, tmp_path):
        """⛔ Not a silent skip: without kubectl there IS no artifact, and a
        deploy step that reports success without producing one is the shape
        this whole file is about."""
        empty = tmp_path / "nopath"
        empty.mkdir()
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), path=str(empty))
        assert r.returncode == 2, r.stdout + r.stderr
        assert "kubectl" in r.stderr
        assert not _out(tmp_path).exists()

    def test_a_failing_kubectl_is_forwarded_and_leaves_no_artifact(
            self, tmp_path, kubectl_shim):
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_FAIL": "error: something kubectl said"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "something kubectl said" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_a_previous_artifact_survives_a_failure_intact(self, tmp_path,
                                                           kubectl_shim):
        """⛔ Renamed from `test_a_stale_artifact_is_not_left_looking_fresh`,
        which said the opposite of what it asserts. Leaving the old file IS
        the intent (`_write_atomically`: a fragment `kubectl apply` would
        consume is worse than an old manifest). What must not happen is the
        RUN claiming the file is gone — see the timeout arm below."""
        d = _tree(tmp_path, ["db-a.yaml"])
        out = _out(tmp_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("apiVersion: v1\n# previous run\n", encoding="utf-8")
        r = _run(d, out, shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_FAIL": "boom"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert out.read_text(encoding="utf-8") == "apiVersion: v1\n# previous run\n"

    def test_the_timeout_arm_passes_its_own_bound_and_says_what_is_on_disk(
            self, tmp_path, monkeypatch):
        """Two things the shim cannot reach.

        ⛔ The bound: blind review deleted `timeout=` from the `kubectl` call
        and all 44 tests stayed green — an unbounded wait on a customer-run
        target is the silent wedge `_KUBECTL_TIMEOUT_S` exists against, and
        nothing was holding it.

        ⛔ The wording: the message said it was "refusing to leave a
        half-built or stale <output> behind" while deliberately leaving the
        previous one there. The docs make assemble and `kubectl apply` two
        steps, so an operator who believes there is no artifact runs the
        second step anyway and ships the OLD config.
        """
        mod = _import_script("cma_kubectl_timeout_probe")
        d = _tree(tmp_path, ["db-a.yaml"])
        out = _out(tmp_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("apiVersion: v1\n# previous run\n", encoding="utf-8")
        assert mod._KUBECTL_TIMEOUT_S > 0, "the wedge guard must be bounded"

        validator = json.dumps([{"check": "tenant_uniqueness",
                                 "status": "pass", "details": []}])

        def _dispatch(cmd, *a, **k):
            if cmd[0] != "kubectl":
                return type("_R", (), {"returncode": 0, "stdout": validator,
                                       "stderr": ""})()
            assert k.get("timeout") == mod._KUBECTL_TIMEOUT_S, (
                "the kubectl call must pass its own bound, not rely on a "
                "default that does not exist")
            raise subprocess.TimeoutExpired(cmd="kubectl", timeout=k["timeout"])

        monkeypatch.setattr(subprocess, "run", _dispatch)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mod.main(["--config-dir", str(d), "--output", str(out)])
        assert rc == 2
        assert out.read_text(encoding="utf-8") == "apiVersion: v1\n# previous run\n"
        said = err.getvalue()
        assert "NOT touched" in said, said
        assert "stale" in said, said


class TestTheArtifactAppearsWholeOrNotAtAll:
    """`_write_atomically` had nothing on it: blind review replaced the whole
    function with `output.write_text(text)` and all 44 tests stayed green,
    because every arm that looks at the output either succeeds or fails
    BEFORE the write. A direct write truncates first, so a crash mid-write
    leaves a fragment `kubectl apply` would consume."""

    def test_the_destination_still_holds_the_old_file_when_the_swap_happens(
            self, tmp_path, monkeypatch):
        mod = _import_script("cma_atomic_probe")
        out = tmp_path / "build" / "threshold-config.yaml"
        out.parent.mkdir(parents=True)
        out.write_text("OLD", encoding="utf-8")

        seen: dict[str, object] = {}
        real_replace = os.replace

        def _spy(src, dst):
            p = Path(dst)
            # The whole point: at the instant the destination changes, the
            # NEW content already exists complete somewhere else, and the
            # destination still holds the OLD one — never a prefix of either.
            seen["dst_before"] = p.read_text(encoding="utf-8") if p.exists() else None
            seen["src_text"] = Path(src).read_text(encoding="utf-8")
            seen["same_dir"] = Path(src).parent == p.parent
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", _spy)
        mod._write_atomically(out, "NEW" * 1000)

        assert seen, "nothing was swapped into place — the write was direct"
        assert seen["dst_before"] == "OLD"
        assert seen["src_text"] == "NEW" * 1000
        # A rename is only atomic within one filesystem, so the temp file has
        # to be a sibling of the destination, not in $TMPDIR.
        assert seen["same_dir"] is True
        assert out.read_text(encoding="utf-8") == "NEW" * 1000

    def test_a_failure_during_the_write_leaves_the_old_file_and_no_debris(
            self, tmp_path, monkeypatch):
        """必響對照組 for the spy above, and the reason it matters: the arm
        that a direct write gets wrong."""
        mod = _import_script("cma_atomic_probe2")
        out = tmp_path / "build" / "threshold-config.yaml"
        out.parent.mkdir(parents=True)
        out.write_text("OLD", encoding="utf-8")

        def _boom(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(mod.os, "replace", _boom)
        with pytest.raises(OSError):
            mod._write_atomically(out, "NEW")
        assert out.read_text(encoding="utf-8") == "OLD"
        assert sorted(p.name for p in out.parent.iterdir()) == [out.name], (
            "a temp file was left behind")


@pytest.mark.skipif(shutil.which("kubectl") is None,
                    reason="no kubectl on this host; the shim above is the "
                           "primary coverage precisely because CI has none")
def test_end_to_end_with_a_real_kubectl(tmp_path):
    """Supplementary: the shim asserts the argv contract, this asserts that
    a real kubectl accepts it and emits a ConfigMap carrying those keys."""
    import yaml  # noqa: PLC0415
    d = _tree(tmp_path, ["db-a.yaml", "DB-U.YAML"], tenant_of={"DB-U.YAML": "db-u"})
    out = _out(tmp_path)
    r = _run(d, out)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert set(doc["data"]) == {"_defaults.yaml", "db-a.yaml", "DB-U.YAML"}
