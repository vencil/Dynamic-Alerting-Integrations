#!/usr/bin/env python3
"""
maintenance_scheduler.py — Evaluate recurring maintenance schedules and create
Alertmanager silences.

Reads tenant configs with _state_maintenance.recurring schedules, evaluates
which maintenance windows are currently active, and creates/extends
Alertmanager silences accordingly.

Designed to run as a K8s CronJob every 5 minutes.

Usage:
  maintenance-scheduler --config-dir conf.d/ --alertmanager http://alertmanager:9093
  maintenance-scheduler --config-dir conf.d/ --dry-run
"""
import argparse
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import load_yaml_file  # noqa: E402

# Creator label for idempotency checks
SILENCE_CREATOR = "da-tools/maintenance-scheduler"

# Pre-compiled regex for Go-style duration parsing (e.g. "1d", "4h", "2h30m", "1h15m30s")
_DURATION_RE = re.compile(r"(\d+)(d|h|m|s)")


def load_recurring_schedules(config_dir):
    """Load all tenant recurring maintenance schedules from conf.d/.

    Returns {tenant: [{"cron": ..., "duration": ..., "reason": ...}]}.
    """
    if not os.path.isdir(config_dir):
        print(f"ERROR: config directory not found: {config_dir}", file=sys.stderr)
        return {}

    schedules = {}

    for fname in sorted(os.listdir(config_dir)):
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        if fname.startswith("_") or fname.startswith("."):
            continue

        path = os.path.join(config_dir, fname)
        raw = load_yaml_file(path, default={})

        tenants = raw.get("tenants", {})
        if not isinstance(tenants, dict):
            continue

        for tenant, overrides in tenants.items():
            if not isinstance(overrides, dict):
                continue

            maint = overrides.get("_state_maintenance")
            if not isinstance(maint, dict):
                continue

            recurring = maint.get("recurring")
            if not isinstance(recurring, list) or not recurring:
                continue

            valid = []
            for entry in recurring:
                if not isinstance(entry, dict):
                    continue
                cron = entry.get("cron", "").strip()
                duration = entry.get("duration", "").strip()
                if not cron or not duration:
                    print(f"  WARN: {tenant}: recurring entry missing cron/duration, skipping",
                          file=sys.stderr)
                    continue
                valid.append({
                    "cron": cron,
                    "duration": duration,
                    "reason": entry.get("reason", "Recurring maintenance"),
                })

            if valid:
                schedules[tenant] = valid

    return schedules


def parse_duration(duration_str):
    """Parse a Go-style duration string to timedelta.

    Supports: "1d", "4h", "30m", "2h30m", "1d12h", "1h15m30s".
    """
    total_seconds = 0
    for match in _DURATION_RE.finditer(duration_str):
        val = int(match.group(1))
        unit = match.group(2)
        if unit == "d":
            total_seconds += val * 86400
        elif unit == "h":
            total_seconds += val * 3600
        elif unit == "m":
            total_seconds += val * 60
        elif unit == "s":
            total_seconds += val
    if total_seconds == 0:
        # Fallback: try pure integer as minutes
        try:
            total_seconds = int(duration_str) * 60
        except ValueError:
            return None
    return timedelta(seconds=total_seconds)


def is_in_window(cron_expr, duration_str, now=None):
    """Check if 'now' falls within a maintenance window defined by cron + duration.

    Returns (in_window, window_start, window_end) or (False, None, None).

    Requires 'croniter' library for cron evaluation.
    """
    try:
        from croniter import croniter
    except ImportError:
        print("ERROR: 'croniter' library required. Install with: pip install croniter",
              file=sys.stderr)
        sys.exit(2)

    if now is None:
        now = datetime.now(timezone.utc)

    duration = parse_duration(duration_str)
    if duration is None:
        return False, None, None

    # Find the most recent cron trigger before now
    cron = croniter(cron_expr, now)
    prev_trigger = cron.get_prev(datetime)
    if prev_trigger.tzinfo is None:
        prev_trigger = prev_trigger.replace(tzinfo=timezone.utc)

    window_end = prev_trigger + duration

    if now <= window_end:
        return True, prev_trigger, window_end

    return False, None, None


def get_existing_silences(alertmanager_url):
    """Get active silences created by this tool from Alertmanager.

    Returns {(tenant, reason): {"id": ..., "endsAt": datetime}}.
    """
    url = f"{alertmanager_url}/api/v2/silences"
    try:
        data = _api_request(url, method="GET")
    except Exception as e:
        print(f"  WARN: failed to fetch silences: {e}", file=sys.stderr)
        return {}

    result = {}
    for silence in data:
        if silence.get("status", {}).get("state") != "active":
            continue
        if silence.get("createdBy") != SILENCE_CREATOR:
            continue

        tenant = None
        for matcher in silence.get("matchers", []):
            if matcher.get("name") == "tenant":
                tenant = matcher.get("value")
                break

        if tenant:
            comment = silence.get("comment", "")
            ends_at_str = silence.get("endsAt", "")
            ends_at = _parse_iso(ends_at_str)
            result[(tenant, comment)] = {
                "id": silence.get("id"),
                "endsAt": ends_at,
            }

    return result


def _parse_iso(iso_str):
    """Parse an ISO 8601 datetime string to timezone-aware datetime.

    Returns None if parsing fails.
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def create_silence(alertmanager_url, tenant, reason, ends_at, dry_run=False):
    """Create an Alertmanager silence for a tenant maintenance window.

    Returns silence ID on success.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "matchers": [
            {"name": "tenant", "value": tenant, "isRegex": False},
        ],
        "startsAt": now.isoformat(),
        "endsAt": ends_at.isoformat(),
        "createdBy": SILENCE_CREATOR,
        "comment": reason,
    }

    if dry_run:
        print(f"  DRY-RUN: would create silence for {tenant} until {ends_at.isoformat()}")
        return None

    url = f"{alertmanager_url}/api/v2/silences"
    try:
        result = _api_request(url, method="POST", payload=payload)
        silence_id = result.get("silenceID", "unknown")
        print(f"  Created silence {silence_id} for {tenant} until {ends_at.isoformat()}")
        return silence_id
    except Exception as e:
        print(f"  ERROR: failed to create silence for {tenant}: {e}", file=sys.stderr)
        return None


def extend_silence(alertmanager_url, silence_id, tenant, reason, ends_at,
                   dry_run=False):
    """Extend an existing Alertmanager silence to a new endsAt time.

    Uses POST with the existing silence ID to update it (Alertmanager v2 API
    treats POST with an ID as an update).  Returns silence ID on success.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "id": silence_id,
        "matchers": [
            {"name": "tenant", "value": tenant, "isRegex": False},
        ],
        "startsAt": now.isoformat(),
        "endsAt": ends_at.isoformat(),
        "createdBy": SILENCE_CREATOR,
        "comment": reason,
    }

    if dry_run:
        print(f"  DRY-RUN: would extend silence {silence_id} for {tenant} "
              f"until {ends_at.isoformat()}")
        return silence_id

    url = f"{alertmanager_url}/api/v2/silences"
    try:
        result = _api_request(url, method="POST", payload=payload)
        new_id = result.get("silenceID", silence_id)
        print(f"  Extended silence {new_id} for {tenant} until {ends_at.isoformat()}")
        return new_id
    except Exception as e:
        print(f"  ERROR: failed to extend silence {silence_id} for {tenant}: {e}",
              file=sys.stderr)
        return None


def _api_request(url, method="GET", payload=None, max_retries=3):
    """Make HTTP request to Alertmanager API with exponential backoff retry.

    Only retries on 5xx and connection errors; 4xx are not retried.
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("Content-Type", "application/json")

            data = None
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")

            with urllib.request.urlopen(req, data=data, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}

        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise  # 4xx: don't retry
            last_error = e
        except (urllib.error.URLError, OSError) as e:
            last_error = e

        if attempt < max_retries - 1:
            wait = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait)

    raise last_error


def evaluate_and_apply(config_dir, alertmanager_url, dry_run=False, now=None):
    """Main logic: evaluate schedules and create/extend/skip silences.

    Returns (created, skipped, errors) counts.  "created" includes both
    new silences and extended silences.
    """
    schedules = load_recurring_schedules(config_dir)
    if not schedules:
        print("No recurring maintenance schedules found.")
        return 0, 0, 0

    print(f"Found {sum(len(v) for v in schedules.values())} recurring schedule(s) "
          f"across {len(schedules)} tenant(s)")

    # Get existing silences for idempotency / extend checks
    existing = {}
    if alertmanager_url and not dry_run:
        existing = get_existing_silences(alertmanager_url)

    created = 0
    skipped = 0
    errors = 0

    for tenant, entries in sorted(schedules.items()):
        for entry in entries:
            in_window, start, end = is_in_window(entry["cron"], entry["duration"], now=now)

            if not in_window:
                continue

            reason = entry["reason"]

            # Idempotency: check if silence already exists
            if (tenant, reason) in existing:
                info = existing[(tenant, reason)]
                existing_end = info.get("endsAt")

                # Self-healing: extend if existing silence expires before window end
                if existing_end is not None and existing_end < end:
                    sid = extend_silence(
                        alertmanager_url, info["id"], tenant, reason, end,
                        dry_run=dry_run)
                    if sid is not None:
                        created += 1
                    else:
                        errors += 1
                else:
                    print(f"  SKIP: {tenant} — silence already active for '{reason}'")
                    skipped += 1
                continue

            # Create silence
            if alertmanager_url:
                sid = create_silence(alertmanager_url, tenant, reason, end, dry_run=dry_run)
                if sid is not None or dry_run:
                    created += 1
                else:
                    errors += 1
            else:
                print(f"  ACTIVE: {tenant} — {reason} (window {start} → {end})")
                created += 1

    return created, skipped, errors


def push_metrics(pushgateway_url, created, skipped, errors, duration_s):
    """Push run metrics to Prometheus Pushgateway.

    Metrics pushed (job="maintenance-scheduler"):
      - maintenance_scheduler_last_run_timestamp_seconds  (gauge)
      - maintenance_scheduler_silences_created              (gauge per run)
      - maintenance_scheduler_silences_skipped              (gauge per run)
      - maintenance_scheduler_errors                        (gauge per run)
      - maintenance_scheduler_run_duration_seconds          (gauge)
    """
    now_ts = time.time()
    lines = [
        "# TYPE maintenance_scheduler_last_run_timestamp_seconds gauge",
        f"maintenance_scheduler_last_run_timestamp_seconds {now_ts:.3f}",
        "# TYPE maintenance_scheduler_silences_created gauge",
        f"maintenance_scheduler_silences_created {created}",
        "# TYPE maintenance_scheduler_silences_skipped gauge",
        f"maintenance_scheduler_silences_skipped {skipped}",
        "# TYPE maintenance_scheduler_errors gauge",
        f"maintenance_scheduler_errors {errors}",
        "# TYPE maintenance_scheduler_run_duration_seconds gauge",
        f"maintenance_scheduler_run_duration_seconds {duration_s:.3f}",
    ]
    body = "\n".join(lines) + "\n"

    url = f"{pushgateway_url}/metrics/job/maintenance-scheduler"
    try:
        req = urllib.request.Request(url, method="POST")
        req.add_header("Content-Type", "text/plain")
        data = body.encode("utf-8")
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            resp.read()
        print(f"  Pushed metrics to {pushgateway_url}")
    except Exception as e:
        # Non-fatal: observability failure should not fail the CronJob
        print(f"  WARN: failed to push metrics to Pushgateway: {e}",
              file=sys.stderr)


def build_parser():
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Evaluate recurring maintenance schedules and create Alertmanager silences",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --config-dir conf.d/ --alertmanager http://alertmanager:9093
              %(prog)s --config-dir conf.d/ --dry-run
              %(prog)s --config-dir conf.d/  # report-only (no --alertmanager)
              %(prog)s --config-dir conf.d/ --alertmanager http://am:9093 --pushgateway http://pushgateway:9091
        """),
    )
    parser.add_argument("--config-dir", required=True,
                        help="Tenant config directory (conf.d/)")
    parser.add_argument("--alertmanager",
                        help="Alertmanager base URL (e.g., http://alertmanager:9093)")
    parser.add_argument("--pushgateway",
                        help="Pushgateway base URL for observability metrics "
                             "(e.g., http://pushgateway:9091)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without creating silences")
    parser.add_argument("--json-output", action="store_true",
                        help="Output results as JSON")
    return parser


def main():
    """Entry point.

    Exit codes:
      0 — success (silences created or none needed)
      1 — errors occurred
      2 — fatal error (bad args, missing deps)
    """
    parser = build_parser()
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config directory not found: {args.config_dir}", file=sys.stderr)
        sys.exit(2)

    t0 = time.monotonic()

    created, skipped, errors = evaluate_and_apply(
        args.config_dir,
        args.alertmanager,
        dry_run=args.dry_run,
    )

    duration_s = time.monotonic() - t0

    if args.json_output:
        print(json.dumps({
            "created": created,
            "skipped": skipped,
            "errors": errors,
        }))

    print(f"\nSummary: {created} created, {skipped} skipped, {errors} errors")

    # Push observability metrics (non-fatal on failure)
    if args.pushgateway and not args.dry_run:
        push_metrics(args.pushgateway, created, skipped, errors, duration_s)

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
