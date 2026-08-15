#!/usr/bin/env python3
"""Behavioural twin for ``scripts/tools/lint/e2e_spec_lint.sh`` (A-13, #1428).

WHY THIS FILE EXISTS
--------------------
#1428 collapsed three copies of the A-13 lint command into one script. That
removes the drift, and replaces it with a single point of failure — which was
measured, not theorised: a blind review replaced the script's final
``exec npm run --silent lint`` with ``exit 0`` and everything stayed green.
The hook passed, the whole test suite passed, and an injected ``test.fixme()``
sailed through. Server side, the only thing looking at the file was
``shellcheck``, which grades syntax and had no opinion.

⛔ So the rule this file enforces is not "the script is correct" but "the
script cannot be disarmed silently", plus the thing that made the old
arrangement rot: nobody was checking that the callers still call it.
``make lint-e2e`` had no caller at all for two releases and no test noticed.

⛔ AND "still calls it" has to mean CALLS, not MENTIONS. The first version of
this file asserted `path in <string>`, and a blind review walked through it
six different ways, all green: ``run: echo bash …/e2e_spec_lint.sh``, a
Makefile recipe whose only reference was a comment, ``entry: bash -c ":" …``,
a hook with ``exclude: .*``, and — worst — ``if: false`` or
``continue-on-error: true`` on the job, which turns off the single server-side
execution point #1428 exists to add. Each assertion below therefore reads the
thing that decides whether the command RUNS, not whether the path appears.

HOW THE BEHAVIOURAL CASES WORK
------------------------------
The script derives the repo root from its own location, so pointing it at a
controlled tree means copying it into a throwaway one (the pattern
``tests/lint/test_mkdocs_strict_check.py`` uses for the same reason). That also
keeps the real ``tests/e2e`` out of it: a test that depended on whether this
checkout happens to have run ``npm ci`` would pass or fail for reasons that
have nothing to do with the script.

The cases are deliberately PAIRED. Two require a non-zero exit and one requires
a ZERO exit from a correctly set-up tree — without the last one, ``exit 1`` as
the script's entire body would satisfy this file, and a broken harness would
satisfy the other two (it did: see ``_BASH`` below).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Literal paths, not `/`-joined components: verify_diff.py builds its
# source→test map by scanning test files for literal path strings, so a split
# form would register this twin against nothing and editing the script would
# not select it.
_SCRIPT_REL = "scripts/tools/lint/e2e_spec_lint.sh"
_SCRIPT = _REPO_ROOT / "scripts/tools/lint/e2e_spec_lint.sh"
_PRECOMMIT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
_MAKEFILE = _REPO_ROOT / "Makefile"

# Resolve bash ONCE, to an absolute path, exactly as
# tests/lint/test_mkdocs_strict_check.py does. Passing the bare name "bash" to
# subprocess lets Windows resolve it through its own PATH, which on this host
# can find the WSL launcher at C:\Windows\System32\bash.exe: it identifies
# itself as /bin/bash, cannot see a `C:/…` path, and answers every invocation
# with exit 127 "No such file or directory". Measured while writing this file:
# 127 silently satisfies every "must fail" assertion here, so both non-zero
# cases were green against a script that had never run.
#
# ⛔ This line is NOT the protection, and an earlier version of this comment
# implied it was. `shutil.which` searches the same PATH the shell would, so a
# tree whose PATH lacks a POSIX bash resolves to that same launcher — a blind
# review reproduced it by removing mingw from PATH and took this file to 3
# failed with `assert 127 == 0`. What the absolute path buys is that the choice
# is made once and is printable. The thing that actually catches a bash which
# cannot run anything is the zero-exit case below, which is why it is not
# optional.
_BASH = shutil.which("bash")

# The shared GitHub-glob matcher. ⛔ Imported rather than reimplemented: a
# second copy of "does this path match that `paths:` entry" is a second answer,
# and the original already refuses to grade patterns outside the alphabet it
# models instead of guessing.
sys.path.insert(0, str(_REPO_ROOT / "tests" / "ops"))
from test_ci_path_filter_coverage import (  # noqa: E402
    _match_segments,
    reject_unmodelled_patterns,
)


def _hook() -> dict:
    """The `playwright-lint` hook, read off the real config."""
    cfg = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks = [
        hook
        for repo in cfg.get("repos") or []
        for hook in repo.get("hooks") or []
        if hook.get("id") == "playwright-lint"
    ]
    assert len(hooks) == 1, (
        f"expected exactly one `playwright-lint` hook, found {len(hooks)}. "
        "This twin pins that hook's contract; with zero or two of them it is "
        "pinning nothing."
    )
    return hooks[0]


_ESLINT_CONFIG = _REPO_ROOT / "tests/e2e/eslint.config.mjs"
_NPM_PACKAGE = _REPO_ROOT / "tests/e2e/package.json"


def _npm_scripts() -> dict:
    return json.loads(_NPM_PACKAGE.read_text(encoding="utf-8")).get("scripts", {})


def _js_literal(text: str, opener: str) -> str:
    """The balanced `{…}` or `[…]` that starts at `opener`, as source text.

    Enough JS to read two literals out of a flat config, and no more: a real
    parser is not available here and a regex would stop at the first `}`.
    """
    start = text.index(opener) + len(opener) - 1
    pairs = {"{": "}", "[": "]"}
    close = pairs[text[start]]
    depth = 0
    for i in range(start, len(text)):
        if text[i] == text[start]:
            depth += 1
        elif text[i] == close:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unbalanced {text[start]!r} after {opener!r}")


def _rule_options(config: str) -> dict:
    """The `no-skipped-test` options object, read as data not matched as text."""
    body = _js_literal(config, "'playwright/no-skipped-test': [")
    brace = body.index("{")
    out: dict = {}
    for pair in body[brace + 1:body.rindex("}")].split(","):
        if ":" not in pair:
            continue
        key, _, value = pair.partition(":")
        out[key.strip().strip("'\"")] = {"true": True, "false": False}.get(
            value.strip(), value.strip())
    return out


def _js_string_arrays(config: str, key: str) -> list[list[str]]:
    """EVERY `<key>: [ … ]` array in the config, as lists of string literals.

    ⛔ Three separate holes closed here, each proven end to end with a real
    injected `test.fixme()` and each leaving the script at rc=0:

    * the first version took only the FIRST `ignores:` — a second config object
      further down the flat-config array carries its own and was unread;
    * it kept only lines that START with a quote, so `ignores: ['a','b']` on one
      line read as the empty list and the assertion below went vacuous;
    * `files:` was never read at all, and narrowing it to one spec removes every
      other spec from the run exactly as an ignore would.

    Entries are extracted by quoted-literal rather than by line, so formatting
    is genuinely free — which the previous docstring claimed while a one-line
    list silently emptied it.
    """
    out: list[list[str]] = []
    for match in re.finditer(re.escape(key) + r"\s*:\s*\[", config):
        body = _js_literal(config[match.start():], f"{key}: [")
        out.append(re.findall(r"['\"]([^'\"]+)['\"]", body))
    return out


def _precommit_config() -> dict:
    return yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))


def _silencing_keys(hook: dict, config: dict) -> list[str]:
    """Every reason pre-commit would NOT run this hook on `git commit`.

    ⛔ Returns a list rather than a bool so the failure names the key. Four
    mechanisms, all measured against real pre-commit 4.5.1 in a throwaway git
    repo, all of which leave `files:` reading perfectly:

    * `stages:` on the hook without `pre-commit` in it;
    * a top-level `default_stages:` without `pre-commit`, when the hook does
      not override it — THIS REPO ALREADY SETS THAT KEY, so the interaction is
      live, not hypothetical;
    * `types:` narrowed off the default `[file]`, which ANDs with `files:`;
    * any `exclude_types:`, which subtracts from it.

    The first version asserted `"stages" not in hook`. That is a key-existence
    check, and it was wrong in both directions at once: `stages: [pre-commit,
    pre-push]` is a legitimate widening that it reds, while the top-level
    default it never reads silences the hook just as completely.
    """
    reasons: list[str] = []
    stages = hook.get("stages")
    if stages is None:
        stages = config.get("default_stages")
        where = "the top-level `default_stages:`"
    else:
        where = "the hook's `stages:`"
    if stages is not None and "pre-commit" not in stages:
        reasons.append(f"{where} is {stages!r}, which omits `pre-commit`")

    types = hook.get("types")
    if types is not None and list(types) != ["file"]:
        reasons.append(
            f"`types: {types!r}` narrows the file set beyond `files:` "
            "(pre-commit ANDs them; the default is `[file]`)")
    if hook.get("exclude_types"):
        reasons.append(f"`exclude_types: {hook['exclude_types']!r}` subtracts "
                       "from the file set without touching `files:`")
    # ⛔ `types_or:` is the fifth member of the same closed set, one line away
    # from `types:` in pre-commit's own schema — `run.py` ANDs it in as
    # `(not types_or or tags & types_or)`. A blind review set it to `[python]`
    # against the real hook and got "(no files to check) Skipped" with this
    # file green. Enumerating here is legitimate BECAUSE the set is closed and
    # published; the failure was reading four of five, not the approach.
    if hook.get("types_or"):
        reasons.append(f"`types_or: {hook['types_or']!r}` narrows the file set "
                       "the same way `types:` does (pre-commit ANDs it in)")
    return reasons


def _make_commands(makefile_text: str, target: str) -> list[str]:
    """The COMMAND lines of a Makefile target — comments excluded.

    ⛔ Comments excluded because `lint-e2e` is five comment lines and one
    command, so an assertion that scans the whole recipe is satisfied by a
    comment that merely names the script. Measured: replacing the command with
    ``cd tests/e2e && npm run --silent lint`` and leaving a comment saying
    "originally scripts/tools/lint/e2e_spec_lint.sh" kept this file green —
    the precise regression its docstring claims to catch.
    """
    out: list[str] = []
    active = False
    for line in makefile_text.splitlines():
        if line.startswith(f"{target}:"):
            active = True
            continue
        if not active:
            continue
        if line.startswith("\t"):
            body = _strip_recipe_prefixes(line.lstrip("\t"))
            if not body.lstrip().startswith("#"):
                out.append(body)
        elif line.strip():
            break
    return out


def _strip_recipe_prefixes(body: str) -> str:
    """Drop the recipe prefixes that do not change failure semantics.

    ⛔ `-` is deliberately NOT among them, and this is the whole point of the
    function existing. Make's `-` prefix means "ignore the exit status of this
    line", so `-bash …/e2e_spec_lint.sh` runs the lint and throws the verdict
    away. An earlier version stripped it alongside `@` and `+`, which handed
    the caller a string indistinguishable from the honest invocation: measured
    against GNU Make 4.3, `@bash fail.sh` gives `make` rc=2 while `-bash`,
    `@-bash` and `-@bash` all give rc=0 — and every one of them reduced to the
    same `bash fail.sh` here. Leaving `-`
    attached makes it the head token of the argv, which no interpreter in
    `_invokes_script`'s set matches — so the line reads as "not an invocation"
    and the caller reds, which is the correct answer for a line whose result is
    discarded.

    `@` (do not echo) and `+` (run even under `make -n`) are dropped because
    neither touches the exit status.
    """
    while body[:1] in {"@", "+"}:
        body = body[1:]
    return body


_INTERPRETERS = {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/env"}


def _argv(command: str) -> list[str]:
    """`shlex.split`, with `./x` folded to `x`.

    The Makefile writes `bash ./scripts/…` in other recipes, and `./x` and `x`
    name the same file to every shell that runs any of these three legs.
    Without the fold, adopting the neighbouring recipes' spelling would red a
    change that altered nothing.
    """
    try:
        return [t[2:] if t.startswith("./") else t for t in shlex.split(command)]
    except ValueError:
        return []


def _invokes_script(command: str) -> bool:
    """True when this command RUNS the script — used to FIND it, not to bless it.

    ⛔ Not `_SCRIPT_REL in command`. `echo bash …/e2e_spec_lint.sh` contains
    the path and runs nothing; so does a comment. The argv is what decides.

    ⛔ Deliberately permissive about what FOLLOWS the path, because its job is
    to locate the invocation even when that invocation has been disarmed. A
    finder that rejected `… || true` would make the disarmed step invisible and
    report "no step runs the script", which is red for the wrong reason and
    sends the reader looking for a deleted step. `_is_sole_invocation` is the
    one that judges.
    """
    argv = _argv(command)
    if not argv or _SCRIPT_REL not in argv:
        return False
    head = argv[: argv.index(_SCRIPT_REL)]
    # Either the script is the command itself, or it is the argument of a
    # shell. Anything else in front of it (echo, :, true, a `-c` payload, or
    # make's `-` "ignore errors" prefix) means the path is being printed or
    # its result discarded rather than executed for its verdict.
    return all(token in _INTERPRETERS for token in head)


def _is_sole_invocation(command: str) -> bool:
    """True when the command is that invocation and NOTHING else.

    ⛔ This rejects rather than classifies, and that is the design. The three
    legs do not share failure semantics — pre-commit `shlex`-splits `entry:`
    and execs it with NO shell (so `|| true` there is a literal argument, not
    an operator), the Makefile recipe runs under `/bin/sh` with no `pipefail`,
    and a GitHub `run:` without an explicit `shell:` gets `set -e` but ALSO no
    `pipefail`. Measured under that last one against scripts exiting 1 and 3:
    `bash fail.sh | tee o.txt` and `bash fail.sh & wait` both exit 0, while
    `bash fail.sh 2>&1` and `bash fail.sh; exit 0` both propagate the script's
    OWN status (1 and 3 respectively — not a fixed 1, which an earlier version
    of this sentence claimed). So a predicate that sorted trailing tokens
    into "harmless" and "silencing" would need a different answer per leg, and
    would still be one spelling short — the same enumeration that has been
    reopened once per review round.

    What IS uniform is the correct form: this script takes no arguments and its
    exit status is the entire verdict, so the only right way to call it is to
    call it and nothing else. Anything past the path is therefore refused
    without being understood, and the reader is asked to look.

    ⛔ The cost is real and deliberate: a legitimate edit (a `timeout` wrapper,
    a redirect, a trailing comment, a multi-line `run:` block) reds this until
    someone updates it. That is the review trigger, not a bug — but it is also
    why this is the ONLY assertion written this way, and why the tests below
    pin the forms that must keep passing.
    """
    argv = _argv(command)
    return bool(argv) and argv[-1] == _SCRIPT_REL and _invokes_script(command)


def _workflow_path() -> Path:
    """The workflow that RUNS the script — discovered, not named.

    ⛔ `_WORKFLOW` used to be a constant pointing at playwright.yml, which made
    every assertion here about a filename rather than about the leg. Measured:
    moving the job into `ci.yml` — which `playwright.yml`'s OWN comment
    recommends as the shape for a blocking leg, and which is strictly an
    upgrade — turned this file red with "no step in playwright.yml actually
    runs …", describing a disarming that had not happened and naming the file
    the reader had just deliberately moved the job out of. The cheapest way to
    green was to undo the upgrade.

    Exactly one workflow may run it, because two would be two answers about
    which triggers matter.
    """
    hits = [
        path
        for path in sorted((_REPO_ROOT / ".github/workflows").glob("*.y*ml"))
        for job in (yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        .get("jobs", {}).values()
        for step in (job.get("steps") or [])
        if _invokes_script(str(step.get("run", "")))
    ]
    unique = sorted(set(hits))
    assert len(unique) == 1, (
        f"expected exactly one workflow to run {_SCRIPT_REL}, found "
        f"{[p.name for p in unique]}. Zero means A-13 has no server-side "
        "execution point again, which is the whole subject of #1428; two "
        "means the assertions below cannot say which one's triggers decide "
        "whether the rule runs."
    )
    return unique[0]


def _workflow() -> dict:
    return yaml.safe_load(_workflow_path().read_text(encoding="utf-8"))


_TRIGGER_KEYS = frozenset({"paths", "branches", "branches-ignore"})


def _reads_trigger_key(node: ast.AST) -> bool:
    """A READ of a trigger key out of a mapping — not a write, not a mention.

    ⛔ An earlier version flagged every `ast.Constant` equal to "paths", which
    reds on the fixtures that BUILD `{"paths": …}` and on its own comparison —
    three false positives on an untouched file. Only two forms read a key.
    """
    if isinstance(node, ast.Subscript):
        return (isinstance(node.slice, ast.Constant)
                and node.slice.value in _TRIGGER_KEYS)
    # `.pop` alongside `.get`: a blind review swapped one for the other and
    # walked straight through the door with the negation rejection skipped.
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop", "setdefault"}
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in _TRIGGER_KEYS)


_CI_WORKFLOW = _REPO_ROOT / ".github/workflows/ci.yml"


def _branches_that_carry_required_checks(event: str) -> list[str]:
    """`ci.yml`'s `branches:` for this event — the repo's own declaration.

    ⛔ Not git, and not a literal. The first version asked git for
    `refs/remotes/origin/HEAD`, which is a property of how the checkout was
    MADE rather than of the repo: `git clone` writes that ref and
    `actions/checkout` does not (it does `git init` + `remote add` + `fetch`).
    Measured — in a checkout built that way, `git symbolic-ref
    refs/remotes/origin/HEAD` exits 128 with "is not a symbolic ref", and this
    file went to 1 failed on an untouched tree. `ci.yml` carries the required
    checks and states the branches it runs on, so asking IT keeps the two legs
    moving together and needs neither a network nor a particular clone shape.

    A literal `"main"` here would have been the same shape as the thing being
    checked: a name somebody typed, agreeing with itself after the repo moved.
    """
    ci = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    triggers = ci.get("on") or ci.get(True) or {}
    return (triggers.get(event) or {}).get("branches") or []


def _event_block(event: str) -> dict:
    triggers = _workflow().get("on") or _workflow().get(True)
    return (triggers or {}).get(event) or {}


def _trigger_paths(event: str) -> list[str]:
    """The `on.<event>.paths` list, refusing shapes this file cannot grade.

    ⛔ One of the only two places this file reads a trigger key, and that is
    the point. The first version called `reject_unmodelled_patterns` as its own
    line inside the caller; deleting that one line left the whole file green,
    because the control beside it was pinning the imported function
    rather than the wiring — the same "the control guards the helper, not the
    call" shape this repo has now recorded three times. Routing every read
    through here means the rejection cannot be dropped without dropping the
    accessor, and then there is nothing left to read patterns from.
    """
    patterns = _event_block(event).get("paths") or []
    reject_unmodelled_patterns(
        patterns, f"{_workflow_path().name}::on.{event}.paths")
    return patterns


def _trigger_branches(event: str) -> list[str]:
    """The `on.<event>.branches` list — the ninth off-switch, found by review.

    ⛔ `paths:` was covered and `branches:` two lines above it was not, in the
    same YAML block. Measured: rewriting `branches: [main]` to a name no branch
    has (2 places) left both this file and the shared path-filter module
    green, with the leg dead for every pull request into main —
    which is the only side anybody looks at before a merge. An enumeration of
    off-switches will always be one short; what stops that here is that the
    caller asserts the repo's real default branch is still listed, derived from
    git rather than typed in.
    """
    block = _event_block(event)
    ignored = block.get("branches-ignore")
    assert not ignored, (
        f"`on.{event}.branches-ignore` is {ignored!r}. GitHub makes the two "
        "keys mutually exclusive, so switching to the ignore form empties "
        "`branches:` and lands in the branch this function deliberately "
        "tolerates — measured, a blind review made that two-character edit and "
        "the leg stopped running for every push and every pull request into "
        "main with this file green. This file models the allow form only; if "
        "the ignore form is genuinely wanted, teach the comparison below what "
        "it means rather than letting it read as 'no restriction'."
    )
    return block.get("branches") or []


def _script_step() -> tuple[str, dict, dict]:
    """``(job_id, job, step)`` for the workflow step that runs the script."""
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps") or []:
            if _invokes_script(str(step.get("run", ""))):
                return job_id, job, step
    raise AssertionError(
        f"no step in {_workflow_path().name} actually runs {_SCRIPT_REL} — a step "
        "only mentions the path (echo, a comment, a disabled shell) is not an "
        "execution point, and this job is the only one A-13 has that does not "
        "depend on a developer's work tree being installed."
    )


# --------------------------------------------------------------------------
# Negative controls for the readers above. The real files are all in the state
# these tests want, so asserting only against them cannot tell a working reader
# from a constant — a blind review replaced `_hook` and `_make_recipe` with
# hard-coded returns and this file stayed green.
# --------------------------------------------------------------------------

_FAKE_MAKEFILE = (
    "other-target: ## something else\n"
    "\t@echo other\n"
    "\n"
    "lint-e2e: ## A-13\n"
    "\t@# 說明：本來是 scripts/tools/lint/e2e_spec_lint.sh\n"
    "\t@cd tests/e2e && npm run --silent lint\n"
    "\n"
    "next-target:\n"
    "\t@bash scripts/tools/lint/e2e_spec_lint.sh\n"
)


def test_make_commands_ignores_comments_and_stops_at_the_next_target() -> None:
    """⛔ Both halves were measured to matter.

    Keeping comment lines let a comment naming the script satisfy the caller
    assertion. Not stopping at the next target let ANY target in the Makefile
    satisfy it — including, in the fixture below, one that really does invoke
    the script while `lint-e2e` itself no longer does.
    """
    commands = _make_commands(_FAKE_MAKEFILE, "lint-e2e")
    assert commands == ["cd tests/e2e && npm run --silent lint"], commands
    assert not any(_invokes_script(c) for c in commands), (
        "a comment mentioning the script was read as a command"
    )
    assert _make_commands(_FAKE_MAKEFILE, "next-target") == [
        "bash scripts/tools/lint/e2e_spec_lint.sh"
    ]


_ACCEPTED_SPELLINGS = (
    f"bash {_SCRIPT_REL}",
    f"sh {_SCRIPT_REL}",
    f"/bin/bash {_SCRIPT_REL}",
    f"/bin/sh {_SCRIPT_REL}",
    f"/usr/bin/env bash {_SCRIPT_REL}",
    _SCRIPT_REL,
    # The Makefile writes `bash ./scripts/…` in neighbouring recipes; adopting
    # that spelling here changes nothing and must not red.
    f"bash ./{_SCRIPT_REL}",
    f"./{_SCRIPT_REL}",
)


def test_invokes_script_separates_running_from_mentioning() -> None:
    """The six disarms a blind review walked through, as a fixture."""
    assert _invokes_script(f"bash {_SCRIPT_REL}")
    assert _invokes_script(f"sh {_SCRIPT_REL}")
    assert _invokes_script(_SCRIPT_REL)

    for disarmed in (
        f"echo bash {_SCRIPT_REL}",
        f"echo {_SCRIPT_REL}",
        f'bash -c ":" {_SCRIPT_REL}',
        f"true {_SCRIPT_REL}",
        f"# {_SCRIPT_REL}",
        "cd tests/e2e && npm run --silent lint",
    ):
        assert not _invokes_script(disarmed), (
            f"{disarmed!r} was read as running the script. Every one of these "
            "keeps the path visible while executing nothing, which is exactly "
            "how a caller assertion based on substring containment was walked "
            "through six ways."
        )


def test_make_commands_keeps_the_ignore_errors_prefix_attached() -> None:
    """⛔ The half a blind review walked straight through.

    `_make_commands` used to `.lstrip("-")` alongside `@` and `+`, so a recipe
    line of `-bash <script>` reduced to the same string as the honest call and
    read as a valid invocation. Measured in a container against a script that
    exits 1: `@bash fail.sh` gives `make` rc=2, `-bash fail.sh` gives rc=0 —
    the lint runs and its verdict is discarded.

    Paired, because stripping too little is its own failure: `@` and `+` change
    nothing about the exit status and must still be dropped, or the honest
    recipe line reds.
    """
    tolerated = (
        f"lint-e2e:\n\t@bash {_SCRIPT_REL}\n",
        f"lint-e2e:\n\t+bash {_SCRIPT_REL}\n",
        f"lint-e2e:\n\t@+bash {_SCRIPT_REL}\n",
        f"lint-e2e:\n\tbash {_SCRIPT_REL}\n",
    )
    for makefile in tolerated:
        commands = _make_commands(makefile, "lint-e2e")
        assert any(_is_sole_invocation(c) for c in commands), (
            f"{makefile!r} reduced to {commands!r}, which no longer reads as "
            "an invocation. These prefixes do not touch the exit status."
        )

    for makefile in (
        f"lint-e2e:\n\t-bash {_SCRIPT_REL}\n",
        f"lint-e2e:\n\t@-bash {_SCRIPT_REL}\n",
        f"lint-e2e:\n\t-@bash {_SCRIPT_REL}\n",
    ):
        commands = _make_commands(makefile, "lint-e2e")
        assert not any(_is_sole_invocation(c) for c in commands), (
            f"{makefile!r} reduced to {commands!r} and read as a valid "
            "invocation, but make discards this line's exit status."
        )


def test_is_sole_invocation_refuses_trailing_tokens_but_not_spellings() -> None:
    """⛔ The over-tightness control, which the first version of this file lacked.

    `_is_sole_invocation` refuses anything after the path without interpreting
    it, so the only thing standing between it and an assertion nobody can
    satisfy is this list of forms that MUST keep passing. Both halves were
    measured: the rejected forms exit 0 under a GitHub `run:` step's real shell
    (`set -e`, no `pipefail`) against a script that exits 1, and the tolerated
    forms are the spellings the repo's own recipes already use.
    """
    missing = sorted(
        i for i in _INTERPRETERS
        if not any(a.startswith(i + " ") for a in _ACCEPTED_SPELLINGS))
    assert not missing, (
        f"{missing} were added to `_INTERPRETERS` without a case below. That "
        "set is the last enumeration left in this file, and it grew by one "
        "(`/bin/sh`) in a commit where nothing could see the change — every "
        "member has to appear here or the set is unobserved."
    )

    for accepted in _ACCEPTED_SPELLINGS:
        assert _is_sole_invocation(accepted), (
            f"{accepted!r} was refused. This spelling runs the script and lets "
            "its exit status stand, so refusing it is a false red — and a "
            "false red here is paid by whoever next touches an unrelated line."
        )

    for refused in (
        f"bash {_SCRIPT_REL} || true",
        f"bash {_SCRIPT_REL} || exit 0",
        f"bash {_SCRIPT_REL} | tee out.txt",
        f"bash {_SCRIPT_REL} &",
        f"bash {_SCRIPT_REL} 2>&1",
        f"bash {_SCRIPT_REL}  # A-13",
        f"timeout 300 bash {_SCRIPT_REL}",
        f"echo bash {_SCRIPT_REL}",
        f'bash -c ":" {_SCRIPT_REL}',
    ):
        assert not _is_sole_invocation(refused), (
            f"{refused!r} was accepted. Whether this particular form silences "
            "the result is exactly the question this predicate declines to "
            "answer: the last four rounds of review each found one more "
            "spelling, so anything past the path is refused and looked at."
        )


def test_reading_a_paths_list_goes_through_the_negation_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ Exercises the ACCESSOR, so the wiring is what is under test.

    An import that is never reached is the same as no import, and asserting on
    the imported function alone does not notice when the call site goes away —
    measured: deleting the call from the caller left this file green
    while a test named for the wiring stayed green. Feeding a negated list
    through `_trigger_paths` means removing either the rejection or the
    accessor reds this.

    The tolerated case carries the same weight: a `paths:` list of ordinary
    patterns must come back untouched, or every workflow edit reds.
    """
    def _fake(paths: list[str]) -> dict:
        return {"on": {"push": {"paths": paths}}}

    monkeypatch.setattr(
        __name__ + "._workflow", lambda: _fake(["tests/e2e/**", "package.json"]))
    assert _trigger_paths("push") == ["tests/e2e/**", "package.json"]

    monkeypatch.setattr(
        __name__ + "._workflow", lambda: _fake(["tests/e2e/**", "!tests/e2e/**"]))
    with pytest.raises(AssertionError, match="exclusion patterns"):
        _trigger_paths("push")


def test_nothing_in_this_file_reads_a_trigger_key_around_the_accessor() -> None:
    """⛔ The accessor only helps while it is the only door.

    The test above proves `_trigger_paths` rejects a negation. It cannot notice
    a caller that stops calling it: measured, inlining the old two-line read
    back into the trigger test left this file green with the rejection
    bypassed. So the last step is mechanical, and it reads THIS file's own AST.

    ⛔ The quantifier is the MODULE, not a list of function bodies. The first
    version iterated `ast.FunctionDef` and skipped `_trigger_paths`, which put
    every module-level statement outside it: a blind review hoisted the read to
    module scope in one edit and this file stayed green with the
    rejection bypassed. `ast.FunctionDef` is an enumeration wearing a
    quantifier's clothes — it misses module scope, `async def`, lambdas and
    comprehensions alike. Walking the whole tree and subtracting the accessor's
    own nodes cannot miss a scope, because it never names one.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    accessor_names = {"_trigger_paths", "_trigger_branches",
                      "_branches_that_carry_required_checks"}
    accessors = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in accessor_names
    ]
    assert {a.name for a in accessors} == accessor_names, (
        f"the accessors {sorted(accessor_names)} are not both present "
        f"(found {sorted(a.name for a in accessors)}). They are the only "
        "places allowed to read a trigger key, so a missing one makes this "
        "check permit every read in the file — restore it, or delete this "
        "test in the same commit so the loss is visible."
    )
    inside_accessors = {
        id(node) for a in accessors for node in ast.walk(a)
    }

    offenders = sorted(
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if _reads_trigger_key(node) and id(node) not in inside_accessors
    )
    assert not offenders, (
        f"{offenders} read one of {sorted(_TRIGGER_KEYS)} outside "
        f"{sorted(accessor_names)}. Every read goes through an accessor, "
        "because that is the only thing that makes the negation rejection "
        "unskippable — a second reader is a second answer, and the one it "
        "gives is 'covered' for a pattern that removes coverage. Extend the "
        "accessor instead of reading around it."
    )


def test_silencing_keys_names_every_measured_way_off_the_commit_path() -> None:
    """⛔ Control for the loosened `stages:` rule, which is otherwise dead code.

    The real hook has no `stages:` key, so `stages is None` short-circuits and
    the half that does the judging never runs: measured, replacing the literal
    `"pre-commit"` in the old inline predicate with a nonsense stage left this
    file green. Loosening a rule and pairing it with controls are the
    same decision — the sibling loosening in `_is_sole_invocation` got its
    tolerated/refused table in the same commit and this one did not.
    """
    live = {"id": "playwright-lint"}
    for hook, config, why in (
        ({**live, "stages": ["manual"]}, {}, "hook stages omit pre-commit"),
        ({**live, "stages": []}, {}, "hook stages empty"),
        (live, {"default_stages": ["manual"]}, "top-level default_stages"),
        (live, {"default_stages": ["pre-push"]}, "top-level, pre-push only"),
        ({**live, "types": ["python"]}, {}, "types narrowed"),
        ({**live, "exclude_types": ["ts"]}, {}, "exclude_types subtracts"),
    ):
        assert _silencing_keys(hook, config), (
            f"{why}: {hook!r} + {config!r} was read as still running on "
            "commit. Every one of these was measured to skip the hook while "
            "leaving `files:` intact."
        )

    for hook, config, why in (
        (live, {}, "no stages anywhere — pre-commit's own default"),
        (live, {"default_stages": ["pre-commit"]}, "this repo's actual config"),
        ({**live, "stages": ["pre-commit", "pre-push"]}, {},
         "widening, measured to keep running"),
        ({**live, "stages": ["pre-commit"]}, {"default_stages": ["manual"]},
         "hook overrides a narrowed default"),
        ({**live, "types": ["file"]}, {}, "types spelled as the default"),
        ({**live, "exclude_types": []}, {}, "empty exclude_types"),
    ):
        assert not _silencing_keys(hook, config), (
            f"{why}: {hook!r} + {config!r} was read as silenced. A rule that "
            "reds on legitimate config gets loosened by whoever hits it next, "
            "and the loosening will not come with controls."
        )


def test_the_trigger_key_reader_tells_reads_from_writes_and_mentions() -> None:
    """⛔ Control for `_reads_trigger_key`, which otherwise never returns True.

    On a clean tree the door test's offender list is empty either way, so the
    predicate is unobserved: measured, `return False` as its whole body left
    this file green. That is the same "only ever fed inputs that should
    pass" shape this file already pins for `_is_sole_invocation` — the
    discipline has to reach the helper written last, too.

    Both directions matter. Too narrow and the door opens; too broad and it
    reds on the fixtures that BUILD a trigger dict, which is exactly what the
    first version did (three false positives on an untouched file).
    """
    def _nodes(src: str) -> list[ast.AST]:
        return list(ast.walk(ast.parse(src)))

    for src in (
        'x["paths"]',
        'x.get("paths")',
        'cfg["on"]["push"]["branches"]',
        'cfg.get("branches", [])',
        '[p for p in wf["paths"]]',
        'f = lambda w: w["branches"]',
    ):
        assert any(_reads_trigger_key(n) for n in _nodes(src)), (
            f"{src!r} reads a trigger key and was not detected. The door test "
            "is only as wide as this predicate."
        )

    for src in (
        '{"paths": paths}',                      # builds one
        '{"on": {"push": {"branches": br}}}',    # builds one
        'x["pathsep"]',                          # different key
        'x.get("paths_extra")',                  # different key
        'os.environ.get(name)',                  # non-constant key
        's = "paths"',                           # mentions it
        'assert node.args[0].value == "paths"',  # compares it
    ):
        assert not any(_reads_trigger_key(n) for n in _nodes(src)), (
            f"{src!r} does not read a trigger key but was flagged. A predicate "
            "that bites the fixtures makes the door test unsatisfiable, and "
            "the cheapest way out of that is deleting the door."
        )


# --------------------------------------------------------------------------
# The contract.
# --------------------------------------------------------------------------


def test_all_three_entry_points_run_the_one_script() -> None:
    """⛔ The "single source of truth" claim must be checkable, not prose.

    The script's header names three entry points. Before #1428 the same claim
    was made in `frontend-quality-backlog.md` about a Makefile target that
    nothing invoked — wrong, and it stayed wrong because no test read it.
    """
    assert _SCRIPT.is_file(), f"{_SCRIPT_REL} is missing"

    hook = _hook()
    entry = str(hook.get("entry", ""))
    assert _is_sole_invocation(entry), (
        f"the `playwright-lint` hook's entry is {entry!r}, which is not just a "
        f"call to {_SCRIPT_REL}. ⛔ Inlining the command here is what the "
        "script exists to stop: the hook, `make lint-e2e` and the CI job then "
        "drift independently, which is how the CI leg came to be missing."
    )
    silenced = _silencing_keys(hook, _precommit_config())
    assert not silenced, (
        f"the `playwright-lint` hook is off the commit path: {silenced}. "
        "⛔ These are checked by VALUE, not by key existence: adding `pre-push` "
        "alongside `pre-commit` is a legitimate widening and was measured to "
        "keep the hook running, so it must not red here. Each entry above was "
        "measured against real pre-commit in a throwaway repo — `stages: "
        "[manual]` and a top-level `default_stages: [manual]` both print "
        "\"No hook with id 'playwright-lint' in stage 'pre-commit'\" and exit "
        "0, while `types: [python]` and `exclude_types: [ts]` both print "
        "\"(no files to check) Skipped\" with `files:` still reading correctly."
    )
    assert "exclude" not in hook, (
        "the hook grew an `exclude:` pattern, which can empty its file set "
        "without touching `files:` — measured: `exclude: .*` reports "
        "'(no files to check) Skipped' while `files:` still reads correctly. "
        "If some path genuinely must be exempt, say so in `files:` where the "
        "whole selection is readable at once."
    )

    commands = _make_commands(_MAKEFILE.read_text(encoding="utf-8"), "lint-e2e")
    assert any(_is_sole_invocation(c) for c in commands), (
        f"`make lint-e2e` runs {commands!r}, none of which is a bare call to "
        f"{_SCRIPT_REL}. ⛔ Note a leading `-` counts as failure here: make's "
        "`-` prefix discards the line's exit status, so `-bash <script>` runs "
        "the lint and throws the answer away (measured: `make` rc=0 against a "
        "script that exited 1)."
    )

    _script_step()  # raises with its own message when no step runs the script


def test_the_rule_itself_cannot_be_disarmed_below_the_script() -> None:
    """⛔ The links under the script, which nothing was watching.

    Everything else in this file guards the chain down to `npm run lint`.
    Blind reviews walked what is below that, with a `test.fixme()` already
    injected into a real spec, and every one of these went green — script rc=0,
    whole repo green:

    * emptying the rule's options object — `disallowFixme` defaults to false,
      so `test.fixme()` reports nothing and `eslint .` exits 0;
    * `lint: "eslint . || exit 0"`, which keeps `eslint` as the first token
      while npm's shell throws the status away;
    * `lint: "eslint one-file.spec.ts"`, which keeps it while linting one file;
    * `ignores` gaining a spec glob — on its own line, on the SAME line as the
      opening bracket, or in a second config object further down;
    * `files:` narrowed off `**/*.spec.{ts,js}`, which removes specs from the
      run from the other side and was not read at all until a later round.

    Every one of them leaves the script, all three entry points and this file's
    other assertions untouched. ⛔ Counts were removed from this paragraph on
    purpose: each round of review has added a link to it.

    The options object, the ignore and files arrays are read as DATA — every
    occurrence of each key, entries split by quoted literal rather than by line,
    so formatting really is free. The `lint` script is pinned WHOLE instead,
    for the reason `_is_sole_invocation` gives: npm hands it to a shell, and
    sorting trailing tokens into harmless and silencing is the enumeration this
    file has already lost five times.
    """
    config = _ESLINT_CONFIG.read_text(encoding="utf-8")
    options = _rule_options(config)
    assert options.get("disallowFixme") is True, (
        f"`playwright/no-skipped-test` options are {options!r}. "
        "`disallowFixme` defaults to FALSE, so dropping it takes `test.fixme()` "
        "back to passing silently — measured against this exact config, with "
        "the options object emptied, an injected `test.fixme()` reports "
        "nothing and `eslint .` exits 0. The regression this repo already "
        "recorded once."
    )
    assert options.get("allowConditional") is False, (
        f"`allowConditional` is {options.get('allowConditional')!r}. Setting it "
        "true exempts a call when ANY of three things holds — it takes at "
        "least one argument, OR sits inside an `if` block, OR inside a "
        "`switch` case — none of which asks whether the condition means "
        "anything. Four documents were corrected rather than flipping this; "
        "flipping it now silently reverses that decision."
    )

    lint = _npm_scripts().get("lint", "")
    assert shlex.split(lint) == ["eslint", "."], (
        f"`package.json`'s `lint` script is {lint!r}, not `eslint .`. ⛔ Pinned "
        "whole rather than by first token: npm hands this to a shell, so "
        "`eslint . || exit 0` keeps `eslint` in front and still exits 0, and "
        "`eslint one-file.spec.ts` keeps it while linting one file. Both were "
        "measured against an injected `test.fixme()` — script rc=0, whole repo "
        "green. Same treatment as the three entry points above: anything other "
        "than the invocation is refused without being interpreted, so a "
        "deliberate change updates this line too."
    )

    offenders = {
        f"ignores{i}": [p for p in arr if ".spec." in p]
        for i, arr in enumerate(_js_string_arrays(config, "ignores"))
        if any(".spec." in p for p in arr)
    }
    assert not offenders, (
        f"eslint `ignores` contains {offenders}, which removes specs from the "
        "run while leaving the rule configured exactly as written — the "
        "quietest of the ways down here, because every assertion about the "
        "rule still reads true."
    )

    selected = _js_string_arrays(config, "files")
    assert selected and all(
        set(arr) >= {"**/*.spec.ts", "**/*.spec.js"} for arr in selected), (
        f"eslint `files:` is {selected}, which no longer selects every spec. "
        "⛔ This is the same link as `ignores:` approached from the other side "
        "and it was unread until a blind review narrowed it to a single file — "
        "injected `test.fixme()`, script rc=0, this file green. Narrowing here "
        "removes specs from the run without touching the rule, the script, or "
        "any of the three entry points."
    )


def test_the_ci_legs_signal_is_not_silently_switched_off() -> None:
    """⛔ What this can prove, and what it deliberately does NOT claim.

    It does NOT prove A-13 is enforced. Enforcement lives in branch protection,
    and neither this job nor `Smoke Tests (Chromium)` is among main's required
    contexts (checked against the API on 2026-08-14), so this leg can report
    red and a merge still happens. No in-repo test can read that setting. This
    test's earlier name — "cannot be switched off without this test noticing" —
    promised exactly that, and a blind review produced the counterexample: one
    `- '!tests/e2e/**'` line in `paths:` switched the leg off with this file at
    green, and so was `tests/ops/test_ci_path_filter_coverage.py`.

    What it proves is narrower and still worth having: the signal is not
    silenced without a red line appearing in a diff someone reads. The ways to
    silence it do not share a carrier, so each is checked where it lives —
    `if:`, `continue-on-error:` and `needs:` here, the invocation itself via
    `_is_sole_invocation`, and the `paths:` filter in the trigger test below.

    Measured on this very workflow: `if: false` on the job and
    `continue-on-error: true` on the step each left this whole file passing,
    with the only server-side execution point A-13 has turned off and nothing
    in the repo red. `tests/ops/test_ci_path_filter_coverage.py` does not cover this
    workflow — its scan is scoped to the ones using `dorny/paths-filter`.
    """
    job_id, job, step = _script_step()

    run = str(step.get("run", ""))
    assert _is_sole_invocation(run), (
        f"the step's `run:` is {run!r}, which is not just a call to "
        f"{_SCRIPT_REL}. ⛔ Refused without being interpreted — see "
        "`_is_sole_invocation` for why trailing tokens are not sorted into "
        "harmless and silencing. Measured under this step's actual shell (it "
        "sets no `shell:`, so `set -e` applies and `pipefail` does NOT): "
        "appending `|| true`, `|| exit 0`, `| tee out.txt` or `& wait` each "
        "makes the step exit 0 while the lint fails. If a wrapper is genuinely "
        "needed here, add it AND update this assertion, so it is a change "
        "somebody agreed to rather than one nobody saw."
    )

    assert "if" not in job, (
        f"job {job_id!r} grew an `if:` condition ({job.get('if')!r}). This job "
        "is the only place A-13 runs that does not depend on a developer's "
        "work tree; a condition on it is indistinguishable from deleting it, "
        "and the check reports nothing rather than reporting red."
    )
    assert "if" not in step, (
        f"the step running {_SCRIPT_REL} grew an `if:` ({step.get('if')!r}). "
        "A skipped step is a green job."
    )
    for where, node in (("job", job), ("step", step)):
        assert node.get("continue-on-error") in (None, False), (
            f"{where} sets `continue-on-error: {node.get('continue-on-error')!r}`. "
            "⛔ This is the cheapest way to make a red A-13 go away, and the "
            "workflow's own comments describe the leg as advisory — which is "
            "about branch protection, not about swallowing the result. An "
            "advisory check that cannot report red is decoration."
        )
    assert not job.get("needs"), (
        f"job {job_id!r} grew `needs: {job.get('needs')!r}`. GitHub skips a job "
        "whose dependency was skipped or failed, so a `needs:` here hands the "
        "off-switch to another job."
    )


def test_editing_a_spec_or_the_runner_triggers_the_workflow() -> None:
    """⛔ A path-filtered workflow does not run for files outside its filter.

    Two measured holes, opposite ends of the same rule: dropping
    `tests/e2e/**` from `paths:` (the trigger for the change class A-13 exists
    to catch) left this file green, and before #1428 the runner itself
    was outside the filter, so a pull request that only edited it never started
    the one CI leg that executes it.

    Evaluated, not string-matched: `tests/e2e/**` could legitimately be spelled
    another way, and an assertion on the literal would then demand a rewrite
    for a change that broke nothing.

    ⛔ Third hole, found by blind review and the reason
    `reject_unmodelled_patterns` is imported rather than rewritten here: the
    `any()` below absorbs a negation. `_match_segments` reads `!tests/e2e/**`
    as a literal first segment `"!tests"`, answers False, and the surviving
    positive pattern still reports "covered". Measured — adding that one line
    beside the existing `tests/e2e/**` (2 places) left this file and the
    shared module both green, with the leg off. The shared module had
    already written this rule down for `dorny/paths-filter` filters; importing
    it into this second caller is what stops the two answers from drifting.
    """
    # rglob, not glob: `test_hook_files_pattern_selects_specs_and_its_own_inputs`
    # below requires `tests/e2e/nested/thing.spec.js` to be selected, so a
    # non-recursive search here contradicts the same file two screens down —
    # measured, moving the suite into `tests/e2e/specs/` reported "no E2E
    # specs found", which reads as a deletion rather than a move.
    specs = sorted((_REPO_ROOT / "tests" / "e2e").rglob("*.spec.ts"))
    assert specs, "no E2E specs found — the sample below would prove nothing"
    sample = specs[0].relative_to(_REPO_ROOT).as_posix()

    for event in ("push", "pull_request"):
        branches = _trigger_branches(event)
        required = _branches_that_carry_required_checks(event)
        missing = [b for b in required if b not in branches] if branches else []
        assert not missing, (
            f"`on.{event}.branches` is {branches!r}, which omits {missing} — "
            f"branches `ci.yml` runs its required checks on. This leg then "
            "never runs for the event that matters and reports nothing rather "
            "than reporting red. ⛔ An ABSENT `branches:` is fine and "
            "deliberately tolerated: it means every branch, which is a "
            "superset. Widening is always allowed; only dropping one of "
            "ci.yml's branches reds."
        )
        patterns = _trigger_paths(event)
        if not patterns:
            # ⛔ Absent `paths:` means EVERY path, so the invariant below holds
            # trivially and widening must not red. The first version asserted
            # the list was non-empty, which made "run A-13 on every PR into
            # main" — a strict increase in coverage — indistinguishable from
            # deleting the filter's coverage, while the `branches:` assertion
            # two lines up documents the opposite treatment for the same YAML
            # shape. `paths-ignore:` lands here too, correctly: it also cannot
            # exclude a spec without excluding everything this asks about.
            continue
        for target, why in (
            (sample, "editing an E2E spec is the change class A-13 guards"),
            (_SCRIPT_REL, "editing the runner is the change most likely to break it"),
        ):
            assert any(
                _match_segments(pattern.split("/"), target.split("/"))
                for pattern in patterns
            ), (
                f"`on.{event}.paths` does not match {target!r} — {why}, and a "
                "pull request doing exactly that would not start this workflow."
            )


def test_hook_files_pattern_selects_specs_and_its_own_inputs() -> None:
    """Paired: the things that must be selected, and the things that must not.

    Only pinning the first half lets the pattern drift towards matching
    everything, which would make every commit in a fresh work tree demand an
    `npm ci`; only pinning the second half lets it drift to matching nothing.
    """
    pattern = re.compile(str(_hook()["files"]))

    for path in (
        "tests/e2e/portal-home.spec.ts",
        "tests/e2e/nested/thing.spec.js",
        "tests/e2e/eslint.config.mjs",
        "tests/e2e/package.json",
        "tests/e2e/package-lock.json",
        _SCRIPT_REL,
    ):
        assert pattern.search(path), f"{path!r} should be selected by the hook"

    for path in (
        ".pre-commit-config.yaml",
        "Makefile",
        "tests/e2e/playwright.config.ts",
        "tests/e2e/fixtures/axe-helper.ts",
        "docs/scenarios/gitops-ci-integration.md",
    ):
        assert not pattern.search(path), (
            f"{path!r} should NOT be selected — every extra match is a work "
            "tree that must run `npm ci` before it can commit an unrelated "
            "change."
        )


def test_the_script_probes_for_presence_not_for_an_executable_bit() -> None:
    """⛔ A static pin, because this one cannot be discriminated behaviourally.

    `-e` and `-x` are not distinguishable on the platform this repo is
    developed on: measured, `node_modules/.bin/eslint` reports as executable
    under Git Bash, so swapping the probe to `-x` leaves every behavioural case
    green. The distinction still matters — the executable bit carries no
    reliable meaning across the filesystems this runs on, and a work tree that
    is in fact ready must not be reported as missing — so it is pinned by
    reading the script instead. Same shape as the repo's other cross-platform
    rules: where behaviour cannot separate the two, pin the source.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    # ⛔ Tolerant of shell hygiene that changes nothing: quoting the path,
    # a `./` prefix, `[[ ]]`. An earlier version pinned the bare unquoted
    # literal, so adding quotes reported "the eslint probe uses []" and then
    # lectured about `-x` — a message that names neither the edit nor the
    # rule, for a change with zero behavioural effect.
    probes = re.findall(
        r"\[\[?\s*!\s*(-[a-z])\s+[\"']?(?:\./)?node_modules/\.bin/eslint[\"']?\s*\]",
        text)
    assert probes == ["-e"], (
        f"the eslint probe uses {probes!r}. It must be `-e`: `-x` asks about a "
        "permission bit that Windows filesystems do not carry meaningfully, "
        "and answering 'missing' for a tree that is installed sends the reader "
        "to run `npm ci` again, which will not help."
    )


def _throwaway_tree(tmp_path: Path) -> Path:
    """A repo-shaped tree holding a copy of the script, and nothing else."""
    root = tmp_path / "repo"
    (root / "scripts" / "tools" / "lint").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / "tools" / "lint" / _SCRIPT.name)
    return root


def _run(root: Path, extra_path: Path | None = None):
    env = dict(os.environ)
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [_BASH, str(root / "scripts" / "tools" / "lint" / _SCRIPT.name)],
        # ⛔ Explicit DEVNULL, matching the other subprocess calls in this file.
        # Without it these cases inherit whatever stdin the runner has, and a
        # runner that hands them a closed handle turns all four behavioural
        # cases into `OSError: [WinError 6]` — a failure that looks like the
        # script and is not. Measured: with the inherited handle they need
        # `pytest -s` to pass; with DEVNULL they pass either way.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def test_bash_is_available_wherever_this_suite_is_meant_to_run() -> None:
    """⛔ Without this, `_BASH = None` turns four cases into a silent skip.

    Measured: setting it to None leaves every behavioural case skipped and
    the file green — the
    skip-as-green shape this repo has a documented history with. The skips are
    legitimate on a developer box without bash; they are not legitimate on the
    runners, and every job in this repo's workflows is `ubuntu-latest`.
    """
    if not os.environ.get("CI"):
        pytest.skip("only meaningful where the suite is required to be complete")
    assert _BASH is not None, (
        "bash is not on PATH in CI, so the behavioural half of this file "
        "skipped rather than ran. A skipped guard reports the same green as a "
        "passing one."
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_missing_e2e_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """A checkout with no tests/e2e must fail loudly rather than report success.

    ⛔ The assertion names the script's own prefix, not the string "tests/e2e".
    Measured: deleting the guard entirely still satisfied a `"tests/e2e" in
    stderr` check, because `set -e` then makes bash print
    `cd: …/repo/tests/e2e: No such file or directory` — the path contains the
    needle. The message being the script's own is the whole point of it.
    """
    result = _run(_throwaway_tree(tmp_path))
    assert result.returncode != 0, (
        "the script exited 0 with no tests/e2e to lint — a green tick that "
        f"checked nothing.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "[A-13]" in result.stderr and "refusing" in result.stderr, (
        "the failure came from somewhere other than the script's own guard; a "
        "reader gets a bare `cd:` error and no idea what was supposed to run."
        f"\nstderr={result.stderr!r}"
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_missing_eslint_fails_and_names_the_remedy(tmp_path: Path) -> None:
    """⛔ Fail-closed, and say what to run.

    Both halves matter. The exit code is the guard; the message is why the
    guard survives contact with a developer. What every fresh `git worktree
    add` produced before #1428 was a bare `eslint: not found`, which names no
    remedy and reads as a broken checkout — and a check that looks broken gets
    switched off rather than fixed.
    """
    root = _throwaway_tree(tmp_path)
    (root / "tests" / "e2e").mkdir(parents=True)

    result = _run(root)
    assert result.returncode != 0, (
        "the script exited 0 with no eslint installed — it reported success "
        "for specs nothing had looked at."
    )
    assert "[A-13]" in result.stderr and "npm ci" in result.stderr, (
        "the failure does not tell the reader what to run; that is the whole "
        f"difference #1428 made.\nstderr={result.stderr!r}"
    )
    # The message must not offer skipping as an option: an error that names its
    # own bypass teaches the bypass.
    assert not re.search(r"--no-verify|SKIP=", result.stderr), (
        "the failure message names a way to skip the check. Do not put the "
        "bypass in the error text; a reader who wants it will find it, and a "
        "reader who does not should never see it suggested."
    )


def _install_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_a_correctly_installed_tree_exits_zero(tmp_path: Path) -> None:
    """⛔ The negative control. Without it, `exit 1` would satisfy this file.

    Stubs stand in for eslint and npm so the case measures the SCRIPT's
    decision — whether it reaches the lint and honours its exit code — rather
    than eslint's opinion of some fixture, which is covered where it belongs
    (`tests/e2e/eslint.config.mjs` plus the CI job that runs the real thing).
    """
    root = _throwaway_tree(tmp_path)
    e2e = root / "tests" / "e2e"
    (e2e / "node_modules" / ".bin").mkdir(parents=True)
    (e2e / "node_modules" / ".bin" / "eslint").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
    )

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    _install_stub(bin_dir, "npm", '#!/usr/bin/env bash\necho "stub npm $*"\nexit 0\n')

    result = _run(root, extra_path=bin_dir)
    assert result.returncode == 0, (
        "a correctly installed tree was rejected. This case exists so that a "
        "script which simply always fails cannot pass this file.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.skipif(_BASH is None, reason="bash is required to run the script")
def test_a_failing_lint_is_propagated(tmp_path: Path) -> None:
    """The script must not swallow the linter's verdict.

    ``exec`` makes this true by construction today, which is precisely why it
    needs pinning: replacing that line with anything that returns its own
    status is a one-token edit, and a blind review has already shown that the
    rest of the suite does not notice.
    """
    root = _throwaway_tree(tmp_path)
    e2e = root / "tests" / "e2e"
    (e2e / "node_modules" / ".bin").mkdir(parents=True)
    (e2e / "node_modules" / ".bin" / "eslint").write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8", newline="\n"
    )

    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    _install_stub(
        bin_dir, "npm", '#!/usr/bin/env bash\necho "stub npm rejects" >&2\nexit 1\n'
    )

    result = _run(root, extra_path=bin_dir)
    assert result.returncode != 0, (
        "the linter rejected the specs and the script still exited 0 — the "
        "one failure mode that turns this whole arrangement into decoration."
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
