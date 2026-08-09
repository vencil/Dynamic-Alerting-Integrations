"""Guards for the L2 secret-scan gate (`.github/workflows/secret-scan.yml`, #1364).

Two defects shipped together in that workflow, and they compounded:

  1. The PR diff scan was not bound to the PR branch. trufflehog's git source
     appends `--all` to its internal `git log` when no head is given, and it
     clones with `+refs/*:refs/remotes/origin/*`, so every branch
     `actions/checkout` fetched was walked. `--since-commit` does not bound it
     — it only stops the walk when the merge base is emitted, and another
     branch's commits are not ancestors of the merge base. Measured on #1363:
     2 of 3 findings lived in a file present on neither `main` nor the PR
     branch, so that PR could not be made green by any change to its own diff.

  2. The Lob detector matched ordinary 40-character identifiers, and its
     live-check confirmed every one of them, so they arrived as blocking
     `error`s telling the reader to run the rotate-first SOP.

⛔ The fix for (2) is a SCAN-LAYER detector exclusion, and that is a real loss
of coverage — which is why it is guarded here rather than merely commented.
The rejected alternatives are worth recording, because the obvious one is
wrong in a way that is only visible from the data:

  * `.trufflehogignore` on the offending test files — fail-open. It silences
    EVERY detector on those paths, including the ones that work.
  * A policy-layer exclusion in `trufflehog_to_sarif.py` for hits that land on
    a `def <name>(` identifier (the shape the issue proposed). It does not
    cover the class. The trigger is not "a Python function name": it is
    `(?i:lob)(?:.|[\\n\\r]){0,40}?\\b([a-zA-Z0-9_]{40})\\b` — any 40-character
    identifier within 40 characters of the letters "lob", which occur inside
    `global` and `blob`. One of the two live hits on `main` is
    `mysql_slave_status_seconds_behind_master`, a string in a list literal,
    which no `def`-name rule would ever see. It is also unimplementable in
    full-history mode: the reported path frequently no longer exists in the
    tree (measured — `tests/test_scaffold_db.py`, carrying several of the 242
    alerts, is not on `main`), so there is nothing to parse.
  * Keep Lob ENABLED and downgrade registered detectors to `warning` in the
    converter regardless of `Verified`. Raised in blind review, and it is the
    strongest of the three: it removes the actual harm (a blocking error that
    sends the reader to the rotate-first SOP) without a permanent hole in L2,
    and the data would still reach the Security tab if the project ever did
    integrate with Lob. Rejected on the standing-noise cost: it leaves 240+
    permanent warnings in Code Scanning, which is the broken-windows shape
    this repo has already paid for once — an audit signal known to always
    fire trains everyone to ignore it, so the day it catches something real
    it is lost in the expected noise. It also keeps posting junk credentials
    to a third-party API on every nightly run. ⚠️ This is a JUDGEMENT call,
    not a defect in the alternative; if the standing-warning cost is judged
    acceptable, it is the better design and this exclusion should be
    reversed.

⚠️ HONEST SCOPE. These are STATIC assertions over the workflow text. They
cannot prove trufflehog's runtime semantics — that was established out-of-band
against the pinned CLI (v3.95.3) in a synthetic repo shaped like an
`actions/checkout` workspace (detached HEAD, `refs/remotes/origin/*` for three
branches):

    A  --since-commit only              → 2 findings: mine AND another branch's
    B  + --branch <head sha>            → 1 finding: mine only
    C  + --exclude-detectors Lob        → 0
    D  C, plus a GitHub-token-shaped secret on my own branch → still found
    E  B, with the head commit reachable from NO ref at all   → still found

Cell D is the one that matters for fail-open: excluding a detector must not,
and does not, silence the others. Cell E answers the reachability question the
SHA form raises — a fork PR arrives via a direct SHA fetch with no local ref
pointing at it, and `--branch <sha>` still resolves there, so this bound does
not quietly break fork PRs.

⛔ Cell E was CHALLENGED in blind review on a sound-looking argument: trufflehog
clones `file://…` as a URL, git therefore uses transport rather than local
optimisation, and transport only carries objects reachable from a matched ref
— so the fork commit should be missing and every fork PR should hard-fail.
Re-measured both ways, and the difference is HEAD:

    HEAD left on `main`, commit unreferenced   → object ABSENT, rc=1, 0 findings
    HEAD DETACHED at that commit (real state)  → object PRESENT, rc=0, 1 finding

`git clone` advertises HEAD and transfers its commit even when no ref points at
it. The reviewer's repro moved HEAD; `actions/checkout` does not — it leaves
HEAD detached AT the scanned SHA (confirmed in the real run log). So cell E
stands, and the guarantee rests on that checkout `ref:` line — which is why
`test_the_scanned_commit_is_the_pr_head` pins it.

A second, separate measurement (blind review, one obvious secret in a
throwaway repo) established why `--fail-on-scan-errors` is now mandatory:

    good --branch                 → exit 0, 1 finding
    --branch <nonexistent sha>    → exit 0, 0 findings   ← green, scanned nothing
    same + --fail-on-scan-errors  → exit 1, 0 findings
    good --branch + the flag      → exit 0, 1 finding    (no false block)

The middle row is the one that matters: its output is byte-identical to a
clean scan, which is exactly what the PR describing this change had quoted as
its own evidence of success.

⚠️ What is NOT established: WHY every Lob hit comes back verified. The
detector marks a hit live on any 2xx from its verification endpoint, and 242
of 242 came back verified while every other detector returned 0 — that is
enough to say the signal carries no information, and it is deliberately not
written up as "the endpoint returns 200 for anything", which was not measured.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# ⛔ The two inputs are ALSO written out as literal repo-relative strings below
# (`_GUARDED_INPUTS`), not only in this segment form. verify_diff's text_map is
# built by scanning test sources for LITERAL path strings, so the
# `ROOT / "a" / "b"` form registers nothing — a guard that exists but is never
# selected for the diff that breaks it is the failure mode this file is about.
# Measured: with only the segment form, editing either input selected three
# other tests and not this one.
WORKFLOW = ROOT / ".github" / "workflows" / "secret-scan.yml"
CONVERTER = ROOT / "scripts" / "tools" / "lint" / "trufflehog_to_sarif.py"

_GUARDED_INPUTS = (
    ".github/workflows/secret-scan.yml",
    "scripts/tools/lint/trufflehog_to_sarif.py",
)

# The env var both scan steps must read the exclusion list from. Naming it once
# is the point: two steps that each spell out their own list drift apart, and
# the nightly is the half nobody watches.
EXCLUDE_ENV = "TRUFFLEHOG_EXCLUDE_DETECTORS"

# Detectors this repo switches off at the scan layer, with the premise each one
# rests on. A membership list alone would be the fail-open shape this file
# exists to prevent, so `markers` is re-derived from the tree below: the
# exclusion is only defensible while the project genuinely has no such
# integration, and the day it gains one this test says so.
_JUSTIFIED_EXCLUSIONS: dict[str, dict[str, object]] = {
    "Lob": {
        # ⛔ NOT just the domain. An integration through the official SDK
        # (`import lob` + a key from the secret store) never writes
        # `lob.com` into the tree at all — i.e. the likeliest way for this
        # exclusion to go stale is precisely the way a domain-only marker
        # cannot see. Blind review caught this; the extra two markers cost
        # nothing and cover the SDK shape.
        "markers": ("lob.com", "LOB_API_KEY", "import lob"),
        "why": (
            "matches any 40-char identifier within 40 chars of the letters "
            "'lob' (which occur inside `global` / `blob`), and its verifier "
            "confirms every one of them live — 242 of 242 findings on this "
            "repo arrived Verified, versus 0 verified from every other "
            "detector. Verified results bypass trufflehog's own "
            "known-false-positive filter, so they reach the gate as blocking "
            "errors pointing at the rotate-first SOP."
        ),
        "exit": "the project integrates with Lob (api.lob.com).",
    },
}

# Files that contain a marker only because they DOCUMENT the exclusion — the
# evidence comment, this module, and the CHANGELOG entry. Listed rather than
# pattern-matched, and asserted to exist, so a rename cannot quietly widen the
# carve-out.
#
# ⚠️ The carve-out is per-FILE and applies to every marker of every detector.
# An earlier revision of this comment told you to keep it to "files that can
# only ever hold prose" and then listed a WORKFLOW first — a rule that excluded
# its own first entry (blind review). The honest statement of the criterion is
# narrower: a file belongs here only if the marker in it is *describing this
# exclusion*, and the file is one where an actual provider integration would be
# absurd. secret-scan.yml qualifies on the second clause (a Lob client will not
# live in the secret-scan workflow) — not because it is prose.
#
# (CHANGELOG.md earns its place the hard way: this guard reddened on the very
# entry describing the fix, because that entry names the verification endpoint.)
_MARKER_DOC_FILES = (
    ".github/workflows/secret-scan.yml",
    "tests/ops/test_secret_scan_gate.py",
    "CHANGELOG.md",
    # Added pre-emptively (blind review): this SOP now carries a paragraph
    # describing the exclusion, so the next person to make it concrete by
    # naming the endpoint would trip a security gate for writing correct
    # documentation. A predictable false RED is how carve-outs get widened
    # under pressure; better to place it deliberately now.
    "docs/internal/secret-leak-remediation-sop.md",
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on_block() -> dict:
    """The `on:` triggers. PyYAML parses a bare `on:` key as the BOOLEAN True
    (YAML 1.1), the trap this repo documents elsewhere — accept both."""
    wf = _wf()
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict), (
        f"could not read the `on:` block of {WORKFLOW.name} — got {on!r}. "
        "Every trigger assertion below would be vacuous."
    )
    return on


def _steps() -> list[dict]:
    """Every step of every job, with its job dict attached as `_job`.

    Steps are returned WHOLE (not just `run:`) because the properties that
    decide whether a command runs at all — `if:`, `env:`, `continue-on-error`
    — live on the step and on its job, and an earlier revision of this module
    read only `step["run"]` and was therefore blind to all of them.
    """
    out: list[dict] = []
    for job in _wf()["jobs"].values():
        for step in job.get("steps", []):
            step = dict(step)
            step["_job"] = job
            out.append(step)
    assert out, f"no steps found in {WORKFLOW.name} — parser broke, not the repo"
    return out


def _run_blocks() -> list[tuple[str, str]]:
    """Every `run:` script in the workflow, as (step name, script)."""
    out = [(s.get("name") or "<unnamed>", s["run"])
           for s in _steps() if isinstance(s.get("run"), str)]
    assert out, f"no `run:` steps found in {WORKFLOW.name} — parser broke, not the repo"
    return out


def _trufflehog_invocations() -> list[tuple[str, str, dict]]:
    """(step name, whole invocation on one logical line, the STEP dict).

    Backslash continuations are folded first: the real command is spread over
    several lines, and a per-line scan would see `--branch` and
    `--since-commit` as unrelated fragments.

    The third element is the step (not just its script) so callers can reach
    `if:` / `env:` — see `_steps`.
    """
    found: list[tuple[str, str, dict]] = []
    for step in _steps():
        script = step.get("run")
        if not isinstance(script, str):
            continue
        name = step.get("name") or "<unnamed>"
        folded = re.sub(r"\\\n\s*", " ", script)
        for line in folded.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment quoting the command is not an invocation
            if re.search(r"\btrufflehog\s+git\b", stripped):
                found.append((name, stripped, step))
    return found


def _env_block() -> dict:
    return _wf().get("env") or {}


# (A `_truthy_yaml` helper lived here and is deliberately gone. It tried to
# decide whether a `continue-on-error` VALUE was enabled, which drags in the
# PyYAML-vs-GitHub-Actions disagreement over the YAML 1.1 bool family — PyYAML
# resolves `off` / `no` to False, GHA does not treat them as booleans at all.
# The guard now asserts the KEY IS ABSENT, which is the property we actually
# want and needs no parser agreement.)


def _diff_scans() -> list[tuple[str, str, dict]]:
    """The PR-mode invocation, identified by `--since-commit`.

    Identified by the flag rather than by step name, so renaming the step
    cannot drop it out of the guards that use it.
    """
    invocations = _trufflehog_invocations()
    assert len(invocations) >= 2, (
        f"expected at least the two scan modes, found {len(invocations)} "
        "trufflehog invocation(s) — the discovery regex stopped matching, and "
        "every assertion depending on it would pass vacuously"
    )
    diff = [t for t in invocations if "--since-commit" in t[1]]
    assert len(diff) == 1, (
        f"expected exactly one --since-commit (diff-mode) invocation, got "
        f"{[n for n, _, _ in diff]}"
    )
    return diff


def _full_scans() -> list[tuple[str, str, dict]]:
    """Every invocation that is NOT the diff scan (i.e. the nightly full scan)."""
    full = [t for t in _trufflehog_invocations() if "--since-commit" not in t[1]]
    assert full, (
        "no full-history (non---since-commit) invocation found — the nightly "
        "is the only cross-branch net this design keeps, so its disappearance "
        "must not be silent"
    )
    return full


def _git_log_pickaxe(needle: str) -> list[str]:
    """Commits reachable from HEAD that ever ADDED or REMOVED `needle`
    OUTSIDE the files documenting the exclusion.

    ⛔ `git grep` sees only the working tree, but the exclusion applies to the
    NIGHTLY FULL-HISTORY scan too — and the nightly's entire purpose is
    finding secrets that were committed and later deleted. A credential
    removed together with its marker is invisible to a tree-only check by
    construction.

    Three things this got wrong on the first cut, all from blind review:

    * **`--all` walked every fetched ref (144 locally).** A marker on somebody
      else's unmerged branch would then red THIS PR, with the author unable to
      clear it by changing their own diff — the exact pathology #1364 exists
      to fix, recreated one level up in the guard that fixes it. Scoped to
      HEAD's history: the corpus a PR is answerable for.
    * **The doc carve-out was COMMIT-scoped.** One commit touching both a doc
      file and a real integration was exempted wholesale. Now PATH-scoped via
      pathspec, so only the doc files' own changes are excused.
    * **Case.** `git grep -i` is case-insensitive but `-S` is a fixed string
      and case-SENSITIVE (measured: `-S changelog` 67 commits vs `-S CHANGELOG`
      143), so the two halves of one premise disagreed. `--pickaxe-regex -i`
      makes them agree.
    """
    excludes = [f":(exclude){doc}" for doc in _MARKER_DOC_FILES]
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "log", "HEAD", "--format=%H %s",
         "-i", "--pickaxe-regex", "-S", re.escape(needle), "--", ".", *excludes],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"`git log -S {needle!r}` failed (rc={proc.returncode}): "
        f"{proc.stderr.strip()[:300]}"
    )
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _git_grep_files(needle: str) -> list[str]:
    """Tracked files containing `needle`, case-insensitively. Fail-closed."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-I", "-i", "-l", "-F", "--", needle],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    # git grep: 0 = matches, 1 = no matches, >1 = actual failure.
    assert proc.returncode in (0, 1), (
        f"`git grep -F {needle!r}` failed (rc={proc.returncode}): "
        f"{proc.stderr.strip()[:300]}\nThis guard must not degrade to "
        "'found nothing', which is indistinguishable from 'premise holds'."
    )
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


# ── the two defects ─────────────────────────────────────────────────────────


def test_the_scanned_commit_is_the_pr_head() -> None:
    """`--branch <sha>` only works because checkout leaves HEAD at that commit.

    The bound is `git rev-parse HEAD`, so whatever `actions/checkout` checks
    out IS the scanned range's endpoint. Two things depend on the `ref:` line:

      * scanning the PR head rather than the virtual merge commit (the whole
        point — a merge-ref scan covers a different commit set); and
      * the fork case working at all. trufflehog clones the workspace over git
        transport, which carries only ref-reachable objects PLUS the
        advertised HEAD. A fork PR's commit has no local ref, so it rides
        along solely because HEAD is detached at it. Move HEAD and the object
        is genuinely absent — measured, see the module docstring.

    Deleting this one line therefore changes the scanned range AND removes the
    only reason fork PRs resolve, with every other guard here still green.
    """
    checkout = [s for s in _steps()
                if "actions/checkout" in str(s.get("uses") or "")]
    assert len(checkout) == 1, (
        f"expected exactly one checkout step, found {len(checkout)}"
    )
    ref = str((checkout[0].get("with") or {}).get("ref") or "")
    assert "pull_request.head.sha" in ref, (
        f"the checkout `ref:` is {ref!r}, which no longer pins the PR head "
        "SHA.\nWithout it HEAD is the virtual merge commit, so "
        "`--branch $(git rev-parse HEAD)` bounds the scan to a DIFFERENT "
        "commit set — silently, with every other assertion here still "
        "passing (#1364)."
    )


def test_guarded_inputs_are_named_literally() -> None:
    """Keep `_GUARDED_INPUTS` honest so it cannot be tidied away as dead weight.

    It exists to put both paths into verify_diff's text_map, which is what makes
    a change to either input SELECT this module. Asserting it equals the paths
    actually used means a rename cannot leave a stale literal behind, and a
    future cleanup that deletes the constant reds here instead of silently
    unhooking the guard from its own inputs.
    """
    assert set(_GUARDED_INPUTS) == {
        WORKFLOW.relative_to(ROOT).as_posix(),
        CONVERTER.relative_to(ROOT).as_posix(),
    }
    for rel in _GUARDED_INPUTS:
        assert (ROOT / rel).is_file(), f"{rel} does not exist"


def test_diff_scan_is_bound_to_the_pr_head() -> None:
    """The PR-mode scan must pass `--branch`, resolved to the checked-out commit.

    The diff-mode invocation is identified by `--since-commit` rather than by
    step name, so renaming the step cannot drop it out of this guard.

    ⛔ The accepted values are deliberately narrow, and NOT merely "some
    `--branch` is present". `--branch HEAD` and `--branch <name>` both look
    correct and are both wrong here: trufflehog scans a CLONE of the workspace,
    `actions/checkout` leaves that workspace on a detached HEAD with no
    `refs/heads/*`, so the clone has no local branch and its HEAD does not
    point at the PR head. A full SHA skips ref resolution altogether
    (`resolveHash` → `plumbing.IsHash`). Rejecting an unfamiliar-but-correct
    spelling costs a nuisance red; accepting a familiar-but-wrong one restores
    the bug silently.
    """
    invocations = _trufflehog_invocations()
    assert len(invocations) >= 2, (
        f"expected at least the two scan modes, found {len(invocations)} "
        "trufflehog invocation(s) — the discovery regex stopped matching, and "
        "every assertion below would pass vacuously"
    )

    diff_scans = _diff_scans()
    name, cmd, step = diff_scans[0]
    script = step["run"]

    m = re.search(r"--branch[=\s]+\"?\$\{?(\w+)\}?\"?", cmd)
    assert m, (
        f"the diff scan ({name}) does not pass `--branch <shell var>`:\n  {cmd}\n"
        "Without it trufflehog's git source runs `git log --all` over every ref "
        "its clone fetched, and another branch's finding blocks THIS PR — a red "
        "the author cannot clear by changing their own diff (#1364)."
    )
    var = m.group(1)

    # Behavioural, not spelling-deep: the variable has to be assigned from the
    # resolved HEAD commit in the same script.
    assigned = re.search(rf"^\s*{re.escape(var)}=\$\(\s*git rev-parse HEAD\s*\)",
                         script, re.M)
    assert assigned, (
        f"`--branch ${var}` is passed, but ${var} is not assigned from "
        f"`git rev-parse HEAD` in the same step.\n"
        "It must be a RESOLVED SHA: `HEAD` and branch names do not resolve in "
        "the clone trufflehog makes (detached workspace HEAD, no refs/heads/*)."
    )


def test_pull_requests_actually_reach_the_bounded_scan() -> None:
    """⛔ The bound is worthless if PRs are routed past the step that carries it.

    The first cut of this module asserted only that a `--branch`-bearing
    command EXISTS in the file. It never read `on:` or any `if:` — so the
    command could sit there untouched while `pull_request` was routed to the
    unbounded nightly branch, and all four tests stayed green. Blind review
    named three one-token mutations that do it: spelling `pull_request` as
    `pull_request_target` in the mode step, flipping the two `if:`
    expressions, or dropping `synchronize` from `types:` so only a PR's first
    push is ever scanned.

    This is the repo's own "gate exists but never runs" shape, committed by a
    guard written to prevent that shape.
    """
    on = _on_block()
    assert "pull_request" in on, "the gate no longer triggers on pull_request at all"
    # ⛔ An ABSENT `types:` is not a hole: GitHub's default for pull_request is
    # exactly [opened, synchronize, reopened]. Requiring the list to be spelled
    # out would red a semantically identical workflow — a false RED on a
    # security gate, which is how carve-outs get widened. Blind review.
    pr_on = on["pull_request"] or {}
    types = pr_on.get("types") or ["opened", "synchronize", "reopened"]
    missing = {"opened", "synchronize", "reopened"} - set(types)
    assert not missing, (
        f"`on.pull_request.types` is missing {sorted(missing)} (has {types}).\n"
        "Without `synchronize` only a PR's FIRST push is scanned and every "
        "later commit inherits that green — the gate becomes advisory without "
        "anyone editing a scan command."
    )

    # The mode step must map the pull_request event onto the bounded branch.
    mode_scripts = [s for _, s in _run_blocks() if "GITHUB_OUTPUT" in s and "mode=" in s]
    assert len(mode_scripts) == 1, (
        f"expected exactly one step computing the scan mode, found {len(mode_scripts)}"
    )
    mode = mode_scripts[0]
    assert re.search(r'github\.event_name\s*\}\}"?\s*=\s*"pull_request"', mode), (
        "the mode step no longer branches on exactly `pull_request`:\n"
        f"{mode}\nA near-miss spelling (e.g. `pull_request_target`) sends every "
        "PR down the `mode=full` path, which is deliberately UNBOUNDED."
    )
    # ⛔ Not just "both strings are present somewhere" — `mode=pr` must be in
    # the THEN branch. Blind review: swapping the two `echo` lines leaves the
    # `if:` gates untouched and every other assertion here satisfied, while
    # every PR runs the unbounded scan. Same equivalence class as "flip the
    # two `if:`s", entered from the other side.
    then_branch = mode.split("else", 1)[0]
    assert re.search(r"mode=pr\b", then_branch), (
        "the pull_request branch of the mode step no longer emits `mode=pr`:\n"
        f"{mode}\nIf `mode=full` is emitted for pull_request events, every PR "
        "runs the deliberately UNBOUNDED scan while the bounded command sits "
        "unused in the file."
    )
    assert re.search(r"mode=full\b", mode.split("else", 1)[-1]), (
        "the non-pull_request branch no longer emits `mode=full`"
    )

    # ⛔ JOB-level `if:` too. A job that is skipped reports `skipped`, and this
    # repo has already documented that a skipped required check SATISFIES
    # branch protection — so `if: github.event_name == 'schedule'` on the job
    # makes every PR scan disappear while the workflow still "triggers" and
    # every other assertion in this module stays green. Blind review; the
    # first cut read `on:` and the two STEP `if:`s and stopped there.
    gated_jobs = {name: job.get("if") for name, job in _wf()["jobs"].items()
                  if (job or {}).get("if")}
    assert not gated_jobs, (
        f"job-level `if:` present: {gated_jobs!r}. A skipped job reports "
        "`skipped`, which SATISFIES branch protection — the whole gate would "
        "vanish for PRs with nothing turning red (#1364). "
        "(An earlier cut looped over steps and `break`ed after the first, so "
        "it only ever inspected one job.)"
    )

    # ⛔ …and the CONVERT step, which owns the verified→exit-1 policy. Gate it
    # on anything (`if: github.event_name == 'schedule'` is the one-liner) and
    # PRs stop applying the policy entirely, both uploads skip with it, and
    # nothing else in this module notices. Blind review — the FOURTH place in
    # this change where a guard failed to read an `if:`.
    for step in _steps():
        if "trufflehog_to_sarif.py" in (step.get("run") or ""):
            assert not step.get("if"), (
                f"the converter step is conditional ({step.get('if')!r}). Its "
                "exit code IS the verified-finding policy; gating it means the "
                "policy silently does not apply to whatever the condition "
                "excludes (#1364)."
            )

    # …and the two scan steps must be gated on those two modes, the right way round.
    diff_if = str(_diff_scans()[0][2].get("if") or "")
    assert "mode == 'pr'" in diff_if, (
        f"the --since-commit (bounded) scan is gated on {diff_if!r}, not on "
        "`steps.mode.outputs.mode == 'pr'` — PR events may not reach it."
    )
    for name, _cmd, step in _full_scans():
        full_if = str(step.get("if") or "")
        assert "mode == 'full'" in full_if, (
            f"the unbounded scan ({name}) is gated on {full_if!r}, not on "
            "`steps.mode.outputs.mode == 'full'` — it could run for PRs."
        )


def test_full_scan_is_not_branch_bounded() -> None:
    """The other half of the pair — and the half nobody would think to check.

    The diff scan's comment calls `--branch` "load-bearing, not an
    optimisation", so the two invocations now read asymmetrically. The obvious
    tidy-up is to "restore symmetry" by adding `--branch` to the nightly —
    which would silently delete the only cross-branch coverage this design
    keeps, and every other assertion here would stay green.

    Asserted as its own test rather than folded into the bounding test,
    because it is the opposite property and must fail with its own message.
    """
    offenders = [(n, c) for n, c, _ in _full_scans() if "--branch" in c]
    assert not offenders, (
        "the full-history scan is branch-bounded:\n"
        + "\n".join(f"  {n}: {c}" for n, c in offenders)
        + "\nThe nightly is deliberately unbounded — a secret on an unmerged "
          "branch is still a leaked secret, and this scan is the only thing "
          "that looks at one. Bounding it removes that coverage entirely "
          "while every other guard in this module stays green (#1364)."
    )


def test_nightly_full_scan_is_actually_scheduled() -> None:
    """This design hands ALL cross-branch coverage to the nightly. Pin that it runs.

    Deleting the `cron:` line leaves both invocations, both `if:`s and both
    exclusions intact — so without this, the sole cross-branch net can be
    removed with no test noticing, and `workflow_dispatch` (manual) would be
    the only way it ever fires.
    """
    on = _on_block()
    schedule = on.get("schedule") or []
    crons = [e.get("cron") for e in schedule if isinstance(e, dict)]
    assert any(c for c in crons), (
        f"`on.schedule` declares no cron ({schedule!r}). The full-history scan "
        "is the only cross-branch coverage left after the PR scan was bounded "
        "to its own branch; if it is not scheduled, nothing scans other "
        "branches at all (#1364)."
    )


def test_scan_errors_are_fatal() -> None:
    """⛔ A scan error must not present as "nothing found".

    trufflehog reports scan errors through `reporter.ChunkErr` and still
    EXITS 0 unless `--fail-on-scan-errors` is given (main.go gates
    `Snapshot().Errors` on that flag, default false). So an unresolvable
    `--branch` yields an EMPTY findings.json, the converter prints "no secrets
    detected", and the job goes GREEN having scanned nothing — output
    byte-identical to a clean scan.

    Measured on the pinned CLI in a throwaway repo carrying one obvious secret:
      good --branch                 → exit 0, 1 finding
      --branch <nonexistent sha>    → exit 0, 0 findings   ← the silent green
      same + --fail-on-scan-errors  → exit 1, 0 findings
      good --branch + the flag      → exit 0, 1 finding    (no false block)

    This matters more here than it would have before: bounding the scan to a
    resolved SHA is a NEW dependency on resolution succeeding, so the fix for
    defect 1 widened the mouth of this pre-existing silent channel.
    """
    invocations = _trufflehog_invocations()
    # Own floor, not borrowed from a sibling test: if the discovery regex
    # stops matching (e.g. the command moves into a shell script), `offenders`
    # is empty and this passes without looking at anything. Blind review noted
    # this was the one new assertion without one.
    assert len(invocations) >= 2, (
        f"expected at least the two scan modes, found {len(invocations)} — "
        "this assertion would otherwise pass vacuously"
    )
    offenders = [(n, c) for n, c, _ in invocations
                 if "--fail-on-scan-errors" not in c]
    assert not offenders, (
        "these trufflehog invocations do not pass --fail-on-scan-errors:\n"
        + "\n".join(f"  {n}: {c}" for n, c in offenders)
        + "\nWithout it a scan that fails outright exits 0 with zero findings, "
          "which this gate reports as success (#1364)."
    )


def test_the_blocking_step_cannot_be_made_advisory() -> None:
    """`continue-on-error` on a load-bearing step turns L2 into decoration.

    The exit codes that matter are the scanner's (did the scan complete?) and
    the converter's (was a live credential confirmed?). Either one, swallowed,
    produces a green job that proves nothing.

    ⛔ SCOPE. The first cut of this guard covered ONLY the converter step,
    which left the two trufflehog scan steps completely unguarded — and a
    single `continue-on-error: true` there restores the exact silent green
    this module exists to prevent (the scan dies, `> findings.json` has
    already created an empty file, the converter reads it and reports "no
    secrets detected"). Blind review. The scope is now derived from what each
    step RUNS, not from step names.

    Checks the JOB level too: `continue-on-error` is valid there and swallows
    every step under it.
    """
    offenders: list[str] = []
    checked = 0
    for step in _steps():
        script = step.get("run") or ""
        # Steps whose exit code is load-bearing: the scanner and the policy
        # converter. Derived from what the script runs, not from step names.
        if not re.search(r"\btrufflehog\s+git\b|trufflehog_to_sarif\.py", script):
            continue
        checked += 1
        name = step.get("name") or "<unnamed>"
        # ⛔ ABSENCE, not falsiness. PyYAML resolves the YAML 1.1 bool family
        # (`off` / `no`) to False while GitHub Actions does not treat them as
        # booleans at all — the very divergence `_on_block` documents for the
        # `on:` key. `continue-on-error: off` would therefore read as disabled
        # here and possibly enabled there. Asserting the key is absent sidesteps
        # the whole parser-disagreement class. (Blind review.)
        if "continue-on-error" in step:
            offenders.append(f"{name} (step-level continue-on-error)")
        if "continue-on-error" in (step["_job"] or {}):
            offenders.append(f"{name} (JOB-level continue-on-error swallows it)")
        # ⛔ Fold continuations FIRST. The commands are spread over several
        # backslash-continued lines, so a same-line scan for a swallow misses
        # the spelling that actually occurs — `|| true` parked on the last
        # argument line. Measured: that mutation survived the unfolded form.
        #
        # ⛔ DERIVED, not an enumeration of swallow spellings. An earlier cut
        # looked for `||` only, so `; exit 0`, a trailing `exit 0`, `set +e`
        # and `| tee` all passed. The property is "nothing in this script
        # discards a non-zero exit", so the check is on the disabling
        # mechanisms themselves.
        folded = re.sub(r"\\\n\s*", " ", script)
        for pattern, why in (
            (r"set\s+\+e", "`set +e` disables abort-on-error"),
            (r"\|\|", "`|| ...` swallows a non-zero exit"),
            (r";\s*exit\s+0", "`; exit 0` forces success"),
            (r"^\s*exit\s+0\s*$", "a trailing `exit 0` forces success"),
        ):
            if re.search(pattern, folded, re.M):
                offenders.append(f"{name} ({why})")
    # Anti-vacuity: a renamed converter or scanner makes the loop body
    # unreachable and every assertion above trivially true.
    assert checked >= 2, (
        f"expected at least the scan and convert steps, found {checked} with a "
        "load-bearing exit code — this guard would otherwise pass by never "
        "looking at anything"
    )
    assert offenders == [], (
        "the verified-finding policy step is marked non-fatal: "
        f"{offenders}\nThe converter exits 1 on a confirmed-live credential; "
        "if that does not fail the job, L2 is advisory and nothing blocks the "
        "merge (#1364)."
    )


def test_both_scan_modes_exclude_the_same_detectors() -> None:
    """Neither mode may carry its own exclusion list.

    A property enforced on only one of a pair is the failure this repo keeps
    paying for: the nightly is the half nobody reads, so it is exactly the half
    that would keep mailing 240 'confirmed live credential' reports after the
    PR path went quiet.
    """
    invocations = _trufflehog_invocations()
    assert len(invocations) >= 2, "discovery found fewer than two scan modes"

    offenders = [(n, c) for n, c in ((n, c) for n, c, _ in invocations)
                 if f'--exclude-detectors "${{{EXCLUDE_ENV}}}"' not in c]
    assert not offenders, (
        "these trufflehog invocations do not read the shared exclusion list "
        f'`--exclude-detectors "${{{EXCLUDE_ENV}}}"`:\n'
        + "\n".join(f"  {n}: {c}" for n, c in offenders)
        + "\nBoth scan modes must resolve detector scope from the same env var; "
          "a per-step literal is how the two halves drift apart."
    )

    env = _env_block()
    assert EXCLUDE_ENV in env, (
        f"{EXCLUDE_ENV} is not defined in the workflow-level `env:` block — the "
        "steps reference it, so the registry below would be validating a value "
        "no scan ever receives.\n"
        "(Note the two distinct cases: UNDEFINED is a LOUD failure — the steps "
        "run under `set -u`, so the expansion aborts the step. What silently "
        "excludes nothing is an env var defined as the EMPTY STRING; measured, "
        "trufflehog accepts `--exclude-detectors \"\"` and applies no "
        "exclusion. An earlier revision of this message conflated the two.)"
    )


def test_the_exclusion_list_is_not_overridden_closer_to_the_scan() -> None:
    """⛔ The registry validates the WORKFLOW-level value. Nothing else may win.

    GitHub Actions env precedence is step > job > workflow, and the diff-scan
    step already carries an `env:` block (for BASE_REF) — so a single added key
    there, or one at job level, silently feeds the scans a different detector
    scope while `test_every_excluded_detector_is_justified_and_still_unused`
    keeps happily validating the workflow-level value that nothing reads.

    That is this module's own stated failure shape ("a property enforced on
    only one of a pair") reappearing one layer down: the first cut enforced it
    across the two scan MODES but not across the env LAYERS. Blind review
    caught it; the mutation is one line.
    """
    offenders: list[str] = []
    for step in _steps():
        name = step.get("name") or "<unnamed>"
        if EXCLUDE_ENV in (step.get("env") or {}):
            offenders.append(f"step {name} env:")
        if EXCLUDE_ENV in ((step["_job"] or {}).get("env") or {}):
            offenders.append("job-level env:")
        # ⛔ …and the SHELL layer, which beats every YAML layer. Blind review:
        # the first cut read only the two `env:` keys, so `export VAR=...`
        # inside the script, an inline `VAR=... trufflehog ...` prefix, or a
        # `>> "$GITHUB_ENV"` write from an earlier step all won silently while
        # the registry kept validating the workflow-level value.
        script = step.get("run") or ""
        for pattern, why in (
            (rf"\bexport\s+{re.escape(EXCLUDE_ENV)}=", "`export` in the script"),
            (rf"^\s*{re.escape(EXCLUDE_ENV)}=\S+\s+\S", "inline `VAR=... cmd` prefix"),
            (rf"{re.escape(EXCLUDE_ENV)}=[^\n]*>>\s*\"?\$\{{?GITHUB_ENV",
             "written to $GITHUB_ENV"),
            (rf"^\s*{re.escape(EXCLUDE_ENV)}=", "reassigned in the script"),
        ):
            if re.search(pattern, script, re.M):
                offenders.append(f"step {name} ({why})")
    assert not offenders, (
        f"{EXCLUDE_ENV} is redefined closer to the scan than the workflow-level "
        f"`env:` block: {sorted(set(offenders))}\n"
        "Step- and job-level env WIN over workflow-level, so the detector scope "
        "actually applied would no longer be the one the justification registry "
        "in this module checks. Keep the single definition at workflow level "
        "(#1364)."
    )


def test_every_excluded_detector_is_justified_and_still_unused() -> None:
    """The exclusion list is held to a registry, AND the registry's premise is re-derived.

    Two separate things. Membership stops the list growing without review;
    re-derivation stops a *stale* entry outliving the fact that justified it.
    The second is the one that keeps this honest — the day someone wires up the
    excluded provider, an exclusion that was reasonable becomes a blind spot,
    and nothing else in CI would notice.
    """
    raw = str(_env_block().get(EXCLUDE_ENV, ""))
    excluded = [d.strip() for d in raw.split(",") if d.strip()]

    unjustified = sorted(set(excluded) - set(_JUSTIFIED_EXCLUSIONS))
    assert not unjustified, (
        f"detector(s) excluded from the secret scan with no registered "
        f"justification: {unjustified}\n"
        "Add an entry to _JUSTIFIED_EXCLUSIONS with evidence, the markers that "
        "prove this repo does not use the provider, and an EXIT condition — or "
        "do not exclude it. A scan-layer exclusion is a permanent hole in L2."
    )

    # ⛔ …and the other direction. Emptying the env var makes `excluded` empty,
    # every loop below run zero times, and defect 2 return with this module
    # fully green — the mutation its own error message describes without
    # asserting. Derived rather than "the list must be non-empty": a registry
    # entry whose premise still holds must still be APPLIED, so the only way to
    # drop a detector from the scan config is to drop it from the registry too,
    # which is a visible decision. (Blind review.)
    unapplied = sorted(set(_JUSTIFIED_EXCLUSIONS) - set(excluded))
    assert not unapplied, (
        f"detector(s) registered as excluded but NOT in {EXCLUDE_ENV}: "
        f"{unapplied}. Either exclude them, or delete the registry entry — "
        "leaving the justification behind while the exclusion is gone means "
        "the next reader believes a decision that is no longer in force, and "
        "the false positives it documents come back (#1364)."
    )

    # Anti-vacuity for the search mechanism itself: prove `git grep` finds
    # something that certainly exists before trusting it to find nothing.
    control = _git_grep_files("trufflehog")
    assert len(control) > 3, (
        f"`git grep -F trufflehog` matched only {len(control)} file(s) — the "
        "search is broken, so every 'provider not used' result below would be "
        "a false negative"
    )

    for path in _MARKER_DOC_FILES:
        assert (ROOT / path).is_file(), (
            f"{path} is in _MARKER_DOC_FILES but does not exist — the carve-out "
            "would silently cover nothing, or the wrong thing"
        )

    for detector in excluded:
        entry = _JUSTIFIED_EXCLUSIONS[detector]
        assert len(str(entry["why"])) > 60, f"{detector}: needs real evidence, not a label"
        assert len(str(entry["exit"])) > 20, f"{detector}: needs an EXIT condition"
        markers = entry["markers"]
        assert markers, f"{detector}: no markers — the premise cannot be re-derived"
        for marker in markers:  # type: ignore[union-attr]
            hits = [f for f in _git_grep_files(str(marker))
                    if f not in _MARKER_DOC_FILES]
            assert not hits, (
                f"the {detector} detector is excluded from the secret scan on "
                f"the premise that this project does not use it, but {marker!r} "
                f"now appears in: {hits}\n"
                f"EXIT condition for this exclusion: {entry['exit']}\n"
                "Re-enable the detector (drop it from "
                f"{EXCLUDE_ENV} in .github/workflows/secret-scan.yml), or "
                "record why the premise still holds."
            )
            # …and the same question for HISTORY, because the exclusion also
            # applies to the nightly full-history scan. The doc files are
            # excluded by PATHSPEC inside `_git_log_pickaxe`, not by commit id.
            historical = _git_log_pickaxe(str(marker))
            assert not historical, (
                f"{marker!r} appears in the HISTORY of this repo outside the "
                f"commits that document the {detector} exclusion:\n  "
                + "\n  ".join(historical[:10])
                + f"\nThe nightly full-history scan runs with {detector} "
                  "excluded, so a credential committed and later deleted "
                  "alongside its marker can never be found. Re-enable the "
                  "detector, or record why the premise still holds (#1364)."
            )


# (`test_a_captured_scan_error_is_re_raised` lived here and is gone with the
#  design it guarded. The scan steps no longer capture their exit code — they
#  fail in place, which skips convert, which skips both uploads. That is what
#  this file already documented as intentional, and it is why a broken scan
#  cannot publish a clean `results=0` SARIF over the alert history.)


def test_the_converter_fails_as_a_PROCESS_not_just_as_a_function() -> None:
    """⛔ `sys.exit(main())` is the only thing that turns the policy into an exit code.

    An earlier revision of this module deleted a subprocess-based check of the
    converter after two independent blind reviewers called it a weaker
    duplicate of
    `tests/lint/test_trufflehog_to_sarif.py::TestMainExitPolicy`. Both were
    HALF right, and the deletion was a real loss of coverage — a third
    reviewer caught it:

      * that suite asserts the RETURN VALUE of `tts.main()` in-process, by
        value, and is indeed the stronger test of the POLICY;
      * it contains zero subprocesses, and grep confirms nothing else in the
        repo runs the converter as a program.

    So `sys.exit(main())` → `main()` makes the CLI exit 0 on a confirmed-live
    credential with the entire test suite still green — while the workflow
    invokes it exactly as a program. Two reviewers agreeing is strong evidence
    that a claim is true, not proof: they checked that the other test asserted
    the same POLICY, and so did I; none of us checked that it exercised the
    same ENTRY POINT.

    Asserted statically (no subprocess) so it stays cheap and cannot be
    confused for a second copy of the policy test.
    """
    src = CONVERTER.read_text(encoding="utf-8")
    assert re.search(r'if\s+__name__\s*==\s*["\']__main__["\']\s*:\s*\n\s*sys\.exit\(\s*main\(\)\s*\)',
                     src), (
        f"{CONVERTER.relative_to(ROOT).as_posix()} no longer ends in "
        "`sys.exit(main())`.\nWithout it the process exits 0 no matter what "
        "`main()` returns, so a VERIFIED finding produces a GREEN workflow "
        "step — and every existing test still passes, because they all call "
        "`main()` in-process and inspect its return value (#1364)."
    )
