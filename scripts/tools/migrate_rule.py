#!/usr/bin/env python3
"""migrate_rule.py — 傳統 Prometheus 警報規則遷移輔助工具 (v2)。

自動將傳統的 PromQL (寫死數值) 轉換為本專案的「動態多租戶」三件套：
1. Tenant ConfigMap YAML    → migration_output/tenant-config.yaml
2. 平台 Recording Rule      → migration_output/platform-recording-rules.yaml
3. 平台動態 Alert Rule      → migration_output/platform-alert-rules.yaml
4. 遷移報告                 → migration_output/migration-report.txt

用法:
  python3 migrate_rule.py <legacy_rules.yml>                    # 預設檔案輸出
  python3 migrate_rule.py <legacy_rules.yml> --dry-run          # 僅顯示報告，不產生檔案
  python3 migrate_rule.py <legacy_rules.yml> --interactive      # 遇到不確定時詢問使用者
  python3 migrate_rule.py <legacy_rules.yml> -o /custom/path    # 自訂輸出目錄

Phase 3A 升級:
  - 智能聚合猜測 (Heuristics): 自動判斷 sum/max，減少 90%+ 人工介入
  - 檔案化輸出: 分離的 YAML 檔案，可直接 kubectl apply
  - --dry-run: 預覽模式
  - --interactive: 互動確認模式
"""

import sys
import re
import os
import argparse
import yaml


# ============================================================
# Heuristics: 智能聚合猜測
# ============================================================

def guess_aggregation(base_key, expr_str):
    """根據 metric 名稱和 PromQL 表達式智能猜測聚合模式。

    回傳: (mode, reason) — mode 為 "sum" 或 "max"，reason 為推理說明。
    """
    expr_lower = expr_str.lower()
    key_lower = base_key.lower()

    # Rule 1: rate() / increase() / irate() → sum (叢集總量)
    if re.search(r'\b(rate|increase|irate)\s*\(', expr_lower):
        return "sum", "包含 rate/increase — 叢集聚合總量"

    # Rule 2: _total 後綴 (Prometheus counter 命名慣例) → sum
    if key_lower.endswith('_total'):
        return "sum", "Counter 命名慣例 (_total) — 叢集聚合總量"

    # Rule 3: 包含百分比/比率/延遲/落後 → max (最弱環節)
    ratio_keywords = ('percent', 'ratio', 'lag', 'latency', 'delay',
                      'utilization', 'usage', 'saturation')
    for kw in ratio_keywords:
        if kw in key_lower:
            return "max", f"關鍵字 '{kw}' — 最弱環節 (單點瓶頸)"

    # Rule 4: 包含 total/bytes/count → sum (累積量)
    sum_keywords = ('total', 'bytes', 'count', 'size', 'sent', 'received',
                    'evicted', 'expired', 'rejected', 'errors', 'requests')
    for kw in sum_keywords:
        if kw in key_lower:
            return "sum", f"關鍵字 '{kw}' — 叢集累積量"

    # Rule 5: 包含除法 → max (通常是 ratio/percent 計算)
    if '/' in expr_str:
        return "max", "包含除法運算 — 通常為比率計算"

    # Rule 6: 連線數、佇列長度等 → max (單點上限)
    max_keywords = ('connections', 'connected', 'clients', 'threads',
                    'queue', 'replication', 'slave', 'replica')
    for kw in max_keywords:
        if kw in key_lower:
            return "max", f"關鍵字 '{kw}' — 單點上限"

    # Fallback → max (保障單點安全)
    return "max", "預設 Fallback — 保障單點安全"


# ============================================================
# PromQL 解析器
# ============================================================

# 語義不可轉換的 PromQL 函式
SEMANTIC_BREAK_FUNCS = frozenset({
    'absent', 'absent_over_time', 'vector', 'scalar',
    'predict_linear', 'holt_winters', 'label_replace', 'label_join'
})

# PromQL 內建函式/關鍵字 (用於跳過，找到真正的 metric 名稱)
PROMQL_FUNCS = frozenset({
    'abs', 'absent', 'absent_over_time', 'avg', 'avg_over_time',
    'ceil', 'changes', 'clamp', 'clamp_max', 'clamp_min', 'count',
    'count_over_time', 'day_of_month', 'day_of_week', 'days_in_month',
    'delta', 'deriv', 'exp', 'floor', 'group', 'histogram_quantile',
    'holt_winters', 'hour', 'idelta', 'increase', 'irate', 'label_join',
    'label_replace', 'last_over_time', 'ln', 'log2', 'log10', 'max',
    'max_over_time', 'min', 'min_over_time', 'minute', 'month',
    'predict_linear', 'quantile', 'quantile_over_time', 'rate', 'resets',
    'round', 'scalar', 'sgn', 'sort', 'sort_desc', 'sqrt', 'stddev',
    'stddev_over_time', 'stdvar', 'stdvar_over_time', 'sum',
    'sum_over_time', 'time', 'timestamp', 'vector', 'year',
    'by', 'without', 'on', 'ignoring', 'group_left', 'group_right', 'bool',
})


def extract_label_matchers(expr_str):
    """從 PromQL 表達式中提取 label matchers (如 {queue="tasks", db="0"})。

    回傳: list of dict，每個 dict = {"metric": str, "labels": dict}
    用於 Phase 2B 維度標籤提示。
    """
    results = []
    pattern = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\{([^}]+)\}')
    for m in pattern.finditer(expr_str):
        metric = m.group(1)
        labels_str = m.group(2)
        labels = {}
        for pair in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(!?=~?)\s*"([^"]*)"', labels_str):
            lk, op, lv = pair.group(1), pair.group(2), pair.group(3)
            if lk in ('job', 'instance', '__name__', 'namespace', 'pod', 'container'):
                continue
            if op != '=':
                continue
            labels[lk] = lv
        if labels:
            results.append({"metric": metric, "labels": labels})
    return results


def parse_expr(expr_str):
    """解析 PromQL 表達式，嘗試切分為 LHS, Operator, RHS (閾值數值)。"""
    match = re.match(
        r'^\s*(.*?)\s*(==|!=|>=|<=|>|<)\s*([0-9.]+(?:[eE][+-]?[0-9]+)?)\s*$',
        expr_str
    )
    if not match:
        return None

    lhs, op, rhs = match.groups()

    # 語義不可轉換的函式 → 交由 LLM Fallback
    first_func = re.match(r'\s*([a-zA-Z_]+)\s*\(', lhs)
    if first_func and first_func.group(1) in SEMANTIC_BREAK_FUNCS:
        return None

    is_complex = bool(re.search(r'[\(\)\[\]/+\-*]', lhs))

    # 提取真正的 metric 名稱 (跳過函式名)
    base_key = "unknown_metric"
    for m in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)', lhs):
        if m.group(1) not in PROMQL_FUNCS:
            base_key = m.group(1)
            break

    return {
        "lhs": lhs.strip(),
        "op": op,
        "val": rhs,
        "is_complex": is_complex,
        "base_key": base_key,
    }


# ============================================================
# Rule 處理核心
# ============================================================

class MigrationResult:
    """單條規則的遷移結果。"""

    def __init__(self, alert_name, status, severity="warning"):
        self.alert_name = alert_name
        self.status = status  # "perfect" | "complex" | "unparseable"
        self.severity = severity

        # 三件套內容
        self.tenant_config = {}       # {metric_key: value}
        self.recording_rules = []     # list of dict (YAML-ready)
        self.alert_rules = []         # list of dict (YAML-ready)

        # 報告附加資訊
        self.agg_mode = None
        self.agg_reason = None
        self.dim_hints = []
        self.llm_prompt = None
        self.notes = []


def process_rule(rule, interactive=False):
    """處理單條傳統 Prometheus 規則，回傳 MigrationResult。"""
    alert_name = rule.get('alert')
    if not alert_name:
        return None

    expr = rule.get('expr', '')
    severity = rule.get('labels', {}).get('severity', 'warning')
    parsed = parse_expr(expr)

    # 情境 3: 無法解析
    if not parsed:
        result = MigrationResult(alert_name, "unparseable", severity)
        result.llm_prompt = (
            f"請將以下傳統 Prometheus Alert 轉換為本專案的動態多租戶架構：\n"
            f"要求：\n"
            f"1. 提取閾值並提供 threshold-config.yaml 範例。\n"
            f"2. 提供包含 sum/max by(tenant) 的 Recording Rule。\n"
            f"3. 提供套用 group_left 與 unless maintenance 邏輯的 Alert Rule。\n"
            f"4. 如有維度標籤 (如 queue, db, index)，請用 \"metric{{label=\\\"value\\\"}}\" 語法提供範例。\n\n"
            f"原始規則：\n{yaml.dump([rule], sort_keys=False)}"
        )
        return result

    metric_key = parsed["base_key"]
    metric_key_yaml = f"{metric_key}_critical" if severity == "critical" else metric_key

    # 智能猜測聚合模式
    agg_mode, agg_reason = guess_aggregation(parsed["base_key"], parsed["lhs"])

    # 互動模式: 複雜表達式時詢問使用者
    if interactive and parsed["is_complex"]:
        print(f"\n🔍 Alert: {alert_name}")
        print(f"   Expr: {expr}")
        print(f"   🤖 AI 猜測: {agg_mode} ({agg_reason})")
        choice = input(f"   選擇聚合模式 [s=sum / m=max / Enter=採用猜測]: ").strip().lower()
        if choice == 's':
            agg_mode = "sum"
            agg_reason = "使用者手動選擇"
        elif choice == 'm':
            agg_mode = "max"
            agg_reason = "使用者手動選擇"

    status = "complex" if parsed["is_complex"] else "perfect"
    result = MigrationResult(alert_name, status, severity)
    result.agg_mode = agg_mode
    result.agg_reason = agg_reason

    # 維度標籤提示
    result.dim_hints = extract_label_matchers(expr)

    # === 產出 1. Tenant Config ===
    result.tenant_config[metric_key_yaml] = parsed['val']

    # === 產出 2. Recording Rules ===
    result.recording_rules.append({
        "record": f"tenant:{metric_key}:{agg_mode}",
        "expr": f"{agg_mode} by(tenant) ({parsed['lhs']})",
    })
    result.recording_rules.append({
        "record": f"tenant:alert_threshold:{metric_key}",
        "expr": f'sum by(tenant) (user_threshold{{metric="{metric_key}", severity="{severity}"}})',
    })

    # === 產出 3. Alert Rule ===
    alert_rule = {
        "alert": alert_name,
        "expr": (
            f"(\n"
            f"  tenant:{metric_key}:{agg_mode}\n"
            f"  {parsed['op']} on(tenant) group_left\n"
            f"  tenant:alert_threshold:{metric_key}\n"
            f")\n"
            f'unless on(tenant) (user_state_filter{{filter="maintenance"}} == 1)'
        ),
    }
    if 'for' in rule:
        alert_rule['for'] = rule['for']
    if 'labels' in rule:
        alert_rule['labels'] = rule['labels']
    if 'annotations' in rule:
        alert_rule['annotations'] = rule['annotations']
    result.alert_rules.append(alert_rule)

    return result


# ============================================================
# 輸出引擎
# ============================================================

def write_outputs(results, output_dir):
    """將遷移結果寫入分離的 YAML 檔案。"""
    os.makedirs(output_dir, exist_ok=True)

    # --- tenant-config.yaml ---
    tenant_configs = {}
    for r in results:
        if r.status == "unparseable":
            continue
        for k, v in r.tenant_config.items():
            tenant_configs[k] = f'"{v}"'

    tenant_yaml = {
        "# 將以下 key-value 複製到對應的 tenant YAML (如 db-a.yaml)": None,
        "# 範例: tenants.db-a 區塊中加入這些 key": None,
    }
    # Write as plain YAML-ready snippets
    with open(os.path.join(output_dir, "tenant-config.yaml"), 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Tenant Config — 複製到 conf.d/<tenant>.yaml 的 tenants 區塊\n")
        f.write("# ============================================================\n")
        f.write("# 範例:\n")
        f.write("# tenants:\n")
        f.write("#   db-a:\n")
        for k, v in tenant_configs.items():
            f.write(f"#     {k}: {v}\n")
        f.write("#\n")
        f.write("# 以下為各規則提取的閾值:\n\n")
        for r in results:
            if r.status == "unparseable":
                continue
            f.write(f"# --- From: {r.alert_name} (severity: {r.severity}) ---\n")
            for k, v in r.tenant_config.items():
                f.write(f"{k}: \"{v}\"\n")
            if r.dim_hints:
                f.write("# 📐 維度標籤替代語法:\n")
                for hint in r.dim_hints:
                    label_pairs = ', '.join(f'{lk}="{lv}"' for lk, lv in hint["labels"].items())
                    dim_key = f'{r.tenant_config and list(r.tenant_config.keys())[0].split("_critical")[0] or "metric"}{{{label_pairs}}}'
                    f.write(f'# "{dim_key}": "{list(r.tenant_config.values())[0]}"\n')
            f.write("\n")

    # --- platform-recording-rules.yaml (kubectl apply -f ready) ---
    all_recording_rules = []
    for r in results:
        if r.status == "unparseable":
            continue
        for rr in r.recording_rules:
            rule_with_comment = dict(rr)
            if r.agg_mode and r.agg_reason:
                # Add heuristic annotation as a comment-like field
                rule_with_comment['_comment'] = f"🤖 AI 猜測: {r.agg_mode} — {r.agg_reason}"
            all_recording_rules.append(rule_with_comment)

    with open(os.path.join(output_dir, "platform-recording-rules.yaml"), 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Platform Recording Rules\n")
        f.write("# 加入 configmap-prometheus.yaml 的 recording rule group 中\n")
        f.write("# ============================================================\n\n")
        for r in results:
            if r.status == "unparseable":
                continue
            f.write(f"# --- {r.alert_name} ---\n")
            f.write(f"# 🤖 AI 猜測: {r.agg_mode} — {r.agg_reason}\n")
            for rr in r.recording_rules:
                f.write(yaml.dump([rr], sort_keys=False, allow_unicode=True, default_flow_style=False))
            f.write("\n")

    # --- platform-alert-rules.yaml (kubectl apply -f ready) ---
    with open(os.path.join(output_dir, "platform-alert-rules.yaml"), 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Platform Dynamic Alert Rules\n")
        f.write("# 加入 configmap-prometheus.yaml 的 alerting rule group 中\n")
        f.write("# ============================================================\n\n")
        for r in results:
            if r.status == "unparseable":
                continue
            f.write(f"# --- {r.alert_name} ---\n")
            alert_yaml = yaml.safe_dump(r.alert_rules, sort_keys=False, allow_unicode=True)
            f.write(alert_yaml)
            f.write("\n")

    # --- migration-report.txt ---
    perfect = [r for r in results if r.status == "perfect"]
    complex_rules = [r for r in results if r.status == "complex"]
    unparseable = [r for r in results if r.status == "unparseable"]

    with open(os.path.join(output_dir, "migration-report.txt"), 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("遷移報告 (Migration Report)\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"總規則數: {len(results)}\n")
        f.write(f"  ✅ 完美解析: {len(perfect)}\n")
        f.write(f"  ⚠️  複雜表達式 (已自動猜測): {len(complex_rules)}\n")
        f.write(f"  🚨 無法解析 (需 LLM 協助): {len(unparseable)}\n\n")

        if perfect:
            f.write("-" * 40 + "\n")
            f.write("✅ 完美解析的規則\n")
            f.write("-" * 40 + "\n")
            for r in perfect:
                f.write(f"  • {r.alert_name}: {r.agg_mode} ({r.agg_reason})\n")
            f.write("\n")

        if complex_rules:
            f.write("-" * 40 + "\n")
            f.write("⚠️  複雜表達式 — 已自動猜測聚合模式，建議人工確認\n")
            f.write("-" * 40 + "\n")
            for r in complex_rules:
                f.write(f"  • {r.alert_name}: {r.agg_mode} ({r.agg_reason})\n")
                if r.dim_hints:
                    f.write(f"    📐 維度標籤偵測: {r.dim_hints}\n")
            f.write("\n")

        if unparseable:
            f.write("-" * 40 + "\n")
            f.write("🚨 無法自動解析 — 請將以下 LLM Prompt 交給 Claude 處理\n")
            f.write("-" * 40 + "\n")
            for r in unparseable:
                f.write(f"\n### {r.alert_name} ###\n")
                f.write(r.llm_prompt)
                f.write("\n")

    return len(perfect), len(complex_rules), len(unparseable)


def print_dry_run(results):
    """Dry-run 模式: 僅在 STDOUT 輸出報告摘要。"""
    perfect = [r for r in results if r.status == "perfect"]
    complex_rules = [r for r in results if r.status == "complex"]
    unparseable = [r for r in results if r.status == "unparseable"]

    print(f"\n{'='*60}")
    print("🔍 Dry-Run 預覽 (不產生檔案)")
    print(f"{'='*60}\n")
    print(f"總規則數: {len(results)}")
    print(f"  ✅ 完美解析: {len(perfect)}")
    print(f"  ⚠️  複雜表達式 (自動猜測): {len(complex_rules)}")
    print(f"  🚨 無法解析 (需 LLM): {len(unparseable)}\n")

    for r in results:
        if r.status == "unparseable":
            print(f"  🚨 {r.alert_name}: 無法自動解析 (需 LLM 協助)")
        else:
            icon = "✅" if r.status == "perfect" else "⚠️"
            print(f"  {icon} {r.alert_name}: {r.agg_mode} — {r.agg_reason}")
            for k, v in r.tenant_config.items():
                print(f"     → {k}: \"{v}\"")
            if r.dim_hints:
                print(f"     📐 維度: {r.dim_hints}")
    print()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="傳統 Prometheus 警報規則遷移輔助工具 — 自動轉換為動態多租戶三件套"
    )
    parser.add_argument("input_file", help="傳統 Prometheus alert rules YAML 檔案")
    parser.add_argument("-o", "--output-dir", default="migration_output",
                        help="輸出目錄 (預設: migration_output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="僅顯示報告，不產生檔案")
    parser.add_argument("--interactive", action="store_true",
                        help="遇到複雜表達式時互動詢問聚合模式")
    args = parser.parse_args()

    try:
        with open(args.input_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"Error reading YAML file: {e}", file=sys.stderr)
        sys.exit(1)

    groups = data.get('groups', [])
    if not groups:
        print("No 'groups' found in YAML.")
        return

    # 處理所有規則
    results = []
    for group in groups:
        rules = group.get('rules', [])
        for rule in rules:
            result = process_rule(rule, interactive=args.interactive)
            if result:
                results.append(result)

    if not results:
        print("No alert rules found to process.")
        return

    # 輸出
    if args.dry_run:
        print_dry_run(results)
    else:
        n_perfect, n_complex, n_unparseable = write_outputs(results, args.output_dir)
        print(f"[✓] 成功解析 {n_perfect + n_complex} 條規則 "
              f"(✅ {n_perfect} 完美, ⚠️ {n_complex} 已猜測)")
        if n_unparseable:
            print(f"[!] {n_unparseable} 條需人工處理 (LLM Prompt 已寫入報告)")
        print(f"📁 檔案已輸出至 {args.output_dir}/")


if __name__ == "__main__":
    main()
