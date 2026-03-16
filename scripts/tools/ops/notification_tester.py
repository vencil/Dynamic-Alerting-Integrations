#!/usr/bin/env python3
"""notification_tester.py — Multi-channel notification connectivity testing.

Extracts all configured receivers from tenant YAML _routing sections,
sends test messages to each receiver, and reports connectivity status.

Usage:
  # Test all receivers for all tenants in config directory
  python3 notification_tester.py --config-dir ./conf.d/

  # Test a specific tenant
  python3 notification_tester.py --config-dir ./conf.d/ --tenant db-a

  # Dry-run: validate URLs and config without sending
  python3 notification_tester.py --config-dir ./conf.d/ --dry-run

  # JSON output for CI integration
  python3 notification_tester.py --config-dir ./conf.d/ --json

  # CI gate: exit 1 if any receiver fails
  python3 notification_tester.py --config-dir ./conf.d/ --ci

用法:
  # 測試配置目錄中所有租戶的接收器
  python3 notification_tester.py --config-dir ./conf.d/

  # 測試特定租戶
  python3 notification_tester.py --config-dir ./conf.d/ --tenant db-a

  # 乾跑模式：僅驗證 URL 和配置，不實際發送
  python3 notification_tester.py --config-dir ./conf.d/ --dry-run

  # JSON 輸出（CI 整合用）
  python3 notification_tester.py --config-dir ./conf.d/ --json

  # CI 閘門：任一接收器失敗則 exit 1
  python3 notification_tester.py --config-dir ./conf.d/ --ci
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    RECEIVER_TYPES,
    RECEIVER_URL_FIELDS,
    detect_cli_lang,
    load_tenant_configs,
)

_LANG = detect_cli_lang()

_HELP = {
    'description': {
        'zh': '多通道通知連通性測試 — 驗證所有已配置 receiver 的可達性',
        'en': 'Multi-channel notification connectivity testing',
    },
    'config_dir': {
        'zh': '租戶配置目錄路徑（conf.d/）',
        'en': 'Path to tenant config directory (conf.d/)',
    },
    'tenant': {
        'zh': '只測試指定租戶（省略則測試全部）',
        'en': 'Test only this tenant (omit to test all)',
    },
    'dry_run': {
        'zh': '乾跑模式：僅驗證 URL 格式，不實際發送',
        'en': 'Dry-run: validate URL format only, do not send',
    },
    'json_flag': {
        'zh': 'JSON 格式輸出',
        'en': 'Output as JSON',
    },
    'ci': {
        'zh': 'CI 模式：任一 receiver 失敗則 exit 1',
        'en': 'CI mode: exit 1 if any receiver fails',
    },
    'timeout': {
        'zh': '每個 receiver 的連線逾時秒數（預設 10）',
        'en': 'Connection timeout per receiver in seconds (default: 10)',
    },
    'rate_limit': {
        'zh': '每次測試之間的等待秒數（預設 0.5）',
        'en': 'Seconds to wait between each test (default: 0.5)',
    },
}


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_OK = "OK"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_AUTH_ERROR = "AUTH_ERROR"
STATUS_CONNECTION_REFUSED = "CONNECTION_REFUSED"
STATUS_INVALID_URL = "INVALID_URL"
STATUS_INVALID_CONFIG = "INVALID_CONFIG"
STATUS_HTTP_ERROR = "HTTP_ERROR"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class ReceiverTestResult:
    """Result of a single receiver connectivity test."""

    receiver_name: str
    receiver_type: str
    status: str
    latency_ms: int = 0
    detail: str = ""
    url_tested: str = ""


@dataclass
class TenantTestReport:
    """Aggregated test results for one tenant."""

    tenant: str
    receivers: list[ReceiverTestResult] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    skipped: int = 0


# ---------------------------------------------------------------------------
# Receiver extraction from tenant config
# ---------------------------------------------------------------------------
def extract_receivers(
    tenant_name: str,
    tenant_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract receiver definitions from a tenant config dict.

    Handles:
      - _routing.receiver (single receiver)
      - _routing.overrides[].receiver (per-rule overrides)

    Args:
        tenant_name: Tenant identifier (for labeling).
        tenant_config: Tenant config dict from YAML.

    Returns:
        List of dicts, each with 'name', 'type', and type-specific fields.
    """
    receivers: list[dict[str, Any]] = []
    routing = tenant_config.get("_routing")
    if not isinstance(routing, dict):
        return receivers

    # Main receiver
    main_recv = routing.get("receiver")
    if isinstance(main_recv, dict) and main_recv.get("type"):
        recv = dict(main_recv)
        recv.setdefault("_label", f"{tenant_name}-main")
        receivers.append(recv)

    # Override receivers
    overrides = routing.get("overrides")
    if isinstance(overrides, list):
        for idx, override in enumerate(overrides):
            if not isinstance(override, dict):
                continue
            recv = override.get("receiver")
            if isinstance(recv, dict) and recv.get("type"):
                r = dict(recv)
                r.setdefault("_label", f"{tenant_name}-override-{idx}")
                receivers.append(r)

    return receivers


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def validate_receiver_url(
    receiver: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Validate the URL field of a receiver.

    Args:
        receiver: Receiver dict with 'type' and type-specific fields.

    Returns:
        (url, error) — url string on success, error message on failure.
    """
    rtype = receiver.get("type", "").strip().lower()
    if rtype not in RECEIVER_URL_FIELDS:
        return None, f"unknown receiver type '{rtype}'"

    url_fields = RECEIVER_URL_FIELDS.get(rtype, [])
    if not url_fields:
        # Types like pagerduty have no URL to validate
        return None, None

    for url_field in url_fields:
        url = receiver.get(url_field)
        if not url:
            return None, f"missing required field '{url_field}'"

        # Email smarthost is host:port, not a URL
        if rtype == "email":
            if ":" not in str(url):
                return None, f"smarthost should be host:port format, got '{url}'"
            return str(url), None

        # Validate URL scheme
        parsed = urllib.parse.urlparse(str(url))
        if parsed.scheme not in ("http", "https"):
            return None, f"invalid URL scheme '{parsed.scheme}' (expected http/https)"
        if not parsed.hostname:
            return None, f"missing hostname in URL '{url}'"
        return str(url), None

    return None, None


# ---------------------------------------------------------------------------
# Test payload builders
# ---------------------------------------------------------------------------
_TEST_ALERT_PAYLOAD = {
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {
                "alertname": "DynamicAlertingNotificationTest",
                "severity": "info",
                "tenant": "test",
            },
            "annotations": {
                "summary": "Notification connectivity test — this alert can be safely ignored.",
                "summary_zh": "通知連通性測試 — 此告警可安全忽略。",
            },
            "startsAt": "2024-01-01T00:00:00Z",
            "endsAt": "2024-01-01T00:00:01Z",
        }
    ],
    "version": "4",
    "groupKey": "test-notification-connectivity",
}


def _build_webhook_payload() -> bytes:
    """Build Alertmanager-compatible webhook test payload."""
    return json.dumps(_TEST_ALERT_PAYLOAD).encode("utf-8")


def _build_slack_payload() -> bytes:
    """Build Slack incoming webhook test payload."""
    payload = {
        "text": ":white_check_mark: *Dynamic Alerting — Notification Test*\n"
                "This is a connectivity test message. Safe to ignore.",
    }
    return json.dumps(payload).encode("utf-8")


def _build_teams_payload() -> bytes:
    """Build Microsoft Teams webhook test payload (Adaptive Card)."""
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "Dynamic Alerting — Notification Test",
                            "weight": "bolder",
                            "size": "medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": "Connectivity test. Safe to ignore.",
                            "wrap": True,
                        },
                    ],
                },
            }
        ],
    }
    return json.dumps(payload).encode("utf-8")


def _build_pagerduty_payload(service_key: str) -> bytes:
    """Build PagerDuty Events API v2 test payload (trigger + immediate resolve)."""
    payload = {
        "routing_key": service_key,
        "event_action": "trigger",
        "dedup_key": "da-notification-test",
        "payload": {
            "summary": "Dynamic Alerting — Notification connectivity test",
            "severity": "info",
            "source": "da-tools test-notification",
        },
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------
def test_receiver(
    receiver: dict[str, Any],
    *,
    timeout: int = 10,
    dry_run: bool = False,
) -> ReceiverTestResult:
    """Test connectivity to a single receiver.

    Args:
        receiver: Receiver dict with 'type' and type-specific fields.
        timeout: HTTP timeout in seconds.
        dry_run: If True, only validate URL without sending.

    Returns:
        ReceiverTestResult with status and timing.
    """
    label = receiver.get("_label", "unknown")
    rtype = receiver.get("type", "").strip().lower()

    # Validate receiver type
    if rtype not in RECEIVER_TYPES:
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_INVALID_CONFIG,
            detail=f"unknown receiver type '{rtype}'",
        )

    # Validate required fields
    spec = RECEIVER_TYPES[rtype]
    for req_field in spec["required"]:
        if not receiver.get(req_field):
            return ReceiverTestResult(
                receiver_name=label,
                receiver_type=rtype,
                status=STATUS_INVALID_CONFIG,
                detail=f"missing required field '{req_field}'",
            )

    # Validate URL
    url, url_err = validate_receiver_url(receiver)
    if url_err:
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_INVALID_URL,
            detail=url_err,
        )

    # Dry-run: URL is valid, stop here
    if dry_run:
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_DRY_RUN,
            detail="URL format valid (dry-run, no request sent)",
            url_tested=url or "",
        )

    # Build request
    target_url, payload = _build_test_request(receiver, rtype, url)
    if target_url is None:
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_SKIPPED,
            detail=payload or "no testable URL",  # payload carries skip reason
        )

    # Send request
    return _send_test_request(label, rtype, target_url, payload, timeout)


def _build_test_request(
    receiver: dict[str, Any],
    rtype: str,
    url: Optional[str],
) -> tuple[Optional[str], Any]:
    """Build target URL and payload for a receiver test.

    Returns:
        (target_url, payload_bytes) on success.
        (None, skip_reason_str) if not testable.
    """
    if rtype == "webhook" or rtype == "rocketchat":
        return url, _build_webhook_payload()

    if rtype == "slack":
        return url, _build_slack_payload()

    if rtype == "teams":
        return url, _build_teams_payload()

    if rtype == "pagerduty":
        service_key = receiver.get("routing_key") or receiver.get("service_key", "")
        return "https://events.pagerduty.com/v2/enqueue", _build_pagerduty_payload(service_key)

    if rtype == "email":
        # Email: SMTP handshake test (EHLO only, no actual send)
        return None, "email SMTP test requires --send-test-email (not implemented in dry probe)"

    return None, f"no test implementation for receiver type '{rtype}'"


def _send_test_request(
    label: str,
    rtype: str,
    target_url: str,
    payload: bytes,
    timeout: int,
) -> ReceiverTestResult:
    """Send HTTP POST and return test result.

    Args:
        label: Human-readable receiver label.
        rtype: Receiver type string.
        target_url: URL to POST to.
        payload: Request body bytes.
        timeout: HTTP timeout in seconds.

    Returns:
        ReceiverTestResult with status and latency.
    """
    # SSRF 防護：僅允許 http/https scheme
    parsed = urllib.parse.urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_INVALID_URL,
            detail=f"unsupported URL scheme: {parsed.scheme}",
        )

    start = time.monotonic()
    try:
        req = urllib.request.Request(  # nosec B310
            target_url,
            data=payload,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            resp.read()
            latency = int((time.monotonic() - start) * 1000)
            return ReceiverTestResult(
                receiver_name=label,
                receiver_type=rtype,
                status=STATUS_OK,
                latency_ms=latency,
                detail=f"HTTP {resp.status}",
                url_tested=target_url,
            )

    except urllib.error.HTTPError as exc:
        latency = int((time.monotonic() - start) * 1000)
        status = STATUS_AUTH_ERROR if exc.code in (401, 403) else STATUS_HTTP_ERROR
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=status,
            latency_ms=latency,
            detail=f"HTTP {exc.code}: {exc.reason}",
            url_tested=target_url,
        )

    except urllib.error.URLError as exc:
        latency = int((time.monotonic() - start) * 1000)
        reason = str(exc.reason) if exc.reason else str(exc)
        if "timed out" in reason.lower():
            status = STATUS_TIMEOUT
        elif "refused" in reason.lower():
            status = STATUS_CONNECTION_REFUSED
        else:
            status = STATUS_CONNECTION_REFUSED
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=status,
            latency_ms=latency,
            detail=reason[:80],
            url_tested=target_url,
        )

    except OSError as exc:
        latency = int((time.monotonic() - start) * 1000)
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_CONNECTION_REFUSED,
            latency_ms=latency,
            detail=str(exc)[:80],
            url_tested=target_url,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def test_tenant_receivers(
    tenant_name: str,
    tenant_config: dict[str, Any],
    *,
    timeout: int = 10,
    dry_run: bool = False,
    rate_limit: float = 0.5,
) -> TenantTestReport:
    """Test all receivers for a tenant.

    Args:
        tenant_name: Tenant identifier.
        tenant_config: Tenant config dict from YAML.
        timeout: HTTP timeout per receiver.
        dry_run: Validate URLs only, don't send.
        rate_limit: Seconds between requests (rate limiting).

    Returns:
        TenantTestReport with per-receiver results.
    """
    report = TenantTestReport(tenant=tenant_name)
    receivers = extract_receivers(tenant_name, tenant_config)

    if not receivers:
        return report

    for idx, recv in enumerate(receivers):
        result = test_receiver(recv, timeout=timeout, dry_run=dry_run)
        report.receivers.append(result)

        if result.status == STATUS_OK or result.status == STATUS_DRY_RUN:
            report.passed += 1
        elif result.status == STATUS_SKIPPED:
            report.skipped += 1
        else:
            report.failed += 1

        # Rate limiting between requests
        if rate_limit > 0 and idx < len(receivers) - 1:
            time.sleep(rate_limit)

    return report


def run_all_tests(
    config_dir: str,
    *,
    tenant_filter: Optional[str] = None,
    timeout: int = 10,
    dry_run: bool = False,
    rate_limit: float = 0.5,
) -> list[TenantTestReport]:
    """Run notification tests for all (or filtered) tenants.

    Args:
        config_dir: Path to tenant config directory.
        tenant_filter: If set, only test this tenant.
        timeout: HTTP timeout per receiver.
        dry_run: Validate URLs only.
        rate_limit: Seconds between requests.

    Returns:
        List of TenantTestReport, one per tenant with receivers.
    """
    all_configs = load_tenant_configs(config_dir)

    if tenant_filter:
        if tenant_filter not in all_configs:
            return []
        all_configs = {tenant_filter: all_configs[tenant_filter]}

    reports: list[TenantTestReport] = []
    for tenant_name in sorted(all_configs):
        report = test_tenant_receivers(
            tenant_name,
            all_configs[tenant_name],
            timeout=timeout,
            dry_run=dry_run,
            rate_limit=rate_limit,
        )
        # Only include tenants that have receivers configured
        if report.receivers:
            reports.append(report)

    return reports


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
_STATUS_SYMBOLS = {
    STATUS_OK: "✓",
    STATUS_DRY_RUN: "◎",
    STATUS_SKIPPED: "⊘",
    STATUS_TIMEOUT: "⏱",
    STATUS_AUTH_ERROR: "🔒",
    STATUS_CONNECTION_REFUSED: "✗",
    STATUS_INVALID_URL: "✗",
    STATUS_INVALID_CONFIG: "✗",
    STATUS_HTTP_ERROR: "✗",
}


def format_text_report(reports: list[TenantTestReport]) -> str:
    """Format reports as human-readable text table.

    Args:
        reports: List of TenantTestReport.

    Returns:
        Formatted string for terminal output.
    """
    if not reports:
        msg = "未發現已配置 receiver 的租戶。" if _LANG == 'zh' else "No tenants with configured receivers found."
        return msg

    lines: list[str] = []

    for report in reports:
        lines.append(f"\nTenant: {report.tenant}")
        lines.append(f"{'─' * 72}")
        header = f"  {'Receiver':<25s} {'Type':<12s} {'Status':<20s} {'Latency':<10s}"
        lines.append(header)
        lines.append(f"  {'─' * 25} {'─' * 12} {'─' * 20} {'─' * 10}")

        for r in report.receivers:
            symbol = _STATUS_SYMBOLS.get(r.status, "?")
            latency = f"{r.latency_ms}ms" if r.latency_ms > 0 else "—"
            status_str = f"{symbol} {r.status}"
            lines.append(
                f"  {r.receiver_name:<25s} {r.receiver_type:<12s} {status_str:<20s} {latency:<10s}"
            )
            if r.detail and r.status not in (STATUS_OK, STATUS_DRY_RUN):
                lines.append(f"    └─ {r.detail}")

        total = len(report.receivers)
        lines.append(f"  ({report.passed} passed, {report.failed} failed, {report.skipped} skipped / {total} total)")

    # Overall summary
    total_passed = sum(r.passed for r in reports)
    total_failed = sum(r.failed for r in reports)
    total_skipped = sum(r.skipped for r in reports)
    lines.append(f"\n{'=' * 72}")
    overall = "FAIL" if total_failed > 0 else "PASS"
    lines.append(f"  Overall: {overall} ({total_passed} passed, {total_failed} failed, {total_skipped} skipped)")
    lines.append(f"{'=' * 72}")

    return "\n".join(lines)


def format_json_report(reports: list[TenantTestReport]) -> str:
    """Format reports as JSON string.

    Args:
        reports: List of TenantTestReport.

    Returns:
        JSON string with tool metadata and all results.
    """
    has_failure = any(r.failed > 0 for r in reports)
    output = {
        "tool": "test-notification",
        "status": "fail" if has_failure else "pass",
        "tenants": [asdict(r) for r in reports],
        "summary": {
            "total_receivers": sum(len(r.receivers) for r in reports),
            "passed": sum(r.passed for r in reports),
            "failed": sum(r.failed for r in reports),
            "skipped": sum(r.skipped for r in reports),
        },
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """CLI entry point: multi-channel notification connectivity testing."""
    parser = argparse.ArgumentParser(
        description=_HELP['description'][_LANG],
    )
    parser.add_argument(
        "--config-dir",
        required=True,
        help=_HELP['config_dir'][_LANG],
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help=_HELP['tenant'][_LANG],
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=_HELP['dry_run'][_LANG],
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help=_HELP['json_flag'][_LANG],
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help=_HELP['ci'][_LANG],
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help=_HELP['timeout'][_LANG],
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=0.5,
        help=_HELP['rate_limit'][_LANG],
    )
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        msg = f"配置目錄不存在: {args.config_dir}" if _LANG == 'zh' else f"Config directory not found: {args.config_dir}"
        print(msg, file=sys.stderr)
        sys.exit(1)

    reports = run_all_tests(
        args.config_dir,
        tenant_filter=args.tenant,
        timeout=args.timeout,
        dry_run=args.dry_run,
        rate_limit=args.rate_limit,
    )

    if args.json_output:
        print(format_json_report(reports))
    else:
        print(format_text_report(reports))

    has_failure = any(r.failed > 0 for r in reports)
    if args.ci and has_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
