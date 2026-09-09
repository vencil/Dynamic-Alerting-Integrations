#!/usr/bin/env python3
"""deprecate_rule.py — 規則/指標下架工具。

步驟編號與本工具印出來的一致:
  Step 1: 從每個 `_defaults.yaml`／`_defaults.yml` 載體移除該 metric 的 key
          （`defaults:` 與 `optional_overrides:` 兩個平面）
  Step 2: 從平面目錄下非 `_` 前綴的租戶檔移除該 metric 的 key（含維度鍵）
  Step 3: 印出需要手動處理的 Prometheus ConfigMap 清理指引
  最後:   完成度判定（key 級重掃 + root 層 `_` 前綴檔的載體體檢），不通過 rc 1

下架＝刪 key，不是把值寫成 `disable`（#1787）: `disable` 是租戶 opt-out 的
語意，而 root `defaults:` 的型別是 `map[string]float64`，一個字串會讓
exporter 丟掉整份載體。

用法:
  # 預覽模式 (預設)
  python3 deprecate_rule.py mysql_slave_lag

  # 執行下架 (修改檔案)
  python3 deprecate_rule.py mysql_slave_lag --execute

  # 指定 conf.d 目錄
  python3 deprecate_rule.py mysql_slave_lag --config-dir /path/to/conf.d --execute

  # 同時處理多個 metric
  python3 deprecate_rule.py mysql_slave_lag mysql_innodb_buffer_pool --execute

注意:
  此工具處理 conf.d/ 層面的設定清理。Prometheus ConfigMap 中的
  Recording Rule / Alert Rule 需在下個 Release Cycle 手動移除。
"""

import sys
import os
import argparse
import math
import re
from pathlib import Path
import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import load_yaml_file as _lib_load_yaml  # noqa: E402
from _lib_python import write_text_or_die  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_confd import (  # noqa: E402  (#1588 shared name predicates)
    defaults_files_in,
    has_yaml_extension,
    is_defaults_name,
    iter_config_files,
    resolve_defaults_file,
    unusable_config_entries,
    unusable_reason,
    warn_nested,
)


def _read_yaml(path):
    """`(data, error)`: 讀不到／解析失敗／頂層不是 mapping 時 `error` 是一句話。"""
    try:
        data = _lib_load_yaml(path, default={})
    except (OSError, yaml.YAMLError) as e:
        return None, str(e).splitlines()[0] if str(e) else e.__class__.__name__
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"頂層不是 mapping（{type(data).__name__}）"
    return data, None


def load_yaml_file(path):
    """讀 YAML mapping；讀不到就回 None（呼叫端自己具名，這裡不印）。"""
    data, _err = _read_yaml(path)
    return data


def save_yaml_file(path, data, header_comment=""):
    """安全寫入 YAML 檔案。"""
    content = ""
    if header_comment:
        content += header_comment
    content += yaml.safe_dump(data, default_flow_style=False,
                              allow_unicode=True, sort_keys=False)
    # #1641: an existing conf.d file (derived, not an output flag) that cannot
    # be written is rc 2, not rc 1.
    write_text_or_die(path, content)


def metric_pattern_keys(metric_key):
    """一個 metric 在 `defaults:`／`optional_overrides:` 佔用的精確 key 集合。

    掃描、移除、重掃三邊共用這一份，所以掃得到的就是清得掉的。維度形狀
    `<名字>{…}` 只在租戶平面出現，由 `tenant_key_belongs_to_metric` 錨在這四個
    名字上。
    """
    return [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]


def tenant_key_belongs_to_metric(key, metric_key):
    """租戶平面上「這個 key 是不是這個 metric 的」: 四個確切名字或其維度形狀。

    不是子字串比對——`container_cpu_throttle{pod="x"}` 是另一個指標的資料。
    """
    for pk in metric_pattern_keys(metric_key):
        if key == pk or key.startswith(pk + "{"):
            return True
    return False


# ── 載體體檢：鏡射 yaml.v3 對 `map[string]float64` 的判定 ─────────────────
UNPARSEABLE = "unparseable"          # exporter 整份丟掉這個檔
DECODES_TO_ZERO = "decodes_to_zero"  # 這一個 key 變成 0 閾值
UNREADABLE = "unreadable"            # 本工具讀不到，無法判定
BLOCKING_KINDS = frozenset({UNPARSEABLE, UNREADABLE})

_T_STR, _T_INT, _T_FLOAT, _T_NULL, _T_BOOL, _T_TS, _T_BIN, _T_MERGE = (
    "!!str", "!!int", "!!float", "!!null", "!!bool", "!!timestamp", "!!binary",
    "!!merge")
_RESOLVABLE = frozenset({_T_STR, _T_BOOL, _T_INT, _T_FLOAT, _T_NULL, _T_TS})
_YAML_NS = "tag:yaml.org,2002:"
_RESOLVE_MAP = {}
for _tag, _words in (
        (_T_BOOL, ("true", "True", "TRUE", "false", "False", "FALSE")),
        (_T_NULL, ("", "~", "null", "Null", "NULL")),
        (_T_FLOAT, (".nan", ".NaN", ".NAN", ".inf", ".Inf", ".INF",
                    "+.inf", "+.Inf", "+.INF", "-.inf", "-.Inf", "-.INF")),
        (_T_MERGE, ("<<",))):
    for _w in _words:
        _RESOLVE_MAP[_w] = _tag
_GO_INT = re.compile(
    r"([+-]?)(?:0[xX]([0-9a-fA-F]+)|0[oO]([0-7]+)|0[bB]([01]+)|0([0-7]*)|([1-9][0-9]*))")
_YAML_STYLE_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
_GO_DEC_FLOAT = re.compile(r"[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]+)?")


class _Dropped(Exception):
    """整份檔會被 exporter 丟掉；訊息是理由。"""


def _short_tag(tag):
    if tag and tag.startswith(_YAML_NS):
        return "!!" + tag[len(_YAML_NS):]
    return tag


def _hint(text):
    c = text[:1] or "N"
    if c in "+-":
        return "S"
    if c in "0123456789":
        return "D"
    if c in "yYnNtTfFoO~":
        return "M"
    if c == ".":
        return "."
    return ""


def _go_parse_int(s, *, signed):
    m = _GO_INT.fullmatch(s)
    if not m:
        return None
    sign, hexd, octd, bind, legacy, dec = m.groups()
    if sign and not signed:
        return None
    if hexd is not None:
        v = int(hexd, 16)
    elif octd is not None:
        v = int(octd, 8)
    elif bind is not None:
        v = int(bind, 2)
    elif legacy is not None:
        v = int(legacy or "0", 8)
    else:
        v = int(dec)
    if sign == "-":
        v = -v
    lo, hi = (-(1 << 63), (1 << 63) - 1) if signed else (0, (1 << 64) - 1)
    return v if lo <= v <= hi else None


def _underscore_ok(s):
    saw = "^"
    if s[:1] in ("-", "+"):
        s = s[1:]
    i, hexd = 0, False
    if len(s) >= 2 and s[0] == "0" and s[1].lower() in "box":
        i, saw, hexd = 2, "0", s[1].lower() == "x"
    for ch in s[i:]:
        if ch.isdigit() and ch.isascii() or hexd and ch.lower() in "abcdef":
            saw = "0"
        elif ch == "_":
            if saw != "0":
                return False
            saw = "_"
        elif saw == "_":
            return False
        else:
            saw = "!"
    return saw != "_"


def _go_parse_float(s):
    if "_" in s and not _underscore_ok(s):
        return None
    plain = s.replace("_", "")
    if not _GO_DEC_FLOAT.fullmatch(plain):
        return None
    v = float(plain)
    return None if math.isinf(v) else v      # strconv: ErrRange


def _resolve(tag, text):
    """resolve.go `resolve()` 回的 tag（`tag` 是顯式短 tag 或 None）。"""
    if tag and tag not in _RESOLVABLE:
        return tag
    hint = _hint(text)
    if hint and tag not in (_T_STR, _T_BIN):
        if text in _RESOLVE_MAP:
            return _RESOLVE_MAP[text]
        if hint == ".":
            if _go_parse_float(text) is not None:
                return _T_FLOAT
        elif hint in "DS":
            plain = text.replace("_", "")
            if _go_parse_int(plain, signed=True) is not None:
                return _T_INT
            if _go_parse_int(plain, signed=False) is not None:
                return _T_INT
            if (_YAML_STYLE_FLOAT.fullmatch(plain)
                    and _go_parse_float(plain) is not None):
                return _T_FLOAT
    return _T_STR


def _source_of(node):
    m0, m1 = node.start_mark, node.end_mark
    if m0.buffer is None:
        return ""
    return m0.buffer[m0.pointer:m1.pointer]


def _explicit_tag(node):
    """顯式 tag 的短形式，沒有（或只有非特定的 `!`）就 None。"""
    src = _source_of(node)
    if src.startswith("&"):
        parts = src.split(None, 1)
        src = parts[1] if len(parts) > 1 else ""
    if not src.startswith("!"):
        return None
    if src.split(None, 1)[0] == "!":
        return None
    return _short_tag(node.tag)


def _scalar_verdict(node):
    """decode.go `scalar()` 對 float64 目標的下場: accepted / zero / dropped。"""
    if not isinstance(node, yaml.ScalarNode):
        return "dropped"
    tag = _explicit_tag(node)
    if tag == _T_STR or (tag is None and node.style in ('"', "'", "|", ">")):
        return "dropped"                       # indicatedString
    rtag = _resolve(tag, node.value)
    if tag is None or tag == rtag or tag == _T_BIN:
        final = rtag
    elif tag == _T_FLOAT and rtag == _T_INT:
        final = _T_FLOAT
    else:
        return "dropped"                       # "cannot decode … as a …"
    if final in (_T_INT, _T_FLOAT):
        return "accepted"
    if final == _T_NULL:
        return "zero"
    return "dropped"


def _raw_of(node):
    if isinstance(node, yaml.MappingNode):
        return "<mapping>"
    if isinstance(node, yaml.SequenceNode):
        return "<list>"
    lines = _source_of(node).splitlines() or ["(空)"]
    return lines[0] + ("…" if len(lines) > 1 else "")


def _key_identity(node):
    return (type(node).__name__,
            node.value if isinstance(node, yaml.ScalarNode) else "")


def _no_duplicate_keys(node):
    seen = set()
    for k, _v in node.value:
        ident = _key_identity(k)
        if ident in seen:
            raise _Dropped(f"重複 key `{ident[1]}`")
        seen.add(ident)


def _is_merge_key(node):
    return (isinstance(node, yaml.ScalarNode) and node.value == "<<"
            and _short_tag(node.tag) == _T_MERGE)


def _effective_entries(node, seen, visiting):
    """decode.go `mapping()`+`merge()` 會解碼的 `(key, value)`，顯式在前、merge 在後。"""
    if id(node) in visiting:
        raise _Dropped("anchor 自我參照")
    visiting = visiting | {id(node)}
    _no_duplicate_keys(node)
    merge_node = None
    out = []
    for k, v in node.value:
        if _is_merge_key(k):
            merge_node = v
            continue
        if not isinstance(k, yaml.ScalarNode):
            raise _Dropped("key 不是純量")
        if seen is not None:
            if k.value in seen:
                continue
            seen.add(k.value)
        out.append((k, v))
    if merge_node is not None:
        if seen is None:
            seen = {k.value for k, _ in node.value if isinstance(k, yaml.ScalarNode)}
        sources = (merge_node.value if isinstance(merge_node, yaml.SequenceNode)
                   else [merge_node])
        for src in sources:
            if not isinstance(src, yaml.MappingNode):
                raise _Dropped("`<<` 的值不是 mapping")
            out.extend(_effective_entries(src, seen, visiting))
    return out


def carrier_health(data):
    """root 層載體的位元組 exporter 讀不讀得進去: `[(key, 原文, kind), ...]`。

    輸入是檔案的 bytes。判定鏡射 yaml.v3 v3.0.1 的 `decode.go`（`scalar()`／
    `mapping()`／`merge()`／`indicatedString()`）與 `resolve.go`（`resolve()`）對
    `ThresholdConfig`（`defaults: map[string]float64`）的行為；真值表在
    `tests/golden/fixtures/defaults-carrier-oracle.json`，Go 測試
    `TestDefaultsCarrierOracle` 是那張表的裁判。文件層級的項目 key 為 None。

    NOT GUARDED（不看的東西）:
      * `optional_overrides`／`state_filters`／`tenants`／`profiles` 的值形狀
        （只看 Go 會解碼的那幾層有沒有重複 key）；`max_metrics_per_tenant` 的值。
      * 頂層的 merge key、非純量 key、非特定 tag `!`、UTF-16 等向量表以外的拼法。
      * exporter alias 表（`pkg/config/aliases.go`）的 legacy 拼法；Go 側後續票
        處理（追蹤入口: #1787）。
    """
    try:
        root = next(yaml.compose_all(data), None)
    except yaml.YAMLError as e:
        first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        return [(None, first, UNPARSEABLE)]
    if root is None:
        return []
    if not isinstance(root, yaml.MappingNode):
        return [(None, "頂層不是 mapping", UNPARSEABLE)]
    try:
        _no_duplicate_keys(root)
        top = {k.value: v for k, v in root.value if isinstance(k, yaml.ScalarNode)}
        for section in ("tenants", "profiles"):
            block = top.get(section)
            if isinstance(block, yaml.MappingNode):
                _no_duplicate_keys(block)
                for _name, inner in block.value:
                    if isinstance(inner, yaml.MappingNode):
                        _no_duplicate_keys(inner)
        block = top.get("state_filters")
        if isinstance(block, yaml.MappingNode):
            _no_duplicate_keys(block)
        defaults = top.get("defaults")
        if defaults is None:
            return []
        if isinstance(defaults, yaml.ScalarNode):
            if _scalar_verdict(defaults) == "zero":
                return []                      # `defaults:` 空值＝nil map
            raise _Dropped("defaults 不是 mapping（scalar）")
        if not isinstance(defaults, yaml.MappingNode):
            raise _Dropped("defaults 不是 mapping（list）")
        out = []
        for key_node, val_node in _effective_entries(defaults, None, frozenset()):
            verdict = _scalar_verdict(val_node)
            if verdict == "accepted":
                continue
            out.append((key_node.value, _raw_of(val_node),
                        DECODES_TO_ZERO if verdict == "zero" else UNPARSEABLE))
        return out
    except _Dropped as e:
        return [(None, str(e), UNPARSEABLE)]


def carrier_health_at(path):
    """`carrier_health` 讀檔版；讀不到回一個 `UNREADABLE` 項（key None）。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return [(None, e.strerror or e.__class__.__name__, UNREADABLE)]
    return carrier_health(data)


# ── 掃描與完成度 ──────────────────────────────────────────────────────────
OWNER_TOOL = "tool"          # Step 1／2 會清
OWNER_MANUAL = "manual"      # 本工具射程外，殘留要人去改（兩種模式都 rc 1）
OWNER_EXPORTER = "exporter"  # exporter 自己會丟棄這個區塊，不算殘留


def section_owner(name, section):
    """掃描結果的 `[section]` 在檔 `name` 裡由誰清: 本工具／手動／exporter 丟棄。

    規則來自 exporter 的 `applyBoundaryRules`（`flat_scanner.go`）: 平台區塊
    （`defaults`／`optional_overrides`／`profiles`）只從 `_` 前綴檔讀；本工具只寫
    精確名載體的平台區塊與非 `_` 檔的 `tenants:`，而且不遞迴寫入子目錄。
    """
    if "/" in name or section == "unreadable":
        return OWNER_MANUAL
    block = section.split(".", 1)[0]
    if block in ("defaults", "optional_overrides"):
        if is_defaults_name(name):
            return OWNER_TOOL
        return OWNER_MANUAL if name.startswith("_") else OWNER_EXPORTER
    if block == "profiles":
        return OWNER_MANUAL if name.startswith("_") else OWNER_EXPORTER
    return OWNER_MANUAL if name.startswith("_") else OWNER_TOOL


def manual_reason(name, section, key, val, config_dir):
    """`OWNER_MANUAL` 殘留的那一句話（預覽與執行同一句）。"""
    where = f"[{section}] {key}: {val}"
    if section == "unreadable":
        return f"無法讀取（{val}），本工具無法判定"
    if "/" in name:
        subtree = os.path.join(str(config_dir), *name.split("/")[:-1])
        return (f"子目錄裡的檔，本工具不遞迴寫入；請對該子樹跑 "
                f"`--config-dir {subtree} --plane subtree`：{where}")
    block = section.split(".", 1)[0]
    if block in ("defaults", "optional_overrides"):
        return (f"非 defaults 載體，本工具不寫入；exporter 會把 root 層任何 `_` "
                f"開頭檔的 defaults／optional_overrides 併進全域，需手動處理：{where}")
    if block == "profiles":
        return f"`_` 前綴檔的 `profiles:` 區塊本工具射程外，請手動移除：{where}"
    return (f"本工具依設計不寫任何 `_` 前綴檔的 `tenants:` 區塊，請手動移除："
            f"{where}")


def unclearable_occurrences(metric_key, findings, *, predict):
    """掃描結果中本工具的處置**不會**（`predict=True`）或**沒有**清掉的引用。

    回傳 `[(filename, section, key, value, designed), ...]`，是 key 級完成度的
    唯一判定點。`designed=True` 是 `section_owner` 判給手動的那一類，兩種模式
    都列；`OWNER_TOOL` 的引用預覽時扣掉（本輪會刪），`--execute` 後重掃仍在就
    列出——那才是「寫入沒有發生」。exporter 會丟棄的區塊不算殘留。
    """
    out = []
    for f in findings:
        name = f["filename"]
        for section, key, val in f["occurrences"]:
            owner = section_owner(name, section)
            if owner == OWNER_EXPORTER:
                continue
            if owner == OWNER_TOOL:
                if predict:
                    continue
                designed = False
            else:
                designed = True
            out.append((name, section, key, val, designed))
    return out


def _occurrences_in(data, metric_key):
    """一份已解析的 mapping 裡該 metric 的引用 `[(section, key, value), ...]`。"""
    out = []
    pattern_keys = metric_pattern_keys(metric_key)
    defaults = data.get("defaults") or {}
    if isinstance(defaults, dict):
        out += [("defaults", pk, defaults[pk]) for pk in pattern_keys
                if pk in defaults]
    declared = data.get("optional_overrides") or []
    if isinstance(declared, list):
        out += [("optional_overrides", pk, "（宣告，無值）")
                for pk in pattern_keys if pk in declared]
    for section in ("tenants", "profiles"):
        block = data.get(section) or {}
        if not isinstance(block, dict):
            continue
        for owner_name, cfg in block.items():
            if not isinstance(cfg, dict):
                continue
            out += [(f"{section}.{owner_name}", key, val)
                    for key, val in cfg.items()
                    if tenant_key_belongs_to_metric(key, metric_key)]
    return out


def scan_for_metric(metric_key, config_dir):
    """平面掃描 conf.d 這一層引用該 metric 的檔。

    回傳 `[{filename, path, occurrences}, ...]`；讀不了的檔以 `("unreadable",
    "", 原因)` 列入，不靜默略過。#1607 的 `is_file()` 過濾在這裡，具名在 `main`
    （本函式每個 metric 跑一次）。
    """
    findings = []
    config_base = Path(config_dir)
    warn_nested(config_base, tool="deprecate_rule")
    entries = sorted(config_base.iterdir())
    for entry in (
        p for p in entries
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if filename.startswith('.'):
            continue
        data, err = _read_yaml(path)
        if err is not None:
            occurrences = [("unreadable", "", err)]
        else:
            occurrences = _occurrences_in(data, metric_key)
        if occurrences:
            findings.append({
                "filename": filename,
                "path": path,
                "occurrences": occurrences,
            })
    return findings


def nested_residue(metric_key, config_dir):
    """子目錄裡（平面那一層以下）仍引用該 metric 的檔——只讀，供完成度判定。

    形狀同 `scan_for_metric`，`filename` 是相對 POSIX 路徑（含 `/`，所以
    `section_owner` 判給手動）。
    """
    base = Path(config_dir)
    out = []
    for p in iter_config_files(base, recursive=True):
        if p.parent == base:
            continue
        data, err = _read_yaml(str(p))
        occurrences = ([("unreadable", "", err)] if err is not None
                       else _occurrences_in(data, metric_key))
        if occurrences:
            out.append({"filename": p.relative_to(base).as_posix(),
                        "path": str(p), "occurrences": occurrences})
    return out


def out_of_reach(entries):
    """這一層本工具不寫的載體 `[(名字, 理由), ...]`: 子目錄、`_` 前綴檔的
    `tenants:`／`profiles:` 區塊（含 `_defaults.yaml` 自己的）。"""
    bad = {b.name for b in unusable_config_entries(entries)}
    out = []
    for e in entries:
        if e.name in bad or e.name.startswith("."):
            continue
        if e.is_dir():
            out.append((f"{e.name}/", "子目錄，本工具不遞迴寫入；殘留請對該子樹跑 "
                                      "`--config-dir <子樹> --plane subtree`"))
        elif (e.is_file() and has_yaml_extension(e.name)
              and e.name.startswith("_")):
            data, err = _read_yaml(str(e))
            if err is not None:
                continue                       # 載體體檢會具名
            for section in ("tenants", "profiles"):
                if data.get(section):
                    out.append((e.name, f"`_` 前綴檔的 `{section}:` 區塊，"
                                        f"本工具不寫，殘留需手動移除"))
    return out


def defaults_carriers(config_dir):
    """conf.d 這一層所有 defaults 載體（`_defaults.yaml`／`.yml`，任意大小寫）。

    exporter 兩種拼法都併進 defaults chain（`config_hierarchy.go:216`），所以
    寫入端要列舉掃描看得見的同一組。平面讀取，`warn_nested` 具名子目錄。
    """
    base = Path(config_dir)
    warn_nested(base, tool="deprecate_rule")
    entries = sorted(base.iterdir())
    return defaults_files_in(base, [e.name for e in entries if e.is_file()])


def remove_from_all_defaults(metric_key, config_dir, execute=False):
    """對每個 defaults 載體執行 `_remove_in_carrier`。

    回傳每個載體一筆 `(path, ok, msg, removed)`。沒有任何載體時回
    `resolve_defaults_file` 的正典路徑那一筆（`_defaults.yaml 不存在`）。
    平面讀取: 子樹載體要用 `--config-dir` 指到那一層。
    """
    base = Path(config_dir)
    carriers = defaults_carriers(base)
    if not carriers:
        carriers = [resolve_defaults_file(base)]
    return [(p, *_remove_in_carrier(metric_key, p, execute)) for p in carriers]


def _remove_in_carrier(metric_key, defaults_path, execute=False):
    """從一個 defaults 載體移除 metric 的所有相關 key。

    回傳 `(ok, msg, removed)`，`removed` 為 `[(平面, key, 原值), ...]`，平面是
    `"defaults"` 或 `"optional_overrides"`。要刪的 key 來自 `metric_pattern_keys`
    ——掃描用的同一份。沒有相關 key 的載體是具名 no-op，一個位元組都不寫。
    """
    defaults_path = str(defaults_path)
    name = Path(defaults_path).name
    if not Path(defaults_path).exists():
        return False, f"{name} 不存在", []

    data = load_yaml_file(defaults_path)
    if data is None:
        return False, f"無法讀取 {name}", []

    # `defaults:` 空值是空區塊；list／scalar 是壞檔，不改寫、具名拒絕。
    defaults = data.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        return False, (f"defaults 不是 mapping（{type(defaults).__name__}），"
                       f"本工具不改寫，請先修檔"), []

    pattern_keys = metric_pattern_keys(metric_key)
    declared = data.get("optional_overrides")
    declared_hit = ([pk for pk in pattern_keys if pk in declared]
                    if isinstance(declared, list) else [])

    removed = [("defaults", pk, defaults[pk]) for pk in pattern_keys
               if pk in defaults]
    removed += [("optional_overrides", pk, "（宣告，無值）")
                for pk in declared_hit]

    if not removed:
        return True, f"{name} 沒有 {metric_key} 相關 key，略過", []

    if not execute:
        return True, f"將從 {name} 移除 {len(removed)} 個 key", removed

    # 只碰本輪刪過東西的平面；清空的平面整個拿掉（schema 不要求 `defaults:`
    # 存在，`init_project` 對空 list 的立場也是不寫）。
    if any(where == "defaults" for where, _, _ in removed):
        for where, pk, _val in removed:
            if where == "defaults":
                del defaults[pk]
        if defaults:
            data["defaults"] = defaults
        else:
            data.pop("defaults", None)
    if declared_hit:
        kept = [k for k in declared if k not in declared_hit]
        if kept:
            data["optional_overrides"] = kept
        else:
            data.pop("optional_overrides", None)

    save_yaml_file(defaults_path, data, _header_of(defaults_path))
    return True, f"已從 {name} 移除 {len(removed)} 個 key", removed


def _header_of(path):
    """檔頭連續的 `#` 註解行（寫回時唯一保留的註解）。"""
    header = ""
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('#'):
                header += line
            else:
                break
    return header


def remove_from_tenants(metric_key, config_dir, execute=False):
    """從平面目錄下非 `_` 前綴的租戶檔移除該 metric 的 key。"""
    removed = []
    config_base = Path(config_dir)
    warn_nested(config_base, tool="deprecate_rule")
    for entry in sorted(
        p for p in config_base.iterdir()
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if filename.startswith('_') or filename.startswith('.'):
            continue

        data = load_yaml_file(path)     # 讀不了的檔 `scan_for_metric` 已具名
        if data is None:
            continue

        tenants = data.get("tenants") or {}
        if not isinstance(tenants, dict):
            continue
        modified = False
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            keys_to_remove = [key for key in tenant_config
                              if tenant_key_belongs_to_metric(key, metric_key)]
            for key in keys_to_remove:
                val = tenant_config[key]
                removed.append((filename, tenant_name, key, val))
                if execute:
                    del tenant_config[key]
                    modified = True

        if modified and execute:
            save_yaml_file(path, data, _header_of(path))

    return removed


def _health_reason(key, raw, kind):
    if kind == UNREADABLE:
        return f"無法讀取（{raw}），本工具無法判定 exporter 讀不讀得進去"
    if key is None:
        return f"exporter 讀不進這份檔（{raw}），整份載體會被丟棄；請先修檔"
    if kind == DECODES_TO_ZERO:
        return (f"`defaults:` 的 {key} 是空值（`{key}:`）——exporter 會把它解成 0，"
                f"每個租戶因此多一條 0 閾值（等於永遠觸發），不是「沒有這個 key」；"
                f"請刪掉這一行或補一個數字")
    return (f"`defaults:` 的 {key} 的值 {raw} exporter 讀不成數字（defaults 的型別是 "
            f"map[string]float64），這份載體會被整份丟棄，下架不會生效；請先修掉它")


def main():
    """CLI entry point: 規則/指標下架工具。."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="規則/指標下架工具 — 三步安全淘汰 metric key"
    )
    parser.add_argument("metrics", nargs="+",
                        help="要下架的 metric key (例如 mysql_slave_lag)")
    parser.add_argument("--config-dir",
                        default="components/threshold-exporter/config/conf.d",
                        help="conf.d 目錄路徑")
    parser.add_argument("--execute", action="store_true",
                        help="實際執行下架 (預設只預覽)")
    parser.add_argument("--plane", choices=("root", "subtree"), default="root",
                        help="這個 --config-dir 是 conf.d 的 root（預設）還是"
                             "一層子樹載體。root 會對這一層 `_` 前綴檔做載體"
                             "體檢；subtree 跳過（子樹平面的字串值合法）")

    args = parser.parse_args()

    if not Path(args.config_dir).is_dir():
        print(f"ERROR: config-dir not found: {args.config_dir}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    mode = "執行" if args.execute else "預覽"

    print(f"{'='*60}")
    print(f"🗑️  規則下架工具 — {mode}模式")
    print(f"{'='*60}\n")
    print(f"目標 Metrics: {', '.join(args.metrics)}")
    print(f"Config 目錄: {args.config_dir}\n")

    # 目錄的性質印一次、印在 per-metric 迴圈之前（#1607）。`warn_nested` 與這次
    # 平面列舉同一個 scope；列舉失敗就讓它失敗，`scan_for_metric` 會用同一個目錄。
    base = Path(args.config_dir)
    warn_nested(base, tool="deprecate_rule")
    entries = sorted(base.iterdir())
    for bad in unusable_config_entries(entries):
        print(f"  ⚠️  略過 {safe_label(bad.name)}——{unusable_reason(bad)}",
              file=sys.stderr)
    unreachable = out_of_reach(entries)
    if unreachable:
        print("\n  ℹ️  本工具射程外（若仍持有該 metric 的 key，結尾會具名、rc 1）：")
        for name, reason in unreachable:
            print(f"     • {safe_label(name)}——{safe_label(reason)}")

    # 載體體檢跑在任何寫入之前，母體是這一層所有 `_` 前綴檔（exporter 把它們的
    # 平台區塊全部併進全域）。本輪自己要刪的 key 不算殘留。紅則整輪降級為預覽。
    planned = {k for m in args.metrics for k in metric_pattern_keys(m)}
    health = {}
    blocked = set()
    if args.plane == "root":
        for e in entries:
            if (not e.is_file() or not has_yaml_extension(e.name)
                    or e.name.startswith(".")):
                continue
            items = carrier_health_at(e)
            if e.name.startswith("_"):
                if is_defaults_name(e.name):
                    items = [i for i in items if i[0] not in planned]
                if items:
                    health[e.name] = items
                    if any(kind in BLOCKING_KINDS for _, _, kind in items):
                        blocked.add(e.name)
            else:
                for key, raw, kind in items:
                    if key is not None and kind == UNPARSEABLE:
                        print(f"  ⚠️  {safe_label(e.name)} 的 `defaults:` 有 exporter "
                              f"讀不成數字的值（{safe_label(key)}: {safe_label(raw)}）"
                              f"——exporter 會整份丟掉這個租戶檔；本工具不寫它")
    elif defaults_carriers(base):
        print("  ℹ️  子樹載體不做型別體檢：子樹平面的字串值合法"
              "（`computeEffectiveConfig` 走 map[string]any），"
              "root 的 map[string]float64 規則不適用於這一層。")

    write = args.execute and not blocked
    if args.execute and blocked:
        print(f"\n  ⛔ 本輪降級為預覽：{safe_label('、'.join(sorted(blocked)))} "
              f"exporter 讀不進去，一個位元組都不寫；請先修掉結尾具名的殘留再重跑")
    action = "已移除" if write else ("將移除（本輪未寫入）" if args.execute
                                   else "將移除")

    incomplete = []
    findings_by_metric = {}

    for metric in args.metrics:
        print(f"\n{'─'*40}")
        print(f"📌 Processing: {metric}")
        print(f"{'─'*40}\n")

        # 掃描（印出的 Step 1 之前）
        findings = scan_for_metric(metric, args.config_dir)
        findings_by_metric[metric] = findings
        if findings:
            print(f"  📂 發現 {sum(len(f['occurrences']) for f in findings)} 處引用:")
            for f in findings:
                for section, key, val in f["occurrences"]:
                    if section == "unreadable":
                        print(f"     • {safe_label(f['filename'])} → 無法讀取："
                              f"{safe_label(val)}")
                    else:
                        print(f"     • {safe_label(f['filename'])} → "
                              f"[{safe_label(section)}] {safe_label(key)}: "
                              f"{safe_label(val)}")
        else:
            print(f"  ✅ 未發現任何引用")
        dropped_blocks = set()
        for f in findings:
            for section, _key, _val in f["occurrences"]:
                block = section.split(".", 1)[0]
                if (section_owner(f["filename"], section) == OWNER_EXPORTER
                        and (f["filename"], block) not in dropped_blocks):
                    dropped_blocks.add((f["filename"], block))
                    print(f"  ⚠️  {safe_label(f['filename'])} 的 {block} 區塊 "
                          f"exporter 會丟棄（只從 `_` 前綴檔讀），本工具不寫入")

        # Step 1 (as printed): 從每個 defaults 載體移除相關 key
        results = remove_from_all_defaults(metric, args.config_dir, execute=write)
        for carrier, ok, msg, removed in results:
            print(f"\n  Step 1: {safe_label(carrier.name)}")
            icon = "✅" if ok else "❌"
            print(f"  {icon} {safe_label(msg)}")
            for where, key, val in removed:
                plane = "" if where == "defaults" else f"{where} 的 "
                print(f"     🗑️  {action} {plane}{safe_label(key)}"
                      f"（原值: {safe_label(val)}）")
            if not ok:
                incomplete.append((metric, carrier.name, msg))

        # Step 2 (as printed): 從 tenant configs 移除
        print(f"\n  Step 2: Tenant configs")
        removed = remove_from_tenants(metric, args.config_dir, execute=write)
        if removed:
            for filename, tenant, key, val in removed:
                print(f"  🗑️  {action}: {safe_label(filename)} → "
                      f"{safe_label(tenant)}.{safe_label(key)} (值: {safe_label(val)})")
        else:
            print(f"  ✅ 無需清理 tenant configs")

        # Step 3 (as printed): ConfigMap 指引
        print(f"\n  Step 3: Prometheus ConfigMap (手動)")
        print(f"  📋 下一個 Release Cycle 請手動移除:")
        print(f"     • Recording Rule: tenant:{metric}:* 或 tenant:custom_{metric}:*")
        print(f"     • Alert Rule: 引用上述 Recording Rule 的 Alert")
        print(f"     • Threshold Rule: tenant:alert_threshold:{metric}")
        print(f"     • 若此 metric 在 exporter alias 表（pkg/config/aliases.go）有 "
              f"legacy 名字，租戶檔請一併檢查")

    # 完成度是 KEY 級: 重掃（或預覽時用掃描結果）扣掉本工具會清的，剩下的具名。
    for metric in args.metrics:
        findings = (scan_for_metric(metric, args.config_dir) if write
                    else findings_by_metric[metric])
        findings = findings + nested_residue(metric, args.config_dir)
        for name, section, key, val, designed in unclearable_occurrences(
                metric, findings, predict=not write):
            if designed:
                reason = manual_reason(name, section, key, val, args.config_dir)
            else:
                reason = f"重掃仍有引用：[{section}] {key}: {val}"
            incomplete.append((metric, name, reason))

    for carrier_name, items in health.items():
        for key, raw, kind in items:
            reason = _health_reason(key, raw, kind)
            reason += ("。若這個 --config-dir 其實是子樹載體（exporter 的 "
                       "-config-dir 之下的目錄），請加 `--plane subtree`")
            incomplete.append(("載體體檢", carrier_name, reason))

    # 總結
    print(f"\n{'='*60}")
    if incomplete:
        print("❌ 下架未完成：")
        for metric, carrier, reason in incomplete:
            print(f"   • {safe_label(metric)} → {safe_label(carrier)}："
                  f"{safe_label(reason)}")
        print(f"{'='*60}")
        sys.exit(EXIT_VIOLATION)
    if args.execute:
        print("✅ 下架完成！threshold-exporter 將在下次 reload 時生效。")
        print("📋 請在下個 Release Cycle 清理 Prometheus ConfigMap 中的對應規則。")
    else:
        print("💡 這是預覽模式。要實際執行，請加 --execute 參數。")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
