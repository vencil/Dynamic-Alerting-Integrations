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
canonical key 一樣列為引用、一樣移除；子目錄 `_` 檔裡的拼法用來判殘留）。
root 層 `_` 前綴檔的判定看「本輪寫入之後」：只把這些檔套上本輪寫入放進空的
暫存 conf.d 再問一次（本輪要刪的 key 可能正是讓載體被丟的那一個），仍被丟就
擋寫入、rc 1；那一次問不到就退回現況的答案；讀不到的檔看現況。
da-guard 的 `key-refs` 答不出來時印「未體檢」與原因，行為與 rc 照舊：那一層
體檢是附加的。da-guard 的 exit 2 一律算「答不出來」（未體檢），不當成 exporter
拒絕整棵樹：它也可能只是 da-guard 寫不出 JSON（例如非 UTF-8 檔名）。

讀取與寫入前確認（ADR-036 §2，#2906）：哪些檔、每個檔的內容、要刪哪些 key，
一律取自 `da-guard parse-tree`（yaml.v3 的讀法）；PyYAML 只用來產生寫回的文字。
`--execute` 要寫的每一個檔（所有 metric 合併、每檔一次）都先以 parse-tree 單檔
模式讀回確認——鍵、結構、每個純量的原文／型別／值都必須是原檔拿掉那幾個 key
——全部通過才寫，一個不通過就一個位元組都不寫、本輪降級為預覽、rc 1，結尾具名。
PyYAML 改寫時換了寫法的檔（例如 `~` 或空值寫成 `null`、`!!float 1` 寫成 `1.0`）
會因此被拒寫，請先手動改成不會被改寫的寫法。沒有任何讀取端讀的 defaults 拼法
（目錄另有被選的那一個）不寫：其中若還有該 metric 的 key，結尾具名、rc 1。
找不到可用的 da-guard（`$DA_GUARD_BINARY` → `$PATH`）是 rc 2。

NOT GUARDED（da-guard 也沒回答的）:
  * 平面判定仍靠 `--plane`: da-guard 把 --config-dir 當 root 判，所以
    `--plane subtree` 不採用它對 `_` 前綴檔的判定（租戶檔與拼法照用）。
  * 本工具依 parse-tree 讀出的型別判載體（`_node_verdicts`）；parse-tree 讀不了、
    exporter 其實收的檔（封閉清單以外的差異，ADR-036 §2）仍擋。

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
import contextlib
import io
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
# ADR-036 §2 (#2906): which files there are, what each one holds and what to
# delete are read from `da-guard parse-tree` (yaml.v3's reading: tenant ids
# and keys are their source text, `yes` is the string "yes"). PyYAML is used
# for one thing only — producing the text of a rewrite: the two steps that
# WRITE a file back read it through `load_yaml_file_for_rewrite`, where every
# plain value is its source text (#2220), and dump with `dump_for_rewrite`.
# That text is read back by parse-tree before anything is written.
from _lib_io import load_yaml_file_for_rewrite, strict_load_for_rewrite  # noqa: E402
from _lib_yaml_keys import dump_for_rewrite  # noqa: E402
import _lib_parse_cache  # noqa: E402  (ADR-036 §2, #2906: parse-tree's selection and reading)
import _lib_parse_tree  # noqa: E402
import _lib_write_check  # noqa: E402
from _lib_python import write_text_or_die  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_tenant_values import (  # noqa: E402  (#1822 the exporter's own answer)
    DA_GUARD_PREFIX,
    DaGuardError,
    DaGuardNotFoundError,
    load_key_refs,
)
from _lib_confd import warn_nested  # noqa: E402
from _lib_parse_tree import RESERVED_CLASSES  # noqa: E402  (Go's `_` classes)


def _reader(config_dir):
    """這一輪對 *config_dir* 的 parse-tree 讀取端（`main` 開的 session 內，整輪
    共用同一份文件——寫入之前的那一份）。"""
    return _lib_parse_cache.reader_for(config_dir)


def _top(reader):
    """*reader* 那份文件裡這一層（不含子目錄）的逐路徑索引 `{名字: Entry}`。

    #3206：一輪只有一份索引——取自 `reader` 讀內容的那份 parse-tree 文件
    （`ConfdReader.listing`），不另列一次；是不是設定檔看 Go 的 `role`，本工具
    不自己判（不以 `is_file()` 預設排除：它對 stat 被拒的檔答 False）。平面讀取，
    `warn_nested` 具名子目錄（每個目錄只印一次）。"""
    warn_nested(reader.root, tool="deprecate_rule", reader=reader)
    return {e.path: e for e in reader.listing().entries if e.depth == 0}


def _unreadable_text(exc):
    return f"da-guard parse-tree：{exc.kind}：{exc.message}"


def _tree_read(reader, rel):
    """parse-tree 讀出的 *rel*（yaml.v3 的值）。#3206：這一輪的索引通過了共用
    的拒絕，被選的檔沒有讀不了的（有 problem 的路徑都有 verdict），所以不必再
    處理 `FileUnreadable`。"""
    return reader.read(rel)


def _read_yaml(path, *, for_rewrite=False):
    """`(data, error)`: 讀不到／解析失敗／頂層不是 mapping 時 `error` 是一句話。

    ADR-036 §2（#2906）：內容一律是 `da-guard parse-tree` 的讀法——這份檔的
    位元組以單檔模式、當成它所在目錄的那個檔讀（一個值是什麼、要不要改都依它
    判）。`for_rewrite=True` 是唯一的例外：寫回時產生文字用的 PyYAML 讀取
    （#2220，未加引號的純量讀成原文 `RawPlain`），不拿來做任何判定。
    """
    if for_rewrite:
        try:
            data = load_yaml_file_for_rewrite(path, default={})
        except (OSError, yaml.YAMLError, RecursionError) as e:
            return None, str(e).splitlines()[0] if str(e) else e.__class__.__name__
        if data is None:
            return {}, None
        if not isinstance(data, dict):
            return None, f"頂層不是 mapping（{type(data).__name__}）"
        return data, None
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        return None, e.strerror or e.__class__.__name__
    try:
        tree = _lib_write_check.read_as(p.parent, p.name, raw)
        return _lib_parse_tree.to_python(tree.node(p.name)), None
    except _lib_parse_tree.FileUnreadable as exc:
        return None, _unreadable_text(exc)


def load_yaml_file(path):
    """讀 YAML mapping（parse-tree 的讀法）；讀不到就回 None（呼叫端自己具名，這裡不印）。"""
    data, _err = _read_yaml(path)
    return data


def _load_for_rewrite(path):
    """寫回時產生文字用的 PyYAML 讀取（#2220）；讀不了回 None。"""
    data, _err = _read_yaml(path, for_rewrite=True)
    return data


def _type_name(node):
    """parse-tree 節點的型別名（訊息用）：mapping／list，純量是 yaml.v3 的 tag。"""
    if _lib_parse_tree.is_scalar(node):
        return node["tag"]
    return "list" if isinstance(node, list) else "mapping"


def _render(data, header_comment=""):
    """寫回的文字：檔頭註解＋PyYAML 的輸出（`RawPlain` 照原文寫回，#2220）。"""
    return (header_comment or "") + dump_for_rewrite(
        data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def save_yaml_file(path, data, header_comment=""):
    """安全寫入 YAML 檔案（不做寫入前確認；conf.d 的寫入走 `_write_confirmed`）。"""
    # #1641: an existing conf.d file (derived, not an output flag) that cannot
    # be written is rc 2, not rc 1.
    write_text_or_die(path, _render(data, header_comment))


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


# ── 載體體檢：parse-tree 讀出的 `defaults:` 能不能解成 map[string]float64 ──
UNPARSEABLE = "unparseable"            # exporter 整份丟掉這個檔
NULL_NOT_DECLARED = "null_not_declared"  # 這一個 key 寫成 null：exporter 當作沒宣告（只警告，#2518）
UNREADABLE = "unreadable"              # 本工具讀不到，無法判定
UNPARSED_BY_TOOL = "unparsed_by_tool"  # PyYAML（產生寫回文字）讀不了；exporter 未必
EXPORTER_DROPPED = "exporter_dropped"  # da-guard：exporter 自己的 load 丟掉／讀不到這份檔（#1822）
BLOCKING_KINDS = frozenset({UNPARSEABLE, UNREADABLE, UNPARSED_BY_TOOL,
                            EXPORTER_DROPPED})
_NUMBER_TAGS = ("!!int", "!!float")


def _node_verdicts(node):
    """root 層載體（parse-tree 讀得了的）的 `defaults:` exporter 讀不讀得進去:
    `[(key, 原文, kind), ...]`。

    ADR-036 §2（#2906）：值是 yaml.v3 讀出的節點，判定只看它的 tag——`!!int`／
    `!!float` 解得成 float64；`!!null` 是「沒宣告」（#2518）；其他（字串、布林、
    日期、mapping、list）讓 exporter 整份丟掉這個載體。先前這裡是一份 yaml.v3
    `resolve()`／`decode.go` 的 Python 仿製，已刪；真值表照舊是
    `tests/golden/fixtures/defaults-carrier-oracle.json`（Go 測試
    `TestDefaultsCarrierOracle` 是裁判）。文件層級的項目 key 為 None。
    """
    defaults = node.get("defaults")
    if defaults is None:
        return []
    if _lib_parse_tree.is_scalar(defaults):
        if defaults["tag"] == "!!null":
            return []                          # `defaults:` 空值＝nil map
        return [(None, "defaults 不是 mapping（scalar）", UNPARSEABLE)]
    if isinstance(defaults, list):
        return [(None, "defaults 不是 mapping（list）", UNPARSEABLE)]
    out = []
    for key in sorted(defaults):
        val = defaults[key]
        if _lib_parse_tree.is_scalar(val):
            if val["tag"] in _NUMBER_TAGS:
                continue
            raw = val["text"].splitlines()[0] if val["text"] else "(空)"
            if "\n" in val["text"].rstrip("\n"):
                raw += "…"
            out.append((key, raw, NULL_NOT_DECLARED if val["tag"] == "!!null"
                        else UNPARSEABLE))
        else:
            out.append((key, "<list>" if isinstance(val, list) else "<mapping>",
                        UNPARSEABLE))
    return out


def _verdicts_of(load_node, rewrite_text):
    """`_node_verdicts` of the node `load_node()` returns, plus「本工具自己寫不寫
    得回去」：parse-tree 讀得了、PyYAML（產生寫回文字）讀不了的檔回
    `UNPARSED_BY_TOOL`。parse-tree 讀不了的檔整份 `UNPARSEABLE`（`read_failure`
    是 `UNREADABLE`）。"""
    try:
        node = load_node()
    except _lib_parse_tree.FileUnreadable as exc:
        kind = UNREADABLE if exc.kind == "read_failure" else UNPARSEABLE
        return [(None, f"{exc.kind}：{exc.message}", kind)]
    items = _node_verdicts(node)
    if any(k in BLOCKING_KINDS for _, _, k in items):
        return items
    try:
        strict_load_for_rewrite(rewrite_text())
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError) as e:
        first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        return [(None, f"exporter 讀得進去；{first}", UNPARSED_BY_TOOL)]
    return items


def exporter_verdicts(data):
    """`carrier_health` 不含「本工具讀不讀得了」那一層：*data*（檔案的 bytes）
    以 parse-tree 單檔模式、當成空 conf.d 的 `_defaults.yaml` 讀，再交給
    `_node_verdicts`。"""
    def load():
        with tempfile.TemporaryDirectory(prefix="deprecate-rule-") as empty:
            return _lib_write_check.read_as(empty, "_defaults.yaml", data).node(
                "_defaults.yaml")
    try:
        return _node_verdicts(load())
    except _lib_parse_tree.FileUnreadable as exc:
        return [(None, f"{exc.kind}：{exc.message}", UNPARSEABLE)]


def carrier_health(data):
    """root 層載體的位元組（不在任何樹裡，空 conf.d 的 `_defaults.yaml`）的體檢:
    `exporter_verdicts` 加上 PyYAML 讀不讀得了（`UNPARSED_BY_TOOL`）。"""
    def load():
        with tempfile.TemporaryDirectory(prefix="deprecate-rule-") as empty:
            return _lib_write_check.read_as(empty, "_defaults.yaml", data).node(
                "_defaults.yaml")
    return _verdicts_of(load, lambda: data.decode("utf-8"))


def carrier_health_at(path):
    """`carrier_health`，讀這一輪 parse-tree 文件裡的那個檔（*path* 所在目錄的
    樹；`main` 的 session 內同一份文件）；讀不到回一個 `UNREADABLE` 項（key None）。"""
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            text = fh.read()
    except OSError as e:
        return [(None, e.strerror or e.__class__.__name__, UNREADABLE)]
    reader = _reader(p.parent)
    return _verdicts_of(lambda: reader.node(p.name), lambda: text.decode("utf-8"))


# ── 掃描與完成度 ──────────────────────────────────────────────────────────
OWNER_TOOL = "tool"          # Step 1／2 會清
OWNER_MANUAL = "manual"      # 本工具射程外，殘留要人去改（兩種模式都 rc 1）
OWNER_EXPORTER = "exporter"  # exporter 自己會丟棄／不讀，不算殘留，只警告


def _in_subtree(name, plane):
    """子樹裡的檔: 子目錄裡的（`name` 含 `/`），或 `--plane subtree` 的這一層。"""
    return "/" in name or plane == "subtree"


#: Go 的 class：defaults 載體（`_defaults.yaml`／`.yml`，任何大小寫）；
#: `_` 前綴的類別全體是 `_lib_parse_tree.RESERVED_CLASSES`（另一種是其他 `_` 前綴檔）。
CLS_DEFAULTS = "defaults"


def _nested_ignored(name, plane="root", *, cls):
    """子樹裡 `_defaults` 以外的 `_` 前綴檔: exporter 完全不讀（`isNestedPlatformFile`）。
    *cls*：這個檔的 Go class（`Entry.cls`，#3206：不以名稱自己判）。"""
    return _in_subtree(name, plane) and cls in RESERVED_CLASSES and cls != CLS_DEFAULTS


def section_owner(name, section, plane="root", *, cls):
    """掃描結果的 `[section]` 在檔 `name` 裡由誰清: 本工具／手動／exporter 丟棄。

    root 層的規則來自 exporter 的 `applyBoundaryRules`（`flat_scanner.go`）:
    `defaults`／`optional_overrides` 只從 defaults 載體讀（#1676；其他 `_` 檔的
    這兩個區塊 exporter 出 WARN 後剝除，不是殘留）；`profiles` 從任何 `_` 前綴檔
    讀；本工具只寫載體的平台區塊與非 `_` 檔的 `tenants:`。子樹裡 exporter 只讀
    `_defaults.yaml` 的 `defaults:`（`parseDefaultsBytes`），其他 `_` 檔不讀；
    本工具不遞迴寫入，所以本工具會寫的那兩類在子目錄裡是「手動」（對該子樹跑）。
    *cls*：`name` 的 Go class（`Entry.cls`：`defaults` 是載體、`platform` 是
    其他 `_` 前綴檔）。
    """
    if _nested_ignored(name, plane, cls=cls):
        return OWNER_EXPORTER
    nested = "/" in name
    reserved = cls in RESERVED_CLASSES
    block = section.split(".", 1)[0]
    if block in ("defaults", "optional_overrides"):
        if cls == CLS_DEFAULTS:
            return OWNER_MANUAL if nested else OWNER_TOOL
        # #1676: 非載體（`_` 前綴或租戶檔）的這兩個區塊 exporter 都剝除。
        return OWNER_EXPORTER
    if reserved:
        return OWNER_EXPORTER if _in_subtree(name, plane) else OWNER_MANUAL
    if block == "profiles":
        return OWNER_EXPORTER
    return OWNER_MANUAL if nested else OWNER_TOOL


def exporter_drop_reason(name, section, plane="root", *, cls):
    """`OWNER_EXPORTER` 的那一句警告（接在檔名後面）。*cls* 同 `section_owner`。"""
    reserved = cls in RESERVED_CLASSES
    if _nested_ignored(name, plane, cls=cls):
        return ("exporter 不讀子目錄裡 `_defaults` 以外的 `_` 前綴檔"
                "（isNestedPlatformFile），本工具不寫入")
    block = section.split(".", 1)[0]
    if _in_subtree(name, plane) and reserved:
        return (f"的 {block} 區塊 exporter 不讀（子樹 `_defaults.yaml` 只讀 "
                f"defaults 區塊），本工具不寫入")
    if block in ("defaults", "optional_overrides") and reserved:
        return (f"的 {block} 區塊 exporter 會丟棄（只從 defaults 載體讀，#1676；"
                f"exporter 會 WARN），本工具不寫入")
    return f"的 {block} 區塊 exporter 會丟棄（只從 `_` 前綴檔讀），本工具不寫入"


def manual_reason(name, section, key, val, config_dir, *, reserved, root_dir=None):
    """`OWNER_MANUAL` 殘留的那一句話（預覽與執行同一句）。*reserved* 同 `section_owner`。"""
    where = f"[{section}] {key}: {val}"
    block = section.split(".", 1)[0]
    if "/" in name and (block in ("defaults", "optional_overrides") or not reserved):
        subtree = os.path.join(str(config_dir), *name.split("/")[:-1])
        return (f"子目錄裡的檔，本工具不遞迴寫入；請對該子樹跑 "
                f"`--config-dir {subtree} --plane subtree --root-dir {root_dir or config_dir}`："
                f"{where}")
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
            owner = section_owner(name, section, plane, cls=f["cls"])
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
                for pk in pattern_keys if pk in _declared_texts(declared)]
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


def _declared_texts(declared):
    """`optional_overrides:` 的項目當成 key 名比對（值形狀的項目不算）。"""
    return [d if isinstance(d, str) else str(d) for d in declared
            if not isinstance(d, (dict, list))]


def _top_files(config_dir, reader):
    """parse-tree 選到的這一層（不含子目錄）的設定檔名，排序。Go 判定不是設定檔的
    項目（目錄、目錄 symlink、FIFO、懸空連結）不在其中——有這種項目的樹在取得索引
    時就被拒絕（#3206）。"""
    selected = reader.tree().selected
    return sorted(n for n, e in _top(reader).items() if e.config_file and n in selected)


def scan_for_metric(metric_key, config_dir, spellings=(), *, reader=None):
    """平面掃描 conf.d 這一層引用該 metric 的檔（parse-tree 選的檔與讀法）。

    回傳 `[{filename, path, occurrences}, ...]`（讀不了的檔在共用的拒絕就停了，
    #3206）。*reader* 預設是這一輪的那一份文件；寫入後的重掃傳一個新的
    `ConfdReader`。
    """
    findings = []
    config_base = Path(config_dir)
    reader = reader or _reader(config_dir)
    warn_nested(config_base, tool="deprecate_rule", reader=reader)
    for filename in _top_files(config_dir, reader):
        occurrences = _occurrences_in(_tree_read(reader, filename), metric_key, spellings)
        if occurrences:
            e = reader.entry(filename)  # Go's class (#3206)
            findings.append({
                "filename": filename,
                "path": str(config_base / filename),
                "occurrences": occurrences,
                "reserved": e.reserved,
                "cls": e.cls,
            })
    return findings


def nested_residue(metric_key, config_dir, spellings=(), *, reader=None):
    """子目錄裡（平面那一層以下）仍引用該 metric 的檔——只讀，供完成度判定。

    形狀同 `scan_for_metric`，`filename` 是相對 POSIX 路徑（含 `/`，所以
    `section_owner` 判給手動）。檔是 parse-tree 選的那些。
    """
    base = Path(config_dir)
    reader = reader or _reader(config_dir)
    out = []
    selected = reader.tree().selected
    # #3206：同一份索引，是不是設定檔看 Go 的 `role`。
    for e in sorted((e for e in reader.listing().config_files()
                     if e.depth > 0 and e.path in selected), key=lambda e: e.path):
        rel = e.path
        occurrences = _occurrences_in(_tree_read(reader, rel), metric_key, spellings)
        if occurrences:
            out.append({"filename": rel, "path": str(base / rel),
                        "occurrences": occurrences, "reserved": e.reserved, "cls": e.cls})
    return out


def out_of_reach(config_dir, plane="root"):
    """這一層本工具不寫的載體 `[(名字, 理由), ...]`: 子目錄、`_` 前綴檔的
    `tenants:`／`profiles:` 區塊（含 `_defaults.yaml` 自己的）、沒有讀取端的
    defaults 拼法（parse-tree 的 `not_read`）。

    #3206：這一層有哪些項目、各是什麼，全取自 `reader` 那份索引（Go 的 `role`）：
    子目錄是走訪會進去的目錄（role `dir`，名字像設定檔的也算——它底下的檔照讀）；
    不以 `iterdir()`／`is_dir()` 自己判。"""
    reader = _reader(config_dir)
    tree = reader.tree()
    out = []
    for name, listed in sorted(_top(reader).items()):
        if listed.is_dir:
            out.append((f"{name}/", "子目錄，本工具不遞迴寫入；殘留請對該子樹跑 "
                                    "`--config-dir <子樹> --plane subtree --root-dir <root>`"))
        elif not listed.config_file:
            continue
        elif name in tree.not_read and listed.cls == "defaults":
            out.append((name, f"沒有讀取端讀這個 defaults 拼法（parse-tree："
                                f"{tree.not_read[name]}），本工具不寫它；"
                                f"殘留需手動移除或刪掉此檔"))
        elif name in tree.selected and listed.reserved and plane == "root":
            data = _tree_read(reader, name)
            for section in ("tenants", "profiles"):
                if data.get(section):
                    out.append((name, f"`_` 前綴檔的 `{section}:` 區塊，"
                                        f"本工具不寫，殘留需手動移除"))
    return out


def defaults_carriers(config_dir):
    """conf.d 這一層所有 defaults 載體（`_defaults.yaml`／`.yml`，任意大小寫）。

    exporter 每個目錄只讀一個載體（#1674：`.yaml` 拼法勝過 `.yml`）；哪一個由
    parse-tree 的選檔說（ADR-036 §2）。沒被選的拼法（`not_read`）照樣列出：它
    若持有該 metric 的 key 是殘留（`remove_from_all_defaults` 不寫它——沒有讀取
    端的路徑不寫，ADR-036 §2——而是具名、rc 1），否則一旦被選的那個被刪掉就會
    變成載體，埋一個會復活的閾值。平面讀取，`warn_nested` 具名子目錄。
    """
    base = Path(config_dir)
    reader = _reader(config_dir)
    warn_nested(base, tool="deprecate_rule", reader=reader)
    tree = reader.tree()
    # #3206：同一份索引；載體是 class `defaults` 的設定檔（Go 的 `role`）。
    names = {n for n, e in _top(reader).items()
             if e.config_file and e.cls == "defaults"
             and (n in tree.selected or n in tree.not_read)}
    return [base / n for n in sorted(names)]


def remove_from_all_defaults(metric_key, config_dir, execute=False, spellings=(),
                             *, done=False):
    """對每個 defaults 載體執行 `_remove_in_carrier`。

    回傳每個載體一筆 `(path, ok, msg, removed)`。沒有任何載體時回正典路徑
    那一筆（`_defaults.yaml 不存在`）。平面讀取: 子樹載體要用 `--config-dir`
    指到那一層。*done*：本輪已由 `apply_rewrites` 寫入（訊息用「已」）。
    """
    base = Path(config_dir)
    carriers = defaults_carriers(base)
    if not carriers:
        carriers = [base / "_defaults.yaml"]
    return [(p, *_remove_in_carrier(metric_key, p, execute, spellings, done=done))
            for p in carriers]


def _carrier_plan(node, pattern_keys):
    """parse-tree 節點上的判定：`(removed, defaults 的錯誤)`。`removed` 是
    `[(平面, key, 原值), ...]`（原值是 yaml.v3 的值）。`defaults:` 空值是空區塊；
    list／scalar 是壞檔，不改寫、具名拒絕。"""
    defaults = node.get("defaults")
    is_null = defaults is None or (_lib_parse_tree.is_scalar(defaults)
                                   and defaults["tag"] == "!!null")
    is_map = isinstance(defaults, dict) and not _lib_parse_tree.is_scalar(defaults)
    if not (is_null or is_map):
        return [], (f"defaults 不是 mapping（{_type_name(defaults)}），"
                    f"本工具不改寫，請先修檔")
    defaults = defaults if is_map else {}
    declared = node.get("optional_overrides")
    declared = declared if isinstance(declared, list) else []
    declared_texts = [d["text"] for d in declared if _lib_parse_tree.is_scalar(d)]
    removed = [("defaults", pk, _lib_parse_tree.to_python(defaults[pk]))
               for pk in pattern_keys if pk in defaults]
    removed += [("optional_overrides", pk, "（宣告，無值）")
                for pk in pattern_keys if pk in declared_texts]
    return removed, None


def _carrier_rewrite(path, node, keys):
    """寫回一個載體、拿掉 *keys*（`defaults:` 與 `optional_overrides:` 兩個平面）:
    `(text, expected)`。判定與預期取自 parse-tree 節點 *node*；PyYAML 只產生文字
    （讀不了時 `ValueError`）。只碰本輪刪過東西的平面；清空的平面整個拿掉。"""
    keys = set(keys)
    data = _load_for_rewrite(str(path))
    if data is None:
        raise ValueError("PyYAML 讀不了它，無法產生寫回的文字")
    _lib_write_check.keep_null_spelling(data, node)  # `key:` / `~` written as read
    hit_defaults = isinstance(node.get("defaults"), dict) and \
        not _lib_parse_tree.is_scalar(node["defaults"]) and \
        bool(keys & set(node["defaults"]))
    declared = node.get("optional_overrides")
    hit_declared = isinstance(declared, list) and any(
        _lib_parse_tree.is_scalar(d) and d["text"] in keys for d in declared)
    if hit_defaults:
        defaults = data.get("defaults") or {}
        for pk in keys:
            defaults.pop(pk, None)
        if defaults:
            data["defaults"] = defaults
        else:
            data.pop("defaults", None)
    if hit_declared:
        kept = [k for k in (data.get("optional_overrides") or []) if k not in keys]
        if kept:
            data["optional_overrides"] = kept
        else:
            data.pop("optional_overrides", None)

    def change(tree):
        if hit_defaults:
            for pk in keys:
                tree["defaults"].pop(pk, None)
            if not tree["defaults"]:
                del tree["defaults"]
        if hit_declared:
            tree["optional_overrides"] = [
                d for d in tree["optional_overrides"]
                if not (_lib_parse_tree.is_scalar(d) and d["text"] in keys)]
            if not tree["optional_overrides"]:
                del tree["optional_overrides"]
    return _render(data, _header_of(path)), _lib_write_check.with_change(node, change)


def _write_confirmed(config_dir, items):
    """ADR-036 §2：`[(path, text, expected), ...]` 全部先以 parse-tree 確認（寫入
    的路徑、讀回的內容），一個不通過就一個位元組都不寫（WriteRefused）；全部
    通過才逐一寫入。"""
    base = Path(config_dir)
    _lib_write_check.confirm_all(base, [
        (Path(p).relative_to(base).as_posix(), text, expected)
        for p, text, expected in items])
    for p, text, _expected in items:
        write_conf_text(p, text)


def write_conf_text(path, text):
    """一個已確認的 conf.d 檔寫入（#1641: 寫不了是 rc 2）。"""
    write_text_or_die(str(path), text)


def _remove_in_carrier(metric_key, defaults_path, execute=False, spellings=(), *,
                       done=False):
    """從一個 defaults 載體移除 metric 的所有相關 key。

    回傳 `(ok, msg, removed)`，`removed` 為 `[(平面, key, 原值), ...]`，平面是
    `"defaults"` 或 `"optional_overrides"`。要刪的 key 來自 `metric_pattern_keys`
    ——掃描用的同一份；讀法是 parse-tree 的（ADR-036 §2）。沒有相關 key 的載體
    是具名 no-op，一個位元組都不寫。沒有讀取端讀的拼法不寫：有 key 就是殘留
    （ok=False，手動移除）。
    """
    defaults_path = Path(defaults_path)
    name = defaults_path.name
    reader = _reader(defaults_path.parent)
    if reader.entry(name) is None:  # #3206: the listing, not `exists()`
        return False, f"{name} 不存在", []
    tree = reader.tree()
    pattern_keys = metric_pattern_keys(metric_key, spellings)
    if name in tree.not_read:
        # Read for the question "does it still hold the key" only; never written.
        try:
            node = _lib_write_check.read_as(defaults_path.parent, name,
                                            defaults_path.read_bytes()).node(name)
        except (OSError, _lib_parse_tree.FileUnreadable):
            return True, f"{name} 沒有讀取端讀它，本工具不寫入", []
        removed, _err = _carrier_plan(node, pattern_keys)
        if not removed:
            return True, f"{name} 沒有 {metric_key} 相關 key，略過", []
        return False, (f"{name} 沒有任何讀取端讀它（parse-tree：{tree.not_read[name]}），"
                       f"本工具不寫入；其中 {len(removed)} 個 key 請手動移除或刪掉此檔"), removed
    node = reader.node(name)  # #3206: a run past the shared refusal reads it
    removed, err = _carrier_plan(node, pattern_keys)
    if err:
        return False, err, []
    if not removed:
        return True, f"{name} 沒有 {metric_key} 相關 key，略過", []
    if done:
        return True, f"已從 {name} 移除 {len(removed)} 個 key", removed
    if not execute:
        return True, f"將從 {name} 移除 {len(removed)} 個 key", removed
    try:
        text, expected = _carrier_rewrite(defaults_path, node, pattern_keys)
        _write_confirmed(defaults_path.parent, [(defaults_path, text, expected)])
    except ValueError as exc:
        return False, f"{name}：{exc}，本工具不改寫，請先修檔", removed
    except _lib_write_check.WriteRefused as exc:
        return False, f"{name}：寫入前的 parse-tree 確認不通過，未寫入：{exc.detail}", removed
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


def _tenant_plan(node, belongs):
    """parse-tree 節點上的判定：`[(tenant, key, 原值), ...]`。"""
    tenants = node.get("tenants")
    if not isinstance(tenants, dict) or _lib_parse_tree.is_scalar(tenants):
        return []
    out = []
    for tenant_name, cfg in tenants.items():
        if not isinstance(cfg, dict) or _lib_parse_tree.is_scalar(cfg):
            continue
        out += [(tenant_name, key, _lib_parse_tree.to_python(cfg[key]))
                for key in cfg if belongs(key)]
    return out


def _tenant_rewrite(path, node, hits):
    """寫回一個租戶檔、拿掉 *hits*（`[(tenant, key, _), ...]`，取自 parse-tree）:
    `(text, expected)`；PyYAML 只產生文字（讀不了時 `ValueError`）。"""
    data = _load_for_rewrite(str(path))
    tenants = data.get("tenants") if isinstance(data, dict) else None
    if not isinstance(tenants, dict):  # parse-tree read a mapping: PyYAML did not
        raise ValueError("PyYAML 讀不了它，無法產生寫回的文字")
    _lib_write_check.keep_null_spelling(data, node)  # `key:` / `~` written as read
    for tenant_name, key, _val in hits:
        cfg = tenants.get(tenant_name)
        if isinstance(cfg, dict):
            cfg.pop(key, None)

    def change(tree):
        for tenant_name, key, _val in hits:
            tree["tenants"][tenant_name].pop(key, None)
    return _render(data, _header_of(path)), _lib_write_check.with_change(node, change)


def remove_from_tenants(metric_key, config_dir, execute=False, *,
                        skip=frozenset(), spellings=(), done=False):
    """從平面目錄下非 `_` 前綴的租戶檔移除該 metric 的 key；`skip` 裡的檔不碰。

    判定（哪個檔、哪個 key）取自 parse-tree；`execute` 時每個檔寫入前都經
    parse-tree 確認，一個不通過就這一批都不寫（WriteRefused 往上丟）。
    """
    removed = []
    config_base = Path(config_dir)
    reader = _reader(config_dir)
    warn_nested(config_base, tool="deprecate_rule", reader=reader)
    writes = []
    for filename in _top_files(config_dir, reader):
        if reader.entry(filename).reserved or filename in skip:
            continue
        node = reader.node(filename)  # #3206: past the shared refusal, readable
        hits = _tenant_plan(node, lambda k: tenant_key_belongs_to_metric(
            k, metric_key, spellings))
        removed += [(filename, t, k, v) for t, k, v in hits]
        if hits and execute and not done:
            path = config_base / filename
            writes.append((path, *_tenant_rewrite(path, node, hits)))
    if writes:
        _write_confirmed(config_base, writes)
    return removed


def plan_rewrites(config_dir, metrics, spellings, *, skip=frozenset()):
    """本輪 `--execute` 要寫的每一個檔 `[(path, text, expected), ...]`，所有
    metric 合併（每個檔只寫一次）。判定取自這一輪的 parse-tree 文件，文字由
    PyYAML 產生；PyYAML 讀不了一個要改寫的檔時 `ValueError`（含檔名）。"""
    base = Path(config_dir)
    reader = _reader(config_dir)
    tree = reader.tree()
    keys = {k for m in metrics for k in metric_pattern_keys(m, spellings[m])}
    out = []
    for path in defaults_carriers(base):
        if path.name in tree.not_read:
            continue
        node = reader.node(path.name)
        removed, err = _carrier_plan(node, sorted(keys))
        if removed and not err:
            try:
                out.append((path, *_carrier_rewrite(path, node, keys)))
            except ValueError as exc:
                raise ValueError(f"{path.name}：{exc}") from None

    def belongs(k):
        return any(tenant_key_belongs_to_metric(k, m, spellings[m]) for m in metrics)
    for filename in _top_files(config_dir, reader):
        if reader.entry(filename).reserved or filename in skip:
            continue
        node = reader.node(filename)
        hits = _tenant_plan(node, belongs)
        if hits:
            path = base / filename
            try:
                out.append((path, *_tenant_rewrite(path, node, hits)))
            except ValueError as exc:
                raise ValueError(f"{filename}：{exc}") from None
    return out


# ── exporter 的真 oracle（#1822）─────────────────────────────────────────


def exporter_precheck(config_dir, metrics):
    """寫入前問 exporter 一次（`da-guard key-refs`，#1822）。

    回 `(tree, unchecked, stderr_lines)`，`tree` 與 `unchecked` 恰一個有值：
      * `tree`: `KeyRefsTree`——這棵樹 exporter 丟掉哪些檔（`parse_failed`／
        `unreadable`），以及每個 metric 被哪些 key（原文）引用，判定用的是
        exporter 自己的 ValidateTenantKeys 分類（alias 表 legacy 拼法、
        `_critical`、維度鍵的各種拼法）。本工具不鏡射那些語意。
      * `unchecked`: 問不到的原因——沒有 da-guard、binary 壞了或太舊、逾時，
        以及 da-guard 的任何 exit 2。exit 2 不當成「exporter 拒絕整棵樹」：它
        也可能是 da-guard 自己寫不出 JSON（例如非 UTF-8 檔名），而 exporter
        收這棵樹（#1822 F5）。呼叫端印「未體檢」，行為照舊。
    `stderr_lines` 是 da-guard 的 stderr，整份、不挑行。
    """
    try:
        tree = load_key_refs(config_dir, list(metrics))
    except DaGuardNotFoundError:
        return (None, "找不到 da-guard（$DA_GUARD_BINARY 未設或指向的路徑沒有檔案，"
                      "$PATH 上也沒有；本 repo 內 `make da-guard-build` 會建到 "
                      ".build/da-guard）", [])
    except DaGuardError as e:
        return None, f"da-guard 無法回答（{e.message}）", e.stderr_lines
    return tree, None, tree.stderr_lines


def _print_guard_lines(what, lines):
    """da-guard 的 stderr 整份照轉到 stderr，每行加 `DA_GUARD_PREFIX`、跳脫。"""
    if lines:
        print(f"  ℹ️  {what} 的 stderr：", file=sys.stderr)
        for line in lines:
            print(f"{DA_GUARD_PREFIX}{safe_label(line)}", file=sys.stderr)


def root_platform_files(config_dir):
    """這一層的 `_` 前綴檔 `[(名字, 實際路徑), ...]`：parse-tree 選到的，以及它列在
    `not_read` 的 defaults 拼法（暫存 conf.d 裡的選檔要與這一層相同）。

    照 exporter 的 walk（parse-tree 的就是它）：不跟目錄 symlink；檔案 symlink
    以連結所在目錄解析（`os.path.realpath(連結路徑)`）。不是一般檔的不在其中。
    """
    # Flat on purpose: only the root `_` files bear on the verdict (see
    # `exporter_after_writes`); the nested-tree WARN is the tool's, printed once.
    reader = _reader(config_dir)
    warn_nested(Path(config_dir), tool="deprecate_rule", reader=reader)
    tree = reader.tree()
    out = []
    # #3206：同一份索引；是不是設定檔看 Go 的 `role`——不是 `os.path.isfile`，
    # 它對指進 000 目錄的連結答 False，平台檔就被靜默排除。讀不到的由呼叫端略過、
    # 由現況那次的 `unreadable` 擋下。
    for name, e in sorted(_top(reader).items()):
        if not (e.config_file and e.reserved) or (
                name not in tree.selected and name not in tree.not_read):
            continue
        out.append((name, os.path.realpath(str(Path(config_dir) / name))))
    return out


def exporter_after_writes(config_dir, metrics, spellings):
    """本輪寫入後，root 層 `_` 前綴檔 exporter 讀不讀得進去（#1822）。

    回 `(parse_failed, unchecked)`：`parse_failed` 是寫入後仍被丟的檔名，問不到
    時為 None、`unchecked` 是原因——呼叫端改用現況的答案（fail-closed）。

    只看 root 層 `_` 檔本身：exporter 判它們丟不丟只看各檔的位元組與哪些
    根載體存在（Go 測試 `TestKeyRefs_RootPlatformVerdictIsPerFile` 釘住），與
    租戶檔、子目錄無關。所以把它們（套上本輪的寫入，同一段 `plan_rewrites`）
    放進一個只有它們的暫存 conf.d 再問一次；真的 conf.d 一個位元組都不碰。
    """
    tmp = None
    try:
        try:
            files = root_platform_files(config_dir)
            if not files:
                return [], None
            tmp = tempfile.mkdtemp(prefix="deprecate-rule-")
            copy = Path(tmp) / "conf.d"
            copy.mkdir()
            for name, real in files:
                # 讀不到的檔略過、不讓整次判定作廢：exporter 也不 decode 它（載體由
                # 下一個拼法遞補），它由現況那次的 `unreadable` 擋下。
                try:
                    with open(real, "rb") as src:
                        data = src.read()
                except OSError:
                    continue
                (copy / name).write_bytes(data)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                for path, text, _expected in plan_rewrites(copy, metrics, spellings):
                    path.write_text(text, encoding="utf-8", newline="\n")
        except (Exception, SystemExit) as e:  # noqa: BLE001 — fail-closed below
            return None, f"無法在暫存目錄套用本輪寫入（{e.__class__.__name__}: {e}）"
        tree, unchecked, lines = exporter_precheck(copy, metrics)
        _print_guard_lines(f"da-guard key-refs（root 層 `_` 檔套用本輪寫入後，暫存於 {copy}）",
                           lines)
        if tree is None:
            return None, unchecked
        return list(tree.parse_failed), None
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _health_reason(key, raw, kind):
    if kind == UNREADABLE:
        return f"本工具讀不了（無法讀取：{raw}），無法判定 exporter 讀不讀得進去"
    if kind == UNPARSED_BY_TOOL:
        return (f"本工具讀不了（寫回的文字由 PyYAML 產生，它讀不了這份檔）：{raw}；"
                f"請先修檔")
    if kind == EXPORTER_DROPPED:
        return (f"exporter 讀不進這份檔（da-guard：{raw}；原因見上方 "
                f"`{DA_GUARD_PREFIX.strip()}` 行），整份檔會被丟棄，下架不會生效；請先修檔")
    if key is None:
        return f"exporter 讀不進這份檔（{raw}），整份載體會被丟棄；請先修檔"
    return (f"`defaults:` 的 {key} 的值 {raw} exporter 讀不成數字（defaults 的型別是 "
            f"map[string]float64），這份載體會被整份丟棄，下架不會生效；請先修掉它。"
            f"若這個 --config-dir 其實是子樹載體（exporter 的 -config-dir 之下的"
            f"目錄），請加 `--plane subtree --root-dir <exporter 的 -config-dir>`")


@_lib_parse_cache.exit_on_parse_tree_error  # ADR-036 §2: no usable da-guard → rc 2
def main():
    """CLI entry point: 規則/指標下架工具。."""
    with _lib_parse_cache.session():  # one parse-tree document for the run
        _main()


def _main():
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
                             "體檢；subtree 跳過（子樹平面的字串值合法），並需要 --root-dir")
    parser.add_argument("--root-dir", default=None,
                        help="--plane subtree 時必填：exporter 的 -config-dir（conf.d 的 "
                             "root），--config-dir 必須是它底下 exporter 會走進的目錄")

    args = parser.parse_args()
    if args.plane == "subtree":
        # #3206: a subtree is not a root — `da-guard parse-tree` run on it
        # judges it as one (a subtree carrier's legal string value reads as a
        # root default the exporter drops). The run's index is the ROOT's
        # tree-mode document: the shared refusal is taken on the whole tree,
        # and the subtree's reader is cut from it (`subtree_reader`); the
        # writes stay in --config-dir.
        if args.root_dir is None:
            parser.error("--plane subtree requires --root-dir <the conf.d root>")
        try:
            _lib_parse_cache.subtree_reader(args.config_dir, args.root_dir)
        except ValueError as exc:
            parser.error(f"--config-dir / --root-dir: {exc}")
    elif args.root_dir is not None:
        parser.error("--root-dir is for --plane subtree only")
    return _deprecate(args)


def _deprecate(args):
    """The run, once the arguments are parsed."""
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
    reader = _reader(args.config_dir)
    # #3206：這一輪只有一份索引——`reader` 讀內容的那份文件。取得它就是共用的
    # fail-closed 出口（`ConfdReader.tree`）：樹上有 da-guard 驗證會點名的路徑
    # （列不出內容的目錄、懸空連結、名字像設定檔的目錄、exporter 解不開的檔……）
    # 就在任何寫入之前逐條點名、rc 2——不再「略過」或結尾列為未完成。
    listing = reader.listing()
    warn_nested(base, tool="deprecate_rule", listing=listing)
    unreachable = out_of_reach(base, args.plane)
    if unreachable:
        print("\n  ℹ️  本工具射程外（若仍持有該 metric 的 key，結尾會具名、rc 1）：")
        for name, reason in unreachable:
            print(f"     • {safe_label(name)}——{safe_label(reason)}")
    carriers_here = defaults_carriers(base)

    # #1822: 任何寫入之前（預覽與 --execute 一樣）問 exporter 一次。da-guard 的
    # stderr 整份照轉（每行加前綴）；問不到就明說「未體檢」，行為照舊。
    oracle, unchecked, guard_lines = exporter_precheck(
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
    # exporter 讀不進去的（blocking）讓整輪降級為預覽。
    planned = {k for m in args.metrics
               for k in metric_pattern_keys(m, spellings[m])}
    health = {}
    blocked = set()
    # #3206: only the `_` files are checked here — a tenant file the exporter
    # drops is a path da-guard validation names, refused before this point.
    for e in (base / n for n in _top_files(base, reader)):
        if not reader.entry(e.name).reserved:  # Go's class (#3206)
            continue
        items = carrier_health_at(e)
        name = safe_label(e.name)
        if args.plane != "root":
            # 子樹載體的字串值合法；本工具自己讀不了／讀不到的仍要擋寫入。
            if reader.entry(e.name).cls == CLS_DEFAULTS:
                items = [i for i in items if i[2] in (UNREADABLE, UNPARSED_BY_TOOL)]
                if items:
                    health[e.name] = items
                    blocked.add(e.name)
            continue
        if reader.entry(e.name).cls == CLS_DEFAULTS:
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
    # #1822: exporter 自己的 load 丟掉／讀不到的這一層檔。子目錄裡的檔不是這一層
    # 的，略過。root 層 `_` 前綴檔看「本輪寫入之後」——本輪自己要刪的 key 可能
    # 正是讓它被丟的那一個（`old_metric: disable`），見 `exporter_after_writes`；
    # 那一次問不到就退回現況的答案（fail-closed，不放行）。讀不到的檔本工具修
    # 不了，看現況。子樹平面不採用 `_` 前綴檔的判定：da-guard 把 --config-dir
    # 當 root 判，子樹載體的值形狀規則不同。
    if oracle is not None:
        # #3206: the current `parse_failed` / `unreadable` are paths da-guard
        # validation names, so they are empty on a run that got here; what is
        # left to ask is the tree AFTER this round's writes.
        for u in oracle.unscanned:
            print(f"  ⚠️  da-guard 列不出 {safe_label(u.file)} 的 key"
                  f"（{safe_label(u.reason)}）——這份檔的其他拼法未體檢")
        dropped = []
        if args.plane == "root":
            after, after_why = exporter_after_writes(
                args.config_dir, args.metrics, spellings)
            if after is not None:
                dropped += [(n, "本輪寫入後仍在 parse_failed") for n in after]
            else:
                print(f"\n  ⚠️  寫入後的體檢問不到（{safe_label(after_why)}）——root 層 "
                      f"`_` 前綴檔改用現況的 parse_failed 判定")
                dropped += [(n, "現況在 parse_failed；寫入後的體檢問不到")
                            for n in oracle.parse_failed]
        for name, why in dropped:
            entry = reader.entry(name)
            if ("/" in name or name == "." or entry is None or not entry.reserved
                    or name in blocked):
                continue
            if args.plane != "root":
                continue
            health.setdefault(name, []).append((None, why, EXPORTER_DROPPED))
            blocked.add(name)
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
    # ADR-036 §2 (#2906): every file this run rewrites — all metrics merged,
    # one write per file — is confirmed by `da-guard parse-tree` before ANY
    # is written; one refusal (or a file PyYAML cannot produce the rewrite
    # of) and nothing is written: the run is a preview, rc 1, the reason
    # named at the end.
    refused = None
    if write:
        try:
            _write_confirmed(base, plan_rewrites(base, args.metrics, spellings))
        except _lib_write_check.WriteRefused as exc:
            refused = (exc.rel, f"寫入前的 parse-tree 確認不通過（{exc.reason}）：{exc.detail}")
        except ValueError as exc:
            name, _sep, why = str(exc).partition("：")
            refused = (name, f"{why}，本工具不改寫，請先修檔")
        if refused is not None:
            write = False
            print(f"\n  ⛔ 本輪降級為預覽：{safe_label(refused[0])} 的寫回沒有通過寫入前"
                  f"確認，一個位元組都不寫；請先修掉結尾具名的問題再重跑")
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
                    print(f"     • {safe_label(f['filename'])} → "
                          f"[{safe_label(section)}] {safe_label(key)}: "
                          f"{safe_label(val)}")
        else:
            print(f"  ✅ 未發現任何引用")
        # exporter 會丟棄／不讀的區塊只警告，一檔一區塊一次（含子目錄）。
        warned = set()
        for f in findings + nested:
            for section, _key, _val in f["occurrences"]:
                if section_owner(f["filename"], section, args.plane,
                                 cls=f["cls"]) != OWNER_EXPORTER:
                    continue
                block = section.split(".", 1)[0]
                if (f["filename"], block) in warned:
                    continue
                warned.add((f["filename"], block))
                print(f"  ⚠️  {safe_label(f['filename'])} "
                      f"{safe_label(exporter_drop_reason(f['filename'], section, args.plane, cls=f['cls']))}")

        # Step 1 (as printed): 從每個 defaults 載體移除相關 key
        if args.plane != "root" and not carriers_here:
            print("\n  Step 1: 此子樹沒有自己的載體，跳過")
            results = []
        else:
            results = remove_from_all_defaults(metric, args.config_dir,
                                               spellings=spellings[metric],
                                               done=write)
        for carrier, ok, msg, removed in results:
            print(f"\n  Step 1: {safe_label(carrier.name)}")
            icon = "✅" if ok else "❌"
            if ok and carrier.name in blocked:
                # 寫得進去不等於生效：exporter 讀不進這份載體（見結尾）。
                icon, msg = "⛔", f"{msg}——但這份載體讀不進去，見結尾"
            print(f"  {icon} {safe_label(msg)}")
            for where, key, val in removed:
                plane = "" if where == "defaults" else f"{where} 的 "
                print(f"     🗑️  {action if ok else '殘留（本工具不寫）'} "
                      f"{plane}{safe_label(key)}（原值: {safe_label(val)}）")
            if not ok and carrier.name not in health:
                incomplete.append((metric, carrier.name, msg))

        # Step 2 (as printed): 從 tenant configs 移除
        print(f"\n  Step 2: Tenant configs")
        removed = remove_from_tenants(metric, args.config_dir,
                                      spellings=spellings[metric], done=write)
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
        if unchecked:
            # 有 exporter 的答案時 legacy 拼法已在 Step 1／2 處理；未體檢就只能提醒。
            print(f"     • 若此 metric 在 exporter alias 表（pkg/config/aliases.go）有 "
                  f"legacy 名字，租戶檔請一併檢查")

    # 完成度是 KEY 級: 重掃（或預覽時用掃描結果）扣掉本工具會清的，剩下的具名。
    # 載體體檢已具名的檔不再以「無法讀取」重複列。
    after = None
    if write:
        after = (_lib_parse_cache.subtree_reader(args.config_dir, args.root_dir, fresh=True)
                 if args.plane == "subtree" else _lib_parse_cache.ConfdReader(args.config_dir))
    for metric in args.metrics:
        # After the writes: a NEW parse-tree read (the run's document is the
        # tree as it was before them).
        findings = (scan_for_metric(metric, args.config_dir, spellings[metric],
                                    reader=after)
                    if write else findings_by_metric[metric])
        findings = findings + nested_by_metric[metric]
        reserved = {f["filename"] for f in findings if f["reserved"]}
        for name, section, key, val, designed in unclearable_occurrences(
                metric, findings, predict=not write, plane=args.plane):
            if designed:
                # The guidance names the conf.d ROOT: --root-dir on the subtree
                # plane (#3206), --config-dir on the root plane.
                reason = manual_reason(name, section, key, val, args.config_dir,
                                       reserved=name in reserved,
                                       root_dir=args.root_dir if args.plane == "subtree"
                                       else None)
            else:
                reason = f"重掃仍有引用：[{section}] {key}: {val}"
            incomplete.append((metric, name, reason))

    for carrier_name, items in health.items():
        for key, raw, kind in items:
            incomplete.append(("載體體檢", carrier_name,
                               _health_reason(key, raw, kind)))
    if refused is not None:
        incomplete.append(("寫入前確認", refused[0], refused[1]))

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
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
