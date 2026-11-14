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
JSON array and exits 0, and reports the caller's canary on stdin at the
registered levels (#3643); ``pint lint`` logs ``entries=N``. A call whose
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
import os
import re
import shutil
import subprocess
import tempfile
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
    unusable = _on_path_not_executable(binary)
    if unusable:
        # Installed, but nothing may execute it (#3539): "install it" would
        # send the operator after a binary they already have.
        return NoVerdict(f"cannot run {engine}: {unusable} is on PATH but is "
                         "not executable (no execute permission)")
    how = f"install the {binary} binary" + (
        ", or Docker for the containerised fallback" if docker_image else "")
    return NoVerdict(f"{engine} is not available ({how})")


def _on_path_not_executable(binary: str) -> Optional[str]:
    """The first file named *binary* in a PATH directory that this process
    may not execute (the kernel's EACCES) — asked when ``which`` found none
    to run. The mode is measured here, not inferred from ``which``'s answer."""
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        candidate = os.path.join(directory or os.curdir, binary)
        if os.path.isfile(candidate) and not os.access(candidate, os.X_OK):
            return candidate
    return None


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
        timeout: float, cwd: Optional[str] = None,
        docker_run: Sequence[str] = (), entrypoint: Optional[str] = None,
        stdin_text: Optional[str] = None, env: Optional[dict] = None,
        expect: Optional[dict] = None, capture: str = "pipe",
        no_stdin: bool = False) -> Result:
    """Run *located* and judge the result with the proof registered for
    ``(engine, subcommand)``. Never raises for an engine failure: a binary
    that cannot start, a timeout, and output without proof are all
    :class:`NoVerdict`.

    *expect* is what the proof checks the output against beyond its shape —
    how many inputs were sent (``inputs``), the file the run must write
    (``output``).

    *capture* ``"file"`` hands the engine files for stdout and stderr, read
    once the engine itself has exited (:func:`_run_to_files`); the default
    reads them through pipes. It needs a writable temporary directory; none
    is a NoVerdict of its own.

    The engine's stdin is *stdin_text* when given; otherwise the caller's
    own stdin (an engine that prompts — an exec credential plugin under
    kubectl — can), or ``/dev/null`` with *no_stdin*.

    An engine killed by a signal is a NoVerdict that says so: it ran, and
    how far it got is unknown (for a write: the request may have been sent)."""
    proof = PROOFS[(located.engine, subcommand)]
    cmd = argv(located, args, docker_run=docker_run, entrypoint=entrypoint)
    via = "" if located.mode == "binary" else " (docker)"
    if capture not in ("pipe", "file"):
        raise ValueError(f"capture is 'pipe' or 'file', not {capture!r}")
    stdin = subprocess.DEVNULL if no_stdin and stdin_text is None else None
    sinks = None
    if capture == "file":
        # Outside the try below: an OSError here is about the temporary
        # directory, not about starting the engine (review of #3539).
        try:
            sinks = (tempfile.TemporaryFile(), tempfile.TemporaryFile())
        except OSError as exc:
            return NoVerdict(f"cannot capture the output of {located.engine}{via}: "
                             f"no temporary file could be created ({exc})")
    try:
        if sinks is not None:
            proc = _run_to_files(cmd, sinks, cwd=cwd, stdin_text=stdin_text, stdin=stdin,
                                 timeout=timeout, env=env)
        elif stdin_text is None:
            proc = subprocess.run(cmd, cwd=cwd, stdin=stdin, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env)
        else:
            proc = subprocess.run(cmd, cwd=cwd, input=stdin_text, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # 0.375 → "0.375", 120 → "120", 1.12485 → "1.12" (a deadline's rest)
        within = f"{timeout:.0f}" if timeout >= 100 else f"{timeout:.3g}"
        return NoVerdict(f"{located.engine}{via} did not finish within {within}s "
                         f"(rc {_TIMEOUT_RC})")
    except OSError as exc:
        # ENOENT for a file `locate` just found: the kernel names the file
        # when what is missing is the interpreter on its `#!` line (#3534).
        # ⛔ Only when the working directory is there: a *cwd* that does not
        # exist is ENOENT too, and is not about the interpreter.
        hint = (" — the file is there; its `#!` interpreter is not?"
                if isinstance(exc, FileNotFoundError) and located.mode == "binary"
                and os.path.isfile(located.path)
                and (cwd is None or os.path.isdir(cwd)) else "")
        return NoVerdict(f"cannot run {located.engine}{via}: {exc}{hint} "
                         f"(rc {_NOT_RUNNABLE_RC})")
    finally:
        for sink in sinks or ():
            sink.close()
    if proc.returncode < 0:
        # ⛔ Not "did not run": it ran until the signal, and what it had done
        # by then is unknown (a kubectl write may have reached the cluster).
        return NoVerdict(
            f"{located.engine} {subcommand}{via} was killed by signal "
            f"{-proc.returncode}", (proc.stderr or "").strip()[-_STDERR_TAIL:])
    try:
        data = proof(proc, expect or {})
    except NoProof as exc:
        return NoVerdict(
            f"{located.engine} {subcommand}{via} did not run: {exc} "
            f"(exit {proc.returncode})",
            (proc.stderr or "").strip()[-_STDERR_TAIL:])
    return Ran(data, proc)


def _run_to_files(cmd: Sequence[str], sinks: tuple, *, cwd: Optional[str],
                  stdin_text: Optional[str], stdin: Optional[int],
                  timeout: float, env: Optional[dict]) -> subprocess.CompletedProcess:
    """``subprocess.run`` with stdout and stderr in the two files *sinks*,
    not pipes: the output is read once the engine itself has exited. A
    process the engine leaves behind still holding a pipe (an exec
    credential plugin's helper under kubectl) would otherwise hold the read
    open until it exits too — an answer reported as a timeout (#3349
    review). stdin is *stdin_text* (a character that is not UTF-8-encodable
    is sent as ``?``), else *stdin* (None: the caller's own). The text is
    decoded as UTF-8, newlines as written."""
    out, err = sinks
    if stdin_text is None:
        proc = subprocess.run(cmd, cwd=cwd, stdin=stdin, stdout=out,
                              stderr=err, timeout=timeout, env=env, check=False)
    else:
        proc = subprocess.run(cmd, cwd=cwd, input=stdin_text.encode("utf-8", "replace"),
                              stdout=out, stderr=err, timeout=timeout, env=env,
                              check=False)
    out.seek(0)
    err.seek(0)
    return subprocess.CompletedProcess(
        cmd, proc.returncode,
        out.read().decode("utf-8", "replace"),
        err.read().decode("utf-8", "replace"))


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


#: The name hadolint reports a Dockerfile read from stdin under (measured,
#: v2.12.0: ``"file": "-"``, whatever its position among the file arguments).
_HADOLINT_STDIN = "-"


def _hadolint(proc: subprocess.CompletedProcess, expect: dict) -> list[dict]:
    """``hadolint --no-fail --format json <files> -``: exit 0 means "ran"
    (--no-fail), and stdout is a JSON array (a clean run prints ``[]``) whose
    items carry the fields every finding has — one without ``level`` would be
    classified as the lowest severity (PR #3610 review).

    ⛔ That it ran does not show it ran **with the caller's levels** (#3643):
    a config file hadolint cannot parse is dropped whole, a mistyped key or
    rule code is dropped silently, ``HADOLINT_*`` variables override the
    command line, and an inline ``# hadolint ignore=`` removes a finding —
    all at exit 0 with a well-formed array (measured, v2.12.0 and v2.14.0).
    So the same run also lints a canary Dockerfile the caller sends on stdin,
    and ``expect["canary"]`` (rule code → the levels it must come back with,
    one per expected hit) is compared with what came back for ``-``: only
    ``code`` and ``level`` are read, no message text (#3611 Q2). A code that
    is missing, comes back at another level, or comes back more often than
    expected is no verdict. The canary's own findings are then removed, so
    ``data`` is the findings on the caller's files only."""
    _requires(proc, "--no-fail", "--format", "json", "--disable-ignore-pragma",
              _HADOLINT_STDIN)
    canary = expect.get("canary")
    if not canary:
        raise ValueError("the hadolint proof needs expect['canary'] "
                         "(rule code -> expected levels on stdin)")
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
    for code, levels in canary.items():
        got = sorted(f["level"] for f in doc
                     if f["file"] == _HADOLINT_STDIN and f["code"] == code)
        if got != sorted(levels):
            raise NoProof(
                f"the canary on stdin came back with {code} at {got}, expected "
                f"{sorted(levels)}: the registered levels / trusted registries "
                "were not the ones applied")
    return [f for f in doc if f["file"] != _HADOLINT_STDIN]


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


def _kubectl_exited(proc: subprocess.CompletedProcess, expect: dict) -> subprocess.CompletedProcess:
    """Any ``kubectl`` verb: it ran to an exit status of its own. kubectl's
    exit status **is** its answer, and what a non-zero one means differs by
    verb and caller (a read that got no answer; ``create --dry-run=client``
    refusing a key name, which is a finding on the caller's files; a write
    the cluster refused), so the status is the caller's to judge —
    ``_lib_kubectl`` does it for reads. The runs without one are the ones
    :func:`run` names itself, before any proof: not startable, not finished
    in time, killed by a signal (#3539)."""
    return proc


#: (engine, subcommand) → proof. The key is the pair, not the engine: one
#: engine's subcommands prove a run differently (helm ``template`` vs
#: ``package``; node runs a different driver for each tool).
PROOFS: dict[tuple[str, str], Callable[[subprocess.CompletedProcess, dict], Any]] = {
    ("hadolint", "lint"): _hadolint,
    ("helm", "template"): _helm_template,
    ("helm", "show-chart"): _helm_show_chart,
    ("helm", "push"): _helm_push,
    ("kube-linter", "lint"): _kube_linter_lint,
    ("kubectl", "apply"): _kubectl_exited,
    ("kubectl", "create"): _kubectl_exited,
    ("kubectl", "delete"): _kubectl_exited,
    ("kubectl", "get"): _kubectl_exited,
    ("kubectl", "label"): _kubectl_exited,
    ("kubectl", "logs"): _kubectl_exited,
    ("kubectl", "patch"): _kubectl_exited,
    ("mmdc", "render"): _mmdc_render,
    ("node", "esbuild-parse"): _node_esbuild_parse,
    ("node", "cli-playground"): _node_cli_playground,
    ("pint", "lint"): _pint_lint,
    ("promtool", "check-rules"): _promtool_check_rules,
    ("promtool", "test-rules"): _promtool_test_rules,
}
