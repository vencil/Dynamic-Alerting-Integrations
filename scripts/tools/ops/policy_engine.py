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

`--config-dir` 讀的是生效值，不是租戶檔的字面內容（#2115 0-B；`load_policy_inputs`）。
規則 target 依 key 種類讀不同來源，`when` 同一套：

  閾值（不以 `_` 開頭）— exporter 在 /metrics 實際發出的數字（`da-guard served-values
      --schedules`）。值寫在根 `defaults:`、平台檔 `tenants:`、租戶檔或子目錄
      `_defaults.yaml` 都一樣；排程閾值逐時段比對，任一時段違規即違規（訊息帶 UTC 時段）。
      某時段 `disable`（served 在該段沒有列）：`required` 算違規，`forbidden` 不算，
      其他運算子略過該段；整天都不發的鍵視同沒有（`required` 違規、其他略過）。
      target 用舊拼法（別名）時，以 served-values 的 `aliases` 表換成現行拼法。
  保留鍵（`_` 開頭，`_routing` 除外）— 寫法＋繼承（`da-guard effective` 的
      `effective_config`）；Go 自動補的預設值（`_severity_dedup`、`_metadata` 空欄位等）不算有寫。
  `_routing` — 路由產生器解析後的結果（`_routing_defaults` 逐層、routing profile、
      租戶 `_routing` 合併後，`{{tenant}}` 已代換）；只在有規則讀到它時才載入。

exporter 丟掉或讀不到的檔、da-guard 拒收的樹（例如根 `defaults:` 與租戶都寫 `X_critical`，
#2115 F0）、路由產生器拒收的樹，一律 exit 2，不評估任何規則。
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union

import yaml

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lib_confd import resolve_defaults_file  # noqa: E402  (#1588)

# ---------------------------------------------------------------------------
# Repo-layout import compatibility: the repo subdir layout needs the
# parent dir. Shipped unmodified; in the flat image it points at /opt,
# which is harmless (#2313).
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from _lib_python import (
        YamlFileError,
        detect_cli_lang,
        format_json_report,
        load_yaml_file,
        parse_duration_seconds,
        safe_label,
    )
except ImportError:
    from scripts.tools._lib_python import (  # type: ignore[no-redef]
        YamlFileError,
        detect_cli_lang,
        format_json_report,
        load_yaml_file,
        parse_duration_seconds,
        safe_label,
    )
# #2123: policy YAML is read strictly — a key written twice in one mapping is
# a YAMLError (YamlFileError from the file reader), not PyYAML's last value.
# #2114: on that same strict read, `exclude_tenants` items are source text
# (the `*_exporter_keys` variants compose the two loaders in `_lib_io`).
try:
    from _lib_io import (
        load_yaml_file_strict, load_yaml_file_strict_exporter_keys,
        strict_load_exporter_keys, strict_safe_load,
    )
except ImportError:
    from scripts.tools._lib_io import (  # type: ignore[no-redef]
        load_yaml_file_strict, load_yaml_file_strict_exporter_keys,
        strict_load_exporter_keys, strict_safe_load,
    )

# #2115 0-B: the values a rule reads come from Go (served-values / effective).
from _lib_tenant_values import (  # noqa: E402
    DaGuardError,
    DaGuardNotFoundError,
    ParseFailedError,
    ServedValuesError,
    load_effective,
    load_served_tree,
    print_load_error,
    print_load_warnings,
    written_config,
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

# #2114: keys whose list items are tenant ids — read as source text, the way
# `load_tenant_configs` reads the tenant keys they are compared with.
POLICY_TENANT_LISTS = frozenset({"exclude_tenants"})


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

    ``exclude_tenants`` 的項目以**原始文字**讀入（#2114）：租戶 id 是
    exporter 讀到的 key 文字（``010`` 就是 ``"010"``），排除清單必須用同一種
    讀法比對，否則 ``exclude_tenants: [010]`` 會被 PyYAML 讀成 ``8``、
    對不上租戶 ``"010"``。
    """
    return rules_from_policy_data(load_yaml_file_strict_exporter_keys(
        source, raw_text_sequences=POLICY_TENANT_LISTS))


def rules_from_policy_data(data: Any) -> list[PolicyRule]:
    """把已解析的 policy YAML（頂層 mapping）轉成 PolicyRule 清單。

    ``None``（空檔）或非 mapping 的頂層 → ``[]``。CLI 的 ``--policy`` 守衛在
    呼叫前就把非 mapping 當 caller error 擋掉；這裡的 ``isinstance`` 是給
    ``load_policies``（library 呼叫端）用的，避免 ``"policies" in "a str"``
    變成子字串測試。
    """
    if not isinstance(data, dict):
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


#: `resolve_defaults_file(skipped_note=...)` for a reader that takes
#: `_policies` from the root carrier and its tenants from the exporter's own
#: load (#2115 0-B): the nested carriers are named, the tenants are not.
POLICIES_SKIPPED_NOTE = (
    "`_policies` are read from the root defaults carrier only (tenants in "
    "subdirectories are evaluated), so a `_policies` list would not be read from these")

# ---------------------------------------------------------------------------
# What a rule reads (#2115 0-B)
# ---------------------------------------------------------------------------
class ServedThreshold(NamedTuple):
    """One threshold key as /metrics serves it over the whole UTC day.

    `segments` are da-guard served-values `--schedules`' segments for the
    key, `(start, end, value)` with `"HH:MM"` bounds; `value` is None in a
    segment in which the key has no /metrics row (a `disable` window). The
    segments are the exporter's reading of the schedule; nothing here reads
    `overrides:` itself. A key without a schedule is one segment,
    `00:00`-`24:00`.
    """
    segments: tuple[tuple[str, str, Optional[float]], ...]


class Unserved(NamedTuple):
    """A threshold key the tenant's config carries (written or inherited)
    that /metrics does not serve at all (served-values `unserved`: switched
    off, or no row to inherit into). `raw` is the value as written. A rule
    finds the key but has no served number to check: `required` is violated
    with a message saying it IS configured; every other operator skips it."""
    raw: Any


class PolicyInputs(NamedTuple):
    """What `load_policy_inputs` reads: `views` is `{tenant: {key: value}}`
    in the shape `evaluate_policies` takes, `aliases` the exporter's alias
    table (retired base key -> canonical base key) rule targets are read
    through."""
    views: dict[str, dict[str, Any]]
    aliases: dict[str, str]


class RoutingTreeRefused(Exception):
    """The route generator refuses the tree (an unreadable tenant file, a
    tenant in two files, a routing-tree error, an invalid tenant id), so
    there is no resolved `_routing` to read. `lines` are its own words."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        super().__init__(self.lines[0] if self.lines else "the route generator refuses this tree")


_ROUTING = "_routing"


def rules_read_routing(rules: list[PolicyRule]) -> bool:
    """Does any rule (its target or its `when` target) read `_routing`?
    Only then is the route generator's reader run (`load_policy_inputs`)."""
    for rule in rules:
        targets = [rule.target]
        if rule.when:
            targets.append(str(rule.when.get("target", "")))
        for target in targets:
            if target and fnmatch.fnmatch(_ROUTING, target.split(".", 1)[0]):
                return True
    return False


def _resolved_routing(config_dir: str) -> dict[str, dict]:
    """`{tenant: routing}` as the route generator resolves it: the
    `_routing_defaults` chain, the routing profile and the tenant's own
    `_routing`, merged, `{{tenant}}` substituted. A tenant with nothing to
    route is absent. Raises `RoutingTreeRefused` for a tree the generator
    refuses in every mode."""
    import generate_alertmanager_routes as gar  # the generator's own reader

    # The generator's reader prints its WARN lines to stdout; this tool's
    # stdout is the report (one JSON document under --json).
    with contextlib.redirect_stdout(sys.stderr):
        tree = gar.load_tenant_tree(config_dir)
    _rc, lines = gar.tree_refusal(tree.files_read, tree.tenant_file_errors,
                                  tree.duplicate_tenants, tree.routing_tree_problems,
                                  tree.invalid_tenant_ids)
    if lines:
        raise RoutingTreeRefused(lines)
    return tree.routing_configs


def load_policy_inputs(config_dir: str, *, routing: bool = False) -> PolicyInputs:
    """Every tenant of the conf.d tree at `config_dir`, as the rules read it
    (#2115 (c), by kind of key):

    * a threshold (no `_` prefix) — `ServedThreshold`, what /metrics serves
      over the whole day (`da-guard served-values --schedules`), canonical
      spelling; a key /metrics serves at no minute of the day is absent;
      a threshold key the config carries but /metrics never serves is an
      `Unserved` (its value as written);
    * a reserved key (`_` prefix) other than `_routing` — as written plus
      inherited (`_lib_tenant_values.written_config`: `da-guard effective`'s
      `effective_config`, only the reserved keys a tenant may carry, and
      `_metadata` as /metrics inherits it minus the exporter's empty fill):
      a default the exporter fills in itself is not there;
    * `_routing` — only with `routing=True`: the route generator's resolved
      routing (`_resolved_routing`); absent otherwise.

    Prints da-guard's stderr and one WARN per file the load serves no tenant
    from (`print_load_warnings`). Raises what `load_served_tree` /
    `load_effective` raise; `ServedValuesError` when the two disagree on the
    tenants or a segment of the day cannot be gathered; `RoutingTreeRefused`.
    """
    tree = load_served_tree(config_dir, schedules=True)
    print_load_warnings(tree)
    effective = load_effective(config_dir)
    if set(effective) != set(tree.tenants):
        raise ServedValuesError(
            "da-guard served-values and da-guard effective disagree on the tenants of this tree: "
            f"{sorted(set(effective) ^ set(tree.tenants))}", None, "")
    routed = _resolved_routing(config_dir) if routing else {}

    views: dict[str, dict[str, Any]] = {}
    for tenant, served in tree.tenants.items():
        view: dict[str, Any] = {
            k: v for k, v in written_config(effective[tenant], served).items()
            if k.startswith("_") and k != _ROUTING}
        for key, raw in served.unserved.items():
            if not str(key).startswith("_"):
                view[_canonical_key(str(key), tree.aliases)] = Unserved(raw)
        if tenant in routed:
            view[_ROUTING] = routed[tenant]
        for key, day in (served.schedules or {}).items():
            for seg in day.segments:
                if seg.error is not None:
                    raise ServedValuesError(
                        f"da-guard served-values: tenant {tenant!r}: /metrics cannot be gathered "
                        f"in {seg.start}-{seg.end} UTC, so no rule can be checked there ({seg.error})",
                        None, "")
            segments = tuple((seg.start, seg.end, seg.value) for seg in day.segments)
            if any(v is not None for _s, _e, v in segments):
                view[key] = ServedThreshold(segments)
        views[tenant] = view
    return PolicyInputs(views, dict(tree.aliases))


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------
_CRITICAL = "_critical"


def _canonical_key(key: str, aliases: Optional[dict[str, str]]) -> str:
    """A threshold key in the exporter's canonical spelling: a retired base
    key (also as `<base>_critical` / `<base>{...}`) through `aliases`.
    Reserved keys and keys not in the table are returned as they are."""
    if not aliases or key.startswith("_"):
        return key
    base, brace, dims = key.partition("{")
    if base in aliases:
        return aliases[base] + brace + dims
    if base.endswith(_CRITICAL) and base[:-len(_CRITICAL)] in aliases:
        return aliases[base[:-len(_CRITICAL)]] + _CRITICAL + brace + dims
    return key


def _is_wildcard(target: str) -> bool:
    return "*" in target and "." not in target


def _canonical_target(target: str, aliases: Optional[dict[str, str]]) -> str:
    """`target` with its first path element in canonical spelling (a
    wildcard is matched against both spellings instead, see
    `_resolve_wildcard_values`)."""
    if _is_wildcard(target):
        return target
    head, dot, rest = target.partition(".")
    return _canonical_key(head, aliases) + dot + rest


def _resolve_target(config: dict, target: str,
                    aliases: Optional[dict[str, str]] = None) -> tuple[bool, Any]:
    """解析 dot-path 目標，從 tenant 配置中取值。

    支援：
    - 精確 key：``_routing.receiver.type``
    - 萬用字元 key：``*_cpu``（fnmatch 匹配所有 key）

    Args:
        config: tenant 配置 dict。
        target: dot-separated path 或含萬用字元的 pattern。
        aliases: exporter 的別名表；給了的話，舊拼法的閾值 key 換成現行拼法再找（#2115）。

    Returns:
        (found, value) — found 表示是否找到，value 是解析到的值。
        萬用字元模式回傳所有匹配值的 dict。
    """
    # Wildcard pattern — match at top level
    if _is_wildcard(target):
        matches = dict(_resolve_wildcard_values(config, target, aliases))
        if matches:
            return True, matches
        return False, None

    # Dot-path navigation
    parts = _canonical_target(target, aliases).split(".")
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


def _resolve_wildcard_values(config: dict, target: str,
                             aliases: Optional[dict[str, str]] = None) -> list[tuple[str, Any]]:
    """解析萬用字元目標，回傳 (key, value) 清單。

    閾值 key 以現行拼法回傳；pattern 對現行拼法或任一舊拼法（別名）相符即算（#2115）。
    """
    retired: dict[str, list[str]] = {}
    for old, new in (aliases or {}).items():
        retired.setdefault(new, []).append(old)
    results = []
    for k, v in config.items():
        names = [k, *retired.get(k, [])]
        if k.endswith(_CRITICAL):
            names += [old + _CRITICAL for old in retired.get(k[:-len(_CRITICAL)], [])]
        if any(fnmatch.fnmatch(n, target) for n in names):
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


def _threshold_text(value: float) -> str:
    """A served threshold as text: `900` for 900.0, `0.5`, `+Inf` / `-Inf` / `NaN`."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if value.is_integer() else repr(value)


def _threshold_equals(value: float, expected: Any) -> bool:
    """A served threshold against a rule's value: as numbers when the rule's
    value is one (`900` / `"900"` / `900.0` all equal a served 900.0), as
    text otherwise."""
    e = _to_comparable(expected)
    if isinstance(e, float):
        return value == e
    return _threshold_text(value) == str(expected)


def _evaluate_threshold(operator: str, value: float, expected: Any) -> bool:
    """`_evaluate_operator` for one served threshold value (a float)."""
    if operator == "required":
        return True
    if operator == "forbidden":
        return False
    if operator in ("equals", "not_equals"):
        same = _threshold_equals(value, expected)
        return same if operator == "equals" else not same
    if operator == "one_of":
        return isinstance(expected, list) and any(_threshold_equals(value, e) for e in expected)
    if operator in ("gte", "lte", "gt", "lt"):
        return _evaluate_operator(operator, value, expected)
    return _evaluate_operator(operator, _threshold_text(value), expected)


def _failures(operator: str, actual: Any, expected: Any) -> list[tuple[Any, Optional[str]]]:
    """`(actual, window)` for every part of `actual` that violates the rule.

    A `ServedThreshold` is checked segment by segment (#2115 (c): every part
    of the day must pass); `window` names the UTC segment unless the key has
    only one. In a segment with no /metrics row `required` is violated and
    every other operator has nothing to check. `required` / `forbidden` give
    at most one finding per key, its `window` listing every failing segment.
    An `Unserved` key violates `required` only. Any other value is checked
    once, `window` None.
    """
    if isinstance(actual, Unserved):
        return [(actual, None)] if operator == "required" else []
    if isinstance(actual, ServedThreshold):
        whole_day = len(actual.segments) == 1
        out: list[tuple[Any, Optional[str]]] = []
        for start, end, value in actual.segments:
            window = None if whole_day else f"{start}-{end}"
            if value is None:
                if operator == "required":
                    out.append((None, window))
            elif not _evaluate_threshold(operator, value, expected):
                out.append((_threshold_text(value), window))
        if operator in ("required", "forbidden") and len(out) > 1:
            # A presence rule is one finding per key: the windows it fails in
            # are listed in that one message.
            values = list(dict.fromkeys(str(a) for a, _w in out if a is not None))
            out = [(" / ".join(values) if values else None, "、".join(w for _a, w in out if w))]
        return out
    return [] if _evaluate_operator(operator, actual, expected) else [(actual, None)]


def _holds(operator: str, actual: Any, expected: Any) -> bool:
    """A `when` condition on a found value: on a `ServedThreshold`, true when
    it holds in some segment that has a /metrics row."""
    if isinstance(actual, Unserved):
        return False
    if isinstance(actual, ServedThreshold):
        return any(v is not None and _evaluate_threshold(operator, v, expected)
                   for _s, _e, v in actual.segments)
    return _evaluate_operator(operator, actual, expected)


# ---------------------------------------------------------------------------
# Condition evaluation (when clause)
# ---------------------------------------------------------------------------
def _evaluate_when(config: dict, when: dict,
                   aliases: Optional[dict[str, str]] = None) -> bool:
    """評估 when 條件子句。

    when 結構：
        target: str — 要檢查的欄位
        operator: str — 運算子
        value: Any — 期望值（可選）

    讀值的規則與主規則相同（`_resolve_target`）。排程閾值：任一有發出值的時段成立即成立。
    有寫但 exporter 不發的鍵（`Unserved`）視同沒有：精確 target 與萬用字元展開的每個鍵皆然。

    Returns:
        True 表示條件成立（主規則應該評估），False 表示條件不成立（跳過）。
    """
    target = when.get("target", "")
    operator = when.get("operator", "required")
    value = when.get("value")

    if not target:
        return True

    # A key carried but not served (`Unserved`) is absent to a condition —
    # for an exact target and for every key a wildcard target expands to.
    found, actual = _resolve_target(config, target, aliases)
    if isinstance(actual, Unserved):
        found, actual = False, None
    elif _is_wildcard(target) and isinstance(actual, dict):
        actual = {k: v for k, v in actual.items() if not isinstance(v, Unserved)}
        if not actual:
            found, actual = False, None
    if operator == "required":
        return found and actual is not None
    if operator == "forbidden":
        return not found or actual is None

    if not found:
        return False

    return _holds(operator, actual, value)


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------
def evaluate_rule(rule: PolicyRule, tenant: str, config: dict,
                  aliases: Optional[dict[str, str]] = None) -> list[Violation]:
    """對單一 tenant 評估單一策略規則。

    Args:
        rule: 策略規則。
        tenant: tenant 名稱。
        config: tenant 配置 dict（`load_policy_inputs` 的 view，或一般 dict）。
        aliases: exporter 的別名表（`PolicyInputs.aliases`）。

    Returns:
        違規清單（空 = 通過）。
    """
    # Check tenant exclusion — text to text (#2114): tenant ids and the
    # `exclude_tenants` items are both the YAML source text.
    if tenant in rule.exclude_tenants:
        return []

    # Evaluate when condition
    if rule.when and not _evaluate_when(config, rule.when, aliases):
        return []

    def violation(target: str, actual: Any, window: Optional[str]) -> Violation:
        if isinstance(actual, Unserved):
            message = (f"'{target}' 已配置（值: {actual.raw}）但 exporter 不發出"
                       "（/metrics 沒有這一列，例如 disable 或沒有可繼承的預設值）")
        else:
            message = _format_violation_msg(rule.operator, target, actual, rule.value)
        if window is not None:
            message += f"（UTC {window}）"
        return Violation(tenant=tenant, rule_name=rule.name, description=rule.description,
                         severity=rule.severity, target=target, message=message)

    # Wildcard target — evaluate against all matching keys
    if _is_wildcard(rule.target):
        matched = _resolve_wildcard_values(config, rule.target, aliases)
        if rule.operator == "required" and not matched:
            return [Violation(
                tenant=tenant,
                rule_name=rule.name,
                description=rule.description,
                severity=rule.severity,
                target=rule.target,
                message=f"未找到匹配 '{rule.target}' 的配置項",
            )]
        return [violation(key, actual, window)
                for key, val in matched
                for actual, window in _failures(rule.operator, val, rule.value)]

    # Standard dot-path target
    found, actual = _resolve_target(config, rule.target, aliases)

    if rule.operator in ("required", "forbidden"):
        return [violation(rule.target, a, window)
                for a, window in _failures(rule.operator, actual if found else None, None)]

    if not found:
        # Non-existence operators other than required/forbidden — skip silently
        return []

    return [violation(rule.target, a, window)
            for a, window in _failures(rule.operator, actual, rule.value)]


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
    aliases: Optional[dict[str, str]] = None,
) -> PolicyResult:
    """對所有 tenant 評估所有策略規則。

    Args:
        rules: 策略規則清單。
        tenant_configs: {tenant_name: config_dict}（`load_policy_inputs(...).views`）。
        aliases: exporter 的別名表（`load_policy_inputs(...).aliases`）。

    Returns:
        PolicyResult 包含所有違規及統計。
    """
    result = PolicyResult(
        tenants_evaluated=len(tenant_configs),
        rules_evaluated=len(rules),
    )

    for tenant, config in sorted(tenant_configs.items()):
        for rule in rules:
            violations = evaluate_rule(rule, tenant, config, aliases)
            result.violations.extend(violations)

    return result


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


def _unreadable_yaml_exit(exc: YamlFileError, args, lang: str) -> int:
    """#1654: a conf.d file whose CONTENT cannot be read → rc 2, file named.

    ``_defaults.yaml`` and every tenant file reach ``_lib_io.load_yaml_file``
    with no handler; a ``\\xff`` byte in one of them used to be a
    ``UnicodeDecodeError`` traceback (rc 1, 0 bytes of stdout under
    ``--json``). Not the shared ``exit_on_yaml_file_error`` wrapper: this
    tool owes ``--json`` its ``caller_error`` envelope on every terminal
    path, same as the ``--policy`` ladder above.
    """
    label = safe_label(exc)   # untrusted filename in the message (#1538)
    print(f"無法讀取 {label}" if lang == "zh" else f"ERROR: cannot read {label}",
          file=sys.stderr)
    if args.json_output:
        print(format_json_report({
            "status": "caller_error",
            "reason": "yaml_file_unreadable",
            "tenants_evaluated": 0,
            "rules_evaluated": 0,
            "error_count": 0,
            "warning_count": 0,
            "passed": False,
            "violations": [],
        }))
    return EXIT_CALLER_ERROR


def _load_error_exit(exc: Exception, args, rules_evaluated: int) -> int:
    """#2115: the tenants could not be read the way the exporter reads them
    → rc 2, nothing evaluated. da-guard missing or failing, a file the
    exporter drops or cannot read (named, with da-guard's stderr below), a
    tree da-guard refuses, or one the route generator refuses (its own
    lines). The ``--json`` envelope as on every other caller-error path."""
    if isinstance(exc, RoutingTreeRefused):
        print("ERROR: the route generator refuses this tree, so `_routing` "
              "cannot be resolved:", file=sys.stderr)
        for line in exc.lines:
            print(safe_label(line), file=sys.stderr)
        reason = "routing_tree_refused"
    else:
        print_load_error(exc)
        reason = ("yaml_file_unreadable" if isinstance(exc, ParseFailedError)
                  else "da_guard_failed")
    if args.json_output:
        print(format_json_report({
            "status": "caller_error",
            "reason": reason,
            "tenants_evaluated": 0,
            "rules_evaluated": rules_evaluated,
            "error_count": 0,
            "warning_count": 0,
            "passed": False,
            "violations": [],
        }))
    return EXIT_CALLER_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 進入點。"""
    try_utf8_stdout()
    lang = detect_cli_lang()
    parser = build_parser(lang)
    args = parser.parse_args(argv)

    # A non-existent --config-dir is a caller error, not a vacuous pass:
    # without this guard a typo'd path silently yields no rules AND no tenant
    # configs, both of which return EXIT_OK below — so `policy-engine
    # --config-dir <typo> --ci && promote` would fail open. (Note: a dir that
    # EXISTS but has no _policies is a separate, legitimate no-op handled at
    # the `if not rules` path and left at EXIT_OK.)
    if not Path(args.config_dir).is_dir():
        msg = (f"設定目錄不存在: {args.config_dir}" if lang == "zh"
               else f"config directory not found: {args.config_dir}")
        print(msg, file=sys.stderr)
        if args.json_output:
            print(format_json_report({
                "status": "caller_error",
                "reason": "config_dir_not_found",
                "tenants_evaluated": 0,
                "rules_evaluated": 0,
                "error_count": 0,
                "warning_count": 0,
                "passed": False,
                "violations": [],
            }))
        return EXIT_CALLER_ERROR

    # #1651 (D-06 "fix the class, not the cell"): `--policy` SUPPLIED but not a
    # file is a caller error, never "no policy". Measured before the fix:
    # `--policy ''` returned 0 because `if args.policy:` routed '' into the
    # omitted branch, and `--policy <typo>` returned 0 because
    # `load_yaml_file` returns None for a non-file so `load_policies` gave [].
    # Both then printed "No policy rules found" — a typo'd gate that passes.
    # Omitted is `is None` (argparse default); '' is supplied.
    if args.policy is not None and not Path(args.policy).is_file():
        if lang == "zh":
            print(f"--policy 不是檔案: {args.policy!r}", file=sys.stderr)
            print("  ⛔ 不要靠拿掉 --policy 轉綠——那等於不帶你的策略檔評估。",
                  file=sys.stderr)
        else:
            print(f"--policy: {args.policy!r} is not a file", file=sys.stderr)
            print("  ⛔ Do not drop the flag to clear this — that evaluates "
                  "without your policy file.", file=sys.stderr)
        if args.json_output:
            print(format_json_report({
                "status": "caller_error",
                "reason": "policy_file_not_found",
                "tenants_evaluated": 0,
                "rules_evaluated": 0,
                "error_count": 0,
                "warning_count": 0,
                "passed": False,
                "violations": [],
            }))
        return EXIT_CALLER_ERROR

    # Load policies
    rules: list[PolicyRule] = []

    # From _defaults.yaml in config-dir
    # #2115 0-B: name only the nested `_defaults.yaml` files whose `_policies`
    # this lookup skips; tenants in subdirectories ARE evaluated (their values
    # come from the exporter's own load), so the generic FLAT/SKIPPED warning
    # would contradict the report.
    defaults_path = str(resolve_defaults_file(
        Path(args.config_dir), skipped_note=POLICIES_SKIPPED_NOTE))
    if Path(defaults_path).is_file():
        try:
            rules.extend(load_policies(defaults_path))
        except YamlFileError as exc:
            return _unreadable_yaml_exit(exc, args, lang)

    # From standalone policy file
    if args.policy is not None:   # ⛔ not truthiness: '' is supplied (#1651)
        # Blind-review follow-up to #1651: the guard above only proves the
        # path IS a file. Measured before this: a file that is not valid
        # UTF-8 or not valid YAML escaped `_lib_io.load_yaml_file` as a
        # traceback — rc 1 (EXIT_VIOLATION's number, for a caller mistake)
        # and 0 bytes of stdout under --json; a top-level list or scalar
        # fell through to "No policy rules found" rc 0 because
        # `"policies" in "just a string"` is a substring test. Same handler
        # ladder as `lint_custom_rules.load_policy`: each shape is a caller
        # error (2) naming the path and the reason, with the --json envelope
        # the not-found path emits.
        problem: Optional[tuple[str, str, str]] = None   # (reason, en, zh)
        try:
            with open(args.policy, encoding="utf-8") as f:
                # #2123 strict; #2114 `exclude_tenants` items as source
                # text, see `load_policies`. Same pure parser as before.
                policy_data = strict_load_exporter_keys(
                    f, raw_text_sequences=POLICY_TENANT_LISTS)
        except OSError as e:
            problem = ("policy_file_unreadable",
                       f"cannot read {args.policy!r}: {e}",
                       f"無法讀取 {args.policy!r}: {e}")
        except UnicodeDecodeError as e:
            # ⛔ NOT an OSError — it is a ValueError, so the handler above
            # does not see it.
            problem = ("policy_file_invalid",
                       f"{args.policy!r} is not valid UTF-8: {e}",
                       f"{args.policy!r} 不是有效的 UTF-8: {e}")
        except yaml.YAMLError as e:
            problem = ("policy_file_invalid",
                       f"{args.policy!r} is not valid YAML: {e}",
                       f"{args.policy!r} 不是有效的 YAML: {e}")
        else:
            # An EMPTY file parses to None and stays "no rules" (rc 0 below,
            # the #1649 content axis); any other non-mapping top level is
            # a file the engine cannot read rules from.
            if policy_data is not None and not isinstance(policy_data, dict):
                kind = type(policy_data).__name__
                problem = ("policy_file_invalid",
                           f"top level of {args.policy!r} is {kind}, "
                           "expected a mapping with a `policies:` list",
                           f"{args.policy!r} 的頂層是 {kind}，應為含 "
                           "`policies:` 清單的 mapping")
        if problem is not None:
            reason, en, zh = problem
            print(f"--policy: {zh}" if lang == "zh" else f"--policy: {en}",
                  file=sys.stderr)
            if lang == "zh":
                print("  ⛔ 不要靠拿掉 --policy 轉綠——那等於不帶你的策略檔評估。",
                      file=sys.stderr)
            else:
                print("  ⛔ Do not drop the flag to clear this — that evaluates "
                      "without your policy file.", file=sys.stderr)
            if args.json_output:
                print(format_json_report({
                    "status": "caller_error",
                    "reason": reason,
                    "tenants_evaluated": 0,
                    "rules_evaluated": 0,
                    "error_count": 0,
                    "warning_count": 0,
                    "passed": False,
                    "violations": [],
                }))
            return EXIT_CALLER_ERROR
        rules.extend(rules_from_policy_data(policy_data))

    # #1112: an early return is still a `--json` terminal path — emit the report
    # schema with everything zeroed (`passed: true`: nothing was evaluated, so
    # nothing failed) plus a status/reason discriminator, and send the prose to
    # stderr. A consumer reading `.error_count` / `.violations` works unchanged.
    def _empty_report(status: str, reason: str, rules_evaluated: int = 0) -> dict:
        return {
            "status": status,
            "reason": reason,
            "tenants_evaluated": 0,
            "rules_evaluated": rules_evaluated,
            "error_count": 0,
            "warning_count": 0,
            "passed": True,
            "violations": [],
        }

    if not rules:
        # #1651: when a policy file WAS supplied (it exists — the guard above
        # ran — but yielded no rules), "specify --policy" is the wrong advice:
        # the operator already did. Name both carriers that came up empty.
        # The content axis itself stays exit 0 (#1649, owner decision).
        carrier = Path(defaults_path).name
        if lang == "zh":
            if args.policy is None:
                print(f"未找到策略規則。在 {carrier} 新增 _policies 或指定 --policy。",
                      file=sys.stderr)
            else:
                print(f"未找到策略規則。{carrier} 沒有 _policies，"
                      f"且 --policy {args.policy!r} 沒有 policies: 清單。",
                      file=sys.stderr)
        else:
            # #1588 review: name the carrier this run actually resolved.
            # Telling an operator to edit `_defaults.yaml` on a tree whose
            # carrier is `_DEFAULTS.YAML` sends them to a file that is not
            # there.
            if args.policy is None:
                print(f"No policy rules found. Add _policies to "
                      f"{carrier} or specify --policy.",
                      file=sys.stderr)
            else:
                print(f"No policy rules found. {carrier} has no _policies "
                      f"and --policy {args.policy!r} has no policies: list.",
                      file=sys.stderr)
        if args.json_output:
            print(format_json_report(_empty_report("no_policies", "no_policy_rules_found")))
        return EXIT_OK

    # Load tenants as the exporter (and, for `_routing`, the route
    # generator) reads them — #2115 0-B; see the module docstring.
    try:
        inputs = load_policy_inputs(args.config_dir, routing=rules_read_routing(rules))
    except (DaGuardNotFoundError, DaGuardError, ParseFailedError, RoutingTreeRefused) as exc:
        return _load_error_exit(exc, args, len(rules))
    tenant_configs = inputs.views
    if not tenant_configs:
        if lang == "zh":
            print(f"未找到 tenant 配置於 {args.config_dir}", file=sys.stderr)
        else:
            print(f"No tenant configs found in {args.config_dir}", file=sys.stderr)
        if args.json_output:
            print(format_json_report(_empty_report(
                "no_tenant_configs", "no_tenant_configs_found",
                rules_evaluated=len(rules))))
        return EXIT_OK

    # Evaluate
    result = evaluate_policies(rules, tenant_configs, inputs.aliases)

    # Output
    if args.json_output:
        print(format_json_report(generate_json_report(result)))
    else:
        print(generate_text_report(result, lang))

    # Exit code
    if args.ci and not result.passed:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
