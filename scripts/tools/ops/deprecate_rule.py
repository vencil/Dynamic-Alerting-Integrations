#!/usr/bin/env python3
"""deprecate_rule.py — 規則/指標下架工具。

安全地將指定的 metric key 從平台中淘汰。步驟編號與本工具印出來的一致:
  Step 1: 從每個 `_defaults.yaml`／`_defaults.yml` 載體移除該 metric 的相關
          key（`defaults:` 與 `optional_overrides:` 兩個平面）
  Step 2: 掃描平面目錄下非 `_` 前綴的租戶檔，移除殘留的 metric key
  Step 3: 印出需要手動處理的 Prometheus ConfigMap 清理指引
  最後: 完成度判定（key 級重掃 + root 載體型別體檢），不通過就 rc 1

下架的定義是「刪掉 key」，不是「把值寫成 disable」(#1787):
  * `disable` 是**租戶 opt-out** 的語意（租戶不要平台仍在供應的這個指標），
    不是下架；下架之後平台已經不供應它了，沒有東西可以 opt out。
  * 寫進 root `defaults:` 的字串更不是「關掉一個 key」——理由與實測見
    `non_numeric_defaults`。
  * 完成度以 KEY 為單位判定：`--execute` 之後會用同一支 `scan_for_metric`
    重掃，還掃得到引用就走「下架未完成」(rc 1)。

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
import re
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
from _lib_python import load_yaml_file as _lib_load_yaml  # noqa: E402
from _lib_python import write_text_or_die  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION  # noqa: E402
from _lib_confd import (  # noqa: E402  (#1588 shared name predicates)
    defaults_files_in,
    has_yaml_extension,
    is_defaults_name,
    resolve_defaults_file,
    unusable_config_entries,
    unusable_reason,
    warn_nested,
)



def load_yaml_file(path):
    """安全載入 YAML 檔案，with warning on parse error.

    Delegates to ``_lib_python.load_yaml_file`` for the common path.
    Catches ``yaml.YAMLError`` on corrupt files (which the lib version
    intentionally does not swallow) so the deprecation workflow can
    continue with a warning instead of aborting.
    """
    try:
        result = _lib_load_yaml(path, default={})
        if result is None:
            return {}
        return result
    except (OSError, yaml.YAMLError) as e:
        print(f"  ⚠️  無法讀取 {safe_label(path)}: {safe_label(e)}")
        return None


def save_yaml_file(path, data, header_comment=""):
    """安全寫入 YAML 檔案。"""
    content = ""
    if header_comment:
        content += header_comment
    content += yaml.safe_dump(data, default_flow_style=False,
                              allow_unicode=True, sort_keys=False)
    # #1641: the path is an existing conf.d file (derived, not an output
    # flag) — no flag to name, but an unwritable file is rc=2, not rc=1.
    write_text_or_die(path, content)


def metric_pattern_keys(metric_key):
    """一個 metric 在 conf.d 佔用的**精確 key 集合** —— 掃描與處置的同一份來源。

    #1787 (rulebook D-05d: 控制項必須呼叫產線那份)。這份清單原本有兩份複製，
    **兩份都在掃描側** (`scan_for_metric` 與 `remove_from_tenants` 各自的
    `pattern_keys`)；defaults 的寫入端則根本沒有清單可言 —— 它只 `get` 一個
    BASE key，於是一個只有 `<m>_critical` 的載體會被掃描報出來、被寫入端當成
    「沒這個 metric」，然後補上一行 `<m>: disable` 就宣告處理完畢。一份清單、
    三邊呼叫，才會有「掃得到的，處置就清得掉」這個性質。

    ⚠️ 這裡**不含**維度形狀 (`<m>{label="v"}`)：標籤集合是開放的。租戶平面
    的完整述詞是 `tenant_key_belongs_to_metric`，它把維度形狀錨在這四個確切
    名字上；本函式是封閉的那一半。
    """
    return [
        metric_key,
        f"{metric_key}_critical",
        f"custom_{metric_key}",
        f"custom_{metric_key}_critical",
    ]


def tenant_key_belongs_to_metric(key, metric_key):
    """租戶平面上「這個 key 是不是這個 metric 的」—— 掃描／移除／重掃同一份述詞。

    #1787 二輪 (D-05d)。這一軸原本有兩個**不同**的述詞:

    * 掃描 (`scan_for_metric`): `metric_key in key`（子字串）
    * 移除 (`remove_from_tenants`): `key in pattern_keys or (metric_key in key
      and "{" in key)`

    掃描比移除寬，所以每一個「含有這個字串、但不是這個 metric」的 key 都會被
    掃描報成引用、卻永遠不會被移除 —— key 級完成度重掃於是把它判成殘留，
    rc 1，而且**沒有任何重跑清得掉**。實測: stock 樹
    (`da-tools init --rule-packs mariadb,kubernetes`) 下架 `container_cpu`，
    會被另一個活指標的 `container_cpu_throttle_critical` 卡死。反過來也壞:
    移除端的子字串分支會把 `container_cpu_throttle{pod="x"}` 當成
    `container_cpu` 的維度鍵刪掉 —— 那是**另一個指標的資料**。

    正確的述詞是可推導的，不是列舉的: 一個租戶 key 屬於這個 metric，當且僅當
    它**是** `metric_pattern_keys` 的某個名字，或是那個名字的維度形狀
    (`<pk>{...}`)。維度那一半用形狀比對（標籤集合是開放的），但錨點仍是那四個
    確切名字，所以「含有這個字串」不再等於「屬於這個 metric」。
    """
    for pk in metric_pattern_keys(metric_key):
        if key == pk or key.startswith(pk + "{"):
            return True
    return False


# `non_numeric_defaults` 的兩種類別 —— 兩種**不同的**下場，不是一種的兩個
# 說法（#1787 三輪 F-03）。
UNPARSEABLE = "unparseable"      # 整份載體被丟
DECODES_TO_ZERO = "decodes_to_zero"  # 這一個 key 變成 0 閾值

# YAML 1.2 core schema 的數值文法，逐字寫出來 —— 這是一個**跨語言 oracle**，
# 所以不能借 PyYAML 的 resolver（#1787 四輪 F-05，rulebook D-05a:「你的 parser
# 與它的 parser 對同一段位元組可能給出不同的型別，而兩邊都不報錯」）。實測的
# 分歧: PyYAML 走 YAML 1.1，把 `yes` 解成 bool、`1:30` 解成六十進位 int 90、
# 而不認得 `0o17`；Go 的 yaml.v3 走 core schema，`1:30`／`yes` 兩個都是
# **字串**（餵進 `map[string]float64` 會讓整份檔解析失敗），`0o17` 是 15。
# ⚠️ `0o` 是**選擇性**的: 實測 yaml.v3 也把 `017` 收成 15（legacy octal），
# 所以文法要跟著它，不是跟著規格書。那一格就是這面鏡子存在的理由。
# 兩邊的矩陣，case 名相同:
#   tests/ops/test_deprecate_rule_carriers.py 的 `DEFAULTS_SCALAR_ORACLE`
#   components/threshold-exporter/app/config_deprecation_terminal_shape_test.go
#   的 `defaultsScalarOracle`
_YAML12_INT = re.compile(
    r"^[-+]?(0|[1-9][0-9_]*)$|^[-+]?0o?[0-7_]+$|^0x[0-9a-fA-F_]+$")
_YAML12_FLOAT = re.compile(
    r"^[-+]?(\.[0-9_]+|[0-9][0-9_]*(\.[0-9_]*)?)([eE][-+]?[0-9]+)?$"
    r"|^[-+]?\.(inf|Inf|INF)$|^\.(nan|NaN|NAN)$")
_YAML12_NULL = ("", "~", "null", "Null", "NULL")


def non_numeric_defaults(carrier_text):
    """root `defaults:` 底下每一個 exporter 讀不成門檻的 `(key, 原文, kind)`。

    輸入是載體的**原始 YAML 文字**，不是解析後的 mapping (#1787 四輪 F-05)。
    理由是 oracle: 要判的是「yaml.v3 能不能把這段**位元組**收進
    `map[string]float64`」，而 `yaml.safe_load` 先用 PyYAML 的 YAML 1.1
    resolver 把它變成 Python 物件之後，那個問題已經被答錯了 ——
    `cpu_usage: 1:30` 在 Python 這邊是 int 90（看起來合格），在 Go 那邊是字串
    （整份檔失敗）；`cpu_usage: "80"` 兩邊都是字串，但 `safe_load` 之後與
    `80` 不再分得出來。所以這裡走 `yaml.compose`，拿到 ScalarNode 的原文與
    引號樣式，再套上面那兩條**自己寫的** core-schema 文法。

    `ThresholdConfig.Defaults` 是 `map[string]float64`
    (`components/threshold-exporter/app/pkg/config/types.go:208`)，它有兩種
    壞法，實測分開（三輪 F-03；二輪只寫了第一種，並把第二種也掛在第一種的
    理由下，那句話是假的）:

    * `UNPARSEABLE` —— 引號字串／`yes`／`1:30`／日期／mapping／list。
      `yaml.Unmarshal` 對整個 document 回錯，`parsePartialConfig` 回
      ok=false，被丟掉的是**整份載體**（這份檔的其餘門檻連同它的
      `state_filters:`／`_routing_defaults:`）。舊版工具留下的
      `old_metric: disable` 是這一類。
    * `DECODES_TO_ZERO` —— 空值／`null`／`~`。實測 ok=**true**，解成 `0`。
      檔案好好的，但那個 key 變成一條 **0 閾值**，對每個租戶都成立 —— 等於
      武裝一條永遠觸發的告警。這不是「沒有這個 key」。

    ⛔ 只對 **root／平面**載體有意義，呼叫端用 `--plane` 決定（四輪 F-02）。
    子樹載體走 `computeEffectiveConfig` 的 `map[string]any`，字串在那裡合法
    且生效。
    """
    try:
        root = yaml.compose(carrier_text)
    except yaml.YAMLError:
        return []          # 壞檔：Step 1 的 `無法讀取` 已經具名
    if not isinstance(root, yaml.MappingNode):
        return []
    out = []
    for k_node, v_node in root.value:
        if getattr(k_node, "value", None) != "defaults":
            continue
        if not isinstance(v_node, yaml.MappingNode):
            return []      # 空 block／非 mapping：各自已有具名路徑
        for key_node, val_node in v_node.value:
            key = key_node.value
            if not isinstance(val_node, yaml.ScalarNode):
                kind_of = ("mapping" if isinstance(val_node, yaml.MappingNode)
                           else "list")
                out.append((key, f"<{kind_of}>", UNPARSEABLE))
                continue
            raw = val_node.value
            if val_node.style in ('"', "'"):
                # 引號＝明確的 `!!str`。yaml.v3 不會把它強制成 float64。
                out.append((key, f"{val_node.style}{raw}{val_node.style}",
                            UNPARSEABLE))
            elif raw in _YAML12_NULL:
                out.append((key, raw or "(空)", DECODES_TO_ZERO))
            elif _YAML12_INT.match(raw) or _YAML12_FLOAT.match(raw):
                continue
            else:
                out.append((key, raw, UNPARSEABLE))
    return out


def carrier_defaults_health(path):
    """讀一個載體檔，回傳 `non_numeric_defaults` 的結果（讀不到就空）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return non_numeric_defaults(fh.read())
    except OSError:
        return []


def unclearable_occurrences(metric_key, findings, *, predict):
    """掃描結果中，本工具的處置**不會**（或**沒有**）清掉的引用。

    回傳 `[(filename, section, key, value), ...]`，是 key 級完成度不變式的
    唯一判定點，兩種模式共用 (#1787 二輪 F-03):

    * `predict=False`（`--execute` 後重掃）: 只排除**依設計不歸本工具**的那
      一類。一個 Step 1／2 應該刪、卻還在的 key 必須留在結果裡 —— 否則寫入
      失敗會被這支函式自己遮掉。
    * `predict=True`（預覽）: 額外扣掉本輪**計畫**要刪的 key。預覽沒有寫任何
      東西，所以不能直接拿掃描結果當殘留；但它必須和 `--execute` 給出同一個
      判斷，否則 #1609 那句「預覽不能讀起來比執行乾淨」就只是註解。

    ⛔ 兩種模式都排除**非載體檔的 `defaults:`／`optional_overrides:` 區塊**:
    exporter 自己會丟棄它（main 已按檔名具名警告），而本工具依設計不寫它 ⇒
    列入會製造一個沒有任何重跑清得掉的 rc 1。#1609 的 blind review 正是為此
    把它拿掉的。

    回傳的第五格 `designed` 把殘留分成兩種**不同的話**（#1787 三輪 F-04）:

    * `designed=True` —— 本工具依設計不碰的東西: **任何** `_` 前綴檔的
      `tenants:` 區塊（`remove_from_tenants` 明文跳過所有 `_` 開頭檔名）。
      它需要的是一句「請手動移除」，不是「重掃仍有引用」——後者讀起來像寫入
      失敗，會把人送去查一個沒發生的 bug。

    ⚠️ **`defaults:`／`optional_overrides:` 這兩個 section 的同一條規則寫在
    `main`**（`with_defaults` 那一段）: 非載體檔的那兩個 section 被本函式跳過
    之後，由 main 決定它是「`_` 前綴 ⇒ exporter 會併進全域 ⇒ incomplete 具名」
    還是「租戶檔 ⇒ exporter 會丟棄 ⇒ 只警告」。四輪 F-04 的洞就長在這兩半
    中間: 本函式跳過了 `optional_overrides`，而 main 那一半當時只看
    `defaults`，於是它兩邊都沒人管。改動任一半都要看另一半。
    * `designed=False` —— Step 1／2 應該清掉、卻還在的東西。這才是「寫入
      沒有發生」。
    """
    out = []
    pattern_keys = metric_pattern_keys(metric_key)
    for f in findings:
        name = f["filename"]
        for section, key, val in f["occurrences"]:
            designed = False
            if section in ("defaults", "optional_overrides"):
                if not is_defaults_name(name):
                    continue
                if predict and key in pattern_keys:
                    continue
            else:
                # tenants.*：射程的定義在 `remove_from_tenants` —— 它只寫
                # 平面目錄下非 `_` 前綴的檔（`.` 前綴的檔 `scan_for_metric`
                # 根本不會列，所以這裡不必再判一次）。predict 時扣掉它會刪的
                # 那些；`_` 前綴的則兩種模式都留下，標成 designed。
                if name.startswith("_"):
                    designed = True
                elif predict:
                    continue
            out.append((name, section, key, val, designed))
    return out


def scan_for_metric(metric_key, config_dir):
    """掃描 conf.d/ 中所有引用指定 metric 的檔案。

    回傳: list of {filename, path, section, occurrences}
    """
    findings = []
    # The defaults plane is exact-name only: dimensional keys are not in
    # `defaults:` (resolve.go), so the closed half of the predicate is all
    # this half needs. The tenants plane below adds the dimensional shape.
    pattern_keys = metric_pattern_keys(metric_key)

    config_base = Path(config_dir)
    # #1339: flat by design here — but a hierarchical conf.d must not
    # look like an empty one. Name the files this scan cannot see.
    warn_nested(config_base, tool="deprecate_rule")
    entries = sorted(config_base.iterdir())
    # #1607: the `is_file()` filter below is a THIRD axis (neither the
    # extension axis of #1588 nor the recursion axis left alone above), and
    # it was silent. A directory named `notes.yaml/` or a broken symlink is
    # something the operator called configuration — reporting "✅ 未發現任何
    # 引用" for a tree containing one is the same false all-clear this tool
    # already gave on `.YAML`.
    #
    # ⛔ The naming is in `main`, NOT here. This function runs once PER
    # METRIC and one invocation takes several: measured, `deprecate_rule a b
    # c` printed the same two warnings three times. `warn_nested` above gets
    # away with sitting here only because it dedups per directory per
    # process internally.
    for entry in (
        p for p in entries
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if filename.startswith('.'):
            continue

        data = load_yaml_file(path)
        if data is None:
            continue

        occurrences = []

        # Check defaults section (`defaults:` with no value is None, not {})
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, dict):
            defaults = {}
        for pk in pattern_keys:
            if pk in defaults:
                occurrences.append(("defaults", pk, defaults[pk]))

        # #1787 三輪 F-02: the DECLARED tier. `optional_overrides:` is a list
        # of key NAMES the platform recognises but asserts no value for
        # (`types.go` OptionalOverrides, `resolveDeclaredRows`). A key that
        # leaves `defaults:` but stays here is still platform-recognised —
        # `ValidateTenantKeys` admits it and a tenant that writes it gets a
        # row. Deprecating the value while leaving the declaration behind is
        # not a deprecation; it is a metric only tenants can turn on.
        declared = data.get("optional_overrides") or []
        if isinstance(declared, list):
            for pk in pattern_keys:
                if pk in declared:
                    occurrences.append(
                        ("optional_overrides", pk, "（宣告，無值）"))

        # Check tenants section — exact names AND the dimensional shape
        # `metric{label="value"}`, both from the one shared predicate
        # (#1787 二輪 F-01: this used to be a substring test, which reported
        # a DIFFERENT metric's key as an occurrence of this one).
        tenants = data.get("tenants", {})
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            for key, val in tenant_config.items():
                if tenant_key_belongs_to_metric(key, metric_key):
                    occurrences.append((f"tenants.{tenant_name}", key, val))

        if occurrences:
            findings.append({
                "filename": filename,
                "path": path,
                "occurrences": occurrences,
            })

    return findings


def defaults_carriers(config_dir):
    """列出 conf.d 內所有 defaults 載體（`_defaults.yaml`／`.yml`，任意大小寫）。

    #1609: the exporter merges BOTH spellings into the defaults chain
    (`config_hierarchy.go:216`), so a write that touches only the one
    `resolve_defaults_file` picks leaves the threshold live in the other.
    The scan (`scan_for_metric`) already sees every carrier; the writer has
    to enumerate the same set or its report contradicts its action.

    Flat read, like every other scan in this tool — hence `warn_nested`
    (it dedups per directory per process, so this adds no second line).
    `defaults_files_in` takes already-listed names on purpose (see its
    docstring); listing here and filtering on `is_file()` keeps the set
    identical to what the scan could read. Sorted, so a directory carrying
    two spellings is processed in a deterministic order.
    """
    base = Path(config_dir)
    warn_nested(base, tool="deprecate_rule")
    entries = sorted(base.iterdir())
    return defaults_files_in(base, [e.name for e in entries if e.is_file()])


def remove_from_defaults(metric_key, config_dir, execute=False, *,
                         defaults_path=None):
    """從單一 defaults 載體移除該 metric 的相關 key。

    Single-carrier entry point, kept for callers that unpack `ok, msg`.
    Without `defaults_path` it writes the one carrier
    `resolve_defaults_file` picks — which is exactly the #1609 blind spot,
    so the CLI flow goes through `remove_from_all_defaults` instead.
    """
    if defaults_path is None:
        defaults_path = resolve_defaults_file(Path(config_dir))
    ok, msg, _removed = _remove_in_carrier(metric_key, Path(defaults_path),
                                           execute)
    return ok, msg


def remove_from_all_defaults(metric_key, config_dir, execute=False, *,
                            blocked=frozenset()):
    """對每個 defaults 載體執行 `_remove_in_carrier`。

    Returns one `(path, ok, msg, removed)` per carrier from
    `defaults_carriers`. With NO carrier present it returns the single
    canonical `resolve_defaults_file` entry (`_defaults.yaml 不存在`), so a
    tree without defaults reports exactly what it did before #1609.

    ⛔ root 與子樹的 defaults 載體都是「刪 key」，不是「寫 disable」(#1787)，
    但兩個平面的理由不同 —— root／平面是型別（見 `non_numeric_defaults`），
    子樹與租戶是 `resolve.go` 的 unknown-key blocking error（下架後仍持有該
    key 的那一層會被 tenant-api 的 `gitops/writer.go` 整份拒寫；main 在
    Step 1 之後把這件事印給 operator 看）。而 `disable` 本來就是租戶
    opt-out，不是下架 —— 這也是 ADR-017 的平面分界。

    ⚠️ 本函式與整支工具一樣是**平面**讀取（只看 `config_dir` 這一層）。子樹
    載體要用 `--config-dir` 指到那一層才會被處理；`warn_nested` 會把這一層看
    不見的目錄具名列出來。
    """
    base = Path(config_dir)
    carriers = defaults_carriers(base)
    if not carriers:
        carriers = [resolve_defaults_file(base)]
    # #1787 四輪 F-06: `blocked` 的載體本輪只預覽、不寫。寫回是整份
    # `safe_dump`，所以碰一個帶 `1:30`（yaml.v3 讀成字串、PyYAML 讀成 90）的
    # 檔會把鄰居的**語意**改掉。那種檔要先讓人修，不是順手重新序列化。
    return [(p, *_remove_in_carrier(metric_key, p,
                                    execute and p.name not in blocked))
            for p in carriers]


def _remove_in_carrier(metric_key, defaults_path, execute=False):
    """從指定的 defaults 載體移除 metric 的所有相關 key。

    回傳 `(ok, msg, removed)`，`removed` 為 `[(平面, key, 原值), ...]`，平面是
    `"defaults"` 或 `"optional_overrides"`（預覽模式下是「將移除」的那一組，
    內容相同）。

    要刪的 key 集合來自 `metric_pattern_keys` —— `scan_for_metric` 用的同一
    支函式，所以掃描報出來的每一個引用，這裡都會被刪掉；不會再有「只寫 BASE
    key、`<m>_critical` 留在檔裡」的落差 (#1787)。**一個載體有兩個平面**:
    `defaults:`（有值）與 `optional_overrides:`（只有名字），兩個都要清 ——
    只清前者的話，平台仍然認得這個 key，租戶寫上去就有 row（三輪 F-02）。

    ⛔ 沒有任何相關 key 的載體是 **no-op**，不是「補一行」。舊行為在載體本來
    就沒有這個 metric 時仍然新增 `<m>: disable`，把一個好檔寫壞（值型別見
    `remove_from_all_defaults` 的理由 1）。這裡具名回報並且**一個位元組都不
    寫**，測試用 `cmp` 釘住。
    """
    defaults_path = str(defaults_path)
    name = Path(defaults_path).name
    if not Path(defaults_path).exists():
        return False, f"{name} 不存在", []

    data = load_yaml_file(defaults_path)
    if data is None:
        return False, f"無法讀取 {name}", []

    # `defaults:` with no value parses to None — an empty block, written
    # like one. Any OTHER non-mapping (list / scalar) is a broken file the
    # exporter cannot decode either; replacing it with a mapping would
    # silently discard its content (re-review, #1609) → refuse, by name.
    defaults = data.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        return False, (f"defaults 不是 mapping（{type(defaults).__name__}），"
                       f"本工具不改寫，請先修檔"), []

    pattern_keys = metric_pattern_keys(metric_key)
    # #1787 三輪 F-02: `optional_overrides:` 是同一個載體的第二個平面 ——
    # 一個離開 `defaults:` 卻留在這裡的 key，平台仍然「認得」它。
    declared = data.get("optional_overrides")
    declared_hit = ([pk for pk in pattern_keys if pk in declared]
                    if isinstance(declared, list) else [])

    removed = [("defaults", pk, defaults[pk]) for pk in pattern_keys
               if pk in defaults]
    removed += [("optional_overrides", pk, "（宣告，無值）")
                for pk in declared_hit]

    if not removed:
        # ⛔ 具名的 no-op：不是靜默略過，也不是新增一行。
        return True, f"{name} 沒有 {metric_key} 相關 key，略過", []

    if not execute:
        return True, f"將從 {name} 移除 {len(removed)} 個 key", removed

    for where, pk, _val in removed:
        if where == "defaults":
            del defaults[pk]
    # #1787 四輪 F-10: 只有 `optional_overrides:` 命中的載體本來就沒有
    # `defaults:`，別給它長一個空的出來 —— 與下面清空的 list 整個拿掉對稱。
    if defaults or "defaults" in data:
        data["defaults"] = defaults
    if declared_hit:
        kept = [k for k in declared if k not in declared_hit]
        # 清空的 list 整個拿掉，而不是留一個 `optional_overrides: []`——
        # `init_project` 對空 list 的既有立場就是不寫（「空 list 讀起來像
        # 意外，Go loader 對缺席與空的處理相同」）。
        if kept:
            data["optional_overrides"] = kept
        else:
            data.pop("optional_overrides", None)

    # Read original file to preserve header comment
    header = ""
    with open(defaults_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('#'):
                header += line
            else:
                break

    save_yaml_file(defaults_path, data, header)
    return True, f"已從 {name} 移除 {len(removed)} 個 key", removed


def remove_from_tenants(metric_key, config_dir, execute=False):
    """從所有 tenant 設定中移除殘留的 metric key。"""
    removed = []

    config_base = Path(config_dir)
    # #1339: second scan site — the guard must live where the scan does,
    # otherwise a hierarchical conf.d is silently empty on THIS path.
    warn_nested(config_base, tool="deprecate_rule")
    # #1607: same `is_file()` third axis as the reference scan, deliberately
    # NOT re-reported. `scan_for_metric` walks this identical directory and
    # always runs first in the CLI flow, so naming the same dropped entries
    # again would print each one twice per metric — and this tool is run
    # across several metrics in one invocation.
    for entry in sorted(
        p for p in config_base.iterdir()
        if p.is_file() and has_yaml_extension(p.name)
    ):
        filename = entry.name
        path = str(entry)
        if filename.startswith('_') or filename.startswith('.'):
            continue  # Skip _defaults.yaml

        data = load_yaml_file(path)
        if data is None:
            continue

        tenants = data.get("tenants", {})
        modified = False
        for tenant_name, tenant_config in tenants.items():
            if not isinstance(tenant_config, dict):
                continue
            # #1787 二輪 F-01: the same predicate the scan uses. The old
            # form here (`metric_key in key and '{' in key`) would delete
            # `container_cpu_throttle{pod="x"}` while deprecating
            # `container_cpu` — another metric's data.
            keys_to_remove = [key for key in tenant_config
                              if tenant_key_belongs_to_metric(key, metric_key)]
            for key in keys_to_remove:
                val = tenant_config[key]
                removed.append((filename, tenant_name, key, val))
                if execute:
                    del tenant_config[key]
                    modified = True

        if modified and execute:
            header = ""
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('#'):
                        header += line
                    else:
                        break
            save_yaml_file(path, data, header)

    return removed


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
                             "一層子樹載體。root 會對 defaults 的值做型別"
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

    # #1607: named ONCE per invocation, before the per-metric loop — see
    # `scan_for_metric`, which is where the entries are actually dropped and
    # which runs once per metric.
    #
    # ⛔ `warn_nested` belongs HERE and not only in the scanning functions:
    # this line is itself a FLAT enumeration of the conf.d, and
    # `test_confd_enumeration_contract` reddened for exactly that — the guard
    # must live in the same scope as the scan, because "the import is in the
    # file" does nothing for the code path that gives the wrong answer. It
    # dedups per directory per process, so `scan_for_metric`'s call below
    # stays silent rather than repeating this one.
    #
    # ⛔ NO `try/except OSError` here, deliberately. The first version had one
    # and it was false robustness: `scan_for_metric` lists the same directory
    # two lines later with no guard of its own (and did so before #1607), so
    # swallowing the error here bought nothing except the impression that an
    # unlistable conf.d is handled. Measured — injecting EIO on this directory
    # made the run die inside `scan_for_metric` anyway, with the guard in
    # place. One failure mode, reported once, at the place that owns it.
    _base = Path(args.config_dir)
    warn_nested(_base, tool="deprecate_rule")
    _entries = sorted(_base.iterdir())
    for bad in unusable_config_entries(_entries):
        print(f"  ⚠️  略過 {safe_label(bad.name)}——{unusable_reason(bad)}",
              file=sys.stderr)

    # #1787 四輪 F-07: 本工具的租戶清理射程是「平面目錄下非 `_` 前綴的檔」
    # (`remove_from_tenants`)。射程外**而且可能持有租戶 key** 的載體有兩類，
    # 在這裡列一次（`_entries` 是同一次列舉，不另外走一趟目錄）。
    _bad_names = {b.name for b in unusable_config_entries(_entries)}
    _unreachable = []
    for _e in _entries:
        if _e.name in _bad_names:
            continue  # 上面已經按名字報過，不要說兩次
        if _e.is_dir():
            _unreachable.append(f"{_e.name}/（子目錄，本工具不遞迴）")
        elif (_e.is_file() and has_yaml_extension(_e.name)
              and _e.name.startswith("_") and not is_defaults_name(_e.name)):
            _d = load_yaml_file(str(_e))
            if isinstance(_d, dict) and _d.get("tenants"):
                _unreachable.append(
                    f"{_e.name}（`_` 前綴檔的 tenants: 區塊，本工具不寫）")
    # ⛔ 印在 per-metric 迴圈**之前**、一次。與 #1607 對 `unusable_config_entries`
    # 的處置同一條理由: 這是目錄的性質，不是某個 metric 的性質，而
    # `deprecate_rule a b c` 會把迴圈內的每一行印三次。
    if _unreachable:
        print("\n  ⚠️  下架後，這些本工具不寫入的檔若仍持有該 metric 的 key，"
              "tenant-api 會拒寫**整份檔**"
              "（resolve.go:1383 unknown key → gitops/writer.go）：")
        for _label in _unreachable:
            print(f"     • {safe_label(_label)}")

    # #1609: (metric, carrier, reason) for every defaults carrier the scan
    # listed but Step 1 did not write, plus every carrier Step 1 itself
    # reported as failed. Non-empty ⇒ the tool must NOT say 下架完成.
    incomplete = []
    # #1787 四輪 F-06: 載體體檢跑在**任何寫入之前**。寫回是整份
    # `safe_dump`，所以一個 `1:30`（yaml.v3 讀成字串、PyYAML 讀成 90）的鄰居
    # 值會在「順手重新序列化」中被改掉語意 —— 那必須先擋下來，不是事後才報。
    #
    # ⛔ 只在 `--plane root` 跑（四輪 F-02，PM 拍板）。這個問題的真值是「這個
    # 目錄在 exporter 的 `-config-dir` **之下的哪一層**」，而本工具拿不到那個
    # 事實 —— 三輪用祖先目錄推導，兩個方向都有反例:
    # `tests/golden/fixtures/mixed-mode/conf.d/db` 是沒有祖先載體的真子樹
    # （會被判成 root ⇒ 假 rc 1），而祖先放一個無關的 `_defaults.yaml` 或一條
    # symlink 就能讓真 root 被判成子樹 ⇒ **體檢靜默關閉**。推不出來的東西就
    # 用旗標問，預設 fail-closed（root，體檢照跑）。
    carrier_health = {}
    blocked_carriers = set()
    if args.plane == "root":
        _planned = {k for m in args.metrics for k in metric_pattern_keys(m)}
        for carrier in defaults_carriers(Path(args.config_dir)):
            residue = [(k, raw, kind)
                       for k, raw, kind in carrier_defaults_health(carrier)
                       if k not in _planned]
            if residue:
                carrier_health[carrier.name] = residue
                if any(kind == UNPARSEABLE for _, _, kind in residue):
                    blocked_carriers.add(carrier.name)
    elif defaults_carriers(Path(args.config_dir)):
        print("  ℹ️  子樹載體不做型別體檢：子樹平面的字串值合法"
              "（`computeEffectiveConfig` 走 map[string]any），"
              "root 的 map[string]float64 規則不適用於這一層。")
    # #1787 二輪 F-01/F-03: the completeness verdict is taken ONCE, after the
    # loop. What actually needs that position is the CARRIER HEALTH CHECK: it
    # is invocation-wide, so inside the loop `deprecate a b` would judge the
    # tree over a residue that `b`'s own turn is about to remove.
    #
    # ⚠️ The per-metric RESCAN does not need it — with an exact predicate,
    # metric `a`'s rescan cannot see metric `b`'s keys, so in-loop and
    # post-loop are equivalent for that half (measured: reverting only that
    # move leaves the suite green). It sits here for one verdict site, not
    # for detection. The loop only collects.
    findings_by_metric = {}
    # carrier filename → the keys THIS run removes from it (all metrics).
    # Preview needs it to subtract what it is about to delete; execute keeps
    # it so both modes take the same subtraction.
    planned_removals = {}

    for metric in args.metrics:
        print(f"\n{'─'*40}")
        print(f"📌 Processing: {metric}")
        print(f"{'─'*40}\n")

        # Step 1: 掃描
        findings = scan_for_metric(metric, args.config_dir)
        findings_by_metric[metric] = findings
        if findings:
            print(f"  📂 發現 {sum(len(f['occurrences']) for f in findings)} 處引用:")
            for f in findings:
                for section, key, val in f["occurrences"]:
                    print(f"     • {safe_label(f['filename'])} → "
                          f"[{safe_label(section)}] {safe_label(key)}: {safe_label(val)}")
        else:
            print(f"  ✅ 未發現任何引用")

        # Step 1 (as printed): 從每個 defaults 載體移除相關 key
        # (#1609: ALL carriers,
        # not the one `resolve_defaults_file` picks — the scan above lists
        # every carrier and the exporter reads every carrier).
        results = remove_from_all_defaults(metric, args.config_dir,
                                           execute=args.execute,
                                           blocked=blocked_carriers)
        for carrier, ok, msg, removed in results:
            print(f"\n  Step 1: {safe_label(carrier.name)}")
            icon = "✅" if ok else "❌"
            print(f"  {icon} {safe_label(msg)}")
            if args.execute and carrier.name in blocked_carriers:
                print(f"  ⛔ 本輪不寫入 {safe_label(carrier.name)}："
                      f"載體還有 exporter 讀不成數字的值，整份重新序列化會連"
                      f"帶改掉它們的拼法甚至語意（見結尾的載體體檢）")
            # 逐 key 具名 (#1787): 「移除 2 個 key」不告訴 operator 是哪兩個，
            # 而 `<m>` 與 `<m>_critical` 的下場是他唯一需要複查的東西。
            action = "已移除" if args.execute else "將移除"
            for where, key, val in removed:
                plane = "" if where == "defaults" else f"{where} 的 "
                print(f"     🗑️  {action} {plane}{safe_label(key)}"
                      f"（原值: {safe_label(val)}）")
            # 只有 `defaults:` 那一半餵載體體檢——體檢看的就是那個 mapping。
            planned_removals.setdefault(carrier.name, set()).update(
                key for where, key, _ in removed if where == "defaults")

        # "found N / changed N" must agree before the summary may say 完成.
        # `found` is what the scan ITSELF reported as a defaults carrier —
        # the same output the operator just read — so a carrier the scan
        # named and Step 1 skipped is named again here, with its reason.
        # #1787 四輪 F-04: `optional_overrides` 與 `defaults` 一起算。
        # `flat_scanner.go:335` 的 boundary rule 對 `_` 開頭的檔**不剝**
        # OptionalOverrides，所以一個 `_shared.yaml` 的宣告層會被併進全域 ——
        # 掃描列得出來、Step 1 依設計不寫它，中間那個洞讓它印了「下架完成」。
        with_defaults = {f["filename"] for f in findings
                         if any(sec in ("defaults", "optional_overrides")
                                for sec, _, _ in f["occurrences"])}
        # Only the exact-name carriers are the writer's; the rest follow the
        # exporter's own boundary rule (flat_scanner.go applyBoundaryRules):
        # a root-level `_`-prefixed file's `defaults:` IS merged into the
        # global defaults — on both planes, since the hierarchical plane
        # overlays the same flat merge (config_hierarchy.go, "merged at the
        # top level via scanDirFileHashes") — but this tool must not write
        # it (is_defaults_name) → incomplete, by name; a tenant file's
        # `defaults:` block is dropped with a WARN by the exporter → named
        # here, not counted (blind review, #1609).
        found = {n for n in with_defaults if is_defaults_name(n)}
        done = {p.name for p, ok, _, _ in results if ok}
        reasons = {p.name: msg for p, ok, msg, _ in results if not ok}
        for name in sorted(found - done):
            incomplete.append((metric, name, reasons.get(name, "未寫入")))
        for p, ok, msg, _ in results:
            if not ok and p.name not in found:
                incomplete.append((metric, p.name, msg))
        for name in sorted(with_defaults - found):
            if name.startswith("_"):
                incomplete.append((metric, name,
                                   "非 defaults 載體，本工具不寫入；exporter 會把 root 層任何 `_` 開頭檔的 "
                                   "defaults／optional_overrides 併進全域"
                                   "（flat／hierarchical 皆然），需手動處理"))
            else:
                print(f"  ⚠️  {safe_label(name)} 的 defaults 區塊 exporter 會丟棄"
                      f"（只認 _defaults.yaml／.yml），本工具不寫入")

        # Step 2 (as printed): 從 tenant configs 移除
        print(f"\n  Step 2: Tenant configs")
        removed = remove_from_tenants(metric, args.config_dir, execute=args.execute)
        if removed:
            for filename, tenant, key, val in removed:
                action = "已移除" if args.execute else "將移除"
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

    # #1787: 完成度不變式是 **KEY 級**，不是檔案級。「每個載體都寫到了」不等於
    # 「這個 metric 不見了」——Step 1／2 各自依設計有不碰的東西（`_` 開頭的租戶
    # 載體、子目錄裡的檔），而「✅ 下架完成」蓋在一個還活著的 key 上，正是這張
    # 票的形狀。判定用的是 operator 剛剛讀過的**同一支** `scan_for_metric`，
    # 過濾則走 `unclearable_occurrences`，兩種模式同一條路徑 (二輪 F-03)。
    #
    # ⚠️ 這一段在迴圈外是為了**只有一個判定點**，不是為了偵測力：述詞精確之後
    # a 的重掃看不見 b 的 key，迴圈內外等價（二輪 F-01／三輪 F-08）。真正需要
    # 這個位置的是下面的載體體檢——它是一次調用的性質。
    for metric in args.metrics:
        if args.execute:
            # 真實重掃 —— 寫入是否真的發生，只有檔案說了算。
            leftovers = unclearable_occurrences(
                metric, scan_for_metric(metric, args.config_dir), predict=False)
            label = "重掃仍有引用"
        else:
            # 預測殘留 = 掃描結果 − 本輪計畫刪除的 key。
            leftovers = unclearable_occurrences(
                metric, findings_by_metric[metric], predict=True)
            label = "本工具不會清掉"
        for name, section, key, val, designed in leftovers:
            if designed:
                # #1787 三輪 F-04: 依設計不歸本工具的殘留，兩種模式同一句。
                # 「重掃仍有引用」讀起來像寫入失敗，會把人送去查一個沒發生的
                # bug；比照 `_` 前綴非載體那條，講的是「需手動處理」。
                incomplete.append((
                    metric, name,
                    f"本工具依設計不寫任何 `_` 前綴檔的 `tenants:` 區塊，"
                    f"請手動移除："
                    f"[{section}] {key}: {val}"))
            else:
                incomplete.append((metric, name,
                                   f"{label}：[{section}] {key}: {val}"))

    # #1787 二輪 F-02 / 四輪 F-06: 載體體檢的**結果**在這裡變成判定。體檢本身
    # 已經在任何寫入之前跑完（`carrier_health`），這裡只是把它接到同一個
    # incomplete 出口，讓「下架完成」不會蓋在一份 exporter 讀不進去的載體上。
    for carrier_name, residue in carrier_health.items():
        for key, raw, kind in residue:
            if kind == DECODES_TO_ZERO:
                reason = (f"`defaults:` 的 {key} 是空值（`{key}:`）——"
                          f"exporter 會把它解成 0，每個租戶因此多一條 0 閾值"
                          f"（等於永遠觸發），不是「沒有這個 key」；請刪掉這"
                          f"一行或補一個數字")
            else:
                reason = (f"`defaults:` 的 {key} 的值 {raw} 不是 YAML 1.2 的"
                          f"數字——exporter 的 defaults 型別是 "
                          f"map[string]float64，這份載體會被整份丟棄，下架不"
                          f"會生效；請先修掉它")
            reason += ("。若這個 --config-dir 其實是子樹載體（exporter 的 "
                       "-config-dir 之下的目錄），請加 `--plane subtree`")
            incomplete.append(("載體體檢", carrier_name, reason))

    # 總結
    print(f"\n{'='*60}")
    if incomplete:
        # #1609: a preview that cannot complete must not read as clean
        # either, so this branch runs in both modes.
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
