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
  uses the existing /metrics endpoint instead, capturing the Go runtime
  collector signals for "is anything leaking":
    * go_goroutines                  (goroutine leak detector)
    * go_memstats_sys_bytes          (RSS proxy — total OS memory held)
    * go_memstats_alloc_bytes        (live heap allocations)
    * go_memstats_heap_inuse_bytes
    * go_memstats_heap_idle_bytes    (held but not in use)
    * go_memstats_heap_objects       (live object count)
    * go_gc_duration_seconds_count   (GC activity, informational)
  See TRACKED_METRICS below for the canonical list. process_* collectors
  (process_resident_memory_bytes / process_open_fds) are NOT tracked
  because threshold-exporter doesn't register prometheus.NewProcessCollector
  — go_memstats_sys_bytes serves as the RSS proxy.
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
    2  Caller error — bad args / config-dir missing / target not reachable on
       first probe, OR soak interrupted (SIGINT/SIGTERM) with partial output
       preserved. Both are "the tool could not finish a clean soak", per the
       0/1/2 contract in scripts/tools/_lib_exitcodes.py. There is no exit-1
       (finding) state: this harness records signals, it does not gate.
"""
from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import os

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import (  # noqa: E402  (#1789; #3276)
    OutputWriteError, exit_on_output_write_error, output_write, safe_label)
from _lib_paths import classify_path  # noqa: E402  (#3276)
import _lib_parse_cache  # noqa: E402  (#3206: da-guard's listing)

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
    "go_memstats_heap_released_bytes",  # idle pages returned to OS (#459: the
                                        # direct return-to-OS signal — rises
                                        # when GOMEMLIMIT / FreeOSMemory levers
                                        # reclaim the heap_idle high-water creep)
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
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        print(f"[error] /metrics fetch rejected: non-http(s) scheme in {url!r}", file=sys.stderr)
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:  # nosec B310  #scheme validated above
            text = resp.read().decode("utf-8", errors="replace")
        return parse_metrics(text)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[warn] /metrics fetch failed: {exc}", file=sys.stderr)
        return None


def reload_carriers(config_dir: Path) -> list[Path]:
    """The files `trigger_reload` may perturb: every tenant file the
    exporter reads (da-guard's listing; no `_`-prefixed platform file, so
    platform invariants stay stable), in walk order.

    #3206: taken ONCE, at the start of the run — the soak rewrites these
    files, so a listing per pass would index a tree the soak itself is
    changing. A tree Go names paths on ends the run here, every such path
    named, exit 2."""
    listing = _lib_parse_cache.ConfdReader(config_dir).listing()
    return [config_dir / e.path for e in listing.config_files()
            if not e.reserved and "exporter" in (e.readers or ())]


def trigger_reload(carriers: list[Path]) -> bool:
    """Rewrite the first writable of *carriers* (`reload_carriers`) to fire
    the exporter's SHA-256 diff.

    threshold-exporter's hot-reload watches mtime + content hash. Touching
    mtime alone won't fire if content unchanged; we append a no-op comment
    line that toggles between two values to force a fresh hash each pass.

    Returns True only if a carrier the exporter WOULD reload was written.
    """
    for yf in carriers:
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
            # ⚠️ NOT an --output-dir sink and deliberately NOT wrapped
            # (#1789): this perturbs a file under `--config-dir`, which is the
            # INPUT tree the soak is chaos-testing, and the `except OSError:
            # continue` below is the point — an unwritable carrier means "try
            # the next one", not "the operator mistyped a flag".
            yf.write_text(new, encoding="utf-8", newline="\n")
            return True
        except OSError:
            continue
    return False


def _report_deferred_write_error(
    exc: OutputWriteError,
    in_flight: type[BaseException] | None,
) -> bool:
    """Say the deferred write failure out loud — but only if it is about to be
    swallowed. Returns whether a line was printed.

    The summary/run-config writes live in a ``finally``, so their
    ``OutputWriteError`` is REMEMBERED and re-raised after the block (raising
    inside a ``finally`` would replace whatever was already unwinding). Two
    cases follow from that, and they need opposite handling:

    * *in_flight is None* — nothing else is unwinding, the re-raise really
      happens, and the decorator prints the standard one-line rc=2. Printing
      here as well would say the same thing twice.
    * *in_flight is not None* — the exception already on its way out wins and
      the re-raise is never reached. Without this line the write failure
      disappears with no trace at all (#1789). It does NOT touch the rc:
      whatever is in flight still decides that.
    """
    if in_flight is None:
        return False
    print(f"[warn] output write failed: {safe_label(str(exc))}", file=sys.stderr)
    return True


@exit_on_output_write_error
@_lib_parse_cache.exit_on_parse_tree_error  # #3206: the listing is da-guard's — rc 2, named
def main() -> int:
    try_utf8_stdout()
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
    # #3276: the shared judgement of a caller-given path.
    problem = classify_path(config_dir, "dir", flag="--config-dir")
    if problem is not None:
        word = "not found" if problem.missing else "not usable"
        print(f"[error] config-dir {word}: "
              f"{problem.message}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    carriers = reload_carriers(config_dir)

    out = Path(args.output_dir)
    with output_write(out, flag="--output-dir", action="create directory"):
        out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "metrics-timeseries.csv"
    summary_path = out / "summary.txt"
    run_config_path = out / "run-config.json"

    # First-probe check: target must be reachable before we commit to a long run
    initial = fetch_metrics(args.target_url)
    if initial is None:
        print(f"[error] cannot reach {args.target_url}/metrics — aborting before soak start",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not initial:
        print(f"[warn] /metrics returned no tracked metrics — soak will record empty rows",
              file=sys.stderr)

    cfg.started_at_utc = datetime.now(timezone.utc).isoformat()
    end_at = time.time() + (args.duration_min * 60)
    next_reload_at = time.time() + args.reload_interval_sec
    next_poll_at = time.time()  # first poll immediately

    # Open CSV with header.
    # ⚠️ Only the `open` is wrapped, and that is the whole of what can be
    # wrapped here: the handle lives for the length of the soak, so its
    # `writerow` / `flush` (in the loop below) and its `close()` (in the
    # `finally`) happen OUTSIDE any block a context manager could span. A
    # write or a flush that fails there — a filesystem that filled up mid-run
    # — still exits 1 with a traceback. Written down in
    # tests/shared/test_output_write_sites_stay_guarded.py (below its
    # NOT_GUARDED list, which cannot hold them: they are not sinks the scanner
    # can see), not silently ignored.
    with output_write(csv_path, flag="--output-dir"):
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
    # An output-write failure inside the `finally` below is REMEMBERED, not
    # raised there: a `raise` (or a `sys.exit`) inside a `finally` replaces
    # whatever exception was already unwinding — a KeyboardInterrupt, an error
    # from the poll loop — and the operator would be shown the wrong failure.
    # It is re-raised after the block instead, where it only wins if nothing
    # else was in flight, and the decorator turns it into rc=2.
    pending_write_error: OutputWriteError | None = None
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
                if trigger_reload(carriers):
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
        # What was already unwinding when this `finally` started, if anything.
        # ⛔ Read HERE and not in the handler below: inside
        # `except OutputWriteError` the current exception IS that error, so
        # `sys.exc_info()` there can never answer this question.
        in_flight = sys.exc_info()[0]
        csv_file.close()
        cfg.ended_at_utc = datetime.now(timezone.utc).isoformat()

        # Write summary + run-config
        try:
            with output_write(summary_path, flag="--output-dir"):
                with open(summary_path, "w", encoding="utf-8", newline="\n") as f:
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

            with output_write(run_config_path, flag="--output-dir"):
                with open(run_config_path, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(asdict(cfg), f, indent=2, ensure_ascii=False)
        except OutputWriteError as exc:
            # First failure wins and skips the rest; the info lines below still
            # run so the operator sees where the (partial) output went.
            pending_write_error = exc
            _report_deferred_write_error(exc, in_flight)

        print(f"\n[info] soak {'completed' if not interrupted else 'interrupted'}: "
              f"{cfg.reload_count} reloads / {cfg.poll_count} polls", file=sys.stderr)
        print(f"[info] output: {out}", file=sys.stderr)

    if pending_write_error is not None:
        raise pending_write_error

    return EXIT_CALLER_ERROR if interrupted else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
