#!/usr/bin/env python3
"""
run_chaos_soak.py — v2.8.0 readiness harness: compressed-time chaos soak runner.

Purpose
-------
Replaces the originally-planned 5-6 week wall-clock staging soak with a
~4-hour compressed-time run that touches the exporter's hot-reload path
~240 times while sampling /metrics every N seconds. The output is a
time-series CSV + summary text suitable for `render_soak_diff.py` to
render a before/after report.

Design rationale
----------------
- threshold-exporter doesn't expose pprof in production. Adding pprof is
  a separate hardening question (security/perf surface). This harness
  uses the existing /metrics endpoint instead, capturing the
  Prometheus-exposed proxies for "is anything leaking":
    * process_resident_memory_bytes  (RSS — heap + stack + everything)
    * process_open_fds
    * go_goroutines                  (goroutine leak detector)
    * go_memstats_alloc_bytes        (live heap allocations)
    * go_memstats_heap_inuse_bytes
    * go_memstats_heap_idle_bytes    (held but not in use)
    * go_gc_duration_seconds_count   (GC activity)
- Reload trigger uses the watched config dir: bumping any file's mtime
  forces threshold-exporter's SHA-256 diff to fire.
- Run is fully reproducible: same args + same starting config = same
  reload count; metrics drift is the only varying signal.
- stdlib only (no requests / pandas) — runs in dev container without
  pip installs.

Usage
-----
    python3 scripts/tools/dx/run_chaos_soak.py \\
        --target-url http://localhost:8080 \\
        --config-dir /path/to/conf.d \\
        --duration-min 240 \\
        --reload-interval-sec 60 \\
        --metrics-poll-sec 30 \\
        --output-dir .build/v2.8.0-soak

Quick validation (CI / dev box, ~2 minutes):
    python3 scripts/tools/dx/run_chaos_soak.py \\
        --target-url http://localhost:8080 \\
        --config-dir /path/to/conf.d \\
        --duration-min 2 --reload-interval-sec 10 --metrics-poll-sec 5 \\
        --output-dir /tmp/soak-smoke

Output
------
    <output-dir>/metrics-timeseries.csv  -- one row per /metrics poll
    <output-dir>/summary.txt             -- header + reload count + first/last samples
    <output-dir>/run-config.json         -- exact args + start/end timestamps

Exit codes
----------
    0  Soak completed cleanly
    1  Caller error (bad args, target not reachable on first probe)
    2  Soak interrupted but partial output preserved
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Metrics we extract from /metrics (Prometheus text format).
# Adding new ones here automatically extends the timeseries CSV.
#
# Why no `process_*` collectors: threshold-exporter / tenant-api register
# the default Go runtime collector but NOT the process collector
# (`prometheus.NewProcessCollector`). For a Go program, `go_memstats_sys_bytes`
# is the closest RSS-equivalent (total bytes obtained from the OS); we use
# that instead of `process_resident_memory_bytes`. If a future binary opts
# into the process collector, add `process_resident_memory_bytes` etc. here
# and they'll be picked up automatically.
TRACKED_METRICS = (
    "go_goroutines",
    "go_memstats_sys_bytes",            # ~ RSS proxy (total OS memory held)
    "go_memstats_alloc_bytes",          # current live heap
    "go_memstats_heap_inuse_bytes",     # heap pages actively used
    "go_memstats_heap_idle_bytes",      # heap pages held but unused
    "go_memstats_heap_objects",         # live object count (proxy for leak)
    "go_gc_duration_seconds_count",     # cumulative GC count (informational)
)


@dataclass
class RunConfig:
    target_url: str
    config_dir: str
    duration_min: int
    reload_interval_sec: int
    metrics_poll_sec: int
    output_dir: str
    started_at_utc: str = ""
    ended_at_utc: str = ""
    reload_count: int = 0
    poll_count: int = 0


def parse_metrics(text: str) -> dict[str, float]:
    """Extract TRACKED_METRICS from Prometheus text exposition format.

    Lines matching `<metric_name> <value>` are captured. Lines with labels
    (`<metric_name>{label=...} <value>`) are ignored — we want the
    process-level singletons, not per-tenant breakdowns.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        # Skip labeled samples — we only want the unlabeled process metrics
        if "{" in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, value = parts[0], parts[1]
        if name not in TRACKED_METRICS:
            continue
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


def fetch_metrics(target_url: str, timeout_sec: float = 5.0) -> dict[str, float] | None:
    """GET <target>/metrics and parse. Returns None on network error."""
    url = target_url.rstrip("/") + "/metrics"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return parse_metrics(text)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[warn] /metrics fetch failed: {exc}", file=sys.stderr)
        return None


def trigger_reload(config_dir: Path) -> bool:
    """Bump mtime of all .yaml files under config_dir to fire SHA-256 diff.

    threshold-exporter's hot-reload watches mtime + content hash. Touching
    mtime alone won't fire if content unchanged; we append a no-op comment
    line that toggles between two values to force a fresh hash each pass.
    """
    if not config_dir.exists():
        return False
    yaml_files = list(config_dir.rglob("*.yaml"))
    if not yaml_files:
        return False
    # Pick the first non-_defaults file to perturb (keeps platform invariants stable)
    for yf in yaml_files:
        if yf.name.startswith("_"):
            continue
        try:
            content = yf.read_text(encoding="utf-8")
            marker = "# soak-toggle: A\n"
            alt = "# soak-toggle: B\n"
            if marker in content:
                new = content.replace(marker, alt)
            elif alt in content:
                new = content.replace(alt, marker)
            else:
                new = content.rstrip() + "\n" + marker
            yf.write_text(new, encoding="utf-8")
            return True
        except OSError:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--target-url", required=True,
                        help="threshold-exporter URL (e.g. http://localhost:8080)")
    parser.add_argument("--config-dir", required=True,
                        help="conf.d/ path threshold-exporter watches; harness toggles a file in here")
    parser.add_argument("--duration-min", type=int, default=240,
                        help="Soak duration in minutes (default 240 = 4 hours)")
    parser.add_argument("--reload-interval-sec", type=int, default=60,
                        help="Trigger reload every N seconds (default 60)")
    parser.add_argument("--metrics-poll-sec", type=int, default=30,
                        help="Poll /metrics every N seconds (default 30)")
    parser.add_argument("--output-dir", required=True,
                        help="Where to write timeseries CSV + summary")
    args = parser.parse_args()

    cfg = RunConfig(
        target_url=args.target_url,
        config_dir=args.config_dir,
        duration_min=args.duration_min,
        reload_interval_sec=args.reload_interval_sec,
        metrics_poll_sec=args.metrics_poll_sec,
        output_dir=args.output_dir,
    )

    config_dir = Path(args.config_dir)
    if not config_dir.exists():
        print(f"[error] config-dir not found: {config_dir}", file=sys.stderr)
        return 1

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "metrics-timeseries.csv"
    summary_path = out / "summary.txt"
    run_config_path = out / "run-config.json"

    # First-probe check: target must be reachable before we commit to a long run
    initial = fetch_metrics(args.target_url)
    if initial is None:
        print(f"[error] cannot reach {args.target_url}/metrics — aborting before soak start",
              file=sys.stderr)
        return 1
    if not initial:
        print(f"[warn] /metrics returned no tracked metrics — soak will record empty rows",
              file=sys.stderr)

    cfg.started_at_utc = datetime.now(timezone.utc).isoformat()
    end_at = time.time() + (args.duration_min * 60)
    next_reload_at = time.time() + args.reload_interval_sec
    next_poll_at = time.time()  # first poll immediately

    # Open CSV with header
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow(["timestamp_utc", "elapsed_sec", "reload_count_so_far", *TRACKED_METRICS])

    interrupted = False

    def on_signal(signum, frame):  # noqa: ARG001 — signature mandated
        nonlocal interrupted
        interrupted = True
        print(f"\n[info] caught signal {signum} — finalising output", file=sys.stderr)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    started_wall = time.time()
    try:
        while time.time() < end_at and not interrupted:
            now = time.time()

            # Poll metrics
            if now >= next_poll_at:
                metrics = fetch_metrics(args.target_url)
                row = [
                    datetime.now(timezone.utc).isoformat(),
                    f"{now - started_wall:.1f}",
                    cfg.reload_count,
                ]
                if metrics is None:
                    row.extend([""] * len(TRACKED_METRICS))
                else:
                    for m in TRACKED_METRICS:
                        row.append(f"{metrics.get(m, ''):.0f}" if isinstance(metrics.get(m), float) else "")
                writer.writerow(row)
                csv_file.flush()
                cfg.poll_count += 1
                next_poll_at = now + args.metrics_poll_sec

            # Trigger reload
            if now >= next_reload_at:
                if trigger_reload(config_dir):
                    cfg.reload_count += 1
                else:
                    print(f"[warn] reload trigger failed at t={now - started_wall:.0f}s",
                          file=sys.stderr)
                next_reload_at = now + args.reload_interval_sec

            # Sleep till next event (poll or reload, whichever sooner)
            sleep_for = min(next_poll_at, next_reload_at, end_at) - time.time()
            if sleep_for > 0:
                time.sleep(min(sleep_for, 5.0))  # cap at 5s for responsiveness to signals
    finally:
        csv_file.close()
        cfg.ended_at_utc = datetime.now(timezone.utc).isoformat()

        # Write summary + run-config
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"v2.8.0 readiness chaos soak — summary\n")
            f.write(f"=" * 60 + "\n")
            f.write(f"target:           {args.target_url}\n")
            f.write(f"config-dir:       {args.config_dir}\n")
            f.write(f"duration:         {args.duration_min} min "
                    f"({'completed' if not interrupted else 'INTERRUPTED'})\n")
            f.write(f"reload interval:  {args.reload_interval_sec}s\n")
            f.write(f"metrics poll:     {args.metrics_poll_sec}s\n")
            f.write(f"started (UTC):    {cfg.started_at_utc}\n")
            f.write(f"ended (UTC):      {cfg.ended_at_utc}\n")
            f.write(f"reload count:     {cfg.reload_count}\n")
            f.write(f"metric polls:     {cfg.poll_count}\n")
            f.write(f"\nTimeseries:       {csv_path.name}\n")
            f.write(f"Run report:       run `python3 scripts/tools/dx/render_soak_diff.py "
                    f"--input-dir {out}`\n")

        with open(run_config_path, "w", encoding="utf-8") as f:
            json.dump(asdict(cfg), f, indent=2, ensure_ascii=False)

        print(f"\n[info] soak {'completed' if not interrupted else 'interrupted'}: "
              f"{cfg.reload_count} reloads / {cfg.poll_count} polls", file=sys.stderr)
        print(f"[info] output: {out}", file=sys.stderr)

    return 2 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
