#!/usr/bin/env python3
"""lint_custom_rules.py — Custom Rule deny-list linter。

掃描 Prometheus rule YAML 檔案，檢查是否違反平台治理規範：
  1. 禁止使用高成本 PromQL 函式 (holt_winters, predict_linear 等)
  2. 禁止危險 regex pattern (全通配 =~".*")
  3. 強制要求 tenant label
  4. 限制 range vector duration
  5. 禁止破壞 tenant 隔離的語法

用法:
  # 掃描 custom rule 目錄
  python3 scripts/tools/lint_custom_rules.py rule-packs/custom/

  # 掃描指定檔案
  python3 scripts/tools/lint_custom_rules.py path/to/rules.yaml

  # 使用自訂 policy 檔
  python3 scripts/tools/lint_custom_rules.py rule-packs/custom/ --policy .github/custom-rule-policy.yaml

  # CI 模式 (非零退出碼)
  python3 scripts/tools/lint_custom_rules.py rule-packs/custom/ --ci

參考: docs/custom-rule-governance.md §4
"""

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Defaults (used when no policy file is provided)
# ---------------------------------------------------------------------------
DEFAULT_POLICY = {
    "denied_functions": [
        "holt_winters",
        "predict_linear",
        "quantile_over_time",
    ],
    "denied_patterns": [
        '=~".*"',
        "without(tenant)",
    ],
    "required_labels": [
        "tenant",
    ],
    "max_range_duration": "1h",
    "max_evaluation_interval": "60s",
}

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------
DURATION_RE = re.compile(r"^(\d+)([smhd])$")
DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_seconds(val):
    """Parse Prometheus duration string to seconds. Returns None on failure."""
    if isinstance(val, (int, float)):
        return int(val)
    m = DURATION_RE.match(str(val).strip())
    if not m:
        return None
    return int(m.group(1)) * DURATION_MULTIPLIERS[m.group(2)]


# ---------------------------------------------------------------------------
# Lint checks
# ---------------------------------------------------------------------------
class LintResult:
    """Single lint finding."""

    def __init__(self, filepath, rule_name, line_hint, severity, message):
        self.filepath = filepath
        self.rule_name = rule_name
        self.line_hint = line_hint
        self.severity = severity  # "ERROR" or "WARN"
        self.message = message

    def __str__(self):
        loc = f"{self.filepath}"
        if self.line_hint:
            loc += f":{self.line_hint}"
        name = f" [{self.rule_name}]" if self.rule_name else ""
        return f"{self.severity}: {loc}{name} - {self.message}"


def lint_expr(expr, policy, filepath, rule_name):
    """Check a PromQL expr string against policy. Returns list of LintResult."""
    results = []
    if not expr:
        return results

    # Check denied functions
    for func in policy.get("denied_functions", []):
        pattern = re.compile(r"\b" + re.escape(func) + r"\s*\(")
        if pattern.search(expr):
            results.append(LintResult(
                filepath, rule_name, None, "ERROR",
                f"denied function '{func}' in expr"
            ))

    # Check denied patterns (whitespace-tolerant matching)
    for pat in policy.get("denied_patterns", []):
        # Build regex: escape each character for literal match, then insert
        # optional \s* after operator chars and before/after parens.
        # This tolerates '=~ ".*"' matching '=~".*"' and
        # 'without (tenant)' matching 'without(tenant)'.
        regex_parts = []
        for ch in pat:
            # Insert optional whitespace before opening/closing parens
            if ch in ('(', ')'):
                regex_parts.append(r'\s*')
            regex_parts.append(re.escape(ch))
            # Insert optional whitespace after operator chars and parens
            if ch in ('=', '~', '!', '<', '>', '(', ')'):
                regex_parts.append(r'\s*')
        ws_regex = ''.join(regex_parts)
        if re.search(ws_regex, expr):
            results.append(LintResult(
                filepath, rule_name, None, "ERROR",
                f"denied pattern '{pat}' in expr"
            ))

    # Check range vector duration
    max_range = policy.get("max_range_duration")
    if max_range:
        max_secs = parse_duration_seconds(max_range)
        if max_secs:
            for m in re.finditer(r"\[(\d+[smhd])\]", expr):
                range_secs = parse_duration_seconds(m.group(1))
                if range_secs and range_secs > max_secs:
                    results.append(LintResult(
                        filepath, rule_name, None, "ERROR",
                        f"range vector [{m.group(1)}] exceeds max allowed [{max_range}]"
                    ))

    return results


def lint_labels(labels, policy, filepath, rule_name, is_recording):
    """Check that required labels are present (for alert rules only)."""
    results = []
    if is_recording:
        return results  # Recording rules 不強制 label

    required = policy.get("required_labels", [])
    existing = set((labels or {}).keys())

    for req in required:
        if req not in existing:
            results.append(LintResult(
                filepath, rule_name, None, "ERROR",
                f"missing required label '{req}'"
            ))

    return results


def lint_group_interval(interval, policy, filepath, group_name):
    """Check evaluation_interval against policy max."""
    results = []
    max_interval = policy.get("max_evaluation_interval")
    if not max_interval or not interval:
        return results

    max_secs = parse_duration_seconds(max_interval)
    actual_secs = parse_duration_seconds(interval)
    if max_secs and actual_secs and actual_secs > max_secs:
        results.append(LintResult(
            filepath, f"group:{group_name}", None, "WARN",
            f"evaluation_interval '{interval}' exceeds recommended max '{max_interval}'"
        ))
    return results


def check_expiry_label(labels, filepath, rule_name):
    """Warn if custom rule lacks expiry date."""
    results = []
    if not labels or "expiry" not in labels:
        results.append(LintResult(
            filepath, rule_name, None, "WARN",
            "Tier 3 rule should have 'expiry' label (governance §5)"
        ))
    return results


def check_owner_label(labels, filepath, rule_name):
    """Warn if custom rule lacks owner label."""
    results = []
    if not labels or "owner" not in labels:
        results.append(LintResult(
            filepath, rule_name, None, "WARN",
            "Tier 3 rule should have 'owner' label (governance §3)"
        ))
    return results


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------
def lint_file(filepath, policy):
    """Lint a single YAML rule file. Returns (list of LintResult, rule_count)."""
    results = []
    rule_count = 0
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        results.append(LintResult(filepath, None, None, "ERROR", f"cannot read file: {e}"))
        return results, rule_count

    # Handle ConfigMap-wrapped rules (data: key: |)
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as e:
        results.append(LintResult(filepath, None, None, "ERROR", f"YAML parse error: {e}"))
        return results, rule_count

    if not doc:
        return results, rule_count

    # Extract rule groups — support both direct format and ConfigMap wrapper
    groups = []
    if isinstance(doc, dict):
        if "groups" in doc:
            groups = doc["groups"]
        elif "data" in doc:
            # ConfigMap format: data contains YAML strings
            for _key, val in doc["data"].items():
                if isinstance(val, str):
                    try:
                        inner = yaml.safe_load(val)
                        if isinstance(inner, dict) and "groups" in inner:
                            groups.extend(inner["groups"])
                    except yaml.YAMLError:
                        pass

    if not groups:
        return results, rule_count

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = group.get("name", "<unnamed>")

        # Check group interval
        interval = group.get("interval")
        if interval:
            results.extend(lint_group_interval(interval, policy, filepath, group_name))

        for rule in group.get("rules", []):
            if not isinstance(rule, dict):
                continue
            rule_count += 1

            # Determine rule name and type
            rule_name = rule.get("alert") or rule.get("record") or "<unnamed>"
            is_recording = "record" in rule
            expr = rule.get("expr", "")
            labels = rule.get("labels", {})

            # Core checks
            results.extend(lint_expr(expr, policy, filepath, rule_name))
            results.extend(lint_labels(labels, policy, filepath, rule_name, is_recording))

            # Governance checks (Tier 3 best practices)
            if not is_recording:
                results.extend(check_expiry_label(labels, filepath, rule_name))
                results.extend(check_owner_label(labels, filepath, rule_name))

    return results, rule_count


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------
def load_policy(policy_path):
    """Load policy from YAML file, falling back to defaults."""
    if not policy_path:
        return DEFAULT_POLICY.copy()
    try:
        with open(policy_path, 'r', encoding='utf-8') as f:
            custom = yaml.safe_load(f) or {}
        # Merge: custom overrides defaults
        merged = DEFAULT_POLICY.copy()
        merged.update(custom)
        return merged
    except Exception as e:
        print(f"⚠️  Cannot load policy file {policy_path}: {e}", file=sys.stderr)
        print("    Falling back to built-in defaults.", file=sys.stderr)
        return DEFAULT_POLICY.copy()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def collect_files(paths):
    """Expand paths to list of YAML files."""
    files = []
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix in (".yaml", ".yml"):
            files.append(str(path))
        elif path.is_dir():
            for ext in ("*.yaml", "*.yml"):
                files.extend(str(f) for f in path.rglob(ext))
    return sorted(set(files))


def main():
    parser = argparse.ArgumentParser(
        description="Lint custom Prometheus rules against platform governance policy."
    )
    parser.add_argument(
        "paths", nargs="+",
        help="YAML file(s) or directory to scan"
    )
    parser.add_argument(
        "--policy", default=None,
        help="Path to custom-rule-policy.yaml (default: built-in policy)"
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: exit with non-zero code on any ERROR"
    )
    args = parser.parse_args()

    policy = load_policy(args.policy)
    files = collect_files(args.paths)

    if not files:
        print("No YAML files found.")
        sys.exit(0)

    all_results = []
    files_checked = 0
    rules_checked = 0

    for filepath in files:
        results, count = lint_file(filepath, policy)
        all_results.extend(results)
        files_checked += 1
        rules_checked += count

    # Output
    errors = [r for r in all_results if r.severity == "ERROR"]
    warns = [r for r in all_results if r.severity == "WARN"]

    for r in all_results:
        print(str(r))

    print()
    print(f"Scanned {files_checked} file(s), {rules_checked} rule(s).")
    print(f"  Errors: {len(errors)}  Warnings: {len(warns)}")

    if not errors and not warns:
        print("  ✅ All checks passed.")

    if args.ci and errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
