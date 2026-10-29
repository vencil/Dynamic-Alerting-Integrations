#!/usr/bin/env python3
"""lint_custom_rules.py — Custom Rule deny-list linter。

掃描 Prometheus rule YAML 檔案，檢查是否違反平台治理規範：
  1. 禁止使用高成本 PromQL 函式 (holt_winters, predict_linear 等)
  2. 禁止危險 regex pattern (全通配 =~".*")
  3. 強制要求 tenant label
  4. 限制 range vector duration
  5. 禁止破壞 tenant 隔離的語法

平台 COMPILED pack（如 rule-pack-custom-alerts.yaml，帶 GENERATED 檔頭）可由
policy 的 `file_overrides` 取得逐檔豁免（例如 forecast recipe 的 predict_linear），
其餘檢查照跑；缺 GENERATED 標記時豁免不生效並回報 ERROR。

用法:
  # 掃描 custom rule 目錄
  python3 scripts/tools/ops/lint_custom_rules.py rule-packs/custom/

  # 掃描指定檔案
  python3 scripts/tools/ops/lint_custom_rules.py path/to/rules.yaml

  # 使用自訂 policy 檔
  python3 scripts/tools/ops/lint_custom_rules.py rule-packs/custom/ --policy .github/custom-rule-policy.yaml

  # CI 模式 (非零退出碼)
  python3 scripts/tools/ops/lint_custom_rules.py rule-packs/custom/ --ci

參考: docs/custom-rule-governance.md §4
"""

import argparse
import copy
import os
import re
import sys
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
from _lib_python import parse_duration_seconds  # noqa: E402
from _lib_constants import FORECAST_MAX_LOOKBACK  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, die_caller_error  # noqa: E402
from _lib_io import classify_path  # noqa: E402  (#3276)
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)

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
    # Per-file overrides for platform-COMPILED packs (kept in sync with
    # .github/custom-rule-policy.yaml so the no-policy path — e.g.
    # validate_config.py without --policy — behaves identically). The deny-list
    # governs tenant-authored raw PromQL; a compiled pack's recipes are
    # platform-vetted with cost mitigations baked in at compile time
    # (e.g. forecast: platform-derived lookback + cold-start sample gate),
    # so selected denials don't apply. Guard rails: the override is honored
    # only when the file header carries the GENERATED marker (missing marker
    # → ERROR), and only the listed keys are overridden — every other check
    # still runs on the file. CI separately drift-checks the pack via
    # compile_custom_alerts.py --check.
    "file_overrides": [
        {
            "path": "rule-packs/rule-pack-custom-alerts.yaml",
            "require_generated_marker": True,
            "policy": {
                "denied_functions": [
                    "holt_winters",
                    "quantile_over_time",
                ],
                # The longest lookback a compiled forecast reads, generated
                # from pkg/config/recipe_contract.json into _lib_constants
                # (tests/ops/test_lint_custom_rules.py compiles that case).
                "max_range_duration": FORECAST_MAX_LOOKBACK,
            },
        },
    ],
}

# A file_overrides entry with require_generated_marker only applies when this
# token appears in the file's first few lines (compile_custom_alerts._render
# writes "# GENERATED from ... — DO NOT EDIT." into the header block).
GENERATED_MARKER = "GENERATED"
GENERATED_MARKER_SCAN_LINES = 10

# An override may relax ONLY these policy keys. The deny-list's structural
# integrity checks (tenant-isolation patterns, required labels, group interval)
# must NEVER be disable-able via a per-file override, even for a compiled pack —
# those guard against compiler bugs, not just tenant misuse. Anything outside
# this set in an override's `policy` block is ignored (and flagged), so a stray
# or malicious `required_labels: []` cannot silently widen the exemption.
OVERRIDABLE_POLICY_KEYS = frozenset({"denied_functions", "max_range_duration"})


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
def read_rule_files(files):
    """{path: [RuleFile, ...]} — every file of *files* as Prometheus loads it
    (#3206, ADR-036 §12). A file whose Kubernetes reading (da-crdecode) holds
    a ``kind: ConfigMap`` is a ConfigMap: each of its data values that sets
    out to be a rule file (a ``groups:`` key; ``manifest_rule_file_results``
    with ``only_declared``) is one. Any other file is a rule file by the
    same test — a document of its Kubernetes reading is a mapping with a
    ``groups`` key (``documents_declare_rule_groups``); one without is no
    rule file (a recipe, the registry beside the packs) and is not read, as
    before. A file kubectl's reading refuses is handed to the rule loader
    all the same: that reading refuses some files the loader loads (an
    annotation value ``.inf``), and a file here that neither reads is an
    ERROR, as a YAML parse error was — never skipped. The rule files are
    read by Prometheus' rule loader (``da-read prometheus-rules``), the
    plain ones in one run and the ConfigMaps' in another; a file that is no
    rule file maps to ``[]``. Raises ``_lib_consumer_read.ConsumerReadError``
    when a binary cannot be had."""
    plain, configmaps, others = [], [], []
    for path in files:
        try:
            docs = _lib_consumer_read.kubernetes_documents(path)
        except _lib_consumer_read.InputRefused:
            plain.append(path)
            continue
        if any(isinstance(d, dict) and d.get("kind") == "ConfigMap" for d in docs):
            configmaps.append(path)
        elif _lib_consumer_read.documents_declare_rule_groups(docs):
            plain.append(path)
        else:
            others.append(path)
    out = {p: [rf] for p, rf in zip(
        plain, _lib_consumer_read.prometheus_rule_file_results(plain))}
    for rf in _lib_consumer_read.manifest_rule_file_results(configmaps, only_declared=True):
        if rf.kind == "ConfigMap":
            out.setdefault(rf.manifest, []).append(rf)
    for p in (*configmaps, *others):
        out.setdefault(p, [])
    return out


def lint_file(filepath, policy, rule_files=None):
    """Lint a single YAML rule file (or a ConfigMap holding rule files).
    *rule_files* is its reading from ``read_rule_files``; read here when not
    given. Returns (list of LintResult, rule_count)."""
    results = []
    rule_count = 0
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        # ⛔ #1618: `UnicodeDecodeError` is a ValueError, NOT an OSError, so
        # the bare `except OSError` here did not see it and one non-UTF-8
        # file (cp950 bytes in a .yaml) escaped as a traceback with 0 bytes
        # of stdout — every OTHER file's findings were lost with it. Now it
        # is one ERROR LintResult for THAT file (per-file quarantine, #1008
        # style); under `--ci` that ERROR still fails the run.
        results.append(LintResult(filepath, None, None, "ERROR", f"cannot read file: {e}"))
        return results, rule_count

    # Per-file overrides (platform-compiled packs; policy `file_overrides`)
    policy, override_results = resolve_file_policy(filepath, content, policy)
    results.extend(override_results)

    # #3206: the file is read the way Prometheus loads it — a plain rule file
    # by its rule loader, a ConfigMap (data: key: |) the way kubectl reads it
    # and each data value by the rule loader. A file or data value the loader
    # refuses is an ERROR naming it, with the loader's reason; it is never
    # skipped (a ConfigMap value with a `groups:` key that did not parse used
    # to be dropped silently). A file or value that is no rule file at all
    # (no `groups:` key) is not read, as before — the same test for both.
    if rule_files is None:
        rule_files = read_rule_files([filepath])[filepath]
    groups = []
    for rf in rule_files:
        if rf.problem is not None:
            where = f" (data key {rf.key!r})" if rf.key is not None else ""
            results.append(LintResult(
                filepath, None, None, "ERROR",
                f"Prometheus' rule loader refuses it{where}: {rf.problem}"))
            continue
        groups.extend(rf.groups)

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
def _override_path_matches(norm, ov_path):
    """True iff `norm` is the override's canonical path, anchored at the TOP of
    the scanned tree — NOT merely a suffix.

    The override path (e.g. ``rule-packs/rule-pack-custom-alerts.yaml``) is
    repo-root-relative, and CI runs the linter from the repo root, so a
    legitimate hit is either an exact match (CI) or the canonical path under an
    absolute prefix that does NOT itself re-enter the scanned tree (local /
    tmp invocations). A bare suffix match would let a tenant smuggle a file at
    a NESTED path like ``rule-packs/custom/rule-packs/rule-pack-custom-alerts.yaml``
    into the exemption — the recursive scan descends into it, the drift gate
    (compile_custom_alerts --check) only inspects the single canonical path, so
    nothing else would catch it. Rejecting any prefix that re-introduces the
    override's anchor directory closes that hole while still allowing an
    absolute `/tmp/.../rule-packs/<file>` prefix.
    """
    if norm == ov_path:
        return True
    suffix = "/" + ov_path
    if not norm.endswith(suffix):
        return False
    prefix = norm[: -len(suffix)]
    anchor = ov_path.split("/", 1)[0]            # e.g. "rule-packs"
    return anchor not in prefix.split("/")


def resolve_file_policy(filepath, content, policy):
    """Resolve the effective policy for one file (per-file overrides).

    Returns (effective_policy, list of LintResult). A matching override whose
    GENERATED-marker requirement fails yields an ERROR and the base policy —
    fail loud: either the pack was hand-authored at a platform path or the
    policy entry is stale. Only OVERRIDABLE_POLICY_KEYS are relaxable; any other
    key in an override's `policy` block is ignored and flagged.
    """
    norm = Path(filepath).as_posix()
    for override in policy.get("file_overrides") or []:
        if not isinstance(override, dict):       # tolerate malformed policy entries
            continue
        ov_path = override.get("path")
        if not ov_path or not _override_path_matches(norm, ov_path):
            continue
        if override.get("require_generated_marker", True):
            head = content.splitlines()[:GENERATED_MARKER_SCAN_LINES]
            if not any(GENERATED_MARKER in line for line in head):
                return policy, [LintResult(
                    filepath, None, None, "ERROR",
                    f"file_overrides entry for '{ov_path}' requires a "
                    f"{GENERATED_MARKER} header marker but none was found in "
                    f"the first {GENERATED_MARKER_SCAN_LINES} lines — "
                    "hand-authored files are fully linted"
                )]
        ov_policy = override.get("policy") or {}
        rejected = sorted(set(ov_policy) - OVERRIDABLE_POLICY_KEYS)
        out = []
        if rejected:
            out.append(LintResult(
                filepath, None, None, "ERROR",
                f"file_overrides for '{ov_path}' may only relax "
                f"{sorted(OVERRIDABLE_POLICY_KEYS)}; ignoring disallowed "
                f"key(s) {rejected} (those checks stay enforced)"
            ))
        effective = policy.copy()
        effective.update({k: v for k, v in ov_policy.items()
                          if k in OVERRIDABLE_POLICY_KEYS})
        return effective, out
    return policy, []


def load_policy(policy_path):
    """Load policy from YAML file; the built-in policy is the DEFAULT, not a
    fallback for a broken ``--policy``.

    ⛔ #1556: the previous version warned on stderr and carried on with the
    built-in policy, so `lint --policy <typo>` linted against different rules
    than the operator asked for and still exited 0. A warning nobody reads in
    CI is not a control. dev-rules #13 files "檔案/路徑不存在" and
    "malformed 輸入" under EXIT_CALLER_ERROR.
    """
    # ⛔ `is None`, not `not policy_path`. An empty string is SUPPLIED — it is
    # what an unset shell variable expands to — and the falsy test routed it
    # into the omitted branch, silently linting against the built-in policy
    # instead of the operator's. The flag's argparse default is None, so
    # nothing else reaches this branch. Same split as #1616's --base-config.
    if policy_path is None:
        # ⛔ deepcopy, not `.copy()`: the latter is shallow, so a caller
        # appending to one of the nested lists reaches into the module default
        # and changes what every later call in the process returns. Same shape
        # as #1616's load_base_config fix; fixed here for symmetry rather than
        # because a mutating caller exists today.
        return copy.deepcopy(DEFAULT_POLICY)
    try:
        with open(policy_path, 'r', encoding='utf-8') as f:
            custom = yaml.safe_load(f) or {}
    except OSError as e:
        # ⛔ `!r` so an empty value is visible as '' rather than as a message
        # that stops after the colon.
        die_caller_error(
            f"--policy: cannot read {policy_path!r}: {e}\n"
            "  ⛔ Do not drop the flag to clear this — that silently lints "
            "against the built-in policy instead of yours.")
    except UnicodeDecodeError as e:
        # ⛔ NOT an OSError — it is a ValueError, so the handler above does not
        # see it and a non-UTF-8 policy file escaped as a traceback with rc=1.
        die_caller_error(f"--policy: {policy_path!r} is not valid UTF-8: {e}")
    except yaml.YAMLError as e:
        die_caller_error(f"--policy: {policy_path!r} is not valid YAML: {e}")
    if not isinstance(custom, dict):
        die_caller_error(
            f"--policy: top level of {policy_path!r} is "
            f"{type(custom).__name__}, expected a mapping")
    # Merge: custom overrides defaults
    merged = copy.deepcopy(DEFAULT_POLICY)
    merged.update(custom)
    return merged


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
    """CLI entry point: Custom Rule deny-list linter。."""
    try_utf8_stdout()
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

    # ⛔ #1618 (D-06): a scan target that does not exist is a caller error,
    # not a clean scan. `collect_files` simply skipped it, so
    # `lint /typo/dir --ci` printed "No YAML files found." and exited 0 —
    # a governance gate that passes on a path nobody scanned. Same answer
    # with and without `--ci`. A target that EXISTS but holds no YAML is a
    # legitimate no-op and stays EXIT_OK below.
    # ⛔ `p == ""` is a separate test, not folded into `exists()`:
    # `Path('')` is `PosixPath('.')`, so `exists()` is True and `lint ''`
    # (an unset shell variable) silently linted the CURRENT DIRECTORY and
    # exited on whatever it found there. '' is supplied and unusable — same
    # family as a missing target, and `!r` makes it visible as ''.
    # #3276: every other target is judged by the shared caller-path check —
    # under a parent the caller cannot enter it is named, not a traceback.
    if "" in args.paths:
        die_caller_error(
            "scan target(s) not found or empty: ''"
            "\n  ⛔ Do not drop the path to clear this — an absent target "
            "is not a clean one, and '' is not the current directory.")
    problems = [p for p in (classify_path(t, "any") for t in args.paths) if p is not None]
    if problems:
        die_caller_error(
            "".join(f"ERROR: {p.message}\n" for p in problems)
            + "  ⛔ Do not drop the path to clear this — an absent target "
              "is not a clean one.", problems[0].reason)

    files = collect_files(args.paths)

    if not files:
        print("No YAML files found under: " + ", ".join(repr(p) for p in args.paths))
        sys.exit(EXIT_OK)

    all_results = []
    files_checked = 0
    rules_checked = 0

    try:
        readings = read_rule_files(files)
    except _lib_consumer_read.ConsumerReadError as exc:
        die_caller_error(f"ERROR: rule files: {_lib_consumer_read.error_text(exc)}")

    for filepath in files:
        results, count = lint_file(filepath, policy, readings[filepath])
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
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
