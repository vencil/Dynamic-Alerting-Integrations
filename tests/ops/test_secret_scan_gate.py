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

⚠️ HONEST SCOPE. These are STATIC assertions over the workflow text plus one
behavioural pin on the converter. They cannot prove trufflehog's runtime
semantics — that was established out-of-band against the pinned CLI (v3.95.3)
in a synthetic repo shaped like an `actions/checkout` workspace (detached
HEAD, `refs/remotes/origin/*` for three branches):

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

⚠️ What is NOT established: WHY every Lob hit comes back verified. The
detector marks a hit live on any 2xx from its verification endpoint, and 242
of 242 came back verified while every other detector returned 0 — that is
enough to say the signal carries no information, and it is deliberately not
written up as "the endpoint returns 200 for anything", which was not measured.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
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
        "markers": ("lob.com",),
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

# Files that legitimately contain a marker because they are PROSE ABOUT the
# exclusion — never an integration with the provider. Listed rather than
# pattern-matched, and asserted to exist, so a rename cannot quietly widen the
# carve-out. ⚠️ The carve-out is per-FILE and applies to every marker of every
# detector, so keep it to files that can only ever hold prose; anything that is
# also a config or code path does not belong here.
# (CHANGELOG.md earns its place the hard way: this guard reddened on the very
# entry describing the fix, because that entry names the verification endpoint.)
_MARKER_DOC_FILES = (
    ".github/workflows/secret-scan.yml",
    "tests/ops/test_secret_scan_gate.py",
    "CHANGELOG.md",
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _run_blocks() -> list[tuple[str, str]]:
    """Every `run:` script in the workflow, as (step name, script)."""
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if isinstance(step.get("run"), str):
                out.append((step.get("name") or "<unnamed>", step["run"]))
    assert out, f"no `run:` steps found in {WORKFLOW.name} — parser broke, not the repo"
    return out


def _trufflehog_invocations() -> list[tuple[str, str, str]]:
    """(step name, whole invocation on one logical line, full step script).

    Backslash continuations are folded first: the real command is spread over
    five lines, and a per-line scan would see `--branch` and `--since-commit`
    as unrelated fragments.
    """
    found: list[tuple[str, str, str]] = []
    for name, script in _run_blocks():
        folded = re.sub(r"\\\n\s*", " ", script)
        for line in folded.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment quoting the command is not an invocation
            if re.search(r"\btrufflehog\s+git\b", stripped):
                found.append((name, stripped, script))
    return found


def _env_block() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")).get("env") or {}


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

    diff_scans = [(n, cmd, src) for n, cmd, src in invocations if "--since-commit" in cmd]
    assert len(diff_scans) == 1, (
        f"expected exactly one --since-commit (diff-mode) invocation, got "
        f"{[n for n, _, _ in diff_scans]}"
    )
    name, cmd, script = diff_scans[0]

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
        "steps reference it, so an undefined value silently excludes nothing "
        "(measured: trufflehog accepts an empty --exclude-detectors and applies "
        "no exclusion)."
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


def test_verified_findings_still_block(tmp_path: Path) -> None:
    """The noise was not fixed by making the gate fail-open.

    Behavioural, against the real converter: a VERIFIED finding must still exit
    non-zero (merge blocked) and an unverified one must still exit zero. Both
    directions, because a converter that blocked on everything would satisfy
    the first half while making the gate useless in the other direction.
    """
    tmp = tmp_path / "findings.ndjson"
    out = tmp_path / "results.sarif"

    def _convert(verified: bool) -> subprocess.CompletedProcess:
        finding = {
            "DetectorName": "Github",
            "Verified": verified,
            "SourceMetadata": {"Data": {"Git": {"file": "a.py", "line": 1}}},
        }
        tmp.write_text(json.dumps(finding) + "\n", encoding="utf-8", newline="\n")
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(CONVERTER),
             "--input", str(tmp), "--output", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )

    blocked = _convert(True)
    allowed = _convert(False)

    assert blocked.returncode != 0, (
        "a VERIFIED finding no longer fails the converter — L2 has become "
        f"advisory:\n{blocked.stdout}\n{blocked.stderr}"
    )
    assert allowed.returncode == 0, (
        "an UNVERIFIED finding now fails the converter — every PR would be "
        f"blocked on regex noise:\n{allowed.stdout}\n{allowed.stderr}"
    )
