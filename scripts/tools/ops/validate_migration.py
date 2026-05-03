#!/usr/bin/env python3
"""validate_migration.py — Shadow Monitoring 驗證工具。

比對新舊 Recording Rule 的數值輸出，驗證遷移後的行為等價性。
透過 Prometheus API 抓取兩組 Recording Rule 的即時向量值，
逐一比對數值差異，產出 diff 報告。

用法:
  # 基本比對: 指定新舊兩組 recording rule 名稱
  python3 validate_migration.py \\
    --old "mysql_global_status_threads_connected" \\
    --new "tenant:custom_mysql_global_status_threads_connected:max" \\
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090

  # 批次比對: 使用 prefix-mapping.yaml
  python3 validate_migration.py \\
    --mapping migration_output/prefix-mapping.yaml \\
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090

  # 持續監控模式 (每 60 秒比對一次，運行 N 輪)
  python3 validate_migration.py \\
    --mapping migration_output/prefix-mapping.yaml \\
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090 \\
    --watch --interval 60 --rounds 1440

  # 本地開發 (透過 port-forward)
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &
  python3 validate_migration.py \\
    --mapping migration_output/prefix-mapping.yaml \\
    --prometheus http://localhost:9090

  # 叢集內執行 (包裝為 K8s Job，適合長期 Shadow Monitoring)
  # 參見 docs/migration-guide.md §11 的 Job manifest 範例

需求:
  - Prometheus Query API 必須可從腳本執行位置存取
    * 叢集內: K8s Service (http://prometheus.monitoring.svc.cluster.local:9090)
    * 叢集外: port-forward 或 Ingress
    * 多叢集: Thanos Query / VictoriaMetrics 等統一查詢端點亦可
  - 新舊兩套 Recording Rule 必須同時在 Prometheus 中運行
  - 建議在 Shadow Monitoring 階段 (新 Alert 掛 migration_status=shadow) 使用
"""

import sys
import os
import csv
import io
import json
import time
import argparse
from datetime import datetime, timezone

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import http_get_json, write_text_secure, write_json_secure, query_prometheus_instant  # noqa: E402

# Alias for backward-compat within this module
query_prometheus = query_prometheus_instant


def extract_value_map(results, group_by="tenant"):
    """將 Prometheus 結果轉換為 {label_key: float_value} 字典。"""
    value_map = {}
    for item in results:
        metric = item.get("metric", {})
        key = metric.get(group_by, "__no_label__")
        val_str = item.get("value", [None, None])[1]
        try:
            value_map[key] = float(val_str)
        except (TypeError, ValueError):
            value_map[key] = None
    return value_map


def compare_vectors(old_map, new_map, tolerance=0.001):
    """比對兩組向量值，回傳差異清單。

    tolerance: 允許的數值誤差 (預設 0.1%)
    """
    diffs = []
    all_keys = sorted(set(list(old_map.keys()) + list(new_map.keys())))

    for key in all_keys:
        old_val = old_map.get(key)
        new_val = new_map.get(key)

        if old_val is None and new_val is None:
            status = "both_empty"
        elif old_val is None:
            status = "old_missing"
        elif new_val is None:
            status = "new_missing"
        elif abs(old_val - new_val) <= tolerance * max(abs(old_val), abs(new_val), 1):
            status = "match"
        else:
            status = "mismatch"

        diffs.append({
            "tenant": key,
            "old_value": old_val,
            "new_value": new_val,
            "status": status,
            "delta": (new_val - old_val) if old_val is not None and new_val is not None else None,
        })
    return diffs


def run_single_comparison(prom_url, old_query, new_query, label):
    """執行單次比對，回傳 diff 結果。"""
    old_results, old_err = query_prometheus(prom_url, old_query)
    if old_err:
        print(f"  ❌ 查詢舊規則失敗: {old_err}", file=sys.stderr)
        return None

    new_results, new_err = query_prometheus(prom_url, new_query)
    if new_err:
        print(f"  ❌ 查詢新規則失敗: {new_err}", file=sys.stderr)
        return None

    old_map = extract_value_map(old_results, group_by="tenant")
    new_map = extract_value_map(new_results, group_by="tenant")

    diffs = compare_vectors(old_map, new_map)
    return {
        "label": label,
        "old_query": old_query,
        "new_query": new_query,
        "old_count": len(old_results),
        "new_count": len(new_results),
        "diffs": diffs,
    }


def load_mapping_pairs(mapping_path):
    """從 prefix-mapping.yaml 載入比對組。"""
    with open(mapping_path, 'r', encoding='utf-8') as f:
        mapping = yaml.safe_load(f) or {}

    pairs = []
    for prefixed_key, info in mapping.items():
        original = info.get("original_metric")
        if not original:
            continue
        # 構造新舊 Recording Rule 名稱
        # 舊: 原始 metric name (直接查詢)
        # 新: tenant:<prefixed_key>:<agg> (需猜測 agg，但 recording rule 已存在)
        pairs.append({
            "label": prefixed_key,
            "old_query": original,
            "new_query": f"tenant:{prefixed_key}:max",  # 預設 max，使用者可在 CSV 中修改
            "alert_name": info.get("alert_name", ""),
            "golden_match": info.get("golden_match"),
        })
    return pairs


def print_summary(all_results):
    """印出比對摘要。"""
    total_pairs = len(all_results)
    total_matches = 0
    total_mismatches = 0
    total_missing = 0

    for result in all_results:
        if result is None:
            continue
        for d in result["diffs"]:
            if d["status"] == "match":
                total_matches += 1
            elif d["status"] == "mismatch":
                total_mismatches += 1
            elif d["status"] in ("old_missing", "new_missing"):
                total_missing += 1

    print(f"\n{'='*60}")
    print("📊 驗證摘要 (Validation Summary)")
    print(f"{'='*60}\n")
    print(f"比對組數: {total_pairs}")
    print(f"  ✅ 數值一致: {total_matches}")
    print(f"  ❌ 數值差異: {total_mismatches}")
    print(f"  ⚠️  缺少資料: {total_missing}\n")

    if total_mismatches == 0 and total_missing == 0:
        print("🎉 所有 Recording Rule 數值完全一致！可以安全切換。\n")
    elif total_mismatches > 0:
        print("⚠️  發現數值差異，請檢查以下項目:\n")
        for result in all_results:
            if result is None:
                continue
            for d in result["diffs"]:
                if d["status"] == "mismatch":
                    print(f"  • [{result['label']}] tenant={d['tenant']}: "
                          f"舊={d['old_value']} 新={d['new_value']} "
                          f"(差異={d['delta']:.4f})")
        print()


def write_csv_report(all_results, output_dir):
    """將比對結果寫入 CSV。"""
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "validation-report.csv")

    buf = io.StringIO()
    # lineterminator='\n' — write_text_secure opens in text mode, which on
    # Windows translates each \n → \r\n. csv.writer's default \r\n would then
    # become \r\r\n on disk, producing phantom blank rows when downstream tools
    # use universal-newlines reading. Pin \n here so the OS does the only
    # translation.
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow([
        "Label", "Tenant", "Old Query", "New Query",
        "Old Value", "New Value", "Delta", "Status",
        "Timestamp",
    ])
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    for result in all_results:
        if result is None:
            continue
        for d in result["diffs"]:
            writer.writerow([
                result["label"],
                d["tenant"],
                result["old_query"],
                result["new_query"],
                d["old_value"],
                d["new_value"],
                d["delta"],
                d["status"],
                ts,
            ])
    write_text_secure(csv_path, "\ufeff" + buf.getvalue())
    return csv_path


class ConvergenceTracker:
    """Track metric pair convergence across watch rounds.

    Records per-pair status ("match" / "mixed" / "error") each round.
    Reports cutover readiness when all pairs are stable for N consecutive rounds.
    """

    def __init__(self, stability_window=5):
        self.stability_window = stability_window
        self.pair_history = {}  # {label: [status, status, ...]}
        self.round_count = 0

    def record_round(self, all_results):
        """Record results from one polling round."""
        self.round_count += 1
        for result in all_results:
            if result is None:
                continue
            label = result["label"]
            statuses = [d["status"] for d in result["diffs"]]
            if all(s == "match" for s in statuses):
                agg = "match"
            elif any(s == "mismatch" for s in statuses):
                agg = "mismatch"
            else:
                agg = "mixed"  # missing or empty
            self.pair_history.setdefault(label, []).append(agg)

    def is_converged(self, label):
        """Check if a single pair has been stable for stability_window rounds."""
        history = self.pair_history.get(label, [])
        if len(history) < self.stability_window:
            return False
        recent = history[-self.stability_window:]
        return all(s == "match" for s in recent)

    def compute_report(self):
        """Return cutover readiness assessment."""
        if self.round_count < 2:
            return {
                "ready": False,
                "reason": f"Insufficient rounds ({self.round_count}, need >= 2)",
                "round_count": self.round_count,
            }

        total = len(self.pair_history)
        if total == 0:
            return {"ready": False, "reason": "No pairs tracked", "round_count": self.round_count}

        converged = []
        unconverged = []
        for label in sorted(self.pair_history):
            if self.is_converged(label):
                converged.append(label)
            else:
                unconverged.append(label)

        pct = 100.0 * len(converged) / total
        ready = len(unconverged) == 0

        return {
            "ready": ready,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "convergence_percentage": round(pct, 1),
            "converged_count": len(converged),
            "total_pairs": total,
            "converged_pairs": converged,
            "unconverged_pairs": unconverged,
            "round_count": self.round_count,
            "stability_window": self.stability_window,
            "recommendation": "Safe to cutover" if ready
                else f"{len(unconverged)} pair(s) not yet stable",
        }

    def print_status(self):
        """Print convergence status to stdout."""
        report = self.compute_report()
        if "convergence_percentage" not in report:
            # Early rounds or no pairs — minimal output
            print(f"\n  Convergence: {report.get('reason', 'pending')}")
            return report
        pct = report["convergence_percentage"]
        conv = report["converged_count"]
        total = report["total_pairs"]
        print(f"\n  Convergence: {conv}/{total} pairs stable ({pct:.0f}%)")
        if report["unconverged_pairs"]:
            print(f"  Unconverged: {', '.join(report['unconverged_pairs'])}")
        if report["ready"]:
            print("\n  *** CUTOVER READY ***")
        return report


def main():
    """CLI entry point: Shadow Monitoring 驗證工具。."""
    parser = argparse.ArgumentParser(
        description="Shadow Monitoring 驗證工具 — 比對新舊 Recording Rule 數值",
    )

    # 比對來源
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mapping", help="prefix-mapping.yaml 檔案路徑 (批次比對)")
    group.add_argument("--old", help="舊 Recording Rule 的 PromQL 查詢")

    parser.add_argument("--new", help="新 Recording Rule 的 PromQL 查詢 (搭配 --old)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus Query API URL "
                             "(預設: http://localhost:9090; "
                             "叢集內建議用 http://prometheus.monitoring.svc.cluster.local:9090)")
    parser.add_argument("-o", "--output-dir", default="validation_output",
                        help="輸出目錄 (預設: validation_output)")
    parser.add_argument("--tolerance", type=float, default=0.001,
                        help="數值誤差容忍度 (預設: 0.001 = 0.1%%)")
    parser.add_argument("--watch", action="store_true",
                        help="持續監控模式")
    parser.add_argument("--interval", type=int, default=60,
                        help="監控間隔秒數 (預設: 60)")
    parser.add_argument("--rounds", type=int, default=10,
                        help="監控輪數 (預設: 10)")
    parser.add_argument("--auto-detect-convergence", action="store_true",
                        help="Track convergence across rounds; auto-stop when cutover ready")
    parser.add_argument("--stability-window", type=int, default=5,
                        help="Consecutive match rounds required for convergence (default: 5)")
    parser.add_argument("--convergence-output",
                        help="Write cutover readiness report JSON to file")

    args = parser.parse_args()

    # 構造比對組
    pairs = []
    if args.mapping:
        pairs = load_mapping_pairs(args.mapping)
        print(f"📂 載入 {len(pairs)} 組比對自 {args.mapping}")
    elif args.old and args.new:
        pairs = [{
            "label": "manual",
            "old_query": args.old,
            "new_query": args.new,
        }]
    else:
        print("錯誤: 使用 --old 時必須同時指定 --new", file=sys.stderr)
        sys.exit(1)

    if not pairs:
        print("No comparison pairs found.")
        return

    def run_once():
        all_results = []
        for pair in pairs:
            print(f"  🔍 比對: {pair['label']}...")
            result = run_single_comparison(
                args.prometheus,
                pair["old_query"],
                pair["new_query"],
                pair["label"],
            )
            all_results.append(result)
        return all_results

    if args.watch:
        tracker = None
        if args.auto_detect_convergence:
            tracker = ConvergenceTracker(stability_window=args.stability_window)

        print(f"\n  Watch mode: every {args.interval}s, up to {args.rounds} rounds")
        if tracker:
            print(f"  Convergence detection: stability window = {args.stability_window} rounds\n")

        csv_path = None
        for i in range(args.rounds):
            print(f"\n--- Round {i+1}/{args.rounds} ({time.strftime('%H:%M:%S')}) ---")
            all_results = run_once()
            print_summary(all_results)
            csv_path = write_csv_report(all_results, args.output_dir)

            if tracker:
                tracker.record_round(all_results)
                report = tracker.print_status()
                if report["ready"]:
                    # Write convergence report and stop
                    conv_path = args.convergence_output or os.path.join(
                        args.output_dir, "cutover-readiness.json"
                    )
                    os.makedirs(os.path.dirname(conv_path) if os.path.dirname(conv_path) else ".", exist_ok=True)
                    write_json_secure(conv_path, report)
                    print(f"\n  Cutover readiness report: {conv_path}")
                    break

            if i < args.rounds - 1:
                time.sleep(args.interval)

        if tracker and not tracker.compute_report()["ready"]:
            print(f"\n  Watch completed ({args.rounds} rounds) without full convergence.")
            final = tracker.compute_report()
            conv_path = args.convergence_output or os.path.join(
                args.output_dir, "cutover-readiness.json"
            )
            os.makedirs(os.path.dirname(conv_path) if os.path.dirname(conv_path) else ".", exist_ok=True)
            write_json_secure(conv_path, final)
            print(f"  Partial convergence report: {conv_path}")

        if csv_path:
            print(f"\n  CSV report: {csv_path}")
    else:
        all_results = run_once()
        print_summary(all_results)
        csv_path = write_csv_report(all_results, args.output_dir)
        print(f"📁 CSV 報告: {csv_path}")


if __name__ == "__main__":
    main()
