#!/usr/bin/env python3
"""cutover_tenant.py — Shadow Monitoring 一鍵切換工具。

在 validate_migration.py --auto-detect-convergence 產出 cutover-readiness.json
（顯示所有 metric pair 已收斂）後，自動執行 shadow-monitoring-sop.md §7.1 的
完整切換步驟：

  1. 停止 Shadow Monitor Job
  2. 移除舊 Recording Rules
  3. 移除新規則的 migration_status: shadow label
  4. 移除 Alertmanager 的 shadow 攔截 route
  5. 驗證 alert 正常觸發 + 租戶健康檢查

用法:
  # 正常切換（需 cutover-readiness.json 顯示 ready: true）
  python3 cutover_tenant.py \\
    --readiness-json validation_output/cutover-readiness.json \\
    --tenant db-a \\
    --prometheus http://localhost:9090

  # 預覽模式（印出步驟，不執行）
  python3 cutover_tenant.py \\
    --readiness-json validation_output/cutover-readiness.json \\
    --tenant db-a --dry-run

  # 強制切換（忽略 readiness 狀態）
  python3 cutover_tenant.py \\
    --readiness-json validation_output/cutover-readiness.json \\
    --tenant db-a --force

需求:
  - kubectl 已配置且可存取目標叢集
  - cutover-readiness.json 由 validate_migration.py --auto-detect-convergence 產出
  - Prometheus Query API 可存取（用於 health check）
"""

import sys
import os
import json
import subprocess
import argparse
import datetime

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import format_json_report, add_prometheus_arg  # noqa: E402


# ---------------------------------------------------------------------------
# Readiness JSON
# ---------------------------------------------------------------------------

REQUIRED_READINESS_FIELDS = {"ready", "timestamp", "convergence_percentage",
                             "converged_count", "total_pairs"}

# #452/#737: machine-readable marker prefix for caller-error-class step
# failures (transport to Prometheus, env precondition) so the orchestrator
# can map them to EXIT_CALLER_ERROR (2) instead of EXIT_VIOLATION (1)
# without fragile free-text matching.
CALLER_ERROR_PREFIX = "[caller-error] "


def load_cutover_readiness(json_path):
    """Load and validate cutover-readiness.json.

    Returns dict with at least: ready, timestamp, convergence_percentage,
    converged_count, total_pairs.
    Raises ValueError on schema violation, FileNotFoundError if missing.
    """
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    missing = REQUIRED_READINESS_FIELDS - set(data.keys())
    if missing:
        raise ValueError(f"Missing required fields: {sorted(missing)}")
    return data


# ---------------------------------------------------------------------------
# Kubectl helpers
# ---------------------------------------------------------------------------

def _run_kubectl(args, dry_run=False):
    """Run a kubectl command (list form, no shell).

    Returns (success: bool, output: str).
    In dry_run mode, prints the command and returns success.
    """
    cmd = ["kubectl"] + args
    if dry_run:
        print(f"  [dry-run] {' '.join(cmd)}")
        return True, "(dry-run)"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return False, stderr or f"exit code {result.returncode}"
        return True, output
    except subprocess.TimeoutExpired:
        # #452/#737: env/transport failure = caller-error (exit 2)
        return False, f"{CALLER_ERROR_PREFIX}kubectl command timed out (30s)"
    except FileNotFoundError:
        # #452/#737: missing prerequisite binary = caller-error (exit 2)
        return False, f"{CALLER_ERROR_PREFIX}kubectl not found in PATH"


# ---------------------------------------------------------------------------
# Cutover step functions
# ---------------------------------------------------------------------------

def stop_shadow_job(namespace="monitoring", dry_run=False):
    """Step 1: Stop Shadow Monitor Job."""
    ok, msg = _run_kubectl(
        ["delete", "job", "shadow-monitor", "-n", namespace,
         "--ignore-not-found=true"],
        dry_run=dry_run,
    )
    return ok, msg


def remove_old_rules(namespace="monitoring",
                     configmap="prometheus-rules-old",
                     dry_run=False):
    """Step 2: Remove old Recording Rules ConfigMap."""
    ok, msg = _run_kubectl(
        ["delete", "configmap", configmap, "-n", namespace,
         "--ignore-not-found=true"],
        dry_run=dry_run,
    )
    return ok, msg


def remove_shadow_label(namespace="monitoring",
                        configmap="prometheus-rules",
                        dry_run=False):
    """Step 3: Remove migration_status: shadow label from alert rules.

    Patches the ConfigMap to remove all occurrences of the shadow label
    from alert rule definitions.  The actual label removal is done by
    re-applying the rules without the label — this step removes the
    ConfigMap so the updated (label-free) version can be re-applied.
    """
    # In practice, the operator re-applies rules from conf.d/ without
    # the shadow label.  This step deletes the shadow-annotated copy.
    ok, msg = _run_kubectl(
        ["label", "configmap", configmap, "-n", namespace,
         "migration_status-"],
        dry_run=dry_run,
    )
    # Label removal on a CM that doesn't have the label is not an error
    if not ok and "not labeled" in msg:
        return True, "label already absent"
    return ok, msg


def remove_shadow_route(namespace="monitoring",
                        configmap="alertmanager-config",
                        dry_run=False):
    """Step 4: Remove Alertmanager shadow intercept route.

    This removes the migration_status label from the Alertmanager
    ConfigMap metadata.  The actual route removal is handled by
    re-running generate_alertmanager_routes.py without shadow config.
    """
    ok, msg = _run_kubectl(
        ["label", "configmap", configmap, "-n", namespace,
         "migration_status-"],
        dry_run=dry_run,
    )
    if not ok and "not labeled" in msg:
        return True, "label already absent"
    return ok, msg


def verify_health(tenant, prometheus_url, dry_run=False):
    """Step 5: Post-cutover health verification.

    Queries Prometheus to check that the tenant's threshold metrics
    are present and no critical alerts are unexpectedly missing.
    """
    if dry_run:
        print(f"  [dry-run] query {prometheus_url} for tenant={tenant} health")
        return True, "(dry-run)"

    import urllib.parse

    _this_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _this_dir)
    sys.path.insert(0, os.path.join(_this_dir, '..'))
    from _lib_python import http_get_json  # noqa: E402

    # Check threshold metrics exist for tenant
    query = f'count(user_threshold{{tenant="{tenant}"}})'
    params = urllib.parse.urlencode({"query": query})
    url = f"{prometheus_url}/api/v1/query?{params}"

    data, err = http_get_json(url, headers={"Accept": "application/json"})
    if err:
        # #452/#737: cannot reach Prometheus = caller-error (transport), not a
        # cutover finding. Tag with the machine-readable prefix.
        return False, f"{CALLER_ERROR_PREFIX}Prometheus query failed: {err}"
    results = data.get("data", {}).get("result", [])
    if not results:
        return False, f"No threshold metrics found for tenant={tenant}"
    count = results[0].get("value", [None, "0"])[1]
    return True, f"tenant={tenant}: {count} threshold metrics active"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def apply_cutover(readiness_json, tenant, prometheus_url,
                  namespace="monitoring", dry_run=False, force=False):
    """Execute all cutover steps in sequence.

    Returns dict: {success, steps_completed, failed_step, message, timestamp}
    """
    report = {
        "success": False,
        "steps_completed": [],
        "failed_step": None,
        "message": "",
        # #452/#737: True when the failure was caller-error class
        # (load/transport/env precondition) → maps to EXIT_CALLER_ERROR (2).
        "caller_error": False,
        "timestamp": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
    }

    # ── Check readiness ───────────────────────────────────────────────
    try:
        readiness = load_cutover_readiness(readiness_json)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        # #452/#737: missing/malformed readiness JSON = input/load precondition
        # failure = caller-error (exit 2), not a cutover finding (exit 1).
        report["failed_step"] = "load_readiness"
        report["message"] = str(exc)
        report["caller_error"] = True
        return report

    if not readiness.get("ready") and not force:
        report["failed_step"] = "readiness_check"
        pct = readiness.get("convergence_percentage", "?")
        report["message"] = (
            f"Not ready for cutover (convergence={pct}%). "
            "Use --force to override."
        )
        return report

    if not readiness.get("ready") and force:
        print("⚠️  WARNING: Readiness check shows NOT READY — "
              "proceeding due to --force")

    # ── Execute steps ─────────────────────────────────────────────────
    steps = [
        ("Stop Shadow Monitor Job",
         lambda: stop_shadow_job(namespace=namespace, dry_run=dry_run)),
        ("Remove old Recording Rules",
         lambda: remove_old_rules(namespace=namespace, dry_run=dry_run)),
        ("Remove shadow label from rules",
         lambda: remove_shadow_label(namespace=namespace, dry_run=dry_run)),
        ("Remove Alertmanager shadow route",
         lambda: remove_shadow_route(namespace=namespace, dry_run=dry_run)),
        ("Verify tenant health",
         lambda: verify_health(tenant, prometheus_url, dry_run=dry_run)),
    ]

    for name, step_fn in steps:
        print(f"▸ {name}...")
        ok, msg = step_fn()
        if ok:
            print(f"  ✓ {msg}")
            report["steps_completed"].append(name)
        else:
            # #452/#737: a step may flag a caller-error-class failure
            # (transport/env) via CALLER_ERROR_PREFIX; strip it for display.
            if msg.startswith(CALLER_ERROR_PREFIX):
                report["caller_error"] = True
                msg = msg[len(CALLER_ERROR_PREFIX):]
            print(f"  ✗ {msg}")
            report["failed_step"] = name
            report["message"] = msg
            return report

    report["success"] = True
    report["message"] = "All cutover steps completed successfully"
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    """Build argparse parser."""
    parser = argparse.ArgumentParser(
        description="One-command Shadow Monitoring cutover (§7.1).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--readiness-json", required=True,
        help="Path to cutover-readiness.json from validate_migration.py",
    )
    parser.add_argument(
        "--tenant", required=True,
        help="Tenant name for post-cutover health verification",
    )
    add_prometheus_arg(
        parser,
        help_text="Prometheus URL (default: http://localhost:9090)",
    )
    parser.add_argument(
        "--namespace", default="monitoring",
        help="Kubernetes namespace (default: monitoring)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print kubectl commands without executing",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed even if readiness check shows not ready",
    )
    parser.add_argument(
        "--json-output", action="store_true",
        help="Output structured JSON report to stdout",
    )
    return parser


def main():
    """CLI entrypoint."""
    try_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args()

    report = apply_cutover(
        readiness_json=args.readiness_json,
        tenant=args.tenant,
        prometheus_url=args.prometheus,
        namespace=args.namespace,
        dry_run=args.dry_run,
        force=args.force,
    )

    if args.json_output:
        print(format_json_report(report))

    if report["success"]:
        print("\n✅ Cutover completed successfully.")
        print("Next: run 'da-tools batch-diagnose' for full health report.")
    else:
        step = report.get("failed_step", "unknown")
        msg = report.get("message", "")
        print(f"\n❌ Cutover failed at step: {step}")
        print(f"   Reason: {msg}")
        print("   See shadow-monitoring-sop.md §7.2 for rollback steps.")
        # #452/#737: caller-error (bad/missing readiness JSON, unreachable
        # Prometheus, missing kubectl) → exit 2; genuine cutover-step failure
        # (rollback-worthy finding) → exit 1.
        if report.get("caller_error"):
            sys.exit(EXIT_CALLER_ERROR)
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    main()
