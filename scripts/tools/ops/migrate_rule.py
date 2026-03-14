#!/usr/bin/env python3
"""migrate_rule.py — 傳統 Prometheus 警報規則遷移輔助工具 (v4 — AST Engine)。

自動將傳統的 PromQL (寫死數值) 轉換為本專案的「動態多租戶」三件套：
1. Tenant ConfigMap YAML    → migration_output/tenant-config.yaml
2. 平台 Recording Rule      → migration_output/platform-recording-rules.yaml
3. 平台動態 Alert Rule      → migration_output/platform-alert-rules.yaml
4. 遷移報告                 → migration_output/migration-report.txt
5. Triage CSV               → migration_output/triage-report.csv
6. Prefix Mapping           → migration_output/prefix-mapping.yaml

用法:
  python3 migrate_rule.py <legacy_rules.yml>                    # 預設檔案輸出
  python3 migrate_rule.py <legacy_rules.yml> --dry-run          # 僅顯示報告，不產生檔案
  python3 migrate_rule.py <legacy_rules.yml> --interactive      # 遇到不確定時詢問使用者
  python3 migrate_rule.py <legacy_rules.yml> -o /custom/path    # 自訂輸出目錄
  python3 migrate_rule.py <legacy_rules.yml> --triage           # Triage 模式: 只產出 CSV 分桶報告
  python3 migrate_rule.py <legacy_rules.yml> --no-prefix        # 停用 custom_ 前綴 (不建議)
  python3 migrate_rule.py <legacy_rules.yml> --no-ast           # 強制使用舊版 regex 引擎

v4 升級 (AST Engine — Phase 11):
  - promql-parser (Rust/PyO3) 取代 regex 進行 metric name 辨識
  - AST-Informed String Surgery: 精準 prefix 替換 + tenant label 注入
  - Reparse 驗證: 確保改寫後的 PromQL 仍然合法
  - Graceful degradation: promql-parser 不可用時自動降級為 regex
"""

import sys
import re
import os
import csv
import argparse
import yaml

# ---------------------------------------------------------------------------
# AST Engine: promql-parser (optional — graceful degradation)
# ---------------------------------------------------------------------------
try:
    import promql_parser
    HAS_AST = True
except ImportError:
    HAS_AST = False


# ============================================================
# AST Engine: PromQL AST 走訪與改寫
# ============================================================

def _walk_vector_selectors(node):
    """遞迴走訪 AST，yield 所有 VectorSelector 節點。

    支援的 AST 節點類型:
      BinaryExpr (.lhs, .rhs), ParenExpr (.expr), UnaryExpr (.expr),
      AggregateExpr (.expr), Call (.args), MatrixSelector (.vector_selector),
      SubqueryExpr (.expr), VectorSelector (leaf node)
    """
    tname = type(node).__name__
    if tname == 'VectorSelector':
        yield node
        return
    if tname == 'MatrixSelector':
        vs = getattr(node, 'vector_selector', None)
        if vs is not None:
            yield vs
        return
    # Recurse into known child attributes
    for attr in ('lhs', 'rhs', 'expr'):
        child = getattr(node, attr, None)
        if child is not None:
            yield from _walk_vector_selectors(child)
    # Function call arguments
    args = getattr(node, 'args', None)
    if args:
        for arg in args:
            yield from _walk_vector_selectors(arg)
    # AggregateExpr param (e.g. histogram_quantile(0.95, ...))
    param = getattr(node, 'param', None)
    if param is not None and type(param).__name__ not in ('NumberLiteral', 'StringLiteral'):
        yield from _walk_vector_selectors(param)


def extract_metrics_ast(expr_str):
    """使用 AST 精準提取 PromQL 中所有 metric 名稱。

    回傳: list of unique metric names (保留出現順序)。
    若 promql-parser 不可用或解析失敗，回傳空 list (呼叫端降級為 regex)。
    """
    if not HAS_AST:
        return []
    try:
        ast = promql_parser.parse(expr_str)
    except Exception:
        return []
    names = []
    for vs in _walk_vector_selectors(ast):
        name = vs.name
        if name and name not in names:
            names.append(name)
    return names


def extract_label_matchers_ast(expr_str):
    """使用 AST 提取每個 VectorSelector 的 label matchers。

    回傳: list of {"metric": str, "labels": dict}
    只保留「有意義」的維度標籤 (排除 job/instance/__name__/namespace/pod/container)。
    """
    if not HAS_AST:
        return []
    try:
        ast = promql_parser.parse(expr_str)
    except Exception:
        return []

    skip_labels = frozenset({'job', 'instance', '__name__', 'namespace', 'pod', 'container'})
    results = []
    for vs in _walk_vector_selectors(ast):
        name = vs.name or ''
        matchers_obj = vs.matchers
        if matchers_obj is None:
            continue
        inner = getattr(matchers_obj, 'matchers', [])
        labels = {}
        for m in inner:
            if m.name in skip_labels:
                continue
            # Only exact-match labels for dimension hints
            if str(m.op) == 'MatchOp.Equal':
                labels[m.name] = m.value
        if labels:
            results.append({"metric": name, "labels": labels})
    return results


def detect_semantic_break_ast(expr_str):
    """使用 AST 偵測語義不可轉換的函式 (absent, predict_linear 等)。

    回傳: True 如果表達式包含語義中斷函式。
    """
    if not HAS_AST:
        return False
    try:
        ast = promql_parser.parse(expr_str)
    except Exception:
        return False

    def _walk_calls(node):
        tname = type(node).__name__
        if tname == 'Call':
            func_obj = getattr(node, 'func', None)
            if func_obj is not None:
                fname = getattr(func_obj, 'name', '')
                if fname in SEMANTIC_BREAK_FUNCS:
                    return True
            # Walk into Call arguments (the only children of a Call node)
            args = getattr(node, 'args', [])
            if args:
                for arg in args:
                    if _walk_calls(arg):
                        return True
            return False
        # Non-Call nodes: walk known child attributes
        for attr in ('lhs', 'rhs', 'expr'):
            child = getattr(node, attr, None)
            if child is not None:
                if _walk_calls(child):
                    return True
        # AggregateExpr / other nodes with args
        args = getattr(node, 'args', None)
        if args:
            for arg in args:
                if _walk_calls(arg):
                    return True
        return False

    return _walk_calls(ast)


def rewrite_expr_prefix(expr_str, rename_map):
    """AST-Informed String Surgery: 精準替換 metric 名稱 (加 prefix)。

    rename_map: dict {old_name: new_name}
    使用 word-boundary regex，確保不誤改 label name 或子字串。
    改寫後 reparse 驗證；驗證失敗回傳原始字串。
    """
    result = expr_str
    for old_name, new_name in rename_map.items():
        if old_name == new_name:
            continue
        result = re.sub(r'\b' + re.escape(old_name) + r'\b', new_name, result)

    # Validate rewrite
    if HAS_AST:
        try:
            promql_parser.parse(result)
        except Exception:
            return expr_str  # 驗證失敗，回退原始
    return result


def rewrite_expr_tenant_label(expr_str, metric_names):
    """AST-Informed String Surgery: 注入 tenant label matcher。

    對每個 metric name:
      - 有 {...} 的: 在 { 後插入 tenant=~".+",
      - 無 label 的: 在 metric name 後附加 {tenant=~".+"}
    改寫後 reparse 驗證。

    Known limitation: 若同一 metric 同時有帶 label 和裸露的用法 (e.g.
    "my_metric > on() group_left my_metric{a="1"}")，if/else 分支只會套用
    帶 label 的 pattern，裸露出現不會被注入 tenant。Recording rule LHS 通常
    只有一種形式，此限制在實際遷移場景中影響極小。
    """
    result = expr_str
    for name in metric_names:
        # Pattern 1: metric{existing...} → metric{tenant=~".+",existing...}
        pattern_with_labels = r'\b' + re.escape(name) + r'\{'
        if re.search(pattern_with_labels, result):
            result = re.sub(
                pattern_with_labels,
                name + '{tenant=~".+",',
                result
            )
        else:
            # Pattern 2: bare metric name → metric{tenant=~".+"}
            # Negative lookahead: not followed by { or alphanumeric (substring)
            pattern_bare = r'\b' + re.escape(name) + r'(?![{a-zA-Z0-9_])'
            result = re.sub(pattern_bare, name + '{tenant=~".+"}', result)

    # Validate rewrite
    if HAS_AST:
        try:
            promql_parser.parse(result)
        except Exception:
            return expr_str  # 驗證失敗，回退原始
    return result


# ============================================================
# Metric Dictionary: 載入外部啟發式字典
# ============================================================

def load_metric_dictionary(script_dir=None):
    """載入 metric-dictionary.yaml 啟發式字典。"""
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    dict_path = os.path.join(script_dir, "metric-dictionary.yaml")
    if os.path.exists(dict_path):
        with open(dict_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}


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
    """從 PromQL 表達式中提取 label matchers。

    v4: 優先使用 AST 引擎，降級為 regex。
    回傳: list of dict，每個 dict = {"metric": str, "labels": dict}
    """
    # AST path
    ast_result = extract_label_matchers_ast(expr_str)
    if ast_result:
        return ast_result

    # Regex fallback (legacy)
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


def extract_all_metrics(expr_str):
    """從 PromQL 中提取所有 metric 名稱。

    v4: 優先使用 AST 引擎，降級為 regex + function blacklist。
    """
    # AST path
    ast_result = extract_metrics_ast(expr_str)
    if ast_result:
        return ast_result

    # Regex fallback (legacy)
    metrics = []
    for m in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)', expr_str):
        name = m.group(1)
        if name not in PROMQL_FUNCS and not name.isdigit():
            metrics.append(name)
    return list(dict.fromkeys(metrics))  # deduplicate, preserve order


def parse_expr(expr_str, use_ast=True):
    """解析 PromQL 表達式，嘗試切分為 LHS, Operator, RHS (閾值數值)。

    v4: 使用 AST 進行 metric name 辨識與語義中斷偵測。
    """
    match = re.match(
        r'^\s*(.*?)\s*(==|!=|>=|<=|>|<)\s*([0-9.]+(?:[eE][+-]?[0-9]+)?)\s*$',
        expr_str
    )
    if not match:
        return None

    lhs, op, rhs = match.groups()

    # 語義不可轉換的函式偵測
    if use_ast and HAS_AST and detect_semantic_break_ast(lhs):
        return None
    elif not use_ast or not HAS_AST:
        # Regex fallback — when AST is disabled or unavailable
        first_func = re.match(r'\s*([a-zA-Z_]+)\s*\(', lhs)
        if first_func and first_func.group(1) in SEMANTIC_BREAK_FUNCS:
            return None

    is_complex = bool(re.search(r'[\(\)\[\]/+\-*]', lhs))

    # v4: AST-based metric extraction (精準，不需 function blacklist)
    base_key = "unknown_metric"
    ast_metrics = extract_metrics_ast(lhs) if use_ast else []
    if ast_metrics:
        base_key = ast_metrics[0]

    # Regex fallback
    if base_key == "unknown_metric":
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
        "all_metrics": ast_metrics,
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

        # Auto-suppression 用
        self.op = None                # 比較運算子 (e.g., ">", "<")

        # 報告附加資訊
        self.agg_mode = None
        self.agg_reason = None
        self.dim_hints = []
        self.llm_prompt = None
        self.notes = []

        # v3: Dictionary match
        self.dict_match = None        # dict entry from metric-dictionary.yaml
        self.triage_action = None     # "auto" | "review" | "skip" | "use_golden"
        self.original_expr = ""


def lookup_dictionary(metric_name, dictionary):
    """在啟發式字典中查找 metric，回傳匹配條目或 None。"""
    if not dictionary:
        return None
    return dictionary.get(metric_name)


def process_rule(rule, interactive=False, prefix="custom_", dictionary=None,
                  use_ast=True):
    """處理單條傳統 Prometheus 規則，回傳 MigrationResult。

    v4: use_ast=True 啟用 AST 引擎進行精準 metric 辨識與表達式改寫。
    """
    alert_name = rule.get('alert')
    if not alert_name:
        return None

    expr = rule.get('expr', '')
    severity = rule.get('labels', {}).get('severity', 'warning')
    parsed = parse_expr(expr, use_ast=use_ast)

    # 情境 3: 無法解析
    if not parsed:
        result = MigrationResult(alert_name, "unparseable", severity)
        result.original_expr = expr
        result.triage_action = "skip"
        result.llm_prompt = (
            f"請將以下傳統 Prometheus Alert 轉換為本專案的動態多租戶架構：\n"
            f"要求：\n"
            f"1. 提取閾值並提供 threshold-config.yaml 範例。\n"
            f"2. 提供包含 sum/max by(tenant) 的 Recording Rule。\n"
            f"3. 提供套用 group_left 與 unless maintenance 邏輯的 Alert Rule。\n"
            f"4. 如有維度標籤 (如 queue, db, index)，請用 \"metric{{label=\\\"value\\\"}}\" 語法提供範例。\n\n"
            f"原始規則：\n{yaml.dump([rule], sort_keys=False)}"
        )
        # Check dictionary for guidance even on unparseable
        all_metrics = extract_all_metrics(expr)
        for m in all_metrics:
            match = lookup_dictionary(m, dictionary)
            if match and match.get("golden_rule"):
                result.dict_match = match
                result.triage_action = "use_golden"
                result.notes.append(
                    f"字典建議: {m} → 黃金標準 {match['golden_rule']} ({match.get('note', '')})"
                )
                break
        return result

    metric_key = parsed["base_key"]

    # v3: Dictionary lookup
    dict_match = lookup_dictionary(metric_key, dictionary)
    has_golden = dict_match and dict_match.get("golden_rule")

    # 決定 metric key (含 prefix 與 severity)
    if prefix and not has_golden:
        prefixed_key = f"{prefix}{metric_key}"
    else:
        prefixed_key = metric_key

    metric_key_yaml = f"{prefixed_key}_critical" if severity == "critical" else prefixed_key

    # 智能猜測聚合模式
    agg_mode, agg_reason = guess_aggregation(parsed["base_key"], parsed["lhs"])

    # 互動模式: 複雜表達式時詢問使用者
    if interactive and parsed["is_complex"]:
        print(f"\n🔍 Alert: {alert_name}")
        print(f"   Expr: {expr}")
        print(f"   🤖 AI 猜測: {agg_mode} ({agg_reason})")
        if has_golden:
            print(f"   📖 字典建議: 改用黃金標準 {dict_match['golden_rule']}")
        choice = input(f"   選擇聚合模式 [s=sum / m=max / Enter=採用猜測]: ").strip().lower()
        if choice == 's':
            agg_mode = "sum"
            agg_reason = "使用者手動選擇"
        elif choice == 'm':
            agg_mode = "max"
            agg_reason = "使用者手動選擇"

    status = "complex" if parsed["is_complex"] else "perfect"
    result = MigrationResult(alert_name, status, severity)
    result.op = parsed['op']
    result.agg_mode = agg_mode
    result.agg_reason = agg_reason
    result.original_expr = expr
    result.dict_match = dict_match

    # Triage action
    if has_golden:
        result.triage_action = "use_golden"
        result.notes.append(
            f"字典建議: {metric_key} → 黃金標準 {dict_match['golden_rule']} "
            f"(Rule Pack: {dict_match.get('rule_pack', 'unknown')})"
        )
    elif status == "perfect":
        result.triage_action = "auto"
    else:
        result.triage_action = "review"

    # 維度標籤提示
    result.dim_hints = extract_label_matchers(expr)

    # === 產出 1. Tenant Config ===
    result.tenant_config[metric_key_yaml] = parsed['val']

    # === 產出 2. Recording Rules ===
    record_name = f"tenant:{prefixed_key}:{agg_mode}"
    threshold_suffix = "_critical" if severity == "critical" else ""
    threshold_name = f"tenant:alert_threshold:{prefixed_key}{threshold_suffix}"

    # v4: AST-Informed String Surgery — 改寫 LHS 表達式
    recording_lhs = parsed['lhs']
    if use_ast and HAS_AST:
        all_metrics = parsed.get('all_metrics', [])
        # Step 1: Prefix injection (如果需要)
        if prefix and not has_golden and all_metrics:
            rename_map = {}
            for m_name in all_metrics:
                if not m_name.startswith(prefix):
                    rename_map[m_name] = f"{prefix}{m_name}"
            if rename_map:
                recording_lhs = rewrite_expr_prefix(recording_lhs, rename_map)
        # Step 2: Tenant label injection
        rewritten_metrics = extract_metrics_ast(recording_lhs) or all_metrics
        if rewritten_metrics:
            recording_lhs = rewrite_expr_tenant_label(recording_lhs, rewritten_metrics)

    result.recording_rules.append({
        "record": record_name,
        "expr": f"{agg_mode} by(tenant) ({recording_lhs})",
    })
    result.recording_rules.append({
        "record": threshold_name,
        "expr": f'max by(tenant) (user_threshold{{metric="{prefixed_key}", severity="{severity}"}})',
    })

    # === 產出 3. Alert Rule ===
    alert_prefix = f"Custom" if prefix else ""
    alert_rule = {
        "alert": f"{alert_prefix}{alert_name}" if prefix else alert_name,
        "expr": (
            f"(\n"
            f"  {record_name}\n"
            f"  {parsed['op']} on(tenant) group_left\n"
            f"  {threshold_name}\n"
            f")\n"
            f'unless on(tenant) (user_state_filter{{filter="maintenance"}} == 1)'
        ),
    }
    if 'for' in rule:
        alert_rule['for'] = rule['for']
    labels = dict(rule.get('labels', {}))
    if prefix:
        labels['source'] = 'legacy'
        labels['migration_status'] = 'shadow'
    if labels:
        alert_rule['labels'] = labels
    if 'annotations' in rule:
        alert_rule['annotations'] = rule['annotations']
    result.alert_rules.append(alert_rule)

    return result


# ============================================================
# Auto-Suppression: Warning ↔ Critical 配對
# ============================================================

def apply_auto_suppression(results):
    """配對 warning/critical 規則，為兩者加上 metric_group label。

    v1.2.0 起，Auto-Suppression 從 PromQL 層移至 Alertmanager 層：
    - Warning 和 Critical 都在 TSDB 留下紀錄（不再用 PromQL unless 消滅 warning）
    - Alertmanager inhibit_rules 依 tenant + metric_group 配對，壓制 warning 通知
    - Tenant 可設 _severity_dedup: "disable" 取消壓制，同時收到兩種通知

    修改 results 中 warning + critical MigrationResult 的 alert_rules labels（in-place）。
    回傳配對成功的數量。
    """
    # 建立 base_key → {severity: result} 映射
    pairs = {}  # base_key → {"warning": result, "critical": result}
    for r in results:
        if r.status == "unparseable" or r.triage_action == "use_golden":
            continue
        if not r.tenant_config:
            continue

        # 取出 metric_key_yaml (tenant_config 的第一個 key)
        metric_key_yaml = list(r.tenant_config.keys())[0]

        # 推導 base_key：critical 去掉 _critical 後綴
        if r.severity == "critical" and metric_key_yaml.endswith("_critical"):
            base_key = metric_key_yaml[: -len("_critical")]
        else:
            base_key = metric_key_yaml

        if base_key not in pairs:
            pairs[base_key] = {}
        pairs[base_key][r.severity] = r

    paired = 0
    for base_key, sev_map in pairs.items():
        warn_r = sev_map.get("warning")
        crit_r = sev_map.get("critical")
        if not warn_r or not crit_r:
            continue
        if not warn_r.alert_rules or len(crit_r.recording_rules) < 2:
            continue

        # 從 base_key 推導 metric_group（取最後一段作為 group name）
        # 例: mysql_connections → connections, container_cpu → cpu
        parts = base_key.split("_")
        metric_group = parts[-1] if parts else base_key

        # 為 warning 和 critical alert 加上 metric_group label
        for ar in warn_r.alert_rules:
            if "labels" not in ar:
                ar["labels"] = {}
            ar["labels"]["metric_group"] = metric_group

        for ar in crit_r.alert_rules:
            if "labels" not in ar:
                ar["labels"] = {}
            ar["labels"]["metric_group"] = metric_group

        warn_r.notes.append(
            f"Severity Dedup: 已配對 critical ({crit_r.alert_name})，"
            f"metric_group=\"{metric_group}\"。"
            f"Alertmanager inhibit 預設壓制 warning 通知 "
            f"(tenant 可設 _severity_dedup: \"disable\" 取消)"
        )
        paired += 1

    return paired


# ============================================================
# v3: Triage Mode — CSV 報告
# ============================================================

def write_triage_csv(results, output_dir, dictionary):
    """產出 CSV 分桶報告，供大規模遷移時在 Excel 中批次決策。"""
    csv_path = os.path.join(output_dir, "triage-report.csv")
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow([
            "Alert Name",
            "Triage Action",
            "Status",
            "Severity",
            "Metric Key",
            "Threshold",
            "Aggregation",
            "Aggregation Reason",
            "Golden Standard Match",
            "Golden Rule",
            "Rule Pack",
            "Dictionary Note",
            "Dimensions",
            "Original Expression",
        ])
        for r in results:
            golden_match = r.dict_match.get("maps_to", "") if r.dict_match else ""
            golden_rule = r.dict_match.get("golden_rule", "") if r.dict_match else ""
            rule_pack = r.dict_match.get("rule_pack", "") if r.dict_match else ""
            dict_note = r.dict_match.get("note", "") if r.dict_match else ""
            metric_keys = ", ".join(r.tenant_config.keys()) if r.tenant_config else ""
            thresholds = ", ".join(r.tenant_config.values()) if r.tenant_config else ""
            dims = "; ".join(str(d) for d in r.dim_hints) if r.dim_hints else ""

            writer.writerow([
                r.alert_name,
                r.triage_action or "unknown",
                r.status,
                r.severity,
                metric_keys,
                thresholds,
                r.agg_mode or "",
                r.agg_reason or "",
                golden_match,
                golden_rule,
                rule_pack,
                dict_note,
                dims,
                r.original_expr[:200],  # Truncate long exprs
            ])
    os.chmod(csv_path, 0o600)
    return csv_path


def write_prefix_mapping(results, output_dir, prefix):
    """產出 prefix mapping table，記錄 custom_ 前綴對應黃金標準的關係。"""
    if not prefix:
        return None

    mapping = {}
    for r in results:
        if r.status == "unparseable":
            continue
        for key in r.tenant_config.keys():
            original = key.replace(prefix, "", 1) if key.startswith(prefix) else key
            mapping[key] = {
                "original_metric": original,
                "alert_name": r.alert_name,
                "golden_match": r.dict_match.get("maps_to") if r.dict_match else None,
                "golden_rule": r.dict_match.get("golden_rule") if r.dict_match else None,
            }

    if not mapping:
        return None

    mapping_path = os.path.join(output_dir, "prefix-mapping.yaml")
    with open(mapping_path, 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Prefix Mapping Table — custom_ 前綴對應關係\n")
        f.write("# ============================================================\n")
        f.write("# 用途: 未來收斂至黃金標準時的對照表\n")
        f.write(f"# Prefix: {prefix}\n")
        f.write("# ============================================================\n\n")
        yaml.safe_dump(mapping, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.chmod(mapping_path, 0o600)
    return mapping_path


# ============================================================
# 輸出引擎
# ============================================================

def write_outputs(results, output_dir, prefix="custom_", dictionary=None):
    """將遷移結果寫入分離的 YAML 檔案 (含合法 YAML 結構)。"""
    os.makedirs(output_dir, exist_ok=True)

    # --- tenant-config.yaml (含 boilerplate 範例) ---
    tenant_configs = {}
    for r in results:
        if r.status == "unparseable":
            continue
        if r.triage_action == "use_golden":
            continue  # 建議使用黃金標準的不輸出到 tenant config
        for k, v in r.tenant_config.items():
            tenant_configs[k] = v

    tenant_config_path = os.path.join(output_dir, "tenant-config.yaml")
    with open(tenant_config_path, 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Tenant Config — 複製到 conf.d/<tenant>.yaml\n")
        f.write("# ============================================================\n")
        f.write("# 請將以下內容縮排並貼入您專屬的 tenant 設定中，例如：\n")
        f.write("# tenants:\n")
        f.write("#   my-tenant-name:\n")
        for k, v in tenant_configs.items():
            f.write(f'#     {k}: "{v}"\n')
        f.write("\n")
        for r in results:
            if r.status == "unparseable" or r.triage_action == "use_golden":
                continue
            f.write(f"# --- From: {r.alert_name} (severity: {r.severity}) ---\n")
            if r.notes:
                for note in r.notes:
                    f.write(f"# 📖 {note}\n")
            for k, v in r.tenant_config.items():
                f.write(f'{k}: "{v}"\n')
            if r.dim_hints:
                f.write("# 維度標籤替代語法:\n")
                for hint in r.dim_hints:
                    label_pairs = ', '.join(f'{lk}="{lv}"' for lk, lv in hint["labels"].items())
                    dim_key = f'{list(r.tenant_config.keys())[0].split("_critical")[0]}{{{label_pairs}}}'
                    f.write(f'# "{dim_key}": "{list(r.tenant_config.values())[0]}"\n')
            f.write("\n")
    os.chmod(tenant_config_path, 0o600)

    # --- platform-recording-rules.yaml (合法 YAML, 含 groups/rules 結構) ---
    # Deduplication: 追蹤已產出的 recording rule record 名稱
    seen_records = set()
    deduplicated_rules = []
    for r in results:
        if r.status == "unparseable" or r.triage_action == "use_golden":
            continue
        for rr in r.recording_rules:
            record_name = rr["record"]
            if record_name in seen_records:
                continue
            seen_records.add(record_name)
            deduplicated_rules.append((r, rr))

    # 計算收斂率
    total_input = len([r for r in results if r.status != "unparseable"])
    total_output = len(deduplicated_rules)

    group_name = f"{prefix}migrated-recording-rules" if prefix else "migrated-recording-rules"
    recording_rules_path = os.path.join(output_dir, "platform-recording-rules.yaml")
    with open(recording_rules_path, 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Platform Recording Rules — 可直接合併至 Prometheus ConfigMap\n")
        f.write("# ============================================================\n")
        if total_input > 0:
            compression = round((1 - total_output / max(total_input * 2, 1)) * 100, 1)
            f.write(f"# 收斂率: {total_input} 條規則 → {total_output} 條 Recording Rules")
            f.write(f" (壓縮 {compression}%)\n")
        f.write("# ============================================================\n\n")
        f.write("groups:\n")
        f.write(f"  - name: {group_name}\n")
        f.write("    rules:\n")
        for r, rr in deduplicated_rules:
            # 當聚合模式為 AI 猜測 (非使用者手動選擇) 時，插入醒目警告方塊
            if r.status == "complex" and r.agg_reason != "使用者手動選擇":
                f.write("      # ============================================================\n")
                f.write("      # 🚨🚨🚨 [AI 智能猜測注意] 🚨🚨🚨\n")
                f.write("      # ============================================================\n")
                f.write(f"      # 以下 recording rule 的聚合模式為 AI 自動猜測: {r.agg_mode}\n")
                f.write(f"      # 猜測原因: {r.agg_reason}\n")
                f.write(f"      # 原始 Alert: {r.alert_name}\n")
                f.write("      #\n")
                f.write("      # ⚠️  請在複製貼上前確認:\n")
                f.write(f"      #   - 聚合模式 {r.agg_mode} 是否正確? (sum=叢集總量, max=單點瓶頸)\n")
                f.write("      #   - 如不確定，請用 --interactive 模式重新執行\n")
                f.write("      # ============================================================\n")
            else:
                f.write(f"      # {r.alert_name} | {r.agg_mode} — {r.agg_reason}\n")
            f.write(f"      - record: {rr['record']}\n")
            f.write(f"        expr: {rr['expr']}\n")
            f.write("\n")
    os.chmod(recording_rules_path, 0o600)

    # --- platform-alert-rules.yaml (合法 YAML, 含 groups/rules 結構) ---
    alert_group_name = f"{prefix}migrated-alert-rules" if prefix else "migrated-alert-rules"
    alert_rules_path = os.path.join(output_dir, "platform-alert-rules.yaml")
    with open(alert_rules_path, 'w', encoding='utf-8') as f:
        f.write("# ============================================================\n")
        f.write("# Platform Dynamic Alert Rules — 可直接合併至 Prometheus ConfigMap\n")
        f.write("# ============================================================\n")
        f.write("groups:\n")
        f.write(f"  - name: {alert_group_name}\n")
        f.write("    rules:\n")
        for r in results:
            if r.status == "unparseable" or r.triage_action == "use_golden":
                continue
            f.write(f"      # --- {r.alert_name} ---\n")
            # Write alert rule with proper indentation
            for ar in r.alert_rules:
                f.write(f"      - alert: {ar['alert']}\n")
                # Multiline expr — use YAML literal block
                f.write(f"        expr: |\n")
                for line in ar['expr'].strip().split('\n'):
                    f.write(f"          {line}\n")
                if 'for' in ar:
                    f.write(f"        for: {ar['for']}\n")
                if 'labels' in ar:
                    f.write(f"        labels:\n")
                    for lk, lv in ar['labels'].items():
                        f.write(f"          {lk}: {lv}\n")
                if 'annotations' in ar:
                    f.write(f"        annotations:\n")
                    for ak, av in ar['annotations'].items():
                        f.write(f"          {ak}: \"{av}\"\n")
            f.write("\n")
    os.chmod(alert_rules_path, 0o600)

    # --- migration-report.txt ---
    perfect = [r for r in results if r.status == "perfect"]
    complex_rules = [r for r in results if r.status == "complex"]
    unparseable = [r for r in results if r.status == "unparseable"]
    golden_matches = [r for r in results if r.triage_action == "use_golden"]

    report_path = os.path.join(output_dir, "migration-report.txt")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        engine = "AST" if HAS_AST else "regex"
        f.write(f"遷移報告 (Migration Report) — v4 ({engine} engine)\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"總規則數: {len(results)}\n")
        f.write(f"  ✅ 完美解析: {len(perfect)}\n")
        f.write(f"  ⚠️  複雜表達式 (已自動猜測): {len(complex_rules)}\n")
        f.write(f"  🚨 無法解析 (需 LLM 協助): {len(unparseable)}\n")
        f.write(f"  📖 建議使用黃金標準: {len(golden_matches)}\n\n")

        # 收斂率統計 — 排除 unparseable 的 golden matches 避免多扣
        golden_parseable = len([r for r in results
                                if r.triage_action == "use_golden"
                                and r.status != "unparseable"])
        convertible = len(perfect) + len(complex_rules) - golden_parseable
        if convertible > 0:
            f.write(f"📊 收斂率統計:\n")
            f.write(f"  輸入: {len(results)} 條傳統規則\n")
            f.write(f"  輸出: {total_output} 條 Recording Rules "
                    f"+ {convertible} 條 Alert Rules\n")
            if total_input > 0:
                compression = round((1 - total_output / max(total_input * 2, 1)) * 100, 1)
                f.write(f"  壓縮率: {compression}%\n")
            f.write("\n")

        if golden_matches:
            f.write("-" * 40 + "\n")
            f.write("📖 建議使用黃金標準 — 請用 scaffold_tenant.py 設定閾值\n")
            f.write("-" * 40 + "\n")
            for r in golden_matches:
                golden = r.dict_match
                f.write(f"  • {r.alert_name}\n")
                f.write(f"    → 黃金標準: {golden.get('golden_rule', '?')}\n")
                f.write(f"    → Metric Key: {golden.get('maps_to', '?')}\n")
                f.write(f"    → Rule Pack: {golden.get('rule_pack', '?')}\n")
                f.write(f"    → {golden.get('note', '')}\n")
            f.write("\n")

        if perfect:
            f.write("-" * 40 + "\n")
            f.write("✅ 完美解析的規則\n")
            f.write("-" * 40 + "\n")
            for r in perfect:
                if r.triage_action == "use_golden":
                    continue
                f.write(f"  • {r.alert_name}: {r.agg_mode} ({r.agg_reason})\n")
            f.write("\n")

        if complex_rules:
            f.write("-" * 40 + "\n")
            f.write("⚠️  複雜表達式 — 已自動猜測聚合模式，建議人工確認\n")
            f.write("-" * 40 + "\n")
            for r in complex_rules:
                if r.triage_action == "use_golden":
                    continue
                f.write(f"  • {r.alert_name}: {r.agg_mode} ({r.agg_reason})\n")
                if r.dim_hints:
                    f.write(f"    📐 維度標籤偵測: {r.dim_hints}\n")
            f.write("\n")

        if unparseable:
            f.write("-" * 40 + "\n")
            f.write("🚨 無法自動解析 — 請將以下 LLM Prompt 交給 Claude 處理\n")
            f.write("-" * 40 + "\n")
            for r in unparseable:
                if r.triage_action == "use_golden":
                    continue
                f.write(f"\n### {r.alert_name} ###\n")
                f.write(r.llm_prompt)
                f.write("\n")
    os.chmod(report_path, 0o600)

    # --- v3: Triage CSV ---
    csv_path = write_triage_csv(results, output_dir, dictionary)

    # --- v3: Prefix Mapping ---
    mapping_path = write_prefix_mapping(results, output_dir, prefix)

    return len(perfect), len(complex_rules), len(unparseable), golden_parseable


def print_dry_run(results):
    """Dry-run 模式: 僅在 STDOUT 輸出報告摘要。"""
    perfect = [r for r in results if r.status == "perfect"]
    complex_rules = [r for r in results if r.status == "complex"]
    unparseable = [r for r in results if r.status == "unparseable"]
    golden_matches = [r for r in results if r.triage_action == "use_golden"]

    print(f"\n{'='*60}")
    print("🔍 Dry-Run 預覽 (不產生檔案)")
    print(f"{'='*60}\n")
    print(f"總規則數: {len(results)}")
    print(f"  ✅ 完美解析: {len(perfect)}")
    print(f"  ⚠️  複雜表達式 (自動猜測): {len(complex_rules)}")
    print(f"  🚨 無法解析 (需 LLM): {len(unparseable)}")
    print(f"  📖 建議使用黃金標準: {len(golden_matches)}\n")

    for r in results:
        if r.triage_action == "use_golden":
            golden = r.dict_match
            print(f"  📖 {r.alert_name}: 建議改用黃金標準 "
                  f"{golden.get('golden_rule', '?')} (scaffold_tenant.py)")
        elif r.status == "unparseable":
            print(f"  🚨 {r.alert_name}: 無法自動解析 (需 LLM 協助)")
        else:
            icon = "✅" if r.status == "perfect" else "⚠️"
            print(f"  {icon} {r.alert_name}: {r.agg_mode} — {r.agg_reason}")
            for k, v in r.tenant_config.items():
                print(f"     → {k}: \"{v}\"")
            if r.dim_hints:
                print(f"     📐 維度: {r.dim_hints}")
    print()


def print_triage(results):
    """Triage 模式: 精簡統計 + CSV 路徑指引。"""
    auto = [r for r in results if r.triage_action == "auto"]
    review = [r for r in results if r.triage_action == "review"]
    skip = [r for r in results if r.triage_action == "skip"]
    golden = [r for r in results if r.triage_action == "use_golden"]

    print(f"\n{'='*60}")
    print("📊 Triage 分析報告 (大規模遷移前置分析)")
    print(f"{'='*60}\n")
    print(f"總規則數: {len(results)}\n")
    print(f"  ✅ 可自動轉換 (auto):      {len(auto):>4} 條")
    print(f"  ⚠️  需人工確認 (review):     {len(review):>4} 條")
    print(f"  🚨 無法轉換 (skip):         {len(skip):>4} 條")
    print(f"  📖 建議黃金標準 (use_golden): {len(golden):>4} 條\n")

    # 收斂率
    unique_records = set()
    for r in results:
        if r.status != "unparseable":
            for rr in r.recording_rules:
                unique_records.add(rr["record"])
    convertible = len(auto) + len(review)
    if convertible > 0:
        print(f"📈 預估收斂率:")
        print(f"   {convertible} 條可轉換規則 → {len(unique_records)} 條 Recording Rules")
        compression = round((1 - len(unique_records) / max(convertible * 2, 1)) * 100, 1)
        print(f"   壓縮率: {compression}%\n")

    if golden:
        print(f"💡 建議:")
        print(f"   {len(golden)} 條規則已有黃金標準覆蓋，建議直接用")
        print(f"   scaffold_tenant.py 設定閾值，不需要轉換。\n")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="傳統 Prometheus 警報規則遷移輔助工具 (v4 AST) — 自動轉換為動態多租戶三件套"
    )
    parser.add_argument("input_file", help="傳統 Prometheus alert rules YAML 檔案")
    parser.add_argument("-o", "--output-dir", default="migration_output",
                        help="輸出目錄 (預設: migration_output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="僅顯示報告，不產生檔案")
    parser.add_argument("--interactive", action="store_true",
                        help="遇到複雜表達式時互動詢問聚合模式")
    parser.add_argument("--triage", action="store_true",
                        help="Triage 模式: 只產出 CSV 分桶報告 (適合大規模遷移前置分析)")
    parser.add_argument("--no-prefix", action="store_true",
                        help="停用 custom_ 前綴隔離 (不建議: 可能與黃金標準衝突)")
    parser.add_argument("--prefix", default="custom_",
                        help="自訂前綴 (預設: custom_)")
    parser.add_argument("--no-dictionary", action="store_true",
                        help="停用啟發式字典比對")
    parser.add_argument("--no-ast", action="store_true",
                        help="停用 AST 引擎，強制使用舊版 regex 解析 (除錯用)")
    args = parser.parse_args()

    # 確定 prefix
    prefix = "" if args.no_prefix else args.prefix

    # AST 引擎
    use_ast = (not args.no_ast) and HAS_AST
    if not args.no_ast and not HAS_AST:
        print("[WARN] promql-parser 未安裝，降級為 regex 引擎。"
              "安裝: pip install promql-parser", file=sys.stderr)

    # 載入字典
    dictionary = {} if args.no_dictionary else load_metric_dictionary()

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
            result = process_rule(
                rule,
                interactive=args.interactive,
                prefix=prefix,
                dictionary=dictionary,
                use_ast=use_ast,
            )
            if result:
                results.append(result)

    if not results:
        print("No alert rules found to process.")
        return

    # Auto-Suppression: warning ↔ critical 配對
    n_paired = apply_auto_suppression(results)
    if n_paired:
        print(f"[🔗] Auto-Suppression: {n_paired} 組 warning↔critical 配對完成")

    # 輸出
    if args.triage:
        # Triage mode: 只產 CSV + 統計
        os.makedirs(args.output_dir, exist_ok=True)
        csv_path = write_triage_csv(results, args.output_dir, dictionary)
        print_triage(results)
        print(f"📁 CSV 報告已輸出至 {csv_path}")
        print(f"   請在 Excel/Google Sheets 中開啟，批次決策每條規則的處理方式。\n")
    elif args.dry_run:
        print_dry_run(results)
    else:
        n_perfect, n_complex, n_unparseable, n_golden = write_outputs(
            results, args.output_dir, prefix, dictionary
        )
        convertible = n_perfect + n_complex - n_golden
        print(f"[✓] 成功轉換 {convertible} 條規則 "
              f"(✅ {n_perfect} 完美, ⚠️ {n_complex} 已猜測)")
        if n_golden:
            print(f"[📖] {n_golden} 條建議改用黃金標準 (詳見報告)")
        if n_unparseable:
            print(f"[!] {n_unparseable} 條需人工處理 (LLM Prompt 已寫入報告)")
        print(f"📁 檔案已輸出至 {args.output_dir}/")
        if prefix:
            print(f"🏷️  前綴: {prefix} (Prefix Mapping 已輸出)")


if __name__ == "__main__":
    main()
