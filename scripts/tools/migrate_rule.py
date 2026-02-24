#!/usr/bin/env python3
"""migrate_rule.py — 傳統 Prometheus 警報規則遷移輔助工具。

自動將傳統的 PromQL (寫死數值) 轉換為本專案的「動態多租戶」三件套：
1. Tenant ConfigMap YAML
2. 平台 Recording Rule
3. 平台動態 Alert Rule

用法: python3 scripts/tools/migrate_rule.py <legacy_rules.yml>
"""

import sys
import re
import yaml

def parse_expr(expr_str):
    """
    解析 PromQL 表達式，嘗試切分為 LHS (左值), Operator (運算子), RHS (閾值數值)。
    """
    # 匹配模式：(左邊任意字串) (比較運算子) (數字/浮點數)
    match = re.match(r'^\s*(.*?)\s*(==|!=|>=|<=|>|<)\s*([0-9.]+(?:[eE][+-]?[0-9]+)?)\s*$', expr_str)
    
    if not match:
        return None
    
    lhs, op, rhs = match.groups()
    
    # 語義不可轉換的 PromQL 函式 — 這些函式改變了向量的根本語義，
    # 無法簡單套入 "max/sum by(tenant)" 正規化模式
    SEMANTIC_BREAK_FUNCS = {'absent', 'absent_over_time', 'vector', 'scalar',
                            'predict_linear', 'holt_winters', 'label_replace', 'label_join'}
    first_func = re.match(r'\s*([a-zA-Z_]+)\s*\(', lhs)
    if first_func and first_func.group(1) in SEMANTIC_BREAK_FUNCS:
        return None  # 交由情境 3 (LLM Fallback) 處理

    # 判斷複雜度：如果左邊包含括號、中括號或運算符，則為複雜表達式
    is_complex = bool(re.search(r'[\(\)\[\]/+\-*]', lhs))
    
    # 嘗試提取一個合適的 Metric Key 基礎名稱
    # 需要跳過 PromQL 函式名 (rate, absent, sum, avg, ...)，找到真正的 metric 名稱
    PROMQL_FUNCS = {
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
    }
    for m in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)', lhs):
        if m.group(1) not in PROMQL_FUNCS:
            base_key = m.group(1)
            break
    else:
        base_key = "unknown_metric"
    
    return {
        "lhs": lhs.strip(),
        "op": op,
        "val": rhs,
        "is_complex": is_complex,
        "base_key": base_key
    }

def process_rule(rule):
    alert_name = rule.get('alert')
    if not alert_name:
        return

    expr = rule.get('expr', '')
    severity = rule.get('labels', {}).get('severity', 'warning')
    
    print(f"\n{'='*60}")
    print(f"🔍 正在分析警報: {alert_name}")
    print(f"原始 Expr: {expr}")
    
    parsed = parse_expr(expr)
    
    # 🔴 情境 3: 無法解析 (Fallback to LLM)
    if not parsed:
        print("🚨 狀態: [無法自動解析]")
        print("原因: 此表達式不符合標準的 `指標 > 數值` 結構 (可能是 absent 等特殊函式)。")
        print("\n💡 建議解法: 請將以下 Prompt 複製給 LLM (如 Claude) 協助轉換：")
        print("-" * 40)
        print(f"請將以下傳統 Prometheus Alert 轉換為本專案的動態多租戶架構：\n"
              f"要求：\n"
              f"1. 提取閾值並提供 threshold-config.yaml 範例。\n"
              f"2. 提供包含 sum/max by(tenant) 的 Recording Rule。\n"
              f"3. 提供套用 group_left 與 unless maintenance 邏輯的 Alert Rule。\n\n"
              f"原始規則：\n{yaml.dump([rule], sort_keys=False)}")
        print("-" * 40)
        return

    # 決定 Metric Key (處理 critical 後綴)
    metric_key = parsed["base_key"]
    if severity == "critical":
        metric_key_yaml = f"{metric_key}_critical"
    else:
        metric_key_yaml = metric_key

    # 🟡 情境 2: 複雜表達式 (部分降級)
    if parsed["is_complex"]:
        print("⚠️ 狀態: [複雜表達式 - 需人工確認 Recording Rule聚合方式]")
    else:
        print("✅ 狀態: [完美解析]")

    print(f"提取閾值: {parsed['val']} (Severity: {severity})")
    
    # === 產出 1. Tenant Config ===
    print("\n--- 1. Tenant Config (交給租戶填寫至 db-*.yaml) ---")
    print(f"{metric_key_yaml}: \"{parsed['val']}\"")
    
    # === 產出 2. Recording Rule ===
    print("\n--- 2. Platform Recording Rule (加入平台 configmap-prometheus.yaml) ---")
    if parsed["is_complex"]:
        print(f"# ⚠️ TODO: 系統偵測到複雜運算。請人工決定使用 `sum by(tenant)` 或 `max by(tenant)`")
        print(f"- record: tenant:{metric_key}:normalized")
        print(f"  expr: max by(tenant) ({parsed['lhs']})  # <-- 請確認這行")
    else:
        print(f"- record: tenant:{metric_key}:max")
        print(f"  expr: max by(tenant) ({parsed['lhs']})")

    print(f"\n- record: tenant:alert_threshold:{metric_key}")
    print(f"  expr: sum by(tenant) (user_threshold{{metric=\"{metric_key}\", severity=\"{severity}\"}})")

    # === 產出 3. Alert Rule ===
    print("\n--- 3. Platform Dynamic Alert Rule (加入平台 configmap-prometheus.yaml) ---")
    
    new_rule = {
        "alert": alert_name,
        "expr": f"(\n  tenant:{metric_key}:{ 'normalized' if parsed['is_complex'] else 'max' }\n  {parsed['op']} on(tenant) group_left\n  tenant:alert_threshold:{metric_key}\n)\nunless on(tenant) (user_state_filter{{filter=\"maintenance\"}} == 1)",
    }
    
    # 保留原本的其他屬性
    if 'for' in rule: new_rule['for'] = rule['for']
    if 'labels' in rule: new_rule['labels'] = rule['labels']
    if 'annotations' in rule: new_rule['annotations'] = rule['annotations']
    
    # 輸出為 YAML 格式 (使用 safe_dump 避免產生奇怪的 python tags)
    yaml_str = yaml.safe_dump([new_rule], sort_keys=False, allow_unicode=True)
    # 將 expr 裡的字串強制轉為 Folded Block (|) 以增加可讀性
    yaml_str = yaml_str.replace("expr: '", "expr: |\n    ").replace("\\n", "\n    ").replace("'", "")
    print(yaml_str.strip())


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 migrate_rule.py <legacy-rules.yaml>")
        sys.exit(1)
        
    filepath = sys.argv[1]
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"Error reading YAML file: {e}")
        sys.exit(1)
        
    groups = data.get('groups', [])
    if not groups:
        print("No 'groups' found in YAML.")
        return
        
    for group in groups:
        rules = group.get('rules', [])
        for rule in rules:
            process_rule(rule)

if __name__ == "__main__":
    main()