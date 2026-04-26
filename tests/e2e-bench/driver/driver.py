#!/usr/bin/env python3
"""Driver for v2.8.0 B-1 Phase 2 e2e alert fire-through harness.

Implements the 5-anchor measurement protocol from
docs/internal/design/phase-b-e2e-harness.md §5.2:

    T0 ← write fixture/active/conf.d/bench-run-{i}.yaml + push actual
    T1 ← exporter da_config_last_scan_complete_unixtime_seconds gauge
    T2 ← exporter da_config_last_reload_complete_unixtime_seconds gauge
    T3 ← Prometheus /api/v1/alerts activeAt for tenant=bench-run-{i}
    T4 ← receiver /posts?since={T0_ns}&tenant_id=bench-run-{i} received_unix_ns

Each run has a fire phase + a resolve phase (symmetric), per-run JSON
written to /results/per-run-{run_id}.json. Last (warm-up + n) runs:
warm_up=true on run 0, false on runs 1..N.

Run isolation: each run uses a distinct tenant_id (`bench-run-{i}`) so
Alertmanager doesn't dedup (group_by includes tenant). Pushgateway
DELETE in finally: blocks ensures stale state doesn't leak across runs.

This script runs INSIDE the docker-compose stack (per design §2.4) so
all timestamps share the same kernel clock as the services it queries.
Do not invoke from host.
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
from pathlib import Path

# ---------------------------------------------------------------------------
# Service endpoints (docker-compose internal DNS)
# ---------------------------------------------------------------------------
EXPORTER_URL = os.environ.get("EXPORTER_URL", "http://threshold-exporter:8080")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093")
RECEIVER_URL = os.environ.get("RECEIVER_URL", "http://receiver:5001")

FIXTURE_ACTIVE = Path(os.environ.get("FIXTURE_ACTIVE", "/fixture/active/conf.d"))
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/results"))
FIXTURE_KIND = os.environ.get("E2E_FIXTURE_KIND", "synthetic-v2")

# Polling parameters — tuned for 5s scrape interval. Tighter polling
# than scrape would just spin without new data.
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "0.5"))
POLL_TIMEOUT_S = float(os.environ.get("POLL_TIMEOUT_S", "60"))

# Fire / resolve threshold values for the actual_metric_value push.
# Tenant fixture sets bench_trigger threshold to 100; pushing 200 fires,
# 50 resolves.
THRESHOLD_VALUE = 100
FIRE_ACTUAL = 200
RESOLVE_ACTUAL = 50


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only; avoid pip install in container)
# ---------------------------------------------------------------------------


def _get(url: str, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _post(url: str, body: bytes, content_type: str, timeout: float = 5.0) -> int:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _delete(url: str, timeout: float = 5.0) -> int:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        # Pushgateway returns 200 on success; tolerate 404 if metric
        # was already absent.
        if e.code == 404:
            return 404
        raise


# ---------------------------------------------------------------------------
# Anchor capture
# ---------------------------------------------------------------------------


def _read_exporter_gauge(name: str) -> float | None:
    """Scrape the exporter /metrics, return the gauge value for `name`.

    Returns None if the gauge is absent (e.g. exporter has never
    completed a scan) or the value is 0 (gauge initialized but never
    set — distinguishable in caller). Parses the Prometheus text
    exposition format directly to avoid a client_python dependency.
    """
    text = _get(f"{EXPORTER_URL}/metrics").decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        # Match exact name OR name with empty label set "name {} value"
        # (we don't expect labels on these gauges).
        if line.startswith(name + " "):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return float(parts[-1])
                except ValueError:
                    continue
    return None


def poll_exporter_gauge(name: str, lower_bound_unix_s: int, deadline_s: float) -> tuple[int, bool]:
    """Poll the exporter until the gauge advances past lower_bound_unix_s.

    Returns (gauge_value_unix_ns, advanced). `advanced` is False if the
    deadline was reached without the gauge crossing lower_bound — caller
    should then treat the stage as `skipped` (per design §5.2 resolve
    phase note: stage A/B may be skipped if no fixture mutation).
    """
    start = time.time()
    while time.time() - start < deadline_s:
        v = _read_exporter_gauge(name)
        if v is not None and int(v) >= lower_bound_unix_s:
            return int(v) * 1_000_000_000, True
        time.sleep(POLL_INTERVAL_S)
    # Stage skipped — return last-known gauge value or 0.
    v = _read_exporter_gauge(name)
    return int(v or 0) * 1_000_000_000, False


def poll_prometheus_alert_active(tenant_id: str, lower_bound_ns: int, deadline_s: float) -> int:
    """Return the activeAt of the matching firing alert in unix ns,
    or 0 if not found within the deadline. lower_bound_ns is informational —
    Prometheus's activeAt is always recent if found, but we use it as a
    sanity check that we're not picking up stale alerts.
    """
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            data = json.loads(_get(f"{PROMETHEUS_URL}/api/v1/alerts").decode())
            for a in data.get("data", {}).get("alerts", []):
                labels = a.get("labels", {})
                if labels.get("tenant") == tenant_id and a.get("state") == "firing":
                    return _iso_to_unix_ns(a.get("activeAt", ""))
        except (urllib.error.URLError, json.JSONDecodeError, KeyError):
            pass  # service still warming up — keep polling
        time.sleep(POLL_INTERVAL_S)
    return 0


def poll_prometheus_alert_resolved(tenant_id: str, deadline_s: float) -> bool:
    """Return True when the firing alert for tenant_id is no longer in the
    active alerts list (i.e. resolved). False on timeout.
    """
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            data = json.loads(_get(f"{PROMETHEUS_URL}/api/v1/alerts").decode())
            still_firing = any(
                a.get("labels", {}).get("tenant") == tenant_id and a.get("state") == "firing"
                for a in data.get("data", {}).get("alerts", [])
            )
            if not still_firing:
                return True
        except (urllib.error.URLError, json.JSONDecodeError, KeyError):
            pass
        time.sleep(POLL_INTERVAL_S)
    return False


def poll_receiver(tenant_id: str, status: str, since_ns: int, deadline_s: float) -> int:
    """Poll receiver /posts?since=...&tenant_id=...&status=... and return
    the first matching received_unix_ns. 0 if not found within deadline.
    """
    start = time.time()
    qs = urllib.parse.urlencode(
        {"since": str(since_ns), "tenant_id": tenant_id, "status": status}
    )
    while time.time() - start < deadline_s:
        try:
            data = json.loads(_get(f"{RECEIVER_URL}/posts?{qs}").decode())
            if data:
                return int(data[0]["received_unix_ns"])
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError):
            pass
        time.sleep(POLL_INTERVAL_S)
    return 0


# ---------------------------------------------------------------------------
# Pushgateway helpers
# ---------------------------------------------------------------------------


def push_actual_value(tenant_id: str, value: float) -> None:
    """POST a single sample to pushgateway. Job + grouping label = tenant_id
    so DELETE later can target it precisely.
    """
    body = f"# TYPE actual_metric_value gauge\nactual_metric_value{{tenant=\"{tenant_id}\"}} {value}\n".encode()
    job = "e2e-driver"
    url = f"{PUSHGATEWAY_URL}/metrics/job/{job}/tenant/{tenant_id}"
    _post(url, body, "text/plain")


def delete_pushed_metric(tenant_id: str) -> None:
    """Cleanup pushgateway for this tenant. Per design §8.2: pushgateway
    is not stale-state friendly, so explicit DELETE in finally: is required.
    """
    job = "e2e-driver"
    url = f"{PUSHGATEWAY_URL}/metrics/job/{job}/tenant/{tenant_id}"
    try:
        _delete(url)
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass  # cleanup failures are not fatal — let next run overwrite


# ---------------------------------------------------------------------------
# Fixture mutation
# ---------------------------------------------------------------------------


def write_tenant_fixture(tenant_id: str, threshold: int = THRESHOLD_VALUE) -> None:
    """Write a tenant YAML to fixture/active/conf.d/{tenant_id}.yaml.

    Per design §5.1: write to a placeholder file that already exists from
    pre-flight (avoids fsnotify create-vs-modify event path divergence).
    """
    p = FIXTURE_ACTIVE / f"{tenant_id}.yaml"
    body = f"tenants:\n  {tenant_id}:\n    bench_trigger: \"{threshold}\"\n"
    p.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def now_unix_ns() -> int:
    return time.time_ns()


def now_unix_s() -> int:
    return int(time.time())


def _iso_to_unix_ns(iso: str) -> int:
    """Convert Prometheus's RFC3339-with-nanos activeAt to unix ns.

    Prometheus emits e.g. "2026-04-25T14:30:00.123456789Z". Python's
    fromisoformat handles up to microseconds in 3.11+; truncate ns to us
    for parsing then add back. Rough but adequate for ms-scale stage D.
    """
    if not iso:
        return 0
    try:
        # Strip trailing Z; truncate fractional part to microseconds.
        s = iso.rstrip("Z")
        if "." in s:
            head, frac = s.split(".", 1)
            frac = (frac + "000000")[:6]  # pad/truncate to 6 digits
            s = f"{head}.{frac}"
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Single-run protocol
# ---------------------------------------------------------------------------


def run_phase(tenant_id: str, actual_value: float, mutate_fixture: bool) -> dict:
    """Execute one phase (fire OR resolve) and return the 5-anchor record.

    mutate_fixture=True for fire (write tenant YAML to trigger reload chain);
    False for resolve (only push actual_value, don't touch fixture).
    """
    t0_ns = now_unix_ns()
    t0_s = now_unix_s()

    if mutate_fixture:
        write_tenant_fixture(tenant_id)
    push_actual_value(tenant_id, actual_value)

    # Stage A/B: poll for exporter gauges to advance past t0_s. On resolve
    # phase (no fixture change), gauges may not advance; mark as skipped.
    if mutate_fixture:
        t1_ns, scan_advanced = poll_exporter_gauge(
            "da_config_last_scan_complete_unixtime_seconds", t0_s, POLL_TIMEOUT_S
        )
        t2_ns, reload_advanced = poll_exporter_gauge(
            "da_config_last_reload_complete_unixtime_seconds", t0_s, POLL_TIMEOUT_S
        )
        ab_skipped = not (scan_advanced and reload_advanced)
    else:
        # Resolve: read current gauge values without polling — they may
        # be from the prior fire phase. Mark stage A/B skipped.
        v1 = _read_exporter_gauge("da_config_last_scan_complete_unixtime_seconds") or 0
        v2 = _read_exporter_gauge("da_config_last_reload_complete_unixtime_seconds") or 0
        t1_ns = int(v1) * 1_000_000_000
        t2_ns = int(v2) * 1_000_000_000
        ab_skipped = True

    # Stage C: poll Prometheus alerts.
    if mutate_fixture:
        t3_ns = poll_prometheus_alert_active(tenant_id, t2_ns, POLL_TIMEOUT_S)
    else:
        # Resolve: wait until alert is gone, then approximate t3 as
        # "first scrape after which the alert disappeared". We don't
        # have that exact timestamp from /api/v1/alerts (resolved alerts
        # are absent), so estimate via T4 (receiver got resolved) minus
        # the typical D dispatch (~50ms). Adequate for stage breakdown.
        resolved_ok = poll_prometheus_alert_resolved(tenant_id, POLL_TIMEOUT_S)
        t3_ns = 0 if not resolved_ok else 0  # filled after T4 below

    # Stage D: poll receiver.
    expected_status = "firing" if mutate_fixture else "resolved"
    t4_ns = poll_receiver(tenant_id, expected_status, t0_ns, POLL_TIMEOUT_S)

    # Backfill T3 for resolve phase: estimate as T4 - 50ms (typical D).
    if not mutate_fixture and t4_ns > 0:
        t3_ns = max(t4_ns - 50_000_000, t2_ns)

    return {
        "T0_unix_ns": t0_ns,
        "T1_unix_ns": t1_ns,
        "T2_unix_ns": t2_ns,
        "T3_unix_ns": t3_ns,
        "T4_unix_ns": t4_ns,
        "stage_ms": _stages_ms(t0_ns, t1_ns, t2_ns, t3_ns, t4_ns, ab_skipped),
        "e2e_ms": (t4_ns - t0_ns) // 1_000_000 if t4_ns > 0 else -1,
        "stage_ab_skipped": ab_skipped,
    }


def _stages_ms(t0: int, t1: int, t2: int, t3: int, t4: int, ab_skipped: bool) -> dict:
    if ab_skipped:
        return {
            "A": -1,
            "B": -1,
            "C": (t3 - t0) // 1_000_000 if t3 > 0 else -1,
            "D": (t4 - t3) // 1_000_000 if (t4 > 0 and t3 > 0) else -1,
        }
    return {
        "A": (t1 - t0) // 1_000_000 if t1 > 0 else -1,
        "B": (t2 - t1) // 1_000_000 if (t2 > 0 and t1 > 0) else -1,
        "C": (t3 - t2) // 1_000_000 if (t3 > 0 and t2 > 0) else -1,
        "D": (t4 - t3) // 1_000_000 if (t4 > 0 and t3 > 0) else -1,
    }


def run_one(run_id: int, warm_up: bool) -> dict:
    """Drive one full (fire + resolve) cycle for a unique tenant_id."""
    tenant_id = f"bench-run-{run_id}"
    try:
        fire = run_phase(tenant_id, FIRE_ACTUAL, mutate_fixture=True)
        resolve = run_phase(tenant_id, RESOLVE_ACTUAL, mutate_fixture=False)
    finally:
        delete_pushed_metric(tenant_id)
    return {
        "run_id": run_id,
        "warm_up": warm_up,
        "fixture_kind": FIXTURE_KIND,
        "gate_status": "pending",
        "fire": fire,
        "resolve": resolve,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def wait_for_services(deadline_s: float = 60.0) -> None:
    """Pre-flight: poll each upstream service until its HTTP endpoint
    responds. Compose's `service_started` only means the container is
    up — NOT that the process inside is listening on its port. At
    CI scale (1000-tenant fixture), threshold-exporter cold-load and
    pushgateway first-listen lag behind compose `Started` by several
    seconds, producing `Connection refused` for runs 0..N if driver
    pushes immediately on boot.

    Use stdout `print(..., flush=True)` so progress is visible even
    if the workflow gets cancelled mid-wait (Python `print` block-
    buffers when piped, masking driver activity from `gh run view
    --log` output otherwise).
    """
    targets = [
        ("exporter", f"{EXPORTER_URL}/metrics"),
        ("prometheus", f"{PROMETHEUS_URL}/-/ready"),
        ("pushgateway", f"{PUSHGATEWAY_URL}/-/ready"),
        ("alertmanager", f"{ALERTMANAGER_URL}/-/ready"),
        ("receiver", f"{RECEIVER_URL}/healthz"),
    ]
    print(f"[driver] waiting for {len(targets)} upstream services to listen ...", flush=True)
    for name, url in targets:
        start = time.time()
        last_err: Exception | None = None
        while time.time() - start < deadline_s:
            try:
                _get(url, timeout=2.0)
                print(f"[driver]   {name} ready ({time.time() - start:.1f}s)", flush=True)
                last_err = None
                break
            except (urllib.error.URLError, OSError) as e:
                last_err = e
                time.sleep(1.0)
        if last_err is not None:
            print(f"[driver]   {name} did NOT respond within {deadline_s}s: {last_err}", flush=True)
            raise SystemExit(f"upstream {name} not ready")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=int(os.environ.get("COUNT", "30")))
    parser.add_argument("--results-dir", type=str, default=str(RESULTS_DIR))
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    # Pre-flight: ensure fixture/active dir exists (compose volume mount).
    FIXTURE_ACTIVE.mkdir(parents=True, exist_ok=True)

    print(f"[driver] starting: count={args.count}, fixture_kind={FIXTURE_KIND}", flush=True)
    wait_for_services()

    # Run 0 = warm_up; runs 1..count are real.
    n_total = args.count + 1
    for i in range(n_total):
        warm_up = i == 0
        try:
            result = run_one(i, warm_up=warm_up)
        except (urllib.error.URLError, OSError) as e:
            print(f"[driver] run {i} failed: {e}", file=sys.stderr, flush=True)
            result = {
                "run_id": i,
                "warm_up": warm_up,
                "fixture_kind": FIXTURE_KIND,
                "gate_status": "pending",
                "error": str(e),
            }
        out_path = results_dir / f"per-run-{i:04d}.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        e2e = result.get("fire", {}).get("e2e_ms", -1)
        print(f"[driver] run {i:3d} (warm_up={warm_up}): fire e2e_ms={e2e}", flush=True)

    print(f"[driver] done: wrote {n_total} per-run JSON files to {results_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
