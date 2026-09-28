#!/usr/bin/env python3
"""baseline_discovery.py — Baseline Discovery 工具。

在負載注入環境下觀測指標，協助決定合理的閾值設定。
透過 Prometheus API 採集指標時間序列，計算統計摘要（p50/p90/p95/p99/max），
產出 CSV + 建議閾值報告。

領域邊界（Day 0 vs Day N，#719）：
  本工具是 **Day 0 / 冷啟動粗估** —— 直接查底層 raw exporter metric
  （DEFAULT_METRICS，如 mysql_global_status_threads_connected），用於新租戶
  onboarding 階段、rule-pack recording rule 尚未累積足夠歷史時，給一個粗略基準。
  **Day N / 上線後精確微調**請用 `threshold_recommend.py` —— 它查租戶閾值在
  rule-pack alert 中**實際比對**的 recording rule（同單位/拓撲），精度較高。
  ⛔ 兩者**勿合併**：資料源不同（raw exporter vs normalized recording rule）、
  時空背景不同（冷啟動 vs 穩態微調）。

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
  #   Terminal 2: python3 scripts/tools/ops/baseline_discovery.py --tenant db-a

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
from _lib_python import (  # noqa: E402
    http_get_json, write_text_secure, query_prometheus_instant,
    add_prometheus_arg, DOCS_INSTALL_URL,
)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import exit_on_output_write_error, output_write  # noqa: E402  (#1789)

# Alias for backward-compat within this module
query_prometheus = query_prometheus_instant

# 預設觀測指標：PromQL 模板 (tenant 會被替換)
# `config_key`：建議值要寫進哪個租戶 key。先前一律拼成 `mysql_<名稱>`，其中
# mysql_memory／mysql_disk_io／mysql_slow_queries 沒有任何 rule pack 讀，
# mysql_cpu 在別名視窗內變成 mysql_threads_running（issue 1196）。
# cpu／memory 對到 container_cpu／container_memory，而那兩個 key 在 rule pack
# 裡比的是「佔 limit 的 %」，所以查詢用的是 rule-pack-kubernetes.yaml
# `tenant:container_{cpu,memory}_percent:by_container` 的分子分母原文（只把
# namespace 換成這個租戶），取租戶內最弱的容器。仍然是原始 cAdvisor／
# kube-state-metrics 指標，不讀 recording rule——Day 0 的邊界不變（#719）。
# tests/ops/test_baseline_discovery.py 把這兩段與 rule pack 逐字比對。
DEFAULT_METRICS = {
    "connections": {
        "query": 'mysql_global_status_threads_connected{{tenant="{tenant}"}}',
        "unit": "connections",
        "description": "MariaDB active connections",
        "config_key": "mysql_connections",
    },
    "cpu": {
        "query": ('max((sum by(namespace, pod, container) ('
                  'rate(container_cpu_usage_seconds_total{{namespace="{tenant}", '
                  'container!="", container!="POD"}}[5m])) / '
                  'sum by(namespace, pod, container) ('
                  'kube_pod_container_resource_limits{{resource="cpu", '
                  'namespace="{tenant}"}})) * 100)'),
        "unit": "% of limit",
        "description": "Container CPU % of limit (weakest container)",
        "config_key": "container_cpu",
        "bounded": True,
    },
    "slow_queries": {
        "query": 'rate(mysql_global_status_slow_queries{{tenant="{tenant}"}}[5m]) * 60',
        "unit": "queries/min",
        "description": "Slow queries per minute",
        "config_key": None,
        "no_key_reason": ("MariaDBHighSlowQueries compares against a fixed value, "
                          "not a tenant threshold"),
    },
    "memory": {
        "query": ('max((sum by(namespace, pod, container) ('
                  'container_memory_working_set_bytes{{namespace="{tenant}", '
                  'container!="", container!="POD"}}) / '
                  'sum by(namespace, pod, container) ('
                  'kube_pod_container_resource_limits{{resource="memory", '
                  'namespace="{tenant}"}})) * 100)'),
        "unit": "% of limit",
        "description": "Container memory % of limit (weakest container)",
        "config_key": "container_memory",
        "bounded": True,
    },
    "disk_io": {
        "query": ('sum(rate(container_fs_reads_bytes_total{{namespace="{tenant}", '
                  'container!="", container!="POD"}}[5m])) / 1024'),
        "unit": "KiB/s",
        "description": "Disk read throughput",
        "config_key": None,
        "no_key_reason": "no rule pack alert reads a disk read-throughput threshold",
    },
}


# 「佔 limit 的 %」有上界：到 100 就 OOMKill（memory）或被 CFS 節流（CPU）。
# 這類門檻由「多接近上限才危險」決定，是平台層的固定值（kubernetes-mixin、
# Datadog 的做法；我們的預設在 scaffold）。舊公式 p95×1.2／p99×1.5 在
# p99 ≥ 66.7 時給出 >100 的門檻，永遠不會響（issue 1196）。所以這兩項不算
# 門檻，只拿觀測值對照平台預設：預設夠用就說夠用；平時用量已頂到預設，
# 就建議調高 limit（讓 p99 落在預設的 LIMIT_TARGET_SHARE），而不是把門檻往上推。
LIMIT_TARGET_SHARE = 0.9
NO_LIMIT_NOTE = ("沒有設 limit 的容器量不到（查的是用量 ÷ kube_pod_container_resource_limits，"
                 "需要 kube-state-metrics）。rule pack 對沒設 CPU limit 的容器改用佔節點的比例，"
                 "baseline 不涵蓋；memory 沒設 limit 時 rule pack 也不告警。")


def platform_default(key):
    """平台對 ``key`` 的預設值，取自 scaffold_tenant.RULE_PACKS（registry 由它生成；
    映像與 repo 佈局都有這支）。讀不到回 None。"""
    try:
        import scaffold_tenant
    except ImportError:
        return None
    for pack in scaffold_tenant.RULE_PACKS.values():
        entry = (pack.get("defaults") or {}).get(key)
        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
    return None


def percent_verdict(stats, key):
    """對照平台預設的判定：(一行摘要, 是否需要調 limit, limit 倍數或 None)。"""
    default = platform_default(key)
    if default is None:
        return f"{key} 的平台預設讀不到，無法對照", False, None
    p99 = stats["p99"]
    if p99 < default:
        return (f"{key} 平台預設 {default}% 可用（p99 = {p99:.2f}%），不需覆寫", False, None)
    factor = p99 / (default * LIMIT_TARGET_SHARE)
    return (f"{key} 平台預設 {default}% 太緊：平時 p99 = {p99:.2f}% 已達預設，照預設會常響。"
            f"請先調高 limit（約 ×{factor:.2f}，讓 p99 落在預設的 "
            f"{int(LIMIT_TARGET_SHARE * 100)}%），不要把門檻往上推", True, factor)


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


@exit_on_output_write_error
def main():
    """CLI entry point: Baseline Discovery 工具。."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Baseline Discovery — 負載觀測 + 閾值建議工具",
    )
    parser.add_argument("--tenant", required=True, help="Tenant namespace (e.g. db-a)")
    add_prometheus_arg(parser,
                       help_text="Prometheus URL (預設: $PROMETHEUS_URL，否則 http://localhost:9090)")
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
        sys.exit(EXIT_CALLER_ERROR)

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
            print(f"  ⚠️  無有效資料")
            if info.get("bounded"):
                print(f"  {NO_LIMIT_NOTE}")
            print()
            continue

        print(f"  Samples: {stats['count']}")
        print(f"  Range: {stats['min']:.2f} ~ {stats['max']:.2f}")
        print(f"  Average: {stats['avg']:.2f}")
        print(f"  Percentiles: p50={stats['p50']:.2f}  p90={stats['p90']:.2f}  "
              f"p95={stats['p95']:.2f}  p99={stats['p99']:.2f}")

        if info.get("bounded") and stats["count"] >= 10:
            print(f"  💡 {percent_verdict(stats, info['config_key'])[0]}")
        elif suggestion["warning"] is not None:
            no_key = "" if info.get("config_key") else "；沒有租戶閾值 key，僅供參考"
            print(f"  💡 建議 warning: {suggestion['warning']}  "
                  f"critical: {suggestion['critical']}  ({suggestion['note']}{no_key})")
        else:
            print(f"  💡 {suggestion['note']}")
        print()

    # 寫入 CSV
    #
    # CRLF note: csv.writer emits "\r\n" line terminators per RFC 4180.
    # If we route through write_text_secure (default text-mode open),
    # Python's universal-newlines on Windows translates "\n" → "\r\n"
    # again, producing "\r\r\n" on disk. csv.reader can technically
    # recover but produces empty rows that break parsers/tests. Fix:
    # write in binary mode (UTF-8 + BOM) so the writer's terminators
    # reach disk verbatim. Matches write_text_secure's UTF-8 + 0o600
    # contract without the newline translation step.
    with output_write(args.output_dir, flag="-o/--output-dir",
                      action="create directory"):
        os.makedirs(args.output_dir, exist_ok=True)

    def _write_csv_secure(path: str, body_with_bom: str) -> None:
        # #1789: the `with` lives INSIDE this helper, not around its two call
        # sites: the static pin walks up from a sink and stops at the
        # enclosing `def`, so a wrapper around the CALL would leave these two
        # sinks reading as bare. The chmod is inside the same block as the
        # open — 0o600 is part of producing this file, and a chmod that fails
        # leaves the operator a CSV whose mode is not the promised one.
        with output_write(path, flag="-o/--output-dir"):
            with open(path, "wb") as fh:
                fh.write(body_with_bom.encode("utf-8"))
            os.chmod(path, 0o600)

    # 原始時間序列
    ts_path = str(Path(args.output_dir) / f"baseline-{args.tenant}-timeseries.csv")
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["timestamp"] + list(metrics.keys())
    writer.writerow(header)
    for i, ts in enumerate(timestamps):
        row = [ts] + [samples[key][i] for key in metrics]
        writer.writerow(row)
    _write_csv_secure(ts_path, "\ufeff" + buf.getvalue())

    # 統計摘要 + 建議
    summary_path = str(Path(args.output_dir) / f"baseline-{args.tenant}-summary.csv")
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
        if info.get("bounded"):
            verdict = (percent_verdict(stats, info["config_key"])[0]
                       if stats["count"] >= 10 else
                       (NO_LIMIT_NOTE if stats["count"] == 0 else suggestion["note"]))
            suggestion = {"warning": None, "critical": None, "note": verdict}
        writer.writerow([
            key, info["unit"], stats["count"],
            stats["min"], stats["max"], stats["avg"],
            stats["p50"], stats["p90"], stats["p95"], stats["p99"],
            suggestion["warning"], suggestion["critical"], suggestion["note"],
        ])
    _write_csv_secure(summary_path, "\ufeff" + buf.getvalue())

    print(f"📁 輸出:")
    print(f"  時間序列: {ts_path}")
    print(f"  統計摘要: {summary_path}")

    # 建議 patch 指令
    print(f"\n{'='*70}")
    print("💡 建議的閾值（下面用 patch-config 的寫法表示，先決條件見下方 ⛔）:")
    print(f"{'='*70}")
    # #1447: these lines used to name `scripts/tools/patch_config.py`, a path
    # that exists in neither the customer's repository nor (since the tools
    # moved under `ops/`) this one. The subcommand form is what the reader
    # actually has — but "可直接執行" was still not true of it, so the
    # prerequisite is stated rather than implied.
    print(f"（`da-tools` 的取得方式見 {DOCS_INSTALL_URL}）")
    print("⛔ patch-config 改的是叢集裡的 ConfigMap，需要 kubectl 與叢集存取權，"
          "而 da-tools 映像本身不含 kubectl。")
    print("   走 GitOps 的話請直接把下面的值寫進你 repo 的 conf.d/<tenant>.yaml，"
          "再 commit。\n")

    for key, info in metrics.items():
        suggestion = suggest_threshold(all_stats[key], key)
        if info.get("bounded"):
            if all_stats[key]["count"] >= 10:
                print(f"  # {key}: {percent_verdict(all_stats[key], info['config_key'])[0]}")
                print()
            continue
        if suggestion["warning"] is not None:
            config_key = info["config_key"]
            print(f"  # {key}: warning={suggestion['warning']}")
            if config_key is None:
                # issue 1196: no tenant key reads this measurement, so a
                # patch-config line would set a key that changes nothing.
                print(f"  #   no tenant threshold key for this one: {info['no_key_reason']}")
                print()
                continue
            print(f"  da-tools patch-config {args.tenant} {config_key} {suggestion['warning']}")
            if suggestion["critical"] is not None:
                print(f"  da-tools patch-config {args.tenant} {config_key}_critical {suggestion['critical']}")
            print()


if __name__ == "__main__":
    main()
