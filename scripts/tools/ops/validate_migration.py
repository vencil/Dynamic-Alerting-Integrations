#!/usr/bin/env python3
"""validate_migration.py — Shadow Monitoring 驗證工具：比對 legacy 與 migrated 的告警集合。

讀 migrate-rule 寫的遷移對照（``.migration/mapping/<租戶>.yaml``），每一輪做三件事：

0. 規則檢查：對照裡每條告警，legacy 端要載入不帶 ``da_migrated`` 的同名規則，migrated
   端要載入該租戶的 migrated 規則（``/api/v1/rules``）；任一邊沒載入，或所在規則組的評估
   間隔大於取樣窗口（``--interval``，窗口裡可能沒有任何一次評估，看起來就像沒響），
   結束碼 2、逐條點名。少了這一步，migrated 規則根本沒部署而這段期間又沒有告警響時，
   兩邊都是空集合，會被判成一致。
1. count 預檢：對照裡每組 selector，legacy（注入前）在 legacy 端、pinned（注入後）
   在 migrated 端各數一次序列。legacy 有、pinned 是 0 → 注入選不到資料（租戶歸屬的
   假設錯了，或 ``job=``／``instance=`` 與平台的抓取設定不一致；migrated 規則永遠
   不會響），結束碼 2、逐條點名。兩邊都是 0 → 不可驗，不放行。只查現有資料。
2. 告警集合比對：每條有 legacy 對應的 migrated 告警，兩邊各查一次
   ``max_over_time(ALERTS{…}[<interval>])``——這一輪的窗口內響過（firing 與
   pending 分開）的序列。比對鍵是（租戶, ``da_cluster``, alertname, 狀態）加上
   序列其餘的 label，去掉 ``tenant``、``da_migrated``、``alertstate``。租戶歸屬：
   migrated 側是規則寫的靜態 ``tenant``；legacy 側取告警的 ``tenant``，沒有就取
   ``namespace``，兩者都沒有 → 不可驗。legacy 側其他租戶的告警是注入本來就會收窄
   的部分，另列、不算 mismatch。遷移時新增的告警（``added: true``）不比。

用窗口而不是瞬時值：兩台 Prometheus 的評估相位不同時，瞬時值會誤報 mismatch。
代價是短於一輪的時差看不到；遷移不改 ``for:``，這個代價可接受。窗口是左開區間
``(t − interval, t]``，t 是各端收到查詢的時刻；兩輪之間還隔著一輪查詢的耗時，所以
相鄰兩輪的窗口之間有一道小縫，比縫還短的單邊告警可能看不到。

已知限制：legacy 端以 alertname 查 ALERTS。另一條同名、沒有被遷移的 legacy 規則
（例如改用黃金標準的，或在別的檔案裡）的告警沒有 migrated 對應，會一直是 mismatch。

數值比對（``--old``／``--new``、prefix-mapping、``--tolerance``）已移除（#4227）：
prefix-mapping 以具名的結束碼 2 拒收，請用新版 migrate-rule 重產對照。

用法:
  # 單次比對（legacy 與 migrated 規則載入同一台 Prometheus）
  python3 validate_migration.py \\
    --mapping migration_output/.migration/mapping/db-a.yaml \\
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090

  # legacy 規則還在客戶原本的 Prometheus 上：兩端分別指定
  python3 validate_migration.py \\
    --mapping migration_output/.migration/mapping/db-a.yaml \\
    --prometheus http://edge-prometheus:9090 \\
    --legacy-prometheus http://legacy-prometheus:9090

  # 持續監控，收斂（連續 N 輪乾淨）就寫 cutover-readiness.json 並停止
  python3 validate_migration.py \\
    --mapping migration_output/.migration/mapping/db-a.yaml \\
    --watch --interval 60 --rounds 1440 --auto-detect-convergence

結束碼：0 = 告警集合一致、預檢乾淨（watch + 收斂偵測：已收斂）；1 = 有 mismatch
或不可驗（watch + 收斂偵測：未收斂）；2 = 輸入錯誤、查詢失敗，或規則沒載入、評估間隔
大於窗口、注入後選不到資料（等不掉的設定問題，watch 也立刻停止）。
"""

import sys
import os
import csv
import io
import json
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_python import ensure_dir_or_die, write_operator_output_or_die, output_dir_type, add_prometheus_arg, prometheus_url  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError, json_file_text  # noqa: E402
from _lib_label import safe_label  # noqa: E402  (#4210: a key is file content)
from _lib_validation import is_valid_tenant_id, operator_file_type, positive_int, positive_seconds, url_type  # noqa: E402
import _lib_input  # noqa: E402  (#3445: --mapping is read by the lib)
import _lib_shadow_report as report  # noqa: E402  (#3662 / #4227: the one definition)
from _lib_shadow_report import tenant_cell  # noqa: E402
from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL  # noqa: E402  (ADR-006)
from _grar_routes import MIGRATED_LABEL, MIGRATED_VALUE  # noqa: E402  (#4355: the one spelling)
# #3133: a query not answered raises QueryError; a sample is read once, by
# the lib (#3213).
from _lib_prometheus import QueryError, prometheus_instant, prometheus_rules, vector_count  # noqa: E402

# The query seams tests replace (the lib's raising readers).
query_prometheus = prometheus_instant
query_rules = prometheus_rules

#: The label a migrated rule writes, and the one a legacy alert is
#: attributed by first.
TENANT_LABEL = "tenant"
#: A legacy alert without ``tenant`` is attributed by this one.
NAMESPACE_LABEL = "namespace"
#: The ALERTS label that tells firing from pending.
ALERTSTATE_LABEL = "alertstate"
#: Labels that are not part of an alert's label set: the side (``tenant`` is
#: static on the migrated side; the migration identity), the state (a key
#: field of its own), and the ones the key holds by name.
_NOT_IN_LABELS = frozenset({"__name__", "alertname", TENANT_LABEL, MIGRATED_LABEL,
                            ALERTSTATE_LABEL, CLUSTER_IDENTITY_LABEL})


#: --mapping：migrate-rule 寫的遷移對照，頂層是映射（#3445：內容讀不了、或頂層型別不對，
#: 都是點名旗標與路徑的 caller error，不是 traceback）。
MAPPING_DOCUMENT = _lib_input.Document("yaml", dict)

_EXPECTED = {"str": "非空字串", "list": "清單", "dict": "映射", "bool": "布林值",
             "alert_or_record": "alert 與 record 恰好其一",
             "tenant_id": "合法的租戶 id（ADR-035）",
             "unique": "不同時是要比的告警與 added 的告警",
             "added_alert": f"只有 {report.ADDED_ALERT} 能標 added"}


def load_mapping(args):
    """`--mapping` 的遷移對照（``_lib_shadow_report.read_mapping``）。

    ⛔ prefix-mapping（數值比對用的 ``old_query``／``new_query``／``original_metric``）
    以具名的 caller error 拒收（rc 2，reason ``mapping_prefix_format``），不退回
    數值比對（#4227）。不是 migrate-rule 寫的形狀：每一處都點名後 rc 2（#4210／
    #4221：讀不了的項目不靜默略過）。沒有任何可比的告警也是 rc 2：零比對組等於
    什麼都沒驗證。
    """
    path = safe_label(str(args.mapping))
    doc = _lib_input.document(args, "mapping", lang="zh")
    if report.is_prefix_mapping(doc):
        raise CallerError(
            f"--mapping {path}: 這是數值比對用的 prefix-mapping（{'／'.join(report.PREFIX_MAPPING_FIELDS)}），"
            "validate 已改為比對告警集合，不再讀它。請用新版 migrate-rule 重產遷移對照"
            "（da-tools migrate-rule <rules.yml> --tenant <租戶> --owner <團隊> --expiry <日期>），"
            "再以 --mapping <輸出目錄>/.migration/mapping/<租戶>.yaml 執行",
            reason="mapping_prefix_format")
    mapping, problems = report.read_mapping(doc, is_valid_tenant_id)
    if problems:
        shown = "；".join(f"{safe_label(where)}（應為{_EXPECTED[want]}）" for where, want in problems)
        raise CallerError(
            f"--mapping {path}: {len(problems)} 處不是 migrate-rule 寫的遷移對照，整份未比對"
            f"（請用 migrate-rule 重產這個檔，或修正這幾處）: {shown}",
            reason="mapping_entry_unusable")
    if not mapping.alerts:
        raise CallerError(
            f"--mapping {path}: 沒有任何有 legacy 對應的告警可比"
            f"（遷移時新增的 {len(mapping.added)} 條不比），零比對組等於什麼都沒驗證",
            reason="mapping_no_alerts")
    return mapping


def promql_string(value):
    """*value* as a PromQL double-quoted string: JSON's escapes are a subset
    of the Go string syntax PromQL reads."""
    return json.dumps(value, ensure_ascii=False)


def window_text(seconds):
    """A range-selector duration for *seconds* (in milliseconds, so a
    fractional --interval keeps its value)."""
    return f"{max(1, round(seconds * 1000))}ms"


def alerts_query(alert, window, side, tenant):
    """The ALERTS series of *alert* seen during the last *window* seconds on
    *side* (``legacy`` or ``migrated``): a migrated alert carries
    ``da_migrated`` and its tenant, a legacy one never carries it."""
    matchers = [f"alertname={promql_string(alert)}"]
    if side == "migrated":
        matchers += [f"{MIGRATED_LABEL}={promql_string(MIGRATED_VALUE)}",
                     f"{TENANT_LABEL}={promql_string(tenant)}"]
    else:
        matchers.append(f'{MIGRATED_LABEL}=""')
    return f"max_over_time(ALERTS{{{', '.join(matchers)}}}[{window_text(window)}])"


class AlertKey(NamedTuple):
    """One alert series of one side, as the two sides are compared by."""

    alert: str
    tenant: "str | None"
    da_cluster: "str | None"
    state: str
    labels: tuple  # ((name, value), ...) sorted, without _NOT_IN_LABELS


def alert_key(alert, metric, side):
    """The key of the ALERTS series *metric*. Its tenant: on the migrated
    side the static ``tenant`` the rule writes; on the legacy side the
    alert's ``tenant``, else its ``namespace``, else None (unverifiable)."""
    owner = metric.get(TENANT_LABEL) or None
    if side == "legacy" and owner is None:
        owner = metric.get(NAMESPACE_LABEL) or None
    labels = tuple(sorted((k, v) for k, v in metric.items() if k not in _NOT_IN_LABELS))
    return AlertKey(alert, owner, metric.get(CLUSTER_IDENTITY_LABEL) or None,
                    metric.get(ALERTSTATE_LABEL, ""), labels)


def labels_text(labels):
    """*labels* as a PromQL label set."""
    return "{" + ", ".join(f"{k}={promql_string(v)}" for k, v in labels) + "}"


def _order(key):
    return (key.da_cluster or "", key.state, key.labels)


def compare_alerts(alert, tenant, legacy_keys, migrated_keys):
    """The report rows of *alert*: each key of either side, with its status."""
    rows = []
    legacy_mine = {k for k in legacy_keys if k.tenant == tenant}
    for key in sorted(legacy_mine | set(migrated_keys), key=_order):
        on_legacy, on_migrated = key in legacy_mine, key in migrated_keys
        status = report.STATUS_MATCH if on_legacy and on_migrated else report.STATUS_MISMATCH
        rows.append(_alert_row(key, on_legacy, on_migrated, status))
    for key in sorted(set(legacy_keys) - legacy_mine,
                      key=lambda k: (k.tenant is not None, k.tenant or "", *_order(k))):
        status = report.STATUS_UNVERIFIABLE if key.tenant is None else report.STATUS_OTHER_TENANT
        rows.append(_alert_row(key, True, False, status))
    return rows


def _alert_row(key, on_legacy, on_migrated, status):
    return {"kind": report.KIND_ALERT, "rule": key.alert, "tenant": key.tenant or "",
            CLUSTER_IDENTITY_LABEL: key.da_cluster, "state": key.state,
            "labels": labels_text(key.labels), "legacy": int(on_legacy),
            "migrated": int(on_migrated), "status": status}


def selector_status(legacy_count, pinned_count):
    """The precheck verdict of one selector pair."""
    if pinned_count > 0:
        return report.STATUS_OK
    if legacy_count > 0:
        return report.STATUS_PIN_SELECTS_NOTHING
    return report.STATUS_UNVERIFIABLE


def _count(prom, selector):
    """How many series *selector* selects on *prom*, or None (said on
    stderr) when the query is not answered."""
    try:
        return vector_count(query_prometheus(prom, f"count({selector})")) or 0
    except QueryError as exc:
        print(f"  ❌ 查詢失敗（{prom}）: count({selector}): {exc}", file=sys.stderr)
        return None


def _alert_keys(prom, alert, window, side, tenant):
    try:
        rows = query_prometheus(prom, alerts_query(alert, window, side, tenant))
    except QueryError as exc:
        print(f"  ❌ 查詢 {side} 端 ALERTS 失敗（{prom}）: {alert}: {exc}", file=sys.stderr)
        return None
    return {alert_key(alert, row.get("metric", {}), side) for row in rows}


def rule_rows(mapping, prom, window, side):
    """The rule rows of *side*: every alert of *mapping* not loaded there, or
    loaded in a group evaluated less often than *window*. None (said on
    stderr) when the rules API does not answer."""
    try:
        groups = query_rules(prom, rule_type="alert")
    except QueryError as exc:
        print(f"  ❌ 查詢 {side} 端規則失敗（{prom}）: {exc}", file=sys.stderr)
        return None
    loaded = report.loaded_alert_rules(groups, mapping.alerts, migrated=side == "migrated",
                                       tenant=mapping.tenant, label=MIGRATED_LABEL,
                                       value=MIGRATED_VALUE)
    rows = []
    for alert in mapping.alerts:
        rules = loaded[alert]
        slowest = max((r.interval for r in rules if r.interval is not None), default=None)
        failing = [r.error for r in rules if r.error]
        if not rules:
            status, labels = report.STATUS_RULE_NOT_LOADED, "（沒有載入）"
        elif slowest is not None and slowest > window:
            status, labels = report.STATUS_WINDOW_TOO_SHORT, f"評估間隔 {slowest:g}s > 窗口 {window:g}s"
        elif failing:
            # A rule that does not evaluate writes no ALERTS: its side reads
            # as "nothing fired" (CodeRabbit on #4381; byo_check's reading).
            status, labels = report.STATUS_UNVERIFIABLE, f"評估錯誤: {safe_label(failing[0])[:120]}"
        else:
            continue
        rows.append({"kind": report.KIND_RULE, "rule": alert, "tenant": mapping.tenant,
                     CLUSTER_IDENTITY_LABEL: None, "state": "", "labels": labels,
                     "legacy": int(side == "legacy"), "migrated": int(side == "migrated"),
                     "status": status})
    return rows


def _unanswered(kind, rule, tenant, labels, side=None):
    """The row of a query not answered: unverifiable, so a report missing an
    answer never reads as clean (CodeRabbit on #4381: the CSV is all
    `shadow-verify --report-csv` reads)."""
    return {"kind": kind, "rule": rule, "tenant": tenant, CLUSTER_IDENTITY_LABEL: None,
            "state": "", "labels": f"查詢失敗: {labels}", "legacy": int(side == "legacy"),
            "migrated": int(side == "migrated"), "status": report.STATUS_UNVERIFIABLE}


def run_round(mapping, legacy_prom, migrated_prom, window):
    """One round: ``{"rows": [...], "query_errors": n}``. A query not
    answered counts in ``query_errors`` AND leaves an unverifiable row."""
    rows, query_errors = [], 0
    for prom, side in ((legacy_prom, "legacy"), (migrated_prom, "migrated")):
        found = rule_rows(mapping, prom, window, side)
        if found is None:
            query_errors += 1
            rows.append(_unanswered(report.KIND_RULE, "*", mapping.tenant,
                                    f"{side} 端 /api/v1/rules", side))
        else:
            rows.extend(found)
    for pair in mapping.selectors:
        legacy_n = _count(legacy_prom, pair.legacy)
        pinned_n = _count(migrated_prom, pair.pinned)
        if legacy_n is None or pinned_n is None:
            query_errors += 1
            rows.append(_unanswered(report.KIND_SELECTOR, pair.rule, mapping.tenant,
                                    f"{pair.legacy} → {pair.pinned}"))
            continue
        rows.append({"kind": report.KIND_SELECTOR, "rule": pair.rule,
                     "tenant": mapping.tenant, CLUSTER_IDENTITY_LABEL: None, "state": "",
                     "labels": f"{pair.legacy} → {pair.pinned}",
                     "legacy": legacy_n, "migrated": pinned_n,
                     "status": selector_status(legacy_n, pinned_n)})
    for alert in mapping.alerts:
        legacy_keys = _alert_keys(legacy_prom, alert, window, "legacy", mapping.tenant)
        migrated_keys = _alert_keys(migrated_prom, alert, window, "migrated", mapping.tenant)
        if legacy_keys is None or migrated_keys is None:
            query_errors += 1
            rows.append(_unanswered(report.KIND_ALERT, alert, mapping.tenant, "ALERTS"))
            continue
        rows.extend(compare_alerts(alert, mapping.tenant, legacy_keys, migrated_keys))
    return {"rows": rows, "query_errors": query_errors}


#: The counts a round is judged by (and the readiness reports), by name.
_TALLIED = (report.STATUS_MATCH, report.STATUS_MISMATCH, *report.SETUP_STATUSES,
            report.STATUS_UNVERIFIABLE, report.STATUS_OTHER_TENANT)


def tally(result):
    """The status counts of a round, and its query errors."""
    counts = {status: 0 for status in _TALLIED}
    for row in result["rows"]:
        if row["status"] in counts:
            counts[row["status"]] += 1
    counts["query_errors"] = result["query_errors"]
    return counts


def is_clean(result):
    """A round with nothing in the way of a cutover: every rule loaded and
    evaluated within the window, every pin selecting data, no mismatch,
    nothing unverifiable, every query answered."""
    counts = tally(result)
    return not (any(counts[s] for s in report.BLOCKING_STATUSES) or counts["query_errors"])


def setup_problems(result):
    """How many rows of *result* no waiting clears (``SETUP_STATUSES``)."""
    counts = tally(result)
    return sum(counts[s] for s in report.SETUP_STATUSES)


def _where(row):
    cluster = row.get(CLUSTER_IDENTITY_LABEL)
    tenant = row["tenant"] or "（無 tenant／namespace）"
    return tenant if cluster is None else f"{tenant} {CLUSTER_IDENTITY_LABEL}={cluster}"


def print_summary(result):
    """印出一輪的摘要；有問題的列逐條點名。"""
    counts = tally(result)
    rows = result["rows"]
    selectors = [r for r in rows if r["kind"] == report.KIND_SELECTOR]
    print(f"\n{'='*60}")
    print("📊 驗證摘要 (Validation Summary)")
    print(f"{'='*60}\n")
    print(f"規則: 沒有載入 {counts[report.STATUS_RULE_NOT_LOADED]}、"
          f"評估間隔大於窗口 {counts[report.STATUS_WINDOW_TOO_SHORT]}")
    print(f"selector 預檢: {len(selectors)} 組")
    print(f"  ❌ 注入後選不到資料: {counts[report.STATUS_PIN_SELECTS_NOTHING]}")
    print(f"告警序列: {sum(1 for r in rows if r['kind'] == report.KIND_ALERT)}")
    print(f"  ✅ 兩邊一致: {counts[report.STATUS_MATCH]}")
    print(f"  ❌ 只在一邊: {counts[report.STATUS_MISMATCH]}")
    print(f"  ℹ️  legacy 側其他租戶（注入收窄，不算 mismatch）: {counts[report.STATUS_OTHER_TENANT]}")
    print(f"不可驗（不放行）: {counts[report.STATUS_UNVERIFIABLE]}\n")

    for row in rows:
        status = row["status"]
        if row["kind"] == report.KIND_RULE or row["labels"].startswith("查詢失敗"):
            # A rule row (not loaded, a group slower than the window, an
            # evaluation error) or a query not answered: its own words.
            side = ("legacy 端" if row["legacy"] else "migrated 端" if row["migrated"] else "")
            print(f"  • [{row['rule']}] {side}{'規則: ' if row['kind'] == report.KIND_RULE else ''}"
                  f"{row['labels']}")
        elif status == report.STATUS_PIN_SELECTS_NOTHING:
            print(f"  • [{row['rule']}] 注入後選不到資料: {row['labels']}"
                  f"（legacy {row['legacy']} 條序列，pinned 0 條）")
        elif status == report.STATUS_MISMATCH:
            side = "legacy" if row["legacy"] else "migrated"
            print(f"  • [{row['rule']}] 只在 {side} 端 {row['state']}: "
                  f"{_where(row)} {row['labels']}")
        elif status == report.STATUS_UNVERIFIABLE and row["kind"] == report.KIND_SELECTOR:
            print(f"  • [{row['rule']}] 不可驗（兩邊都沒有序列）: {row['labels']}")
        elif status == report.STATUS_UNVERIFIABLE:
            print(f"  • [{row['rule']}] 不可驗（legacy 告警沒有 tenant 也沒有 namespace，"
                  f"歸不了租戶）: {row['state']} {row['labels']}")

    if counts["query_errors"]:
        print(f"\n⚠️  {counts['query_errors']} 組查詢失敗（Prometheus 連線／查詢層錯誤），"
              "本輪結果不完整——未驗證的比對組不得視為一致。")
    if is_clean(result):
        print("🎉 告警集合一致、預檢乾淨。\n")
    else:
        print()


def round_exit_code(result):
    """The exit code a round answers on its own: a query not answered or a
    setup problem (``SETUP_STATUSES``) → 2; a mismatch or anything
    unverifiable → 1."""
    counts = tally(result)
    if counts["query_errors"] or setup_problems(result):
        return EXIT_CALLER_ERROR
    if counts[report.STATUS_MISMATCH] or counts[report.STATUS_UNVERIFIABLE]:
        return EXIT_VIOLATION
    return EXIT_OK


def write_csv_report(result, output_dir):
    """將一輪的結果寫入 CSV（欄位是 ``_lib_shadow_report.REPORT_COLUMNS``）。"""
    out_dir = Path(output_dir)
    # #1641: an unusable -o/--output-dir is rc=2 + one line naming the flag,
    # not a traceback at rc=1 (which reads as a finding).
    ensure_dir_or_die(out_dir, flag="-o/--output-dir")
    csv_path = str(out_dir / "validation-report.csv")

    buf = io.StringIO()
    # lineterminator='\n': the writer (the operator entry since #3107) pins
    # newline="\n" itself, so the file is LF on every platform.
    # #3662: the header, the row keys and the timestamp format are
    # `_lib_shadow_report`'s — the one definition `shadow-verify
    # --report-csv` reads the file by. DictWriter refuses a key that is no
    # column, so a row cannot drift from the header.
    writer = csv.DictWriter(buf, fieldnames=report.REPORT_COLUMNS, lineterminator='\n')
    writer.writeheader()
    ts = time.strftime(report.TIMESTAMP_FORMAT, time.gmtime())
    for row in result["rows"]:
        writer.writerow({
            "Kind": row["kind"],
            "Rule": row["rule"],
            "Tenant": tenant_cell(row["tenant"], row.get(CLUSTER_IDENTITY_LABEL)),
            "State": row["state"],
            "Labels": row["labels"],
            "Legacy": row["legacy"],
            "Migrated": row["migrated"],
            "Status": row["status"],
            "Timestamp": ts,
        })
    write_operator_output_or_die(csv_path, "﻿" + buf.getvalue(), flag="-o/--output-dir")
    return csv_path


def _convergence_flag(args) -> str:
    """Which flag the convergence-report path came from (#1641).

    ``--convergence-output`` when given; otherwise the report lands under
    ``-o/--output-dir``, and that is the flag the write error must name.
    """
    return "--convergence-output" if args.convergence_output else "-o/--output-dir"


class ConvergenceTracker:
    """Track clean rounds across watch rounds (#4227).

    Ready when the last ``stability_window`` rounds were all clean
    (:func:`is_clean`): no mismatch, nothing unverifiable, every pin
    selecting data, every query answered.
    """

    def __init__(self, tenant, stability_window=5):
        self.tenant = tenant
        self.stability_window = stability_window
        self.round_count = 0
        self.clean_rounds = 0  # consecutive, ending at the last round
        self.last = None

    def record_round(self, result):
        """Record one polling round."""
        self.round_count += 1
        self.clean_rounds = self.clean_rounds + 1 if is_clean(result) else 0
        self.last = tally(result)

    def compute_report(self):
        """Return the cutover readiness assessment: the fields of
        ``_lib_shadow_report.READINESS_FIELDS``, in order, nothing else."""
        ready = self.clean_rounds >= self.stability_window
        if ready:
            recommendation = "Safe to cutover"
        else:
            recommendation = (f"{self.clean_rounds}/{self.stability_window} consecutive "
                              "clean rounds")
        values = {
            "ready": ready,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tenant": self.tenant,
            "round_count": self.round_count,
            "stability_window": self.stability_window,
            "clean_rounds": self.clean_rounds,
            "last_round": self.last or {},
            "recommendation": recommendation,
        }
        return {field: values[field] for field in report.READINESS_FIELDS}

    def print_status(self):
        """Print convergence status to stdout."""
        out = self.compute_report()
        print(f"\n  Convergence: {out['clean_rounds']}/{out['stability_window']} "
              "consecutive clean rounds")
        if out["ready"]:
            print("\n  *** CUTOVER READY ***")
        return out


def _write_readiness(args, data):
    conv_path = args.convergence_output or str(Path(args.output_dir) / "cutover-readiness.json")
    conv_flag = _convergence_flag(args)
    ensure_dir_or_die(Path(conv_path).parent, flag=conv_flag)
    write_operator_output_or_die(conv_path, json_file_text(data, flag=conv_flag), flag=conv_flag)
    return conv_path


def build_parser():
    """The validate parser."""
    parser = argparse.ArgumentParser(
        description="Shadow Monitoring 驗證工具 — 比對 legacy 與 migrated 規則的告警集合",
    )
    _lib_input.add_input(parser, "--mapping", decode=MAPPING_DOCUMENT, required=True,
                         help="migrate-rule 寫的遷移對照（.migration/mapping/<租戶>.yaml）")
    add_prometheus_arg(parser,
                       help_text="migrated 規則所在的 Prometheus Query API URL "
                                 "(預設: $PROMETHEUS_URL，否則 http://localhost:9090; "
                                 "叢集內建議用 http://prometheus.monitoring.svc.cluster.local:9090)")
    parser.add_argument("--legacy-prometheus", default=None, type=url_type(empty="refuse"),
                        help="legacy 規則所在的 Prometheus Query API URL（預設: 與 --prometheus 相同）")
    parser.add_argument("-o", "--output-dir", default="validation_output", type=output_dir_type(),
                        help="輸出目錄 (預設: validation_output)")
    parser.add_argument("--watch", action="store_true",
                        help="持續監控模式")
    parser.add_argument("--interval", type=positive_seconds, default=60,
                        help="每輪的間隔秒數，也是告警取樣窗口（> 0，可帶小數；預設: 60）。"
                             "小於規則組的評估間隔時結束碼 2：窗口裡沒有一次評估，那一側就看不到告警")
    parser.add_argument("--rounds", type=positive_int, default=10,
                        help="監控輪數 (預設: 10)")
    parser.add_argument("--auto-detect-convergence", action="store_true",
                        help="Track clean rounds; auto-stop and write cutover readiness when ready")
    parser.add_argument("--stability-window", type=positive_int, default=5,
                        help="Consecutive clean rounds required for convergence (default: 5)")
    # #3107 ③b: the progress and summary own stdout, so `-` is refused.
    parser.add_argument("--convergence-output",
                        type=operator_file_type(
                            stdout="this tool prints its progress and summary there"),
                        help="Write cutover readiness report JSON to file")
    return parser


def main():
    """CLI entry point: Shadow Monitoring 驗證工具。."""
    try_utf8_stdout()
    args = build_parser().parse_args()

    mapping = load_mapping(args)
    print(f"📂 租戶 {mapping.tenant}: {len(mapping.alerts)} 條告警、"
          f"{len(mapping.selectors)} 組 selector，載入自 {args.mapping}")

    # #3528: --prometheus, else $PROMETHEUS_URL judged here (rc 2, named).
    migrated_prom = prometheus_url(args.prometheus)
    legacy_prom = args.legacy_prometheus or migrated_prom

    def one_round():
        result = run_round(mapping, legacy_prom, migrated_prom, args.interval)
        print_summary(result)
        return result, write_csv_report(result, args.output_dir)

    if not args.watch:
        result, csv_path = one_round()
        print(f"📁 CSV 報告: {csv_path}")
        return round_exit_code(result)

    tracker = (ConvergenceTracker(mapping.tenant, stability_window=args.stability_window)
               if args.auto_detect_convergence else None)
    print(f"\n  Watch mode: every {args.interval}s, up to {args.rounds} rounds")
    if tracker:
        print(f"  Convergence detection: stability window = {args.stability_window} rounds\n")

    result = csv_path = None
    for i in range(args.rounds):
        print(f"\n--- Round {i+1}/{args.rounds} ({time.strftime('%H:%M:%S')}) ---")
        result, csv_path = one_round()
        if tracker:
            tracker.record_round(result)
            if tracker.print_status()["ready"]:
                print(f"\n  Cutover readiness report: "
                      f"{_write_readiness(args, tracker.compute_report())}")
                break
        # A setup problem is no state to wait out: a pin that selects nothing,
        # a rule not loaded, a window no evaluation falls in.
        if setup_problems(result):
            print("\n  規則沒載入、評估間隔大於窗口，或注入後選不到資料：停止監控（修正後再驗）。",
                  file=sys.stderr)
            break
        if i < args.rounds - 1:
            time.sleep(args.interval)

    if tracker and not tracker.compute_report()["ready"]:
        print(f"\n  Watch completed ({tracker.round_count} rounds) without convergence.")
        print(f"  Partial convergence report: {_write_readiness(args, tracker.compute_report())}")
    print(f"\n  CSV report: {csv_path}")

    # Exit 語意（watch）：收斂 → 0（早期輪的 mismatch 是等待收斂的常態）；
    # 否則以最後一輪為準：查詢失敗或注入選不到資料 → 2；有收斂偵測但未收斂
    # → 1，即使最後一輪碰巧乾淨；無收斂偵測 → 最後一輪自己的結束碼。
    if tracker is not None and tracker.compute_report()["ready"]:
        return EXIT_OK
    code = round_exit_code(result)
    if code == EXIT_CALLER_ERROR or tracker is None:
        return code
    return EXIT_VIOLATION


if __name__ == "__main__":
    # entrypoint.py 以 exec_module(__main__) 執行本檔，exit code 只能靠
    # SystemExit 上傳——裸 main() 會把回傳值丟掉。main() 回傳 int，run() 把它當成
    # process exit code。
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
