#!/usr/bin/env python3
"""Verify Prometheus Operator CRD deployment status.

Usage:
    da-tools operator-check [--namespace NS] [--prometheus URL] [--json] [--ci]

Checks: (1) Operator CRD, (2) PrometheusRule count, (3) ServiceMonitor,
(4) AlertmanagerConfig, (5) Target Health (opt)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import detect_cli_lang, http_get_json  # noqa: E402


# i18n strings
STRINGS = {
    "en": {
        "title": "Dynamic Alerting — Operator Check Report",
        "check_operator": "Operator Detection",
        "check_rules": "PrometheusRule",
        "check_monitor": "ServiceMonitor",
        "check_alertconfig": "AlertmanagerConfig",
        "check_targets": "Target Health",
        "pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP", "result": "Result",
        "operator_found": "Prometheus Operator detected",
        "rules_loaded": "rules loaded",
        "monitor_found": "ServiceMonitor found",
        "monitor_missing": "ServiceMonitor not found",
        "alertconfig_found": "tenant configs",
        "targets_healthy": "targets healthy",
        "targets_unhealthy": "targets unhealthy",
        "targets_skip": "no Prometheus URL",
        "error_api": "API call failed",
    },
    "zh": {
        "title": "Dynamic Alerting — Operator 檢查報告",
        "check_operator": "Operator 偵測", "check_rules": "PrometheusRule",
        "check_monitor": "ServiceMonitor", "check_alertconfig": "AlertmanagerConfig",
        "check_targets": "Target 健檢",
        "pass": "通過", "warn": "警告", "fail": "失敗", "skip": "跳過", "result": "結果",
        "operator_found": "Prometheus Operator 已偵測到",
        "rules_loaded": "已加載規則",
        "monitor_found": "ServiceMonitor 已找到",
        "monitor_missing": "ServiceMonitor 未找到",
        "alertconfig_found": "租戶配置",
        "targets_healthy": "Target 健康",
        "targets_unhealthy": "Target 不健康",
        "targets_skip": "未提供 Prometheus URL",
        "error_api": "API 調用失敗",
    }
}


def i18n(key: str, lang: str = "en") -> str:
    """Get i18n string."""
    return STRINGS.get(lang, STRINGS["en"]).get(key, key)


class CheckResult:
    """Single check result."""
    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status  # "pass", "warn", "fail", "skip"
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
        }


class OperatorChecker:
    """Prometheus Operator checker."""
    def __init__(self, args):
        self.args = args
        self.lang = detect_cli_lang()
        self.checks: list[CheckResult] = []

    def run_kubectl(self, *cmd) -> tuple[str, str, int]:
        """Run kubectl command. Return (stdout, stderr, returncode)."""
        full_cmd = ["kubectl"]
        if self.args.kubeconfig:
            full_cmd.extend(["--kubeconfig", self.args.kubeconfig])
        full_cmd.extend(cmd)

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout, result.stderr, result.returncode
        except FileNotFoundError:
            return "", i18n("error_kubectl", self.lang), 127
        except subprocess.TimeoutExpired:
            return "", i18n("error_connection", self.lang), 124

    def check_operator_detection(self) -> CheckResult:
        """Check 1: Operator CRD exists."""
        _, _, rc = self.run_kubectl(
            "get", "crd", "prometheusrules.monitoring.coreos.com"
        )
        status = "pass" if rc == 0 else "fail"
        detail = i18n("operator_found", self.lang) if rc == 0 else "not found"
        return CheckResult(i18n("check_operator", self.lang), status, detail)

    def check_prometheus_rule_status(self) -> CheckResult:
        """Check 2: PrometheusRule resources loaded."""
        stdout, stderr, rc = self.run_kubectl(
            "get", "prometheusrule", "-n", self.args.namespace,
            "-l", "app.kubernetes.io/part-of=dynamic-alerting", "-o", "json",
        )
        try:
            loaded = len(json.loads(stdout).get("items", [])) if rc == 0 else 0
        except (json.JSONDecodeError, ValueError):
            loaded = 0

        expected = len(list(
            Path(self.args.rule_packs_dir).glob("rule-pack-*.yaml")
        )) if os.path.isdir(self.args.rule_packs_dir) else 0

        status = "pass" if loaded == expected > 0 else ("warn" if loaded > 0 else "fail")
        detail = f"{loaded}/{expected}" if expected > 0 else f"{loaded}"
        return CheckResult(i18n("check_rules", self.lang), status, detail)

    def check_servicemonitor_status(self) -> CheckResult:
        """Check 3: ServiceMonitor for threshold-exporter."""
        stdout, _, rc = self.run_kubectl(
            "get", "servicemonitor", "-n", self.args.namespace,
            "-l", "app.kubernetes.io/part-of=dynamic-alerting", "-o", "json",
        )
        try:
            found = len(json.loads(stdout).get("items", [])) > 0 if rc == 0 else False
        except (json.JSONDecodeError, ValueError):
            found = False
        status = "pass" if found else "fail"
        detail = i18n("monitor_found", self.lang) if found else i18n("monitor_missing", self.lang)
        return CheckResult(i18n("check_monitor", self.lang), status, detail)

    def check_alertmanager_config(self) -> CheckResult:
        """Check 4: AlertmanagerConfig resources."""
        stdout, _, rc = self.run_kubectl(
            "get", "alertmanagerconfig", "-n", self.args.namespace,
            "-l", "app.kubernetes.io/part-of=dynamic-alerting", "-o", "json",
        )
        try:
            count = len(json.loads(stdout).get("items", [])) if rc == 0 else 0
        except (json.JSONDecodeError, ValueError):
            count = 0
        status = "pass" if count > 0 else "warn"
        detail = f"{count} {i18n('alertconfig_found', self.lang)}" if count > 0 else "no tenants"
        return CheckResult(i18n("check_alertconfig", self.lang), status, detail)

    def check_target_health(self) -> CheckResult:
        """Check 5: Prometheus target health (optional)."""
        if not self.args.prometheus:
            return CheckResult(i18n("check_targets", self.lang), "skip", i18n("targets_skip", self.lang))

        data, err = http_get_json(f"{self.args.prometheus}/api/v1/targets?state=active", timeout=10)
        if err:
            return CheckResult(i18n("check_targets", self.lang), "warn", f"{i18n('error_api', self.lang)}: {err[:60]}")

        try:
            targets = data.get("data", {}).get("activeTargets", [])
            exporter_targets = [t for t in targets if any(
                kw in t.get("labels", {}).get("job", "").lower() for kw in ["threshold", "exporter"]
            )]
        except (KeyError, TypeError):
            exporter_targets = []

        if not exporter_targets:
            return CheckResult(i18n("check_targets", self.lang), "warn", "no targets found")

        unhealthy = [t for t in exporter_targets if t.get("health", "").lower() != "up"]
        status = "fail" if unhealthy else "pass"
        detail = i18n("targets_healthy", self.lang) if not unhealthy else i18n("targets_unhealthy", self.lang)
        return CheckResult(i18n("check_targets", self.lang), status, f"{len(exporter_targets)} {detail}")

    def run_all_checks(self) -> None:
        """Run all checks sequentially."""
        self.checks.append(self.check_operator_detection())
        self.checks.append(self.check_prometheus_rule_status())
        self.checks.append(self.check_servicemonitor_status())
        self.checks.append(self.check_alertmanager_config())
        self.checks.append(self.check_target_health())

    def print_human_report(self) -> None:
        """Print human-readable report."""
        title = i18n("title", self.lang)
        print(f"\n╔══════════════════════════════════════════════════╗")
        print(f"║  {title:<48} ║")
        print(f"╠══════════════════════════════════════════════════╣")

        for check in self.checks:
            status_str = {
                "pass": "✓",
                "warn": "⚠",
                "fail": "✗",
                "skip": "—",
            }.get(check.status, "?")

            status_label = {
                "pass": i18n("pass", self.lang),
                "warn": i18n("warn", self.lang),
                "fail": i18n("fail", self.lang),
                "skip": i18n("skip", self.lang),
            }.get(check.status, "?")

            line = f"║ {status_str} {status_label:<6} {check.name:<22} "
            if check.detail:
                line += f"({check.detail})"
            # Pad to 50 chars
            line = line.ljust(50) + "║"
            print(line)

        print(f"╠══════════════════════════════════════════════════╣")

        # Summary
        pass_count = sum(1 for c in self.checks if c.status == "pass")
        warn_count = sum(1 for c in self.checks if c.status == "warn")
        fail_count = sum(1 for c in self.checks if c.status == "fail")

        summary = (
            f"{i18n('result', self.lang)}: {pass_count} {i18n('pass', self.lang)}, "
            f"{warn_count} {i18n('warn', self.lang)}, {fail_count} {i18n('fail', self.lang)}"
        )
        line = f"║ {summary:<48} ║"
        print(line)
        print(f"╚══════════════════════════════════════════════════╝\n")

    def print_json_report(self) -> None:
        """Print JSON report."""
        pass_count = sum(1 for c in self.checks if c.status == "pass")
        warn_count = sum(1 for c in self.checks if c.status == "warn")
        fail_count = sum(1 for c in self.checks if c.status == "fail")

        report = {
            "checks": [c.to_dict() for c in self.checks],
            "summary": {
                "pass": pass_count,
                "warn": warn_count,
                "fail": fail_count,
                "total": len(self.checks),
            },
        }
        print(json.dumps(report, indent=2))

    def exit_code(self) -> int:
        """Return exit code based on checks."""
        if not self.args.ci:
            return 0
        fail_count = sum(1 for c in self.checks if c.status == "fail")
        return 1 if fail_count > 0 else 0


def main():
    parser = argparse.ArgumentParser(
        description="Verify Prometheus Operator CRD deployment status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--namespace",
        default="monitoring",
        help="Kubernetes namespace (default: monitoring)",
    )
    parser.add_argument(
        "--rule-packs-dir",
        default="rule-packs/",
        help="Directory with rule pack YAML files (default: rule-packs/)",
    )
    parser.add_argument(
        "--config-dir",
        default="conf.d/",
        help="Directory with tenant config files (default: conf.d/)",
    )
    parser.add_argument(
        "--prometheus",
        help="Prometheus API URL for target health check (optional)",
    )
    parser.add_argument(
        "--kubeconfig",
        help="Path to kubeconfig file (optional)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit non-zero if any check fails",
    )

    args = parser.parse_args()

    checker = OperatorChecker(args)
    checker.run_all_checks()

    if args.json:
        checker.print_json_report()
    else:
        checker.print_human_report()

    sys.exit(checker.exit_code())


if __name__ == "__main__":
    main()
