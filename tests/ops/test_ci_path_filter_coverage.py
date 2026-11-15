"""Drift guard for workflow-level path filters (`on.pull_request.paths`).

One invariant: **every `scripts/**` file a workflow's steps execute is covered
by that workflow's own `on.pull_request.paths`** (#3601). A workflow that
gates itself with GitHub's `paths:` never starts on a PR that touches only a
script it runs, so a break in that script merges with the workflow never
having looked. Scope, refusals and the two ledgers are spelled out at the
invariant itself, in the second half of this file; the first half is the
extraction it grades with ("which tracked file does this step run").

⛔ What is NOT here any more, and why (#3756). Until then this module held
three more invariants, all about the `dorny/paths-filter` job-level gates in
ci.yml, docs-ci.yaml and validate.yaml:

  1. every out-of-tree file a portal Vitest reads is in the `portal` filter;
  2. every out-of-tree file a pytest reads is in the `python` filter;
  3. every file a path-gated job executes or installs from triggers the
     condition that gates it.

Each existed for one failure mode: `detect-changes` answers "not needed" for a
leg that was needed, the leg is skipped, and the always-run aggregate turns
that skip into a green required check. Those gates are retired — every PR
runs every required leg, and the aggregates pass on `success` only — so the
three invariants have nothing left to grade. What keeps the gates from
coming back is `tests/ops/test_ci_aggregate_gate_contract.py`.

The numbering is kept ("invariant 4") because other files cite it by that
name. (Renamed from `test_portal_path_filter_coverage.py` in #1373.)

⚠️ Other modules import helpers from here by name — `_load_workflow`,
`_logical_shell_lines`, `_tracked_files`, `_match_segments`,
`reject_unmodelled_patterns`. They stay here because moving them is a
refactor nobody asked for; removing one breaks an importer loudly.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from _pysource import parse_py

ROOT = Path(__file__).resolve().parents[2]
_GLOB_SPEC = importlib.util.spec_from_file_location(
    "paths_filter_glob", ROOT / "scripts" / "ops" / "paths_filter_glob.py")
_GLOB = importlib.util.module_from_spec(_GLOB_SPEC)
# Registered under its own name: workflow_path_filter.py imports it by that
# name, and must get THIS object — two module objects would be two `Unmodelled`
# classes, and an `except` on one would not catch the other.
sys.modules.setdefault("paths_filter_glob", _GLOB)
_GLOB = sys.modules["paths_filter_glob"]
if not hasattr(_GLOB, "covers"):
    _GLOB_SPEC.loader.exec_module(_GLOB)
sys.path.append(str(ROOT / "scripts" / "ops"))
import workflow_path_filter as _WPF  # noqa: E402  (scripts/ops, appended above)


@lru_cache(maxsize=None)
def _load_workflow(path: Path) -> dict:
    """Parse a workflow once. `ci.yml` is long and the importers of this
    helper (tests/_ci_lint_wiring.py and others) want it per job."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_MIN_TRACKED = 1000


@lru_cache(maxsize=1)
def _tracked_files() -> tuple[str, ...]:
    """Every tracked path, from git rather than from a walk.

    ⛔ `-z` AND the NUL split — the half-fix yields ONE element holding the
    whole listing. `_MIN_TRACKED` is the tripwire, hard-coded not derived.
    """

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
            # ⛔ Not optional: an inherited invalid stdin handle makes
            # subprocess raise WinError 6 on some Windows pytest runners, and
            # the clause below then misreports it as "not a git checkout".
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="surrogateescape", check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(
            "`git ls-files -z` failed, so the tracked-file set "
            "is unavailable. ⚠️ Read the "
            "underlying error first: no checkout, a subprocess-layer failure "
            f"and a stalled FUSE mount all land here. {exc!r}"
        ) from exc
    return _split_tracked(out)


def _split_tracked(raw: str) -> tuple[str, ...]:
    """Pure, so the floor below can be handed degenerate input."""
    files = tuple(sorted(p for p in raw.split("\0") if p))
    assert len(files) >= _MIN_TRACKED, (
        f"`git ls-files -z` yielded {len(files)} path(s), which is not a "
        "plausible listing of this repo: a truncated or unsplit listing makes "
        "every `all(...)` judgement below trivially true and the module green "
        "and quiet. Check the split matches the flag (`-z` -> NUL, no flag -> "
        "newline)."
    )
    # ⛔ The floor alone is not enough: slicing to `[:_MIN_TRACKED]` satisfies
    # it exactly. Cross-check the SEPARATOR COUNT, derived from the same input
    # by a different route. ⚠️ Honest bound: this defeats truncation of the
    # LIST, not of the INPUT — truncating `raw` earlier passes both sides.
    assert len(files) >= raw.count("\0"), (
        f"the listing contains {raw.count(chr(0))} NUL separators but only "
        f"{len(files)} paths survived: something is dropping entries, and a "
        "shortened sweep passes every `all(...)` below for what it skips."
    )
    return files


def test_tracked_split_refuses_an_unsplit_listing() -> None:
    """The floor fires on the shape that actually happens: `-z` with a newline
    split (the natural half-fix for `core.quotePath`) yields ONE element.
    """
    blob = "\0".join(f"zdir/zfile{i}.txt" for i in range(2000))

    assert len(_split_tracked(blob)) == 2000

    # the half-fix: NUL-joined data split as if it were newline-separated
    with pytest.raises(AssertionError, match="not a\n?\\s*plausible listing"):
        _split_tracked(blob.replace("\0", "\n"))

    # and an outright empty listing, the other way a broken call looks
    with pytest.raises(AssertionError, match="plausible listing"):
        _split_tracked("")


def test_tracked_set_matches_an_independent_git_listing() -> None:
    """⛔ Both guards above live INSIDE `_split_tracked`, so anything one frame
    out is unguarded: a pathspec on the argv, a slice, or a filter on the
    return each narrowed the set with the whole suite green (blind review).

    Independent BY CONSTRUCTION — its own argv, its own split. Building the
    expected side from a module helper would compare a narrowing to itself.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="surrogateescape", check=True, timeout=120).stdout
    expected = {p for p in out.split("\0") if p}
    actual = set(_tracked_files())
    assert actual == expected, (
        f"`_tracked_files()` is not `git ls-files -z`: {len(actual)} path(s) "
        f"against {len(expected)}. Every `all(...)` below then quantifies over "
        "a SUBSET and passes for what it skips.\n"
        f"    dropped: {sorted(expected - actual)[:10]}\n"
        f"    extra:   {sorted(actual - expected)[:10]}")


def test_tracked_files_is_never_sliced_at_a_use_site() -> None:
    """The same disarm one frame further out, where no in-helper guard sees it.

    ⛔ `for path in _tracked_files()[:1500]` is a two-character edit at the
    CALL site; the floor and the NUL cross-check both still pass.

    ⚠️ HONEST BOUND, because the first version claimed more than it did: this
    recognises the direct subscript, a local bound from the call and then
    subscripted, and a subscript on a wrapper (`sorted(...)`, `list(...)`).
    It does NOT model `itertools.islice`, a filtering comprehension, or an
    `enumerate`/`break`. Those remain unmodelled. What this buys is the
    plausible "cap the one O(files x patterns) call" edit, not a proof.
    """
    tree = parse_py(Path(__file__).resolve())

    def _is_call(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_tracked_files")

    bound = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and _is_call(node.value)
        for target in node.targets if isinstance(target, ast.Name)
    }
    sliced = sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and (_is_call(node.value)
             or (isinstance(node.value, ast.Name) and node.value.id in bound)
             or (isinstance(node.value, ast.Call)
                 and any(_is_call(a) for a in node.value.args)))
    )
    assert not sliced, (
        f"`_tracked_files()` is subscripted at line(s) {sliced}. Slicing the "
        "QUANTIFIER makes every `all(...)`/`any(...)` over it silently PARTIAL: "
        "the assertion still reads as a statement about every tracked file "
        "while asking it of a subset. Iterate the whole set.")


def reject_unmodelled_patterns(patterns: Iterable[object], where: str) -> None:
    """Refuse to grade a `!`-prefixed pattern rather than silently mis-grade it.

    ⛔ Public because `tests/lint/test_e2e_spec_lint.py` asks the same question
    of a workflow's `on.*.paths` list, and a second copy of this rule is a
    second answer. The shape it rejects is the one that cannot make any caller
    red: every caller wraps `_match_segments` in `any()`, `_match_segments`
    reads `!x` as the literal segment `"!x"` and answers False, and the
    remaining positive patterns still report "covered" — so the single pattern
    form that REMOVES coverage is absorbed. Measured on playwright.yml: adding
    `- '!tests/e2e/**'` beside `- 'tests/e2e/**'` (2 places) left this module at
    green and so was the twin, with A-13's only server-side execution point
    switched off.
    """
    negated = sorted(
        str(p) for p in patterns if isinstance(p, str) and p.startswith("!"))
    if negated:
        raise AssertionError(
            f"{where}: exclusion patterns {negated} cannot be expressed by the "
            "OR-over-positive-patterns model these assertions use — an excluded "
            "path would still report as covered. Model exclusion before adding "
            "one; do not silence this by dropping the pattern from the list "
            "being checked.")


@lru_cache(maxsize=None)
def _is_tracked_file(rel: str) -> bool:
    """Exists, is a file, and is spelled exactly as on disk.

    Every filesystem call is guarded: a candidate that the OS refuses to even
    stat is simply not a path, and must not become an error in a drift guard.

    ⛔ Absolute and `..` candidates are rejected UP FRONT, not left to the
    segment walk. `ROOT / "/tmp/x.sh"` is `/tmp/x.sh` on POSIX — pathlib drops
    the left side — so `validate.yaml`'s real `sh /tmp/golangci-install.sh`
    step would `is_file()` a live SYSTEM path. Today the walk happens to reject
    it (`"".split("/")` yields an empty first segment that matches no entry),
    i.e. it is correct by accident of ordering. Make it correct on purpose.
    """
    if rel.startswith("/") or ".." in rel.split("/"):
        return False
    try:
        target = ROOT / rel
        if not target.is_file():
            return False
    except OSError:
        return False
    probe = ROOT
    for part in rel.split("/"):
        if part not in _dir_entries(probe):
            return False
        probe = probe / part
    return True


@lru_cache(maxsize=None)
def _dir_entries(directory: Path) -> frozenset[str]:
    """Cached, case-exact listing. The walk above asks for the same ancestor
    directories thousands of times — once per segment per candidate across the
    whole `tests/` tree — and each ask was a fresh `iterdir()`."""
    try:
        return frozenset(p.name for p in directory.iterdir())
    except OSError:
        return frozenset()


# ── which tracked file does a step run ─────────────────────────────────────
#
# Invariant 4 asks this of every step of every workflow that carries
# `on.pull_request.paths`. Same best-effort posture throughout: only literal,
# existing paths are seen. A path built at runtime, or one reached through a
# script the step merely invokes, is invisible — under-detection, the safe
# direction (the failure is "not asserted", never a false red).

# ⛔ EXECUTED or INSTALLED-FROM, never merely MENTIONED. The first version of
# this extraction took any tracked-file token anywhere in the job, and that
# predicate is wrong in both directions:
#   * `run: echo "::error::see docs/internal/foo.md for the fix"` would demand
#     a runbook in the workflow's `paths:` — a false red on a PR that only
#     improved an error message, with a failure message telling the maintainer
#     to make the filter WORSE. (`#` lines inside a `run: |` block are string content,
#     not YAML comments, so every prose comment was a candidate too.)
#   * `pytest --ignore=<some test file>` names files the job
#     deliberately does NOT run — an exclusion read as a dependency.
# So match only the idioms that actually make a file decide what a job does.
# There are THREE, and `_command_files` yields from each: an interpreter's
# script argument, pip's constraints/requirements file, and a `./script` run
# directly in command position. ⛔ All three are kept: an earlier version of
# this comment said "two", and the third is the one fewest workflows
# exercise, so a maintainer trusting the count would find little using it
# and delete it.
INTERPRETERS = {"python", "python3", "py", "bash", "sh", "node", "ruby", "perl"}
# Interpreter flags whose VALUE is a separate token. Without these, the
# first-non-flag rule took the value as the script and lost the real one:
# bench-probe-write-latency.yaml runs
# `python3 -X utf8 scripts/tools/dx/analyze_probe.py`,
# and the scan saw `utf8` (#3601 review).
PYTHON_INTERPRETERS = {"python", "python3", "py"}
SHELL_INTERPRETERS = {"bash", "sh"}
INTERPRETER_VALUE_FLAGS = {
    "python": {"-X", "-W"},
    "shell": {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"},
}
# `bash -eo pipefail x.sh`: a short-option cluster whose LAST letter is o/O
# takes the next token as its value.
SHELL_VALUE_CLUSTER = re.compile(r"[-+][A-Za-z]*[oO]")
DEPENDENCY_FLAGS = {"-c", "--constraint", "-r", "--requirement"}
PIP_COMMAND = re.compile(r"pip3?")
# `python3 "$GITHUB_WORKSPACE/scripts/ops/ci_flake_retry.py"` is a literal path
# behind a fixed prefix. Without this strip a step spelled that way yields
# ZERO files and its coverage assertion is entirely vacuous.
# ⛔ Named variables only. Stripping ANY `$VAR/` turns `"$RUNNER_TEMP/Makefile"`
# — a scratch file that merely shares a name with a repo file — into a demanded
# filter entry. These two are the ENV-VAR spellings of the checkout root.
VAR_PREFIX = re.compile(r"^\$\{?(?:GITHUB_WORKSPACE|CI_PROJECT_DIR)\}?/")

# ⛔ The same root has a THIRD spelling, and an earlier version of the comment
# above claimed the two env vars were "the ones that actually denote the
# checkout" — false: `${{ github.workspace }}` is the GitHub-expression form and
# this repo's workflows use it (e.g. bench-record.yaml). It cannot
# be handled by VAR_PREFIX, because `${{ github.workspace }}/x` contains spaces:
# tokenising splits it into `${{`, `github.workspace`, `}}/x`, the interpreter
# branch takes `${{` as the script argument and stops — the real path never
# becomes a candidate at all. So it is normalised out of the RAW TEXT, before
# any splitting. Blind review, and not hypothetical for long: the moment a
# step spells its script that way, its coverage assertion goes vacuous silently.
# ⚠️ Only this one expression is modelled. Any other `${{ … }}` around a path is
# still invisible; there is no general answer without evaluating the expression.
WORKSPACE_EXPR = re.compile(r"\$\{\{\s*github\.workspace\s*\}\}/")


def _step_files(step: dict) -> set[str]:
    """Repo-relative tracked files ONE step's `run:` executes or installs from.

    Policy about the step (its `if:`, `continue-on-error`) is the caller's
    business: invariant 4 asks only whether the WORKFLOW starts, which a
    step-level condition cannot change.
    """
    found: set[str] = set()
    work_dir = str(step.get("working-directory") or "").strip("/")
    for candidate in _run_script_files(step.get("run") or ""):
        for resolved in (f"{work_dir}/{candidate}" if work_dir else None,
                         candidate):
            if resolved and _is_tracked_file(resolved):
                found.add(resolved)
                break
    return found


SHELL_SEPARATORS = re.compile(r"\s(?:&&|\|\||[;|])\s|\s*[;|]\s*")


def _shell_commands(line: str):
    """Split a logical line into individual commands.

    ⛔ Scanning per LINE instead of per COMMAND is what let two narrowings leak:
    `./x` was harvested from any argument position (so
    `echo "see ./docs/foo.md"` demanded a runbook — the exact prose
    false-positive this predicate exists to avoid), and a `-c` flag was
    harvested from any line merely CONTAINING the word `pip`
    (`pip --version && tox -c reqs.txt`). Both are "which command owns this
    token" questions, and neither is answerable from the line.

    Over-splitting is safe: a repo path never spans a command separator, so the
    worst case is under-detection, this module's stated safe direction.
    """
    for part in SHELL_SEPARATORS.split(line):
        tokens = part.split()
        if tokens:
            yield tokens


def _run_script_files(script: str):
    """Yield candidate repo paths from a shell script's command arguments."""
    script = WORKSPACE_EXPR.sub("", script)
    for line in _logical_shell_lines(script):
        for tokens in _shell_commands(line):
            yield from _command_files(tokens)


# ⛔ SKIP-LISTS, so the failure directions are the opposite of a root-name
# list: over-inclusion costs nothing (skipping a token that was really a
# command loses at most that command), under-inclusion silently blinds the
# scanner. That is why inert members are tolerated here and forbidden in
# an allowlist. ⚠️ Measured per member (remove one, re-run), before #3756:
# against this module's own fixtures exactly `then`, `do`, `sudo`, `env`
# and `xargs` are load-bearing. Two earlier versions of this comment got
# that list wrong in both
# directions — state it as a measurement or not at all.
#
# ⭐ The structure behind it is more useful than the list: only a keyword that
# IMMEDIATELY PRECEDES a command matters. `if`/`while`/`until`/`for` are
# followed by a condition or a word list, so skipping them changes nothing —
# the unrecognised head is not an interpreter either way. `then` and `do` are
# the ones a command actually follows.
#
# ⛔ And this IS an enumeration, with known gaps.
# Mostly under-detection: a wrapper flag that
# takes a SEPARATE value (`sudo -u runner bash x.sh`, `xargs -n 1 bash x.sh`),
# a wrapper with a mandatory POSITIONAL argument (`timeout 600 bash x.sh` —
# which is why `timeout`/`nice`/`stdbuf` are not listed: every real spelling
# carries the argument, so listing them would help in no case at all, the same
# argument that removed the `[` branch), `source`/`.`, an interpreter flag with
# a separate value OTHER than those in `INTERPRETER_VALUE_FLAGS` (python's
# `-X`/`-W`, the shells' `-o`/`-O`/`--rcfile`/`--init-file` and a `-eo`-style
# cluster are modelled since #3601's review), `-m pip` anywhere but
# immediately after the interpreter, `uv pip`, `case … in` bodies, a subshell
# with no inner separator (`(bash x.sh)` — the pinned fixture uses
# `(cd x && bash x.sh)`, which `SHELL_SEPARATORS` splits first, so the plain
# form is untested), a lone `&` background operator, a nested command context
# (`bash -c "bash x.sh"`), and a variable verb (`$PYTHON x.py`).
#
# ⚠️ One gap runs the OTHER way, so "all under-detection" would be wrong: for
# a value-taking flag NOT in `INTERPRETER_VALUE_FLAGS`, the flag-skipping loop
# takes the flag's VALUE as the script. Weak in practice — a file handed to a
# flag is usually a real dependency — but it is over-detection and belongs on
# the list. (`bash --rcfile x.sh -c true`, the example that used to stand here,
# is now modelled.)
#
# Resolving these properly means parsing shell, which is not worth it here;
# what matters is that the list does not pretend to be complete.
ENV_PREFIXES = {"sudo", "env", "exec", "time", "xargs", "nohup", "command"}
# Shell keywords that can stand where a command does, once `_shell_commands`
# has split on `;`. A `run: |` step wrapped in `if … then … fi` is an ordinary
# refactor, and without these the wrapped command becomes invisible.
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while",
                  "for", "until", "case", "esac", "{", "}", "(", ")", "!"}
ASSIGNMENT = re.compile(r"^\w+=")


def _command_verb(tokens: list[str]) -> tuple[int, str | None]:
    """(index, basename) of the word this command actually runs.

    ⛔ Skips everything that can legitimately precede the verb: wrappers
    (`sudo`, `env`, `nohup`, …), THEIR flags (`sudo -E bash x.sh` — four of the
    five wrappers were blind in their flagged spelling), `VAR=value`
    assignments, and shell keywords left at the head of a command by the
    `;`-split (`if [ -f x ]; then bash x.sh; fi`). Each of those was a separate
    member of the same "which token is the command" class; the class is
    enumerated here rather than discovered one review round at a time.

    ⚠️ An unrecognised head is returned AS the verb — it is not skipped, and
    `(0, None)` happens only if every token was skipped. That matters because
    `_command_files` uses `tokens[verb_index]` for the `./` branch whether or
    not the verb was recognised. The safe behaviour comes from the verb then
    failing every downstream test, not from refusing to name one.
    """
    skipping_wrapper = False
    for index, token in enumerate(tokens):
        bare = token.strip("'\"")
        if bare in SHELL_KEYWORDS or ASSIGNMENT.match(bare):
            skipping_wrapper = False
            continue
        if bare in ENV_PREFIXES:
            skipping_wrapper = True
            continue
        # a flag belonging to the wrapper we just skipped, not to the verb
        if skipping_wrapper and bare.startswith("-"):
            continue
        # ⛔ No `[` branch here. It was dead for every real spelling — after
        # `[` the next token is a test operator, which becomes the verb and is
        # neither an interpreter nor `./` — and its ONE reachable effect was
        # wrong: `[ ./x.sh ]` is a `test`, not an execution, and it made the
        # path a dependency. An inert branch whose only live behaviour is a
        # false positive is worse than no branch.
        return index, bare.rsplit("/", 1)[-1]
    return 0, None


def _command_files(tokens: list[str]):
    """Candidate paths from ONE command's tokens."""
    # ⛔ Direct execution, COMMAND POSITION ONLY. `./scripts/ops/x.sh` runs the
    # file; the same token as an argument is being passed, not run — and
    # accepting it anywhere made `echo "::error::see ./docs/foo.md"` demand a
    # runbook, the precise prose false-positive this predicate exists to avoid.
    verb_index, verb = _command_verb(tokens)

    # ⛔ The verb, not `tokens[0]` — otherwise `sudo ./x.sh` and
    # `env FOO=1 ./x.sh` miss, while the two sibling predicates below handle
    # them. Third member of the same "which token is the command" class.
    head = tokens[verb_index].strip("'\"")
    if head.startswith("./"):
        yield _normalise_token(head[2:])

    # ⛔ `pip` as a WORD, and as THIS command's word — not merely present
    # somewhere on the line. `pipefail`/`pipeline`/`pipx` satisfy a substring
    # test, and `pip --version && tox -c reqs.txt` satisfies a line-scoped one.
    #
    # ⛔ …and `python -m pip` IS pip. Keying only on the verb missed it, and
    # `ci.yml` uses that spelling (`python3 -m pip install --quiet pyyaml -c
    # requirements/ci-constraints.txt`) — the constraints file vanished from a
    # form the surrounding comment claims to handle. Of the four spellings in
    # practice (`pip`, `pip3`, `python -m pip`, `python3 -m pip`) two were
    # unhandled and one of those is already in the tree.
    is_pip = (verb is not None and PIP_COMMAND.fullmatch(verb) is not None) or (
        verb in INTERPRETERS
        and tokens[verb_index + 1:verb_index + 3] == ["-m", "pip"])

    # ⛔ An interpreter counts in COMMAND POSITION ONLY. Scanning every token
    # for one meant a message that SHOWS the fix became a dependency:
    #   echo "::error::spec drift. Run python3 scripts/tools/dx/bump_docs.py"
    # yielded `bump_docs.py`. Naming the fix command in `::error::` is this
    # repo's convention (ci.yml does it), so this was live, not
    # hypothetical — safe only by the accident that ci.yml's instances
    # named `make`, which is not an interpreter. Third instance of the same
    # position bug in this one function, after `./` and `pip`.
    if verb is not None and verb in INTERPRETERS:
        # The first NON-FLAG argument, and only that one. Flags are skipped
        # rather than stopping the scan, so `python3 -u x.py` and `bash -x
        # x.sh` still resolve; stopping at the first flag lost those silently.
        # Taking only the first non-flag argument is what keeps `bash deploy.sh
        # docs/runbook.md` from demanding the runbook — a false red whose
        # remedy would be to widen the filter. (`python3 -m pytest tests/`
        # yields `pytest`, not a tracked file.)
        expects_value = False
        for following in tokens[verb_index + 1:]:
            if expects_value:
                expects_value = False
                continue
            if following.startswith("-") or (
                    verb in SHELL_INTERPRETERS and following.startswith("+")):
                expects_value = _flag_takes_value(verb, following)
                continue
            yield _normalise_token(following)
            break

    if not is_pip:
        return
    for index, token in enumerate(tokens):
        if token in DEPENDENCY_FLAGS and index + 1 < len(tokens):
            yield _normalise_token(tokens[index + 1])
        elif "=" in token and token.split("=", 1)[0] in DEPENDENCY_FLAGS:
            yield _normalise_token(token.split("=", 1)[1])


def _flag_takes_value(verb: str, flag: str) -> bool:
    """Does this interpreter flag consume the NEXT token as its value?"""
    if verb in PYTHON_INTERPRETERS:
        return flag in INTERPRETER_VALUE_FLAGS["python"]
    if verb in SHELL_INTERPRETERS:
        return (flag in INTERPRETER_VALUE_FLAGS["shell"]
                or SHELL_VALUE_CLUSTER.fullmatch(flag) is not None)
    return False


def _logical_shell_lines(script: str) -> list[str]:
    """Join trailing-backslash continuations, as `check_unpinned_deps` does,
    then drop comment lines.

    `pip install pyyaml \\`⏎`  -c requirements/ci-constraints.txt` is ONE
    command; scanning the physical lines separately loses the `-c` file,
    because the continuation line does not contain the word `pip`.

    Three shell rules this has to respect, because getting any of them wrong
    silently DELETES a command from the scan (and the legs that would notice
    are the ones with the fewest pins):

      * a `#` comment ends at its newline — a trailing `\\` does NOT continue
        it. Joining first and testing `#` afterwards let one prose line eat the
        real command below it.
      * an EVEN number of trailing backslashes is an escaped backslash, not a
        continuation (`echo end\\\\`).
      * whitespace AFTER the backslash means bash does not continue either, so
        strip only the line terminator before testing.
    """
    lines, buffer, joining = [], "", False
    for raw in script.splitlines():
        stripped = raw.rstrip("\r")
        is_comment = not joining and stripped.lstrip().startswith("#")
        continues = (not is_comment
                     and (len(stripped) - len(stripped.rstrip("\\"))) % 2 == 1)
        if is_comment:
            buffer, joining = "", False
            continue
        buffer += stripped[:-1] + " " if continues else stripped
        if continues:
            joining = True
            continue
        lines.append(buffer)
        buffer, joining = "", False
    if buffer:
        lines.append(buffer)
    return lines


def _normalise_token(token: str) -> str:
    # Trailing shell punctuation is not part of the path: `python3 x.py; echo`
    # tokenizes to `x.py;`, which is not a tracked file, so the dependency
    # vanished silently. `validate.yaml::go-lint` extracts exactly ONE file, so
    # one stray `;` there would empty the leg.
    return VAR_PREFIX.sub("", token.strip().strip("'\"").strip(";|&()"))


# The matcher itself lives in scripts/ops/paths_filter_glob.py so
# scripts/ops/workflow_path_filter.py uses the SAME definition of "covered"
# without importing this pytest module. Its docstring carries the ⛔ notes
# that used to sit here; the pins below still exercise it through these names.
_covers = _GLOB.covers
_match_segments = _GLOB.match_segments


def test_covers_matches_only_the_shapes_in_use() -> None:
    """`_covers` is the disarm surface for every coverage assertion.

    Each is a NEGATIVE assertion ("nothing uncovered"), so a
    `_covers` that says yes too often turns the whole module green and quiet —
    the opposite of a false red, and much harder to notice. Pin both
    directions: the shapes really in the filters must match, and everything
    else must NOT (an unrecognised glob is uncovered, i.e. loud).

    ⛔ Every path here is DELIBERATELY not a real repo path. `_covers` never
    touches the filesystem, so fixtures are pure strings and fake ones assert
    exactly as much — while a real one pollutes verify_diff's text_map as
    "a file this test reads". Keep them fictional.
    """
    assert _covers("Zfile", "Zfile")
    # ⛔ An exact-path entry covers THAT path, never its subtree — dorny
    # matches the literal only. Mutating `_match_segments`' base case to
    # `return True` (so a consumed pattern accepts any remaining segments) was
    # green: every other negative fixture here is killed by the suffix or
    # literal branch, none by the base case, so the one over-permissive
    # direction that would disarm the whole module was unpinned.
    assert not _covers("zdocs", "zdocs/index.zmd")
    assert not _covers("zreq/pins.txt", "zreq/pins.txt/deeper.txt")
    assert _covers("zreq/**", "zreq/pins.txt")
    assert _covers("zreq/**", "zreq")
    assert not _covers("zreq/**", "zreq-dev/x.txt")
    assert not _covers("zreq/**", "other/zreq/x.txt")

    # `**/`-rooted: basename match at any depth.
    assert _covers("**/zmod.txt", "zsrc/zpkg/zmod.txt")
    assert _covers("**/zmod.txt", "zmod.txt")
    assert not _covers("**/zmod.txt", "zsrc/zpkg/zsum.txt")
    assert not _covers("**/zmod.txt", "zmod.txt.bak")
    assert _covers("**/*.zpy", "zsrc/zlint/ztool.zpy")
    assert not _covers("**/*.zpy", "zreq/pins.txt")
    # ⛔ A bare ".zpy" IS matched by `*.zpy`: picomatch with `dot: true` (what
    # dorny/paths-filter passes) lets `*` match the empty string. This line
    # asserted the opposite until #3756, on the strength of a comment; it is
    # now measured against the real picomatch for every pattern in the
    # workflows (tests/ops/test_workflow_path_filter.py).
    assert _covers("**/*.zpy", ".zpy")

    # `prefix/**/*.ext`. `**` must also
    # absorb ZERO segments, or `zsrc/ztop.zpy` reads as uncovered.
    assert _covers("zsrc/**/*.zpy", "zsrc/zlint/ztool.zpy")
    assert _covers("zsrc/**/*.zpy", "zsrc/ztop.zpy")
    assert not _covers("zsrc/**/*.zpy", "zsrc/zlint/ztool.zsh")
    assert not _covers("zsrc/**/*.zpy", "zother/ztool.zpy")

    # `*.ext` is a SINGLE segment: it matches a file beside the prefix, never
    # one nested below it (picomatch's `*` does not cross a separator).
    assert _covers("zdocs/*.zmd", "zdocs/index.zmd")
    assert not _covers("zdocs/*.zmd", "zdocs/zsub/index.zmd")

    # `*` inside a segment and `[...]` classes (#3756): four patterns the
    # workflows really use were unmodelled, so they read as covering nothing —
    # and a dead-entry check asking this function would have called them dead.
    assert _covers("z*x/**", "zyx/a.txt")
    assert not _covers("z*x/**", "zyq/a.txt")
    assert _covers("zdocs/*", "zdocs/index.zmd")
    assert not _covers("zdocs/*", "zdocs/zsub/index.zmd")
    assert _covers("zops/_zgr_*.zpy", "zops/_zgr_core.zpy")
    assert not _covers("zops/_zgr_*.zpy", "zops/zgr_core.zpy")
    assert _covers("**/_*.[Yy][Aa][Mm][Ll]", "zconf/_zdefaults.YAML")
    assert _covers("**/_*.[Yy][Aa][Mm][Ll]", "_zdefaults.yaml")
    assert not _covers("**/_*.[Yy][Aa][Mm][Ll]", "zconf/zdefaults.yaml")
    assert not _covers("**/_*.[Yy][Aa][Mm][Ll]", "zconf/_zdefaults.yamlx")
    assert _covers("zdocs/z[a-c].zmd", "zdocs/zb.zmd")
    assert not _covers("zdocs/z[a-c].zmd", "zdocs/zd.zmd")
    # A regex metacharacter in a literal stays a literal.
    assert not _covers("zdocs/a.zmd", "zdocs/abzmd")
    assert not _covers("zdocs/z*.zmd", "zdocs/zabzmd")

    # Unrecognised shapes must fail loud (report uncovered), never be guessed —
    # and `unmodelled` must name each one, or the dead-entry check would read
    # "cannot say" as "matches nothing".
    for pattern, path in (
            ("zdocs/z?.zmd", "zdocs/za.zmd"),      # GitHub: zero-or-one; picomatch: any one
            ("zdocs/z+.zmd", "zdocs/zz.zmd"),
            ("zdocs/{a,b}.zmd", "zdocs/a.zmd"),
            ("zdocs/[!a].zmd", "zdocs/b.zmd"),
            ("zdocs/[^a].zmd", "zdocs/b.zmd"),
            ("zdocs/**.zmd", "zdocs/a.zmd"),
            ("zdocs**/a.zmd", "zdocs/a.zmd"),
            ("./zdocs/a.zmd", "zdocs/a.zmd"),
            ("zdocs/", "zdocs"),
            ("zdocs/[a.zmd", "zdocs/[a.zmd"),
            ("zdocs/[z-a]*.zmd", "zdocs/zb.zmd")):     # a reversed range: no traceback
        assert not _covers(pattern, path), pattern
        assert _GLOB.unmodelled(pattern), pattern
    for pattern in ("Zfile", "zreq/**", "**/*.zpy", "z*x/**", "zdocs/*",
                    "**/_*.[Yy][Aa][Mm][Ll]", "zdocs/z[a-c].zmd"):
        assert _GLOB.unmodelled(pattern) is None, pattern
    assert "exclusion" in _GLOB.unmodelled("!zdocs/**")


def test_run_script_extraction_rejects_the_documented_near_misses() -> None:
    """The two narrowings above are documented but were unenforced.

    Reverting `PIP_COMMAND` to a plain `"pip" in line` substring test, or
    widening `VAR_PREFIX` back to any `$VAR/`, both left every other test green
    (blind review) — so the hazards their comments describe had no teeth.
    """
    def files(script: str) -> set[str]:
        # the same two steps `_step_files` applies: extract, then keep only
        # what is really in the tree. Asserting on the raw candidates alone
        # would test the wrong layer — an unresolvable `$VAR/…` token IS
        # yielded, and it is `_is_tracked_file` that rejects it.
        return {c for c in _run_script_files(script) if _is_tracked_file(c)}

    # `pip` as a word, not a substring of pipefail / pipeline / pipx
    assert files("pip install pyyaml -c requirements/ci-constraints.txt") == {
        "requirements/ci-constraints.txt"}
    assert files("set -o pipefail; cp -r requirements/ci-constraints.txt /tmp/"
                 ) == set()
    assert files("pipx run tox -c requirements/ci-constraints.txt") == set()

    # only checkout-denoting variables are stripped; a scratch dir that happens
    # to hold a repo-shaped name must not become a demanded filter entry
    assert files('python3 "$GITHUB_WORKSPACE/scripts/ops/ci_flake_retry.py"'
                 ) == {"scripts/ops/ci_flake_retry.py"}
    assert files('python3 "$RUNNER_TEMP/scripts/ops/ci_flake_retry.py"') == set()

    # ⛔ an interpreter's first non-flag argument ONLY. This must be asserted
    # with a REAL interpreter: the `pytest tools/portal --ignore=…` case below
    # returns empty because `pytest` is not in INTERPRETERS at all, so it holds
    # identically with the extractor fully widened — it looks like it covers
    # this rule and does not.
    assert files("bash scripts/ops/_verify_download.sh docs/internal/dev-rules.md"
                 ) == {"scripts/ops/_verify_download.sh"}
    assert files("python3 -m pytest tests/") == set()
    assert files("bash scripts/ops/_verify_download.sh x y") == {
        "scripts/ops/_verify_download.sh"}
    # flags are SKIPPED, not treated as end-of-arguments
    assert files("bash -x scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}
    # trailing shell punctuation is not part of the path.
    # ⛔ Use the SUBSHELL form: the `;` case below is split by
    # `SHELL_SEPARATORS` before `_normalise_token` ever sees it, so it passes
    # identically with the strip deleted — a pin that holds without the thing
    # it claims to pin (blind review). Parens are what the strip is for.
    assert files("(cd x && bash scripts/ops/_verify_download.sh)") == {
        "scripts/ops/_verify_download.sh"}
    assert files("bash scripts/ops/_verify_download.sh; echo done") == {
        "scripts/ops/_verify_download.sh"}

    # ⛔ The full "which token is the command" class, enumerated rather than
    # discovered one review round at a time: shell keywords left at a command's
    # head by the `;`-split, and a wrapper's OWN flags. Wrapping an existing
    # `run:` step in `if … then … fi` is an ordinary refactor, and four of the
    # five wrappers were blind in their flagged spelling.
    verifier = {"scripts/ops/_verify_download.sh"}
    assert files("if [ -f x ]; then bash scripts/ops/_verify_download.sh; fi"
                 ) == verifier
    assert files("for f in a b; do bash scripts/ops/_verify_download.sh; done"
                 ) == verifier
    assert files("sudo -E bash scripts/ops/_verify_download.sh") == verifier
    assert files("xargs -0 bash scripts/ops/_verify_download.sh") == verifier

    # `--flag=value` form of a dependency flag — live code, previously untested.
    assert files("pip install --constraint=requirements/ci-constraints.txt x"
                 ) == {"requirements/ci-constraints.txt"}
    # prose is not a dependency
    assert files('echo "::error::see docs/internal/dev-rules.md"') == set()
    # (spelled `--ignore=<a real doc>` rather than a real test path: `verify_diff`
    # indexes this module by TEXT, so naming a test file here would route every
    # edit of it to this guard.)
    assert files("pytest tools/portal --ignore=docs/internal/dev-rules.md"
                 ) == set()

    # ⛔ direct execution has no interpreter token to key on. No gated job uses
    # it today, so this is the only thing holding that branch honest — without
    # it, deleting the branch stays green (blind review) and the day a step is
    # written `./scripts/ops/x.sh` the dependency goes silently unseen.
    assert files("./scripts/ops/_verify_download.sh a b") == {
        "scripts/ops/_verify_download.sh"}
    # ⛔ …but ONLY in command position. Pinning just the line above is what let
    # the `./` branch harvest from any argument position for two rounds: prose
    # in an `::error::` string, and a path handed to another command, both
    # became "dependencies" — the exact false positive this whole predicate
    # was narrowed to avoid (blind review).
    assert files('echo "::error::see ./docs/internal/dev-rules.md"') == set()
    assert files("bash scripts/ops/_verify_download.sh ./docs/internal/dev-rules.md"
                 ) == {"scripts/ops/_verify_download.sh"}

    # ⛔ command-scoped, not line-scoped: `pip` must be THIS command's word.
    assert files("pip --version && tox -c requirements/ci-constraints.txt") == set()

    # ⛔ An INTERPRETER also only counts in command position. Naming the fix
    # command inside `::error::` is this repo's convention, so a message that
    # SHOWS the command must not become a dependency. The earlier pin used a
    # prose string with no interpreter token in it, so it held identically with
    # the extractor fully widened — it looked like it covered this and did not.
    assert files(
        'echo "::error::spec drift. Run python3 scripts/tools/dx/bump_docs.py --fix"'
    ) == set()
    assert files('echo "::error::run bash scripts/ops/_verify_download.sh"') == set()
    # …while the wrappers that legitimately precede a command still resolve —
    # for ALL THREE verb-keyed predicates, not just the two that had pins.
    # `./` kept using `tokens[0]` after its siblings moved to the resolved
    # verb, so `sudo ./x.sh` silently found nothing (blind review).
    assert files("sudo bash scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}
    assert files("env FOO=1 python3 scripts/tools/dx/bump_docs.py") == {
        "scripts/tools/dx/bump_docs.py"}
    assert files("sudo ./scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}
    assert files("env FOO=1 ./scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}

    # ⛔ `python -m pip` IS pip. Keying only on the command verb missed it, and
    # ci.yml already spells it that way — so a constraints file vanished from a
    # form the comment above claims to handle. Four spellings exist in
    # practice; two were unhandled and one of those was live in the tree.
    assert files("python3 -m pip install --quiet pyyaml "
                 "-c requirements/ci-constraints.txt") == {
        "requirements/ci-constraints.txt"}
    assert files("python -m pip install pyyaml "
                 "-c requirements/ci-constraints.txt") == {
        "requirements/ci-constraints.txt"}
    # …but `-m <not pip>` must not inherit pip's flag handling.
    assert files("python3 -m pytest tests/ -c requirements/ci-constraints.txt"
                 ) == set()
    # …and a real command after a separator is still seen.
    assert files("echo hi; bash scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}


def test_is_tracked_file_is_case_exact_and_stays_inside_the_repo() -> None:
    """The case-exact walk, pinned; plus the repo-escape behaviour.

    ⛔ Honest about what pins what, and WHERE — this test does not mean the
    same thing in the three places it runs:

      * Windows host — the case assertions bite: `Docs/index.md` resolves, so
        deleting the per-segment `iterdir()` walk reds them.
      * Dev container — they bite HERE TOO, and not for the reason you would
        guess. Its repo mount is 9p from `C:\\` and inherits NTFS
        case-insensitivity (measured: `ls Docs/index.md` succeeds inside the
        container, while the container's own `/tmp` is case-sensitive). ⚠️ So
        "verified in the dev container" is NOT evidence about case-sensitive
        behaviour — the container is not a faithful oracle for it.
      * Real CI (ubuntu-latest, ext4) — `Docs/index.md` does not exist, so the
        case assertions pass TRIVIALLY. The walk is genuinely load-bearing
        there (it is what makes the scanner agree across platforms) but
        nothing in CI pins it. Accepted: the two environments a human actually
        edits in do pin it.

    The absolute/`..` rejection is NOT pinned by any of them, anywhere: the
    walk already rejects those (`"/etc/hosts".split("/")` starts with an empty
    segment; `..` is never an `iterdir()` entry). It is kept as defence in
    depth — it avoids `stat()`ing a live system path on POSIX, where
    `ROOT / "/etc/hosts"` IS `/etc/hosts` — but the assertions below verify the
    OUTCOME, not that particular guard.
    """
    assert _is_tracked_file("docs/index.md")
    # wrong case: real on a case-insensitive filesystem, absent on Linux
    assert not _is_tracked_file("Docs/index.md")
    assert not _is_tracked_file("docs/INDEX.md")
    # never escape the repo: `ROOT / "/tmp/x"` is `/tmp/x` on POSIX
    assert not _is_tracked_file("/etc/hosts")
    assert not _is_tracked_file("docs/../docs/index.md")
    assert not _is_tracked_file("../CHANGELOG.md")


def test_logical_shell_lines_follows_the_three_shell_rules() -> None:
    """The joiner is currently invisible to the integration path — every
    continuation in the scanned workflows happens to carry no dependency — so
    without this its three documented rules have zero coverage, and a wrong
    edit fails by silently deleting a command from the scan."""
    bs = "\\"

    def tokens(script):
        # what the consumer actually sees; exact run-lengths of the joining
        # whitespace are not part of the contract.
        return [line.split() for line in _logical_shell_lines(script)]

    # continuation joins, and the joined line is scanned as one command
    assert tokens(f"pip install pyyaml {bs}\n  -c reqs/pins.txt\n") == [
        ["pip", "install", "pyyaml", "-c", "reqs/pins.txt"]]
    # a comment ends at its newline: a trailing backslash must NOT eat the
    # command below it
    assert tokens(f"# note {bs}\nbash scripts/x.sh\n") == [
        ["bash", "scripts/x.sh"]]
    # an EVEN number of trailing backslashes is an escaped backslash
    assert tokens(f"echo end{bs}{bs}\nbash scripts/x.sh\n") == [
        ["echo", f"end{bs}{bs}"], ["bash", "scripts/x.sh"]]
    # whitespace after the backslash means bash does not continue either
    assert tokens(f"echo a {bs}  \nbash scripts/x.sh\n") == [
        ["echo", "a", bs], ["bash", "scripts/x.sh"]]
    # CRLF is handled like LF
    assert tokens(f"pip install a {bs}\r\n  -c reqs/pins.txt\r\n") == [
        ["pip", "install", "a", "-c", "reqs/pins.txt"]]


def test_workspace_expression_is_stripped_before_tokenising() -> None:
    """`${{ github.workspace }}/x` contains SPACES, so it must go first.

    Tokenising it first yields `${{` as the script argument and the real path
    is never a candidate — the failure is total, not partial.
    """
    found = set(_run_script_files(
        'python3 "${{ github.workspace }}/scripts/ops/ci_flake_retry.py" --'))
    assert "scripts/ops/ci_flake_retry.py" in found, found


def test_working_directory_resolves_step_relative_scripts() -> None:
    """Resolve where the shell would, then fall back to the repo root.

    ⛔ A relative script argument resolves against the step's
    `working-directory:`, not the repo root: `working-directory: scripts` +
    `python3 tools/lint/x.py` tested as `<root>/tools/lint/x.py` is found
    missing and dropped — the dependency becomes invisible with every test
    green (blind review). The step directory is tried FIRST and the repo root
    second, which is right for both spellings.
    """
    relative = {"working-directory": "scripts",
                "run": "python3 tools/lint/check_doc_links.py --ci\n"}
    assert _step_files(relative) == {"scripts/tools/lint/check_doc_links.py"}

    # Root-anchored spellings must still land at the root: nothing exists under
    # `<work_dir>/scripts/...`, so the fallback is what makes both work.
    anchored = {"working-directory": "components/tenant-api",
                "run": 'python3 "$GITHUB_WORKSPACE/scripts/ops/ci_flake_retry.py"\n'}
    assert _step_files(anchored) == {"scripts/ops/ci_flake_retry.py"}

    # Control: without the directory the relative spelling names no tracked file.
    assert _step_files({"run": relative["run"]}) == set()


# ---------------------------------------------------------------------------
# Invariant 4 — workflow-level `on.pull_request.paths` vs the scripts it runs.
# ---------------------------------------------------------------------------
#
# A workflow can gate itself with GitHub's own
# `on.pull_request.paths`, and then a PR touching only a script the workflow
# runs never starts the workflow at all (#3601). guard-defaults-impact.yml ran
# `scripts/ops/guard_defaults_scopes.py` for months without listing it, so a PR
# breaking that helper merged with the guard never having looked.
#
# Invariant: **every `scripts/**` file a job's `run:` step executes (per
# `_step_files`) is covered by that workflow's `on.pull_request.paths`.**
#
# ⛔ Scope, stated so nobody reads more into a green run:
#   * `pull_request` and `pull_request_target` only. A `push.paths` gate fires
#     after merge; the question here is whether the PR that breaks the script
#     is checked.
#   * `scripts/**` only. Six on.paths workflows install from
#     `requirements/ci-constraints.txt` and five of them do not list it;
#     whether it belongs in each is a CI-cost call, not drift.
#   * Disclosed blind spots of the extraction above: files
#     reached indirectly through a script (or a Makefile target), directories,
#     a job-level reusable `uses:` and a local composite action
#     (`uses: ./.github/actions/x`) — neither has `run:` steps to read here —
#     `defaults.run.working-directory` (only step-level is honoured),
#     `shell: python3 {0}`, a path held in a matrix value or `env:` variable,
#     and `_command_verb`'s gaps. None of these occurs in a scanned workflow
#     today (measured at #3601's review); they are named so a green run is not
#     read as more than it is.
#   * Step-level `if:` / `continue-on-error` are NOT refused here:
#     they change what a job reports, not whether the workflow
#     starts.
#   * `!` patterns ARE modelled, because GitHub defines
#     them for `on.paths` and bench-gate-pr.yaml uses one: patterns are read in
#     order and the LAST one matching a path decides (`_on_paths_covers`).
#     ⛔ The two kinds are matched in OPPOSITE approximations, each failing
#     loud: a positive pattern through `_covers` (an unmodelled glob answers
#     "no match" → uncovered → red), a negated one through
#     `_negation_may_match` (an unmodelled glob answers "match" → excluded →
#     red). Using `_covers` for both silently dropped any exclusion it could
#     not parse — including bench-gate-pr's own `*_test.go` (#3601 review).
#   * REFUSED (raise): `paths-ignore` (coverage would be the complement; none
#     is in use, so it is refused rather than modelled speculatively).
#   * A workflow that does not parse is LISTED and turns the test red — never
#     skipped, because an unparsed workflow is one this guard did not look at.
#
# ⚠️ The "reached indirectly through a script" blind spot above is closed a
# different way, per workflow (#3756): a workflow that carries the `GENERATED
# path-trace` block has its list derived from what its jobs were MEASURED to
# load, and its last step fails on a load the list does not cover
# (scripts/ops/workflow_path_filter.py; tests/ops/test_workflow_path_filter.py).
# This static invariant stays for every workflow, traced or not — it reads the
# steps without running them, so it also sees a step the traced run skipped —
# and both grade with the same two functions, bound just below.
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
WORKFLOW_PATHS_SCOPE = "scripts/"

# ⛔ Only shrinks. Each entry is a (workflow, script) the scan finds uncovered
# today and nobody has yet decided on; `test_workflow_paths_pending_only_shrinks`
# turns red the moment one becomes covered, so a fixed entry cannot linger and
# hide a regression of the same pair. Adding an entry is the wrong fix for a red
# invariant 4 unless the omission is a deliberate CI-cost decision — then write
# that decision here.
WORKFLOW_PATHS_PENDING: dict[tuple[str, str], str] = {}

# ⛔ Deliberately NOT triggered: a (workflow, script) the scan finds uncovered
# and the owner decided to leave that way (#3665, option C′). Each entry names
# what DOES check the script on a PR, and that claim is measured, not trusted:
#   ("workflow", X, via) — X's `on.pull_request.paths` cover the script, and
#       a step runs it with no `if:` on the step, its job, or any job in its
#       `needs:` chain, and no `continue-on-error` (refused rather than
#       modelled). When X runs it only INDIRECTLY (the scan sees direct runs
#       only — a disclosed blind spot above), `via` names the script X runs
#       that way, and the name must appear in via's shell code outside comments;
#   ("pytest", T, None) — test module T exists and IMPORTS the script's
#       module (AST, not text). Python Tests runs on every PR (#3756), so
#       nothing further has to cover the script.
# ⚠️ NOT measured (same limits as invariant 4, plus two of this check's own):
#   `on.pull_request.types` / `branches` narrowing; a shell-level `if` inside
#   the step's `run:` or inside via; whether via's mention is a call rather
#   than, say, an `echo`.
# `test_workflow_paths_not_triggered_is_still_justified` turns red when the
# claim stops holding, `..._is_still_needed` when the pair becomes covered
# (the entry would then excuse a regression of the same pair). Both red.
# (bench-workload-effect.yaml's two entries left in #2348: its self-test now
# uses the merge commit's first parent (main's tip) as the reference, builds,
# and runs both scripts,
# so they are in its `on.pull_request.paths` instead.)
# ⚠️ bench-gate-pr.yaml documents a future FLIP that drops its pull_request
# block; the "workflow" entries naming it then turn red, correctly.
WORKFLOW_PATHS_NOT_TRIGGERED: dict[tuple[str, str],
                                  tuple[str, str, str | None, str]] = {
    ("bench-workload-record.yaml", "scripts/tools/ops/bench_gate_compare.sh"): (
        "workflow", "bench-gate-pr.yaml", None,
        "its PR gate is test-file shaped (bench inputs) and a full "
        "interleaved run; bench-gate-pr.yaml runs the comparator on PRs"),
}


# ⛔ The gate's extraction and GitHub's ordered `!` rule are NOT defined here
# any more (#3756). scripts/ops/workflow_path_filter.py asks the same two
# questions at run time, inside the workflow, of the files a job really loaded
# — and a second copy of "what is this workflow's gate" / "does it cover this
# path" would be a second answer. Both live beside the matcher and are bound
# here by name, so every assertion below grades with the functions CI runs.
_pull_request_paths = _WPF.pull_request_paths
_on_paths_covers = _GLOB.on_paths_covers
_negation_may_match = _GLOB.negation_may_match


def _workflow_paths_scan(
        directory: Path,
) -> tuple[list[str], dict[str, tuple[list[str], set[str]]]]:
    """(unparseable workflow names, {name: (paths, scoped files it runs)}).

    Only workflows with `on.pull_request.paths` appear in the mapping.
    """
    unparseable: list[str] = []
    scoped: dict[str, tuple[list[str], set[str]]] = {}
    for path in sorted(directory.glob("*.y*ml")):
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
            unparseable.append(f"{path.name}: {type(exc).__name__}")
            continue
        patterns = _pull_request_paths(workflow, path.name)
        if patterns is None:
            continue
        files: set[str] = set()
        jobs = workflow.get("jobs") or {}
        if not isinstance(jobs, dict):
            raise AssertionError(f"{path.name}: `jobs` is not a mapping")
        for job in jobs.values():
            for step in (job or {}).get("steps") or []:
                if isinstance(step, dict):
                    files |= {f for f in _step_files(step)
                              if f.startswith(WORKFLOW_PATHS_SCOPE)}
        scoped[path.name] = (patterns, files)
    return unparseable, scoped


def _workflow_paths_uncovered(
        scoped: dict[str, tuple[list[str], set[str]]],
) -> set[tuple[str, str]]:
    return {
        (name, f)
        for name, (patterns, files) in scoped.items()
        for f in files
        if not _on_paths_covers(patterns, f)
    }


def test_workflow_paths_cover_every_script_their_jobs_run() -> None:
    unparseable, scoped = _workflow_paths_scan(WORKFLOWS_DIR)
    assert not unparseable, (
        "workflows that do not parse were NOT checked by invariant 4 — fix "
        "them, do not skip them:\n" + "\n".join(f"  {u}" for u in unparseable))
    new = sorted(_workflow_paths_uncovered(scoped) - set(WORKFLOW_PATHS_PENDING)
                 - set(WORKFLOW_PATHS_NOT_TRIGGERED))
    assert not new, (
        "a workflow gated by `on.pull_request.paths` runs a script that is NOT "
        "in those paths — a PR changing only that script never starts the "
        "workflow, so a break in it merges unchecked (#3601). Add each script "
        "to the workflow's `on.pull_request.paths`:\n"
        + "\n".join(f"  {wf}: {p!r}" for wf, p in new))


def test_workflow_paths_pending_only_shrinks() -> None:
    _, scoped = _workflow_paths_scan(WORKFLOWS_DIR)
    stale = sorted(set(WORKFLOW_PATHS_PENDING) - _workflow_paths_uncovered(scoped))
    assert not stale, (
        "these WORKFLOW_PATHS_PENDING entries are no longer uncovered (fixed, "
        "renamed or removed). Delete them — a stale entry would silently "
        "excuse the same pair if it regressed:\n"
        + "\n".join(f"  {wf}: {p!r}" for wf, p in stale))


def test_workflow_paths_not_triggered_is_still_needed() -> None:
    """An excused pair that has become covered must leave the list."""
    _, scoped = _workflow_paths_scan(WORKFLOWS_DIR)
    stale = sorted(set(WORKFLOW_PATHS_NOT_TRIGGERED) - _workflow_paths_uncovered(scoped))
    assert not stale, (
        "these WORKFLOW_PATHS_NOT_TRIGGERED entries are covered now (or the "
        "workflow no longer runs the script). Delete them — a stale excuse "
        "would hide the same pair if it regressed:\n"
        + "\n".join(f"  {wf}: {p!r}" for wf, p in stale))


def _unconditional_runners(workflow_path: Path, script: str) -> list[str]:
    """`job/step` names that run `script` with nothing that can skip it or
    swallow its failure: no `if:` on the step, its job, or any job its job
    `needs:` (transitively — a skipped upstream skips it too), and no
    `continue-on-error` on the step or job."""
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    jobs = workflow.get("jobs") or {}

    def chain_ok(job_id: str, seen: frozenset[str] = frozenset()) -> bool:
        job = jobs.get(job_id) or {}
        if job_id in seen or "if" in job or job.get("continue-on-error"):
            return False
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        return all(chain_ok(n, seen | {job_id}) for n in needs)

    hits = []
    for job_id, job in jobs.items():
        for i, step in enumerate((job or {}).get("steps") or []):
            if (isinstance(step, dict) and script in _step_files(step)
                    and "if" not in step and not step.get("continue-on-error")
                    and chain_ok(job_id)):
                hits.append(f"{job_id}/{step.get('name', i)}")
    return hits


def _shell_code(text: str) -> str:
    """Shell text with comments removed (whole-line and trailing ` # …`), so a
    file name that survives only in a comment does not count as a call."""
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.split(r"\s#", line, maxsplit=1)[0])
    return "\n".join(out)


def _imports_module(test_text: str, module: str) -> bool:
    """True when the test module imports `module` (or `from module import …`)
    in its AST — a mention in a comment or docstring does not count."""
    for node in ast.walk(ast.parse(test_text)):
        if isinstance(node, ast.Import) and any(
                a.name.split(".")[-1] == module for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == module:
            return True
    return False


def _not_triggered_justification_errors(directory: Path) -> list[str]:
    _, scoped = _workflow_paths_scan(directory)
    broken = []
    for (wf, script), (kind, target, via, _why) in sorted(WORKFLOW_PATHS_NOT_TRIGGERED.items()):
        where = f"{wf}: {script!r} ({kind} {target}" + (f" via {via}" if via else "") + ")"
        if kind == "workflow":
            patterns, _files = scoped.get(target, (None, set()))
            if target == wf:
                broken.append(f"{where} — names itself")
            elif patterns is None:
                broken.append(f"{where} — no pull_request path gate in scope")
            elif not _on_paths_covers(patterns, script):
                broken.append(f"{where} — does not start on the script")
            elif not _unconditional_runners(directory / target, via or script):
                broken.append(f"{where} — no job/step runs {via or script!r} without an `if:`")
            elif via and Path(script).name not in _shell_code((ROOT / via).read_text(encoding="utf-8")):
                broken.append(f"{where} — {via!r} does not invoke the script")
        elif kind == "pytest" and via is None:
            module = Path(script).stem
            test = ROOT / target
            if not test.is_file():
                broken.append(f"{where} — test module missing")
            elif not _imports_module(test.read_text(encoding="utf-8"), module):
                broken.append(f"{where} — test module does not import {module!r}")
        else:
            broken.append(f"{where} — unknown kind")
    return broken


def test_workflow_paths_not_triggered_is_still_justified() -> None:
    """The stated reason is measured with the same scan, so it cannot go stale
    silently. An empty list passes: deleting the last entry is the correct end
    state, not a vacuous one (each entry is still guarded while it exists)."""
    broken = _not_triggered_justification_errors(WORKFLOWS_DIR)
    assert not broken, (
        "a deliberately-not-triggered entry lost its justification — a PR "
        "changing the script would no longer be checked by what it names:\n"
        + "\n".join(f"  {b}" for b in broken))


def test_workflow_paths_lists_are_disjoint() -> None:
    both = sorted(set(WORKFLOW_PATHS_PENDING) & set(WORKFLOW_PATHS_NOT_TRIGGERED))
    assert not both, f"a pair cannot be both undecided and decided: {both}"


def test_workflow_paths_scan_still_sees_the_motivating_input() -> None:
    """Vacuity tripwire: the scan must still find the #3601 helper.

    Every assertion above is negative ("nothing uncovered"), so an extraction
    that silently finds nothing would pass them all.
    """
    _, scoped = _workflow_paths_scan(WORKFLOWS_DIR)
    assert "guard-defaults-impact.yml" in scoped, sorted(scoped)
    _, files = scoped["guard-defaults-impact.yml"]
    assert "scripts/ops/guard_defaults_scopes.py" in files, sorted(files)


def _write_workflow(directory: Path, name: str, on_block: str,
                    run: str) -> None:
    (directory / name).write_text(
        "name: synthetic\n" + on_block
        + "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        + f"      - if: github.event_name == 'pull_request'\n"
        + f"        run: {run}\n",
        encoding="utf-8")


def test_workflow_paths_scan_grades_scope_and_coverage(tmp_path: Path) -> None:
    """Positive, negative and control cases on synthetic workflows.

    The scripts named must be TRACKED files, because `_step_files` only
    resolves those; the workflow files themselves are synthetic.
    """
    script = "scripts/tools/lint/check_doc_links.py"
    run = f"python3 {script} --ci && pip install -c requirements/ci-constraints.txt x"
    _write_workflow(tmp_path, "uncovered.yml",
                    "on:\n  pull_request:\n    paths:\n      - 'docs/**'\n", run)
    _write_workflow(tmp_path, "covered.yml",
                    "on:\n  pull_request:\n    paths:\n"
                    "      - 'scripts/tools/lint/*.py'\n", run)
    # Control: no workflow-level path gate ⇒ starts on every PR ⇒ out of scope.
    _write_workflow(tmp_path, "ungated.yml", "on: [pull_request]\n", run)
    # pull_request_target is a PR gate too.
    _write_workflow(tmp_path, "target.yml",
                    "on:\n  pull_request_target:\n    paths:\n      - 'docs/**'\n", run)
    # Control: a push-only path gate is out of scope too.
    _write_workflow(tmp_path, "push-only.yml",
                    "on:\n  push:\n    paths:\n      - 'docs/**'\n", run)

    unparseable, scoped = _workflow_paths_scan(tmp_path)
    assert unparseable == []
    assert sorted(scoped) == ["covered.yml", "target.yml", "uncovered.yml"]
    # requirements/ is outside the scope even though the step installs from it.
    assert scoped["uncovered.yml"][1] == {script}
    assert _workflow_paths_uncovered(scoped) == {
        ("uncovered.yml", script), ("target.yml", script)}


def test_workflow_paths_scan_lists_unparseable_and_refuses_unmodelled(
        tmp_path: Path) -> None:
    (tmp_path / "broken.yml").write_text("on: [pull_request\n", encoding="utf-8")
    unparseable, scoped = _workflow_paths_scan(tmp_path)
    assert unparseable == ["broken.yml: ParserError"] and scoped == {}

    for name, on_block in (
        ("ignore.yml", "on:\n  pull_request:\n    paths-ignore:\n      - 'docs/**'\n"),
        ("only-negated.yml", "on:\n  pull_request:\n    paths:\n"
                             "      - '!scripts/tools/**'\n"),
        ("empty.yml", "on:\n  pull_request:\n    paths: []\n"),
    ):
        directory = tmp_path / name.split(".")[0]
        directory.mkdir()
        _write_workflow(directory, name, on_block, "true")
        with pytest.raises(AssertionError, match=name):
            _workflow_paths_scan(directory)


def test_on_paths_covers_follows_githubs_last_match_rule() -> None:
    f = "scripts/tools/lint/check_doc_links.py"
    assert _on_paths_covers(["scripts/**"], f)
    assert not _on_paths_covers(["docs/**"], f)
    # A later exclusion removes it — the shape an OR over all patterns misses.
    assert not _on_paths_covers(["scripts/**", "!scripts/tools/**"], f)
    # A later positive re-includes it.
    assert _on_paths_covers(
        ["scripts/**", "!scripts/tools/**", "scripts/tools/lint/*.py"], f)
    # An exclusion that does not match leaves the earlier match standing.
    assert _on_paths_covers(["scripts/**", "!scripts/ops/**"], f)
    # ⛔ An exclusion `_covers` cannot parse must still EXCLUDE (review repro:
    # `guard_*.py` / `?` were silently dropped and the file read as covered).
    assert not _on_paths_covers(["scripts/**", "!scripts/tools/lint/check_*.py"], f)
    assert not _on_paths_covers(["scripts/**", "!scripts/tools/lint/check_doc_link?.py"], f)
    assert not _on_paths_covers(["scripts/**", "!scripts/**/*_links.py"], f)
    # Round 2: `**` inside a segment crosses `/`; `+` is a glob character.
    for neg in ("!**.py", "!scripts/**.py", "!scripts/tools**",
                "!scripts**/*.py", "!scripts/tool+s/lint/*.py"):
        assert not _on_paths_covers(["scripts/**", neg], f), neg
    for neg in ("!{scripts,docs}/**", "!scripts/(tools)/**",
                "!./scripts/**", "!scripts/tools/"):
        with pytest.raises(AssertionError, match="not modelled"):
            _on_paths_covers(["scripts/**", neg], f)
    # …while its literal segments still bound it (bench-gate-pr.yaml's shape).
    assert _on_paths_covers(
        ["scripts/**", "!components/threshold-exporter/app/**/*_test.go"], f)
    assert not _on_paths_covers(
        ["components/**", "!components/threshold-exporter/app/**/*_test.go"],
        "components/threshold-exporter/app/pkg/x_test.go")


def test_interpreter_value_flags_do_not_hide_the_script() -> None:
    """Review repro: `python3 -X utf8 x.py` yielded `utf8` and lost `x.py`."""
    x = "scripts/x.py"
    for spelling in (f"python3 -X utf8 {x}", f"python3 -W error -u {x}",
                     f"bash -eo pipefail {x}", f"bash -o pipefail {x}",
                     f"sh +o errexit {x}", f"bash --rcfile r.sh {x}"):
        assert list(_run_script_files(spelling)) == [x], spelling
    # Control: a flag that takes no value still lets the next token through.
    assert list(_run_script_files(f"python3 -u {x}")) == [x]
    assert list(_run_script_files(f"bash -x {x}")) == [x]
