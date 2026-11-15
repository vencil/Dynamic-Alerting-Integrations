"""Policy guard for .github/workflows/nightly-race.yaml (duplicate-issue rework).

What went wrong
===============
The workflow called `github.rest.issues.create()` unconditionally, from INSIDE a
2-leg `fail-fast: false` matrix job, with a date-stamped title. One persistent
failure therefore minted one duplicate P2 issue per night — #1213 / #1230 /
#1258 / #1263, four issues for one bug — and the body never named the failing
test, which is precisely why nobody noticed they were duplicates.

The fix then went one step too far the other way: ONE issue per module merged
unrelated failures (#2977 carried a package timeout, a one-night assertion bug
and another package's timeouts under one first-seen date), and every non-race
failure got isolation vocabulary (#3343 failed in all ten repetitions). Identity
is now a failure fingerprint, one issue each — section (b) below.

Why the guard lives HERE, in Python
-----------------------------------
Same reason as tests/ops/test_nightly_scan_matrix_drift.py: this workflow is
`schedule` + `workflow_dispatch` only, so it NEVER runs in its own PR's CI. The
only thing that can catch a regression before it ships a month of silence is an
offline test in a job that actually runs. `tests/ops/*.sh` would not do — pytest
does not collect shell scripts. This file is collected by the existing
"Python Tests" required check, which runs on every PR, so editing the workflow
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
    LABEL_DESC,
    LABEL_DESC_MAX,
    MAX_RENDERED_TESTS,
    MAX_TEST_INCIDENTS,
    TITLE_PREFIX_TMPL,
    compose_body,
    derive_incidents,
    fingerprint_marker,
    parse_state,
    render_state,
)

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


# ── (b) identity: one issue per failure fingerprint ──────────────────────────
#
# #2977 mixed three unrelated incidents (a cmd/da-guard timeout, a one-night
# assertion bug, repeated pkg/parsetree timeouts) because identity was the
# module. These cases pin the incident rules and the position-0 marker.


def _kinds(frag: dict) -> list[tuple[str, str]]:
    return [(i["kind"], i["subject"]) for i in derive_incidents(frag)]


def test_each_failure_shape_derives_its_own_incident() -> None:
    cases = {
        "build": (_frag(failed_tests=["TestA"], build_failed=True, had_data_race=True,
                        race_tests=["TestA"]), [("build", "tenant-api")]),
        "timeout": (_frag(failed_tests=[], timed_out=True, timeout_packages=[
            {"package": "ex/pkg/parsetree", "after": "30m0s", "running_tests": ["TestP"]},
            {"package": "ex/cmd/da-guard", "after": "30m0s", "running_tests": []}],
            timeout_running_tests=["TestP"]),
            [("timeout", "ex/pkg/parsetree"), ("timeout", "ex/cmd/da-guard")]),
        "data race": (_frag(failed_tests=["TestR", "TestR/sub", "TestOther"],
                            had_data_race=True, race_tests=["TestR/sub"]),
                      [("data-race", "tenant-api"), ("test", "TestOther")]),
        "tests, subtests grouped": (_frag(failed_tests=["TestA", "TestA/x", "TestB"]),
                                    [("test", "TestA"), ("test", "TestB")]),
        "unattributed": (_frag(failed_tests=[], unattributed_failure=True),
                         [("unattributed", "tenant-api")]),
        "green": (_frag(failed=False, failed_tests=[], go_exit_code=0), []),
        "missing fragment": (file_race_report.load_fragment(Path("/nonexistent"),
                                                           "tenant-api")[0],
                             [("no-result", "tenant-api")]),
    }
    problems = []
    for name, (frag, want) in cases.items():
        got = _kinds(frag)
        if got != want:
            problems.append(f"{name}: got {got}, expected {want}")
    assert not problems, "incident derivation drifted:\n  " + "\n  ".join(problems)


def test_tests_running_at_the_timeout_are_not_test_incidents() -> None:
    """Running at the deadline is not failing; a test that ALSO failed is."""
    tp = {"package": "p", "after": "30m0s", "running_tests": ["TestSlow/case"]}
    merely = _frag(failed_tests=["TestSlow", "TestSlow/case"], timed_out=True,
                   timeout_packages=[tp], timeout_running_tests=["TestSlow/case"])
    also = _frag(failed_tests=["TestSlow", "TestSlow/case", "TestSlow/other"],
                 timed_out=True, timeout_packages=[tp],
                 timeout_running_tests=["TestSlow/case"])
    assert _kinds(merely) == [("timeout", "p")]
    assert _kinds(also) == [("timeout", "p"), ("test", "TestSlow")]


def test_more_than_five_failing_tests_collapse_into_one_issue() -> None:
    five = _frag(failed_tests=[f"Test{i}" for i in range(MAX_TEST_INCIDENTS)])
    six = _frag(failed_tests=[f"Test{i}" for i in range(MAX_TEST_INCIDENTS + 1)])
    problems = []
    if [k for k, _ in _kinds(five)] != ["test"] * MAX_TEST_INCIDENTS:
        problems.append(f"{MAX_TEST_INCIDENTS} failing tests did not get one issue each: {_kinds(five)}")
    if _kinds(six) != [("tests", "tenant-api")]:
        problems.append(f"{MAX_TEST_INCIDENTS + 1} failing tests did not collapse: {_kinds(six)}")
    elif len(derive_incidents(six)[0]["tests"]) != MAX_TEST_INCIDENTS + 1:
        problems.append("the collapsed issue does not list every failing test")
    assert MAX_TEST_INCIDENTS == 5
    assert not problems, "issue-flood guard broken:\n  " + "\n  ".join(problems)


def test_fingerprint_marker_is_the_first_line_and_a_later_copy_does_not_match() -> None:
    inc = derive_incidents(_frag())[0]
    fp = inc["fingerprint"]
    assert fp == "tenant-api|test|TestConfigManager_HotReload"
    body = _issue_body(inc, _frag())
    problems = []
    if not body.startswith(f"<!-- race-fingerprint: {fp} -->\n"):
        problems.append(f"the marker is not at position 0: {body[:80]!r}")
    if file_race_report.body_fingerprint(body) != fp:
        problems.append("the body does not read back as its own fingerprint")
    spoof = "Some prose quoting a log:\n" + fingerprint_marker(fp) + "\nmore"
    if file_race_report.body_matches(spoof, fp):
        problems.append("a marker quoted LATER in a body matched — that issue would "
                        "capture another fingerprint's refreshes")
    if not file_race_report.body_matches("<!-- race-fingerprint: " + fp + " -->\r\nx", fp):
        problems.append("a CRLF body (as GitHub may return it) no longer matches")
    if file_race_report.body_matches(fingerprint_marker(fp + "_v2") + "\n", fp):
        problems.append("a longer fingerprint sharing the prefix matched")
    assert not problems, "fingerprint identity broken:\n  " + "\n  ".join(problems)


def _row(num: int, frag: dict, kind_subject: tuple[str, str] | None = None,
         state: dict | None = None, title: str = "t") -> dict:
    """An open issue as `gh issue list --json number,title,body` returns it."""
    module = frag["module"]
    kind, subject = kind_subject or ("test", "TestConfigManager_HotReload")
    inc = {"module": module, "kind": kind, "subject": subject,
           "fingerprint": file_race_report.fingerprint(module, kind, subject),
           "tests": [subject]}
    body = compose_body(inc, frag, state or _state(), "https://gh/runs/1", "2026-10-01",
                        True, "")
    return {"number": num, "title": title, "body": body}


def _list_script(rows: list[dict], calls: list[list[str]], fail_edit: bool = False):
    def script(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(rows)
        if args[:2] == ["issue", "edit"] and fail_edit:
            return 1, ""
        return 0, ""
    return script


def _writes(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] in (["issue", "create"], ["issue", "edit"],
                                          ["issue", "comment"], ["issue", "close"])]


def _arg(call: list[str], flag: str) -> str:
    return call[call.index(flag) + 1]


def test_matching_fingerprint_is_refreshed_and_a_new_one_is_created(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    frag = _frag(failed_tests=["TestConfigManager_HotReload", "TestNewBug"],
                 test_runs={"TestConfigManager_HotReload": {"fail": 10, "pass": 0},
                            "TestNewBug": {"fail": 2, "pass": 8}})
    calls: list[list[str]] = []
    rows = [_row(3001, frag, state=_state(first_seen="2026-09-01", streak=4))]
    rc = _run_main(tmp_path, frag, _list_script(rows, calls), monkeypatch)
    capsys.readouterr()
    writes = _writes(calls)
    problems = []
    edits = [c for c in writes if c[:2] == ["issue", "edit"]]
    creates = [c for c in writes if c[:2] == ["issue", "create"]]
    if [c[2] for c in edits] != ["3001"]:
        problems.append(f"expected exactly one edit of #3001, got {[c[2] for c in edits]}")
    if len(creates) != 1 or "TestNewBug" not in _arg(creates[0], "--title"):
        problems.append(f"expected one create for TestNewBug, got {creates}")
    if any(c[:2] in (["issue", "comment"], ["issue", "close"]) for c in writes):
        problems.append("commented or closed — both are explicitly out of scope")
    if edits:
        state = parse_state(_arg(edits[0], "--body"))
        if not state or state["first_seen"] != "2026-09-01" or state["streak"] != 5:
            problems.append(f"the refreshed issue lost its own baseline: {state}")
    if creates:
        state = parse_state(_arg(creates[0], "--body"))
        if not state or state["first_seen"] == "2026-09-01":
            problems.append("the NEW fingerprint inherited another issue's first-seen (#2977)")
        if not _arg(creates[0], "--body").startswith(
                "<!-- race-fingerprint: tenant-api|test|TestNewBug -->"):
            problems.append("the created body does not start with its fingerprint marker")
    if rc != 0:
        problems.append(f"healthy run exited {rc}")
    assert not problems, "per-fingerprint dedup broken:\n  " + "\n  ".join(problems)


def test_a_fingerprint_not_seen_tonight_is_refreshed_silently_never_closed(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    frag = _frag(failed=False, failed_tests=[], go_exit_code=0)
    calls: list[list[str]] = []
    rows = [_row(3002, frag, ("timeout", "ex/pkg/parsetree"),
                 state=_state(streak=6, tests=["TestP"]))]
    rc = _run_main(tmp_path, frag, _list_script(rows, calls), monkeypatch)
    out = capsys.readouterr().out
    writes = _writes(calls)
    problems = []
    if [c[:3] for c in writes] != [["issue", "edit", "3002"]]:
        problems.append(f"expected one silent edit of #3002, got {writes}")
    else:
        body = _arg(writes[0], "--body")
        title = _arg(writes[0], "--title")
        state = parse_state(body)
        if "not seen on" not in title or "not seen on" not in body.lower():
            problems.append(f"the not-seen night is not recorded: {title!r}")
        if not state or state["streak"] != 0 or not state["last_green"]:
            problems.append(f"streak/last_green not reset: {state}")
        if state and state["first_seen"] != "2026-07-24":
            problems.append("first_seen was lost on a not-seen refresh")
        if not body.startswith("<!-- race-fingerprint: tenant-api|timeout|ex/pkg/parsetree -->"):
            problems.append("the not-seen refresh dropped the fingerprint marker")
    if rc != 0:
        problems.append(f"exit {rc}")
    assert not problems, ("not-seen refresh broken:\n  " + "\n  ".join(problems)
                          + f"\n--- output ---\n{out}")


def test_an_inconclusive_night_does_not_tell_other_issues_not_seen(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A build failure ran no test: 'not seen' would be the silent-clean direction."""
    frag = _frag(failed_tests=[], build_failed=True)
    calls: list[list[str]] = []
    rows = [_row(3003, frag)]
    _run_main(tmp_path, frag, _list_script(rows, calls), monkeypatch)
    out = capsys.readouterr().out
    edits = [c for c in _writes(calls) if c[:2] == ["issue", "edit"]]
    assert not edits, f"an unmeasured fingerprint issue was edited: {edits}"
    assert "#3003" in out and "Not re-evaluated" in out


def test_legacy_issue_without_a_marker_is_left_untouched_and_named(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    frag = _frag(module="threshold-exporter")
    calls: list[list[str]] = []
    legacy = {"number": 2977,
              "title": TITLE_PREFIX_TMPL.format(module="threshold-exporter")
              + " — package timeout in 1 package(s)",
              "body": "Nightly tracking issue …\n<!-- race-state: module=threshold-exporter "
                      "first_seen=2026-10-01 streak=9 tests=[] -->"}
    other_module = {"number": 3343,
                    "title": TITLE_PREFIX_TMPL.format(module="tenant-api") + " — x",
                    "body": "no marker"}
    rc = _run_main(tmp_path, frag, _list_script([legacy, other_module], calls), monkeypatch)
    out = capsys.readouterr().out
    writes = _writes(calls)
    problems = []
    if any(c[:3] == ["issue", "edit", "2977"] for c in writes):
        problems.append("the legacy issue #2977 was edited")
    if not any(c[:2] == ["issue", "create"] for c in writes):
        problems.append("tonight's incident was not filed as a fingerprint issue")
    if "#2977" not in out or "Legacy" not in out:
        problems.append("the step summary does not name #2977 as legacy")
    if "#3343" in out:
        problems.append("another module's legacy issue was attributed to this module")
    if rc != 0:
        problems.append(f"exit {rc}")
    assert not problems, "legacy handling broken:\n  " + "\n  ".join(problems) + f"\n{out}"


def test_existing_issue_is_refreshed_not_duplicated(tmp_path: Path) -> None:
    """Behavioural, through the real CLI and its DRY_RUN seams."""
    frag = _frag()
    prev = tmp_path / "prev.md"
    prev.write_text(_row(1213, frag)["body"], encoding="utf-8")
    proc = _run_report(tmp_path, frag, {"DRY_RUN_ISSUE_NUM": "1213",
                                        "DRY_RUN_PREV_BODY": str(prev)})
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
    frag = _frag(failed=False, failed_tests=[], go_exit_code=0, unattributed_failure=False)
    prev = tmp_path / "prev.md"
    prev.write_text(_row(1213, _frag())["body"], encoding="utf-8")
    proc = _run_report(tmp_path, frag, {"DRY_RUN_ISSUE_NUM": "1213",
                                        "DRY_RUN_PREV_BODY": str(prev)})
    problems = [w for w in ("issue close", "issue comment") if w in proc.stdout]
    assert not problems, (
        "a green night must be recorded as `not seen` in the body, not by "
        f"closing or commenting (found {problems}). One green night does not prove "
        "a repeat-run failure fixed, and close/reopen churn notifies twice per flap."
    )
    assert "issue edit 1213" in proc.stdout, "the open issue was not refreshed on a green night"
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


def _issue_body(inc: dict, frag: dict, **kw) -> str:
    return compose_body(inc, frag, _state(tests=inc.get("tests") or []),
                        "https://gh/runs/999", "2026-10-14", True, "", **kw)


def _bodies(frag: dict) -> dict[str, str]:
    """Rendered body per incident kind, state block stripped."""
    return {i["kind"]: _strip_state_blocks(_issue_body(i, frag)) for i in derive_incidents(frag)}


def test_issue_content_carries_the_failing_test_names(tmp_path: Path) -> None:
    """The direct cause of four unnoticed duplicates: a body with no evidence."""
    frag = _frag(failed_tests=["TestConfigManager_HotReload", "TestWatchLoop/reload",
                               "TestWatchLoop"])
    # Asserted against the RENDERED BODIES, not the process output, and with the
    # state block stripped: names surviving only inside `tests=[...]` or in the
    # step summary made an earlier version of this guard vacuous.
    rendered = "\n".join(_strip_state_blocks(_issue_body(i, frag))
                         for i in derive_incidents(frag))
    problems = []
    for name in frag["failed_tests"]:
        if name not in rendered:
            problems.append(f"failing test {name!r} does not appear in any RENDERED issue body")
    if "race-raw-tenant-api" not in rendered:
        problems.append("the raw -json artifact pointer is missing from the body")
    if "First seen" not in rendered:
        problems.append("the first-seen date is missing from the body")
    assert not problems, (
        "issue body lost its evidence:\n  " + "\n  ".join(problems) + f"\n--- body ---\n{rendered}"
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
    with_race = _run_report(
        tmp_path / "b", _frag(had_data_race=True, race_tests=["TestX"]), {}
    ).stdout
    if "DATA RACE" not in with_race:
        problems.append("a REAL data race was not titled DATA RACE")
    assert not problems, "verdict wording drifted from the evidence:\n  " + "\n  ".join(problems)


def test_title_shape_carries_kind_subject_and_verdict(tmp_path: Path) -> None:
    out = _run_report(tmp_path, _frag(test_runs={
        "TestConfigManager_HotReload": {"fail": 10, "pass": 0}}), {}).stdout
    want = (TITLE_PREFIX_TMPL.format(module="tenant-api")
            + " — test: TestConfigManager_HotReload — fails every repetition (10/10)")
    assert want in out, f"title shape drifted; expected {want!r} in:\n{out}"


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
    inc = derive_incidents(_frag())[0]
    body = compose_body(inc, _frag(), _state(first_seen="2026-07-28"), "u", "2026-07-28",
                        False, "")
    assert "re-stamped" in body, (
        "an unreadable baseline silently reset first-seen. That erases the only record "
        "of how long this has been failing — the evidence four duplicate issues lost."
    )


# ── (c2) the diagnosis must follow the evidence, not default to isolation ────
#
# #3177: a package `-timeout` expiry on #2977 was narrated as "a repeat-run /
# test-isolation failure" with "bisect -count" as step 2, directly under a
# warning that said the cause was NOT attributed. #3343: a test that failed in
# all ten repetitions (a missing binary) was titled "repeat-run failure". These
# cases pin each prose block to the evidence that licenses it.

# The two phrases that ARE the isolation diagnosis. Asserted as phrases, not
# as the word "isolation": the timeout prose legitimately says it is NOT one.
ISOLATION_CLAIM = "This is a repeat-run / test-isolation failure"
ISOLATION_STEP = "Bisect the repetition count"


def _timeout_frag() -> dict:
    """A fragment produced by the REAL parser from the real timeout fixture."""
    from parse_go_test_json import parse_stream  # noqa: PLC0415

    fixture = ROOT / "tests" / "ops" / "fixtures" / "go_test_json_package_timeout.jsonl"
    parsed = parse_stream(fixture.read_text(encoding="utf-8").splitlines(), go_exit_code=1)
    return _frag(**parsed, module="threshold-exporter")


def _section(body: str, heading: str) -> str:
    """The text under `## <heading>` up to the next `## `."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", body, flags=re.S | re.M)
    return m.group(1) if m else ""


def test_package_timeout_gets_its_own_verdict_and_triage(tmp_path: Path) -> None:
    frag = _timeout_frag()
    incs = derive_incidents(frag)
    problems = []
    if [i["kind"] for i in incs] != ["timeout"]:
        problems.append(f"expected one timeout incident, got {[i['kind'] for i in incs]}")
    inc = incs[0]
    body = _strip_state_blocks(_issue_body(inc, frag))
    if "package timeout" not in file_race_report.incident_verdict(inc):
        problems.append("the verdict does not say `package timeout`")
    if inc["subject"] != "github.com/vencil/threshold-exporter/cmd/da-guard":
        problems.append(f"the subject is not the timed-out package: {inc['subject']!r}")
    what = _section(body, "What this is")
    if "Package timeout" not in what:
        problems.append("`What this is` does not describe the timeout")
    for phrase in (ISOLATION_CLAIM, ISOLATION_STEP):
        if phrase in body:
            problems.append(f"a timeout body still contains {phrase!r}")
    if "-timeout" not in _section(body, "Triage"):
        problems.append("Triage does not point at the package's time budget (`-timeout`)")
    for name in frag["timeout_running_tests"]:
        if name not in _section(body, "Package timeout"):
            problems.append(f"running-at-deadline test {name!r} is not listed")
    title_out = _run_report(tmp_path, frag, {}).stdout
    if "timeout: github.com/vencil/threshold-exporter/cmd/da-guard — package timeout" not in title_out:
        problems.append("the filed title does not carry the timeout fingerprint and verdict")
    assert not problems, (
        "a package timeout is still reported as something else:\n  "
        + "\n  ".join(problems) + f"\n--- body ---\n{body}"
    )


def test_unattributed_failure_does_not_assert_a_cause() -> None:
    """The prose must say what the warning says: we do not know."""
    body = _bodies(_frag(failed_tests=[], fail_events=1, unattributed_failure=True))["unattributed"]
    what = _section(body, "What this is")
    problems = []
    if "Cause NOT attributed" not in what:
        problems.append("`What this is` does not say the cause is unattributed")
    for phrase in (ISOLATION_CLAIM, ISOLATION_STEP):
        if phrase in body:
            problems.append(f"an unattributed body still asserts {phrase!r}")
    assert not problems, "\n  ".join(problems) + f"\n--- body ---\n{body}"


def test_every_repetition_failure_is_not_called_repeat_run(tmp_path: Path) -> None:
    """#3343: failed 10 of 10 (a missing binary) is a plain failure, not leaked state."""
    frag = _frag(test_runs={"TestConfigManager_HotReload": {"fail": 10, "pass": 0}})
    inc = derive_incidents(frag)[0]
    body = _strip_state_blocks(_issue_body(inc, frag))
    out = _run_report(tmp_path, frag, {}).stdout
    problems = []
    if "fails every repetition (10/10)" not in file_race_report.incident_verdict(inc):
        problems.append(f"verdict: {file_race_report.incident_verdict(inc)!r}")
    for phrase in ("repeat-run", ISOLATION_STEP, "isolation-shaped"):
        if phrase in body or phrase in out:
            problems.append(f"an every-repetition failure still says {phrase!r}")
    if "plain failure" not in _section(body, "What this is"):
        problems.append("`What this is` does not call it a plain failure")
    if "NOT leaked state" not in body:
        problems.append("the body does not rule out leaked state")
    assert not problems, "\n  ".join(problems) + f"\n--- body ---\n{body}"


def test_some_repetitions_failure_is_isolation_shaped() -> None:
    frag = _frag(test_runs={"TestConfigManager_HotReload": {"fail": 3, "pass": 7}})
    inc = derive_incidents(frag)[0]
    body = _strip_state_blocks(_issue_body(inc, frag))
    problems = []
    if "failed 3 of 10" not in file_race_report.incident_verdict(inc):
        problems.append(f"verdict: {file_race_report.incident_verdict(inc)!r}")
    for phrase in (ISOLATION_CLAIM, ISOLATION_STEP):
        if phrase not in body:
            problems.append(f"a k<n failure lost {phrase!r}")
    assert not problems, "\n  ".join(problems) + f"\n--- body ---\n{body}"


def test_unknown_repetition_count_picks_no_vocabulary() -> None:
    """An older fragment without `test_runs`: say unknown, never guess."""
    frag = _frag()
    frag.pop("test_runs", None)
    inc = derive_incidents(frag)[0]
    body = _strip_state_blocks(_issue_body(inc, frag))
    problems = []
    if "not recorded" not in file_race_report.incident_verdict(inc):
        problems.append(f"verdict: {file_race_report.incident_verdict(inc)!r}")
    for phrase in (ISOLATION_CLAIM, ISOLATION_STEP, "plain failure —"):
        if phrase in body:
            problems.append(f"an unknown count still asserts {phrase!r}")
    assert not problems, "\n  ".join(problems) + f"\n--- body ---\n{body}"


def test_isolation_narrative_appears_only_for_some_repetition_failures() -> None:
    shapes = {
        "k<n test": _frag(test_runs={"TestConfigManager_HotReload": {"fail": 3, "pass": 7}}),
        "k=n test": _frag(test_runs={"TestConfigManager_HotReload": {"fail": 10, "pass": 0}}),
        "build failure": _frag(failed_tests=[], build_failed=True),
        "data race": _frag(had_data_race=True, race_tests=["TestR"], failed_tests=["TestR"]),
        "timeout": _timeout_frag(),
        "unattributed": _frag(failed_tests=[], unattributed_failure=True),
        "missing fragment": file_race_report.load_fragment(Path("/nonexistent"),
                                                          "tenant-api")[0],
    }
    problems = []
    for name, frag in shapes.items():
        bodies = _bodies(frag)
        has = any(ISOLATION_CLAIM in b or ISOLATION_STEP in b for b in bodies.values())
        want = name == "k<n test"
        if has != want:
            problems.append(f"{name}: isolation narrative present={has}, expected {want}")
    assert not problems, (
        "the isolation diagnosis is not gated on k<n evidence:\n  " + "\n  ".join(problems)
    )


def test_timeout_with_failed_tests_files_both_without_contradiction() -> None:
    """One package can hit `-timeout` while a test fails elsewhere: two issues,
    and the timeout one must not deny the failure the other one reports."""
    frag = {**_timeout_frag(), "failed_tests": ["TestElsewhere"],
            "test_runs": {"TestElsewhere": {"fail": 1, "pass": 9}}}
    bodies = _bodies(frag)
    problems = []
    if sorted(bodies) != ["test", "timeout"]:
        problems.append(f"expected a timeout and a test issue, got {sorted(bodies)}")
    if ISOLATION_STEP not in bodies.get("test", ""):
        problems.append("the failing test lost its isolation triage step")
    for phrase in ("Do not bisect", "No test reported a failure"):
        if phrase in bodies.get("timeout", ""):
            problems.append(f"the timeout body still says {phrase!r}")
    assert not problems, "\n  ".join(problems)


def test_timed_out_round_trips_and_old_blocks_still_parse() -> None:
    restored = parse_state(render_state(_state(timed_out=1)))
    assert restored is not None and restored["timed_out"] == 1
    # A block written before `timed_out` existed must stay readable — an
    # unreadable baseline re-stamps first-seen.
    old = ("<!-- race-state: module=tenant-api first_seen=2026-07-24 "
           "first_run=https://gh/runs/1 last_run=https://gh/runs/2 last_green=none "
           "streak=3 had_data_race=0 build_failed=0 unattributed=1 tests_truncated=0 "
           "tests=[] -->")
    old_state = parse_state(old)
    assert old_state is not None
    assert old_state["timed_out"] == 0
    assert old_state["first_seen"] == "2026-07-24"
    assert old_state["unattributed"] == 1


def test_missing_fragment_reads_as_worse_not_clean(tmp_path: Path) -> None:
    """A leg that never uploaded has proven nothing."""
    proc = _run_report(tmp_path, None, {})
    problems = []
    if proc.returncode == 0:
        problems.append("exit code was 0 — a degraded report must red the run")
    if "issue create" not in proc.stdout:
        problems.append("no alert was filed for the module with no fragment")
    if "no-result: ghost" not in proc.stdout:
        problems.append("the missing fragment was not filed as a `no-result` incident")
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
    calls: list[list[str]] = []
    frag = _frag()
    rc = _run_main(tmp_path, frag, _list_script([_row(1213, frag)], calls, fail_edit=True),
                   monkeypatch)
    out = capsys.readouterr().out
    problems = []
    if not any(c[:3] == ["issue", "edit", "1213"] for c in calls):
        problems.append("never attempted the refresh — the test would be vacuous")
    if rc == 0:
        problems.append("exit code 0 despite the issue edit failing")
    if "Refreshed issue #1213 (silent)." in out:
        problems.append("claimed the refresh succeeded when the edit FAILED")
    assert not problems, (
        "a failed issue refresh was reported as success:\n  " + "\n  ".join(problems)
        + f"\n--- output ---\n{out}"
    )


def test_not_seen_refresh_failure_reds_the_run(tmp_path: Path, capsys, monkeypatch) -> None:
    calls: list[list[str]] = []
    frag = _frag(failed=False, failed_tests=[], go_exit_code=0)
    rc = _run_main(tmp_path, frag, _list_script([_row(1214, frag)], calls, fail_edit=True),
                   monkeypatch)
    out = capsys.readouterr().out
    assert any(c[:3] == ["issue", "edit", "1214"] for c in calls)
    assert rc != 0, "a failed not-seen refresh exited 0"
    assert "Refreshed issue #1214" not in out


def test_lookup_failure_is_not_mistaken_for_no_existing_issue(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A 502 on `issue list` must not fall through to 'nothing found' at exit 0.

    That fall-through silently re-mints the duplicate this whole change removes.
    Chosen behaviour: still FILE (a duplicate is visible and closable; a skipped
    first night is silence), but red the run and say so in the body.
    """
    calls: list[list[str]] = []

    def script(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return 1, ""
        return 0, ""

    rc = _run_main(tmp_path, _frag(), script, monkeypatch)
    out = capsys.readouterr().out
    create = next((c for c in calls if c[:2] == ["issue", "create"]), None)
    problems = []
    if rc == 0:
        problems.append("exit code 0 despite the dedup lookup failing")
    if "::error" not in out:
        problems.append("no ::error annotation for the failed lookup")
    if create is None:
        problems.append("the alert was skipped instead of filed loudly")
    elif "Dedup was unavailable" not in _arg(create, "--body"):
        problems.append("the filed body does not say dedup was unavailable")
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
    if any(c[:2] == ["issue", "list"] and "--label" in c for c in calls):
        problems.append("queried by a label that does not exist instead of unlabelled")
    if rc == 0:
        problems.append("exit code 0 — a degraded lookup key must red the run")
    assert not problems, "the label-outage path regressed:\n  " + "\n  ".join(problems)


def test_unlabelled_issue_from_an_outage_night_is_found(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The labelled list misses an issue filed during a label outage; the
    unlabelled fallback must find it, or the next night files a duplicate."""
    frag = _frag()
    row = _row(1300, frag)
    calls: list[list[str]] = []

    def script(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return 0, ("[]" if "--label" in args else json.dumps([row]))
        return 0, ""

    rc = _run_main(tmp_path, frag, script, monkeypatch)
    capsys.readouterr()
    writes = _writes(calls)
    assert [c[:3] for c in writes] == [["issue", "edit", "1300"]], writes
    assert rc == 0


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


def _tests_incident(names: list[str], **frag_over) -> tuple[dict, dict]:
    frag = _frag(failed_tests=names, **frag_over)
    incs = derive_incidents(frag)
    assert [i["kind"] for i in incs] == ["tests"]
    return incs[0], frag


def test_body_fits_the_github_cap_at_whole_package_failure_scale() -> None:
    """One broken shared fixture reds the WHOLE package — the realistic worst case.

    `components/tenant-api` has 933 top-level `func Test*` (measured), average
    name 34 chars. Each costs ~40 chars in the rendered list plus ~35 in the
    state block, so an unclamped body lands near 70k against GitHub's 65536 cap
    — a 422 on precisely the night the issue matters most.
    """
    names = [f"TestSomeReasonablyLongHandlerName_Subcase{i:04d}" for i in range(933)]
    inc, frag = _tests_incident(names, fail_events=9330)
    body = compose_body(inc, frag, _state(tests=inc["tests"]), "https://gh/runs/999",
                        "2026-07-28", True, "")
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
    if file_race_report.body_fingerprint(body) != inc["fingerprint"]:
        problems.append("the fingerprint marker did not survive")
    assert not problems, (
        f"body-size handling is wrong at {len(names)}-test scale (len={len(body)}):\n  "
        + "\n  ".join(problems)
    )


def test_pathological_body_is_truncated_with_a_visible_marker() -> None:
    """Backstop: even absurd names must not produce a 422 — or lose the marker."""
    names = [f"Test{'X' * 400}{i}" for i in range(MAX_RENDERED_TESTS)]
    inc, frag = _tests_incident(names)
    body = compose_body(inc, frag, _state(tests=names), "u", "2026-07-28", True, "")
    problems = []
    if len(body) > ISSUE_BODY_MAX:
        problems.append(f"body is {len(body)} chars, over the {ISSUE_BODY_MAX} cap")
    if "truncated" not in body.lower():
        problems.append("truncation happened silently, with no marker for the reader")
    if parse_state(body) is None:
        problems.append("the state block was destroyed by truncation")
    if file_race_report.body_fingerprint(body) != inc["fingerprint"]:
        problems.append("the fingerprint marker was destroyed by truncation")
    assert not problems, "pathological-body clamp failed:\n  " + "\n  ".join(problems)


def test_normal_body_is_not_truncated() -> None:
    """Discriminability: the clamp must not fire on an ordinary failure."""
    inc = derive_incidents(_frag())[0]
    body = _issue_body(inc, _frag())
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
    if "per module" in LABEL_DESC.lower():
        problems.append("LABEL_DESC still describes the retired per-module model")
    assert not problems, "label description would 422:\n  " + "\n  ".join(problems)


def test_issue_titles_fit_the_github_api_cap() -> None:
    problems = []
    if ISSUE_TITLE_MAX != 256:
        problems.append(f"ISSUE_TITLE_MAX is {ISSUE_TITLE_MAX}, GitHub's cap is 256")
    long_subject = "github.com/vencil/threshold-exporter/" + "very/deep/package/" * 20
    for module in _matrix_modules():
        for verdict in ("NO RESULT — leg produced no fragment", "not seen on 2026-10-14",
                        "isolation-shaped — failed 9 of 10 repetitions",
                        "WARNING, not failing — 1799.9 s of the 1800 s -timeout"):
            title = file_race_report.make_title(module, "slow-package", long_subject, verdict)
            if len(title) > 256:
                problems.append(f"{module}: {len(title)} > 256")
            if not title.endswith(f" — {verdict}"):
                problems.append(f"{module}: clamping cut the verdict: {title!r}")
    assert not problems, "issue title would 422 or lose its verdict:\n  " + "\n  ".join(problems)


# ── module SSOT <-> matrices <-> report drift ─────────────────────────────────
#
# Since #3541 the module list lives in ONE place, plan_race_shards.MODULES. The
# race matrix (module x shard) and the merge matrix (module) are the `plan`
# job's output, and the report computes --expected-modules from the same script.
# These guards pin that every consumer DERIVES from it: a hand-written copy
# anywhere is exactly the drift the old literal list allowed.

PLAN_PY = "scripts/ops/plan_race_shards.py"


def _matrix_modules() -> list[str]:
    """Modules the race + merge matrices run: whatever `plan` emits from the SSOT."""
    import plan_race_shards
    jobs = _jobs()
    problems = []
    for job, output in (("race", "race_matrix"), ("merge", "merge_matrix")):
        matrix = (jobs.get(job, {}).get("strategy") or {}).get("matrix")
        want = f"fromJSON(needs.plan.outputs.{output})"
        if not isinstance(matrix, str) or want not in matrix.replace(" ", ""):
            problems.append(f"{job}.strategy.matrix is {matrix!r}, expected ${{{{ {want} }}}}")
        if "plan" not in (jobs.get(job, {}).get("needs") or []):
            problems.append(f"{job} does not need `plan`")
    plan_job = jobs.get("plan") or {}
    plan_run = "\n".join(str(s.get("run") or "") for s in plan_job.get("steps") or [])
    if f"{PLAN_PY} plan" not in plan_run:
        problems.append(f"the plan job no longer runs `{PLAN_PY} plan`")
    outs = plan_job.get("outputs") or {}
    for output in ("race_matrix", "merge_matrix"):
        if f"steps.plan.outputs.{output}" not in str(outs.get(output)):
            problems.append(f"plan job output {output!r} is not the plan step's: {outs.get(output)!r}")
    assert not problems, "the matrices stopped deriving from the module SSOT:\n  " + "\n  ".join(problems)
    return plan_race_shards.module_names()


def _report_expected_modules() -> list[str]:
    """What the report's --expected-modules evaluates to, read from its run block."""
    import plan_race_shards
    run = "\n".join(
        str(s.get("run") or "") for s in _jobs()["report"]["steps"]
    )
    m = re.search(r'--expected-modules\s+"([^"]+)"', run)
    assert m is not None, (
        "could not find --expected-modules in the report job — the drift check below "
        "would be vacuous."
    )
    value = m.group(1)
    var = re.fullmatch(r"\$\{?(\w+)\}?", value)
    if var is None:
        # A literal list: compared below, and refused as a second copy.
        return [x.strip() for x in value.split(",") if x.strip()]
    assign = re.search(rf'^\s*{var.group(1)}="\$\(python3 {re.escape(PLAN_PY)} modules\)"\s*$',
                       run, re.M)
    assert assign is not None, (
        f"--expected-modules reads ${var.group(1)}, but nothing assigns it from "
        f"`python3 {PLAN_PY} modules` — cannot tell what the report expects"
    )
    return plan_race_shards.module_names()


def test_report_expected_modules_track_the_race_matrix() -> None:
    """A module added to the matrix but not to --expected-modules never reports.

    Same class as the third-party scan-matrix drift guard: the report cannot
    notice a fragment it was never told to expect, so the new module would fail
    silently forever. Since #3541 both sides must DERIVE from
    plan_race_shards.MODULES; an equal-today literal copy is still refused,
    because that is how they drifted before.
    """
    matrix, expected = sorted(_matrix_modules()), sorted(_report_expected_modules())
    assert matrix == expected, (
        "the report job's --expected-modules drifted from the race matrix.\n"
        f"  only in matrix : {sorted(set(matrix) - set(expected))}\n"
        f"  only in report : {sorted(set(expected) - set(matrix))}\n"
        "A module missing from --expected-modules is never reported on at all."
    )
    run = "\n".join(str(s.get("run") or "") for s in _jobs()["report"]["steps"])
    # _report_expected_modules already proved a variable is assigned from the
    # SSOT; here the argument itself must BE that variable, not a literal.
    assert re.search(r'--expected-modules\s+"\$\{?\w+\}?"', run), (
        "the report's --expected-modules is a hand-written list again; derive it from "
        f"`python3 {PLAN_PY} modules` so it cannot drift from the matrices"
    )


def test_every_module_reaches_both_matrices_in_the_plan(tmp_path: Path) -> None:
    """The plan emits a merge leg and >=1 race leg for every SSOT module."""
    import plan_race_shards

    def fake_go_list(args, cwd=None):
        return subprocess.CompletedProcess(args, 0, b"x/a\t1\t0\nx/b\t1\t0\n", b"")
    out = plan_race_shards.plan(None, runner=fake_go_list, root=tmp_path)
    names = plan_race_shards.module_names()
    assert [m["module"] for m in out["merge"]] == names
    assert sorted({leg["module"] for leg in out["race"]}) == sorted(names)


def test_report_job_fans_in_from_the_matrix() -> None:
    jobs = _jobs()
    report, merge = jobs["report"], jobs.get("merge") or {}
    problems = []
    if report.get("needs") != ["merge"]:
        problems.append(f"report needs is {report.get('needs')!r}, expected ['merge']")
    if "always()" not in str(report.get("if") or ""):
        problems.append("report `if` lacks always() — a red matrix leg would skip reporting")
    if (report.get("strategy") or {}).get("matrix"):
        problems.append("the report job grew a matrix — that reintroduces the write race")
    if "race" not in (merge.get("needs") or []):
        problems.append(f"merge needs is {merge.get('needs')!r}; it must wait for the race legs")
    if "always()" not in str(merge.get("if") or ""):
        problems.append("merge `if` lacks always() — a red race leg would skip its module's fragment")
    merge_run = "\n".join(str(s.get("run") or "") for s in merge.get("steps") or [])
    if merge_run.count("parse_go_test_json.py") != 1:
        problems.append("merge must parse each module's merged stream exactly once")
    if any("parse_go_test_json" in str(s.get("run") or "") for s in jobs["race"].get("steps") or []):
        problems.append("a race shard parses its own stream — fragments must come from the merge")
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


# ---------------------------------------------------------------------------
# -short split between nightly and PR CI (#2977 option A, #3180)
# ---------------------------------------------------------------------------
# cmd/da-guard's exhaustive matrices honour testing.Short(). Nightly runs them
# reduced (otherwise -count=10 blows the 30m per-package timeout); PR CI runs
# them in full, once per PR. Both halves are pinned here, because each half
# alone is a silent regression: nightly losing -short times out again, and PR
# CI gaining -short drops the full matrix from EVERY gate with nothing red.

CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXPORTER_WORKDIR = "components/threshold-exporter/app"
_SHORT_FLAG_RE = re.compile(r"^--?short(?:=(.*))?$")


def _go_test_commands(run: str) -> list[list[str]]:
    """Every `go test ...` invocation in a run block, as whitespace tokens.

    Backslash continuations are joined first so a flag on a continuation line
    still belongs to its command; a command ends at a newline, `|`, `;` or `&&`.
    """
    joined = re.sub(r"\\\n", " ", run)
    cmds = []
    for line in joined.splitlines():
        for segment in re.split(r"\|\|?|;|&&", line):
            tokens = segment.split()
            for i in range(len(tokens) - 1):
                if tokens[i] == "go" and tokens[i + 1] == "test":
                    cmds.append(tokens[i + 2:])
                    break
    return cmds


# Package patterns that include cmd/da-guard, where the exhaustive matrices
# live. A narrower scope (`./internal/...`) keeps the step green while the
# matrices silently stop running in PR CI.
_DA_GUARD_PACKAGES = ("./...", "./cmd/...", "./cmd/da-guard", "./cmd/da-guard/...")


def _covers_da_guard(tokens: list[str]) -> bool:
    return any(tok in _DA_GUARD_PACKAGES for tok in tokens if tok.startswith("./"))


def _has_short(tokens: list[str]) -> bool:
    for tok in tokens:
        m = _SHORT_FLAG_RE.match(tok)
        if m and (m.group(1) is None or m.group(1).lower() in ("1", "t", "true")):
            return True
    return False


def _load_workflow(path: Path) -> dict:
    """Parse a workflow; unreadable means unmeasured, never clean."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AssertionError(f"cannot parse {path.relative_to(ROOT)}: {exc}") from exc
    assert isinstance(doc, dict) and isinstance(doc.get("jobs"), dict), (
        f"{path.relative_to(ROOT)} has no `jobs:` mapping — cannot locate the go test step"
    )
    return doc


def test_nightly_go_test_runs_with_short() -> None:
    """Nightly is the repeat-stress tier and keeps -short for good (#3541).

    Full coverage is PR CI's job (see the next test); a package is one test
    binary, so sharding cannot split a full matrix run x10 across legs.
    """
    steps =(_load_workflow(WORKFLOW)["jobs"].get("race") or {}).get("steps") or []
    cmds = [c for s in steps for c in _go_test_commands(str(s.get("run") or ""))]
    assert cmds, "no `go test` command found in nightly-race.yaml's race job"
    missing = [" ".join(c) for c in cmds if not _has_short(c)]
    assert not missing, (
        "nightly go test lost -short (#2977). Without it cmd/da-guard's exhaustive "
        "matrices run in full x10 and hit the 30m per-package timeout:\n  "
        + "\n  ".join(missing)
    )


def test_pr_ci_exporter_race_step_runs_the_full_matrix() -> None:
    """#2977 acceptance 3: PR CI keeps the exhaustive matrix (no -short)."""
    jobs = _load_workflow(CI_WORKFLOW)["jobs"]
    found, shorted, narrowed = [], [], []
    for job_id, job in jobs.items():
        for step in (job or {}).get("steps") or []:
            if str(step.get("working-directory") or "").rstrip("/") != EXPORTER_WORKDIR:
                continue
            for cmd in _go_test_commands(str(step.get("run") or "")):
                if "-race" not in cmd:
                    continue
                where = f"{job_id} / {step.get('name')!r}"
                found.append(where)
                if _has_short(cmd):
                    shorted.append(f"{where}: go test {' '.join(cmd)}")
                if not _covers_da_guard(cmd):
                    narrowed.append(f"{where}: go test {' '.join(cmd)}")
    assert found, (
        f"no `go test ... -race` step with working-directory {EXPORTER_WORKDIR} in "
        "ci.yml — the step moved or was renamed; re-anchor this guard, do not delete it"
    )
    assert not shorted, (
        "PR CI's threshold-exporter race step runs with -short, so the exhaustive "
        "cmd/da-guard matrices are reduced in EVERY gate (#2977 acceptance 3; make "
        "them cheaper instead, #3180):\n  " + "\n  ".join(shorted)
    )
    assert not narrowed, (
        "PR CI's threshold-exporter race step no longer includes cmd/da-guard, so "
        "its exhaustive matrices run in no gate at all (#2977 acceptance 3):\n  "
        + "\n  ".join(narrowed)
    )


# ── (f) slow packages are tracked, but are warnings (#3541 PR-B) ─────────────
#
# A package over half its per-binary -timeout gets its own `slow-package`
# fingerprint issue, filed / refreshed / not-seen like every other kind — but
# it is NOT a failure: it must not turn a green module red, suppress
# `unattributed`, or be filed a second time for a package that timed out.

SLOW_PKG = "github.com/vencil/tenant-api/internal/handler"


def _slow(pkg: str = SLOW_PKG, elapsed: float = 940.79) -> dict:
    return {"package": pkg, "elapsed_s": elapsed}


def _slow_frag(**over) -> dict:
    """A green night with one slow package, as the REAL parser shapes it."""
    from parse_go_test_json import GO_TEST_TIMEOUT_S, SLOW_PACKAGE_RATIO  # noqa: PLC0415

    base = {"failed": False, "failed_tests": [], "go_exit_code": 0, "fail_events": 0,
            "slow_packages": [_slow()],
            "slow_package_threshold_s": GO_TEST_TIMEOUT_S * SLOW_PACKAGE_RATIO,
            "go_test_timeout_s": GO_TEST_TIMEOUT_S}
    base.update(over)
    return _frag(**base)


def test_slow_package_kind_is_registered() -> None:
    assert "slow-package" in file_race_report.INCIDENT_KINDS
    assert file_race_report.WARNING_KINDS == {"slow-package"}
    assert file_race_report.WARNING_KINDS <= set(file_race_report.INCIDENT_KINDS)


def test_slow_package_derivation() -> None:
    tp = {"package": SLOW_PKG, "after": "30m0s", "running_tests": []}
    cases = {
        "green + slow": (_slow_frag(), [("slow-package", SLOW_PKG)]),
        "failing test + slow": (_slow_frag(failed=True, failed_tests=["TestA"], go_exit_code=1),
                                [("test", "TestA"), ("slow-package", SLOW_PKG)]),
        # Defence in depth: the parser already drops it, an odd fragment must too.
        "timed out is not also slow": (
            _slow_frag(failed=True, go_exit_code=1, timed_out=True,
                       timeout_packages=[tp]), [("timeout", SLOW_PKG)]),
        "slow does not hide an unattributed failure": (
            _slow_frag(failed=True, go_exit_code=1, unattributed_failure=True),
            [("unattributed", "tenant-api"), ("slow-package", SLOW_PKG)]),
        "build subsumes it": (_slow_frag(failed=True, build_failed=True),
                              [("build", "tenant-api")]),
        "older fragment without the field": (
            _frag(failed=False, failed_tests=[], go_exit_code=0), []),
    }
    problems = []
    for name, (frag, want) in cases.items():
        got = _kinds(frag)
        if got != want:
            problems.append(f"{name}: got {got}, expected {want}")
    inc = derive_incidents(_slow_frag())[0]
    if inc["fingerprint"] != f"tenant-api|slow-package|{SLOW_PKG}":
        problems.append(f"fingerprint shape drifted: {inc['fingerprint']}")
    assert not problems, "slow-package derivation drifted:\n  " + "\n  ".join(problems)


def test_slow_package_body_explains_elapsed_threshold_and_triage() -> None:
    frag = _slow_frag()
    body = _bodies(frag)["slow-package"]
    problems = []
    for phrase in ("940.8 s", "900 s", "1800 s", "one test binary", "cannot split it",
                   "race-raw-tenant-api-<shard>", "rank the tests"):
        if phrase.lower() not in body.lower():
            problems.append(f"body does not say {phrase!r}")
    for phrase in (ISOLATION_CLAIM, ISOLATION_STEP, "Package timeout"):
        if phrase in body:
            problems.append(f"a slow-package body contains {phrase!r}")
    if f"go test {SLOW_PKG} -race -count=10 -short" not in _section(body, "Triage"):
        problems.append("Triage does not reproduce the single package with the nightly flags")
    if "WARNING, not failing" not in file_race_report.incident_verdict(derive_incidents(frag)[0]):
        problems.append("the verdict does not read as a warning")
    assert not problems, ("slow-package body is incomplete:\n  " + "\n  ".join(problems)
                          + f"\n--- body ---\n{body}")


def test_a_slow_package_keeps_a_green_module_green(tmp_path: Path, capsys, monkeypatch) -> None:
    calls: list[list[str]] = []
    rc = _run_main(tmp_path, _slow_frag(), _list_script([], calls), monkeypatch)
    out = capsys.readouterr().out
    creates = [c for c in _writes(calls) if c[:2] == ["issue", "create"]]
    problems = []
    if rc != 0:
        problems.append(f"a slow package alone made the report exit {rc}")
    if "### tenant-api — GREEN (" not in out or "1 warning(s)" not in out:
        problems.append("the module no longer reads GREEN with its warning listed")
    if "incident(s)" in out:
        problems.append("a warning was counted as a failing incident")
    if len(creates) != 1:
        problems.append(f"expected one filed slow-package issue, got {len(creates)}")
    else:
        title = _arg(creates[0], "--title")
        labels = [creates[0][i + 1] for i, a in enumerate(creates[0]) if a == "--label"]
        if f"slow-package: {SLOW_PKG} — WARNING" not in title:
            problems.append(f"title does not carry kind/subject/warning verdict: {title!r}")
        if labels != ["bug", "ci", "P2", LABEL]:
            problems.append(f"labels differ from the other kinds: {labels}")
    assert not problems, "slow package broke the module verdict:\n  " + "\n  ".join(problems)


def test_failures_are_counted_apart_from_slow_warnings(tmp_path: Path, capsys, monkeypatch) -> None:
    frag = _slow_frag(failed=True, failed_tests=["TestA"], go_exit_code=1)
    _run_main(tmp_path, frag, _list_script([], []), monkeypatch)
    out = capsys.readouterr().out
    assert "### tenant-api — 1 incident(s) — 1 warning(s)" in out, out


def test_slow_package_issue_is_refreshed_then_marked_not_seen(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    frag = _slow_frag()
    row = _row(3601, frag, ("slow-package", SLOW_PKG), state=_state(streak=2))
    calls: list[list[str]] = []
    _run_main(tmp_path, frag, _list_script([row], calls), monkeypatch)
    capsys.readouterr()
    refreshed = _writes(calls)
    problems = []
    if [c[:3] for c in refreshed] != [["issue", "edit", "3601"]]:
        problems.append(f"expected one refresh of #3601, got {refreshed}")
    else:
        state = parse_state(_arg(refreshed[0], "--body"))
        if not state or state["streak"] != 3:
            problems.append(f"streak not carried: {state}")

    # The next night the package is back under the line: not seen, never closed.
    calls = []
    green = _slow_frag(slow_packages=[])
    rc = _run_main(tmp_path / "n2", green, _list_script([row], calls), monkeypatch)
    out = capsys.readouterr().out
    unseen = _writes(calls)
    if [c[:3] for c in unseen] != [["issue", "edit", "3601"]]:
        problems.append(f"expected one not-seen edit of #3601, got {unseen}")
    elif "not seen on" not in _arg(unseen[0], "--title"):
        problems.append("the not-seen night is not in the title")
    if "### tenant-api — GREEN (" not in out or rc != 0:
        problems.append(f"green night without the slow package did not read GREEN (rc={rc})")
    assert not problems, "slow-package lifecycle broken:\n  " + "\n  ".join(problems)


def test_slow_package_that_timed_out_tonight_says_so(tmp_path: Path, capsys, monkeypatch) -> None:
    """Absent because it got WORSE must not read like relief."""
    tp = {"package": SLOW_PKG, "after": "30m0s", "running_tests": ["TestH"]}
    frag = _slow_frag(failed=True, go_exit_code=1, slow_packages=[], timed_out=True,
                      timeout_packages=[tp], timeout_running_tests=["TestH"])
    row = _row(3602, frag, ("slow-package", SLOW_PKG))
    calls: list[list[str]] = []
    _run_main(tmp_path, frag, _list_script([row], calls), monkeypatch)
    capsys.readouterr()
    edit = [c for c in _writes(calls) if c[:3] == ["issue", "edit", "3602"]]
    assert edit, f"#3602 was not refreshed: {_writes(calls)}"
    assert "hit the `-timeout` tonight" in _arg(edit[0], "--body")


def test_no_result_night_leaves_slow_package_issues_alone(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Same as every other kind: an unmeasured night tells no issue `not seen`."""
    row = _row(3603, _slow_frag(), ("slow-package", SLOW_PKG))
    calls: list[list[str]] = []
    rc = _run_main(tmp_path, None, _list_script([row], calls), monkeypatch,
                   modules="tenant-api")
    out = capsys.readouterr().out
    edits = [c for c in _writes(calls) if c[:2] == ["issue", "edit"]]
    assert not edits, f"an unmeasured slow-package issue was edited: {edits}"
    assert "#3603" in out and "Not re-evaluated" in out
    assert rc != 0, "a missing fragment must still red the run"


def test_slow_package_end_to_end_from_the_real_parser() -> None:
    """parse_stream -> fragment fields -> incident, with no hand-built fragment."""
    from parse_go_test_json import GO_TEST_TIMEOUT_S, parse_stream  # noqa: PLC0415

    lines = [json.dumps({"Action": "pass", "Package": p, "Elapsed": e})
             for p, e in ((SLOW_PKG, GO_TEST_TIMEOUT_S * 0.6), ("m/fast", 12.0))]
    frag = _frag(**parse_stream(lines, go_exit_code=0), failed=False)
    assert _kinds(frag) == [("slow-package", SLOW_PKG)]
    inc = derive_incidents(frag)[0]
    assert inc["threshold_s"] == GO_TEST_TIMEOUT_S / 2 and inc["timeout_s"] == GO_TEST_TIMEOUT_S


def _cache_keys(steps: list) -> list[str]:
    return [str((s.get("with") or {}).get("key") or "") for s in steps
            if str(s.get("uses") or "").startswith("actions/cache")]


def test_each_race_module_restores_the_go_cache_pr_ci_saves_for_it() -> None:
    """#3541: nightly legs restore `<os>-go-<module>-<hash>`, and for every
    module in plan_race_shards.MODULES a PR CI job that runs that module's Go
    tests saves exactly that key. A shared key kept whichever job saved first,
    so tenant-api's in-test generator-tool builds never reached the nightly."""
    import plan_race_shards
    race = _load_workflow(WORKFLOW)["jobs"]["race"]
    nightly = _cache_keys(race.get("steps") or [])
    want_nightly = "${{ runner.os }}-go-${{ matrix.module }}-${{ hashFiles('**/go.mod', '**/go.sum') }}"
    assert nightly == [want_nightly], (
        f"nightly race legs must restore exactly the per-module PR CI key, got {nightly}")

    ci_jobs = _load_workflow(CI_WORKFLOW)["jobs"]
    missing = []
    for m in plan_race_shards.MODULES:
        want = "${{ runner.os }}-go-" + m["module"] + "-${{ hashFiles('**/go.mod', '**/go.sum') }}"
        savers = []
        for job_id, job in ci_jobs.items():
            steps = (job or {}).get("steps") or []
            runs_module = any(
                str(s.get("working-directory") or "").rstrip("/") == m["workdir"]
                and _go_test_commands(str(s.get("run") or "")) for s in steps)
            if runs_module and want in _cache_keys(steps):
                savers.append(job_id)
        if not savers:
            missing.append(f"{m['module']}: no ci.yml job running go test in {m['workdir']} saves {want}")
    assert not missing, "\n".join(missing)
