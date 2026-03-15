#!/usr/bin/env python3
"""
policy_engine.py — Policy-as-Code 引擎（Path A — 內建 DSL）。

從 _defaults.yaml 的 ``_policies`` section 或獨立 policy 檔案載入宣告式策略，
對每個 tenant 配置進行評估。透過 ``validate_config.py`` 整合或獨立 CLI 執行。

DSL 運算子：
  required    — 欄位必須存在且非空
  forbidden   — 欄位不得存在
  equals      — 值完全相等
  not_equals  — 值不相等
  gte / lte / gt / lt — 數值或 duration 比較
  matches     — 正則表達式匹配
  one_of      — 值必須在指定清單內
  contains    — 字串包含子字串

條件式規則：
  when 子句：僅在條件成立時才評估主規則。

嚴重度：
  error   — 違規視為失敗（CI exit 1）
  warning — 違規視為警告（僅報告）
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# Repo-layout import compatibility (stripped in Docker build)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from _lib_python import (
        detect_cli_lang,
        load_yaml_file,
        parse_duration_seconds,
    )
except ImportError:
    from scripts.tools._lib_python import (  # type: ignore[no-redef]
        detect_cli_lang,
        load_yaml_file,
        parse_duration_seconds,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_OPERATORS = frozenset({
    "required", "forbidden",
    "equals", "not_equals",
    "gte", "lte", "gt", "lt",
    "matches", "one_of", "contains",
})

VALID_SEVERITIES = frozenset({"error", "warning"})


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class PolicyRule:
    """一條策略規則。"""
    name: str
    description: str
    target: str
    operator: str
    value: Any = None
    severity: str = "error"
    when: Optional[dict] = None
    exclude_tenants: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.operator not in VALID_OPERATORS:
            raise ValueError(
                f"Policy '{self.name}': unknown operator '{self.operator}'. "
                f"Valid: {sorted(VALID_OPERATORS)}"
            )
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"Policy '{self.name}': unknown severity '{self.severity}'. "
                f"Valid: {sorted(VALID_SEVERITIES)}"
            )


@dataclass
class Violation:
    """一筆策略違規。"""
    tenant: str
    rule_name: str
    description: str
    severity: str
    target: str
    message: str


@dataclass
class PolicyResult:
    """整體策略評估結果。"""
    violations: list[Violation] = field(default_factory=list)
    tenants_evaluated: int = 0
    rules_evaluated: int = 0

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")

    @property
    def passed(self) -> bool:
        return self.error_count == 0


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------
def load_policies(source: str) -> list[PolicyRule]:
    """從 YAML 檔案或 _defaults.yaml 載入策略規則。

    Args:
        source: YAML 檔案路徑。可以是：
            - 獨立 policy 檔案（頂層 ``policies`` key）
            - _defaults.yaml（取 ``_policies`` section）

    Returns:
        PolicyRule 清單。
    """
    data = load_yaml_file(source)
    if data is None:
        return []

    raw_rules: list[dict] = []
    if "policies" in data:
        raw_rules = data["policies"]
    elif "_policies" in data:
        raw_rules = data["_policies"]
    else:
        return []

    if not isinstance(raw_rules, list):
        return []

    rules: list[PolicyRule] = []
    for i, r in enumerate(raw_rules):
        if not isinstance(r, dict):
            continue
        try:
            rule = PolicyRule(
                name=r.get("name", f"rule-{i}"),
                description=r.get("description", ""),
                target=str(r.get("target", "")),
                operator=str(r.get("operator", "required")),
                value=r.get("value"),
                severity=str(r.get("severity", "error")),
                when=r.get("when") if isinstance(r.get("when"), dict) else None,
                exclude_tenants=r.get("exclude_tenants", []),
            )
            rules.append(rule)
        except ValueError as exc:
            print(f"  WARN: 跳過無效策略規則 #{i}: {exc}", file=sys.stderr)

    return rules


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------
def _resolve_target(config: dict, target: str) -> tuple[bool, Any]:
    """解析 dot-path 目標，從 tenant 配置中取值。

    支援：
    - 精確 key：``_routing.receiver.type``
    - 萬用字元 key：``*_cpu``（fnmatch 匹配所有 key）

    Args:
        config: tenant 配置 dict。
        target: dot-separated path 或含萬用字元的 pattern。

    Returns:
        (found, value) — found 表示是否找到，value 是解析到的值。
        萬用字元模式回傳所有匹配值的 list。
    """
    # Wildcard pattern — match at top level
    if "*" in target and "." not in target:
        matches = {}
        for k, v in config.items():
            if fnmatch.fnmatch(k, target):
                matches[k] = v
        if matches:
            return True, matches
        return False, None

    # Dot-path navigation
    parts = target.split(".")
    current: Any = config
    for part in parts:
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            else:
                return False, None
        else:
            return False, None
    return True, current


def _resolve_wildcard_values(config: dict, target: str) -> list[tuple[str, Any]]:
    """解析萬用字元目標，回傳 (key, value) 清單。"""
    results = []
    for k, v in config.items():
        if fnmatch.fnmatch(k, target):
            results.append((k, v))
    return results


# ---------------------------------------------------------------------------
# Value comparison
# ---------------------------------------------------------------------------
def _to_comparable(value: Any) -> Union[float, str]:
    """將值轉為可比較的型別（數值或字串）。

    支援 Prometheus duration 字串（如 ``5m`` → ``300``）。
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Try numeric
        try:
            return float(value)
        except ValueError:
            pass
        # Try duration
        secs = parse_duration_seconds(value)
        if secs is not None:
            return float(secs)
    return str(value)


def _evaluate_operator(operator: str, actual: Any, expected: Any) -> bool:
    """評估單一運算子。

    Args:
        operator: 運算子名稱。
        actual: 實際值。
        expected: 規則期望值。

    Returns:
        True 表示「符合規則」（無違規），False 表示「違反規則」。
    """
    if operator == "required":
        if actual is None:
            return False
        if isinstance(actual, str) and actual.strip() == "":
            return False
        if isinstance(actual, dict) and len(actual) == 0:
            return False
        if isinstance(actual, list) and len(actual) == 0:
            return False
        return True

    if operator == "forbidden":
        return actual is None

    if operator == "equals":
        return str(actual) == str(expected)

    if operator == "not_equals":
        return str(actual) != str(expected)

    if operator in ("gte", "lte", "gt", "lt"):
        try:
            a = _to_comparable(actual)
            e = _to_comparable(expected)
            if not isinstance(a, float) or not isinstance(e, float):
                return False
            if operator == "gte":
                return a >= e
            if operator == "lte":
                return a <= e
            if operator == "gt":
                return a > e
            return a < e
        except (TypeError, ValueError):
            return False

    if operator == "matches":
        pattern = str(expected)
        # ReDoS 防護：限制長度 + 偵測巢狀量詞（catastrophic backtracking）
        if len(pattern) > 200:
            return False
        if re.search(r'\([^)]*[+*][^)]*\)[+*?]', pattern):
            return False  # 拒絕巢狀量詞如 (a+)+
        try:
            return bool(re.search(pattern, str(actual)))
        except re.error:
            return False

    if operator == "one_of":
        if not isinstance(expected, list):
            return False
        return str(actual) in [str(v) for v in expected]

    if operator == "contains":
        return str(expected) in str(actual)

    return True  # Unknown operator — pass (should not reach due to validation)


# ---------------------------------------------------------------------------
# Condition evaluation (when clause)
# ---------------------------------------------------------------------------
def _evaluate_when(config: dict, when: dict) -> bool:
    """評估 when 條件子句。

    when 結構：
        target: str — 要檢查的欄位
        operator: str — 運算子
        value: Any — 期望值（可選）

    Returns:
        True 表示條件成立（主規則應該評估），False 表示條件不成立（跳過）。
    """
    target = when.get("target", "")
    operator = when.get("operator", "required")
    value = when.get("value")

    if not target:
        return True

    found, actual = _resolve_target(config, target)
    if operator == "required":
        return found and actual is not None
    if operator == "forbidden":
        return not found or actual is None

    if not found:
        return False

    return _evaluate_operator(operator, actual, value)


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------
def evaluate_rule(rule: PolicyRule, tenant: str, config: dict) -> list[Violation]:
    """對單一 tenant 評估單一策略規則。

    Args:
        rule: 策略規則。
        tenant: tenant 名稱。
        config: tenant 配置 dict。

    Returns:
        違規清單（空 = 通過）。
    """
    # Check tenant exclusion
    if tenant in rule.exclude_tenants:
        return []

    # Evaluate when condition
    if rule.when and not _evaluate_when(config, rule.when):
        return []

    violations: list[Violation] = []

    # Wildcard target — evaluate against all matching keys
    if "*" in rule.target and "." not in rule.target:
        matched = _resolve_wildcard_values(config, rule.target)
        if rule.operator == "required" and not matched:
            violations.append(Violation(
                tenant=tenant,
                rule_name=rule.name,
                description=rule.description,
                severity=rule.severity,
                target=rule.target,
                message=f"未找到匹配 '{rule.target}' 的配置項",
            ))
        else:
            for key, val in matched:
                if not _evaluate_operator(rule.operator, val, rule.value):
                    violations.append(Violation(
                        tenant=tenant,
                        rule_name=rule.name,
                        description=rule.description,
                        severity=rule.severity,
                        target=key,
                        message=_format_violation_msg(
                            rule.operator, key, val, rule.value
                        ),
                    ))
        return violations

    # Standard dot-path target
    found, actual = _resolve_target(config, rule.target)

    if rule.operator in ("required", "forbidden"):
        if not _evaluate_operator(rule.operator, actual if found else None, None):
            violations.append(Violation(
                tenant=tenant,
                rule_name=rule.name,
                description=rule.description,
                severity=rule.severity,
                target=rule.target,
                message=_format_violation_msg(
                    rule.operator, rule.target, actual if found else None, None
                ),
            ))
        return violations

    if not found:
        # Non-existence operators other than required/forbidden — skip silently
        return []

    if not _evaluate_operator(rule.operator, actual, rule.value):
        violations.append(Violation(
            tenant=tenant,
            rule_name=rule.name,
            description=rule.description,
            severity=rule.severity,
            target=rule.target,
            message=_format_violation_msg(
                rule.operator, rule.target, actual, rule.value
            ),
        ))

    return violations


def _format_violation_msg(operator: str, target: str, actual: Any, expected: Any) -> str:
    """格式化違規訊息。"""
    if operator == "required":
        return f"'{target}' 為必填欄位但未配置或為空"
    if operator == "forbidden":
        return f"'{target}' 為禁止欄位但已配置（值: {actual}）"
    if operator == "equals":
        return f"'{target}' 期望等於 '{expected}'，實際為 '{actual}'"
    if operator == "not_equals":
        return f"'{target}' 不得等於 '{expected}'，但實際為 '{actual}'"
    if operator in ("gte", "lte", "gt", "lt"):
        ops = {"gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
        return f"'{target}' 期望 {ops[operator]} {expected}，實際為 '{actual}'"
    if operator == "matches":
        return f"'{target}' 期望匹配 /{expected}/，實際為 '{actual}'"
    if operator == "one_of":
        return f"'{target}' 期望為 {expected} 之一，實際為 '{actual}'"
    if operator == "contains":
        return f"'{target}' 期望包含 '{expected}'，實際為 '{actual}'"
    return f"'{target}': 策略違規（{operator}）"


def evaluate_policies(
    rules: list[PolicyRule],
    tenant_configs: dict[str, dict],
) -> PolicyResult:
    """對所有 tenant 評估所有策略規則。

    Args:
        rules: 策略規則清單。
        tenant_configs: {tenant_name: config_dict}。

    Returns:
        PolicyResult 包含所有違規及統計。
    """
    result = PolicyResult(
        tenants_evaluated=len(tenant_configs),
        rules_evaluated=len(rules),
    )

    for tenant, config in sorted(tenant_configs.items()):
        for rule in rules:
            violations = evaluate_rule(rule, tenant, config)
            result.violations.extend(violations)

    return result


# ---------------------------------------------------------------------------
# Tenant config loading (from config-dir)
# ---------------------------------------------------------------------------
def load_tenant_configs(config_dir: str) -> dict[str, dict]:
    """從 config-dir 載入所有 tenant 配置。

    跳過 ``_`` 開頭的檔案（_defaults.yaml, _profiles.yaml 等）。
    支援 flat 格式和 ``tenants:`` wrapper 格式。

    Args:
        config_dir: conf.d/ 目錄路徑。

    Returns:
        {tenant_name: config_dict}。
    """
    configs: dict[str, dict] = {}

    if not os.path.isdir(config_dir):
        return configs

    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        if fname.startswith("_"):
            continue

        fpath = os.path.join(config_dir, fname)
        data = load_yaml_file(fpath)
        if not isinstance(data, dict):
            continue

        # Multi-tenant wrapper format
        if "tenants" in data and isinstance(data["tenants"], dict):
            for tenant_name, tenant_cfg in data["tenants"].items():
                if isinstance(tenant_cfg, dict):
                    configs[tenant_name] = tenant_cfg
        else:
            # Flat format — filename (sans extension) is tenant name
            tenant_name = os.path.splitext(fname)[0]
            configs[tenant_name] = data

    return configs


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_text_report(result: PolicyResult, lang: str = "en") -> str:
    """產生純文字報告。"""
    lines: list[str] = []

    if lang == "zh":
        lines.append("═══ 策略評估報告 ═══")
        lines.append(f"租戶數: {result.tenants_evaluated} | "
                     f"規則數: {result.rules_evaluated}")
        lines.append(f"錯誤: {result.error_count} | "
                     f"警告: {result.warning_count}")
    else:
        lines.append("═══ Policy Evaluation Report ═══")
        lines.append(f"Tenants: {result.tenants_evaluated} | "
                     f"Rules: {result.rules_evaluated}")
        lines.append(f"Errors: {result.error_count} | "
                     f"Warnings: {result.warning_count}")

    if not result.violations:
        lines.append("")
        lines.append("✓ All policies passed." if lang == "en"
                     else "✓ 所有策略均通過。")
        return "\n".join(lines)

    lines.append("")

    # Group by tenant
    by_tenant: dict[str, list[Violation]] = {}
    for v in result.violations:
        by_tenant.setdefault(v.tenant, []).append(v)

    for tenant in sorted(by_tenant):
        lines.append(f"[{tenant}]")
        for v in by_tenant[tenant]:
            icon = "✗" if v.severity == "error" else "⚠"
            lines.append(f"  {icon} {v.rule_name}: {v.message}")
        lines.append("")

    status = "FAIL" if not result.passed else "PASS"
    lines.append(f"Result: {status}" if lang == "en"
                 else f"結果: {status}")

    return "\n".join(lines)


def generate_json_report(result: PolicyResult) -> dict:
    """產生 JSON 格式報告。"""
    return {
        "tenants_evaluated": result.tenants_evaluated,
        "rules_evaluated": result.rules_evaluated,
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "passed": result.passed,
        "violations": [
            {
                "tenant": v.tenant,
                "rule_name": v.rule_name,
                "description": v.description,
                "severity": v.severity,
                "target": v.target,
                "message": v.message,
            }
            for v in result.violations
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser(lang: str = "en") -> argparse.ArgumentParser:
    """建構 CLI 解析器。"""
    if lang == "zh":
        parser = argparse.ArgumentParser(
            description="Policy-as-Code 評估引擎 — 對 tenant 配置執行宣告式策略檢查。",
        )
        parser.add_argument(
            "--config-dir", required=True,
            help="conf.d/ 目錄路徑（含 tenant YAML）",
        )
        parser.add_argument(
            "--policy", required=False,
            help="獨立策略檔案路徑（頂層 policies: key）",
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="輸出 JSON 格式",
        )
        parser.add_argument(
            "--ci", action="store_true",
            help="CI 模式：有 error 級違規時 exit 1",
        )
    else:
        parser = argparse.ArgumentParser(
            description="Policy-as-Code evaluation engine — declarative policy checks for tenant configs.",
        )
        parser.add_argument(
            "--config-dir", required=True,
            help="Path to conf.d/ directory containing tenant YAML files",
        )
        parser.add_argument(
            "--policy", required=False,
            help="Path to standalone policy file (top-level policies: key)",
        )
        parser.add_argument(
            "--json", action="store_true", dest="json_output",
            help="Output in JSON format",
        )
        parser.add_argument(
            "--ci", action="store_true",
            help="CI mode: exit 1 if any error-level violations found",
        )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 進入點。"""
    lang = detect_cli_lang()
    parser = build_parser(lang)
    args = parser.parse_args(argv)

    # Load policies
    rules: list[PolicyRule] = []

    # From _defaults.yaml in config-dir
    defaults_path = os.path.join(args.config_dir, "_defaults.yaml")
    if os.path.isfile(defaults_path):
        rules.extend(load_policies(defaults_path))

    # From standalone policy file
    if args.policy:
        rules.extend(load_policies(args.policy))

    if not rules:
        if lang == "zh":
            print("未找到策略規則。在 _defaults.yaml 新增 _policies 或指定 --policy。")
        else:
            print("No policy rules found. Add _policies to _defaults.yaml or specify --policy.")
        return 0

    # Load tenant configs
    tenant_configs = load_tenant_configs(args.config_dir)
    if not tenant_configs:
        if lang == "zh":
            print(f"未找到 tenant 配置於 {args.config_dir}")
        else:
            print(f"No tenant configs found in {args.config_dir}")
        return 0

    # Evaluate
    result = evaluate_policies(rules, tenant_configs)

    # Output
    if args.json_output:
        print(json.dumps(generate_json_report(result), indent=2, ensure_ascii=False))
    else:
        print(generate_text_report(result, lang))

    # Exit code
    if args.ci and not result.passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
