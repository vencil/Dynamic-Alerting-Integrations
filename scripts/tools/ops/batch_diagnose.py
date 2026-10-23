#!/usr/bin/env python3
"""batch_diagnose.py — Post-cutover multi-tenant health report.

Auto-discovers all tenants from the threshold-config ConfigMap and runs
diagnose checks on each in parallel, producing a unified health report.

Usage:
  # Auto-discover tenants, parallel health check
  python3 batch_diagnose.py --prometheus http://localhost:9090

  # Specify tenants explicitly
  python3 batch_diagnose.py --tenants db-a,db-b --prometheus http://localhost:9090

  # Adjust parallelism and timeout
  python3 batch_diagnose.py --workers 10 --timeout 30

  # Dry-run: list tenants without checking
  python3 batch_diagnose.py --dry-run

  # Output JSON report to file
  python3 batch_diagnose.py --output /tmp/health-report.json

需求:
  - kubectl 可用且 ConfigMap threshold-config 存在（auto-discover 模式）
  - Prometheus Query API 可達（health check 模式）
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Import diagnose core function
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from diagnose import check as diagnose_check  # noqa: E402
from diagnose import query_prometheus  # noqa: E402
from diagnose import exit_code as diagnose_exit_code  # noqa: E402  (#2493)
from diagnose import CommandNotFoundError  # noqa: E402  (#2493)
from diagnose import CommandTimeoutError, DEADLINE_GRACE_S  # noqa: E402  (#2968)
from diagnose import deadline as diagnose_deadline  # noqa: E402  (#2968)
from _lib_python import (  # noqa: E402
    add_prometheus_arg, format_json_report, positive_int, positive_seconds,
    write_json_or_die,
)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR,
    arm_crash_envelope, disarm_crash_envelope,
)
import _lib_parse_tree  # noqa: E402  (ADR-036 §7 step 9, #3206: Go classes the keys)


# #2954: per-tenant deadline. diagnose.check() makes up to four Prometheus
# queries, each bounded by query_prometheus_instant's own 10s timeout, so a
# stuck Prometheus takes ~40s to come back as "Prometheus query failed". A
# shorter deadline would replace that diagnosis (and its recommendation) with
# a bare "timed out"; test_default_timeout_outlasts_the_prometheus_queries
# keeps the two in step.
DEFAULT_TIMEOUT_S = 60


def discover_tenants(namespace="monitoring", configmap="threshold-config"):
    """Discover tenant names from threshold-config ConfigMap keys.

    Returns sorted list of tenant IDs (excludes _defaults.yaml and
    other reserved keys starting with '_').
    """
    cmd = [
        "kubectl", "get", "configmap", configmap,
        "-n", namespace, "-o", "json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        if result.returncode != 0:
            print(f"ERROR: kubectl failed: {result.stderr.strip()}", file=sys.stderr)
            return []
        cm_data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("ERROR: kubectl timed out", file=sys.stderr)
        return []
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return []

    # #1588: the carrier here is a ConfigMap KEY, not a directory entry —
    # but the key names ARE the conf.d filenames, so this is the same
    # filename->tenant-id question the exporter's scanner answers, asked on
    # a different carrier. It was answered with yet another hand-written
    # copy of the rule, and a case-SENSITIVE one: measured with a synthetic
    # ConfigMap holding one tenant key, `db-a.yaml` -> ['db-a'] while
    # `DB-A.YAML` -> [], i.e. every tenant in the report vanished and the
    # run still exited 0.
    #
    # #1603: match the PRODUCER. `threshold-config` is built from a conf.d
    # by `scripts/ops/configmap_assemble.py` (what `make configmap-assemble`
    # runs), so its keys are conf.d filenames. A narrower set here
    # reproduces the original defect one hop downstream: a tenant set
    # smaller than what is deployed.
    #
    # ⛔ What each key IS is Go's answer, not a Python name predicate
    # (ADR-036 §7 step 9, #3206): `da-guard parse-tree --list-only --names`
    # classes each key by its name alone, by the exporter's own rule
    # (`confdname`, case-insensitive suffix — the producer's selection,
    # #1792). A key of class `tenant` carries the tenant id Go names
    # (`tenant_named`, case kept); `_`-prefixed keys (defaults / platform)
    # and names that are not config files are not tenants.
    data_keys = [k for k in cm_data.get("data", {}) if isinstance(k, str)]
    try:
        named = _lib_parse_tree.classify_names(data_keys) if data_keys else {}
    except _lib_parse_tree.ParseTreeError as exc:
        print(f"ERROR: cannot class the ConfigMap keys through `da-guard parse-tree "
              f"--names` (ADR-036): {' '.join(str(exc).split())}", file=sys.stderr)
        return []
    tenants = []
    for key in data_keys:
        entry = named[key]
        if entry.cls == "hidden":
            # The exporter's walk skips a hidden name, so agreeing with it is
            # right — but a dropped key must not be silent: the symptom of
            # the bug above was a tenant quietly missing from the report.
            print(f"WARNING: ConfigMap key {key!r} carries no tenant id — "
                  f"skipped", file=sys.stderr)
            continue
        if entry.cls != "tenant":
            continue
        tenant = entry.tenant_named
        if tenant in tenants:
            # Only reachable since this site started accepting both spellings:
            # `db-a.yaml` + `db-a.yml` are two keys, one stem.
            # ⛔ Says only what it knows. This tool identifies tenants by KEY
            # NAME; the exporter's own predicate is "one id declared in two
            # files", and an id comes from the file's `tenants:` mapping, not
            # from its stem (`validate_config` states this explicitly). So two
            # same-stem keys may or may not be a duplicate declaration — what
            # is certain is that this report can only count the stem once.
            print(f"WARNING: ConfigMap keys collapse to one tenant id here — "
                  f"{key!r} repeats {tenant!r}; counted once. This report "
                  f"names tenants by key, so check those two keys by hand",
                  file=sys.stderr)
            continue
        tenants.append(tenant)
    return sorted(tenants)


def _check_tenant(tenant, prom_url):
    """diagnose.check() for one tenant, as a result dict (never raises for the
    failures diagnose itself can report)."""
    import io

    try:
        # ⛔ Ask check() to write into OUR buffer. Do NOT reach for
        # `contextlib.redirect_stdout`: it rebinds the PROCESS-GLOBAL
        # `sys.stdout`, and this function runs in a ThreadPoolExecutor worker.
        # With two tenants the interleaving "A enters, B enters, A exits,
        # B exits" makes B restore A's buffer, so the real stdout is gone for
        # good and main()'s JSON report lands in a dead StringIO — the process
        # exits 0 having printed nothing. Measured before this fix: 91 of 100
        # runs of `batch_diagnose --tenants a,b --json` produced empty stdout.
        buf = io.StringIO()
        diagnose_check(tenant, prom_url, out=buf)
        output = buf.getvalue().strip()
        if output:
            result = json.loads(output)
        else:
            result = {"status": "error", "tenant": tenant, "issues": ["empty output"]}
    except (CommandNotFoundError, CommandTimeoutError) as exc:
        # `da-tools diagnose` alone exits 2 on these (the environment lacks
        # kubectl, or it did not answer in time), not 1; keep the same class
        # here (#2493, #2968). Before the generic OSError branch below, which
        # CommandTimeoutError would otherwise fall into.
        result = {"status": "error", "tenant": tenant, "issues": [str(exc)],
                  "_environment_error": True}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result = {"status": "error", "tenant": tenant, "issues": [str(exc)]}
    return result


def run_diagnose_for_tenant(tenant, prom_url, timeout_sec=30):
    """Run diagnose.check() for a single tenant, giving up after timeout_sec.

    Returns dict: {tenant, status, ...}; status "error" when the check could
    not run. A missing `kubectl` also sets `_environment_error` (see
    batch_exit_code), which main() strips before reporting.

    #2954: the check runs in a daemon thread joined with `timeout_sec`; a
    check still running then is reported as an environment error (it could
    not be completed, like a missing kubectl), and the thread is left behind —
    Python cannot stop it, but the batch no longer waits for it.

    #2968: the thread runs under diagnose.deadline(start + timeout_sec), so
    each kubectl it starts gets the time left minus a grace and is killed by
    subprocess before the join below gives up (an environment error, rc 2,
    like the join's own timeout); past the deadline none is started, and a
    Prometheus query gets at most the time left. The grace is
    min(DEADLINE_GRACE_S, timeout_sec / 4): a --timeout at or below the fixed
    0.5s would otherwise leave kubectl no time at all and report "the
    deadline has passed" before anything ran. That shrunken grace is
    best-effort: with a --timeout under about 0.5s, or a heavily loaded
    machine, a kubectl can still outlive the batch (see
    diagnose.DEADLINE_GRACE_S). Only kubectl itself is killed, not a
    credential plugin it started (see diagnose._DEADLINE).
    """
    start = time.monotonic()
    box = {}
    grace = min(DEADLINE_GRACE_S, timeout_sec / 4)

    def _work():
        try:
            # ⚠️ Set here, in the tenant's own thread: a new thread does not
            # inherit the context of the thread that started it.
            with diagnose_deadline(start + timeout_sec, grace=grace):
                box["result"] = _check_tenant(tenant, prom_url)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller below
            box["exc"] = exc

    worker = threading.Thread(target=_work, name=f"diagnose-{tenant}", daemon=True)
    worker.start()
    worker.join(timeout_sec)
    if worker.is_alive():
        result = {"status": "error", "tenant": tenant,
                  "issues": [f"timed out after {timeout_sec:g}s"],
                  "_environment_error": True}
    elif "exc" in box:
        raise box["exc"]
    else:
        result = box["result"]

    elapsed = time.monotonic() - start
    result["elapsed_seconds"] = round(elapsed, 2)
    return result


def generate_report(results, prom_url):
    """Generate unified health report from individual diagnose results."""
    healthy = [r for r in results if r.get("status") == "healthy"]
    # issue 1513: diagnose reports `unchecked` for a tenant whose Pod and
    # exporter checks were skipped (not MariaDB, or db_type not declared).
    # Counting it as healthy inflated the score with tenants nobody looked at.
    unchecked = [r for r in results if r.get("status") == "unchecked"]
    with_issues = [r for r in results
                   if r.get("status") not in ("healthy", "unchecked")]

    total = len(results)
    checked = total - len(unchecked)
    if checked > 0:
        health_score = round(len(healthy) / checked, 2)
    elif total > 0:
        health_score = None  # every tenant unchecked: no score, not 0% or 100%
    else:
        health_score = 0.0

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prometheus_url": prom_url,
        "total_tenants": total,
        "healthy_count": len(healthy),
        "unchecked_count": len(unchecked),
        "issue_count": len(with_issues),
        "health_score": health_score,
        "tenants": {r["tenant"]: r for r in results},
    }

    # Remediation recommendations
    recommendations = []
    for r in with_issues:
        tenant = r.get("tenant", "unknown")
        issues = r.get("issues", [])
        for issue in issues:
            if "Pod" in issue:
                recommendations.append(
                    f"{tenant}: Check pod status — kubectl get pods -n {tenant}"
                )
            elif "Exporter" in issue or "DOWN" in issue:
                recommendations.append(
                    f"{tenant}: Check exporter — kubectl logs -n {tenant} deploy/mariadb -c exporter --tail=20"
                )
            elif "Prometheus" in issue:
                recommendations.append(
                    f"{tenant}: Verify Prometheus connectivity to {prom_url}"
                )
            else:
                recommendations.append(f"{tenant}: {issue}")
    report["recommendations"] = recommendations

    return report


def print_text_report(report):
    """Print human-readable health report to stdout."""
    print()
    print("=" * 60)
    print("  Post-Cutover Health Report")
    print(f"  Generated: {report['timestamp']}")
    print("=" * 60)
    print()

    total = report["total_tenants"]
    healthy = report["healthy_count"]
    issues = report["issue_count"]
    unchecked = report.get("unchecked_count", 0)
    score = report["health_score"]
    checked = total - unchecked
    if score is None:
        print(f"  Overall Health Score: n/a (0/{total} tenants checked)")
    else:
        print(f"  Overall Health Score: {score:.0%} ({healthy}/{checked} checked tenants healthy)")
    print()

    # Healthy tenants
    if healthy > 0:
        print(f"  Healthy Tenants ({healthy}):")
        for name, data in report["tenants"].items():
            if data.get("status") == "healthy":
                mode = data.get("operational_mode", "normal")
                elapsed = data.get("elapsed_seconds", "?")
                suffix = f" [{mode}]" if mode != "normal" else ""
                print(f"    + {name}{suffix}  ({elapsed}s)")
        print()

    # Tenants whose Pod / exporter checks were skipped
    if unchecked > 0:
        print(f"  Unchecked Tenants ({unchecked}) — Pod and exporter checks skipped:")
        for name, data in report["tenants"].items():
            if data.get("status") == "unchecked":
                reasons = sorted({s.get("reason", "") for s in data.get("skipped", [])})
                print(f"    ? {name}: {'; '.join(r for r in reasons if r)}")
        print()

    # Tenants with issues
    if issues > 0:
        print(f"  Tenants with Issues ({issues}):")
        for name, data in report["tenants"].items():
            if data.get("status") not in ("healthy", "unchecked"):
                issue_list = data.get("issues", [])
                print(f"    - {name}: {', '.join(issue_list)}")
        print()

    # Recommendations
    if report["recommendations"]:
        print("  Remediation Steps:")
        for i, rec in enumerate(report["recommendations"], 1):
            print(f"    {i}. {rec}")
        print()

    print("=" * 60)


def batch_exit_code(results, environment_errors=()) -> int:
    """The run's exit code from every tenant's result (#2493).

    Each tenant is classified as ``diagnose`` classifies it alone; then a
    Prometheus failure on any tenant wins (2: the environment, not a finding —
    the same precedence as validate's query errors), else any other failure
    (1), else 0. Before this a failed check also exited 0, so a CI gate on
    batch-diagnose never failed.
    """
    codes = {diagnose_exit_code(r) for r in results}
    if environment_errors:          # e.g. no kubectl — as `diagnose` alone
        codes.add(EXIT_CALLER_ERROR)
    if EXIT_CALLER_ERROR in codes:
        return EXIT_CALLER_ERROR
    if EXIT_VIOLATION in codes:
        return EXIT_VIOLATION
    return EXIT_OK


def main():
    """CLI entry point: Post-cutover multi-tenant health report."""
    parser = argparse.ArgumentParser(
        description="Post-cutover multi-tenant health report",
    )
    parser.add_argument(
        "--tenants",
        help="Comma-separated tenant IDs (default: auto-discover from ConfigMap)",
    )
    add_prometheus_arg(
        parser,
        help_text="Prometheus Query API URL "
                  "(default: $PROMETHEUS_URL, else http://localhost:9090)",
    )
    parser.add_argument(
        "--workers", type=positive_int, default=5,
        help="Max parallel diagnose workers (default: 5)",
    )
    parser.add_argument(
        "--timeout", type=positive_seconds, default=DEFAULT_TIMEOUT_S,
        help=(f"Per-tenant timeout in seconds (> 0, fractions allowed; default: "
              f"{DEFAULT_TIMEOUT_S}); a tenant still being checked then is reported "
              "as an error"),
    )
    parser.add_argument(
        "--namespace", default="monitoring",
        help="K8s namespace for threshold-config ConfigMap (default: monitoring)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List tenants without running health checks",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON only (no text report)",
    )
    args = parser.parse_args()
    if args.json:
        # #3094: a crash in --json mode still prints one envelope, with the
        # dry-run document's keys below (nothing checked, no tenant counted)
        arm_crash_envelope(build=lambda: {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prometheus_url": args.prometheus,
            "total_tenants": 0,
            "healthy_count": None,
            "unchecked_count": None,
            "issue_count": None,
            "health_score": None,
            "tenants": {},
            "recommendations": [],
        })

    # Discover or parse tenants
    if args.tenants:
        tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]
    else:
        tenants = discover_tenants(
            namespace=args.namespace,
            configmap="threshold-config",
        )

    if not tenants:
        print("ERROR: No tenants found", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Dry-run: just list tenants
    if args.dry_run:
        print(f"Discovered {len(tenants)} tenants:", file=sys.stderr)
        for t in tenants:
            print(f"  - {t}", file=sys.stderr)
        print("\nUse without --dry-run to run health checks.", file=sys.stderr)
        if args.json:
            # #1112: same schema as generate_report(), with every MEASURED
            # field null because nothing was checked — `healthy_count` /
            # `issue_count` / `health_score` are all null rather than 0, so a
            # consumer cannot mistake "not measured" for "everything
            # unhealthy" (0/2 healthy would read as 0%). `total_tenants` stays
            # a real count: the tenants WERE discovered, just not checked.
            # `status` is the discriminator.
            print(format_json_report({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "dry_run",
                "reason": "health_checks_skipped",
                "prometheus_url": args.prometheus,
                "total_tenants": len(tenants),
                "healthy_count": None,
                "unchecked_count": None,
                "issue_count": None,
                "health_score": None,
                "tenants": {
                    t: {"tenant": t, "status": "not_checked"} for t in tenants
                },
                "recommendations": [],
            }))
        return EXIT_OK

    # Run diagnose in parallel
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_diagnose_for_tenant, tenant, args.prometheus, args.timeout,
            ): tenant
            for tenant in tenants
        }
        for future in as_completed(futures):
            tenant = futures[future]
            try:
                # Already bounded by run_diagnose_for_tenant's own deadline
                # (#2954); as_completed only yields finished futures.
                result = future.result()
            except Exception as exc:
                result = {
                    "status": "error",
                    "tenant": tenant,
                    "issues": [f"executor error: {exc}"],
                }
            results.append(result)

    # Internal marker only: the report keeps its schema.
    environment_errors = {r.get("tenant") for r in results
                          if r.pop("_environment_error", False)}

    # Sort results by tenant name
    results.sort(key=lambda r: r.get("tenant", ""))

    # Generate report
    report = generate_report(results, args.prometheus)

    # Output
    if args.json:
        print(format_json_report(report))
        # #3094: the document is out; a later -o write crash must not add a second
        disarm_crash_envelope()
    else:
        print_text_report(report)

    if args.output:
        # #1641: an unwritable -o is rc=2 + one line, not a traceback at rc=1.
        write_json_or_die(args.output, report, flag="-o/--output")
        if not args.json:
            print(f"\nJSON report written to: {args.output}")

    return batch_exit_code(results, environment_errors)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
