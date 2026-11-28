#!/usr/bin/env python3
"""migrate_rule.py — 把一個租戶的傳統 Prometheus 規則原樣遷移成 migrated 規則（#4226）。

每條規則的 expr、for、labels、annotations、alertname 都原樣保留，只做三件事：

1. 租戶隔離：每個 selector 以 Prometheus 自己的語法樹注入租戶 matcher（``da-read
   promql-pin``，同 prom-label-proxy／Prometheus Operator ``enforcedNamespaceLabel``
   的做法）。怎麼注入由 ``_lib_metric_sources.pin_form`` 決定：序列帶 tenant 的指標
   加 ``tenant="<租戶>"``；kube-state-metrics 有 namespace 的物件與 cAdvisor／kubelet 的基礎設施指標加
   ``namespace="<租戶>", tenant=""``；其餘（叢集層級物件、平台自己的序列、每個
   抓取目標都有的序列、告警自己的序列）沒有一種注入只選到一個租戶，結束碼 2。
2. 標記：labels 加 ``tenant: <租戶>``、``component: migrated``（靜態值，切換前後
   不變）；annotations 加 ``owner``、``expiry``（放 label 的話，續期改 expiry 會讓
   ``for:`` 重新計時）。
3. 到期伴隨告警：``vector(time()) > <expiry − 14 天的 Unix 秒數>``，不依賴任何
   指標資料。

同一個來源檔裡被遷移的告警讀到的 recording rule 一起遷移（加 ``tenant``），並改名為
``migrated:<租戶>:<原名>``（租戶的 ``-`` 寫成 ``_``），讀它的 migrated 規則一起改：
migrated 檔與 legacy 規則載入同一台 Prometheus，同名的話 legacy 告警會讀到 migrated
record 的序列（多出一條不走影子路由的告警），或兩條規則寫同一組序列。讀到檔裡沒有
的 record，結束碼 2。

``component: migrated`` 蓋掉序列自己的 ``component``：運算式明寫保留它的（matcher、
``by``／``on``／``group_left``、``label_replace`` 等寫入）結束碼 2；序列從抓取帶來的
看不出來，報告提醒（多條只差 component 的序列會變成同一組 label，Prometheus 不評估）。字典（metric-dictionary.yaml）命中的告警平台 rule
pack 已涵蓋，只在報告裡點名改用黃金標準，不產生 migrated 規則。

任何一條規則被拒，就逐條列出原因、結束碼 2、一個檔都不寫。寫之前，migrate 對
自己的輸出跑 ``lint_custom_rules``（租戶隔離、owner／expiry 治理；``--policy``
可換政策），有 ERROR 也一樣結束碼 2、不寫。

輸出（``-o``，預設 migration_output）：

* ``.migration/migrated/<租戶>/rules.yml`` — migrated 規則檔。由客戶抓取原始指標
  的那台 Prometheus 載入（單一 Prometheus，或 federation 架構下的 edge：只帶
  namespace 的 kube-state-metrics 序列不會被拉到中央）。``rule_files`` 要讀得到
  ``*.yml``。
* ``.migration/mapping/<租戶>.yaml`` — 遷移對照（validate-migration 讀，#4227）：
  每條規則的 selector 注入前與注入後，以及 (租戶, alertname)。
* ``migration-report-<租戶>.txt`` — 遷移報告。

點開頭的 ``.migration/`` 是 conf.d 的每個讀取端都整棵略過的目錄（#3997），所以
``-o`` 落在一棵 conf.d 裡也不會被當成設定檔讀；既有的 ``-o`` 先過共用的 conf.d
拒絕（#3206、#4014）。

用法:
  python3 migrate_rule.py <rules.yml> --tenant db-a --owner team-db --expiry 2027-03-31
  python3 migrate_rule.py <rules.yml> --tenant db-a --owner team-db --expiry 2027-03-31 --dry-run
"""

import sys
import re
import math
import os
import json
import argparse
import datetime
import tempfile
from pathlib import Path

import yaml

_THIS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(_THIS_DIR))  # Docker flat layout
sys.path.insert(0, str(_THIS_DIR.parent))  # Repo subdir layout
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_python import (  # noqa: E402
    ensure_dir_or_die, output_dir_type, write_operator_output_or_die,
)
from _lib_output import OutputWriteError, check_writable_artifact  # noqa: E402  (#4250)
from _lib_exitcodes import die_caller_error  # noqa: E402
from _lib_label import safe_label  # noqa: E402
from _lib_io import exit_on_yaml_file_error, find_metric_dictionary, load_yaml_file  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
import _lib_input  # noqa: E402  (#4040: the rule file is read once, here)
import _lib_metric_sources  # noqa: E402  (#4226: how each metric is pinned)
import _lib_write_check  # noqa: E402  (the shared refusal of an existing -o, #3206)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import is_valid_tenant_id, tenant_id_rule  # noqa: E402  (ADR-035)
# ADR-006: the one spelling of the cluster label (shipped flat in da-tools).
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402

# #1641: every path this tool writes descends from -o/--output-dir, so every
# writer names that flag; an unusable path is rc=2 + one line.
_OUTPUT_FLAG = "-o/--output-dir"

#: 與 check_cluster_identity.IDENTITY_LABELS 同一組（由測試釘住；da-tools
#: 映像不帶 lint）：`by(...)`／`on(...)` 點名其中任一個，就是「逐這個東西」，
#: 而它在每個叢集各有一份。
_IDENTITY_LABELS = frozenset({"tenant", "instance", "pod", "namespace", "node"})

# ============================================================
# PromQL：一律讀 Prometheus 的語法樹（#4215）
# ============================================================

def read_trees(exprs):
    """每條運算式的語法樹（``_lib_consumer_read.promql_shape_each``），整批讀
    一次；Prometheus 的 parser 拒收的那條是它的 ``InputRefused``。"""
    return _lib_consumer_read.promql_shape_each(list(exprs))


def _tree_of(expr_or_tree):
    """*expr_or_tree* 的語法樹：已是樹就原樣，是字串就讀一次（拒收即 raise，
    點名該運算式）。"""
    if isinstance(expr_or_tree, dict):
        return expr_or_tree
    return _lib_consumer_read.promql_shape_each([expr_or_tree], raise_refused=True)[0]


def _node_text(src, node):
    """*node* 在原式 *src*（str）中的原文：offset 是 UTF-8 byte。"""
    return src.encode("utf-8")[node["start"]:node["end"]].decode("utf-8")


def _selectors(tree):
    return [n for n in _lib_consumer_read.shape_nodes(tree) if n["kind"] == "selector"]


def _selector_metric(node):
    """selector 以 ``__name__`` 等值 matcher 釘住的指標名，或 None。"""
    for m in node["matchers"]:
        if m["name"] == "__name__" and m["type"] == "=":
            return m["value"]
    return None


def _calls(tree):
    return {n["name"] for n in _lib_consumer_read.shape_nodes(tree) if n["kind"] == "call"}


def groupings_without_cluster(tree):
    """原式子自己寫的、點名身分 label 卻沒有叢集 label 的分組與配對。

    *tree* 是語法樹（或其子樹）。回傳 list（依出現順序、不重複），每項是
    `on(instance)`、`by(pod, namespace)` 這樣的文字：vector matching 的
    `on(...)` 與聚合的 `by(...)`，label 清單含 tenant／instance／pod／
    namespace／node 任一個而不含 `da_cluster`；以及點名 `da_cluster` 的
    `without(...)`／`ignoring(...)`——那同樣把叢集丟掉。沒點名身分 label 的
    （`by(le)`、`on()`）、沒點名叢集的 `without(...)`／`ignoring(...)` 不算。讀的是
    Prometheus 的語法樹，所以 `ON(...)`、`BY (...)`、`by("instance")` 與字串
    常值裡的文字都不會看錯。

    ⛔ 只偵測、不改寫（#1200）：要不要補 `da_cluster` 取決於配對的兩邊是否都帶
    這個 label——`x / on(tenant) group_left quota`、右邊是租戶一份的序列時，
    補上去就什麼都配不到，告警悄悄消失——而這從語法樹看不出來。
    """
    found = []

    def note(keyword, labels):
        labels = list(labels)
        keeps = keyword in ("by", "on")   # the list is what survives
        if (_IDENTITY_LABELS & set(labels) and CLUSTER_IDENTITY_LABEL not in labels
                if keeps else CLUSTER_IDENTITY_LABEL in labels):
            # a quoted label name can hold anything: written so that it stays
            # on one line of a comment
            shown = [x if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", x)
                     else json.dumps(x, ensure_ascii=False) for x in labels]
            text = f"{keyword}({', '.join(shown)})"
            if text not in found:
                found.append(text)

    for node in _lib_consumer_read.shape_nodes(tree):
        if node["kind"] == "aggregate":
            note("without" if node["without"] else "by", node["labels"])
        elif node["kind"] == "binary" and node["matching"]:
            note("on" if node["matching"]["on"] else "ignoring", node["matching"]["labels"])
    return found


def cluster_grouping_note(groupings):
    """一行：原式子的這些分組／配對沒有叢集 label，要人看過（#1200）。"""
    return (f"原式子的 {'、'.join(groupings)} 不保留 {CLUSTER_IDENTITY_LABEL}：在持有多個叢集資料的 "
            f"Prometheus 上會配對或合併不同叢集的同名序列（on() 可能 many-to-many）。"
            f"兩邊都帶 {CLUSTER_IDENTITY_LABEL} 才補上，請人工確認；工具不改寫")


# ============================================================
# Metric Dictionary: 載入外部啟發式字典
# ============================================================

def load_metric_dictionary(script_dir=None):
    """載入 metric-dictionary.yaml 啟發式字典。

    沒給 script_dir 時，映像佈局（與工具同層）與 repo 佈局（scripts/tools/，
    工具在 ops/ 子目錄）都找；先前只找工具同層，repo 佈局下字典恆為空
    （issue 1513）。
    """
    if script_dir is None:
        dict_path = find_metric_dictionary(str(_THIS_DIR))
    else:
        dict_path = str(Path(script_dir) / "metric-dictionary.yaml")
    if dict_path and os.path.exists(dict_path):
        # #4064: the one reading of a YAML file (strict: a key given twice
        # is refused, YamlFileError naming the file), as analyze-gaps and
        # generate-tenant-mapping read the same dictionary.
        return load_yaml_file(dict_path, default={}) or {}
    return {}


# ============================================================
# Heuristics: 智能聚合猜測
# ============================================================

def guess_aggregation(base_key, expr_or_tree):
    """根據 metric 名稱和 PromQL 表達式（語法樹，或讀成語法樹的原文）智能猜測
    聚合模式。函式呼叫與除法從語法樹讀：label 值裡的 `rate(`、`/` 不算。

    回傳: (mode, reason) — mode 為 "sum" 或 "max"，reason 為推理說明。
    """
    tree = _tree_of(expr_or_tree)
    key_lower = base_key.lower()

    # Rule 1: rate() / increase() / irate() → sum (叢集總量)
    if _calls(tree) & {"rate", "increase", "irate"}:
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
    if any(n["kind"] == "binary" and n["op"] == "/"
           for n in _lib_consumer_read.shape_nodes(tree)):
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

# 語義不可轉換的 PromQL 函式：遷移政策，不是 PromQL 的函式清單。每個名字都必須是
# 部署的 Prometheus 認得的函式（tests/ops/test_migrate_ast.py 以 da-read
# promql-names 對帳）；它不認得的函式，規則在 rule loader 那關就被拒收（rc 2）。
SEMANTIC_BREAK_FUNCS = frozenset({
    'absent', 'absent_over_time', 'vector', 'scalar',
    'predict_linear', 'label_replace', 'label_join'
})

#: 衍生出的閾值 key 與 record 名稱用得上的指標名（Prometheus 的 legacy 命名）。
_LEGACY_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
#: 比較運算子：告警運算式的根節點是 `LHS <op> 數字` 才切得出門檻。
_COMPARISONS = frozenset({"==", "!=", ">", "<", ">=", "<="})
#: 維度提示不收的 label：scrape 身分與 Kubernetes 位置，不是業務維度。
_NON_DIMENSION_LABELS = frozenset({'job', 'instance', '__name__', 'namespace', 'pod', 'container'})


def extract_label_matchers(expr_or_tree):
    """每個帶名稱的 selector 以 `=` 釘住的業務維度 label。

    回傳: list of {"metric": str, "labels": dict}（沒有維度的 selector 不列）。
    """
    results = []
    for node in _selectors(_tree_of(expr_or_tree)):
        labels = {m["name"]: m["value"] for m in node["matchers"]
                  if m["type"] == "=" and m["name"] not in _NON_DIMENSION_LABELS}
        if labels:
            results.append({"metric": node["name"], "labels": labels})
    return results


def _plain_number(text):
    """門檻寫成閾值檔讀得懂的有限十進位數：PromQL 也接受 `0x1F`、`5m`、`Inf`、
    `NaN`、`1_000`，但閾值檔與 da-guard 不收，寫檔時才失敗會讓整份遷移沒有
    產出——它們判成無法解析。"""
    try:
        value = float(text)
    except ValueError:
        return False
    digits = text.lstrip("+-")
    # Prometheus reads an integer with a leading 0 as octal (`012` is 10);
    # float() reads it as decimal — a different threshold.
    octal = len(digits) > 1 and digits[0] == "0" and digits.isdigit()
    return math.isfinite(value) and "_" not in text and not octal


def parse_expr(expr_str, tree=None):
    """把告警運算式切成 LHS、比較運算子與門檻數字。

    *tree* 是 *expr_str* 的語法樹，或其中一個子樹（offset 都指向 *expr_str*）；
    省略時讀一次。根節點不是「不帶 bool 的比較、右邊是數字」、LHS 用到語義
    中斷的函式、或 Prometheus 拒收，都回 None（無法解析）。
    """
    if tree is None:
        (tree,) = read_trees([expr_str])
    if isinstance(tree, _lib_consumer_read.InputRefused):
        return None
    if not (tree["kind"] == "binary" and tree["op"] in _COMPARISONS
            and not tree["bool"] and tree["rhs"]["kind"] == "number"):
        return None
    lhs = tree["lhs"]
    if _calls(lhs) & SEMANTIC_BREAK_FUNCS:
        return None
    val = _node_text(expr_str, tree["rhs"])
    if not _plain_number(val):
        return None
    metrics = _lib_consumer_read.shape_metric_names(lhs)
    # The derived key and record names are built from the first metric: it must
    # be one a bare selector can spell (`{__name__="legacy metric"}` is not), and
    # there must be one — a shared `unknown_metric` key would merge unrelated
    # rules' thresholds and recordings.
    if not metrics or not _LEGACY_METRIC_NAME.fullmatch(metrics[0]):
        return None
    return {
        "lhs": _node_text(expr_str, lhs),
        "op": tree["op"],
        "val": val,
        "is_complex": not (lhs["kind"] == "selector" and not lhs["range"]),
        "base_key": metrics[0],
        "all_metrics": metrics,
        "expr": expr_str,
        "lhs_tree": lhs,
    }



def lookup_dictionary(metric_name, dictionary):
    """在啟發式字典中查找 metric，回傳匹配條目或 None。"""
    if not dictionary:
        return None
    return dictionary.get(metric_name)


# ============================================================
# 原樣遷移（#4226）
# ============================================================

from _grar_routes import MIGRATED_LABEL, MIGRATED_VALUE  # noqa: E402  (the one spelling)

TENANT_LABEL = _lib_metric_sources.TENANT_LABEL

#: 到期伴隨告警：expiry 前這麼多天起響。
EXPIRY_WARNING_DAYS = 14
EXPIRY_GROUP = "migrated-expiry"
EXPIRY_ALERT = "MigratedRulesExpiring"

#: 點開頭的草稿根（#3997：conf.d 的讀取端整棵略過）。不在
#: ``_lib_write_check.DRAFT_ROOTS``：那張表是寫 conf.d 草稿的工具的，migrate-rule
#: 自 #4226 起不寫 conf.d（寫的是規則檔，conf.d 的讀取端不讀，在這底下也不讀）。
DRAFT_ROOT = ".migration"
def report_rel(tenant):
    """遷移報告，相對於 -o：每個租戶一份（同一個 -o 可以放多個租戶的產物）。"""
    return f"migration-report-{tenant}.txt"


def migrated_record_name(tenant, record):
    """migrated recording rule 的名稱：``migrated:<租戶>:<原名>``。租戶 id（ADR-035：
    小寫、數字、``-``）的 ``-`` 寫成 ``_``——id 裡沒有 ``_``，所以一對一。"""
    return f"migrated:{tenant.replace('-', '_')}:{record}"
_EXPIRY_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def rules_rel(tenant):
    """migrated 規則檔，相對於 -o：``<草稿根>/migrated/<租戶>/rules.yml``——
    ``lint_custom_rules`` 掃 ``<草稿根>/migrated`` 時，目錄名就是租戶。"""
    return f"{DRAFT_ROOT}/migrated/{tenant}/rules.yml"


def mapping_rel(tenant):
    """遷移對照檔，相對於 -o（不在 migrated/ 之下：那裡的 YAML 都是規則檔）。"""
    return f"{DRAFT_ROOT}/mapping/{tenant}.yaml"


def expiry_date(text):
    """``--expiry``：YYYY-MM-DD、日曆上有的日子、在今天（UTC）之後，否則 None。"""
    if not isinstance(text, str) or not _EXPIRY_DATE.match(text):
        return None
    try:
        day = datetime.date.fromisoformat(text)
    except ValueError:
        return None
    return day if day > datetime.datetime.now(datetime.timezone.utc).date() else None


def expiry_alert(tenant, owner, expiry):
    """到期伴隨告警：expiry 前 ``EXPIRY_WARNING_DAYS`` 天（UTC 零時）起響。只讀
    ``time()``，不讀任何指標：沒有資料也不會誤報或漏報。"""
    warn_at = datetime.datetime.combine(
        expiry - datetime.timedelta(days=EXPIRY_WARNING_DAYS), datetime.time(),
        tzinfo=datetime.timezone.utc)
    return {
        "alert": EXPIRY_ALERT,
        "expr": f"vector(time()) > {int(warn_at.timestamp())}",
        "labels": {"severity": "warning", TENANT_LABEL: tenant, MIGRATED_LABEL: MIGRATED_VALUE},
        "annotations": {
            "summary": (f"Migrated rules of tenant {tenant} expire on {expiry.isoformat()}: "
                        "promote them to platform rules or renew the expiry"),
            "owner": owner, "expiry": expiry.isoformat(),
        },
    }


class Plan:
    """一次遷移的判讀結果：被拒的原因（有任何一條就什麼都不寫）、輸出的
    groups、對照、報告要點名的東西。"""

    def __init__(self):
        self.refusals = []        # "<group>/<rule>: <why>"
        self.groups = []          # 輸出的 rule groups（dict，原 group 的 key 與順序）
        self.mapping = []         # {"group", "alert"|"record", "selectors": [...]}
        self.migrated_alerts = []
        self.migrated_records = []
        self.golden = []          # (alert, metric, dictionary entry)
        self.unread_records = []  # 沒有被遷移的告警讀到的 record
        self.cluster_notes = []   # (rule, groupings)
        self.lint_warnings = []
        self.renamed = {}         # legacy record name -> migrated name


def _rule_name(rule):
    return rule.get("alert") or rule.get("record") or "<unnamed>"


def _selector_refusal(node, own_records):
    """一個 selector 為什麼沒有一種注入只選到一個租戶（或 None），及它的指標名
    與注入方式（"tenant"／"namespace"）。"""
    metric = _selector_metric(node)
    if metric is None:
        return ("it names no metric by equality: which series it reads, and so how they "
                "name the tenant, is not known"), None, None
    if metric in own_records:
        return None, metric, "tenant"
    if ":" in metric:
        return (f"it reads recording rule {metric}, which this file does not define: "
                "migrate the file that defines it together with this one"), metric, None
    form, why = _lib_metric_sources.pin_form(metric)
    if form is None:
        return f"{why} — a platform-level rule, not migrated per tenant", metric, None
    return None, metric, form


def _keeps_migrated_label(tree):
    """運算式明寫保留（或寫入）``MIGRATED_LABEL`` 的地方，依出現順序：它的值會
    被 migrated 的靜態 label 蓋掉。"""
    found = []
    for node in _lib_consumer_read.shape_nodes(tree):
        kind = node["kind"]
        if kind == "selector" and any(m["name"] == MIGRATED_LABEL for m in node["matchers"]):
            found.append(f"a {MIGRATED_LABEL} matcher")
        elif kind == "aggregate" and not node["without"] and MIGRATED_LABEL in node["labels"]:
            found.append(f"by({MIGRATED_LABEL})")
        elif kind == "aggregate" and node["op"] == "count_values" and \
                (node.get("param") or {}).get("value") == MIGRATED_LABEL:
            found.append(f'count_values("{MIGRATED_LABEL}")')
        elif kind == "binary" and node["matching"]:
            m = node["matching"]
            if (m["on"] and MIGRATED_LABEL in m["labels"]) or MIGRATED_LABEL in m["include"]:
                found.append(f"on/group_*({MIGRATED_LABEL})")
        elif kind == "call" and node["name"] in ("label_replace", "label_join") and \
                len(node["args"]) > 1 and (node["args"][1] or {}).get("value") == MIGRATED_LABEL:
            found.append(f'{node["name"]}(…, "{MIGRATED_LABEL}", …)')
    return list(dict.fromkeys(found))


#: A raw metric name no table knows, so label_flow takes its series to carry
#: tenant (``_matching_changes``).
_TENANT_CARRIER = "migrate_rule_tenant_carrier"


def _operand_reads(node, needed):
    """Whether the operand *node* reads one of the *needed* records."""
    return any(_selector_metric(n) in needed for n in _lib_consumer_read.shape_nodes(node)
               if n["kind"] == "selector")


def _matching_changes(chosen, groups, needed, tenant, *, flow=None):
    """Why a binary operation of a migrated rule would match differently (one line
    each). A migrated record carries ``tenant`` (its ``labels:``), which its
    legacy one may not: ``job:x:sum / sum(y)`` matched two results without
    tenant, and the migrated record's carries it while ``sum(y)`` does not —
    no pair, no alert. Each vector/vector operation with an operand reading a
    migrated record is judged twice by ``_lib_metric_sources.label_flow``
    (*flow*, tests inject): with the records as written and with ``tenant``
    added to the migrated ones. The match changes when the two operands agree
    on carrying tenant in one and not the other (every tenant value is this
    tenant's); an operand neither can tell (``unknown``) is refused too.
    ``on(…)`` without tenant, and ``ignoring(…)`` with it, do not look at it."""
    flow = flow or _lib_metric_sources.label_flow
    found = []   # (where, operation text, lhs text, rhs text)
    for group, rule, tree in chosen:
        expr = str(rule["expr"]).encode("utf-8")
        text = lambda n: expr[n["start"]:n["end"]].decode("utf-8", "replace")  # noqa: E731
        for node in _lib_consumer_read.shape_nodes(tree):
            m = node["kind"] == "binary" and node["matching"]
            if not m or (m["on"] and TENANT_LABEL not in m["labels"]) or \
                    (not m["on"] and TENANT_LABEL in m["labels"]):
                continue
            if _operand_reads(node["lhs"], needed) or _operand_reads(node["rhs"], needed):
                found.append((f"{group.get('name', '<unnamed>')}/{_rule_name(rule)}",
                              text(node), text(node["lhs"]), text(node["rhs"])))
    if not found:
        return []
    legacy, migrated = {}, {}
    for group in groups:
        for rule in group.get("rules") or []:
            if rule.get("record"):
                labels = {**(group.get("labels") or {}), **(rule.get("labels") or {})}
                d = {"expr": str(rule.get("expr", "")), "labels": labels}
                legacy.setdefault(rule["record"], []).append(d)
                # a migrated record carries tenant on every series (its
                # labels:); label_flow reads a static label as "unknown" (it
                # asks whether the value is a series'), so the record is told
                # as a raw metric every series of which carries it
                migrated.setdefault(rule["record"], []).append(
                    {"expr": _TENANT_CARRIER, "labels": {}} if rule["record"] in needed else d)
    operands = [e for _, _, lhs, rhs in found for e in (lhs, rhs)]
    before = [r["result"] for r in flow(TENANT_LABEL, operands, legacy)]
    after = [r["result"] for r in flow(TENANT_LABEL, operands, migrated)]
    out = []
    for i, (where, op, _, _) in enumerate(found):
        b, a = before[2 * i:2 * i + 2], after[2 * i:2 * i + 2]
        if "unknown" in b + a:
            out.append(f"{where}: {op}: whether its two sides carry {TENANT_LABEL} cannot be "
                       "told, so whether the migrated record's added tenant changes how they "
                       "match cannot either")
        elif (b[0] == b[1]) != (a[0] == a[1]):
            out.append(f"{where}: {op}: the migrated record carries {TENANT_LABEL}, so the two "
                       f"sides would match {'no longer' if b[0] == b[1] else 'where they did not'} "
                       f"(one carries {TENANT_LABEL}, the other not); keep {TENANT_LABEL} on both "
                       f"sides (`by ({TENANT_LABEL})`) or match them on(…) without it")
    return out


def plan_migration(groups, tenant, owner, expiry, dictionary=None, *, pin=None):
    """判讀 *groups*（Prometheus rule loader 讀出的 rule groups）的遷移。

    *pin* 預設 ``_lib_consumer_read.promql_pin``（測試注入）。回傳 ``Plan``；
    ``plan.refusals`` 非空時其餘欄位不可用。"""
    pin = pin or _lib_consumer_read.promql_pin
    plan = Plan()
    entries = [(group, rule) for group in groups for rule in group.get("rules") or []]
    trees = read_trees([str(rule.get("expr", "")) for _, rule in entries])
    own_records = {rule["record"] for _, rule in entries if rule.get("record")}
    reads = {}   # id(rule) -> metric names its expression reads
    for (_, rule), tree in zip(entries, trees):
        reads[id(rule)] = _lib_consumer_read.shape_metric_names(tree)

    # 字典命中：只讀一個原始指標、它在字典裡有黃金標準規則
    golden_ids = set()
    for (group, rule), tree in zip(entries, trees):
        if not rule.get("alert"):
            continue
        metrics = [m for m in reads[id(rule)] if m not in own_records]
        hit = lookup_dictionary(metrics[0], dictionary) if len(metrics) == 1 else None
        # every selector reads that metric: one that names none (`{job="x"}`)
        # is not covered by the golden rule, and goes through the selector
        # checks like any other
        only = all(_selector_metric(n) == metrics[0] for n in _selectors(tree)) if metrics else False
        if isinstance(hit, dict) and hit.get("golden_rule") and len(reads[id(rule)]) == 1 and only:
            golden_ids.add(id(rule))
            plan.golden.append((rule["alert"], metrics[0], hit))

    # 被遷移的告警讀到的 record（遞移），同檔一起遷移
    by_record = {}
    for group, rule in entries:
        if rule.get("record"):
            by_record.setdefault(rule["record"], []).append(rule)
    needed, todo = set(), [m for g, r in entries if r.get("alert") and id(r) not in golden_ids
                           for m in reads[id(r)]]
    while todo:
        name = todo.pop()
        if name in by_record and name not in needed:
            needed.add(name)
            todo.extend(m for r in by_record[name] for m in reads[id(r)])
    plan.unread_records = sorted(own_records - needed)

    chosen = [(g, r, t) for (g, r), t in zip(entries, trees)
              if (r.get("alert") and id(r) not in golden_ids) or r.get("record") in needed]
    names = {r.get("alert") for _, r in entries}
    if EXPIRY_ALERT in names:
        plan.refusals.append(f"{EXPIRY_ALERT}: the expiry alert migrate-rule adds has this "
                             "name; rename the rule")
    if any(g.get("name") == EXPIRY_GROUP for g in groups):
        plan.refusals.append(f"group {EXPIRY_GROUP}: the group of the expiry alert migrate-rule "
                             "adds has this name; rename the group")

    namespace_form = set()
    for group, rule, tree in chosen:
        where = f"{group.get('name', '<unnamed>')}/{_rule_name(rule)}"
        is_alert = bool(rule.get("alert"))
        for node in _selectors(tree):
            why, metric, form = _selector_refusal(node, own_records)
            if why:
                plan.refusals.append(f"{where}: selector {_node_text(str(rule['expr']), node)}: "
                                     f"{why}")
            elif form == "namespace":
                namespace_form.add(metric)
        if is_alert:
            kept = _keeps_migrated_label(tree)
            if kept:
                plan.refusals.append(
                    f"{where}: the expression keeps {MIGRATED_LABEL} ({', '.join(kept)}); a "
                    f"migrated alert writes {MIGRATED_LABEL}={MIGRATED_VALUE} over it, so "
                    "alerts that differ only by it collapse into one labelset Prometheus "
                    "refuses to evaluate")
        written = {**(group.get("labels") or {}), **(rule.get("labels") or {})}
        mine = {TENANT_LABEL: tenant, **({MIGRATED_LABEL: MIGRATED_VALUE} if is_alert else {})}
        for key, value in mine.items():
            if key in written and written[key] != value:
                plan.refusals.append(
                    f"{where}: label {key} is {json.dumps(str(written[key]))}, not "
                    f"{json.dumps(value)}: a migrated rule writes {key}={value} itself")
        if is_alert:
            for key, value in (("owner", owner), ("expiry", expiry.isoformat())):
                have = (rule.get("annotations") or {}).get(key)
                if key in written:
                    plan.refusals.append(f"{where}: {key} is a label; a migrated alert's "
                                         f"{key} is an annotation (governance)")
                elif have is not None and str(have) != value:
                    plan.refusals.append(
                        f"{where}: annotation {key} is {json.dumps(str(have))}, not "
                        f"{json.dumps(value)} (--{key})")

    plan.renamed = {name: migrated_record_name(tenant, name) for name in sorted(needed)}
    for name, new_name in plan.renamed.items():
        if new_name in own_records:
            plan.refusals.append(f"record {name}: its migrated name {new_name} is a record this "
                                 "file defines too; the two would write the same series")
    plan.refusals.extend(_matching_changes(chosen, groups, needed, tenant))
    answers = pin(tenant, [str(r["expr"]) for _, r, _ in chosen], sorted(namespace_form),
                  rename=plan.renamed)
    for (group, rule, _), answer in zip(chosen, answers):
        if "conflict" in answer:
            plan.refusals.append(f"{group.get('name', '<unnamed>')}/{_rule_name(rule)}: "
                                 f"selector {answer['conflict']}")
    if plan.refusals:
        return plan

    out_groups = {}
    for (group, rule, tree), answer in zip(chosen, answers):
        out = out_groups.get(id(group))
        if out is None:
            out = {k: v for k, v in group.items() if k != "rules"}
            out["rules"] = []
            out_groups[id(group)] = out
        new = dict(rule)
        if rule.get("record"):
            new["record"] = plan.renamed[rule["record"]]
        new["expr"] = answer["expr"]
        is_alert = bool(rule.get("alert"))
        new["labels"] = {**(rule.get("labels") or {}), TENANT_LABEL: tenant,
                         **({MIGRATED_LABEL: MIGRATED_VALUE} if is_alert else {})}
        if is_alert:
            new["annotations"] = {**(rule.get("annotations") or {}),
                                  "owner": owner, "expiry": expiry.isoformat()}
            plan.migrated_alerts.append(rule["alert"])
        else:
            plan.migrated_records.append(rule["record"])
        out["rules"].append(new)
        entry = {"group": group.get("name"),
                 ("alert" if is_alert else "record"): _rule_name(rule)}
        if not is_alert:
            entry["migrated_record"] = new["record"]
        entry["selectors"] = answer.get("selectors", [])
        plan.mapping.append(entry)
        groupings = groupings_without_cluster(tree)
        if groupings:
            plan.cluster_notes.append((_rule_name(rule), groupings))
    plan.groups = [out_groups[id(g)] for g in groups if id(g) in out_groups]
    if plan.groups:
        plan.groups.append({"name": EXPIRY_GROUP,
                            "rules": [expiry_alert(tenant, owner, expiry)]})
        # only on the migrated side: the alert-set comparison must expect it
        plan.mapping.append({"group": EXPIRY_GROUP, "alert": EXPIRY_ALERT, "added": True,
                             "selectors": []})
    return plan


def render_rules(plan, tenant, source):
    """migrated 規則檔的文字。"""
    head = (f"# migrate-rule：租戶 {tenant} 由 {source} 原樣遷移的規則。\n"
            "# 由抓取原始指標的那台 Prometheus 載入（單一 Prometheus，或 federation 的 edge）。\n"
            "# expr 是 Prometheus 印出的注入後運算式；要改規則，改來源後重跑 migrate-rule。\n")
    return head + yaml.safe_dump({"groups": plan.groups}, sort_keys=False,
                                 allow_unicode=True, width=1000)


def render_mapping(plan, tenant):
    """遷移對照檔的文字（validate-migration 讀，#4227）。"""
    head = ("# migrate-rule 的遷移對照：每條 migrated 規則的 selector 注入前（legacy）\n"
            "# 與注入後（pinned），以及 (tenant, alert)。selector 是不帶 range／offset／@ 的\n"
            "# instant selector，可直接拿去數序列：legacy 有、pinned 沒有的，就是注入選不到。\n")
    return head + yaml.safe_dump(
        {"tenant": tenant, "rules_file": rules_rel(tenant), "rules": plan.mapping},
        sort_keys=False, allow_unicode=True, width=1000)


def render_report(plan, tenant, owner, expiry):
    """遷移報告（中文）。"""
    lines = [f"migrate-rule 遷移報告：租戶 {tenant}", "=" * 60,
             f"owner：{owner}　expiry：{expiry.isoformat()}"
             f"（{EXPIRY_WARNING_DAYS} 天前起 {EXPIRY_ALERT} 會響）", "",
             "載入點：抓取原始指標的那台 Prometheus（單一 Prometheus，或 federation 架構下的 "
             "edge）。只帶 namespace 的 kube-state-metrics 序列不會被拉到中央，載入中央的話這些"
             "規則永遠不響。", ""]
    lines.append(f"已遷移的告警（{len(plan.migrated_alerts)}）：")
    lines += [f"  - {a}" for a in plan.migrated_alerts] or ["  （無）"]
    if plan.migrated_records:
        lines.append(f"一起遷移的 recording rule（{len(plan.migrated_records)}，改名以免與 "
                     "legacy 的同名 record 互讀）：")
        lines += [f"  - {r} → {plan.renamed[r]}" for r in plan.migrated_records]
    if plan.migrated_alerts:
        lines += ["", f"注意：每條 migrated 告警寫 {MIGRATED_LABEL}={MIGRATED_VALUE}，蓋掉序列自己的 "
                  f"{MIGRATED_LABEL}。序列本身帶 {MIGRATED_LABEL}（從抓取來）、多條只差它的，"
                  "會變成同一組 label，Prometheus 不評估這條告警（規則的 health 是 err）。"]
    if plan.golden:
        lines += ["", "改用黃金標準（平台 rule pack 已涵蓋，未產生 migrated 規則）："]
        for alert, metric, hit in plan.golden:
            lines.append(f"  - {alert}（{metric}）→ {hit.get('golden_rule')}"
                         f"（rule pack {hit.get('rule_pack', '?')}）")
            if hit.get("note"):
                lines.append(f"      {hit['note']}")
    if plan.unread_records:
        lines += ["", "未遷移的 recording rule（沒有被遷移的告警讀到）："]
        lines += [f"  - {r}" for r in plan.unread_records]
    if plan.cluster_notes:
        lines += ["", "需人工確認（多叢集）："]
        for name, groupings in plan.cluster_notes:
            lines.append(f"  - {name}：{cluster_grouping_note(groupings)}")
    if plan.lint_warnings:
        lines += ["", "lint WARN："]
        lines += [f"  - {w}" for w in plan.lint_warnings]
    return "\n".join(lines) + "\n"


def lint_output(text, tenant, policy_doc=None, policy_path=None):
    """對自己的輸出跑 ``lint_custom_rules``（租戶隔離、治理、deny-list），以
    租戶規則檔的佈局（``<掃描目錄>/<租戶>/rules.yml``）。→ (errors, warnings)，
    各為一行文字的 list。"""
    import lint_custom_rules
    policy = lint_custom_rules.load_policy(policy_doc, policy_path)
    with tempfile.TemporaryDirectory(prefix="migrate-rule-lint-") as scan:
        path = os.path.join(scan, tenant, "rules.yml")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        results, _ = lint_custom_rules.lint_file(path, policy, tenant=tenant)
    def line(r):
        return f"{r.rule_name or '<file>'}: {r.message}"
    return ([line(r) for r in results if r.severity == "ERROR"],
            [line(r) for r in results if r.severity == "WARN"])


def _require_output_tree(output_dir):
    """寫進 -o 之前：既有的目錄先過共用的 conf.d 拒絕
    （`_lib_write_check.require_output_tree`，#3206）；沒有可用的 da-guard
    ⇒ 結束碼 2、一個檔都不寫。"""
    import _lib_parse_tree
    try:
        _lib_write_check.require_output_tree(output_dir)
    except _lib_parse_tree.ParseTreeError as exc:
        print(f"ERROR: cannot judge the existing output directory through "
              f"`da-guard parse-tree` (ADR-036): {exc}; nothing was written",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)


def write_outputs(output_dir, tenant, rules_text, mapping_text, report_text):
    """三個檔：先確認每個目標都寫得進去（``check_writable_artifact``，#4250：
    目錄、唯讀檔、建不出來的目錄），再寫第一個——不會只寫了規則檔、沒有對照。"""
    _require_output_tree(output_dir)
    out = Path(output_dir)
    files = ((rules_rel(tenant), rules_text), (mapping_rel(tenant), mapping_text),
             (report_rel(tenant), report_text))
    # -o itself first: a file there (or under a file) is named as -o, not as
    # the first file under it
    ensure_dir_or_die(out, flag=_OUTPUT_FLAG)
    try:
        for rel, _ in files:
            check_writable_artifact(str(out / rel), flag=_OUTPUT_FLAG, make_dirs=True)
    except OutputWriteError as exc:
        die_caller_error(f"ERROR: {safe_label(str(exc))}", reason="output_unwritable")
    for rel, text in files:
        ensure_dir_or_die((out / rel).parent, flag=_OUTPUT_FLAG)
        write_operator_output_or_die(str(out / rel), text, flag=_OUTPUT_FLAG)


def _refuse(lines):
    for line in lines:
        print(f"ERROR: {line}", file=sys.stderr)
    print(f"ERROR: {len(lines)} problem(s) above — nothing was written", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


# ============================================================
# Main
# ============================================================

@exit_on_yaml_file_error  # #4064: an unreadable metric dictionary → rc 2, named
def main():
    """CLI entry point: 把一個租戶的傳統規則原樣遷移成 migrated 規則。"""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="把一個租戶的傳統 Prometheus 規則原樣遷移成 migrated 規則：每個 selector "
                    "注入租戶隔離，加 tenant／component=migrated label 與 owner／expiry "
                    "annotation，附到期伴隨告警（#4226）"
    )
    # #4040：規則檔由 _lib_input 讀一次；da-read 讀的是那份文字，不再開檔。
    _lib_input.add_input(parser, "input_file", decode=_lib_input.Document("text", str),
                         help="一個租戶的傳統 Prometheus rules YAML 檔案")
    parser.add_argument("-o", "--output-dir", default="migration_output", type=output_dir_type(),
                        help="草稿目錄 (預設: migration_output)；不得是服務中的 conf.d")
    parser.add_argument("--tenant", required=True,
                        help="租戶 id：每個 selector 釘到它，label tenant 寫它")
    parser.add_argument("--owner", required=True,
                        help="負責人：寫進每條告警的 owner annotation")
    parser.add_argument("--expiry", required=True, metavar="YYYY-MM-DD",
                        help="到期日（今天之後）：寫進 expiry annotation；"
                             f"{EXPIRY_WARNING_DAYS} 天前起 {EXPIRY_ALERT} 會響")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印報告，不寫檔")
    parser.add_argument("--no-dictionary", action="store_true",
                        help="停用字典比對（每條告警都遷移，不建議改用黃金標準）")
    import lint_custom_rules  # the lint's own declaration of its policy document
    _lib_input.add_input(parser, "--policy", decode=lint_custom_rules.POLICY_DOCUMENT,
                         help="自我 lint 用的 custom-rule-policy.yaml（預設：內建政策）")
    args = parser.parse_args()

    if not is_valid_tenant_id(args.tenant):
        _refuse([f"--tenant {json.dumps(args.tenant)} is not a valid tenant id "
                 f"({tenant_id_rule()[1]})"])
    owner = args.owner.strip()
    if not owner:
        _refuse(["--owner is empty"])
    if "{{" in owner:
        # an annotation value is a template Prometheus expands per alert
        _refuse(["--owner holds '{{': an annotation is a template, so it would be "
                 "expanded per alert, not written as given"])
    expiry = expiry_date(args.expiry)
    if expiry is None:
        _refuse([f"--expiry {json.dumps(args.expiry)} is not a calendar date written "
                 "YYYY-MM-DD after today (UTC)"])

    # the declared inputs first: a policy that cannot be read is refused, named,
    # before any rule is judged
    policy_doc = _lib_input.document(args, "policy", lang="en")
    dictionary = {} if args.no_dictionary else load_metric_dictionary()
    if not args.no_dictionary and not dictionary:
        print("[WARN] 找不到 metric-dictionary.yaml（工具同層與上一層都沒有），"
              "本次不做黃金標準比對。", file=sys.stderr)

    # #3206（ADR-036 §12）：輸入的規則檔以 Prometheus 的 rule loader 讀
    # （da-read prometheus-rules）；它拒收的檔、或找不到 da-read，都以 rc 2 結束。
    source = _lib_input.named(args, "input_file")
    text = _lib_input.document(args, "input_file", lang="en")
    try:
        (rule_file,) = _lib_consumer_read.require_read(
            _lib_consumer_read.prometheus_rule_text_results([(source, text)]))
        plan = plan_migration(rule_file.groups or [], args.tenant, owner, expiry, dictionary)
    except _lib_consumer_read.ConsumerReadError as e:
        _refuse([_lib_consumer_read.error_text(e)])
    if plan.refusals:
        _refuse(plan.refusals)

    rules_text = render_rules(plan, args.tenant, Path(source).name) if plan.groups else None
    if rules_text is not None:
        try:
            errors, plan.lint_warnings = lint_output(rules_text, args.tenant, policy_doc,
                                                     args.policy)
        except _lib_consumer_read.ConsumerReadError as e:
            _refuse([f"lint of the migrated rules: {_lib_consumer_read.error_text(e)}"])
        if errors:
            _refuse([f"lint of the migrated rules: {e}" for e in errors])
    report = render_report(plan, args.tenant, owner, expiry)

    if args.dry_run:
        print(report, end="")
        return
    if rules_text is None:
        print(report, end="")
        print("沒有要遷移的規則（每條告警都改用黃金標準，或檔裡沒有告警）：不寫檔。")
        stale = [rel for rel in (rules_rel(args.tenant), mapping_rel(args.tenant))
                 if (Path(args.output_dir) / rel).exists()]
        if stale:
            print(f"[WARN] {args.output_dir} still holds an earlier run's "
                  f"{', '.join(stale)} for tenant {args.tenant}: this run migrated nothing, "
                  "so they are not this source's migration; remove them", file=sys.stderr)
        return
    write_outputs(args.output_dir, args.tenant, rules_text,
                  render_mapping(plan, args.tenant), report)
    print(f"[✓] 已遷移 {len(plan.migrated_alerts)} 條告警、{len(plan.migrated_records)} 條 "
          f"recording rule；{len(plan.golden)} 條改用黃金標準（見報告）")
    print(f"📁 規則檔 {args.output_dir}/{rules_rel(args.tenant)}、對照 "
          f"{args.output_dir}/{mapping_rel(args.tenant)}、報告 "
          f"{args.output_dir}/{report_rel(args.tenant)}")
    print("⚠️  規則檔交給抓取原始指標的那台 Prometheus 載入（federation 架構下是 edge）。")


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
