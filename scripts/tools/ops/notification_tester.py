#!/usr/bin/env python3
"""notification_tester.py — Multi-channel notification connectivity testing.

Takes every tenant's receivers from what the route generator
(generate_alertmanager_routes) renders for its `_routing` as the generator
resolves it — `_routing_defaults` chain, routing profile, platform `tenants:`
overlay, subdirectory tenant files — sends test messages to each rendered
receiver, and reports connectivity status. Per-tenant receivers only: the
platform-enforced (`_routing_enforced`, NOC) receiver the generator also
renders is not tested. A conf.d tree the route generator refuses is refused here too (rc 2).

#3076: the receiver list is the generator's own per-slot verdict
(`tenant_slot_verdicts`: the main receiver, overrides[i], routes[i]), not a
walk of `_routing` of our own. A slot the generator does not render is never
sent and counts as failed: INVALID_URL / INVALID_CONFIG when its own receiver
is refused (build_receiver_config) — INVALID_URL when the URL field (or email
smarthost) is given but malformed, else INVALID_CONFIG, a missing, empty or
null URL field included — and NOT_RENDERED, with the generator's reason, when
the entry is dropped at the routing level (an override with no alertname /
metric_group, routes[i] with an unsupported key, or any child of a tenant
whose main receiver is not rendered). On top of that the port must be
1-65535. Hostname encoding (IDNA) is checked only when a request is sent, so
--dry-run does not cover it. The --policy domain allowlist is not applied (this tool takes no --policy).

Usage:
  # Test all receivers for all tenants in config directory
  python3 notification_tester.py --config-dir ./conf.d/

  # Test a specific tenant
  python3 notification_tester.py --config-dir ./conf.d/ --tenant db-a

  # Dry-run: the generator's verdict and the port range, without sending
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

  # 乾跑模式：依路由產生器判定並檢查 port 範圍，不實際發送
  python3 notification_tester.py --config-dir ./conf.d/ --dry-run

  # JSON 輸出（CI 整合用）
  python3 notification_tester.py --config-dir ./conf.d/ --json

  # CI 閘門：任一接收器失敗則 exit 1
  python3 notification_tester.py --config-dir ./conf.d/ --ci
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    RECEIVER_URL_FIELDS,
    detect_cli_lang,
    receiver_field_state,
    receiver_required_problem,
    FIELD_SET,
    FIELD_UNSET,
    exit_on_yaml_file_error,
    non_negative_seconds,
    positive_seconds,
)
# Aliased: the local format_json_report() below (domain report builder,
# exercised directly by tests) delegates its final dump to the shared helper.
from _lib_python import format_json_report as _dump_json  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _grar_merge import receiver_type  # noqa: E402  (#2839 the one type read)
from _grar_merge import build_receiver_config  # noqa: E402  (#3048 the generator's verdict)
from _grar_merge import Finding  # noqa: E402  (#3076 a slot verdict's reason)
from _lib_exitcodes import (  # noqa: E402
    EXIT_VIOLATION, EXIT_CALLER_ERROR, arm_crash_envelope, disarm_crash_envelope,
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
        'zh': '乾跑模式：依路由產生器的判定檢查 receiver 配置與 URL 格式、port 須在 1–65535，不實際發送（主機名編碼只在送出時檢查）',
        'en': 'Dry-run: check receiver config and URL format by the route generator\'s verdict and the port range 1-65535, do not send (hostname encoding is checked only when a request is sent)',
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
        'zh': '每個 receiver 的逾時秒數，連線與等待回應都適用（> 0，可帶小數；預設 10）',
        'en': 'Timeout per receiver in seconds, for connecting and for the response (> 0, fractions allowed; default: 10)',
    },
    'rate_limit': {
        'zh': '每次測試之間的等待秒數（≥ 0，0 = 不等待，可帶小數；預設 0.5）',
        'en': 'Seconds to wait between each test (>= 0, 0 = no wait, fractions allowed; default: 0.5)',
    },
}


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_OK = "OK"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_AUTH_ERROR = "AUTH_ERROR"
STATUS_CONNECTION_REFUSED = "CONNECTION_REFUSED"
STATUS_CONNECTION_ERROR = "CONNECTION_ERROR"
STATUS_TLS_ERROR = "TLS_ERROR"
STATUS_INVALID_URL = "INVALID_URL"
STATUS_INVALID_CONFIG = "INVALID_CONFIG"
STATUS_HTTP_ERROR = "HTTP_ERROR"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_SKIPPED = "SKIPPED"
# #3076: the route generator does not render this slot (routing-level skip);
# never sent, counted as failed.
STATUS_NOT_RENDERED = "NOT_RENDERED"


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
# Receiver slots: the route generator's per-slot verdict (#3076)
# ---------------------------------------------------------------------------
def _slot_label(tenant_name: str, slot: str) -> str:
    """The report label of a generator slot: ``<tenant>-main`` /
    ``-override-<i>`` / ``-route-<i>`` (``overrides`` / ``routes`` when that
    value is not a list). The index is the authored one, as in the
    generator's receiver name."""
    if slot == "receiver":
        return f"{tenant_name}-main"
    field_name, _, idx = slot.partition("[")
    kind = {"overrides": "override", "routes": "route"}.get(field_name, field_name)
    return f"{tenant_name}-{kind}-{idx.rstrip(']')}" if idx else f"{tenant_name}-{field_name}"


def slot_verdicts(tenant_name: str, routing: dict[str, Any]) -> list[Any]:
    """The route generator's verdict for every receiver slot of *routing*
    (``_grar_routes.tenant_slot_verdicts``): what it renders, and why it
    drops the rest. No domain allowlist (this tool has no --policy)."""
    import generate_alertmanager_routes as gar  # the generator's own verdict

    return gar.tenant_slot_verdicts(tenant_name, routing)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def _non_string_type_problem(receiver: dict[str, Any]) -> Optional[str]:
    """The error for a receiver whose ``type`` is set but is not a string.

    #2839: ``receiver_type()`` (the route generator's one read, #2780) reads
    such a type as ``""``; a mapping or list used to crash the ``in`` checks
    below with ``TypeError: unhashable type``. Named here instead of as
    ``unknown receiver type ''`` so the operator sees what is wrong.
    """
    raw = receiver.get("type")
    if raw is None or isinstance(raw, str):
        return None
    return f"receiver type must be a string, got {type(raw).__name__}"


def validate_receiver_url(
    receiver: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Validate the URL field of a receiver.

    Args:
        receiver: Receiver dict with 'type' and type-specific fields.

    Returns:
        (url, error) — url string on success, error message on failure.
    """
    type_err = _non_string_type_problem(receiver)
    if type_err:
        return None, type_err
    # Exact match, no case folding or trimming (#2180): the route generator,
    # the schema (`const`) and the Go guard all reject `Webhook` / ` webhook`.
    rtype = receiver_type(receiver)
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

        # #3048: the format is the route generator's rule for this field (the
        # schema pattern), in the generator's words — not a check of our own.
        problem = receiver_required_problem(rtype, receiver, url_field)
        if problem:
            return None, f"receiver type '{rtype}' {problem}"
        port_err = _port_problem(rtype, str(url))
        if port_err:
            return None, port_err
        return str(url), None

    return None, None


def _port_problem(rtype: str, url: str) -> Optional[str]:
    """The error for a port outside 1-65535 in *url* (an email smarthost is
    ``host:port``), else None.

    #3048: the schema pattern takes any digits; ``parsed.port`` raises
    ValueError for one above 65535, and glibc's ``getaddrinfo`` folds such a
    port modulo 65536, so a request to ``:99999`` went to ``:34463``.
    """
    try:
        parts = urllib.parse.urlsplit("//" + url if rtype == "email" else url)
    except ValueError as exc:  # e.g. "Invalid IPv6 URL"
        return f"invalid URL '{url}': {exc}"
    try:
        port = parts.port
    except ValueError:
        port = 0
    if port == 0:
        return f"port must be from 1 to 65535, got '{url}'"
    return None


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
    timeout: float = 10,
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
    # Exact match, no case folding or trimming (#2180): the route generator,
    # the schema (`const`) and the Go guard all reject `Webhook` / ` webhook`.
    rtype = receiver_type(receiver)

    # Validate receiver type
    type_err = _non_string_type_problem(receiver)
    if type_err:
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_INVALID_CONFIG,
            detail=type_err,
        )
    # #3048: whether the receiver's own config is usable is the route
    # generator's verdict (build_receiver_config) — the one every receiver it
    # renders goes through. A receiver it refuses there is not tested (or
    # passed in dry-run). run_all_tests only hands in receivers the generator
    # rendered (#3076), so this matters for a receiver handed in directly.
    refusal = _generator_refusal(receiver, label, rtype)
    if refusal is not None:
        status, detail = refusal
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=status,
            detail=detail,
        )

    # Validate URL (the generator accepted it; the port range is on top)
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
            detail=("format accepted by the route generator; hostname encoding "
                    "is checked only when a request is sent (dry-run, no request sent)"),
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


def _generator_refusal(
    receiver: dict[str, Any],
    label: str,
    rtype: str,
) -> Optional[tuple[str, str]]:
    """(status, detail) when ``build_receiver_config`` refuses *receiver*,
    else None. For a receiver handed in directly; ``run_all_tests`` takes the
    same verdict from the generator's slot verdicts instead (#3076).

    The detail is the generator's own line without its framing (``WARN:
    <label>:`` and ``, skipping``). A URL field (email: smarthost) that is
    given but malformed or not a string is INVALID_URL, as before; one that is
    missing, empty or null is INVALID_CONFIG, as before, and so is every other
    refusal. ``_label`` is the tester's key, not the receiver's, so the
    generator is given a copy without it.
    """
    receiver_obj = {k: v for k, v in receiver.items() if k != "_label"}
    am_config, warnings = build_receiver_config(receiver_obj, label)
    if am_config is not None:
        return None
    return _refusal_status(receiver, rtype, warnings[0] if warnings else None)


def _generator_detail(finding: object) -> str:
    """The generator's line without its framing (``WARN: <where>:`` and
    ``, skipping``)."""
    detail = str(finding).strip()
    if detail.startswith("WARN: "):
        head, sep, rest = detail[len("WARN: "):].partition(": ")
        detail = rest if sep and " " not in head else detail[len("WARN: "):]
    if detail.endswith(", skipping"):
        detail = detail[:-len(", skipping")]
    return detail


def _refusal_status(
    receiver: dict[str, Any],
    rtype: str,
    finding: Optional[object],
    field_prefix: str = "",
) -> tuple[str, str]:
    """(status, detail) for a receiver the generator refused with *finding*
    (the first refusal line; *field_prefix* is the slot path its ``field``
    starts with, e.g. ``overrides[0].``)."""
    if finding is None:
        return STATUS_INVALID_CONFIG, "the route generator refuses this receiver"
    detail = _generator_detail(finding)
    url_fields = {f"{field_prefix}receiver.{f}": f
                  for f in RECEIVER_URL_FIELDS.get(rtype, [])}
    refused_field = url_fields.get(getattr(finding, "field", None))
    # The generator files "" and null as invalid_receiver_field, but they are
    # "not given" (FIELD_UNSET) like a missing key: INVALID_CONFIG, as before.
    if (getattr(finding, "kind", None) == "invalid_receiver_field"
            and refused_field is not None
            and receiver_field_state(receiver, refused_field) != FIELD_UNSET):
        return STATUS_INVALID_URL, detail
    return STATUS_INVALID_CONFIG, detail


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
        # test_receiver has already enforced "exactly one, and a string" via
        # build_receiver_config (receiver_exactly_one_problem); pick the key
        # by the same rule so the one sent is the one that counts as given.
        key_field = next(f for f in ("routing_key", "service_key")
                         if receiver_field_state(receiver, f) == FIELD_SET)
        return ("https://events.pagerduty.com/v2/enqueue",
                _build_pagerduty_payload(receiver[key_field]))

    if rtype == "email":
        # Email: SMTP handshake test (EHLO only, no actual send)
        return None, "email SMTP test requires --send-test-email (not implemented in dry probe)"

    return None, f"no test implementation for receiver type '{rtype}'"


# http.client raises OSError with this message when an HTTPS proxy refuses the
# CONNECT; DNS failures and refused connections behind the proxy all look like
# this, so what failed is the proxy hop, not necessarily the receiver.
_PROXY_TUNNEL_PREFIX = "Tunnel connection failed"


def _classify_connection_error(cause: object, message: str) -> tuple[str, str]:
    """Map a non-timeout connection error to (status, detail).

    Classified by the exception type of ``cause``, never by its message (the
    proxy prefix is the one exception: http.client gives it no own type).
    ``cause`` is the bare OSError, or ``URLError.reason``, which may be a plain
    string; a string reason is never a refused connection (urllib always
    passes the ConnectionRefusedError object), so it is CONNECTION_ERROR.
    """
    if message.startswith(_PROXY_TUNNEL_PREFIX):
        return STATUS_CONNECTION_ERROR, ("via proxy: " + message)[:80]
    if isinstance(cause, ConnectionRefusedError):
        return STATUS_CONNECTION_REFUSED, message[:80]
    if isinstance(cause, ssl.SSLError):  # includes SSLCertVerificationError
        return STATUS_TLS_ERROR, message[:80]
    # DNS (gaierror), reset, RemoteDisconnected, unreachable, ...
    return STATUS_CONNECTION_ERROR, message[:80]


def _send_test_request(
    label: str,
    rtype: str,
    target_url: str,
    payload: bytes,
    timeout: float,
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
        # The proxy hop first: a CONNECT answered "504 Connection Timed Out"
        # is the proxy's verdict, not this receiver timing out (#2995).
        # Then type: an empty-message TimeoutError gives "" (#2965).
        if reason.startswith(_PROXY_TUNNEL_PREFIX):
            status, detail = _classify_connection_error(exc.reason, reason)
        elif isinstance(exc.reason, TimeoutError) or "timed out" in reason.lower():
            status, detail = STATUS_TIMEOUT, (reason or "timed out")[:80]
        else:
            status, detail = _classify_connection_error(exc.reason, reason)
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=status,
            latency_ms=latency,
            detail=detail,
            url_tested=target_url,
        )

    except TimeoutError as exc:
        # urllib wraps only connect/TLS-handshake errors in URLError; a timeout
        # while waiting for the response (getresponse/read) surfaces as a bare
        # TimeoutError (socket.timeout is its alias since 3.10). It must come
        # before `except OSError`, of which it is a subclass (#2965).
        latency = int((time.monotonic() - start) * 1000)
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_TIMEOUT,
            latency_ms=latency,
            detail=(str(exc) or "timed out")[:80],
            url_tested=target_url,
        )

    except OSError as exc:
        # Errors urllib does not wrap in URLError: a reset or a close without
        # reply (RemoteDisconnected) after the request went out.
        latency = int((time.monotonic() - start) * 1000)
        status, detail = _classify_connection_error(exc, str(exc))
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=status,
            latency_ms=latency,
            detail=detail,
            url_tested=target_url,
        )

    except http.client.InvalidURL as exc:
        # Also an HTTPException, but raised before anything is sent: the URL
        # itself is bad (non-numeric port, control characters), not a reply.
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_INVALID_URL,
            detail=f"invalid URL: {exc}"[:80],
            url_tested=target_url,
        )

    except http.client.HTTPException as exc:
        # The receiver answered, but not with a well-formed HTTP response
        # (BadStatusLine, IncompleteRead, ...). Not an OSError, so nothing above
        # caught it and the whole run crashed (#2995). After `except OSError`
        # on purpose: RemoteDisconnected is both, and stays a connection error.
        latency = int((time.monotonic() - start) * 1000)
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_HTTP_ERROR,
            latency_ms=latency,
            detail=f"malformed response: {type(exc).__name__}: {exc}"[:80],
            url_tested=target_url,
        )

    except UnicodeError as exc:
        # #3048: raised before anything is sent — a hostname IDNA cannot
        # encode (a label over 63 characters, an empty label), which urllib
        # does not wrap in URLError. Not caught by any branch above (it is a
        # ValueError, neither an OSError nor an HTTPException), so it crashed
        # the whole run. A plain ValueError is not caught here on purpose:
        # the one a receiver URL can raise (urlsplit's bracketed-host check)
        # is already refused by _port_problem before any send.
        return ReceiverTestResult(
            receiver_name=label,
            receiver_type=rtype,
            status=STATUS_INVALID_URL,
            detail=f"invalid URL: {exc}"[:80],
            url_tested=target_url,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def test_tenant_receivers(
    tenant_name: str,
    tenant_config: dict[str, Any],
    *,
    timeout: float = 10,
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
    routing = tenant_config.get("_routing")
    if not isinstance(routing, dict):
        return report

    # #3076: the generator's verdict per slot, in render order (main,
    # overrides[i], routes[i]); only what it renders is tested.
    verdicts = slot_verdicts(tenant_name, routing)
    left_to_send = sum(1 for v in verdicts if v.rendered)
    for verdict in verdicts:
        label = _slot_label(tenant_name, verdict.slot)
        if verdict.rendered:
            recv = dict(verdict.receiver)
            recv["_label"] = label
            result = test_receiver(recv, timeout=timeout, dry_run=dry_run)
            left_to_send -= 1
        else:
            result = _unrendered_result(verdict, label)
        report.receivers.append(result)

        if result.status == STATUS_OK or result.status == STATUS_DRY_RUN:
            report.passed += 1
        elif result.status == STATUS_SKIPPED:
            report.skipped += 1
        else:
            report.failed += 1

        # Rate limiting between requests
        if verdict.rendered and rate_limit > 0 and left_to_send > 0:
            time.sleep(rate_limit)

    return report


def _unrendered_result(verdict: Any, label: str) -> ReceiverTestResult:
    """The result for a slot the generator does not render (#3076): never
    sent. Its own receiver refused → INVALID_URL / INVALID_CONFIG, as for a
    receiver handed in directly; dropped at the routing level → NOT_RENDERED
    with the generator's reason."""
    recv = verdict.receiver if isinstance(verdict.receiver, dict) else {}
    rtype = receiver_type(recv)
    reason: Optional[Finding] = verdict.reason
    if verdict.stage == "receiver":
        type_err = _non_string_type_problem(recv)
        if type_err:
            status, detail = STATUS_INVALID_CONFIG, type_err
        else:
            prefix = "" if verdict.slot == "receiver" else f"{verdict.slot}."
            status, detail = _refusal_status(recv, rtype, reason, prefix)
    else:
        status = STATUS_NOT_RENDERED
        detail = (_generator_detail(reason) if reason is not None
                  else "the route generator does not render this entry")
    return ReceiverTestResult(receiver_name=label, receiver_type=rtype,
                              status=status, detail=detail)


class RoutingTreeRefused(Exception):
    """The route generator refuses this conf.d tree (an unreadable tenant
    file, a tenant in two files, a routing-tree error, an invalid tenant id),
    so there is no resolved `_routing` to take receivers from. ``lines`` are
    the generator's own words."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        super().__init__(self.lines[0] if self.lines else
                         "the route generator refuses this tree")


def resolved_routing(config_dir: str) -> dict[str, dict]:
    """``{tenant: routing}`` exactly as the route generator resolves it (#2752,
    #2115 ruling (c): `_routing` is decided by the route generator).

    The generator's own reader (``load_tenant_tree``): the `_routing_defaults`
    chain, the routing profile, platform `tenants:` overlays and tenant files
    in subdirectories, merged, `{{tenant}}` substituted. A tenant with nothing
    to route is absent. Raises ``RoutingTreeRefused`` when the generator's
    ``tree_refusal`` refuses the tree — it refuses it in every mode, so no
    receiver list read from it describes what Alertmanager would run.
    """
    import generate_alertmanager_routes as gar  # the generator's own reader

    tree = gar.load_tenant_tree(config_dir)
    gar.print_reader_warnings(tree)  # #2906: what the reader used to print itself
    _rc, lines = gar.tree_refusal(tree.files_read, tree.tenant_file_errors,
                                  tree.duplicate_tenants,
                                  tree.routing_tree_problems,
                                  tree.invalid_tenant_ids,
                                  tree.field_type_errors)
    if lines:
        raise RoutingTreeRefused(lines)
    return tree.routing_configs


def run_all_tests(
    config_dir: str,
    *,
    tenant_filter: Optional[str] = None,
    timeout: float = 10,
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

    Raises:
        RoutingTreeRefused: the route generator refuses the tree.
    """
    all_configs = {tenant: {"_routing": routing}
                   for tenant, routing in resolved_routing(config_dir).items()}

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
    STATUS_CONNECTION_ERROR: "✗",
    STATUS_TLS_ERROR: "✗",
    STATUS_INVALID_URL: "✗",
    STATUS_INVALID_CONFIG: "✗",
    STATUS_HTTP_ERROR: "✗",
    STATUS_NOT_RENDERED: "✗",
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
        lines.append(f"\nTenant: {safe_label(report.tenant)}")
        lines.append(f"{'─' * 72}")
        header = f"  {'Receiver':<25s} {'Type':<12s} {'Status':<20s} {'Latency':<10s}"
        lines.append(header)
        lines.append(f"  {'─' * 25} {'─' * 12} {'─' * 20} {'─' * 10}")

        for r in report.receivers:
            symbol = _STATUS_SYMBOLS.get(r.status, "?")
            latency = f"{r.latency_ms}ms" if r.latency_ms > 0 else "—"
            status_str = f"{symbol} {r.status}"
            lines.append(
                f"  {safe_label(r.receiver_name):<25s} "
                f"{safe_label(r.receiver_type):<12s} {status_str:<20s} {latency:<10s}"
            )
            if r.detail and r.status not in (STATUS_OK, STATUS_DRY_RUN):
                lines.append(f"    └─ {safe_label(r.detail)}")

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
    return _dump_json(output)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
@exit_on_yaml_file_error  # #1654: unreadable tenant file → rc 2, named
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
        type=positive_seconds,
        default=10,
        help=_HELP['timeout'][_LANG],
    )
    parser.add_argument(
        "--rate-limit",
        type=non_negative_seconds,
        default=0.5,
        help=_HELP['rate_limit'][_LANG],
    )
    args = parser.parse_args()
    if args.json_output:
        # #3094: a crash in --json mode still prints one envelope, with the
        # keys of the routing_tree_refused document below
        arm_crash_envelope(tool="test-notification", tenants=[],
                           summary={"total_receivers": 0, "passed": 0,
                                    "failed": 0, "skipped": 0})

    if not Path(args.config_dir).is_dir():
        msg = f"配置目錄不存在: {args.config_dir}" if _LANG == 'zh' else f"Config directory not found: {args.config_dir}"
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    try:
        reports = run_all_tests(
            args.config_dir,
            tenant_filter=args.tenant,
            timeout=args.timeout,
            dry_run=args.dry_run,
            rate_limit=args.rate_limit,
        )
    except RoutingTreeRefused as exc:
        # #2752: not "no receivers" — the tree cannot be read the way the
        # route generator reads it, so nothing was tested (rc 2).
        print("ERROR: the route generator refuses this tree, so no receiver "
              "can be resolved — nothing was tested:", file=sys.stderr)
        for line in exc.lines:
            print(safe_label(line), file=sys.stderr)
        if args.json_output:
            print(_dump_json({
                "tool": "test-notification",
                "status": "caller_error",
                "reason": "routing_tree_refused",
                "tenants": [],
                "summary": {"total_receivers": 0, "passed": 0,
                            "failed": 0, "skipped": 0},
            }))
        sys.exit(EXIT_CALLER_ERROR)

    if args.json_output:
        print(format_json_report(reports))
        disarm_crash_envelope()  # the document is out; a late crash must not add a second
    else:
        print(format_text_report(reports))

    has_failure = any(r.failed > 0 for r in reports)
    if args.ci and has_failure:
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
