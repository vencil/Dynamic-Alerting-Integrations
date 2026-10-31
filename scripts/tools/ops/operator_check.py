#!/usr/bin/env python3
"""Verify Prometheus Operator CRD deployment status.

Usage:
    da-tools operator-check [--namespace NS] [--prometheus URL] [--json] [--ci]

Checks: (1) Operator CRD, (2) PrometheusRule count, (3) ServiceMonitor,
(4) AlertmanagerConfig, (5) Target Health (opt)

Every cluster read goes through ``_lib_kubectl`` (``kubectl_get`` /
``kubectl_list``, #3207): a
read kubectl does not answer (cluster unreachable, forbidden, kubectl
missing or timed out) is a caller error — the row is marked so and the
exit code is 2 — never "not found". Whether a CRD is installed is a `get`
of that CRD by name (an answer: absent is a finding, not a failed read; it
needs only `get` on customresourcedefinitions).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import detect_cli_lang  # noqa: E402
from _lib_paths import require_dir, supplied  # noqa: E402  (#3392)
from _lib_prometheus import QueryError, prometheus_targets  # noqa: E402  (#3133)
from _lib_kubectl import KubectlError, kubectl_get, kubectl_list  # noqa: E402  (#3207)
import _lib_consumer_read  # noqa: E402  (#3426: rule_pack_files, the one declaration)
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report, arm_crash_envelope)

# The CRDs of the kinds the checks list (#3207): whether each is installed
# is a `kubectl_get` of the CRD by name — an answer, unlike a `kubectl get`
# of a kind the server does not have, which exits 1 like an unreachable
# cluster does.
_CRD_RULES = "prometheusrules.monitoring.coreos.com"
_CRD_MONITORS = "servicemonitors.monitoring.coreos.com"
_CRD_AM_CONFIGS = "alertmanagerconfigs.monitoring.coreos.com"
_PART_OF = "app.kubernetes.io/part-of=dynamic-alerting"


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
    def __init__(self, name: str, status: str, detail: str = "",
                 caller_error: bool = False):
        self.name = name
        self.status = status  # "pass", "warn", "fail", "skip"
        self.detail = detail
        # #452/#737, #3207: True when a "fail" was caused by a read that got
        # no answer (kubectl: KubectlError; Prometheus: QueryError), not a
        # finding.
        self.caller_error = caller_error

    def to_dict(self) -> dict:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "caller_error": self.caller_error,
        }


class OperatorChecker:
    """Prometheus Operator checker."""
    def __init__(self, args):
        self.args = args
        self.lang = detect_cli_lang()
        self.checks: list[CheckResult] = []
        self._crds: dict[str, bool | KubectlError] = {}

    def _list(self, kind: str, **kw) -> list[dict]:
        return kubectl_list(kind, kubeconfig=self.args.kubeconfig, **kw)

    def _installed(self, crd: str) -> bool:
        """Whether *crd* is installed, read once per CRD; KubectlError when
        kubectl does not answer (raised again, not re-asked)."""
        if crd not in self._crds:
            try:
                self._crds[crd] = kubectl_get("crd", crd, kubeconfig=self.args.kubeconfig) is not None
            except KubectlError as exc:
                self._crds[crd] = exc
        got = self._crds[crd]
        if isinstance(got, KubectlError):
            raise got
        return got

    def _unanswered(self, key: str, exc: KubectlError) -> CheckResult:
        return CheckResult(i18n(key, self.lang), "fail", str(exc), caller_error=True)

    def check_operator_detection(self) -> CheckResult:
        """Check 1: Operator CRD exists."""
        try:
            found = self._installed(_CRD_RULES)
        except KubectlError as exc:
            return self._unanswered("check_operator", exc)
        return CheckResult(i18n("check_operator", self.lang), "pass" if found else "fail",
                           i18n("operator_found", self.lang) if found else f"CRD {_CRD_RULES} not installed")

    def check_prometheus_rule_status(self) -> CheckResult:
        """Check 2: PrometheusRule resources loaded."""
        try:
            installed = self._installed(_CRD_RULES)
            loaded = len(self._list("prometheusrule", namespace=self.args.namespace,
                                    selector=_PART_OF)) if installed else 0
        except KubectlError as exc:
            return self._unanswered("check_rules", exc)

        # #3426: the one declaration of which files are rule packs; one that
        # does not cover the directory is no expected count.
        try:
            expected = len(_lib_consumer_read.rule_pack_files(self.args.rule_packs_dir)) \
                if Path(self.args.rule_packs_dir).is_dir() else 0
        except _lib_consumer_read.ConsumerReadError as exc:
            return CheckResult(i18n("check_rules", self.lang), "fail",
                               _lib_consumer_read.error_text(exc), caller_error=True)

        status = "pass" if loaded == expected > 0 else ("warn" if loaded > 0 else "fail")
        detail = f"{loaded}/{expected}" if expected > 0 else f"{loaded}"
        if not installed:
            detail += f" (CRD {_CRD_RULES} not installed)"
        return CheckResult(i18n("check_rules", self.lang), status, detail)

    def check_servicemonitor_status(self) -> CheckResult:
        """Check 3: ServiceMonitor for threshold-exporter."""
        try:
            installed = self._installed(_CRD_MONITORS)
            found = installed and bool(self._list("servicemonitor", namespace=self.args.namespace,
                                                  selector=_PART_OF))
        except KubectlError as exc:
            return self._unanswered("check_monitor", exc)
        detail = i18n("monitor_found", self.lang) if found else i18n("monitor_missing", self.lang)
        if not installed:
            detail += f" (CRD {_CRD_MONITORS} not installed)"
        return CheckResult(i18n("check_monitor", self.lang), "pass" if found else "fail", detail)

    def check_alertmanager_config(self) -> CheckResult:
        """Check 4: AlertmanagerConfig resources."""
        try:
            installed = self._installed(_CRD_AM_CONFIGS)
            count = len(self._list("alertmanagerconfig", namespace=self.args.namespace,
                                   selector=_PART_OF)) if installed else 0
        except KubectlError as exc:
            return self._unanswered("check_alertconfig", exc)
        status = "pass" if count > 0 else "warn"
        detail = f"{count} {i18n('alertconfig_found', self.lang)}" if count > 0 else (
            "no tenants" if installed else f"CRD {_CRD_AM_CONFIGS} not installed")
        return CheckResult(i18n("check_alertconfig", self.lang), status, detail)

    def check_target_health(self) -> CheckResult:
        """Check 5: Prometheus target health (optional)."""
        if not self.args.prometheus:
            return CheckResult(i18n("check_targets", self.lang), "skip", i18n("targets_skip", self.lang))

        # #3133 (owner 2026-10-23): --prometheus is given, so targets that
        # cannot be read are a caller error (exit 2), not a warn; the lib
        # checks the answer's shape (a body that is not a target list used to
        # read as "no targets found").
        try:
            targets = prometheus_targets(self.args.prometheus, timeout=10)
        except QueryError as exc:
            return CheckResult(i18n("check_targets", self.lang), "fail",
                               f"{i18n('error_api', self.lang)}: {str(exc)[:60]}",
                               caller_error=True)
        exporter_targets = [t for t in targets if any(
            kw in t.get("labels", {}).get("job", "").lower() for kw in ["threshold", "exporter"]
        )]

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

    def json_document(self) -> dict:
        """The --json document (run() encodes it, #3216)."""
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
        return report

    def exit_code(self) -> int:
        """Return exit code based on checks.

        #452/#737, #3207: a read that got no answer (kubectl unreachable,
        forbidden, missing or timed out; Prometheus not answering) maps to
        EXIT_CALLER_ERROR (2) even outside --ci, since the tool genuinely
        could not do its job. Genuine "fail" findings only gate
        the run in --ci mode (EXIT_VIOLATION).
        """
        caller_error = any(
            c.status == "fail" and c.caller_error for c in self.checks
        )
        if caller_error:
            return EXIT_CALLER_ERROR
        if not self.args.ci:
            return EXIT_OK
        fail_count = sum(1 for c in self.checks if c.status == "fail")
        return EXIT_VIOLATION if fail_count > 0 else EXIT_OK


def main():
    try_utf8_stdout()
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
    if args.json:
        # #3094: a crash in --json mode still prints one envelope
        arm_crash_envelope()
    # #3392: a --rule-packs-dir the caller gave that is not there or not a
    # directory is a caller error, before the cluster is asked — it was an
    # expected count of 0 and a WARN that never named the path, rc 0. The
    # default nobody gave keeps that reading. An empty directory is still a
    # count of 0 (not this ticket).
    if supplied(args, parser, "rule_packs_dir"):
        require_dir(args.rule_packs_dir, lang=detect_cli_lang(), flag="--rule-packs-dir")

    checker = OperatorChecker(args)
    checker.run_all_checks()

    if args.json:
        return Report(checker.json_document(), checker.exit_code())  # #3216: run() encodes it
    checker.print_human_report()

    sys.exit(checker.exit_code())


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
