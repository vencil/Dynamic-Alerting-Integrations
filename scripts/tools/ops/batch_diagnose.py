#!/usr/bin/env python3
"""batch_diagnose.py — Post-cutover multi-tenant health report.

Reads the threshold-config ConfigMap — the conf.d threshold-exporter mounts —
as a tree, takes its tenants and each one's inheritance chain from
threshold-exporter's own reading of it (`da-guard`, #1525), and runs diagnose
checks on each in parallel, producing a unified health report.

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
  - kubectl 可用且 ConfigMap threshold-config 存在（兩種模式都讀它；--tenants 只挑其中的租戶）
  - da-guard（映像內建）
  - Prometheus Query API 可達（health check 模式）
"""
import argparse
import contextlib
import json
import os
import sys
import re
import tempfile
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
from diagnose import TenantNotFoundError, chain_for, load_chain_source  # noqa: E402  (#1525)
from diagnose import query_prometheus  # noqa: E402
from diagnose import exit_code as diagnose_exit_code  # noqa: E402  (#2493)
from _lib_kubectl import KubectlError, kubectl_get  # noqa: E402  (#3349)
from diagnose import CommandTimeoutError, DEADLINE_GRACE_S  # noqa: E402  (#2968)
from diagnose import deadline as diagnose_deadline  # noqa: E402  (#2968)
from _lib_python import (  # noqa: E402
    add_prometheus_arg, format_json_report, positive_int, positive_seconds,
    write_json_or_die,
)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError,
    arm_crash_envelope, disarm_crash_envelope,
)
from _lib_consumer_read import binary_data_bytes  # noqa: E402  (#3461: the one binaryData decode)
import _lib_parse_cache  # noqa: E402  (#1525: exit_on_parse_tree_error)
import _lib_parse_tree  # noqa: E402  (ADR-036 §7 step 9: Go classes the key names)
from _lib_io import safe_label  # noqa: E402
from _lib_tenant_values import exit_on_served_values_error  # noqa: E402  (#1525)


# #2954: per-tenant deadline. diagnose.check() makes up to four Prometheus
# queries, each bounded by query_prometheus_instant's own 10s timeout, so a
# stuck Prometheus takes ~40s to come back as "Prometheus query failed". A
# shorter deadline would replace that diagnosis (and its recommendation) with
# a bare "timed out"; test_default_timeout_outlasts_the_prometheus_queries
# keeps the two in step.
DEFAULT_TIMEOUT_S = 60


#: A ConfigMap key Kubernetes accepts (`[-._a-zA-Z0-9]+`, not `.`/`..`):
#: anything else would be written outside the tree below, so it is refused.
_CM_KEY = re.compile(r"[-._a-zA-Z0-9]+")


def read_configmap_files(namespace="monitoring", configmap="threshold-config"):
    """The ConfigMap's files, ``{key: bytes}`` — `data` and `binaryData`, as
    the kubelet mounts both into threshold-exporter's conf.d.

    #3349: a read that got no answer (forbidden, unreachable, no kubectl, out
    of time) raises `KubectlError` (rc 2, named); a ConfigMap that is not
    there is named too (`configmap_not_found`).
    """
    cm_data = kubectl_get("configmap", configmap, namespace=namespace, timeout=15)
    if cm_data is None:
        # kubectl answered: the ConfigMap is not there — not a kubectl_error.
        raise CallerError(f"ConfigMap {configmap!r} not found in namespace {namespace!r}: "
                          "nothing to read the tenants from",
                          reason="configmap_not_found")
    def unreadable(why):
        return CallerError(f"ConfigMap {configmap!r}: {why}", reason="configmap_unreadable")

    files = {}
    for section in ("data", "binaryData"):
        entries = cm_data.get(section) or {}
        if not isinstance(entries, dict):
            raise unreadable(f"{section} is not a mapping")
        for key, value in entries.items():
            if (not isinstance(key, str) or not _CM_KEY.fullmatch(key)
                    or key in (".", "..") or not isinstance(value, str)):
                raise unreadable(f"{section} key {safe_label(str(key))!r} is not a file "
                                 "this tool can write as the exporter would mount it")
            if key in files:
                raise unreadable(f"key {key!r} is in both data and binaryData")
            try:
                files[key] = (value.encode("utf-8") if section == "data"
                              else binary_data_bytes(value))
            except UnicodeEncodeError:
                raise unreadable(f"data key {key!r} is not encodable as UTF-8") from None
            except ValueError:   # binascii.Error is a ValueError
                raise unreadable(f"binaryData key {key!r} is not base64") from None
    return files


@contextlib.contextmanager
def configmap_tree(files, configmap="threshold-config"):
    """The ConfigMap written out as the flat conf.d threshold-exporter mounts
    (one file per key), in a temporary directory removed on exit."""
    with tempfile.TemporaryDirectory(prefix="batch-diagnose-") as tmp:
        root = os.path.join(tmp, configmap)
        os.mkdir(root)
        for key, body in files.items():
            try:
                with open(os.path.join(root, key), "xb") as fh:
                    fh.write(body)
            except FileExistsError:
                # Two keys one name here (a case-insensitive filesystem):
                # writing on would serve one key's body as the other's.
                raise CallerError(f"ConfigMap {configmap!r}: key {key!r} collides with "
                                  "another key on this filesystem",
                                  reason="configmap_unreadable") from None
        yield root


def resolve_tenants(files, requested=None, *, where="ConfigMap threshold-config",
                    configmap="threshold-config"):
    """``(tenants, chains)`` from the ConfigMap's files, answered by
    threshold-exporter's own reading of them (`da-guard`, #1525).

    ⛔ The tenant ids are Go's: the ids the files' `tenants:` mappings
    declare, not the key names. A key named `team-x.yaml` declaring `db-a`
    and `db-b` used to be reported as tenant `team-x`, and both real tenants
    were missing from the report. A tree the exporter cannot load raises
    (rc 2 through main's decorators), so no tenant is checked against a
    partial read. *requested* (``--tenants``) picks among them; a name the
    tree does not have raises `TenantNotFoundError` naming every one.
    Each tenant's chain is resolved here, in the main thread, before any
    health check runs.
    """
    with configmap_tree(files, configmap) as root:
        try:
            source = load_chain_source(root, where=where)
            named = _lib_parse_tree.classify_names(list(files)) if files else {}
        except BaseException:
            # da-guard's lines name files of the copy below, not the keys.
            print(f"NOTE: the paths named are keys of {where}, written out under "
                  f"{root} for da-guard (removed on exit)", file=sys.stderr)
            raise
        for key in sorted(files):
            if named[key].cls == "hidden":
                # The exporter's walk skips a hidden name, so agreeing with it
                # is right — but a dropped key must not be silent: the symptom
                # this tool once had was a tenant quietly missing (#1603).
                print(f"WARNING: ConfigMap key {key!r} is a hidden name threshold-exporter "
                      "does not read — skipped", file=sys.stderr)
        known = sorted(source.effective.tenants)
        if requested is not None:
            absent = [t for t in requested if t not in source.effective.tenants]
            if absent:
                raise TenantNotFoundError(
                    f"no tenant {', '.join(repr(safe_label(t)) for t in absent)} in "
                    f"{where}: threshold-exporter reads {len(known)} tenant(s) there")
            tenants = list(dict.fromkeys(requested))
        else:
            tenants = known
        return tenants, {t: chain_for(t, source) for t in tenants}


def _check_tenant(tenant, prom_url, chain=None):
    """diagnose.check() for one tenant, as a result dict (never raises for the
    failures diagnose itself can report). *chain*: the tenant's chain,
    already resolved (`resolve_tenants`)."""
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
        diagnose_check(tenant, prom_url, out=buf, chain=chain)
        output = buf.getvalue().strip()
        if output:
            result = json.loads(output)
        else:
            result = {"status": "error", "tenant": tenant, "issues": ["empty output"]}
    except (KubectlError, CommandTimeoutError) as exc:
        # `da-tools diagnose` alone exits 2 on these (the environment lacks
        # kubectl, or it did not answer in time), not 1; keep the same class
        # here (#2493, #2968). Before the generic OSError branch below, which
        # CommandTimeoutError would otherwise fall into.
        result = {"status": "error", "tenant": tenant, "issues": [str(exc)],
                  "_environment_error": True}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result = {"status": "error", "tenant": tenant, "issues": [str(exc)]}
    return result


def run_diagnose_for_tenant(tenant, prom_url, timeout_sec=30, chain=None):
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
                box["result"] = _check_tenant(tenant, prom_url, chain)
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


@exit_on_served_values_error  # #1525: a tree the exporter cannot load → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error
def main():
    """CLI entry point: Post-cutover multi-tenant health report."""
    parser = argparse.ArgumentParser(
        description="Post-cutover multi-tenant health report",
    )
    parser.add_argument(
        "--tenants",
        help="Comma-separated tenant IDs, each one the ConfigMap must declare "
             "(default: every tenant it declares)",
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

    # #1525: both modes read the ConfigMap; --tenants picks among its tenants.
    requested = ([t.strip() for t in args.tenants.split(",") if t.strip()]
                 if args.tenants else None)
    tenants, chains = resolve_tenants(
        read_configmap_files(namespace=args.namespace, configmap="threshold-config"),
        requested, where=f"ConfigMap {args.namespace}/threshold-config")

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
                chains[tenant],
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
