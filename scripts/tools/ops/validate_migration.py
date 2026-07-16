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
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import http_get_json, write_text_secure, write_json_secure, query_prometheus_instant, add_prometheus_arg  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

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
    """印出比對摘要。

    r3 W2 修正：查詢失敗的 pair（result 為 None）此前被 continue 靜默跳過
    ——「查詢全失敗」時 mismatches/missing 均 0，照樣印 🎉「可以安全切換」
    但 exit 2，摘要與 exit code 自相矛盾。現統計 None 數：任一查詢失敗即
    印警示、抑制 🎉（未驗證 ≠ 一致）。
    """
    total_pairs = len(all_results)
    total_matches = 0
    total_mismatches = 0
    total_missing = 0
    failed_queries = sum(1 for r in all_results if r is None)

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

    if failed_queries:
        print(f"⚠️  {failed_queries}/{total_pairs} 組查詢失敗"
              "（Prometheus 連線 / 查詢層錯誤），本輪結果不完整——"
              "未驗證的比對組不得視為一致。\n")
    if total_mismatches == 0 and total_missing == 0 and failed_queries == 0:
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


def classify_results(all_results):
    """將一輪比對結果歸類為 exit-code 訊號（README §6.3 / _lib_exitcodes 契約）。

    回傳 (has_finding, has_query_error)：
      - has_finding: 任一 diff 為 mismatch / old_missing / new_missing。
        口徑刻意與 print_summary 的計數一致（both_empty 不計）。
      - has_query_error: 任一 pair 查詢失敗（run_single_comparison 回傳
        None——Prometheus 連線 / 查詢層錯誤）。沿用姊妹工具
        shadow_verify.py 的 #452/#737 歸類：unreachable Prometheus /
        query failure 屬 caller error（exit 2，system-actionable），
        不是 violation。（r3 W2 起 print_summary 也統計 None：任一
        查詢失敗即印警示並抑制 🎉，摘要與 exit code 不再矛盾。）
    """
    has_finding = False
    has_query_error = False
    for result in all_results:
        if result is None:
            has_query_error = True
            continue
        for d in result["diffs"]:
            if d["status"] in ("mismatch", "old_missing", "new_missing"):
                has_finding = True
    return has_finding, has_query_error


def write_csv_report(all_results, output_dir):
    """將比對結果寫入 CSV。"""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = str(out_dir / "validation-report.csv")

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
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Shadow Monitoring 驗證工具 — 比對新舊 Recording Rule 數值",
    )

    # 比對來源
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mapping", help="prefix-mapping.yaml 檔案路徑 (批次比對)")
    group.add_argument("--old", help="舊 Recording Rule 的 PromQL 查詢")

    parser.add_argument("--new", help="新 Recording Rule 的 PromQL 查詢 (搭配 --old)")
    add_prometheus_arg(parser,
                       help_text="Prometheus Query API URL "
                                 "(預設: $PROMETHEUS_URL，否則 http://localhost:9090; "
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
        sys.exit(EXIT_CALLER_ERROR)

    if not pairs:
        # r3 W2（沿 #452/#737 系譜）：零比對組 = 什麼都沒驗證，vacuous pass
        # 不得綠燈放行 promote（`da-tools validate && promote` 會誤過）。
        # 空 mapping 屬 caller 可修的輸入問題 → EXIT_CALLER_ERROR，訊息走
        # stderr（stdout 留給驗證結果）。
        print("No comparison pairs found.", file=sys.stderr)
        return EXIT_CALLER_ERROR

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
        all_results = []
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
                    conv_path = args.convergence_output or str(
                        Path(args.output_dir) / "cutover-readiness.json"
                    )
                    parent = Path(conv_path).parent
                    parent.mkdir(parents=True, exist_ok=True)
                    write_json_secure(conv_path, report)
                    print(f"\n  Cutover readiness report: {conv_path}")
                    break

            if i < args.rounds - 1:
                time.sleep(args.interval)

        if tracker and not tracker.compute_report()["ready"]:
            print(f"\n  Watch completed ({args.rounds} rounds) without full convergence.")
            final = tracker.compute_report()
            conv_path = args.convergence_output or str(
                Path(args.output_dir) / "cutover-readiness.json"
            )
            parent = Path(conv_path).parent
            parent.mkdir(parents=True, exist_ok=True)
            write_json_secure(conv_path, final)
            print(f"  Partial convergence report: {conv_path}")

        if csv_path:
            print(f"\n  CSV report: {csv_path}")

        # Exit 語意（watch 以「收斂／最後一輪」判定；README §6.3）：
        #   - --auto-detect-convergence 且 ready → EXIT_OK。收斂（連續
        #     stability_window 輪全 match）即 shadow monitoring 成功；
        #     早期輪的 mismatch 是等待收斂的常態，不算 violation。
        #   - 有 tracker 但未收斂（跑滿 rounds 仍不穩定）→ EXIT_VIOLATION，
        #     即使最後一輪碰巧全 match——單輪乾淨不足以推翻「未達
        #     stability window」的判定。
        #   - 無 tracker → 以最後一輪為準：有 mismatch / missing →
        #     EXIT_VIOLATION，全 match → EXIT_OK。
        #   - 判定輪（最後一輪）有查詢層失敗 → EXIT_CALLER_ERROR，優先於
        #     violation（對齊 shadow_verify.py）；已收斂則不看——收斂本身
        #     已證明查詢可用且數值穩定。
        if tracker is not None and tracker.compute_report()["ready"]:
            return EXIT_OK
        has_finding, has_query_error = classify_results(all_results)
        if has_query_error:
            return EXIT_CALLER_ERROR
        if tracker is not None:
            return EXIT_VIOLATION  # 未收斂
        return EXIT_VIOLATION if has_finding else EXIT_OK
    else:
        all_results = run_once()
        print_summary(all_results)
        csv_path = write_csv_report(all_results, args.output_dir)
        print(f"📁 CSV 報告: {csv_path}")

        # Exit 語意（單次模式；README §6.3——修 silent-pass：mismatch /
        # missing 此前一路 exit 0，客戶 cutover CI 的
        # `da-tools validate && promote` 會在數值不符時誤放行）：
        # 查詢層失敗 → EXIT_CALLER_ERROR（優先，對齊 shadow_verify.py）；
        # mismatch / missing → EXIT_VIOLATION；全 match → EXIT_OK。
        has_finding, has_query_error = classify_results(all_results)
        if has_query_error:
            return EXIT_CALLER_ERROR
        return EXIT_VIOLATION if has_finding else EXIT_OK


if __name__ == "__main__":
    # entrypoint.py 以 exec_module(__main__) 執行本檔，exit code 只能靠
    # SystemExit 上傳——裸 main() 會把回傳值丟掉（正是 silent-pass bug 的
    # 後半段）。main() 回傳 int、這裡統一轉成 process exit code。
    sys.exit(main())
