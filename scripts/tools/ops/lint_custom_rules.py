#!/usr/bin/env python3
"""lint_custom_rules.py — Custom Rule deny-list linter。

掃描 Prometheus rule YAML 檔案，檢查是否違反平台治理規範：
  1. 禁止使用高成本 PromQL 函式 (holt_winters, predict_linear 等)
  2. 每條 alert 的結果保留 tenant（preserved_labels，依 Prometheus 語法樹判定；#4098）
  3. 強制要求 tenant label
  4. 限制 range vector duration

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

  # 目錄裡混有不是 rule file 的 YAML：以 --exclude 排除（可重複）
  python3 scripts/tools/ops/lint_custom_rules.py my-rules/ --exclude 'templates/*'

  # rule-packs 目錄：依宣告（rule-pack-*.yaml|yml）取檔，不手寫 --exclude
  python3 scripts/tools/ops/lint_custom_rules.py --rule-pack-dir rule-packs/ --ci

讀哪些檔由路徑決定（#3426）：檔案照給的讀，目錄遞迴取 *.yaml／*.yml，減去
--exclude 符合的。選到的每個檔都必須是 rule file 或 ConfigMap／PrometheusRule
manifest，否則以讀者理由回報 ERROR，不會當成「沒有 rule」略過。
--ci 下一個檔都沒選到是呼叫端錯誤（rc 2，#3462）；不帶 --ci 時只印提示、rc 0。

參考: docs/custom-rule-governance.md §4
"""

import argparse
import copy
import os
import re
import sys
from pathlib import Path


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_validation import am_duration_seconds  # noqa: E402  (#4061: Prometheus' ParseDuration)
from _lib_constants import FORECAST_MAX_LOOKBACK  # noqa: E402
import _lib_exitcodes  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, die_caller_error  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
import _lib_input  # noqa: E402  (#3462: the one wording of "selected nothing")
from _lib_label import safe_label  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults (used when no policy file is provided)
# ---------------------------------------------------------------------------
DEFAULT_POLICY = {
    "denied_functions": [
        "holt_winters",
        "predict_linear",
        "quantile_over_time",
    ],
    # #4098: every alert's result keeps these labels, judged on Prometheus'
    # parse tree (`da-read promql-label-flow`) — never a pattern over the
    # text, which `without(tenant, b)` or `sum by (a)` walked past.
    "preserved_labels": [
        "tenant",
    ],
    "required_labels": [
        "tenant",
    ],
    "max_range_duration": "1h",
    "max_evaluation_interval": "60s",
    # Per-file overrides for platform-COMPILED packs (kept in sync with
    # .github/custom-rule-policy.yaml so the no-policy path — e.g.
    # validate_config.py without --lint-policy — behaves identically). The deny-list
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

#: The keys of the custom-rule lint policy (#3797): those of the built-in
#: policy, which a --policy is merged over. Any other key is refused — the
#: deployment policy's keys (allowed_domains …) in this file were silently
#: unused, and a misspelt key here silently linted by the default.
POLICY_KEYS = frozenset(DEFAULT_POLICY)

#: Keys this policy had and no longer reads, with what replaced them: a
#: policy carrying one is refused naming the replacement (#4098), not with
#: the bare "no key" a typo gets.
RETIRED_POLICY_KEYS = {
    "denied_patterns": (
        "denied_patterns was removed (#4098): it matched text, which `=~'.*'` or "
        "`without(tenant, b)` walked past. Its tenant-isolation check is "
        "preserved_labels (every alert's result keeps the labels listed, judged "
        "on Prometheus' parse tree); its `=~\".*\"` selects what the selector "
        "without that matcher selects, so it guarded nothing. Delete the key."),
}

# A file_overrides entry with require_generated_marker only applies when this
# token appears in the file's first few lines (compile_custom_alerts._render
# writes "# GENERATED from ... — DO NOT EDIT." into the header block).
GENERATED_MARKER = "GENERATED"
GENERATED_MARKER_SCAN_LINES = 10

# An override may relax ONLY these policy keys. The deny-list's structural
# integrity checks (preserved labels, required labels, group interval)
# must NEVER be disable-able via a per-file override, even for a compiled pack —
# those guard against compiler bugs, not just tenant misuse. Anything outside
# this set in an override's `policy` block is refused when the policy is
# loaded (#3977), so a stray or malicious `required_labels: []` cannot widen
# the exemption.
OVERRIDABLE_POLICY_KEYS = frozenset({"denied_functions", "max_range_duration"})


def _string_list_problem(value) -> "str | None":
    """Why *value* is not a list of non-empty strings (a ``denied_functions``),
    or None. A string is no list: it was matched character by character."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        return "must be a list of non-empty strings"
    return None


def duration_seconds(value) -> "float | None":
    """Seconds of the Prometheus duration *value* (``90m``, ``1h30m``, ``2w``),
    or None when Prometheus' ``model.ParseDuration`` refuses it (#4061: the
    one grammar of every duration this lint reads — policy values, ranges,
    group intervals; ``_lib_validation.am_duration_seconds``, pinned to Go by
    tests/shared/am_duration_matrix.json). Only a str is a duration: an
    unquoted ``5`` has no unit."""
    return am_duration_seconds(value) if isinstance(value, str) else None


def _duration_problem(value) -> "str | None":
    """Why *value* is not a positive Prometheus duration (``90m``, ``1h30m``),
    or None. ⛔ One the checks cannot read (``banana``, ``0s``, ``1.5h``, an
    unquoted ``5``) turned the check off for the files it covered (#3977,
    #4061)."""
    if not duration_seconds(value):
        return "must be a positive Prometheus duration such as 90m or 1h30m"
    return None


#: How each policy value is judged — top level (#4061) and an override's
#: (#3977). ⛔ The one definition: a value none of these reads as it is
#: meant (a string where a list is, ``banana`` for a duration) turned its
#: check off, or crashed the run.
POLICY_VALUE_PROBLEMS = {
    "denied_functions": _string_list_problem,
    "preserved_labels": _string_list_problem,
    "required_labels": _string_list_problem,
    "max_range_duration": _duration_problem,
    "max_evaluation_interval": _duration_problem,
}
assert set(POLICY_VALUE_PROBLEMS) >= OVERRIDABLE_POLICY_KEYS

#: The keys of a ``file_overrides`` entry (``path`` required).
OVERRIDE_ENTRY_KEYS = ("path", "require_generated_marker", "policy")


def file_overrides_problems(overrides) -> "list[str]":
    """Every way *overrides* (a policy's ``file_overrides``) is not what
    ``resolve_file_policy`` reads (#3977): a list of mappings, each with a
    non-empty ``path`` (no two alike), ``require_generated_marker`` a bool
    when given, ``policy`` a mapping of OVERRIDABLE_POLICY_KEYS with values
    as POLICY_VALUE_PROBLEMS judges them. ⛔ Judged whether or not a file
    matches: an unknown key was silent, a disallowed one a finding only on a
    matching file, a non-mapping entry skipped, a list ``policy`` a crash."""
    if not isinstance(overrides, list):
        return [f"file_overrides must be a list of entries, not {type(overrides).__name__}"]
    out, seen = [], set()
    for i, entry in enumerate(overrides):
        at = f"file_overrides[{i}]"
        if not isinstance(entry, dict):
            out.append(f"{at} must be a mapping, not {type(entry).__name__}")
            continue
        unknown = sorted(safe_label(str(k)) for k in entry if k not in OVERRIDE_ENTRY_KEYS)
        if unknown:
            out.append(f"{at} has no key(s) {', '.join(unknown)} "
                       f"(an entry has {', '.join(OVERRIDE_ENTRY_KEYS)})")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            out.append(f"{at}.path must be a non-empty string")
        elif path in seen:
            out.append(f"{at}.path {safe_label(path)} is given twice: the first entry would "
                       "shadow this one")
        else:
            # an earlier entry whose path this one ends with matches this
            # one's files first (_override_path_matches) — and the reverse
            for other in sorted(seen):
                if path.endswith("/" + other) or other.endswith("/" + path):
                    out.append(f"{at}.path {safe_label(path)} and {safe_label(other)} "
                               "can match the same file: which entry applies would "
                               "depend on their order")
            seen.add(path)
        if not isinstance(entry.get("require_generated_marker", True), bool):
            out.append(f"{at}.require_generated_marker must be true or false")
        ov_policy = entry.get("policy", {})
        if not isinstance(ov_policy, dict):
            out.append(f"{at}.policy must be a mapping, not {type(ov_policy).__name__}")
            continue
        disallowed = sorted(safe_label(str(k)) for k in ov_policy
                            if k not in OVERRIDABLE_POLICY_KEYS)
        if disallowed:
            out.append(f"{at}.policy may only relax "
                       f"{', '.join(sorted(OVERRIDABLE_POLICY_KEYS))}, not "
                       f"{', '.join(disallowed)}")
        for key in sorted(OVERRIDABLE_POLICY_KEYS & set(ov_policy)):
            why = POLICY_VALUE_PROBLEMS[key](ov_policy[key])
            if why:
                out.append(f"{at}.policy.{key} {why}")
    return out


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


def _ms(seconds) -> int:
    """A duration's seconds (``duration_seconds``, Prometheus' ParseDuration:
    whole milliseconds) as whole milliseconds, to compare with da-read's
    ``window_ms`` without float rounding (#4080)."""
    return round(seconds * 1000)


def lint_expr(expr, policy, filepath, rule_name, reading=None):
    """Check a PromQL expr string against policy. Returns list of LintResult.

    *reading* is the expression's ``_lib_consumer_read.promql_reads`` entry
    (``lint_file`` reads a file's expressions in one call); read here when not
    given. ⛔ Ranges and function names are Prometheus' reading of the
    expression, never its text (#4080): a regex did not see ``[1w]``,
    ``[1h30m]``, ``[(30d)]``, a subquery or ``topk by (a) (…)``, and saw
    ``[2h]`` in a label value."""
    results = []
    if not expr:
        return results
    if reading is None:
        reading = _lib_consumer_read.promql_reads([expr])[0]
    if reading["problem"] is not None:
        results.append(LintResult(
            filepath, rule_name, None, "ERROR",
            f"Prometheus cannot evaluate the expression: {reading['problem']}"
        ))

    # Check denied functions: a function or an aggregation, by name
    denied = set(policy.get("denied_functions", []))
    for name in dict.fromkeys(c["name"] for c in reading["calls"]):
        if name in denied:
            results.append(LintResult(
                filepath, rule_name, None, "ERROR",
                f"denied function '{name}' in expr"
            ))

    # Check the window each selector reads: its range plus the subqueries
    # around it (#4080; offset and the lookback delta only move it)
    max_range = policy.get("max_range_duration")
    if max_range:
        max_secs = duration_seconds(max_range)
        if max_secs:
            for sel in reading["selectors"]:
                if sel["window_ms"] <= _ms(max_secs):
                    continue
                parts = ""
                if sel["subqueries"]:
                    parts = (" (" + " + ".join(
                        [f"range {sel['range']}"] * bool(sel["range_ms"])
                        + [f"subquery {d}" for d in sel["subqueries"]]) + ")")
                results.append(LintResult(
                    filepath, rule_name, None, "ERROR",
                    f"selector {sel['selector']} reads [{sel['window']}]{parts}, "
                    f"exceeds max allowed [{max_range}]"
                ))

    return results


def _copies_own_value(value, label) -> bool:
    """Whether the alert label value *value* is the template that copies the
    series' own *label* (``{{ $labels.tenant }}``, ``{{ .Labels.tenant }}``,
    with or without spaces or trim markers)."""
    return re.fullmatch(r"\{\{-?\s*(?:\$labels|\.Labels)\.%s\s*-?\}\}" % re.escape(label),
                        value) is not None


def preserved_label_targets(alerts, policy, filepath):
    """Which of *alerts* (``[(rule_name, expr, labels)]``) need the label
    flow of which preserved label, and the findings no flow is needed for
    (#4098). By the alert's own value of the label: none, or the template
    copying the series' own value → the result must keep it (judged by
    ``lint_preserved``); a literal (a single-tenant rule, or "" for none) →
    the alert sets it whatever the result holds, nothing to judge; any other
    template → an ERROR, it writes the label from something else.
    Returns ({label: [(rule_name, expr)]}, [LintResult])."""
    targets, results = {}, []
    for label in policy.get("preserved_labels", []):
        for name, expr, labels in alerts:
            value = (labels or {}).get(label)
            if value is None or (isinstance(value, str) and _copies_own_value(value, label)):
                targets.setdefault(label, []).append((name, expr))
            elif isinstance(value, str) and "{{" in value:
                results.append(LintResult(
                    filepath, name, None, "ERROR",
                    f"label '{label}' is a template that does not copy the series' own "
                    f"'{label}' ({{{{ $labels.{label} }}}}): it writes the label from something "
                    f"else, so the alert can reach another {label}"))
    return targets, results


def lint_preserved(targets, records, filepath, flow_of=None):
    """ERRORs for the alerts of *targets* (``preserved_label_targets``) whose
    result does not keep the label, read on Prometheus' parse tree (``da-read
    promql-label-flow``, following *records* — the recording rules of the
    run, ``{name: [{"expr", "labels"}]}``). *flow_of(label, exprs, records)*
    defaults to ``_lib_consumer_read.promql_label_flow``. ⛔ "unknown" is an
    ERROR too: a check that passes what it cannot prove is fail-open."""
    if flow_of is None:
        flow_of = _lib_consumer_read.promql_label_flow
    results = []
    for label, rules in targets.items():
        verdicts = flow_of(label, [e for _, e in rules], records)
        for (name, _), v in zip(rules, verdicts):
            if v["result"] == "dropped":
                results.append(LintResult(
                    filepath, name, None, "ERROR",
                    f"the result drops label '{label}' (an aggregation without it, "
                    f"without({label}), a one-to-one match not on it, a function with no "
                    f"labels…), so the alert's {label} is empty: keep '{label}' in every "
                    f"aggregation and match"))
            elif v["result"] != "kept":
                results.append(LintResult(
                    filepath, name, None, "ERROR",
                    f"cannot prove the result keeps label '{label}' (a label_replace or "
                    f"group_left that writes it from another series, a recording rule not "
                    f"in this run, a metric name that is not an equality): keep '{label}' "
                    f"from the series it was read from, and lint the rule files it reads "
                    f"together with it"))
    return results


def rule_records(rule_files):
    """The recording rules of *rule_files* (``RuleFile``s, the ones
    Prometheus would load) as label flow reads them: ``{name: [{"expr",
    "labels"}]}``, the labels Prometheus writes — the group's, then the
    rule's (which win)."""
    out = {}
    for rf in rule_files:
        if rf.problem is not None:
            continue
        for g in rf.groups:
            if not isinstance(g, dict):
                continue
            for r in g.get("rules") or []:
                if isinstance(r, dict) and "record" in r:
                    out.setdefault(r["record"], []).append(
                        {"expr": r.get("expr", ""),
                         "labels": {**(g.get("labels") or {}), **(r.get("labels") or {})}})
    return out


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

    max_secs = duration_seconds(max_interval)
    actual_secs = duration_seconds(interval)
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
    """{path: [RuleFile, ...]} — the rule files each of *files* hands to
    Prometheus, as Prometheus loads them: `_lib_consumer_read.rule_files_in`
    (#3426) — plain rule files, ConfigMaps' values and PrometheusRules'
    `spec`. Every file given is one the caller chose to lint (`--exclude`
    leaves the others out): a file that is neither a manifest nor a rule
    file (a recipe, the registry beside the packs) is a RuleFile with
    ``unread`` set and its readers' reason; a rule file the loader refuses
    one with the loader's reason — both ERRORs in `lint_file`, never
    skipped. Raises ``_lib_consumer_read.ConsumerReadError`` when a binary
    cannot be had."""
    return _lib_consumer_read.rule_files_in(files, keep_refusals=True)


def lint_file(filepath, policy, rule_files=None, *, flag=None, selected=False, records=None):
    """Lint a single YAML rule file (or a ConfigMap holding rule files).
    *rule_files* is its reading from ``read_rule_files``; read here when not
    given. *records* are the recording rules of the run (``rule_records``)
    the preserved-label check follows; this file's own when not given.
    Returns (list of LintResult, rule_count).

    *selected*: the file was selected from a declared input (`_lib_input`:
    ``--rule-pack-dir``, named by *flag*, or the scan paths, a positional
    with no flag to name, #3901): its bytes are read as `_lib_input` reads a
    selected file — gone or unreadable since the input was judged is a
    refusal naming it, rc 2 with or without ``--ci`` (#3724), not one ERROR
    among the results. Content that is not UTF-8 stays this file's ERROR:
    what is linted is the file's content."""
    results = []
    rule_count = 0
    try:
        if selected or flag is not None:
            content = _lib_input.read_bytes(filepath, flag=flag, lang="en").decode("utf-8")
        else:
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
    # by its rule loader, a ConfigMap (data: key: |) or PrometheusRule the
    # way kubectl reads it and each data value / its `spec` by the rule
    # loader. A file or data value the loader refuses is an ERROR naming it,
    # with the loader's reason; it is never skipped (a ConfigMap value with
    # a `groups:` key that did not parse used to be dropped silently). #3426:
    # a file that is no manifest and no rule file is an ERROR too — which
    # files are linted is the caller's choice, by path (`--exclude`).
    if rule_files is None:
        rule_files = read_rule_files([filepath])[filepath]
    groups = []
    for rf in rule_files:
        if rf.problem is not None:
            where = f" (data key {rf.key!r})" if rf.key is not None else ""
            if rf.unread:
                msg = (f"no reader reads it as a manifest or a rule file: {rf.problem} "
                       f"({_lib_consumer_read.EXCLUDE_HINT})")
            else:
                msg = f"Prometheus' rule loader refuses it{where}: {rf.problem}"
            results.append(LintResult(filepath, None, None, "ERROR", msg))
            continue
        groups.extend(rf.groups)

    if not groups:
        return results, rule_count

    # #4080: every expression of the file read by Prometheus in one call
    exprs = [rule.get("expr", "") for group in groups if isinstance(group, dict)
             for rule in group.get("rules", []) if isinstance(rule, dict)]
    readings = iter(_lib_consumer_read.promql_reads([e for e in exprs if e]))
    alerts = []

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
            results.extend(lint_expr(expr, policy, filepath, rule_name,
                                     next(readings) if expr else None))
            results.extend(lint_labels(labels, policy, filepath, rule_name, is_recording))
            if not is_recording and expr:
                alerts.append((rule_name, expr, labels))

            # Governance checks (Tier 3 best practices)
            if not is_recording:
                results.extend(check_expiry_label(labels, filepath, rule_name))
                results.extend(check_owner_label(labels, filepath, rule_name))

    # #4098: every alert's result keeps the preserved labels (records are
    # followed, not judged themselves: one is read by the alerts that read it)
    targets, found = preserved_label_targets(alerts, policy, filepath)
    results.extend(found)
    if targets:
        results.extend(lint_preserved(
            targets, rule_records(rule_files) if records is None else records, filepath))

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
    policy entry is stale. ⛔ *policy* must be ``load_policy``'s: its
    file_overrides were judged there (``file_overrides_problems``, #3977) and
    are read here as judged — a malformed one handed in directly raises.
    """
    norm = Path(filepath).as_posix()
    for override in policy.get("file_overrides", []):
        ov_path = override["path"]
        if not _override_path_matches(norm, ov_path):
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
        effective = policy.copy()
        effective.update(override.get("policy", {}))
        return effective, []
    return policy, []


#: --policy: the custom-rule lint policy, a YAML mapping merged over the
#: built-in one. An empty (or null) document reads as no override, declared
#: here (`empty_is`), as the old ``or {}`` did — but that ``or {}`` also read
#: ``[]``, ``0`` and ``""`` as no override and linted against the built-in
#: policy at rc 0 (#3445): any top level that is not a mapping is rc 2 now,
#: named, as is a path or content that cannot be read.
POLICY_DOCUMENT = _lib_input.Document("yaml", dict, empty_is=dict)


def load_policy(custom, path=None):
    """The policy to lint against: the built-in policy (the DEFAULT, not a
    fallback for a broken ``--policy``) with *custom* — the parsed
    --policy (``_lib_input.document``), None when not given — merged over it.

    ⛔ #1556: the previous version warned on stderr and carried on with the
    built-in policy, so `lint --policy <typo>` linted against different rules
    than the operator asked for and still exited 0. A warning nobody reads in
    CI is not a control. dev-rules #13 files "檔案/路徑不存在" and
    "malformed 輸入" under EXIT_CALLER_ERROR. #3445: the reading is
    ``_lib_input.document``'s — ``''`` (an unset shell variable) is supplied
    and refused there, never taken for "not given".
    """
    if custom is not None:
        retired = [RETIRED_POLICY_KEYS[k] for k in sorted(RETIRED_POLICY_KEYS) if k in custom]
        if retired:
            raise _lib_exitcodes.CallerError(
                f"--policy {safe_label(path)}: " + " ".join(retired), "policy_unknown_key")
        unknown = sorted(safe_label(str(k)) for k in custom if k not in POLICY_KEYS)
        if unknown:
            from _grar_validate import POLICY_KEYS as deployment_keys  # noqa: PLC0415
            deploy = [k for k in unknown if k in deployment_keys]
            hint = (f" {', '.join(deploy)} belong{'s' if len(deploy) == 1 else ''} to the "
                    "deployment policy: give that file to `--policy` of validate-config "
                    "or generate-routes, and this one to `validate-config --lint-policy`."
                    if deploy else "")
            raise _lib_exitcodes.CallerError(
                f"--policy {safe_label(path)}: the custom-rule lint policy has no key(s) "
                f"{', '.join(unknown)} "
                f"(it has {', '.join(sorted(POLICY_KEYS))}).{hint}", "policy_unknown_key")
    # ⛔ deepcopy, not `.copy()`: the latter is shallow, so a caller
    # appending to one of the nested lists reaches into the module default
    # and changes what every later call in the process returns. Same shape
    # as #1616's load_base_config fix; fixed here for symmetry rather than
    # because a mutating caller exists today.
    merged = copy.deepcopy(DEFAULT_POLICY)
    if custom is not None:
        merged.update(custom)   # custom overrides defaults, key by key
    # #4061 / #3977: every value read below is judged here, the built-in
    # ones too — matched or not, the same answer.
    problems = [f"{key} {why}" for key, judge in POLICY_VALUE_PROBLEMS.items()
                for why in [judge(merged[key])] if why]
    problems += file_overrides_problems(merged["file_overrides"])
    if problems:
        source = f"--policy {safe_label(path)}" if custom is not None else "the built-in policy"
        raise _lib_exitcodes.CallerError(f"{source}: " + "; ".join(problems), "policy_invalid")
    return merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def collect_files(paths, exclude=()):
    """The files to lint: `_lib_consumer_read.files_to_read` (#3426) — a
    file as given, a directory's *.yaml / *.yml recursively, less the ones
    an *exclude* pattern matches."""
    return _lib_consumer_read.files_to_read(paths, exclude)


def main():
    """CLI entry point: Custom Rule deny-list linter。."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Lint custom Prometheus rules against platform governance policy."
    )
    # #3901: the scan targets are a declared input — each a file or a
    # directory, judged once by `selected` (below): not there, '' (an unset
    # shell variable: never the current directory), a parent that cannot be
    # entered, an entry that cannot be chosen are rc 2 naming the path, the
    # same with and without --ci (#1618: `lint /typo/dir --ci` was a clean
    # scan). Targets that are there and hold no YAML are a no-op without
    # --ci and a caller error with it (#3462: EMPTY_ALLOWED, judged below).
    _lib_input.add_input(
        parser, "paths", select=_lib_consumer_read.FILES_TO_READ, many=True,
        empty=_lib_input.EMPTY_ALLOWED,
        remedy="Do not drop the path to clear this — an absent target is not a clean one.",
        help="YAML file(s) or directory to scan (or --rule-pack-dir instead)"
    )
    # #3755: what --rule-pack-dir selects is declared (`_lib_input`, the
    # rule-pack declaration RULE_PACKS) and judged once by `selected`: not
    # there, no directory or unlistable is rc 2 naming the flag. An empty
    # selection is a run over nothing as this tool reads it (EMPTY_ALLOWED):
    # an advisory note at rc 0, a caller error under --ci (below).
    _lib_input.add_input(
        parser, "--rule-pack-dir", select=_lib_consumer_read.RULE_PACKS,
        empty=_lib_input.EMPTY_ALLOWED, metavar="DIR",
        help="Lint the rule packs of a rule-packs directory as declared "
             "(rule-pack-*.yaml|yml at its top level; the registry beside them is "
             "declared too, any other YAML there is a caller error). The declared "
             "platform packs are not linted: this policy governs tenant packs. "
             "Instead of paths; not with --exclude"
    )
    _lib_input.add_input(
        parser, "--policy", decode=POLICY_DOCUMENT,
        help="Path to custom-rule-policy.yaml (default: built-in policy)"
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: exit with non-zero code on any ERROR; selecting no file "
             "is a caller error (exit 2)"
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="Leave out files this glob matches (repeatable; not read). A GLOB "
             "without '/' matches the file name at any depth; one with '/' the "
             "path relative to the directory given (fnmatch, case-sensitive, '*' "
             "crosses '/'). Every file that is not excluded must be a rule file or "
             "a ConfigMap / PrometheusRule manifest, or it is an ERROR"
    )
    args = parser.parse_args()
    if (args.rule_pack_dir is None) == (not args.paths):
        die_caller_error("give scan paths or --rule-pack-dir DIR (exactly one of them)")
    if args.rule_pack_dir is not None and args.exclude:
        die_caller_error("--exclude does not go with --rule-pack-dir: the rule packs are "
                         "declared by _lib_consumer_read.rule_pack_files, not by patterns")

    # ⛔ Do not drop --policy to clear a refusal here — that silently lints
    # against the built-in policy instead of yours.
    policy = load_policy(_lib_input.document(args, "policy", lang="en"), args.policy)

    # #3426: which files are linted is a declaration by path — the rule
    # packs of a rule-packs directory (RULE_PACKS, the one declaration), or
    # the targets less `--exclude`. An entry that cannot be chosen (an
    # undeclared YAML beside the packs, a dangling symlink), and an
    # `--exclude` that leaves nothing, are caller errors, named.
    try:
        if args.rule_pack_dir is not None:
            # #3755: the one judgement and the one listing of the directory
            # (`_lib_input.selected`; a CallerError is rc 2 naming the flag).
            # #1595 D1a: the custom-rule policy (required `tenant` label, the
            # tenant-isolation deny-list) governs TENANT packs. A declared
            # platform pack (`PLATFORM_PACKS`) watches the platform and has no
            # tenant by design, so it is not this policy's to judge.
            files = [str(f) for f in _lib_input.selected(args, "rule_pack_dir", lang="en")
                     if not _lib_consumer_read.is_platform_pack(
                         _lib_consumer_read.RULE_PACKS.member(f))]
        else:
            # #3901: the one judgement of the scan targets, and their files.
            files = _lib_input.selected(args, "paths", lang="en", exclude=args.exclude)
    except _lib_consumer_read.InputRefused as exc:
        label = ("--rule-pack-dir: " if args.rule_pack_dir is not None else "")
        die_caller_error(f"ERROR: {label}{_lib_consumer_read.error_text(exc)}")

    if not files:
        # #3462: under --ci a run that selected no file is a caller error, not
        # a pass — `lint <dir with no YAML> --ci` was rc 0, a governance gate
        # green over nothing (owner, 2026-11-02). Without --ci it stays the
        # advisory note below, rc 0.
        if args.ci:
            if args.rule_pack_dir is not None:
                label = f"--rule-pack-dir {safe_label(args.rule_pack_dir)}"
                wanted = (f"{_lib_consumer_read.RULE_PACKS.wanted}, less the declared "
                          "platform packs")
            else:
                label = ", ".join(safe_label(p) for p in args.paths)
                wanted = _lib_consumer_read.FILES_TO_READ_WANTED
            raise _lib_input.refuse_empty(label, wanted, lang="en")
        print("No YAML files found under: "
              + ", ".join(repr(p) for p in (args.paths or [args.rule_pack_dir])))
        sys.exit(EXIT_OK)

    all_results = []
    files_checked = 0
    rules_checked = 0

    try:
        readings = read_rule_files(files)
    except _lib_consumer_read.ConsumerReadError as exc:
        die_caller_error(f"ERROR: rule files: {_lib_consumer_read.error_text(exc)}")
    # #4098: an alert may read a record of another file of the run
    records = rule_records([rf for f in files for rf in readings[f]])

    for filepath in files:
        try:
            results, count = lint_file(
                filepath, policy, readings[filepath],
                flag="--rule-pack-dir" if args.rule_pack_dir is not None else None,
                selected=True, records=records)
        except _lib_consumer_read.ConsumerReadError as exc:
            # #4080: da-read cannot read the file's expressions (missing,
            # older than lint, a fault): the reader's problem, named — no
            # traceback read as a defect of lint
            die_caller_error(f"ERROR: {safe_label(filepath)}: expressions: "
                             f"{_lib_consumer_read.error_text(exc)}")
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
