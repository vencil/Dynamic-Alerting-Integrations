"""Engine detection layer: did the external engine actually give a verdict? (#3611)

A lint gate that shells out to an external engine (kube-linter, helm,
hadolint, pint, ...) has three possible answers, not two:

* :class:`Ran` — the engine ran and its output **proves** it examined input.
  Only now are the engine's findings (or their absence) a verdict.
* :class:`NoVerdict` — the engine is absent, could not start, timed out, or
  exited without the output a real run prints. The contract's 2
  (``_lib_exitcodes``: missing binary / environment), never 0 ("clean") and
  never 1 ("your content is wrong").

The failure this layer exists for (#3611, A class "fake success"): each tool
decided "did it run" on its own, and several decided wrongly — kube-linter
exits 0 with an empty stdout when it found no objects to lint ("Warning: no
valid objects found") and 1 with an empty stdout when it could not load its
input, and both were read as a clean scan (rc 0, "OK").

**Proof of running is per (engine, subcommand)** (:data:`PROOFS`), because the
evidence differs: ``kube-linter lint --format json`` always prints a document
with ``Reports`` and ``Summary`` when it linted something; ``helm template``
prints the rendered manifests; ``hadolint --no-fail --format json`` prints a
JSON array and exits 0; ``pint lint`` logs ``entries=N``. A call whose
(engine, subcommand) has no registered proof is a programming error
(``KeyError``), not a silent pass.

**Absence is a NoVerdict regardless of ``--ci``** (owner 2026-11-06, #3611 Q1):
a lint gate whose engine is missing exits 2. To skip a gate, use the runner's
own skip (pre-commit ``SKIP=<id>`` shows "Skipped", not "Passed"); the
environment is expected to provide the engine (``session-start.sh``, CI).
``--ci`` keeps one meaning only: whether findings make the run exit 1.

Engine pins (versions, image digests) stay with the tool that owns them
(``docs/internal/toolchain-versions.md`` is their SSOT map); this module only
locates and judges.

Which call sites must go through here is not a hand list:
``tests/shared/test_engine_call_sites.py`` walks the gated trees' AST, using
:data:`ENGINES` as its vocabulary, and the sites it finds outside this module
are its ``PENDING`` set, which may only shrink.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

#: Every external engine whose output decides a verdict somewhere under the
#: gated trees: the name the gate looks for, mapped to the executable name on
#: PATH. Adding an engine here makes every bypassing call site of it visible to
#: the call-site gate at once.
ENGINES: dict[str, str] = {
    "amtool": "amtool",
    "gofmt": "gofmt",
    "hadolint": "hadolint",
    "helm": "helm",
    "kube-linter": "kube-linter",
    "kubectl": "kubectl",
    "mmdc": "mmdc",
    "node": "node",
    "pint": "pint",
    "promtool": "promtool",
}

#: Exit status a timed-out engine is reported with in a NoVerdict reason
#: (what ``timeout(1)`` prints), and an engine that could not be started (the
#: shell's "command not found") — kept apart so the two environment failures
#: stay tellable apart. Reasons only; never an exit status of the tool.
_TIMEOUT_RC = 124
_NOT_RUNNABLE_RC = 127

_STDERR_TAIL = 2000


@dataclass(frozen=True)
class Ran:
    """The engine ran and proved it examined input.

    ``data`` is what the proof extracted (parsed JSON, the rendered text, the
    entries count, ...); ``proc`` is the raw result for callers that stream
    the engine's own output through."""
    data: Any
    proc: subprocess.CompletedProcess


@dataclass(frozen=True)
class NoVerdict:
    """The engine gave no verdict. ``reason`` is one line naming why, for the
    caller's ``ERROR:`` / ``CALLER_ERROR`` line; ``detail`` is the engine's
    stderr tail, if any."""
    reason: str
    detail: str = ""

    def message(self) -> str:
        return self.reason + (f"\n{self.detail}" if self.detail else "")


Result = Union[Ran, NoVerdict]


class NoProof(Exception):
    """Raised by a proof function: the output does not show a real run."""


@dataclass(frozen=True)
class Located:
    """Where an engine will run from: a binary on PATH, or the tool's pinned
    docker image (the containerised fallback)."""
    engine: str
    mode: str                 # "binary" | "docker"
    path: Optional[str] = None
    image: Optional[str] = None

    def describe(self) -> str:
        return self.path if self.mode == "binary" else f"docker {self.image}"


def locate(engine: str, *, docker_image: Optional[str] = None) -> Located | NoVerdict:
    """The engine's binary on PATH, else the docker fallback when the tool has
    one, else a NoVerdict naming both ways out.

    ``docker`` on PATH is not proof the fallback works (no daemon is the
    common case on a dev box or a cloud session); that is found out when it
    runs, as a NoVerdict from :func:`run`."""
    binary = ENGINES[engine]
    path = shutil.which(binary)
    if path:
        return Located(engine, "binary", path=path)
    if docker_image and shutil.which("docker"):
        return Located(engine, "docker", image=docker_image)
    how = f"install the {binary} binary" + (
        ", or Docker for the containerised fallback" if docker_image else "")
    return NoVerdict(f"{engine} is not available ({how})")


def argv(located: Located, args: Sequence[str], *,
         docker_run: Sequence[str] = (), entrypoint: Optional[str] = None) -> list[str]:
    """The command line for *located*: ``<binary> <args>``, or
    ``docker run --rm <docker_run> [--entrypoint E] <image> <args>``.
    *docker_run* carries the tool's mounts / workdir; paths inside *args* are
    the caller's to translate for the container."""
    if located.mode == "binary":
        return [located.path, *args]
    cmd = ["docker", "run", "--rm", *docker_run]
    if entrypoint:  # ignored for a binary: it IS the entrypoint
        cmd += ["--entrypoint", entrypoint]
    return [*cmd, located.image, *args]


def run(located: Located, subcommand: str, args: Sequence[str], *,
        timeout: int, cwd: Optional[str] = None,
        docker_run: Sequence[str] = (), entrypoint: Optional[str] = None,
        stdin_text: Optional[str] = None, env: Optional[dict] = None,
        expect: Optional[dict] = None) -> Result:
    """Run *located* and judge the result with the proof registered for
    ``(engine, subcommand)``. Never raises for an engine failure: a binary
    that cannot start, a timeout, and output without proof are all
    :class:`NoVerdict`.

    *expect* is what the proof checks the output against beyond its shape —
    how many inputs were sent (``inputs``), the file the run must write
    (``output``)."""
    proof = PROOFS[(located.engine, subcommand)]
    cmd = argv(located, args, docker_run=docker_run, entrypoint=entrypoint)
    via = "" if located.mode == "binary" else " (docker)"
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=stdin_text, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return NoVerdict(f"{located.engine}{via} did not finish within {timeout}s "
                         f"(rc {_TIMEOUT_RC})")
    except OSError as exc:
        return NoVerdict(f"cannot run {located.engine}{via}: {exc} "
                         f"(rc {_NOT_RUNNABLE_RC})")
    try:
        data = proof(proc, expect or {})
    except NoProof as exc:
        return NoVerdict(
            f"{located.engine} {subcommand}{via} did not run: {exc} "
            f"(exit {proc.returncode})",
            (proc.stderr or "").strip()[-_STDERR_TAIL:])
    return Ran(data, proc)


# ── proofs: what a real run of each (engine, subcommand) prints ─────────────

def _requires(proc: subprocess.CompletedProcess, *flags: str) -> None:
    """A proof reads the output a flag asks for; without the flag the same
    output means something else (hadolint without ``--no-fail`` exits 1 on
    a finding). A caller that drops the flag is a programming error."""
    args = list(proc.args) if isinstance(proc.args, (list, tuple)) else []
    missing = [f for f in flags if f not in args]
    if missing:
        raise ValueError(f"proof needs {missing} in the engine's argv: {args}")


def _kube_linter_lint(proc: subprocess.CompletedProcess, expect: dict) -> list[dict]:
    """``kube-linter lint --format json``: rc 0 (clean) or 1 (findings), and
    stdout a JSON object carrying ``Reports`` and ``Summary`` (measured on
    v0.7.4: a clean run prints ``"Reports": null``). No objects found and an
    unloadable path both print nothing on stdout — rc 0 and 1 respectively —
    and neither linted anything."""
    _requires(proc, "--format", "json")
    if proc.returncode not in (0, 1):
        raise NoProof("unexpected exit status")
    out = (proc.stdout or "").strip()
    if not out:
        raise NoProof("no JSON on stdout (it prints a report whenever it "
                      "linted at least one object)")
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        raise NoProof(f"stdout is not JSON: {out[:300]}") from None
    if not (isinstance(doc, dict) and "Reports" in doc and "Summary" in doc):
        raise NoProof("stdout is not a kube-linter report (no Reports/Summary)")
    reports = [] if doc["Reports"] is None else doc["Reports"]
    if not isinstance(reports, list):
        raise NoProof("Reports is not a list")
    return reports


def _helm_template(proc: subprocess.CompletedProcess, expect: dict) -> str:
    """``helm template``: rc 0 and a non-empty render. An empty render lints
    as "no objects", which is no scan at all."""
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise NoProof(lines[0] if lines else "render failed")
    if not (proc.stdout or "").strip():
        raise NoProof("rendered nothing")
    return proc.stdout


#: The unindented `name:` / `version:` lines helm prints for a chart.
_HELM_CHART_NAME = re.compile(r"^name:[ \t]*\S", re.M)
_HELM_CHART_VERSION = re.compile(r"^version:[ \t]*\S", re.M)


def _helm_show_chart(proc: subprocess.CompletedProcess, expect: dict) -> str:
    """``helm show chart <archive>``: rc 0 and Chart.yaml as helm read it —
    a `name:` and a `version:` line. Anything else did not read a chart."""
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise NoProof(lines[0] if lines else "helm show chart failed")
    out = proc.stdout or ""
    if not (_HELM_CHART_NAME.search(out) and _HELM_CHART_VERSION.search(out)):
        raise NoProof("printed no chart name / version")
    return out


def _helm_push(proc: subprocess.CompletedProcess, expect: dict) -> str:
    """``helm push``: rc 0. Its printed summary differs across helm majors, so
    it is not the proof; the caller reads the registry back instead
    (release_publish.py chart)."""
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise NoProof(lines[0] if lines else "helm push failed")
    return (proc.stdout or "") + (proc.stderr or "")


_HADOLINT_FIELDS = frozenset({"file", "line", "code", "level", "message"})


def _hadolint(proc: subprocess.CompletedProcess, expect: dict) -> list[dict]:
    """``hadolint --no-fail --format json``: exit 0 means "ran" (--no-fail),
    and stdout is a JSON array (a clean run prints ``[]``) whose items carry
    the fields every finding has — one without ``level`` would be classified
    as the lowest severity (PR #3610 review)."""
    _requires(proc, "--no-fail", "--format", "json")
    if proc.returncode != 0:
        raise NoProof("non-zero exit under --no-fail")
    out = (proc.stdout or "").strip()
    if not out:
        raise NoProof("no JSON on stdout (a clean run prints [])")
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        raise NoProof(f"stdout is not JSON: {out[:500]}") from None
    if not (isinstance(doc, list)
            and all(isinstance(f, dict) and _HADOLINT_FIELDS <= f.keys() for f in doc)):
        raise NoProof(f"stdout is not a JSON array of findings: {out[:500]}")
    return doc


_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_PINT_ENTRIES_RE = re.compile(r"entries=(\d+)")


def _pint_lint(proc: subprocess.CompletedProcess, expect: dict) -> tuple[int, int]:
    """``pint lint``: rc 0 (clean) or 1 (problems), and its log names how many
    rule entries it checked (``entries=N``; coloured unconditionally, so SGR
    is stripped first). Returns ``(entries, returncode)``. A pint that stops
    before its summary — a ``.pint.hcl`` parse error, a failed image pull —
    prints no ``entries=``, and one whose include matches nothing logs
    ``entries=0`` (measured, 0.86.0): no rule was linted, so no verdict."""
    if proc.returncode not in (0, 1):
        raise NoProof("unexpected exit status")
    m = _PINT_ENTRIES_RE.search(
        _ANSI_SGR_RE.sub("", (proc.stderr or "") + (proc.stdout or "")))
    if m is None:
        raise NoProof("no `entries=N` in its output (it logs one whenever it "
                      "parsed its rule files)")
    if int(m.group(1)) == 0:
        raise NoProof("entries=0: it parsed no rule entry")
    return int(m.group(1)), proc.returncode


def _json_stdout(proc: subprocess.CompletedProcess) -> Any:
    out = (proc.stdout or "").strip()
    if not out:
        raise NoProof("nothing on stdout")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise NoProof(f"stdout is not JSON: {out[:300]}") from None


def _node_esbuild_parse(proc: subprocess.CompletedProcess, expect: dict) -> list[dict]:
    """lint_jsx_syntax's driver (``node -e``, esbuild from the portal's
    lockfile, #3669): rc 0 and exactly one ``{path, ok}`` result per source
    file it was handed (``expect["paths"]``) — the same paths, each once. A
    missing or doubled path is a file never parsed; a non-zero exit (esbuild
    not installed: rc 3, the remedy on stderr) is no run."""
    if proc.returncode != 0:
        raise NoProof("non-zero exit")
    doc = _json_stdout(proc)
    if not (isinstance(doc, list)
            and all(isinstance(r, dict) and {"path", "ok"} <= r.keys() for r in doc)):
        raise NoProof("stdout is not a list of {path, ok} results")
    if sorted(r["path"] for r in doc) != sorted(expect["paths"]):
        raise NoProof(f"{len(doc)} result(s) for {len(expect['paths'])} file(s), "
                      "not one per file")
    return doc


def _node_cli_playground(proc: subprocess.CompletedProcess, expect: dict) -> list[dict]:
    """check_cli_contract's CLI Playground driver: rc 0 and a non-empty list
    of ``{key, kind, cmd}`` rows (a catalog that evaluates to no command is
    a page this check could not read, not a page with nothing wrong)."""
    if proc.returncode != 0:
        raise NoProof("non-zero exit")
    doc = _json_stdout(proc)
    if not (isinstance(doc, list) and doc
            and all(isinstance(r, dict) and {"key", "kind", "cmd"} <= r.keys() for r in doc)):
        raise NoProof("stdout is not a non-empty list of {key, kind, cmd} rows")
    return doc


def _mmdc_render(proc: subprocess.CompletedProcess, expect: dict) -> bool:
    """``mmdc -i <diagram> -o <png>``: ``True`` when it exited 0 and wrote a
    non-empty image (``expect["output"]``); ``False`` when it exited 1.

    ⛔ ``False`` is NOT yet a finding: mermaid-cli exits 1 both for a diagram
    it cannot parse and for a renderer that cannot start (measured, 12.2.0:
    "Parse error on line 3" and "Browser was not found" are both rc 1). Only
    a canary — a known-good diagram rendered by the same command line just
    before or after — separates them, and that is the caller's to run
    (validate_mermaid). No text is read to tell them apart (#3611 Q2)."""
    if proc.returncode == 1:
        return False
    if proc.returncode != 0:
        raise NoProof("unexpected exit status")
    out = Path(expect["output"])
    if not (out.is_file() and out.stat().st_size > 0):
        raise NoProof(f"exit 0 but no image at {out}")
    return True


def _promtool_check_rules(proc: subprocess.CompletedProcess, expect: dict) -> bool:
    """``promtool check rules <file>``: exit 0 is the proof. It prints no
    structured output, and its text (``SUCCESS: N rules found``) is not read
    (#3611 Q2); any other exit
    — a rule that does not parse, a path that does not exist (both rc 1,
    measured on 3.15.1) — is no verdict here. The caller knows how many rules
    it wrote: it rendered the file."""
    if proc.returncode != 0:
        raise NoProof("the rule file did not validate")
    return True


def _promtool_test_rules(proc: subprocess.CompletedProcess, expect: dict) -> dict[str, bool]:
    """``promtool test rules --junit <xml> <test>``: which test groups passed,
    ``{group name: passed}``, read from the JUnit report ``expect["junit"]``
    — never from the console (#3611).

    ⛔ A ``<failure>`` is NOT "the assertion did not hold". promtool reports
    every error inside a test group — input series that do not parse, a rule
    file that does not load, a rule that errors when evaluated, and an
    assertion that does not hold — as the same ``<failure>`` (``tc.Fail`` in
    upstream ``cmd/promtool/unittest.go``; measured on 3.15.1). Only the
    caller's test design can tell them apart (recipe-preview: two groups that
    assert opposite answers on one input), so this proof returns each group's
    pass / fail and judges nothing more.

    It does prove the report is the run asked for: one ``<testsuite>``, its
    testcases exactly the groups named in ``expect["cases"]`` (each once —
    promtool names a case after its group), no ``<error>`` (``ts.Abort``: the
    file itself could not be read or parsed) and no ``<skipped>``, and an exit
    status that agrees (0 exactly when nothing failed).

    ⚠️ A ``rule_files`` entry that does not exist is not an error to promtool:
    it loads no rules (rc 0 if the assertions then hold). Nothing here can see
    that; the caller must assert something only its rule file produces."""
    _requires(proc, "--junit")
    try:
        # The report promtool just wrote into the caller's temp dir, not
        # outside input (same footing as coverage_delta's Cobertura read).
        root = ElementTree.parse(expect["junit"]).getroot()  # nosec B314  # promtool's own report in the caller's mkdtemp
    except (OSError, ElementTree.ParseError) as exc:
        raise NoProof(f"no readable JUnit report: {exc}") from None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if len(suites) != 1:
        raise NoProof(f"{len(suites)} test suites in the JUnit report, expected 1")
    cases = suites[0].findall("testcase")
    error = suites[0].find("testcase/error")
    if error is not None:
        raise NoProof("promtool could not run the test: " + (error.text or "").strip()[:300])
    if suites[0].find("testcase/skipped") is not None:
        raise NoProof("a test group was skipped")
    names = [c.get("name") for c in cases]
    if None in names or sorted(names) != sorted(expect["cases"]):
        raise NoProof(f"the report's test groups {sorted(map(str, names))} are not "
                      f"the ones the test file defines {sorted(expect['cases'])}")
    passed = {c.get("name"): c.find("failure") is None for c in cases}
    if proc.returncode not in (0, 1):   # 0 all held, 1 one failed (3.15.1); else killed / crashed
        raise NoProof(f"exit status {proc.returncode} is neither a pass nor a test failure")
    if (proc.returncode == 0) != all(passed.values()):
        raise NoProof(f"exit status {proc.returncode} disagrees with the report")
    return passed


#: (engine, subcommand) → proof. The key is the pair, not the engine: one
#: engine's subcommands prove a run differently (helm ``template`` vs
#: ``package``; node runs a different driver for each tool).
PROOFS: dict[tuple[str, str], Callable[[subprocess.CompletedProcess, dict], Any]] = {
    ("hadolint", "lint"): _hadolint,
    ("helm", "template"): _helm_template,
    ("helm", "show-chart"): _helm_show_chart,
    ("helm", "push"): _helm_push,
    ("kube-linter", "lint"): _kube_linter_lint,
    ("mmdc", "render"): _mmdc_render,
    ("node", "esbuild-parse"): _node_esbuild_parse,
    ("node", "cli-playground"): _node_cli_playground,
    ("pint", "lint"): _pint_lint,
    ("promtool", "check-rules"): _promtool_check_rules,
    ("promtool", "test-rules"): _promtool_test_rules,
}
