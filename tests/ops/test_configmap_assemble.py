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

# A shim that records exactly what argv it was handed and BUILDS the manifest
# those arguments describe. `KUBECTL_SHIM_FAIL` turns it into a failing
# kubectl; `KUBECTL_SHIM_DROP` / `KUBECTL_SHIM_ADD` / `KUBECTL_SHIM_TRUNCATE`
# make it produce an artifact that disagrees with its own arguments, which is
# what `measure_artifact` exists to catch.
#
# ⛔ It has to build a real manifest now, not a stub: the script reads its own
# output back and reconciles it. A stub would make every arm here red, and a
# shim that cannot be made to LIE would leave the reconciliation untested.
_SHIM = """#!{python}
import base64, json, os, sys
dump = os.environ.get("KUBECTL_SHIM_ARGV")
if dump:
    with open(dump, "w", encoding="utf-8") as fh:
        json.dump(sys.argv, fh)
fail = os.environ.get("KUBECTL_SHIM_FAIL")
if fail:
    sys.stderr.write(fail + "\\n")
    sys.exit(3)
data, binary = {{}}, {{}}
for arg in sys.argv[1:]:
    if not arg.startswith("--from-file="):
        continue
    key, path = arg[len("--from-file="):].split("=", 1)
    raw = open(path, "rb").read()
    if key == os.environ.get("KUBECTL_SHIM_TRUNCATE"):
        raw = raw[:-1]
    if key == os.environ.get("KUBECTL_SHIM_DROP"):
        continue
    try:
        data[key] = raw.decode("utf-8")
    except UnicodeDecodeError:
        binary[key] = base64.b64encode(raw).decode("ascii")
extra = os.environ.get("KUBECTL_SHIM_ADD")
if extra:
    data[extra] = "tenants: {{}}\\n"
doc = {{"apiVersion": "v1", "kind": "ConfigMap",
       "metadata": {{"name": sys.argv[3], "namespace": "monitoring"}}}}
if data:
    doc["data"] = data
if binary:
    doc["binaryData"] = binary
try:
    import yaml
    out = yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True)
except ImportError:
    out = json.dumps(doc)          # JSON is YAML; the reader is the same
sys.stdout.write(out)
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

TARGET = "configmap-assemble"


def _recipe_lines() -> list[str]:
    """The recipe lines of `configmap-assemble`, read with make's own rule.

    ⛔ NOT "everything up to the next target name". That boundary made this
    file's assertions fire on a NEIGHBOUR's recipe the moment a target was
    inserted between the two named ones, and the remedy the failure message
    offers is wrong when applied there. make's rule is positional: a recipe
    is the run of TAB-prefixed lines under the rule line, and it ends at the
    first line that is neither TAB-prefixed nor blank.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    # ⚠️ The rule line is the one carrying the recipe, not merely the first
    # line spelled `configmap-assemble:` — a target-specific variable
    # assignment is spelled exactly the same way and may sit above it.
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith(TARGET + ":")
                  and i + 1 < len(lines) and lines[i + 1].startswith("\t")),
                 None)
    assert start is not None, (
        f"no rule line for `{TARGET}` with a recipe under it was found in "
        f"{MAKEFILE}. make's rule is positional: the recipe's first line "
        f"must come IMMEDIATELY after the rule line — a blank line in "
        f"between is invisible to make but ends the lookup here. (Without "
        f"this message the lookup raised a bare StopIteration and said "
        f"nothing at all.)")
    body = []
    for ln in lines[start + 1:]:
        if ln.startswith("\t"):
            body.append(ln)
        elif ln.strip() == "":
            continue
        else:
            break
    return body


def _recipe_body() -> str:
    return "\n".join(_recipe_lines())


def _make(*args: str) -> str:
    r = subprocess.run(["make", *args], cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _make_rule_block() -> list[str]:
    """What make itself says about this target: `--print-data-base`.

    The paragraph holds the rule line (target + prerequisites), any
    target-specific variable assignments, the automatic variables make
    computed, and the recipe AS STORED — unexpanded, `$$` and `@` intact.

    ⚠️ The rule line is not always the paragraph's first: a target-specific
    variable makes make open the block with `# makefile (from …)`. Matched
    on "a line anywhere in here", so the lookup does not turn a
    target-specific variable into "no rule found".
    """
    for para in _make("--print-data-base", "-n", TARGET).split("\n\n"):
        lines = para.splitlines()
        if (any(ln.startswith(TARGET + ":") for ln in lines)
                and any(ln.startswith("#  recipe to execute") for ln in lines)):
            return lines
    raise AssertionError(f"make --print-data-base printed no rule for {TARGET}")


class TestTheRecipeIsInertToMakeAndToTheShell:
    """The successor to the old `TestRecipeSelection`, to the four-string
    blocklist that replaced it, and to the two-string blocklist that replaced
    THAT.

    ⛔ Both blocklists were ENUMERATIONS OF SPELLINGS and blind review walked
    through both: first `$(foreach …)` + `$(wildcard …)` + `$(notdir …)` past
    the four strings, then `@#$D` — make's SINGLE-CHARACTER variable
    reference, which contains neither `$(` nor `${` — past the two, creating
    a file during `make -n` with every test green. A third list would have
    been the third version of the same mistake.

    What is asked here instead is one property and one reconciliation,
    neither of which enumerates anything:

    * **source side (no make needed)** — with the `$$` escapes removed, a
      recipe line holds no `$`. That is make's grammar, not a list: `$$` is
      the only way to write a literal dollar in a recipe, so every other `$`
      is make about to expand something — `$(`, `${` and `$D` alike,
      comments included (make expands a recipe comment before `#` reaches
      the shell, so a function call written there RUNS).
    * **two sources, both make's** — the recipe make STORED (unexpanded,
      from `--print-data-base`) normalises to exactly what `make -n` prints.
      ⛔ The reason given for deleting the literal-expansion assertion was
      WRONG and it is back below: the sister target `sharded-assemble` does
      open with `@mkdir -p .build`, but `make -n configmap-assemble` never
      prints the sister's recipe, so that assertion could not see it. It was
      restored after measuring that removing it let a SHELL-layer producer
      (`@echo "shipping: $$(ls "$$CONFDIR"/*.yaml)"` — #1792 in its original
      shape) live through the whole class, because the two properties above
      are both satisfied by it: `$$` is a literal dollar to make, so nothing
      is expanded and nothing holds a stray `$`.

    …plus the two things without which neither speaks for the whole target:
    no prerequisites, and no target-specific variable (`SHELL := <wrapper>`
    was the third walk-through).

    ⚠️ `.PHONY` is NOT asserted in this class. It is asked of every target in
    the Makefile by `tests/ops/test_makefile_phony.py` (#1929).

    ⛔ **KNOWN SCOPE, measured, deliberately NOT closed.** Everything here
    covers THIS target's recipe text, its direct prerequisites and its
    target-specific variables — nothing else. Blind review walked through
    four channels that all live outside that scope and all stayed green:
    a global `.EXTRA_PREREQS` (GNU make states it does not enter the
    automatic variables, so `$^` / `$|` cannot see it), a GLOBAL (not
    target-specific) `SHELL :=` — `Makefile:4` already holds one — a global
    `.SHELLFLAGS`, and a silent second `::` rule whose recipe expands to
    nothing so `make -n` prints no extra line.

    ⚠️ The fourth one costs one more edit than the sentence above used to
    admit, and the edit is not optional: appending a `::` rule while this
    target keeps its `:` rule is a make HARD ERROR (measured:
    `*** target file 'configmap-assemble' has both : and :: entries.
    Stop.`), so the walk-through has to rewrite THIS target's own rule line
    to `::` as well. Measured with both rules `::` and a second one whose
    recipe is `@$(EMPTY_VAR)`: this class stays green, 8 passed. The
    channel is real; the one-line version of it was not buildable.

    ⛔ That is a DISCLOSURE, not a to-do. Three versions of "enumerate the
    channels" have been walked through (four strings, two strings, this
    scope), so a fourth predicate is forbidden. A Makefile-WIDE edit is
    outside what any target-scoped assertion can see; review of the
    `Makefile` globals is what covers it.
    """

    def test_the_recipe_calls_the_script(self):
        """必響對照組: without this both properties below are vacuous — an
        empty recipe has no `$` and matches its own expansion."""
        body = _recipe_body()
        assert "scripts/ops/configmap_assemble.py" in body, body
        assert "--config-dir" in body, body

    def test_no_recipe_line_holds_a_dollar_make_would_expand(self):
        """Source side, and the only arm here that needs no `make` binary —
        deliberately, because the class it guards (a producer moving back
        into the recipe) does not stop existing on a host without make."""
        offenders = [ln for ln in _recipe_lines()
                     if "$" in ln.replace("$$", "")]
        assert not offenders, (
            "make will expand something in this recipe. `$$` is the literal "
            "dollar; anything else — `$(shell …)`, `${VAR}`, or the "
            "single-character `$D` — is an expansion, and a comment line is "
            "no exception (make expands it before `#` reaches the shell, so "
            "it RUNS). If a make variable is genuinely needed, `export` it "
            "and read it as `$$NAME`:\n" + "\n".join(offenders))

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                        reason="needs GNU make to answer for its own recipe")
    def test_make_reads_the_same_recipe_lines_this_file_does(self):
        """必響對照組 for the text extraction above: the property is only
        worth anything if the lines it ran on are the lines make will run.
        Pins this file's parse against make's."""
        block = _make_rule_block()
        at = next(i for i, ln in enumerate(block)
                  if ln.startswith("#  recipe to execute"))
        assert block[at + 1:] == _recipe_lines()

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                        reason="needs GNU make to expand the recipe")
    def test_the_expansion_is_the_recipe_with_nothing_expanded(self):
        """Two sources, both make's own: what it stored vs what it will run.

        The normalisation is make's documented recipe-line handling and
        nothing else — one leading TAB, then the `@`/`-`/`+` prefixes, then
        `$$` -> `$`. Anything make actually expanded shows up as a line that
        no longer matches its source.

        ⚠️ `make -n` strips the `@` / `-` prefixes, so "can the gate fail the
        target" is NOT answerable here — `TestTheGateCanActuallyFailTheTarget`
        runs the real thing for that.
        """
        block = _make_rule_block()
        at = next(i for i, ln in enumerate(block)
                  if ln.startswith("#  recipe to execute"))
        stored = [ln[1:].lstrip("@-+").replace("$$", "$")
                  for ln in block[at + 1:]]
        assert stored == _make("-n", TARGET).splitlines(), (
            "make expanded something in this recipe: the line it printed is "
            "not the line it stored. A make-level producer is exactly this "
            "difference, wherever it is written — including in a comment. "
            "⚠️ SCOPE: this reconciliation reads THIS target's recipe only; "
            "a global `SHELL` / `.SHELLFLAGS` / `.EXTRA_PREREQS`, or this "
            "target rewritten as `::` plus a second `::` rule with an "
            "empty recipe, is invisible to it — see the class docstring, "
            "that is a known boundary.")

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                        reason="needs GNU make to expand the recipe")
    def test_the_expansion_is_exactly_the_script_call_and_nothing_else(self):
        """⛔ RESTORED (it had been deleted, and deleting it was the defect).

        The shell layer: the expansion is exactly ONE command and it is the
        script call, so there is no second command in which a glob, a
        `kubectl` or a `basename` could live. The two properties above do
        NOT cover that — `@echo "shipping: $$(ls "$$CONFDIR"/*.yaml)"` is a
        literal dollar to make, so nothing is expanded, nothing holds a
        stray `$`, and the stored recipe reconciles with `make -n`. That is
        #1792 in its original shape, and only this arm kills it.

        Division of labour with `_recipe_lines`: the false red that got this
        deleted was a text-extraction bug (the old boundary was "up to the
        next target name", so inserting a target made every arm here fire on
        a NEIGHBOUR's recipe). `_recipe_lines` fixed that on its own. This
        arm is a PIN — it spells the command out — so adding a flag to the
        script call is meant to red it; the fix is then to update the pin.
        ⚠️ Measured, that is not the only legitimate edit it reds: putting
        `@mkdir -p .build` first, the way the sister target
        `sharded-assemble` (`Makefile:620`) opens, also fails this arm and
        this arm only. ⛔ The predicate is NOT loosened for it — that edit
        is redundant here anyway, `_write_atomically` already does
        `mkdir(parents=True, exist_ok=True)`
        (`scripts/ops/configmap_assemble.py:365`) — but a reader who hits
        that red should not have to rediscover which cell they are in.

        ⚠️ `make -n` strips the `@` / `-` prefixes, so "can the gate fail the
        target" is NOT answerable here — `TestTheGateCanActuallyFailTheTarget`
        runs the real thing for that.
        """
        r = subprocess.run(["make", "-n", TARGET], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        # Join the `\`-continuations make prints verbatim, then drop the
        # comment lines (they are the recipe's own prose; the arms above are
        # what keep them inert).
        text = r.stdout.replace("\\\n", " ")
        commands = [" ".join(ln.split()) for ln in text.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
        assert commands == [
            'python3 ./scripts/ops/configmap_assemble.py '
            '--config-dir "$CONFDIR" --output .build/threshold-config.yaml'
        ], (
            "the expansion is no longer exactly the script call. Either a "
            "second command appeared — that is where a glob, a `kubectl` or "
            "a `basename` moves back into the recipe, and the arms above "
            "cannot see it because a shell-level `$$(…)` is a literal "
            "dollar to make — or the script call itself changed. ⚠️ The "
            "second case is a legitimate edit and this arm is a PIN, so "
            "update the expected line here in the same commit; do NOT "
            "loosen it into a substring check.\n" + r.stdout)

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                        reason="needs GNU make to answer for its own rule")
    def test_nothing_else_reaches_into_this_target(self):
        """The reconciliation above only speaks for the lines make printed
        for THIS target, and only if the shell running them is the ordinary
        one.

        Both halves were walk-throughs: a PREREQUISITE target's recipe runs
        first and its comments expand too (blind review put `$(shell touch
        …)` in one and the canary appeared), and `configmap-assemble: SHELL
        := <wrapper>` hands the same command text to something else
        entirely. Asked of make's rule database, not of the Makefile text,
        and asked as "no target-specific variable at all" rather than as a
        list of the dangerous ones.

        ⛔ `.PHONY` is NOT asserted here. Asserting it on this one target name
        was an enumeration (`D-05`) and missed every other Makefile target
        with the same hole; `tests/ops/test_makefile_phony.py` now asks it of
        all of them (#1929). Not repeated here — two synonymous assertions
        drift, and the narrower one goes stale first.
        """
        block = _make_rule_block()
        prereqs = [ln.split(":=", 1)[1].strip() for ln in block
                   if ln.startswith("# ^ :=") or ln.startswith("# | :=")]
        assert prereqs and not any(prereqs), (
            f"{TARGET} has grown prerequisites; their recipes run (and "
            f"expand) before this one: {prereqs}")
        overrides = [ln for ln in block
                     if ln.startswith(TARGET + ":") and "=" in ln.split(":", 1)[1]]
        assert not overrides, (
            "a target-specific variable on this target changes how the "
            f"recipe runs without changing its text:\n" + "\n".join(overrides))

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

    def test_a_second_checkout_of_this_very_tree_is_let_through(
            self, tmp_path, kubectl_shim):
        """⛔ The DISCLOSED boundary as an assertion, not only as prose
        (D-05g: an exemption's reachability has to be written down as one).

        `SAMPLE_CONFIG_DIR` is derived from this script's `__file__`, so the
        block is PER-CHECKOUT: a byte-identical copy of this repo's sample
        tree in a second clone or worktree builds with rc 0. The arm above
        makes a different claim about a different tree ("merely ends in the
        same components"); this one is the SAME tenants, same bytes, other
        checkout.

        ⚠️ A disclosure, NOT a hole to close: the defect class is "ran with
        the built-in `CONFDIR` default", which always resolves inside the
        running checkout, so reaching this state means typing the other
        path by hand. Converging repo identity through `git` would SILENTLY
        pass where there is no `git`. The refusal message says so, which
        this arm also pins.
        """
        other = (tmp_path / "second-checkout"
                 / SAMPLE_TREE.relative_to(REPO))
        shutil.copytree(SAMPLE_TREE, other)
        assert (other / "db-a.yaml").read_bytes() == (
            SAMPLE_TREE / "db-a.yaml").read_bytes(), "the fixture lost its point"
        r = _run(other, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SAMPLE" not in r.stderr, r.stderr
        # …and the refusal the operator DOES see names this boundary, so the
        # remedy it offers ("point the target at your own tree") cannot be
        # read as covering a checkout it does not cover.
        blocked = _run(SAMPLE_TREE, _out(tmp_path))
        assert blocked.returncode == 1, blocked.stdout + blocked.stderr
        assert "PER-CHECKOUT" in blocked.stderr, blocked.stderr


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
            # The script reads this back and reconciles it with the tree, so
            # the fake has to answer for the fixture, not with a stub.
            stdout = ("apiVersion: v1\nkind: ConfigMap\ndata:\n"
                      + "".join(
                          f"  {p.name}: "
                          f"{json.dumps(p.read_text(encoding='utf-8'))}\n"
                          for p in sorted(d.iterdir())))
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
    """⛔ Refuse BY NAME — but as MESSAGE QUALITY, not as a guard.

    The sentence that stood here ("handing it to `kubectl` produces a
    message that never says which file") was FALSE, and it was the premise
    for keeping this whole family. Real kubectl v1.31.0 names `db b.yaml`
    itself, regex and all, and removing `configmap_key_problem` entirely
    leaves rc 1 with no artifact written — nothing passes silently.

    What this layer does buy is measured and small: it lists ALL the
    offending names (kubectl stops at the first), and it is the only
    by-name answer for **`=` and `,`** — TWO classes, not three. All three
    of `=`, `,` and `"` are mangled by pflag's CSV split and
    `ParseFileSource` before `IsConfigMapKey` ever runs, but only the first
    two lose the name with it: `=` gives `key names or file paths cannot
    contain '='` (nothing named) and `,` gives `error reading db: no such
    file or directory` (a fragment that is not a file), while `"` gives
    `invalid argument "db\\"a.yaml=<path>" for "--from-file" flag: …` —
    the file name is echoed back verbatim. ⚠️ The earlier version of this
    sentence put `"` in the same bucket as the other two; that half had not
    been measured. The per-class measurements are in
    `configmap_key_problem`'s own docstring.

    ⛔ The one thing that WOULD be silent is dropping the file instead of
    refusing: that loses a tenant behind a green light (#1603's shape).
    """

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


class TestTheArtifactIsReconciledAgainstTheTree:
    """The successor to `TestTheFromFileSourceKubectlHasToParse`, and the
    reason that class is gone.

    ⛔ That guard transcribed `ParseFileSource` and counted `=` in the source
    string. Blind review found the layer ABOVE it: `--from-file` is a pflag
    `StringSliceVar`, so the value is split by `readAsCSV` first, and a path
    holding `,` or `"` was fabricated into two sources while the guard said
    nothing. Transcribing CSV as well buys one layer and leaves the next.

    ⛔ **The cost of removing it**, recorded here because it is a real loss:
    `--config-dir .../env=prod/conf.d` is no longer named by us. kubectl
    still refuses (the run fails, its message is forwarded), but that
    message blames "key names or file paths" and names neither.

    What replaces it asks the artifact instead of the parser: the keys and
    the value lengths in the manifest kubectl just produced must be the
    carriers this run selected. Every mangling of the argument list lands
    there, whichever layer did it.
    """

    def test_a_key_the_manifest_lost_is_named_and_nothing_is_written(
            self, tmp_path, kubectl_shim):
        """The silent half of a mangled argument: the artifact builds, rc is
        0, and one tenant is simply not in it."""
        d = _tree(tmp_path, ["db-a.yaml", "db-b.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_DROP": "db-b.yaml"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "db-b.yaml" in r.stderr, r.stderr
        assert "ABSENT" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_a_key_nobody_asked_for_is_named(self, tmp_path, kubectl_shim):
        """The other direction: a fabricated source (what `readAsCSV` does
        with a `,` in the path) puts a key in the ConfigMap that no file in
        the tree declares."""
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_ADD": "db-ghost.yaml"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "db-ghost.yaml" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_a_value_that_is_not_the_file_is_named(self, tmp_path, kubectl_shim):
        """The key can be right and the bytes still wrong — a fabricated
        source whose basename collides. Lengths are compared, so a value
        that is not what is on disk is caught by the same arm."""
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim,
                 env_extra={"KUBECTL_SHIM_TRUNCATE": "db-a.yaml"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "db-a.yaml" in r.stderr, r.stderr
        assert "bytes" in r.stderr, r.stderr
        assert not _out(tmp_path).exists()

    def test_an_honest_manifest_passes(self, tmp_path, kubectl_shim):
        """必響對照組: without this the three above are satisfied by a check
        that refuses every manifest. The fixture carries a directory name
        with a space and parens — the reconciliation must not be a second
        opinion about what a path may look like."""
        d = _tree(tmp_path, ["db-a.yaml", "DB-U.YAML"],
                  tenant_of={"DB-U.YAML": "db-u"}, at="my confs (v2)/conf.d")
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _out(tmp_path).exists()

    def test_the_reconciliation_is_against_the_tree_not_a_shape(self):
        """Pinned at the function, so the property is visible without a
        subprocess: what it compares is the carrier set and the byte
        lengths, not a spelling."""
        mod = _import_script("cma_reconcile_probe")
        manifest = ("apiVersion: v1\nkind: ConfigMap\n"
                    "data:\n  ghost.yaml: x\n")
        problems, measured = mod.measure_artifact(manifest, [])
        assert problems and "ghost.yaml" in problems[0], problems
        assert measured == {"ghost.yaml": 1}
        # "could not measure" is not "measured clean", here too.
        assert mod.measure_artifact("{", [])[0], "unparseable output passed"
        assert mod.measure_artifact("- a list\n", [])[0], "non-mapping passed"
        # A manifest with no `data` at all and nothing expected is not a
        # disagreement — the empty-dir arm owns that question.
        assert mod.measure_artifact("kind: ConfigMap\n", []) == ([], {})
        # ⚠️ `binaryData` is measured DECODED, because that is what
        # `ValidateConfigMap` sums. Asserted here rather than through the
        # shim because nothing in the tested path reaches it: a carrier
        # kubectl would carry as binary is not parseable YAML, so
        # `tenant_uniqueness` refuses the tree one step earlier.
        binary = "kind: ConfigMap\nbinaryData:\n  b.yaml: AAECAwQ=\n"
        assert mod.measure_artifact(binary, [])[1] == {"b.yaml": 5}
        bad = "kind: ConfigMap\nbinaryData:\n  b.yaml: not-base64!!\n"
        assert mod.measure_artifact(bad, [])[0], "unmeasurable value passed"


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

    def test_a_long_list_is_capped_and_says_that_it_is(self, tmp_path,
                                                       kubectl_shim):
        """⛔ Same stderr, same truncation. `_lib_confd`'s nested warning caps
        its list at `WARN_LIMIT` and says `(+N more)`; this reader printed to
        the same plane without a cap, so an operator reading five names could
        not tell from the output whether five was all there was — the answer
        depended on which reader wrote the line.

        The count is imported, not spelled again: two literals would drift.
        """
        from _lib_confd import WARN_LIMIT  # noqa: PLC0415

        d = _tree(tmp_path, ["db-a.yaml"])
        for i in range(WARN_LIMIT + 1):
            (d / f"db-dir{i}.yaml").mkdir()
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        named = [ln for ln in r.stderr.splitlines() if "db-dir" in ln]
        assert len(named) == WARN_LIMIT, r.stderr
        assert "(+1 more" in r.stderr, r.stderr

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

    def test_the_client_side_apply_ceiling_is_said_out_loud(
            self, tmp_path, kubectl_shim):
        """⛔ The limit that bites FIRST, at a quarter of the one above.

        `ValidateConfigMap` calls `ValidateObjectMeta` on its first line, and
        a client-side `kubectl apply -f` — what the deployment doc teaches —
        puts the whole object into the `last-applied-configuration`
        ANNOTATION, capped at 256 KB. So a 400 KB tree passed every check
        here and died at apply on `metadata.annotations: Too long`, naming
        no file: the exact shape this gate exists to delete, inside the gate.

        ⚠️ A WARN, not a refusal, and that is the judgement being pinned:
        `kubectl apply --server-side` stores no such annotation, so refusing
        would stop a deploy the API server accepts.
        """
        mod = _import_script("cma_annotation_probe")
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "db-a.yaml").write_text(
            _TENANT.format(t="db-a") + "#" * (mod._ANNOTATION_TOTAL_LIMIT + 10),
            encoding="utf-8")
        assert sum(p.stat().st_size for p in d.iterdir()) < mod._MAX_CONFIGMAP_BYTES
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "annotations" in r.stderr.lower(), r.stderr
        assert "--server-side" in r.stderr, r.stderr
        assert _out(tmp_path).exists(), "a WARN must not stop the build"

    def test_a_small_tree_hears_nothing_about_annotations(
            self, tmp_path, kubectl_shim):
        """必響對照組: without this the WARN above could be unconditional."""
        d = _tree(tmp_path, ["db-a.yaml"])
        r = _run(d, _out(tmp_path), shim=kubectl_shim)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "annotation" not in r.stderr.lower(), r.stderr

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
    a real kubectl accepts it and emits a ConfigMap carrying those keys.

    ⛔ It also carries the one thing `from_file_source_problem` used to
    hold, now that that transcription is gone: a `--config-dir` spelled
    with an `=` must still end the run non-zero with NO artifact. The cost
    recorded when that guard was withdrawn is that WE no longer name the
    path — kubectl's own refusal says `key names or file paths cannot
    contain '='` and names neither — but "it fails and writes nothing" was
    left with nothing holding it at all. Asserted here rather than in a
    guard of its own: only a real kubectl can answer it, which is why this
    is the arm it hangs on.

    ⛔ **And that is also its limit, stated rather than fixed.** The whole
    function sits behind `shutil.which("kubectl")`, and no CI workflow
    installs kubectl (`grep -rl kubectl .github/workflows/` names only
    `image-ref-resolve.yaml` and `nightly-image-scan.yaml`, neither of
    which runs these tests) ⇒ on CI this SKIPS, so the `=` coverage below
    holds only on a host that happens to have kubectl. No mechanism is
    added for it here: a green CI does not mean that cell ran.
    """
    import yaml  # noqa: PLC0415
    d = _tree(tmp_path, ["db-a.yaml", "DB-U.YAML"], tenant_of={"DB-U.YAML": "db-u"})
    out = _out(tmp_path)
    r = _run(d, out)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert set(doc["data"]) == {"_defaults.yaml", "db-a.yaml", "DB-U.YAML"}

    weird = _tree(tmp_path / "env=prod", ["db-a.yaml"])
    weird_out = tmp_path / "eq-build" / "threshold-config.yaml"
    r2 = _run(weird, weird_out)
    assert r2.returncode != 0, r2.stdout + r2.stderr
    assert not weird_out.exists(), "an artifact was written from a mangled run"
