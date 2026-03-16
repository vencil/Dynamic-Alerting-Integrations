#!/usr/bin/env python3
"""baseline_discovery.py — Baseline Discovery 工具。

在負載注入環境下觀測指標，協助決定合理的閾值設定。
透過 Prometheus API 採集指標時間序列，計算統計摘要（p50/p90/p95/p99/max），
產出 CSV + 建議閾值報告。

用法:
  # 觀測 30 分鐘，每 30 秒採樣一次
  python3 baseline_discovery.py \
    --tenant db-a \
    --duration 1800 --interval 30 \
    --prometheus http://localhost:9090

  # 指定觀測指標（預設觀測所有已知指標）
  python3 baseline_discovery.py \
    --tenant db-a \
    --metrics connections,cpu,slow_queries \
    --prometheus http://localhost:9090

  # Dry-run：僅顯示要觀測的指標，不實際採樣
  python3 baseline_discovery.py \
    --tenant db-a --dry-run

  # 搭配負載注入使用（典型流程）：
  #   Terminal 1: ./scripts/run_load.sh --tenant db-a --type composite
  #   Terminal 2: python3 scripts/tools/baseline_discovery.py --tenant db-a

需求:
  - Prometheus Query API 可達
  - 建議搭配 run_load.sh 負載注入同時使用
"""

import sys
import os
import csv
import io
import json
import time
import math
import argparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import http_get_json, write_text_secure, query_prometheus_instant  # noqa: E402

# Alias for backward-compat within this module
query_prometheus = query_prometheus_instant

# 預設觀測指標：PromQL 模板 (tenant 會被替換)
DEFAULT_METRICS = {
    "connections": {
        "query": 'mysql_global_status_threads_connected{{tenant="{tenant}"}}',
        "unit": "connections",
        "description": "MariaDB active connections",
    },
    "cpu": {
        "query": 'rate(container_cpu_usage_seconds_total{{namespace="{tenant}",container="mariadb"}}[5m]) * 100',
        "unit": "%",
        "description": "Container CPU usage rate",
    },
    "slow_queries": {
        "query": 'rate(mysql_global_status_slow_queries{{tenant="{tenant}"}}[5m]) * 60',
        "unit": "queries/min",
        "description": "Slow queries per minute",
    },
    "memory": {
        "query": 'container_memory_working_set_bytes{{namespace="{tenant}",container="mariadb"}} / 1024 / 1024',
        "unit": "MiB",
        "description": "Container memory working set",
    },
    "disk_io": {
        "query": 'rate(container_fs_reads_bytes_total{{namespace="{tenant}",container="mariadb"}}[5m]) / 1024',
        "unit": "KiB/s",
        "description": "Disk read throughput",
    },
}


def extract_scalar(results):
    """Extract first scalar value from Prometheus result."""
    if not results:
        return None
    val_str = results[0].get("value", [None, None])[1]
    try:
        val = float(val_str)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except (TypeError, ValueError):
        return None


def percentile(sorted_values, p):
    """Calculate percentile from sorted list."""
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_values):
        return sorted_values[-1]
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


def compute_stats(samples):
    """Compute statistics from sample list."""
    valid = [s for s in samples if s is not None]
    if not valid:
        return {
            "count": 0, "min": None, "max": None,
            "avg": None, "p50": None, "p90": None, "p95": None, "p99": None,
        }

    sorted_vals = sorted(valid)
    return {
        "count": len(valid),
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": sum(valid) / len(valid),
        "p50": percentile(sorted_vals, 50),
        "p90": percentile(sorted_vals, 90),
        "p95": percentile(sorted_vals, 95),
        "p99": percentile(sorted_vals, 99),
    }


def suggest_threshold(stats, metric_name):
    """Suggest threshold based on observed statistics.

    策略：
    - warning: p95 × 1.2 (正常運行時 95% 的值再加 20% 緩衝)
    - critical: p99 × 1.5 (接近極限值再加 50% 緩衝)
    - 若觀測樣本不足 (<10)，不給建議
    """
    if stats["count"] < 10:
        return {"warning": None, "critical": None, "note": "樣本不足，建議延長觀測時間"}

    warning = None
    critical = None

    if stats["p95"] is not None and stats["p95"] > 0:
        warning = round(stats["p95"] * 1.2, 2)
    if stats["p99"] is not None and stats["p99"] > 0:
        critical = round(stats["p99"] * 1.5, 2)

    # 特殊邏輯：connections 建議取整
    if metric_name == "connections":
        if warning is not None:
            warning = int(math.ceil(warning))
        if critical is not None:
            critical = int(math.ceil(critical))

    return {"warning": warning, "critical": critical, "note": "基於 p95×1.2 / p99×1.5"}


def main():
    """CLI entry point: Baseline Discovery 工具。."""
    parser = argparse.ArgumentParser(
        description="Baseline Discovery — 負載觀測 + 閾值建議工具",
    )
    parser.add_argument("--tenant", required=True, help="Tenant namespace (e.g. db-a)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus URL (預設: http://localhost:9090)")
    parser.add_argument("--duration", type=int, default=600,
                        help="觀測持續時間（秒，預設: 600 = 10 分鐘）")
    parser.add_argument("--interval", type=int, default=15,
                        help="採樣間隔（秒，預設: 15）")
    parser.add_argument("--metrics", default=None,
                        help="觀測指標（逗號分隔，預設: 全部）")
    parser.add_argument("-o", "--output-dir", default="baseline_output",
                        help="輸出目錄（預設: baseline_output）")
    parser.add_argument("--dry-run", action="store_true",
                        help="僅顯示要觀測的指標，不實際採樣")

    args = parser.parse_args()

    # 選擇指標
    if args.metrics:
        metric_keys = [m.strip() for m in args.metrics.split(",")]
    else:
        metric_keys = list(DEFAULT_METRICS.keys())

    metrics = {}
    for key in metric_keys:
        if key not in DEFAULT_METRICS:
            print(f"⚠️  未知指標: {key}（可用: {', '.join(DEFAULT_METRICS.keys())}）",
                  file=sys.stderr)
            continue
        metrics[key] = DEFAULT_METRICS[key].copy()
        metrics[key]["query"] = metrics[key]["query"].format(tenant=args.tenant)

    if not metrics:
        print("錯誤: 無有效指標可觀測", file=sys.stderr)
        sys.exit(1)

    # Dry-run
    if args.dry_run:
        print(f"\n📋 Baseline Discovery — Dry Run")
        print(f"   Tenant: {args.tenant}")
        print(f"   Duration: {args.duration}s, Interval: {args.interval}s")
        print(f"   Samples: ~{args.duration // args.interval}")
        print(f"\n觀測指標:")
        for key, info in metrics.items():
            print(f"  • {key}: {info['description']}")
            print(f"    Query: {info['query']}")
        return

    # 開始觀測
    total_samples = args.duration // args.interval
    print(f"\n🔍 Baseline Discovery — 開始觀測")
    print(f"   Tenant: {args.tenant}")
    print(f"   Prometheus: {args.prometheus}")
    print(f"   Duration: {args.duration}s, Interval: {args.interval}s")
    print(f"   Expected samples: {total_samples}")
    print(f"   Metrics: {', '.join(metrics.keys())}")
    print()

    # 收集數據
    samples = {key: [] for key in metrics}
    timestamps = []

    for i in range(total_samples):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        timestamps.append(ts)
        sys.stdout.write(f"\r  採樣 {i+1}/{total_samples} ({ts})")
        sys.stdout.flush()

        for key, info in metrics.items():
            results, err = query_prometheus(args.prometheus, info["query"])
            if err:
                samples[key].append(None)
            else:
                samples[key].append(extract_scalar(results))

        if i < total_samples - 1:
            time.sleep(args.interval)

    print(f"\n\n✅ 觀測完成：共 {total_samples} 個採樣點\n")

    # 計算統計
    all_stats = {}
    for key in metrics:
        all_stats[key] = compute_stats(samples[key])

    # 輸出報告
    print(f"{'='*70}")
    print(f"📊 Baseline Discovery Report — {args.tenant}")
    print(f"{'='*70}\n")

    for key, info in metrics.items():
        stats = all_stats[key]
        suggestion = suggest_threshold(stats, key)

        print(f"■ {key} ({info['description']})")
        print(f"  Unit: {info['unit']}")

        if stats["count"] == 0:
            print(f"  ⚠️  無有效資料\n")
            continue

        print(f"  Samples: {stats['count']}")
        print(f"  Range: {stats['min']:.2f} ~ {stats['max']:.2f}")
        print(f"  Average: {stats['avg']:.2f}")
        print(f"  Percentiles: p50={stats['p50']:.2f}  p90={stats['p90']:.2f}  "
              f"p95={stats['p95']:.2f}  p99={stats['p99']:.2f}")

        if suggestion["warning"] is not None:
            print(f"  💡 建議 warning: {suggestion['warning']}  "
                  f"critical: {suggestion['critical']}  ({suggestion['note']})")
        else:
            print(f"  💡 {suggestion['note']}")
        print()

    # 寫入 CSV
    os.makedirs(args.output_dir, exist_ok=True)

    # 原始時間序列
    ts_path = os.path.join(args.output_dir, f"baseline-{args.tenant}-timeseries.csv")
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["timestamp"] + list(metrics.keys())
    writer.writerow(header)
    for i, ts in enumerate(timestamps):
        row = [ts] + [samples[key][i] for key in metrics]
        writer.writerow(row)
    write_text_secure(ts_path, "\ufeff" + buf.getvalue())

    # 統計摘要 + 建議
    summary_path = os.path.join(args.output_dir, f"baseline-{args.tenant}-summary.csv")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "metric", "unit", "samples", "min", "max", "avg",
        "p50", "p90", "p95", "p99",
        "suggested_warning", "suggested_critical", "note",
    ])
    for key, info in metrics.items():
        stats = all_stats[key]
        suggestion = suggest_threshold(stats, key)
        writer.writerow([
            key, info["unit"], stats["count"],
            stats["min"], stats["max"], stats["avg"],
            stats["p50"], stats["p90"], stats["p95"], stats["p99"],
            suggestion["warning"], suggestion["critical"], suggestion["note"],
        ])
    write_text_secure(summary_path, "\ufeff" + buf.getvalue())

    print(f"📁 輸出:")
    print(f"  時間序列: {ts_path}")
    print(f"  統計摘要: {summary_path}")

    # 建議 patch 指令
    print(f"\n{'='*70}")
    print(f"💡 建議 patch 指令（可直接執行或調整後使用）:")
    print(f"{'='*70}\n")

    for key in metrics:
        suggestion = suggest_threshold(all_stats[key], key)
        if suggestion["warning"] is not None:
            config_key = f"mysql_{key}" if not key.startswith("container_") else key
            print(f"  # {key}: warning={suggestion['warning']}")
            print(f"  python3 scripts/tools/patch_config.py {args.tenant} {config_key} {suggestion['warning']}")
            if suggestion["critical"] is not None:
                print(f"  python3 scripts/tools/patch_config.py {args.tenant} {config_key}_critical {suggestion['critical']}")
            print()


if __name__ == "__main__":
    main()
