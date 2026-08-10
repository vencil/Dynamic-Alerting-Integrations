"""Policy guard for .github/workflows/nightly-race.yaml (duplicate-issue rework).

What went wrong
===============
The workflow called `github.rest.issues.create()` unconditionally, from INSIDE a
2-leg `fail-fast: false` matrix job, with a date-stamped title. One persistent
failure therefore minted one duplicate P2 issue per night — #1213 / #1230 /
#1258 / #1263, four issues for one bug — and the body never named the failing
test, which is precisely why nobody noticed they were duplicates.

Why the guard lives HERE, in Python
-----------------------------------
Same reason as tests/ops/test_nightly_scan_matrix_drift.py: this workflow is
`schedule` + `workflow_dispatch` only, so it NEVER runs in its own PR's CI. The
only thing that can catch a regression before it ships a month of silence is an
offline test in a job that actually runs. `tests/ops/*.sh` would not do — pytest
does not collect shell scripts. This file is collected by the existing
"Python Tests (3.13)" required check, whose path filter includes
`.github/workflows/**` (see ci.yml's `python:` filter), so editing the workflow
runs this guard. ⚠️ Anchored by NAME, not by line number: the two citations in
this docstring were both stale before anyone noticed, because a line number is
invalidated by every edit above it and nothing checks it.

The workflow is parsed with `yaml.safe_load`, not grepped: a grep-level guard
cannot tell "issues.create inside the matrix job" from "issues.create in the
fan-in report job", which is the entire distinction being enforced.

NOTE on assertion style: ci.yml runs pytest with `-x` (exit-first, in the
`Run pytest with coverage` step of `python-tests-run`),
so each test collects ALL of its violations and reports them in ONE failure
message. Successive bare asserts would surface one problem per CI round-trip.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "nightly-race.yaml"
REPORT_PY = ROOT / "scripts" / "ops" / "file_race_report.py"
PARSER_PY = ROOT / "scripts" / "ops" / "parse_go_test_json.py"

sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import file_race_report  # noqa: E402
from file_race_report import (  # noqa: E402
    ISSUE_BODY_MAX,
    ISSUE_TITLE_MAX,
    LABEL,
    MAX_RENDERED_TESTS,
    LABEL_DESC,
    LABEL_DESC_MAX,
    TITLE_PREFIX_TMPL,
    compose_body,
    parse_state,
    render_state,
)

# Widest suffix appended to the stable title prefix at runtime, e.g.
# " — repeat-run failure in 12 test(s)" / " — NO RESULT — leg produced no fragment".
TITLE_SUFFIX_BUDGET = 64

# Anything that WRITES an issue. Both spellings: github-script and the gh CLI.
FILING_RE = re.compile(
    r"issues\.create|issues\.update|issues\.createComment"
    r"|gh\s+issue\s+(?:create|edit|comment)"
    r"|\bissue\W+create\b|file_race_report",
    re.I,
)


def _runner(script):
    """Build a fake `gh` runner from a callable(args) -> (returncode, stdout).

    This replaces the old environment seams. Those short-circuited BEFORE the
    subprocess call, so the returncode handling they claimed to cover was never
    executed — swallowing `ensure_label` entirely still passed 21/21. Injecting
    at the runner drives the REAL branches.
    """
    def run(args: list[str]) -> subprocess.CompletedProcess:
        rc, out = script(args)
        return subprocess.CompletedProcess(args, rc, out, "")
    return run


def _gh_ok(args: list[str]) -> tuple[int, str]:
    """Healthy gh: label fine, no existing issue, writes succeed."""
    if args[:2] == ["issue", "list"]:
        return 0, "[]"
    return 0, ""


def _run_main(tmp_path: Path, frag: dict | None, script, monkeypatch,
              modules: str | None = None) -> int:
    frags = tmp_path / "frags"
    frags.mkdir(parents=True, exist_ok=True)
    if frag is not None:
        (frags / f"frag-{frag['module']}.json").write_text(json.dumps(frag), encoding="utf-8")
    monkeypatch.setenv("DO_FILE", "true")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return file_race_report.main(
        ["--frags-dir", str(frags),
         "--expected-modules", modules or (frag["module"] if frag else "ghost"),
         "--run-url", "https://gh/runs/999"],
        runner=_runner(script),
    )


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _workflow()["jobs"]


def _step_text(step: dict) -> str:
    """Every place a step can carry executable text."""
    parts = [str(step.get("run") or ""), str(step.get("uses") or "")]
    with_block = step.get("with") or {}
    if isinstance(with_block, dict):
        parts += [str(v) for v in with_block.values()]
    return "\n".join(parts)


def _run_report(tmp_path: Path, frag: dict | None, env_extra: dict) -> subprocess.CompletedProcess:
    """Drive the REAL report CLI offline through its DRY_RUN seam."""
    frags = tmp_path / "frags"
    frags.mkdir(parents=True, exist_ok=True)
    if frag is not None:
        (frags / f"frag-{frag['module']}.json").write_text(
            json.dumps(frag), encoding="utf-8"
        )
    env = {
        **os.environ,
        "DO_FILE": "true", "DRY_RUN": "1", "REPO": "o/r",
        # The child prints em-dashes and 🚨. Without this the Windows dev host
        # decodes its stdout as cp950 and the TEST dies on the child's output
        # rather than on anything under test.
        "PYTHONIOENCODING": "utf-8",
        **env_extra,
    }
    # Seams must be ABSENT unless a case asks for them: inheriting a stale
    # DRY_RUN_ISSUE_NUM from the environment would silently turn a "create"
    # case into an "edit" case and the assertion would pass for the wrong reason.
    for seam in ("DRY_RUN_ISSUE_NUM", "DRY_RUN_PREV_BODY"):
        if seam not in env_extra:
            env.pop(seam, None)
    return subprocess.run(
        [sys.executable, str(REPORT_PY), "--frags-dir", str(frags),
         "--expected-modules", frag["module"] if frag else "ghost",
         "--run-url", "https://gh/runs/999"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(ROOT), env=env,
    )


def _frag(**over) -> dict:
    base = {
        "schema": 1, "module": "tenant-api", "failed": True,
        "failed_tests": ["TestConfigManager_HotReload"], "race_tests": [],
        "had_data_race": False, "build_failed": False, "unattributed_failure": False,
        "go_exit_code": 1, "fail_events": 10, "non_json_lines": 0, "total_events": 300,
        "raw_missing": False, "run_url": "https://gh/runs/999",
        "workdir": "components/tenant-api", "packages": "./cmd/...",
    }
    base.update(over)
    return base


# ── (a) the matrix must not write issues ─────────────────────────────────────


def test_no_matrix_job_files_an_issue() -> None:
    """The write race is closed by TOPOLOGY: no matrix leg may file anything.

    A `concurrency:` block is run-scoped and does NOT serialise legs within one
    run, so a naive list-then-create inside the matrix would still let both legs
    find "none" and both create. Only a single fan-in writer removes that.
    """
    problems = []
    for name, job in _jobs().items():
        if not (job.get("strategy") or {}).get("matrix"):
            continue
        for step in job.get("steps") or []:
            text = _step_text(step)
            hit = FILING_RE.search(text)
            if hit:
                problems.append(
                    f"job {name!r} has a matrix AND step "
                    f"{step.get('name') or step.get('uses')!r} matching {hit.group(0)!r}"
                )
        perms = job.get("permissions") or {}
        if isinstance(perms, dict) and perms.get("issues") == "write":
            problems.append(f"job {name!r} has a matrix AND holds issues: write")
    assert not problems, (
        "issue filing moved back inside a matrix job — one duplicate issue per leg "
        "per night is exactly the #1213/#1230/#1258/#1263 regression:\n  "
        + "\n  ".join(problems)
    )


def test_exactly_one_job_may_write_issues() -> None:
    writers = [
        n for n, j in _jobs().items()
        if isinstance(j.get("permissions"), dict) and j["permissions"].get("issues") == "write"
    ]
    assert writers == ["report"], (
        f"expected exactly one issue-writing job ('report'), found {writers}. "
        "Least privilege is also the dedup invariant here: a second writer can race."
    )


def test_permissions_are_declared_per_job_not_workflow_wide() -> None:
    wf = _workflow()
    problems = []
    if wf.get("permissions") not in ({}, None):
        problems.append(
            f"workflow-level permissions should be deny-all ({{}}), got {wf.get('permissions')!r}"
        )
    for name, job in _jobs().items():
        perms = job.get("permissions")
        if not isinstance(perms, dict):
            problems.append(f"job {name!r} declares no job-level permissions")
    race_perms = _jobs()["race"].get("permissions") or {}
    if race_perms != {"contents": "read"}:
        problems.append(
            f"the race job must be contents:read only, got {race_perms!r} — it runs "
            "test code under -race and must not hold a writable token"
        )
    assert not problems, "least-privilege drift:\n  " + "\n  ".join(problems)


# ── (b) dedup: an existing issue must be refreshed, never duplicated ───────────────────────────────────


def test_existing_issue_is_refreshed_not_duplicated(tmp_path: Path) -> None:
    """Behavioural: with an issue already open, the run must EDIT, never create."""
    proc = _run_report(tmp_path, _frag(), {"DRY_RUN_ISSUE_NUM": "1213"})
    out = proc.stdout
    problems = []
    if "issue create" in out:
        problems.append("emitted `gh issue create` although issue #1213 already exists")
    if "issue edit 1213" not in out:
        problems.append("did not edit the existing issue #1213")
    if "issue comment" in out:
        problems.append(
            "emitted a comment — comments notify, and a nightly comment on standing "
            "debt is muted within a week (that is Tier 2, deliberately out of scope)"
        )
    if "issue close" in out:
        problems.append("emitted a close — auto-close is explicitly out of scope")
    assert not problems, (
        f"dedup/notification policy broken (rc={proc.returncode}):\n  "
        + "\n  ".join(problems) + f"\n--- stdout ---\n{out}\n--- stderr ---\n{proc.stderr}"
    )


def test_a_green_night_neither_comments_nor_closes(tmp_path: Path) -> None:
    proc = _run_report(
        tmp_path,
        _frag(failed=False, failed_tests=[], go_exit_code=0, unattributed_failure=False),
        {"DRY_RUN_ISSUE_NUM": "1213"},
    )
    problems = [w for w in ("issue close", "issue comment") if w in proc.stdout]
    assert not problems, (
        "a green night must be recorded as `last green:` in the body, not by "
        f"closing or commenting (found {problems}). One green night does not prove "
        "a repeat-run failure fixed, and close/reopen churn notifies twice per flap."
    )
    assert "last_green=" in proc.stdout, "the green night was not recorded in the state block"


# ── (c) the body must name the failing tests ─────────────────────────────────


def _state(**over) -> dict:
    base = {
        "module": "tenant-api", "first_seen": "2026-07-24", "first_run": "https://gh/runs/1",
        "last_run": "https://gh/runs/999", "last_green": "", "streak": 3,
        "had_data_race": 0, "build_failed": 0, "unattributed": 0, "tests": [],
        "_had_issue": True,
    }
    base.update(over)
    return base


def _strip_state_blocks(text: str) -> str:
    """Drop the `<!-- race-state: ... -->` markers, leaving what a reader sees."""
    return re.sub(r"<!--\s*race-state:.*?-->", "", text, flags=re.S)


def test_issue_content_carries_the_failing_test_names(tmp_path: Path) -> None:
    """The direct cause of four unnoticed duplicates: a body with no evidence."""
    frag = _frag(failed_tests=["TestConfigManager_HotReload", "TestWatchLoop/reload"])
    # Asserted against the RENDERED BODY, not the process output, and with the
    # state block stripped. Mutation testing caught two ways this went vacuous:
    # deleting the rendered failing-test list still passed because the names
    # survived (a) inside `tests=[...]`, a machine-readable comment no reader
    # sees, and (b) in the job step-summary, which is not the issue at all. The
    # guard was green on the exact defect it exists to catch.
    body = _strip_state_blocks(compose_body(
        frag=frag, state=_state(tests=frag["failed_tests"]), run_url="https://gh/runs/999",
        date="2026-07-28", baseline_readable=True, degraded="", green=False,
    ))
    problems = []
    for name in frag["failed_tests"]:
        if name not in body:
            problems.append(
                f"failing test {name!r} does not appear in the RENDERED issue body"
            )
    if "race-raw-tenant-api" not in body:
        problems.append("the raw -json artifact pointer is missing from the body")
    if "First seen" not in body:
        problems.append("the first-seen date is missing from the body")
    assert not problems, (
        "issue body lost its evidence — this is the direct reason four duplicate "
        "issues went unnoticed:\n  " + "\n  ".join(problems) + f"\n--- body ---\n{body}"
    )
    # And the wiring: the CLI must actually put that body on the wire.
    assert "TestConfigManager_HotReload" in _run_report(tmp_path, frag, {}).stdout


def test_title_says_data_race_only_when_a_race_was_detected(tmp_path: Path) -> None:
    problems = []
    no_race = _run_report(tmp_path / "a", _frag(), {}).stdout
    if "DATA RACE" in no_race:
        problems.append(
            "a non-race failure was titled DATA RACE — `grep -c 'DATA RACE'` was 0 on "
            "all four nights of the real failure, and mislabeling it is the #932 defect"
        )
    if "repeat-run" not in no_race:
        problems.append("a non-race failure did not use repeat-run/test-isolation wording")
    with_race = _run_report(
        tmp_path / "b", _frag(had_data_race=True, race_tests=["TestX"]), {}
    ).stdout
    if "DATA RACE" not in with_race:
        problems.append("a REAL data race was not titled DATA RACE")
    assert not problems, "verdict wording drifted from the evidence:\n  " + "\n  ".join(problems)


def test_first_seen_run_url_survives_a_refresh() -> None:
    """Round-trip: the nightly must not overwrite when the failure started."""
    night1 = {
        "module": "tenant-api", "first_seen": "2026-07-24",
        "first_run": "https://gh/runs/FIRST", "last_run": "https://gh/runs/FIRST",
        "last_green": "", "streak": 1, "had_data_race": 0, "build_failed": 0,
        "unattributed": 0, "tests": ["TestA"],
    }
    restored = parse_state(f"blah\n{render_state(night1)}\nblah")
    assert restored is not None, "the state block did not survive its own round-trip"
    problems = []
    if restored["first_run"] != "https://gh/runs/FIRST":
        problems.append(f"first_run lost: {restored['first_run']!r}")
    if restored["first_seen"] != "2026-07-24":
        problems.append(f"first_seen lost: {restored['first_seen']!r}")
    if restored["tests"] != ["TestA"]:
        problems.append(f"tests lost: {restored['tests']!r}")
    if restored["streak"] != 1:
        problems.append(f"streak lost: {restored['streak']!r}")
    assert not problems, "state round-trip is lossy:\n  " + "\n  ".join(problems)


def test_unreadable_state_block_is_not_treated_as_clean() -> None:
    """No baseline must read as 'unknown, possibly older', never as 'new today'."""
    assert parse_state("a body with no marker at all") is None
    body = compose_body(
        frag=_frag(), state={
            "module": "tenant-api", "first_seen": "2026-07-28", "first_run": "u",
            "last_run": "u", "last_green": "", "streak": 1, "had_data_race": 0,
            "build_failed": 0, "unattributed": 0, "tests": [], "_had_issue": True,
        },
        run_url="u", date="2026-07-28", baseline_readable=False, degraded="", green=False,
    )
    assert "re-stamped" in body, (
        "an unreadable baseline silently reset first-seen. That erases the only record "
        "of how long this has been failing — the evidence four duplicate issues lost."
    )


def test_missing_fragment_reads_as_worse_not_clean(tmp_path: Path) -> None:
    """A leg that never uploaded has proven nothing."""
    proc = _run_report(tmp_path, None, {})
    problems = []
    if proc.returncode == 0:
        problems.append("exit code was 0 — a degraded report must red the run")
    if "issue create" not in proc.stdout:
        problems.append("no alert was filed for the module with no fragment")
    if "::error" not in proc.stdout:
        problems.append("no ::error annotation for the missing fragment")
    assert not problems, (
        "a missing fragment was treated as a clean module:\n  " + "\n  ".join(problems)
        + f"\n--- stdout ---\n{proc.stdout}"
    )


# ── (d) delivery + label failures must be LOUD ───────────────────────────────
#
# These replace three text-matching guards that mutation testing proved could
# not fail: the `|| true` scanner substring-matched "label create", a literal
# that appears exactly ONCE across all guarded files — inside a docstring — and
# never in the real call site (a `["label", "create", ...]` argv list). Every
# test below drives the actual code path with an injected failing `gh`.


def _first_issue_write(calls: list[list[str]]) -> list[str] | None:
    for c in calls:
        if c[:2] in (["issue", "create"], ["issue", "edit"]):
            return c
    return None


def test_issue_create_failure_reds_the_run(tmp_path: Path, capsys, monkeypatch) -> None:
    """A 422 on create must NOT print success and exit 0.

    This is the whole point of the workflow: a monitoring job that reports
    success while its own delivery failed is worse than the outage it watches.
    """
    def script(args):
        if args[:2] == ["issue", "create"]:
            return 1, ""
        return _gh_ok(args)

    rc = _run_main(tmp_path, _frag(), script, monkeypatch)
    out = capsys.readouterr().out
    problems = []
    if rc == 0:
        problems.append("exit code 0 despite the issue create failing")
    if "::error" not in out:
        problems.append("no ::error annotation for the failed delivery")
    if "Filed a new tracking issue." in out:
        problems.append("claimed 'Filed a new tracking issue.' when the create FAILED")
    assert not problems, (
        "a failed issue write was reported as success:\n  " + "\n  ".join(problems)
        + f"\n--- output ---\n{out}"
    )


def test_issue_edit_failure_reds_the_run(tmp_path: Path, capsys, monkeypatch) -> None:
    """Same for the refresh path (e.g. 410 on a locked/transferred issue)."""
    def script(args):
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps([{"number": 1213, "title": TITLE_PREFIX_TMPL.format(
                module="tenant-api") + " — repeat-run failure in 1 test(s)"}])
        if args[:2] == ["issue", "edit"]:
            return 1, ""
        return 0, ""

    rc = _run_main(tmp_path, _frag(), script, monkeypatch)
    out = capsys.readouterr().out
    problems = []
    if rc == 0:
        problems.append("exit code 0 despite the issue edit failing")
    if "Refreshed issue #1213 (silent)." in out:
        problems.append("claimed the refresh succeeded when the edit FAILED")
    assert not problems, (
        "a failed issue refresh was reported as success:\n  " + "\n  ".join(problems)
        + f"\n--- output ---\n{out}"
    )


def test_lookup_failure_is_not_mistaken_for_no_existing_issue(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A 502 on `issue list` must not fall through to 'nothing found' at exit 0.

    That fall-through silently re-mints the duplicate this whole change removes.
    Chosen behaviour: still FILE (a duplicate is visible and closable; a skipped
    first night is silence), but red the run and say so in the body.
    """
    def script(args):
        if args[:2] == ["issue", "list"]:
            return 1, ""
        return 0, ""

    rc = _run_main(tmp_path, _frag(), script, monkeypatch)
    out = capsys.readouterr().out
    problems = []
    if rc == 0:
        problems.append("exit code 0 despite the dedup lookup failing")
    if "::error" not in out:
        problems.append("no ::error annotation for the failed lookup")
    assert not problems, (
        "a failed lookup was treated as 'no existing issue':\n  " + "\n  ".join(problems)
        + f"\n--- output ---\n{out}"
    )


def test_label_outage_delivers_without_the_label_and_reds_the_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Label create fails AND the API probe says it is absent → degrade, don't lose."""
    calls: list[list[str]] = []

    def script(args):
        calls.append(args)
        if args[:2] == ["label", "create"]:
            return 1, ""
        if args[0] == "api":
            return 1, ""  # label genuinely absent
        if args[:2] == ["issue", "list"]:
            return 0, "[]"
        return 0, ""

    rc = _run_main(tmp_path, _frag(), script, monkeypatch)
    capsys.readouterr()
    write = _first_issue_write(calls)
    problems = []
    if write is None:
        problems.append("the alert was NOT filed when the label was unusable")
    elif LABEL in write:
        problems.append(
            f"applied the {LABEL!r} label that does not exist — gh would reject the create"
        )
    if not any(c[0] == "api" for c in calls):
        problems.append("never probed the API to disambiguate create-failed vs label-absent")
    if rc == 0:
        problems.append("exit code 0 — a degraded dedup key must red the run")
    assert not problems, "the label-outage path regressed:\n  " + "\n  ".join(problems)


def test_label_edit_rejected_but_label_exists_is_not_an_outage(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """`--force` also EDITS, so a non-zero exit is ambiguous — the probe resolves it.

    Discriminability partner to the test above: same failing `label create`, but
    the probe finds the label. Dedup is intact, so the run must stay GREEN and
    the label must still be applied.
    """
    calls: list[list[str]] = []

    def script(args):
        calls.append(args)
        if args[:2] == ["label", "create"]:
            return 1, ""
        if args[0] == "api":
            return 0, ""  # label exists; only the description edit was rejected
        if args[:2] == ["issue", "list"]:
            return 0, "[]"
        return 0, ""

    rc = _run_main(tmp_path, _frag(), script, monkeypatch)
    capsys.readouterr()
    write = _first_issue_write(calls)
    problems = []
    if rc != 0:
        problems.append(f"exit {rc} — an existing label is not an outage, dedup still works")
    if write is None or LABEL not in write:
        problems.append("did not apply the label even though the probe proved it exists")
    assert not problems, "the ambiguous-label-create path regressed:\n  " + "\n  ".join(problems)


def test_healthy_run_exits_zero(tmp_path: Path, capsys, monkeypatch) -> None:
    """Guard the guards above: they must be detecting failure, not always red."""
    rc = _run_main(tmp_path, _frag(), _gh_ok, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0, f"a fully healthy run must exit 0, got {rc}\n--- output ---\n{out}"


# ── GitHub API hard caps ─────────────────────────────────────────────────────


def test_body_fits_the_github_cap_at_whole_package_failure_scale() -> None:
    """One broken shared fixture reds the WHOLE package — the realistic worst case.

    `components/tenant-api` has 933 top-level `func Test*` (measured), average
    name 34 chars. Each costs ~40 chars in the rendered list plus ~35 in the
    state block, so an unclamped body lands near 70k against GitHub's 65536 cap
    — a 422 on precisely the night the issue matters most.
    """
    names = [f"TestSomeReasonablyLongHandlerName_Subcase{i:04d}" for i in range(933)]
    body = compose_body(
        frag=_frag(failed_tests=names, fail_events=9330),
        state=_state(tests=names), run_url="https://gh/runs/999", date="2026-07-28",
        baseline_readable=True, degraded="", green=False,
    )
    problems = []
    if len(body) > ISSUE_BODY_MAX:
        problems.append(f"body is {len(body)} chars, over GitHub's {ISSUE_BODY_MAX} cap")
    # The state block must survive intact — a half-cut `<!-- race-state:` is
    # unparseable, and an unparseable baseline re-stamps first-seen and loses
    # the record of when the failure started.
    restored = parse_state(body)
    if restored is None:
        problems.append("the state block is no longer parseable after clamping")
    elif restored.get("tests_truncated") != 1:
        problems.append(
            "the state block dropped names without recording tests_truncated=1 — "
            "the next night would read the missing ones as NEW failures"
        )
    if "and 733 more" not in body:
        problems.append("the rendered list was capped without telling the reader how many")
    assert not problems, (
        f"body-size handling is wrong at {len(names)}-test scale (len={len(body)}):\n  "
        + "\n  ".join(problems)
    )


def test_pathological_body_is_truncated_with_a_visible_marker() -> None:
    """Backstop: even absurd names must not produce a 422."""
    names = [f"Test{'X' * 400}{i}" for i in range(MAX_RENDERED_TESTS)]
    body = compose_body(
        frag=_frag(failed_tests=names), state=_state(tests=names),
        run_url="u", date="2026-07-28", baseline_readable=True, degraded="", green=False,
    )
    problems = []
    if len(body) > ISSUE_BODY_MAX:
        problems.append(f"body is {len(body)} chars, over the {ISSUE_BODY_MAX} cap")
    if "truncated" not in body.lower():
        problems.append("truncation happened silently, with no marker for the reader")
    if parse_state(body) is None:
        problems.append("the state block was destroyed by truncation")
    assert not problems, "pathological-body clamp failed:\n  " + "\n  ".join(problems)


def test_normal_body_is_not_truncated() -> None:
    """Discriminability: the clamp must not fire on an ordinary failure."""
    body = compose_body(
        frag=_frag(), state=_state(tests=["TestA"]), run_url="u", date="2026-07-28",
        baseline_readable=True, degraded="", green=False,
    )
    assert "Body truncated" not in body, "the clamp fired on a single-test body"
    assert parse_state(body) is not None


def test_label_description_fits_the_github_api_cap() -> None:
    problems = []
    if LABEL_DESC_MAX != 100:
        problems.append(
            f"LABEL_DESC_MAX is {LABEL_DESC_MAX}; GitHub's documented cap is 100 and "
            "raising the constant does not raise the API's limit, it only hides the 422"
        )
    if len(LABEL_DESC) > 100:
        problems.append(f"LABEL_DESC is {len(LABEL_DESC)} chars: {LABEL_DESC!r}")
    assert not problems, "label description would 422:\n  " + "\n  ".join(problems)


def test_issue_titles_fit_the_github_api_cap() -> None:
    problems = []
    if ISSUE_TITLE_MAX != 256:
        problems.append(f"ISSUE_TITLE_MAX is {ISSUE_TITLE_MAX}, GitHub's cap is 256")
    for module in _matrix_modules():
        budgeted = len(TITLE_PREFIX_TMPL.format(module=module)) + TITLE_SUFFIX_BUDGET
        if budgeted > 256:
            problems.append(f"{module}: prefix + suffix budget = {budgeted} > 256")
    assert not problems, "issue title would 422:\n  " + "\n  ".join(problems)


# ── matrix <-> report drift ──────────────────────────────────────────────────


def _matrix_modules() -> list[str]:
    return [e["module"] for e in _jobs()["race"]["strategy"]["matrix"]["include"]]


def _report_expected_modules() -> list[str]:
    run = "\n".join(
        str(s.get("run") or "") for s in _jobs()["report"]["steps"]
    )
    m = re.search(r'--expected-modules\s+"([^"]+)"', run)
    assert m is not None, (
        "could not find --expected-modules in the report job — the drift check below "
        "would be vacuous."
    )
    return [x.strip() for x in m.group(1).split(",") if x.strip()]


def test_report_expected_modules_track_the_race_matrix() -> None:
    """A module added to the matrix but not to --expected-modules never reports.

    Same class as the third-party scan-matrix drift guard: the report cannot
    notice a fragment it was never told to expect, so the new module would fail
    silently forever.
    """
    matrix, expected = sorted(_matrix_modules()), sorted(_report_expected_modules())
    assert matrix == expected, (
        "the report job's --expected-modules drifted from the race matrix.\n"
        f"  only in matrix : {sorted(set(matrix) - set(expected))}\n"
        f"  only in report : {sorted(set(expected) - set(matrix))}\n"
        "A module missing from --expected-modules is never reported on at all."
    )


def test_report_job_fans_in_from_the_matrix() -> None:
    report = _jobs()["report"]
    problems = []
    if report.get("needs") != ["race"]:
        problems.append(f"needs is {report.get('needs')!r}, expected ['race']")
    if "always()" not in str(report.get("if") or ""):
        problems.append("report `if` lacks always() — a red matrix leg would skip reporting")
    if (report.get("strategy") or {}).get("matrix"):
        problems.append("the report job grew a matrix — that reintroduces the write race")
    assert not problems, "fan-in topology broken:\n  " + "\n  ".join(problems)


def test_test_step_uses_json_and_preserves_the_exit_code() -> None:
    """`-json` + pipefail is what makes structured extraction possible at all."""
    steps = _jobs()["race"]["steps"]
    run = next(
        (str(s.get("run") or "") for s in steps if "go test" in str(s.get("run") or "")), ""
    )
    problems = []
    if "-json" not in run:
        problems.append("go test is no longer run with -json — the parser gets nothing")
    if "-race" not in run or "-count=10" not in run:
        problems.append("go test lost -race/-count=10")
    if "pipefail" not in run:
        problems.append(
            "no `set -o pipefail` — piping through tee would mask go test's exit code "
            "and a failing night would look green"
        )
    if "tee" not in run:
        problems.append("the raw stream is no longer tee'd to a file for the artifact")
    assert not problems, "the evidence-capture step regressed:\n  " + "\n  ".join(problems)


def test_raw_stream_is_uploaded_for_human_triage() -> None:
    uploads = [
        s for s in _jobs()["race"]["steps"]
        if "upload-artifact" in str(s.get("uses") or "")
    ]
    names = [str((s.get("with") or {}).get("name") or "") for s in uploads]
    assert any("race-raw" in n for n in names), (
        f"the raw `go test -json` stream is no longer uploaded (artifacts: {names}). "
        "The parsed fragment is lossy by design; without the raw stream a human has "
        "no race report, panic, or build error to read."
    )
