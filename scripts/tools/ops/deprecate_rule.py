#!/usr/bin/env python3
"""deprecate_rule.py — 規則/指標下架工具。

步驟編號與本工具印出來的一致:
  Step 1: 從每個 `_defaults.yaml`／`_defaults.yml` 載體移除該 metric 的 key
          （`defaults:` 與 `optional_overrides:` 兩個平面）
  Step 2: 從平面目錄下非 `_` 前綴的租戶檔移除該 metric 的 key（含維度鍵）
  Step 3: 印出需要手動處理的 Prometheus ConfigMap 清理指引
  最後:   完成度判定（key 級重掃 + root 層 `_` 前綴檔的載體體檢），不通過 rc 1

寫入之前（預覽與 --execute 一樣）先問 exporter 一次（#1822）: `da-guard
key-refs` 以 exporter 自己的程式碼回答兩件事——這一層哪些檔 exporter 的 load
會丟掉（`parse_failed`／`unreadable`），以及哪些 key 經 exporter 的正規化屬於
要下架的 metric（alias 表的 legacy 拼法、`_critical`、維度鍵的各種拼法；與
canonical key 一樣列為引用、一樣移除）。root 層 `_` 前綴檔的判定看「本輪寫入
之後」：把本輪的寫入套在 conf.d 的暫存複本上再問一次（本輪要刪的 key 可能正是
讓載體被丟的那一個），仍被丟就擋寫入、rc 1；讀不到的檔看現況。
找不到 da-guard（`$DA_GUARD_BINARY` → `$PATH`）或它答不出來時印「未體檢」與
原因，行為與 rc 照舊：體檢是附加的，缺了它本工具的判定與 #1822 之前相同，
不是前置條件的缺失（不回 rc 2），也不是對這棵樹的發現（不回 rc 1）。
da-guard 正常回答「exporter 拒絕整棵樹」（例如同一租戶宣告兩次）則擋寫入、rc 1。

NOT GUARDED（da-guard 也沒回答的）:
  * 平面判定仍靠 `--plane`: da-guard 把 --config-dir 當 root 判，所以
    `--plane subtree` 不採用它對 `_` 前綴檔的判定（租戶檔與拼法照用）。
  * 本工具自己的載體鏡射（`exporter_verdicts`）擋下、exporter 其實收的檔仍擋
    （見該函式的 docstring）；體檢只補「鏡射說綠、exporter 整份丟」那一側。

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
import codecs
import contextlib
import io
import math
import re
import shutil
import tempfile
from pathlib import Path
import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
# #2216: this tool WRITES the files it reads, so a tenant id read with
# PyYAML's typing went back to disk renamed (`010:` → `8:`, `yes:` → `true:`).
# Keys are read as the exporter reads them — the scalar's source text (#2114,
# `_lib_yaml_keys`) — and `safe_dump` quotes any text YAML would retype, so
# the id written is the id read. Not strict, as the read was before (#2123).
# `_profile` is the one VALUE that names such a key (a profile name): kept as
# source text too, or `_profile: 010` is written as `8` next to a profile
# still named `'010'`. Every other value keeps its PyYAML type.
# #2220: the two steps that WRITE a file back (Step 1 carriers, Step 2 tenant
# files) read through `load_yaml_file_for_rewrite` instead, where every plain
# value is its source text, and dump with `dump_for_rewrite` — so a value the
# step did not delete is written as it was (`010` stays `010`, not `8`).
from _lib_python import load_yaml_file_exporter_keys  # noqa: E402
from _lib_io import load_yaml_file_for_rewrite  # noqa: E402
from _lib_yaml_keys import RawPlain, dump_for_rewrite  # noqa: E402
from _lib_python import write_text_or_die  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_io import duplicate_in_mapping  # noqa: E402  (#2123 shared check)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_tenant_values import (  # noqa: E402  (#1822 the exporter's own answer)
    DA_GUARD_PREFIX,
    DaGuardError,
    DaGuardNotFoundError,
    load_key_refs,
)
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


def _read_yaml(path, *, for_rewrite=False):
    """`(data, error)`: 讀不到／解析失敗／頂層不是 mapping 時 `error` 是一句話。

    `for_rewrite=True`（#2220）: 未加引號的純量讀成原文（`RawPlain`），給會
    寫回檔案的 Step 1／Step 2 用；其餘讀取維持 PyYAML 型別。
    """
    try:
        if for_rewrite:
            data = load_yaml_file_for_rewrite(path, default={})
        else:
            data = load_yaml_file_exporter_keys(path, default={},
                                                raw_text_scalars=("_profile",))
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


def _load_for_rewrite(path):
    """`load_yaml_file`，但未加引號的純量保留原文（#2220）；寫回路徑專用。"""
    data, _err = _read_yaml(path, for_rewrite=True)
    return data


def _type_name(value):
    """`type(value).__name__`；`RawPlain` 報 PyYAML 讀它時的型別，訊息與 #2220 前一致。"""
    if isinstance(value, RawPlain):
        try:
            value = yaml.safe_load(str(value))
        except yaml.YAMLError:
            return "str"
    return type(value).__name__


def save_yaml_file(path, data, header_comment=""):
    """安全寫入 YAML 檔案。"""
    content = ""
    if header_comment:
        content += header_comment
    # #2220: `RawPlain` values go back plain, as written; everything else is
    # dumped exactly as `yaml.safe_dump` would.
    content += dump_for_rewrite(data, default_flow_style=False,
                                allow_unicode=True, sort_keys=False)
    # #1641: an existing conf.d file (derived, not an output flag) that cannot
    # be written is rc 2, not rc 1.
    write_text_or_die(path, content)


def metric_pattern_keys(metric_key, spellings=()):
    """一個 metric 在 `defaults:`／`optional_overrides:` 佔用的精確 key 集合。

    掃描、移除、重掃三邊共用這一份，所以掃得到的就是清得掉的。維度形狀
    `<名字>{…}` 只在租戶平面出現，由 `tenant_key_belongs_to_metric` 錨在這四個
    名字上。`spellings` 是 exporter 判給這個 metric 的其他寫法（`da-guard
    key-refs` 的答案，#1822：alias 表的 legacy 拼法等），原文照收、接在後面。
    """
    base = [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]
    return base + [k for k in sorted(spellings) if k not in base]


def tenant_key_belongs_to_metric(key, metric_key, spellings=()):
    """租戶平面上「這個 key 是不是這個 metric 的」: 四個確切名字或其維度形狀，
    或 exporter 判給它的寫法（`spellings`，#1822）。

    不是子字串比對——`container_cpu_throttle{pod="x"}` 是另一個指標的資料。
    """
    if key in spellings:
        return True
    for pk in metric_pattern_keys(metric_key):
        if key == pk or key.startswith(pk + "{"):
            return True
    return False


# ── 載體體檢：鏡射 yaml.v3 對 `map[string]float64` 的判定 ─────────────────
UNPARSEABLE = "unparseable"            # exporter 整份丟掉這個檔
NULL_NOT_DECLARED = "null_not_declared"  # 這一個 key 寫成 null：exporter 當作沒宣告（只警告，#2518）
UNREADABLE = "unreadable"              # 本工具讀不到，無法判定
UNPARSED_BY_TOOL = "unparsed_by_tool"  # pure-Python parser 讀不了；exporter 未必
EXPORTER_DROPPED = "exporter_dropped"  # da-guard：exporter 自己的 load 丟掉／讀不到這份檔（#1822）
EXPORTER_REFUSED = "exporter_refused"  # da-guard：exporter 拒絕載入整棵樹（#1822）
BLOCKING_KINDS = frozenset({UNPARSEABLE, UNREADABLE, UNPARSED_BY_TOOL,
                            EXPORTER_DROPPED, EXPORTER_REFUSED})
# yaml.v3 是 libyaml 的移植：有 libyaml 就用它讀，scanner 層的判定才同源。
_LOADER = yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader
# `ThresholdConfig` 會解碼的頂層 key（`pkg/config/types.go`）；其餘頂層 key 的子樹
# Go 不解碼。
_DECODED_SECTIONS = ("defaults", "optional_overrides", "state_filters",
                     "tenants", "profiles", "max_metrics_per_tenant")

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


def _decoded(data):
    """原文的行列表，供 mark 的 line/column 切片（兩種 loader 的 column 都不算 BOM）。"""
    if data.startswith(codecs.BOM_UTF16_LE):
        text = data.decode("utf-16-le", "replace")
    elif data.startswith(codecs.BOM_UTF16_BE):
        text = data.decode("utf-16-be", "replace")
    else:
        text = data.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("﻿"):
        lines[0] = lines[0][1:]
    return lines


def _source_of(node, lines):
    s, e = node.start_mark, node.end_mark

    def seg(i, a, b):
        return lines[i][a:b] if i < len(lines) else ""

    if s.line == e.line:
        return seg(s.line, s.column, e.column)
    return (seg(s.line, s.column, None) + "".join(lines[s.line + 1:e.line])
            + seg(e.line, 0, e.column))


def _explicit_tag(node, lines):
    """顯式 tag 的短形式，沒有（或只有非特定的 `!`）就 None。"""
    src = _source_of(node, lines)
    if src.startswith("&"):
        parts = src.split(None, 1)
        src = parts[1] if len(parts) > 1 else ""
    if not src.startswith("!"):
        return None
    if src.split(None, 1)[0] == "!":
        return None
    return _short_tag(node.tag)


def _scalar_verdict(node, lines):
    """decode.go `scalar()` 對 float64 目標的下場: accepted / null / dropped。

    null：yaml.v3 解成 0，但 exporter 的 ParseConfigFile 接著把寫成 null 的
    key 拿掉（#2518）——這個 key 等於沒寫，不是 0 閾值。"""
    if not isinstance(node, yaml.ScalarNode):
        return "dropped"
    tag = _explicit_tag(node, lines)
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
        return "null"
    return "dropped"


def _raw_of(node, lines):
    if isinstance(node, yaml.MappingNode):
        return "<mapping>"
    if isinstance(node, yaml.SequenceNode):
        return "<list>"
    src = _source_of(node, lines).splitlines() or ["(空)"]
    return src[0] + ("…" if len(src) > 1 else "")


def _no_duplicate_keys(node):
    """decode.go `mapping()` 的 uniqueKeys 檢查（單層）。判定用共用的
    `_lib_io.duplicate_in_mapping`（#2123：node 種類＋原文相同即重複）。"""
    hit = duplicate_in_mapping(node)
    if hit is not None:
        dup = hit[0]
        raise _Dropped(
            f"重複 key `{dup.value if isinstance(dup, yaml.ScalarNode) else ''}`")


def _walk_duplicate_keys(node, visiting=frozenset()):
    """decode.go `mapping()` 的 uniqueKeys 檢查，套到這棵子樹的每一層 mapping。"""
    if id(node) in visiting:
        return
    visiting = visiting | {id(node)}
    if isinstance(node, yaml.MappingNode):
        _no_duplicate_keys(node)
        for _k, v in node.value:
            _walk_duplicate_keys(v, visiting)
    elif isinstance(node, yaml.SequenceNode):
        for v in node.value:
            _walk_duplicate_keys(v, visiting)


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


def exporter_verdicts(data):
    """root 層載體的位元組 exporter 讀不讀得進去: `[(key, 原文, kind), ...]`。

    輸入是檔案的 bytes。判定鏡射 yaml.v3 的 `decode.go`（`scalar()`／
    `mapping()`／`merge()`／`indicatedString()`）與 `resolve.go`（`resolve()`）對
    `ThresholdConfig`（`defaults: map[string]float64`）的行為；真值表在
    `tests/golden/fixtures/defaults-carrier-oracle.json`，Go 測試
    `TestDefaultsCarrierOracle` 是那張表的裁判。文件層級的項目 key 為 None。

    本函式說綠、exporter 卻整份丟的檔（值形狀、第二份 document 的 scanner 錯誤、
    merge key 的覆寫等向量表以外的面），由 `exporter_precheck` 問 exporter 自己的
    load 補上（#1822）；那時這裡只是第二意見。未體檢時這裡就是全部判定。

    已知近似與 NOT GUARDED（體檢補不了的那一側：本函式擋、exporter 收）:
      * 重複 key: Go 會解碼的頂層區塊（`_DECODED_SECTIONS`）底下所有深度都檢查。
        Go 對這些區塊內「未知欄位」底下的重複 key 會接受、本函式會擋——刻意
        fail-closed（YAML 1.2 本就不允許，且寫回的 safe_dump 會靜默丟掉前一個）。
        其他頂層 key 的子樹不看，與 Go 同。
      * merge key 的覆寫判定以 key 的字面文字比對；非 `!!str` 解析的 key（數字／
        布林／null）與 alias 當 key，Go 用解碼後的值比對，可能分歧（本函式多擋）。
      * 沒有 libyaml 時 pure parser 的 scanner 錯誤（如 tab）回 `UNPARSED_BY_TOOL`，
        不冒充 exporter 的判定（有 libyaml 時同一格由 `carrier_health` 補上）。
    """
    try:
        root = next(yaml.compose_all(data, Loader=_LOADER), None)
    except yaml.YAMLError as e:
        first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        if _LOADER is yaml.SafeLoader and isinstance(e, yaml.scanner.ScannerError):
            return [(None, first, UNPARSED_BY_TOOL)]
        return [(None, first, UNPARSEABLE)]
    if root is None:
        return []
    if not isinstance(root, yaml.MappingNode):
        return [(None, "頂層不是 mapping", UNPARSEABLE)]
    lines = _decoded(data)
    try:
        _no_duplicate_keys(root)
        top = {k.value: v for k, v in root.value if isinstance(k, yaml.ScalarNode)}
        for section in _DECODED_SECTIONS:
            if section in top:
                _walk_duplicate_keys(top[section])
        defaults = top.get("defaults")
        if defaults is None:
            return []
        if isinstance(defaults, yaml.ScalarNode):
            if _scalar_verdict(defaults, lines) == "null":
                return []                      # `defaults:` 空值＝nil map
            raise _Dropped("defaults 不是 mapping（scalar）")
        if not isinstance(defaults, yaml.MappingNode):
            raise _Dropped("defaults 不是 mapping（list）")
        out = []
        for key_node, val_node in _effective_entries(defaults, None, frozenset()):
            verdict = _scalar_verdict(val_node, lines)
            if verdict == "accepted":
                continue
            out.append((key_node.value, _raw_of(val_node, lines),
                        NULL_NOT_DECLARED if verdict == "null" else UNPARSEABLE))
        return out
    except _Dropped as e:
        return [(None, str(e), UNPARSEABLE)]


def carrier_health(data):
    """`exporter_verdicts` 加上「本工具自己讀不讀得了」: 讀寫走 PyYAML 的 pure
    parser，它拒絕而 libyaml／yaml.v3 接受的檔（如 tab）回 `UNPARSED_BY_TOOL`。"""
    items = exporter_verdicts(data)
    if _LOADER is yaml.SafeLoader or any(k in BLOCKING_KINDS for _, _, k in items):
        return items
    try:
        next(yaml.compose_all(data, Loader=yaml.SafeLoader), None)
    except yaml.scanner.ScannerError as e:
        first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        return [(None, f"exporter 讀得進去；{first}", UNPARSED_BY_TOOL)]
    except yaml.YAMLError:
        pass                                   # 兩邊都拒絕的檔上面已經列了
    return items


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
OWNER_EXPORTER = "exporter"  # exporter 自己會丟棄／不讀，不算殘留，只警告


def _base_of(name):
    return name.rsplit("/", 1)[-1]


def _in_subtree(name, plane):
    """子樹裡的檔: 子目錄裡的（`name` 含 `/`），或 `--plane subtree` 的這一層。"""
    return "/" in name or plane == "subtree"


def _nested_ignored(name, plane="root"):
    """子樹裡 `_defaults` 以外的 `_` 前綴檔: exporter 完全不讀（`isNestedPlatformFile`）。"""
    base = _base_of(name)
    return (_in_subtree(name, plane) and base.startswith("_")
            and not is_defaults_name(base))


def section_owner(name, section, plane="root"):
    """掃描結果的 `[section]` 在檔 `name` 裡由誰清: 本工具／手動／exporter 丟棄。

    root 層的規則來自 exporter 的 `applyBoundaryRules`（`flat_scanner.go`）:
    `defaults`／`optional_overrides` 只從 defaults 載體讀（#1676；其他 `_` 檔的
    這兩個區塊 exporter 出 WARN 後剝除，不是殘留）；`profiles` 從任何 `_` 前綴檔
    讀；本工具只寫載體的平台區塊與非 `_` 檔的 `tenants:`。子樹裡 exporter 只讀
    `_defaults.yaml` 的 `defaults:`（`parseDefaultsBytes`），其他 `_` 檔不讀；
    本工具不遞迴寫入，所以本工具會寫的那兩類在子目錄裡是「手動」（對該子樹跑）。
    """
    if _nested_ignored(name, plane):
        return OWNER_EXPORTER
    if section == "unreadable":
        return OWNER_MANUAL
    nested = "/" in name
    base = _base_of(name)
    block = section.split(".", 1)[0]
    if block in ("defaults", "optional_overrides"):
        if is_defaults_name(base):
            return OWNER_MANUAL if nested else OWNER_TOOL
        # #1676: 非載體（`_` 前綴或租戶檔）的這兩個區塊 exporter 都剝除。
        return OWNER_EXPORTER
    if base.startswith("_"):
        return OWNER_EXPORTER if _in_subtree(name, plane) else OWNER_MANUAL
    if block == "profiles":
        return OWNER_EXPORTER
    return OWNER_MANUAL if nested else OWNER_TOOL


def exporter_drop_reason(name, section, plane="root"):
    """`OWNER_EXPORTER` 的那一句警告（接在檔名後面）。"""
    if _nested_ignored(name, plane):
        return ("exporter 不讀子目錄裡 `_defaults` 以外的 `_` 前綴檔"
                "（isNestedPlatformFile），本工具不寫入")
    block = section.split(".", 1)[0]
    if _in_subtree(name, plane) and _base_of(name).startswith("_"):
        return (f"的 {block} 區塊 exporter 不讀（子樹 `_defaults.yaml` 只讀 "
                f"defaults 區塊），本工具不寫入")
    if block in ("defaults", "optional_overrides") and _base_of(name).startswith("_"):
        return (f"的 {block} 區塊 exporter 會丟棄（只從 defaults 載體讀，#1676；"
                f"exporter 會 WARN），本工具不寫入")
    return f"的 {block} 區塊 exporter 會丟棄（只從 `_` 前綴檔讀），本工具不寫入"


def manual_reason(name, section, key, val, config_dir):
    """`OWNER_MANUAL` 殘留的那一句話（預覽與執行同一句）。"""
    where = f"[{section}] {key}: {val}"
    if section == "unreadable":
        return f"無法讀取（{val}），本工具無法判定"
    base = _base_of(name)
    block = section.split(".", 1)[0]
    if "/" in name and (block in ("defaults", "optional_overrides")
                        or not base.startswith("_")):
        subtree = os.path.join(str(config_dir), *name.split("/")[:-1])
        return (f"子目錄裡的檔，本工具不遞迴寫入；請對該子樹跑 "
                f"`--config-dir {subtree} --plane subtree`：{where}")
    if block == "profiles":
        return f"`_` 前綴檔的 `profiles:` 區塊本工具射程外，請手動移除：{where}"
    return (f"本工具依設計不寫任何 `_` 前綴檔的 `tenants:` 區塊，請手動移除："
            f"{where}")


def unclearable_occurrences(metric_key, findings, *, predict, plane="root",
                            skip=frozenset()):
    """掃描結果中本工具的處置**不會**（`predict=True`）或**沒有**清掉的引用。

    回傳 `[(filename, section, key, value, designed), ...]`，是 key 級完成度的
    唯一判定點。`designed=True` 是 `section_owner` 判給手動的那一類，兩種模式
    都列；`OWNER_TOOL` 的引用預覽時扣掉（本輪會刪），`--execute` 後重掃仍在就
    列出——那才是「寫入沒有發生」。exporter 會丟棄的區塊不算殘留。`skip` 是
    本輪不改寫的租戶檔，它們的引用兩種模式都列（`designed=True`）。
    """
    out = []
    for f in findings:
        name = f["filename"]
        for section, key, val in f["occurrences"]:
            owner = section_owner(name, section, plane)
            if owner == OWNER_EXPORTER:
                continue
            if owner == OWNER_TOOL and name in skip:
                designed = True
            elif owner == OWNER_TOOL:
                if predict:
                    continue
                designed = False
            else:
                designed = True
            out.append((name, section, key, val, designed))
    return out


def _occurrences_in(data, metric_key, spellings=()):
    """一份已解析的 mapping 裡該 metric 的引用 `[(section, key, value), ...]`。"""
    out = []
    pattern_keys = metric_pattern_keys(metric_key, spellings)
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
                    if tenant_key_belongs_to_metric(key, metric_key, spellings)]
    return out


def scan_for_metric(metric_key, config_dir, spellings=()):
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
            occurrences = _occurrences_in(data, metric_key, spellings)
        if occurrences:
            findings.append({
                "filename": filename,
                "path": path,
                "occurrences": occurrences,
            })
    return findings


def nested_residue(metric_key, config_dir, spellings=()):
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
                       else _occurrences_in(data, metric_key, spellings))
        if occurrences:
            out.append({"filename": p.relative_to(base).as_posix(),
                        "path": str(p), "occurrences": occurrences})
    return out


def out_of_reach(entries, plane="root"):
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
              and e.name.startswith("_") and plane == "root"):
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

    exporter 每個目錄只讀一個載體（#1674：`.yaml` 拼法勝過 `.yml`，見
    `_lib_confd.select_defaults_carrier`），其餘拼法會被 WARN 且不被任何平面
    讀取。這裡仍列舉全部拼法、從每一個移除：沒被讀的那個一旦另一個被刪掉就會
    變成載體，留著舊 key 等於埋一個會復活的閾值。平面讀取，`warn_nested` 具名子目錄。
    """
    base = Path(config_dir)
    warn_nested(base, tool="deprecate_rule")
    entries = sorted(base.iterdir())
    return defaults_files_in(base, [e.name for e in entries if e.is_file()])


def remove_from_all_defaults(metric_key, config_dir, execute=False, spellings=()):
    """對每個 defaults 載體執行 `_remove_in_carrier`。

    回傳每個載體一筆 `(path, ok, msg, removed)`。沒有任何載體時回
    `resolve_defaults_file` 的正典路徑那一筆（`_defaults.yaml 不存在`）。
    平面讀取: 子樹載體要用 `--config-dir` 指到那一層。
    """
    base = Path(config_dir)
    carriers = defaults_carriers(base)
    if not carriers:
        carriers = [resolve_defaults_file(base)]
    return [(p, *_remove_in_carrier(metric_key, p, execute, spellings))
            for p in carriers]


def _remove_in_carrier(metric_key, defaults_path, execute=False, spellings=()):
    """從一個 defaults 載體移除 metric 的所有相關 key。

    回傳 `(ok, msg, removed)`，`removed` 為 `[(平面, key, 原值), ...]`，平面是
    `"defaults"` 或 `"optional_overrides"`。要刪的 key 來自 `metric_pattern_keys`
    ——掃描用的同一份。沒有相關 key 的載體是具名 no-op，一個位元組都不寫。
    """
    defaults_path = str(defaults_path)
    name = Path(defaults_path).name
    if not Path(defaults_path).exists():
        return False, f"{name} 不存在", []

    data = _load_for_rewrite(defaults_path)
    if data is None:
        return False, f"無法讀取 {name}", []

    # `defaults:` 空值是空區塊；list／scalar 是壞檔，不改寫、具名拒絕。
    defaults = data.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        return False, (f"defaults 不是 mapping（{_type_name(defaults)}），"
                       f"本工具不改寫，請先修檔"), []

    pattern_keys = metric_pattern_keys(metric_key, spellings)
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


def remove_from_tenants(metric_key, config_dir, execute=False, *,
                        skip=frozenset(), spellings=()):
    """從平面目錄下非 `_` 前綴的租戶檔移除該 metric 的 key；`skip` 裡的檔不碰。"""
    removed = []
    config_base = Path(config_dir)
    warn_nested(config_base, tool="deprecate_rule")
    for entry in sorted(
        p for p in config_base.iterdir()
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if (filename.startswith('_') or filename.startswith('.')
                or filename in skip):
            continue

        data = _load_for_rewrite(path)  # 讀不了的檔 `scan_for_metric` 已具名
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
                              if tenant_key_belongs_to_metric(key, metric_key,
                                                              spellings)]
            for key in keys_to_remove:
                val = tenant_config[key]
                removed.append((filename, tenant_name, key, val))
                if execute:
                    del tenant_config[key]
                    modified = True

        if modified and execute:
            save_yaml_file(path, data, _header_of(path))

    return removed


# ── exporter 的真 oracle（#1822）─────────────────────────────────────────
DA_GUARD_EXIT_CALLER_ERR = 2  # da-guard 自己的 exit 2：呼叫錯誤，或 exporter 拒絕整棵樹


def exporter_precheck(config_dir, metrics):
    """寫入前問 exporter 一次（`da-guard key-refs`，#1822）。

    回 `(tree, unchecked, refused, stderr_lines)`，三者至多一個有值：
      * `tree`: `KeyRefsTree`——這棵樹 exporter 丟掉哪些檔（`parse_failed`／
        `unreadable`），以及每個 metric 被哪些 key（原文）引用，判定用的是
        exporter 自己的 ValidateTenantKeys 分類（alias 表 legacy 拼法、
        `_critical`、維度鍵的各種拼法）。本工具不鏡射那些語意。
      * `unchecked`: 問不到（沒有 da-guard、binary 壞了或太舊、逾時）的原因——
        呼叫端印「未體檢」，行為照舊。
      * `refused`: da-guard 正常回答「exporter 拒絕載入整棵樹」（exit 2，例如
        同一個租戶宣告兩次）——呼叫端擋寫入。
    `stderr_lines` 是 da-guard 的 stderr，整份、不挑行。
    """
    try:
        tree = load_key_refs(config_dir, list(metrics))
    except DaGuardNotFoundError:
        return (None, "找不到 da-guard（$DA_GUARD_BINARY 未設或指向的路徑沒有檔案，"
                      "$PATH 上也沒有；本 repo 內 `make da-guard-build` 會建到 "
                      ".build/da-guard）", None, [])
    except DaGuardError as e:
        if e.returncode == DA_GUARD_EXIT_CALLER_ERR and not e.binary_fault:
            return None, None, e.message, e.stderr_lines
        return None, f"da-guard 無法回答（{e.message}）", None, e.stderr_lines
    return tree, None, None, tree.stderr_lines


def _print_guard_lines(what, lines):
    """da-guard 的 stderr 整份照轉到 stderr，每行加 `DA_GUARD_PREFIX`、跳脫。"""
    if lines:
        print(f"  ℹ️  {what} 的 stderr：", file=sys.stderr)
        for line in lines:
            print(f"{DA_GUARD_PREFIX}{safe_label(line)}", file=sys.stderr)


def exporter_after_writes(config_dir, metrics, spellings, skip):
    """本輪寫入（Step 1／Step 2，與 `--execute` 同一段寫入邏輯）套在 conf.d 的暫存
    複本上，再問 exporter 一次：回 `(tree, unchecked, refused)`，意義同
    `exporter_precheck`。真的 conf.d 一個位元組都不碰；複本用完即刪。複本跟隨
    symlink、略過懸空的（讀不到的檔看現況那一次的答案）。"""
    tmp = tempfile.mkdtemp(prefix="deprecate-rule-")
    try:
        copy = Path(tmp) / "conf.d"
        try:
            shutil.copytree(config_dir, copy, symlinks=False,
                            ignore_dangling_symlinks=True)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                for m in metrics:
                    remove_from_all_defaults(m, copy, execute=True,
                                             spellings=spellings[m])
                    remove_from_tenants(m, copy, execute=True, skip=skip,
                                        spellings=spellings[m])
        except (OSError, shutil.Error, SystemExit) as e:
            return None, f"無法在暫存複本上套用本輪寫入（{e}），寫入後的載體未經 exporter 判定", None
        tree, unchecked, refused, lines = exporter_precheck(copy, metrics)
        _print_guard_lines(f"da-guard key-refs（套用本輪寫入後的暫存複本 {copy}）", lines)
        return tree, unchecked, refused
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _health_reason(key, raw, kind):
    if kind == UNREADABLE:
        return f"本工具讀不了（無法讀取：{raw}），無法判定 exporter 讀不讀得進去"
    if kind == UNPARSED_BY_TOOL:
        return f"本工具讀不了（pure parser 限制）：{raw}；請先修檔"
    if kind == EXPORTER_DROPPED:
        return (f"exporter 讀不進這份檔（da-guard：{raw}；原因見上方 "
                f"`{DA_GUARD_PREFIX.strip()}` 行），整份檔會被丟棄，下架不會生效；請先修檔")
    if kind == EXPORTER_REFUSED:
        return (f"exporter 拒絕載入整棵樹（da-guard：{raw}），任何寫入都不會生效；"
                f"請先修好這棵樹")
    if key is None:
        return f"exporter 讀不進這份檔（{raw}），整份載體會被丟棄；請先修檔"
    return (f"`defaults:` 的 {key} 的值 {raw} exporter 讀不成數字（defaults 的型別是 "
            f"map[string]float64），這份載體會被整份丟棄，下架不會生效；請先修掉它。"
            f"若這個 --config-dir 其實是子樹載體（exporter 的 -config-dir 之下的"
            f"目錄），請加 `--plane subtree`")


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
    unreachable = out_of_reach(entries, args.plane)
    if unreachable:
        print("\n  ℹ️  本工具射程外（若仍持有該 metric 的 key，結尾會具名、rc 1）：")
        for name, reason in unreachable:
            print(f"     • {safe_label(name)}——{safe_label(reason)}")
    carriers_here = defaults_carriers(base)

    # #1822: 任何寫入之前（預覽與 --execute 一樣）問 exporter 一次。da-guard 的
    # stderr 整份照轉（每行加前綴）；問不到就明說「未體檢」，行為照舊。
    oracle, unchecked, refused, guard_lines = exporter_precheck(
        args.config_dir, args.metrics)
    _print_guard_lines("da-guard key-refs（現況）", guard_lines)
    if unchecked:
        print(f"\n  ⚠️  未體檢：{safe_label(unchecked)}——exporter 讀不讀得進這棵樹、"
              f"alias 表的 legacy 拼法都沒有問 exporter；本輪只用本工具自己的判定")
    spellings = {m: (frozenset(r.key for r in oracle.refs[m]) if oracle
                     else frozenset()) for m in args.metrics}

    # 載體體檢跑在任何寫入之前。root 平面的母體是這一層所有 `_` 前綴檔（exporter
    # 把每個都解碼成 ThresholdConfig；#1676 起非載體的 defaults 雖不生效，解碼失敗
    # 仍讓整檔連同 profiles 一起丟掉）；本輪自己要刪的 key 不算殘留；空值只警告；
    # exporter 讀不進去的（blocking）讓整輪降級為預覽。租戶檔的警告與平面無關。
    planned = {k for m in args.metrics
               for k in metric_pattern_keys(m, spellings[m])}
    health = {}
    blocked = set()
    tenant_bad = {}        # 租戶檔 → exporter 讀不進去的原因；本輪不改寫它
    for e in entries:
        if (not e.is_file() or not has_yaml_extension(e.name)
                or e.name.startswith(".")):
            continue
        items = carrier_health_at(e)
        name = safe_label(e.name)
        if e.name.startswith("_"):
            if args.plane != "root":
                # 子樹載體的字串值合法；本工具自己讀不了／讀不到的仍要擋寫入。
                if is_defaults_name(e.name):
                    items = [i for i in items
                             if i[2] in (UNREADABLE, UNPARSED_BY_TOOL)
                             or (i[2] == UNPARSEABLE and i[0] is None
                                 and _read_yaml(str(e))[1] is not None)]
                    if items:
                        health[e.name] = items
                        blocked.add(e.name)
                continue
            if is_defaults_name(e.name):
                items = [i for i in items if i[0] not in planned]
            for key, _raw, kind in items:
                if kind == NULL_NOT_DECLARED:
                    print(f"  ⚠️  {name} 的 `defaults:` 的 {safe_label(key)} 是空值"
                          f"（`{safe_label(key)}:`）——exporter 當作沒寫這個 key："
                          f"平台沒有宣告它，任何租戶都不會送出它的閾值，自己設了值"
                          f"的租戶也一樣（#2518）；請刪掉這一行或補一個數字")
            items = [i for i in items if i[2] in BLOCKING_KINDS]
            if items:
                health[e.name] = items
                blocked.add(e.name)
        else:
            for key, raw, kind in items:
                if kind != UNPARSEABLE:
                    continue
                if key is None:
                    if _read_yaml(str(e))[1] is not None:
                        continue               # `scan_for_metric` 會以無法讀取具名
                    print(f"  ⚠️  {name} exporter 讀不進去（{safe_label(raw)}）"
                          f"——整份租戶檔會被丟掉；本工具不寫它")
                    tenant_bad[e.name] = raw
                else:
                    print(f"  ⚠️  {name} 的 `defaults:` 有 exporter 讀不成數字的值"
                          f"（{safe_label(key)}: {safe_label(raw)}）——exporter 會整份"
                          f"丟掉這個租戶檔；本工具不寫它")
                    tenant_bad.setdefault(e.name, f"{key}: {raw}")
    # #1822: exporter 自己的 load 丟掉／讀不到的這一層檔。子目錄裡的檔不是這一層
    # 的，略過。租戶檔看現況（da-guard 的 parse_failed）：照既有慣例不改寫它、
    # 殘留具名。root 層 `_` 前綴檔看「本輪寫入之後」——本輪自己要刪的 key 可能
    # 正是讓它被丟的那一個（`old_metric: disable`），所以把本輪寫入套在暫存複本
    # 上再問一次；讀不到的檔本工具修不了，看現況。子樹平面不採用 `_` 前綴檔的
    # 判定：da-guard 把 --config-dir 當 root 判，子樹載體的值形狀規則不同。
    if oracle is not None:
        for name in oracle.parse_failed:
            if ("/" in name or name.startswith("_") or name in tenant_bad
                    or _read_yaml(str(base / name))[1] is not None):
                continue                       # 讀不了的 `scan_for_metric` 會具名
            print(f"  ⚠️  {safe_label(name)} exporter 讀不進去（da-guard：parse_failed）"
                  f"——整份租戶檔會被丟掉；本工具不寫它")
            tenant_bad[name] = "da-guard：parse_failed"
        for u in oracle.unscanned:
            print(f"  ⚠️  da-guard 列不出 {safe_label(u.file)} 的 key"
                  f"（{safe_label(u.reason)}）——這份檔的其他拼法未體檢")
        dropped = [(u.file, f"讀不到：{u.reason}") for u in oracle.unreadable]
        if args.plane == "root":
            after, after_unchecked, after_refused = exporter_after_writes(
                args.config_dir, args.metrics, spellings, tenant_bad)
            if after is not None:
                dropped += [(n, "本輪寫入後仍在 parse_failed")
                            for n in after.parse_failed]
            if after_refused and not refused:
                refused = f"本輪寫入後：{after_refused}"
            unchecked = unchecked or after_unchecked
            if after_unchecked:
                print(f"\n  ⚠️  未體檢：{safe_label(after_unchecked)}")
        for name, why in dropped:
            if "/" in name or name == "." or not name.startswith("_") or name in blocked:
                continue
            if args.plane != "root":
                continue
            health.setdefault(name, []).append((None, why, EXPORTER_DROPPED))
            blocked.add(name)
    if refused:
        health["."] = [(None, refused, EXPORTER_REFUSED)]
        blocked.add(".")
    if args.plane != "root" and carriers_here:
        print("  ℹ️  子樹載體不做型別體檢：子樹平面的字串值合法"
              "（`computeEffectiveConfig` 走 map[string]any），"
              "root 的 map[string]float64 規則不適用於這一層。")

    write = args.execute and not blocked
    if args.execute and blocked:
        tool_only = {n for n, items in health.items()
                     if all(k in (UNPARSED_BY_TOOL, UNREADABLE) for _, _, k in items)}
        who = ("本工具讀不了" if blocked <= tool_only
               else "exporter 讀不進去" if not (blocked & tool_only)
               else "exporter 或本工具讀不進去")
        print(f"\n  ⛔ 本輪降級為預覽：{safe_label('、'.join(sorted(blocked)))} "
              f"{who}，一個位元組都不寫；請先修掉結尾具名的殘留再重跑")
    action = "已移除" if write else ("將移除（本輪未寫入）" if args.execute
                                   else "將移除")

    incomplete = []
    findings_by_metric = {}
    nested_by_metric = {}

    for metric in args.metrics:
        print(f"\n{'─'*40}")
        print(f"📌 Processing: {metric}")
        print(f"{'─'*40}\n")

        # 掃描（印出的 Step 1 之前）
        findings = scan_for_metric(metric, args.config_dir, spellings[metric])
        nested = nested_residue(metric, args.config_dir, spellings[metric])
        findings_by_metric[metric] = findings
        nested_by_metric[metric] = nested
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
        # exporter 會丟棄／不讀的區塊只警告，一檔一區塊一次（含子目錄）。
        warned = set()
        for f in findings + nested:
            for section, _key, _val in f["occurrences"]:
                if section_owner(f["filename"], section, args.plane) != OWNER_EXPORTER:
                    continue
                block = section.split(".", 1)[0]
                if (f["filename"], block) in warned:
                    continue
                warned.add((f["filename"], block))
                print(f"  ⚠️  {safe_label(f['filename'])} "
                      f"{safe_label(exporter_drop_reason(f['filename'], section, args.plane))}")

        # Step 1 (as printed): 從每個 defaults 載體移除相關 key
        if args.plane != "root" and not carriers_here:
            print("\n  Step 1: 此子樹沒有自己的載體，跳過")
            results = []
        else:
            results = remove_from_all_defaults(metric, args.config_dir,
                                               execute=write,
                                               spellings=spellings[metric])
        for carrier, ok, msg, removed in results:
            print(f"\n  Step 1: {safe_label(carrier.name)}")
            icon = "✅" if ok else "❌"
            if ok and carrier.name in blocked:
                # 寫得進去不等於生效：exporter 讀不進這份載體（見結尾）。
                icon, msg = "⛔", f"{msg}——但這份載體讀不進去，見結尾"
            print(f"  {icon} {safe_label(msg)}")
            for where, key, val in removed:
                plane = "" if where == "defaults" else f"{where} 的 "
                print(f"     🗑️  {action} {plane}{safe_label(key)}"
                      f"（原值: {safe_label(val)}）")
            if not ok and carrier.name not in health:
                incomplete.append((metric, carrier.name, msg))

        # Step 2 (as printed): 從 tenant configs 移除
        print(f"\n  Step 2: Tenant configs")
        removed = remove_from_tenants(metric, args.config_dir, execute=write,
                                      skip=tenant_bad, spellings=spellings[metric])
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

    # 完成度是 KEY 級: 重掃（或預覽時用掃描結果）扣掉本工具會清的，剩下的具名。
    # 載體體檢已具名的檔不再以「無法讀取」重複列。
    for metric in args.metrics:
        findings = (scan_for_metric(metric, args.config_dir, spellings[metric])
                    if write else findings_by_metric[metric])
        findings = findings + nested_by_metric[metric]
        for name, section, key, val, designed in unclearable_occurrences(
                metric, findings, predict=not write, plane=args.plane,
                skip=tenant_bad):
            if section == "unreadable" and name in health:
                continue
            if name in tenant_bad:
                reason = (f"租戶檔 exporter 讀不進去（{tenant_bad[name]}），本工具"
                          f"不改寫，請先修檔：[{section}] {key}: {val}")
            elif designed:
                reason = manual_reason(name, section, key, val, args.config_dir)
            else:
                reason = f"重掃仍有引用：[{section}] {key}: {val}"
            incomplete.append((metric, name, reason))

    for carrier_name, items in health.items():
        for key, raw, kind in items:
            incomplete.append(("載體體檢", carrier_name,
                               _health_reason(key, raw, kind)))

    # 總結
    print(f"\n{'='*60}")
    if incomplete:
        print("❌ 下架未完成：")
        for metric, carrier, reason in incomplete:
            print(f"   • {safe_label(metric)} → {safe_label(carrier)}："
                  f"{safe_label(reason)}")
        print(f"{'='*60}")
        sys.exit(EXIT_VIOLATION)
    if unchecked:
        # 不擋（D12）：da-guard 是本工具的附加體檢，不是前置；缺了它本工具的判定
        # 與 #1822 之前相同，rc 照那套判定走。但結論不冒充體檢過。
        print(f"⚠️  未體檢：{safe_label(unchecked)}——以下結論沒有經過 exporter 的判定")
    if args.execute:
        print("✅ 下架完成！threshold-exporter 將在下次 reload 時生效。")
        print("📋 請在下個 Release Cycle 清理 Prometheus ConfigMap 中的對應規則。")
    else:
        print("💡 這是預覽模式。要實際執行，請加 --execute 參數。")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
