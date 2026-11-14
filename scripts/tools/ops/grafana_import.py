#!/usr/bin/env python3
"""grafana_import.py — Grafana dashboard import via ConfigMap sidecar.

Automates the manual kubectl commands for Grafana dashboard deployment
documented in grafana-dashboards.md (Method B: ConfigMap Sidecar).

Usage:
  # Import Platform Overview dashboard
  python3 grafana_import.py \
    --dashboard k8s/03-monitoring/dynamic-alerting-overview.json \
    --name grafana-dashboard-overview \
    --namespace monitoring

  # Import Shadow Monitoring dashboard
  python3 grafana_import.py \
    --dashboard k8s/03-monitoring/shadow-monitoring-dashboard.json \
    --name grafana-dashboard-shadow \
    --namespace monitoring

  # Import all dashboards in a directory
  python3 grafana_import.py \
    --dashboard-dir k8s/03-monitoring/ \
    --namespace monitoring

  # Dry run (preview kubectl commands without executing)
  python3 grafana_import.py \
    --dashboard k8s/03-monitoring/dynamic-alerting-overview.json \
    --dry-run

  # Verify existing dashboard ConfigMaps
  python3 grafana_import.py --verify --namespace monitoring
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, partial_answer)
import _lib_input  # noqa: E402  (#3445: --dashboard is read by the lib)

#: --dashboard: a Grafana dashboard, a JSON object (#3445: a list or a scalar
#: was "Invalid JSON: 'int' object has no attribute 'get'"; content that
#: cannot be read, or another top level, is rc 2 naming the flag and path).
DASHBOARD_DOCUMENT = _lib_input.Document("json", dict)
#: ``--dashboard-dir``: its top-level ``*.json`` entries (#3693).
DASHBOARD_FILES = _lib_input.TopLevel("*.json")
from _lib_kubectl import KubectlError, kubectl_list, kubectl_run  # noqa: E402  (#3349, #3539)

#: Seconds one kubectl command of an import may take.
_KUBECTL_TIMEOUT_S = 120


def run_kubectl(verb, args, dry_run=False, stdin_text=None):
    """``kubectl <verb> <args>`` through ``_lib_kubectl.kubectl_run`` (#3539):
    the whole ``CompletedProcess`` once kubectl has exited — the caller reads
    its exit status. ``KubectlError`` (rc 2, reason kubectl_error) when
    kubectl is not on PATH, cannot be started, or does not finish in time.

    In dry-run mode, prints the command but does not execute (None).
    """
    if not isinstance(args, list):
        raise TypeError(f"run_kubectl() requires list argument, got {type(args).__name__}")
    if dry_run:
        print(f"  [DRY RUN] kubectl {verb} {' '.join(args)}", file=sys.stderr)
        return None
    return kubectl_run(verb, args, timeout=_KUBECTL_TIMEOUT_S, stdin_text=stdin_text,
                       what=f"`kubectl {verb} {' '.join(args[:2])}`")


def read_dashboards(dashboards):
    """``[(path, cm_name, content, raw)]`` for *dashboards* ``[(path, cm_name,
    content, flag)]``: every file's bytes read by ``_lib_input.read_bytes``
    BEFORE any is imported (#3724) — a file gone or unreadable since its flag
    was judged is a refusal naming the flag and the file (rc 2) with no
    ConfigMap applied yet. ``--dashboard`` was judged and decoded by
    ``_lib_input.document``; its bytes for kubectl are read here once more."""
    return [(path, cm_name, content, _lib_input.read_bytes(path, flag=flag, lang="en"))
            for path, cm_name, content, flag in dashboards]


class _NoExitStatus(Exception):
    """kubectl gave no exit status part-way through one dashboard (not
    startable, out of time, killed): ``rows`` is what that dashboard got to.
    ``main`` keeps the rows and attempts no dashboard after it — each would
    wait out the same timeout again (review of #3539)."""

    def __init__(self, rows):
        super().__init__("kubectl gave no exit status")
        self.rows = rows


def import_dashboard(dashboard_path, cm_name, namespace, dry_run=False, dashboard=None, *,
                     raw):
    """Import a single Grafana dashboard JSON as a labeled ConfigMap.

    *raw*: its bytes, read by `read_dashboards` before any dashboard is
    imported. *dashboard*: its parsed content when already read (--dashboard,
    by ``_lib_input.document``); None for a file of --dashboard-dir, decoded
    here from *raw*. ⛔ #3724: kubectl is handed a copy of *raw*, never the
    caller's path to read again (gone, or other bytes, by then).
    """
    results = []

    dashboard_p = Path(dashboard_path)

    # Validate JSON
    try:
        if dashboard is None:
            dashboard = json.loads(raw.decode("utf-8"))
        title = dashboard.get("title", dashboard_p.name)
    # UnicodeDecodeError is a ValueError; a nesting too deep, a RecursionError.
    except (ValueError, AttributeError, RecursionError) as e:
        results.append({
            "action": "import",
            "status": "fail",
            "caller_error": True,  # malformed input file = caller-error (exit 2)
            "detail": f"Invalid JSON: {str(e)[:60]}",
        })
        return results

    filename = dashboard_p.name

    def create_args(source):
        # Step 1: Create ConfigMap (using --dry-run=client to generate, then apply)
        return ["configmap", cm_name,
                f"--from-file={filename}={source}",
                "-n", namespace,
                "--dry-run=client", "-o", "yaml"]

    if dry_run:
        run_kubectl("create", create_args(dashboard_path), dry_run=True)
        run_kubectl("apply", ["-f", "-"], dry_run=True)
        results.append({
            "action": "create_configmap",
            "status": "dry-run",
            "detail": f"Would create ConfigMap {cm_name} from {filename}",
        })
    else:
        with tempfile.TemporaryDirectory(prefix="da-grafana-import-") as tmp:
            copy = Path(tmp) / filename
            copy.write_bytes(raw)
            # Nothing of THIS dashboard is in the cluster yet. A kubectl that
            # gives no exit status here raises KubectlError; `main` turns it
            # into this dashboard's row and stops — the dashboards before it
            # are applied, and their rows stay in the report.
            created = run_kubectl("create", create_args(copy))
        if created.returncode != 0:
            results.append({
                "action": "create_configmap",
                "status": "fail",
                "caller_error": True,  # kubectl transport/env failure = caller-error
                "detail": f"kubectl create configmap failed for {cm_name}",
            })
            return results

        # Apply the generated YAML
        try:
            proc = run_kubectl("apply", ["-f", "-"], stdin_text=created.stdout.strip())
            if proc.returncode == 0:
                results.append({
                    "action": "create_configmap",
                    "status": "pass",
                    "detail": f"ConfigMap {cm_name} applied ({title})",
                })
            else:
                results.append({
                    "action": "create_configmap",
                    "status": "fail",
                    "caller_error": True,  # kubectl apply transport/env failure = caller-error
                    "detail": f"kubectl apply failed: {proc.stderr[:60]}",
                })
                return results
        except KubectlError as e:
            # ⛔ Not "nothing was applied": a kubectl that did not finish in
            # time may have sent the ConfigMap (the message says which).
            results.append({
                "action": "create_configmap",
                "status": "fail",
                "caller_error": True,  # kubectl not run / no exit in time = caller-error
                "detail": str(e),
            })
            raise _NoExitStatus(results) from e

    # Step 2: Label ConfigMap for Grafana sidecar discovery
    label_args = [
        "configmap", cm_name,
        "grafana_dashboard=1",
        "-n", namespace,
        "--overwrite",
    ]
    try:
        labelled = run_kubectl("label", label_args, dry_run=dry_run)
    except KubectlError as e:
        # ⛔ Part-way (#3539): the ConfigMap above IS applied, without the
        # label the sidecar discovers it by. A row, so the report still says
        # what was applied — not one ERROR line that drops it.
        results.append({
            "action": "label_configmap",
            "status": "fail",
            "caller_error": True,  # kubectl not run / no exit in time = caller-error
            "detail": f"ConfigMap {cm_name} is applied, its label is NOT confirmed: {e}",
        })
        raise _NoExitStatus(results) from e
    if dry_run or labelled.returncode == 0:
        results.append({
            "action": "label_configmap",
            "status": "dry-run" if dry_run else "pass",
            "detail": f"Label grafana_dashboard=1 {'would be ' if dry_run else ''}applied to {cm_name}",
        })
    else:
        results.append({
            "action": "label_configmap",
            "status": "fail",
            "caller_error": True,  # kubectl label transport/env failure = caller-error
            "detail": f"Failed to label ConfigMap {cm_name}",
        })

    return results


def verify_dashboards(namespace):
    """Verify existing Grafana dashboard ConfigMaps."""
    checks = []

    # List ConfigMaps with grafana_dashboard label. #3349: a read that got
    # no answer (forbidden, unreachable, no kubectl, out of time, not JSON)
    # raises KubectlError — rc 2, reason kubectl_error — via _lib_kubectl.
    items = kubectl_list("configmap", namespace=namespace, selector="grafana_dashboard=1",
                         timeout=120)

    if not items:
        checks.append({
            "check": "dashboard_configmaps",
            "status": "warn",
            "detail": f"No ConfigMaps with grafana_dashboard=1 in namespace {namespace}",
        })
        return checks

    for item in items:
        name = item.get("metadata", {}).get("name", "?")
        data_keys = list(item.get("data", {}).keys())

        # Validate each JSON in the ConfigMap
        for key in data_keys:
            raw = item.get("data", {}).get(key, "")
            try:
                dashboard = json.loads(raw)
                title = dashboard.get("title", "untitled")
                panel_count = len(dashboard.get("panels", []))
                checks.append({
                    "check": f"cm/{name}/{key}",
                    "status": "pass",
                    "detail": f"Valid dashboard: \"{title}\" ({panel_count} panels)",
                })
            except json.JSONDecodeError:
                checks.append({
                    "check": f"cm/{name}/{key}",
                    "status": "fail",
                    "detail": "Invalid JSON content",
                })

    return checks


def auto_name(dashboard_path):
    """Generate a ConfigMap name from a dashboard filename."""
    base = Path(dashboard_path).stem
    # Convert to safe k8s name: lowercase, hyphens
    safe = base.lower().replace("_", "-").replace(" ", "-")
    return f"grafana-{safe}"


def main():
    """CLI entry point: Grafana dashboard import via ConfigMap sidecar."""
    parser = argparse.ArgumentParser(
        description="Grafana dashboard import via ConfigMap sidecar",
    )
    _lib_input.add_input(parser, "--dashboard", decode=DASHBOARD_DOCUMENT,
                         help="Path to dashboard JSON file")
    # #3693: a declared input — listed by `_lib_input` (once), a directory
    # that cannot be listed or holds no *.json named, rc 2.
    _lib_input.add_input(parser, "--dashboard-dir", select=DASHBOARD_FILES,
                         empty=_lib_input.EMPTY_REFUSED,
                         help="Import all *.json files in directory")
    parser.add_argument("--name",
                        help="ConfigMap name (auto-generated if omitted)")
    parser.add_argument("--namespace", default="monitoring",
                        help="Kubernetes namespace (default: monitoring)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview kubectl commands without executing")
    parser.add_argument("--verify", action="store_true",
                        help="Verify existing dashboard ConfigMaps")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    if args.verify:
        checks = verify_dashboards(args.namespace)
        has_failure = any(c["status"] == "fail" for c in checks)
        # #452/#737: kubectl transport / unparseable-output failures are
        # caller-errors (exit 2), not dashboard findings (exit 1).
        caller_error = any(
            c["status"] == "fail" and c.get("caller_error") for c in checks
        )

        status = (EXIT_CALLER_ERROR if caller_error
                  else EXIT_VIOLATION if has_failure else EXIT_OK)
        if args.json:
            # #3617: the checks kubectl did not answer are named; none
            # answered is rc 2 with stdout empty.
            return partial_answer({  # #3216: run() encodes it
                "tool": "grafana-import",
                "mode": "verify",
                "status": "fail" if has_failure else "pass",
                "checks": checks,
            }, status,
                unanswered=[f"{c['check']}: {c.get('detail', '')}" for c in checks
                            if c["status"] == "fail" and c.get("caller_error")],
                of=len(checks))
        print(f"\n{'='*60}")
        print(f"  GRAFANA DASHBOARD VERIFICATION")
        print(f"{'='*60}")
        for c in checks:
            symbol = {"pass": "✓", "fail": "✗", "warn": "⚠"}.get(c["status"], "?")
            print(f"  {symbol} {c['check']:40s} {c['detail']}")
        print(f"\n  Overall: {'FAIL' if has_failure else 'PASS'}\n")
        sys.exit(status)

    # Collect dashboard files to import
    dashboards = []
    if args.dashboard is not None:  # '' is given: refused, named (#3797)
        cm_name = args.name or auto_name(args.dashboard)
        # #3445: read (and refused, named) before anything is imported.
        dashboards.append((args.dashboard, cm_name,
                           _lib_input.document(args, "dashboard", lang="en"), "--dashboard"))
    elif args.dashboard_dir is not None:  # #3693: a given "" is judged, named
        # #3693: `Path.glob` read a directory it could not list as one with
        # no *.json ("No dashboard files found.", no flag, no path) — the
        # declaration judges it, and an empty selection, by name (#452).
        for entry in _lib_input.selected(args, "dashboard_dir", lang="en"):
            fpath = str(entry)
            dashboards.append((fpath, auto_name(fpath), None, "--dashboard-dir"))
    else:
        parser.error("Provide --dashboard, --dashboard-dir, or --verify")

    all_results = []
    has_failure = False
    caller_error = False  # #452/#737: file-not-found / invalid-JSON / kubectl env

    pending = read_dashboards(dashboards)
    stopped = False
    for done, (dashboard_path, cm_name, content, raw) in enumerate(pending):
        try:
            results = import_dashboard(dashboard_path, cm_name, args.namespace, args.dry_run,
                                       dashboard=content, raw=raw)
        except KubectlError as exc:
            # ⛔ Part-way over a --dashboard-dir (review of #3539): the
            # dashboards before this one ARE in the cluster. One ERROR line
            # here dropped their rows — stdout empty for a run that wrote.
            # kubectl gave no exit status (not startable, out of time, killed),
            # so the ones after it are not attempted: each would wait again.
            stopped = True
            results = [{
                "action": "create_configmap",
                "status": "fail",
                "caller_error": True,  # kubectl not run / no exit in time = caller-error
                "detail": f"{exc}",
            }]
        except _NoExitStatus as exc:
            # The same, later in the dashboard: its apply or its label got no
            # exit status. Its rows stay (an applied ConfigMap whose label is
            # not confirmed among them).
            stopped = True
            results = exc.rows
        if stopped:
            left = len(pending) - done - 1
            if left:
                results[-1]["detail"] += (
                    f" — {left} dashboard(s) after {cm_name} were not attempted")
        all_results.extend(results)
        if any(r["status"] == "fail" for r in results):
            has_failure = True
        if any(r["status"] == "fail" and r.get("caller_error") for r in results):
            caller_error = True
        if stopped:
            break

    # #452/#737: file-not-found / invalid-JSON / kubectl env failures are
    # caller-errors (exit 2); a clean run that merely couldn't apply is still 1.
    status = (EXIT_CALLER_ERROR if caller_error
              else EXIT_VIOLATION if has_failure else EXIT_OK)
    if args.json:
        # #3617: see --verify.
        return partial_answer({  # #3216: run() encodes it
            "tool": "grafana-import",
            "mode": "dry-run" if args.dry_run else "import",
            "status": "fail" if has_failure else "pass",
            "actions": all_results,
        }, status,
            unanswered=[f"{r.get('file') or r.get('action', '?')}: {r.get('detail', '')}"
                        for r in all_results if r["status"] == "fail" and r.get("caller_error")],
            of=len(all_results))
    print(f"\n{'='*60}")
    print(f"  GRAFANA DASHBOARD IMPORT {'(DRY RUN)' if args.dry_run else ''}")
    print(f"{'='*60}")
    for r in all_results:
        symbol = {"pass": "✓", "fail": "✗", "dry-run": "⊘"}.get(r["status"], "?")
        print(f"  {symbol} {r['action']:30s} {r['detail']}")
    print(f"\n  Overall: {'FAIL' if has_failure else 'PASS'}\n")
    sys.exit(status)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
