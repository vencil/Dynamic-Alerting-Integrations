#!/usr/bin/env python3
"""shadow_verify.py — Shadow Monitoring readiness and convergence verification.

Automates the manual verification steps from the Shadow Monitoring SOP:
  - Pre-flight: rules loaded, mapping file, AM interception route
  - Runtime: mismatch count, tenant coverage, operational modes
  - Convergence: cutover-readiness.json assessment + 7-day zero-mismatch check

Usage:
  # Pre-flight check (before starting shadow monitoring)
  python3 shadow_verify.py preflight \
    --mapping migration_output/prefix-mapping.yaml \
    --prometheus http://localhost:9090

  # Runtime health check (during shadow monitoring)
  python3 shadow_verify.py runtime \
    --report-csv validation_output/validation-report.csv \
    --prometheus http://localhost:9090

  # Convergence check (before cutover decision)
  python3 shadow_verify.py convergence \
    --report-csv validation_output/validation-report.csv \
    --readiness-json validation_output/cutover-readiness.json \
    --prometheus http://localhost:9090

  # All checks combined
  python3 shadow_verify.py all \
    --mapping migration_output/prefix-mapping.yaml \
    --report-csv validation_output/validation-report.csv \
    --prometheus http://localhost:9090
"""

import argparse
import os
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import probe_health, add_prometheus_arg, prometheus_url, url_type  # noqa: E402
# #3133: every answer is checked by the lib; a query not answered is a failed
# check, never a missing one.
from _lib_prometheus import (  # noqa: E402
    QueryError, alertmanager_status, collapse_replicas, instant_or_error, vector_count)

#: The tenants in maintenance, one row each however many exporter replicas
#: serve the filter (#3483).
MAINTENANCE_QUERY = collapse_replicas('user_state_filter{filter="maintenance"}')

# --alertmanager when it is not given. Only a URL the caller gave is a source
# the run must read (#3133, owner 2026-10-23: an optional source).
_DEFAULT_ALERTMANAGER = "http://localhost:9093"
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError, partial_answer)
import _lib_input  # noqa: E402  (#3445: the input files are read by the lib)
from _lib_shadow_report import REPORT_COLUMNS, REPORT_FORMATS, parse_timestamp  # noqa: E402  (#3662)

#: The input files (#3445): read by `_lib_input.document` before any phase
#: runs — a path or content that cannot be read, or a top level of another
#: type, is rc 2 naming the flag and the path. The CSV is text: strict UTF-8,
#: parsed by the csv module here (a leading BOM is dropped, as `utf-8-sig`
#: did).
MAPPING_DOCUMENT = _lib_input.Document("yaml", dict)
READINESS_DOCUMENT = _lib_input.Document("json", dict)
#: #3662: the header is `_lib_shadow_report.REPORT_COLUMNS`, the one
#: definition `validate` writes the report by; a document is its data rows.
REPORT_CSV_DOCUMENT = _lib_input.Document("csv", list, columns=REPORT_COLUMNS,
                                          formats=REPORT_FORMATS)

# The module's one query call: (rows, None) or (None, why).
query_prometheus = instant_or_error


def _query_failed(check, err):
    """The check a query that was not answered produces (#452/#737: a
    caller error, exit 2; #3133: on every step, never left out)."""
    return {
        "check": check,
        "status": "fail",
        "caller_error": True,
        "detail": f"Query failed: {err[:60]}",
    }


def _count(check, results):
    """``(count, None)`` — 0 for an empty answer, as before — or ``(None,
    check)`` when the sample is not a finite count (#3133; it used to crash)."""
    try:
        return vector_count(results) or 0, None
    except QueryError as exc:
        return None, _query_failed(check, str(exc))


def run_cmd(cmd):
    """Execute a command safely using list arguments only (no shell=True)."""
    if not isinstance(cmd, list):
        raise TypeError(f"run_cmd() requires list argument, got {type(cmd).__name__}")
    try:
        return subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=120).strip()
    except subprocess.CalledProcessError:
        return None


def check_preflight(args, inputs=None):
    """Pre-flight checks before starting shadow monitoring. *inputs*: the
    documents `judge_inputs` read, by argparse dest."""
    checks = []
    inputs = inputs or {}

    # 1. Mapping file — read and judged by `judge_inputs` before any phase
    # runs (#3392: a missing one was this check's "fail", rc 1, a finding;
    # #3445: content it cannot read is named there too).
    mapping = inputs.get("mapping")
    if mapping is not None:
        pair_count = sum(1 for v in mapping.values()
                        if isinstance(v, dict) and v.get("original_metric"))
        checks.append({
            "check": "mapping_file",
            "status": "pass",
            "detail": f"{pair_count} comparison pairs found",
        })
    else:
        checks.append({
            "check": "mapping_file",
            "status": "skip",
            "detail": "No --mapping provided",
        })

    # 2. Prometheus reachable (shared probe_health — scheme-validated; r3 W2).
    # Success ⇒ probe answered 2xx (urlopen raises on 4xx/5xx), which subsumes
    # the historical `"ok" in body or resp.status == 200` disjunction — real
    # Prometheus answers 200 "Prometheus Server is Healthy." (no "ok"), so the
    # status leg was the one that ever fired; probe success maps to "pass".
    prom_url = prometheus_url(args.prometheus)  # #3528: env judged here
    body, probe_err = probe_health(f"{prom_url}/-/healthy")
    if probe_err is None:
        healthy = body.strip()
        checks.append({
            "check": "prometheus_healthy",
            "status": "pass",
            "detail": healthy[:80],
        })
    else:
        checks.append({
            "check": "prometheus_healthy",
            "status": "fail",
            "caller_error": True,  # #452/#737: transport failure = caller-error (exit 2)
            "detail": probe_err[:80],
        })

    # 3. Rule groups loaded
    results, err = query_prometheus(prom_url, "count(count by(__name__) ({__name__=~\"tenant:.*\"}))")
    if err:
        checks.append(_query_failed("recording_rules_loaded", err))
    else:
        count, failed = _count("recording_rules_loaded", results)
        checks.append(failed or {
            "check": "recording_rules_loaded",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} tenant:* recording rule metric names found",
        })

    # 4. Shadow rules present (migration_status=shadow)
    results, err = query_prometheus(prom_url, 'count({migration_status="shadow"}) or vector(0)')
    if err:
        checks.append(_query_failed("shadow_rules_present", err))
    else:
        count, failed = _count("shadow_rules_present", results)
        checks.append(failed or {
            "check": "shadow_rules_present",
            "status": "pass" if count > 0 else "warn",
            "detail": f"{count} shadow-tagged metric series",
        })

    # 5. Alertmanager shadow interception route
    # Given (--alertmanager) and not answering: a caller error, exit 2. The
    # default URL not answering stays a skip — nobody asked for it (#3133).
    try:
        am_status = alertmanager_status(args.alertmanager or _DEFAULT_ALERTMANAGER)
    except QueryError as exc:
        if args.alertmanager:
            checks.append(_query_failed("alertmanager_shadow_route", str(exc)))
        else:
            checks.append({
                "check": "alertmanager_shadow_route",
                "status": "skip",
                "detail": f"Alertmanager not reachable: {str(exc)[:60]}",
            })
    else:
        config_str = am_status["config"]["original"]
        has_shadow_route = "migration_status" in config_str
        checks.append({
            "check": "alertmanager_shadow_route",
            "status": "pass" if has_shadow_route else "warn",
            "detail": "Shadow interception route found" if has_shadow_route
                      else "No migration_status matcher found in AM config",
        })

    return checks


def check_runtime(args, inputs=None):
    """Runtime health checks during shadow monitoring. *inputs*: as
    `check_preflight`."""
    checks = []
    inputs = inputs or {}

    # 1. CSV report analysis (read by `judge_inputs`, #3392: a missing
    # one was a "skip" and an Overall PASS; #3445: content that was not
    # UTF-8 was a traceback)
    records = inputs.get("report_csv")
    if records is not None:
        mismatch_count = 0
        total_rows = 0
        tenants = set()
        for row in records:
            total_rows += 1
            if row["Status"].strip() == "mismatch":
                mismatch_count += 1
            tenant = row["Tenant"].strip()
            if tenant:
                tenants.add(tenant)

        mismatch_pct = (mismatch_count / total_rows * 100) if total_rows > 0 else 0
        checks.append({
            "check": "csv_mismatch_ratio",
            "status": "pass" if mismatch_count == 0 else "fail",
            "detail": f"{mismatch_count}/{total_rows} mismatches ({mismatch_pct:.1f}%)",
        })
        checks.append({
            "check": "csv_tenant_coverage",
            "status": "pass" if len(tenants) > 0 else "warn",
            "detail": f"{len(tenants)} tenants in report: {', '.join(sorted(tenants))}",
        })

    # 2. Tenant operational modes
    prom_url = prometheus_url(args.prometheus)  # #3528: env judged here
    # #3133: a mode query not answered is a failed check — it used to append
    # nothing, so the report could not tell "not checked" from "none".
    # One row per tenant, not per exporter replica (#3483); the set dedups too.
    results, err = query_prometheus(prom_url, MAINTENANCE_QUERY)
    if err:
        checks.append(_query_failed("maintenance_mode", err))
    elif results:
        maint_tenants = sorted({r.get("metric", {}).get("tenant", "?") for r in results})
        checks.append({
            "check": "maintenance_mode",
            "status": "warn",
            "detail": f"Tenants in maintenance: {', '.join(maint_tenants)}",
        })
    else:
        checks.append({
            "check": "maintenance_mode",
            "status": "pass",
            "detail": "No tenants in maintenance mode",
        })

    results, err = query_prometheus(prom_url, "user_silent_mode")
    if err:
        checks.append(_query_failed("silent_mode", err))
    elif results:
        silent_tenants = list({r.get("metric", {}).get("tenant", "?") for r in results})
        checks.append({
            "check": "silent_mode",
            "status": "warn",
            "detail": f"Tenants in silent mode: {', '.join(sorted(silent_tenants))}",
        })
    else:
        checks.append({
            "check": "silent_mode",
            "status": "pass",
            "detail": "No tenants in silent mode",
        })

    return checks


def check_convergence(args, inputs=None):
    """Convergence checks before cutover decision. *inputs*: as
    `check_preflight`."""
    checks = []
    inputs = inputs or {}

    # 1. cutover-readiness.json (read by `judge_inputs`, #3392: a missing
    # one was a "skip" and an Overall PASS)
    report = inputs.get("readiness_json")
    if report is not None:
        ready = report.get("ready", False)
        pct = report.get("convergence_percentage", 0)
        unconverged = report.get("unconverged_pairs", [])
        checks.append({
            "check": "cutover_readiness_json",
            "status": "pass" if ready else "fail",
            "detail": f"Convergence {pct}%, "
                      + ("READY" if ready else f"{len(unconverged)} pairs not yet stable"),
        })

    # 2. 7-day zero-mismatch (from CSV)
    records = inputs.get("report_csv")
    if records is not None:
        now = datetime.now(timezone.utc)
        recent_mismatches = 0
        recent_rows = 0
        # #3662: every Timestamp's form was judged at reading
        # (REPORT_FORMATS): one that did not parse was skipped here, in silence.
        for row in records:
            age_days = (now - parse_timestamp(row["Timestamp"])).total_seconds() / 86400
            if age_days <= 7:
                recent_rows += 1
                if row["Status"].strip() == "mismatch":
                    recent_mismatches += 1

        if recent_rows > 0:
            checks.append({
                "check": "seven_day_zero_mismatch",
                "status": "pass" if recent_mismatches == 0 else "fail",
                "detail": f"{recent_mismatches} mismatches in last 7 days ({recent_rows} rows)",
            })
        else:
            checks.append({
                "check": "seven_day_zero_mismatch",
                "status": "warn",
                "detail": "No data from last 7 days in CSV",
            })

    return checks


#: #3392: the files each phase reads, by flag and argparse dest.
_PHASE_INPUTS = {
    "preflight": (("--mapping", "mapping"),),
    "runtime": (("--report-csv", "report_csv"),),
    "convergence": (("--readiness-json", "readiness_json"), ("--report-csv", "report_csv")),
}


def judge_inputs(args, phases):
    """The documents of the files the given *phases* read, by argparse dest
    (``_lib_input.document``) — or one `CallerError` (rc 2) naming every
    file that is not there, not a regular file, cannot be opened, or whose
    content cannot be read (#3445) — before any phase runs or prints.

    #3392: a missing --mapping was the preflight check's "fail" (rc 1, read
    as a finding); a missing --report-csv / --readiness-json was a "skip"
    under an Overall PASS (rc 0). A flag not given stays each check's skip,
    and a flag no chosen phase reads is not judged. ⛔ ``''`` is given (an
    unset shell variable), and refused by its declaration (#3797): it was
    read as not given — a skip under an Overall PASS.
    """
    given = []
    for phase in phases:
        for _flag, dest in _PHASE_INPUTS.get(phase, ()):
            if getattr(args, dest) is not None and dest not in given:
                given.append(dest)
    inputs, refused = {}, []
    for dest in given:
        try:
            inputs[dest] = _lib_input.document(args, dest, lang="en")
        except CallerError as exc:
            refused.append(exc)
    if refused:
        raise CallerError("\nERROR: ".join(str(e) for e in refused), refused[0].reason)
    return inputs


def format_output(phase, checks, json_output=False):
    """Format and print check results."""
    if json_output:
        return {"phase": phase, "checks": checks}

    passed = sum(1 for c in checks if c["status"] == "pass")
    failed = sum(1 for c in checks if c["status"] == "fail")
    total = len(checks)

    print(f"\n{'='*60}")
    print(f"  {phase.upper()} ({passed}/{total} passed)")
    print(f"{'='*60}")
    for c in checks:
        symbol = {"pass": "✓", "fail": "✗", "warn": "⚠", "skip": "⊘"}.get(c["status"], "?")
        print(f"  {symbol} {c['check']:35s} {c['detail']}")
    return None


def build_parser():
    """The shadow-verify parser (#3445: its input files declared with
    `_lib_input`)."""
    parser = argparse.ArgumentParser(
        description="Shadow Monitoring readiness and convergence verification",
    )
    parser.add_argument(
        "phase",
        choices=["preflight", "runtime", "convergence", "all"],
        help="Verification phase to run",
    )
    _lib_input.add_input(parser, "--mapping", decode=MAPPING_DOCUMENT,
                         help="Path to prefix-mapping.yaml")
    _lib_input.add_input(parser, "--report-csv", decode=REPORT_CSV_DOCUMENT,
                         help="Path to validation-report.csv")
    _lib_input.add_input(parser, "--readiness-json", decode=READINESS_DOCUMENT,
                         help="Path to cutover-readiness.json")
    add_prometheus_arg(parser,
                       help_text="Prometheus Query API URL "
                                 "(default: $PROMETHEUS_URL, else http://localhost:9090)")
    parser.add_argument("--alertmanager", default=None, type=url_type(empty="refuse"),
                        help=f"Alertmanager API URL (default: {_DEFAULT_ALERTMANAGER}; "
                             "a URL given here that does not answer is exit 2, "
                             "the default not answering is a skip)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for CI integration)")
    return parser


def main():
    """CLI entry point: Shadow Monitoring readiness and convergence verification."""
    args = build_parser().parse_args()

    all_checks = []
    all_results = []
    has_failure = False
    caller_error = False  # #452/#737: transport/load failure → exit 2, not 1

    phases = (
        ["preflight", "runtime", "convergence"]
        if args.phase == "all"
        else [args.phase]
    )
    inputs = judge_inputs(args, phases)  # #3392 / #3445: before any phase runs or prints

    for phase in phases:
        if phase == "preflight":
            checks = check_preflight(args, inputs)
        elif phase == "runtime":
            checks = check_runtime(args, inputs)
        elif phase == "convergence":
            checks = check_convergence(args, inputs)
        else:
            continue

        if any(c["status"] == "fail" for c in checks):
            has_failure = True
        if any(c["status"] == "fail" and c.get("caller_error") for c in checks):
            caller_error = True

        if args.json:
            all_results.append({"phase": phase, "checks": checks})
        else:
            format_output(phase, checks)

    # #452/#737: unreachable Prometheus / query failure / CSV load failure are
    # caller-errors (exit 2); convergence/mismatch findings stay violation (1).
    status = (EXIT_CALLER_ERROR if caller_error
              else EXIT_VIOLATION if has_failure else EXIT_OK)
    if args.json:
        # #3617: the checks not answered are named; none answered is rc 2
        # with stdout empty.
        checks = [(p["phase"], c) for p in all_results for c in p["checks"]]
        return partial_answer({  # #3216: run() encodes it
            "tool": "shadow-verify",
            "status": "fail" if has_failure else "pass",
            "phases": all_results,
        }, status,
            unanswered=[f"{ph} {c['check']}: {c.get('detail', '')}" for ph, c in checks
                        if c["status"] == "fail" and c.get("caller_error")],
            of=len(checks))

    print(f"\n{'='*60}")
    print(f"  Overall: {'FAIL' if has_failure else 'PASS'}")
    print(f"{'='*60}\n")
    sys.exit(status)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
