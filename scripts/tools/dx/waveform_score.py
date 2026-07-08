#!/usr/bin/env python3
"""waveform_score.py — temporal-match + episode 對帳 + catch-rate 報告器（ADR-030 PR-3）

吃 PR-2 ``inject_waveform.py`` 的 JSON 報告（1..N 份）+ 容差矩陣檔（D5 兩段式）→
逐 injected case 做 temporal-match → episode×alert-class 對帳 → catch-rate +
FN 清單 + indeterminate 清單 + fan-out ratio → verdict（D8.1）+ 結構化報告保存
（R2-4 baseline；``schema_version`` 欄）。**不做**：通知送達／post-inhibition 層
（D7 邊界）、divergence 自動歸因敘事、跨 run 趨勢比對（defer）。

計分模型（設計稿 §2 逐條映射 ADR 條文，無新裁量）：
  * injected case（分母單位）= ``(signature, variant)`` × ``expects==must_detect``；
    probe / informational / companion 不入分母（只回報）。
  * episode = 一 case 一 episode（合成場景，D7 A2）。
  * temporal hit = 該 case 至少一筆歸因 alert 的 ``fire_offset_s`` ∈
    ``[fault_start, fault_end + tolerance]``；晚於天花板 = miss（D5：FN 不可被
    放寬窗洗成「晚但接住」）。fault window 血緣 = inject 報告 metadata 的
    ``fault_window_s``（PR-1 合成期導出；absence 變體上界開放 → 以該報告窗長收尾）。
  * FN = must_detect case 無 temporal hit 且該報告 ``unattributed_alerts`` 為空；
    unattributed 非空 → 該 case 記 ``indeterminate`` 非 FN（契約①「不計 FN、
    顯性列出」保留；契約修正 (b)：indeterminate 不再隱形放行 verdict）。
  * verdict 三態（D8.1 + 契約修正 (b)）：**FAIL** = FN>0（exit 1；FN 優先於
    indeterminate）；**INDETERMINATE** = FN==0 且 indeterminate>0（exit 1——
    歸因被聚合規則剝 label 遮蔽時不得偽裝 PASS；診斷逃生門＝驗證期把
    waveform_signature 加入該規則的 by() 子句〔修改後規則 ≠ 生產規則〕）；
    **PASS** = FN==0 且 indeterminate==0（exit 0）。
  * carve-out（D1/OQ1）：容差檔 ``carve_outs``（鍵 = case 的 ``fault_class``——
    分母排除必須在開火前可決定的 case 側身分）→ 從分母排除 + 獨立列出（dual-
    control 人工審軌）。
  * fan-out ratio（D7/R3-1）= 每 episode 的 series-level 命中筆數；報告分位數
    （p50/p90/max）、只揭露不 gate、無魔術閾值。
  * no-silent-caps 守恆（D8.4）：``scored 分母 + indeterminate + carve-out 排除
    == 總 must_detect case 數`` 且 ``hits + FN == scored 分母``——違反 = 工具自身
    bug → exit 2；分母的每一次縮小都顯性計數。零分母分流：**carve-out 致零分母
    → exit 2**（設定面問題）；**indeterminate 致零分母 → verdict INDETERMINATE
    （exit 1）**（偵測面真相、非 operational error）。

容差矩陣（D5 防 gerrymandering 機械化）：
  * ``defaults:`` severity→ceiling 天花板矩陣（pre-commitment；``default`` 鍵
    必填 = 無/未知 severity 的 fallback row）。
  * ``overrides:`` per alert-class 例外——``severity``（宣告天花板 row）+
    ``tolerance_s`` + ``justification`` + ``approved_by`` 全必填；工具強制
    ``tolerance_s <= defaults[severity]``、違者 exit 2；計分時若 alert 實際
    severity label 與宣告 row 矛盾 → exit 2（fail-loud，防宣告低 ceiling 行、
    實際吃高 ceiling）。
  * CLI 無任何就地放寬旋鈕；報告回顯全部生效容差與其來源（default/override）。

Exit codes（與 waveform_compile / inject_waveform 的 0/1/2 契約對齊）:
  0  verdict PASS（FN == 0 且 indeterminate == 0）
  1  verdict FAIL（任一 FN——D8.1 硬門檻）或 INDETERMINATE（FN==0 且
     indeterminate>0——需人工覆核、不得偽裝 PASS；報告照常輸出）
  2  operational（報告/容差檔缺或壞、schema 違規、override>ceiling、報告版本
     不容〔缺 fault_window_s→用新版 inject 重產〕、對帳守恆違反=工具 bug）

Usage:
  python3 scripts/tools/dx/waveform_score.py report1.json [report2.json ...] \
      --tolerances tolerances.yaml [--json] [--out score.json]

D8.2/D8.3 邊界（報告內建 disclaimer）：verdict 僅涵蓋 injected fault set，對未
注入 long tail 零保證；永不宣稱等價；單獨不得作為「可切換」go-signal（須配
passive soak）。fire-edge 入計分、resolve-edge 只揭露不計分（設計稿 §5）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import write_text_secure  # noqa: E402

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", ".."))
_DEFAULT_SCHEMA = os.path.join(
    _REPO_ROOT, "docs", "schemas", "waveform-tolerances.schema.json")

SCHEMA_VERSION = 1              # score 報告格式版本（R2-4 結構化 baseline）
DEFAULT_SEVERITY_ROW = "default"

SCOPE_DISCLAIMER = (
    "verdict 僅涵蓋 injected fault set（D8.1）——對未注入 long tail 零保證；"
    "永不宣稱等價（D8.2）；本報告單獨不得作為「可切換」go-signal，"
    "須配 passive soak（D8.3 HARD-BLOCK）")
NOT_COVERED = [
    "未注入的 long-tail 故障（D2 限制）",
    "通知送達／post-inhibition 層（Alertmanager group_by/inhibit/silence，D7）",
    "資料平面波形忠實度（DTW，遷移方自管，D7）",
    "resolve-edge 保真（只揭露 resolve_offset_s、不計分；action-plane 線）",
]


class ScoreInputError(Exception):
    """Operational error（輸入檔/schema/版本/容差違規）— exit 2。"""


class ScoreToolBug(Exception):
    """對帳守恆違反 = scorer 自身 bug — exit 2（絕不靜默出報告）。"""


# ── tolerances loading（D5 機械化） ──────────────────────────────────

def load_tolerances(path: str, schema: dict, jsonschema_mod) -> dict:
    """載入 + schema 驗證 + 語義檢查（override≤ceiling / severity row 存在 /
    重複鍵 fail-loud）。回傳 {defaults, overrides(by alert_class), carve_outs(by
    fault_class)}。任何違規 → ScoreInputError（exit 2）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ScoreInputError(f"容差矩陣檔讀取失敗 {path}: {exc}") from exc
    errors = sorted(jsonschema_mod.Draft7Validator(schema).iter_errors(doc),
                    key=lambda e: list(e.absolute_path))
    if errors:
        msgs = "; ".join(f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: "
                         f"{e.message}" for e in errors[:5])
        raise ScoreInputError(f"容差矩陣檔 {path} schema 違規: {msgs}")

    # ── code 層語義檢查（FIX-3/4）：--schema 可被換掉（空 schema 全放行），
    # 審計欄位的存在＋非空必須由 code 層自己咬；schema 驗證降為縱深。 ──
    defaults = doc.get("defaults")
    if not isinstance(defaults, dict) or "default" not in defaults:
        raise ScoreInputError(
            f"容差矩陣檔 {path}: defaults 必須是含 'default' fallback row 的映射"
            f"（code 層檢查，不依賴 schema）")
    overrides: dict[str, dict] = {}
    for o in doc.get("overrides") or []:
        if not isinstance(o, dict):
            raise ScoreInputError(f"容差矩陣檔 {path}: override 必須是映射")
        for fld in ("alert_class", "severity", "tolerance_s"):
            if o.get(fld) in (None, ""):
                raise ScoreInputError(
                    f"容差矩陣檔 {path}: override 缺 {fld}（code 層檢查）")
        for fld in ("justification", "approved_by"):
            if not str(o.get(fld) or "").strip():
                raise ScoreInputError(
                    f"容差矩陣檔 {path}: override {o.get('alert_class')!r} 缺非空 "
                    f"{fld}——審計軌跡必填（D5）；code 層強制、schema 只是縱深")
        cls = o["alert_class"]
        if cls in overrides:
            raise ScoreInputError(
                f"容差矩陣檔 {path}: overrides 重複 alert_class {cls!r}——後者會"
                f"靜默蓋前者、審計軌跡失真；請合併")
        if o["severity"] not in defaults:
            raise ScoreInputError(
                f"容差矩陣檔 {path}: override {cls!r} 宣告的 severity row "
                f"{o['severity']!r} 不存在於 defaults（可用: {sorted(defaults)}）")
        ceiling = defaults[o["severity"]]
        if o["tolerance_s"] > ceiling:
            raise ScoreInputError(
                f"容差矩陣檔 {path}: override {cls!r} tolerance_s={o['tolerance_s']} "
                f"超過 severity {o['severity']!r} 的天花板 {ceiling}——D5 防 "
                f"gerrymandering 硬限制，FN 不得被放寬窗洗成「晚但接住」；"
                f"要更寬的容差請走天花板矩陣重新協商（審計軌跡）")
        overrides[cls] = o
    carve_outs: dict[str, dict] = {}
    for c in doc.get("carve_outs") or []:
        if not isinstance(c, dict):
            raise ScoreInputError(f"容差矩陣檔 {path}: carve_out 必須是映射")
        for fld in ("fault_class", "reason", "approved_by"):
            if not str(c.get(fld) or "").strip():
                raise ScoreInputError(
                    f"容差矩陣檔 {path}: carve_out {c.get('fault_class')!r} 缺非空 "
                    f"{fld}——dual-control 審軌必填（D1）；code 層強制")
        fc = c["fault_class"]
        if fc in carve_outs:
            raise ScoreInputError(
                f"容差矩陣檔 {path}: carve_outs 重複 fault_class {fc!r}——請合併")
        carve_outs[fc] = c
    return {"defaults": defaults, "overrides": overrides, "carve_outs": carve_outs}


def tolerance_for(alertname: str, severity: str | None, tol: dict) -> tuple[float, str]:
    """解析一筆 alert 的生效容差 → (tolerance_s, source 字串)。

    override 優先（並驗宣告 severity row 與實際 label 一致——矛盾 fail-loud）；
    否則 defaults[severity]；severity label 缺（None）→ defaults['default']
    fallback row；severity label 存在但不在 defaults rows → fail-loud（FIX-5：
    未知 severity 靜默落最寬 default row 是洗綠路徑——矩陣須對在場 severity
    顯性 pre-commit）。"""
    if severity is not None and severity not in tol["defaults"]:
        raise ScoreInputError(
            f"alert {alertname!r} 的 severity label {severity!r} 不在容差矩陣 "
            f"defaults rows（可用: {sorted(tol['defaults'])}）——未知 severity 不得"
            f"靜默落 default row；請在矩陣 defaults 顯性補該 row（pre-commitment）"
            f"或修正規則 label")
    ov = tol["overrides"].get(alertname)
    row = severity if severity is not None else DEFAULT_SEVERITY_ROW
    if ov is not None:
        declared = ov["severity"]
        if declared != row:
            raise ScoreInputError(
                f"override {alertname!r} 宣告 severity row {declared!r}，但實際 "
                f"alert 的 severity label 解析為 {row!r}（label={severity!r}）——"
                f"宣告與實況矛盾會讓 override 吃錯天花板 row；修正容差檔")
        return float(ov["tolerance_s"]), f"override({declared})"
    if severity is None:
        return float(tol["defaults"][DEFAULT_SEVERITY_ROW]), "default:default(no-severity-label)"
    return float(tol["defaults"][severity]), f"default:{severity}"


# ── inject 報告載入 ──────────────────────────────────────────────────

_REPORT_REQUIRED_KEYS = ("tool", "records", "window", "metadata", "unattributed_alerts")


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def load_report(path: str) -> dict:
    """載入 + 結構驗證 PR-2 inject 報告；版本不容（缺 fault_window_s）→ exit 2。"""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoreInputError(f"inject 報告讀取失敗 {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("tool") != "inject-waveform":
        raise ScoreInputError(
            f"{path} 不是 inject_waveform 報告（tool != 'inject-waveform'）")
    missing = [k for k in _REPORT_REQUIRED_KEYS if k not in doc]
    if missing:
        raise ScoreInputError(f"inject 報告 {path} 缺必要欄位: {missing}")
    for i, entry in enumerate(doc["metadata"].get("series") or []):
        if "fault_window_s" not in entry:
            raise ScoreInputError(
                f"inject 報告 {path} 的 metadata 缺 fault_window_s——報告版本不容"
                f"（PR-3 前的 inject 產物）；請用新版 inject_waveform 重產報告")
        fwv = entry["fault_window_s"]
        if fwv is None:
            continue
        # 血緣最小完整性（FIX-7）：型別 + 單調性。不做完整報告 schema——
        # producer（inject_waveform）與 consumer 同 repo 同版（premise 註記）。
        ok = (isinstance(fwv, list) and len(fwv) == 2 and _is_num(fwv[0])
              and (fwv[1] is None or _is_num(fwv[1])))
        if ok and fwv[1] is not None and fwv[0] > fwv[1]:
            ok = False
        if not ok:
            raise ScoreInputError(
                f"inject 報告 {path} metadata series#{i} 的 fault_window_s 形狀"
                f"非法（{fwv!r}）——需 [start, end] / [start, null]、數值、"
                f"start <= end；報告損壞或版本不容")
    return doc


def _metadata_window_index(report: dict, path: str) -> dict:
    """metadata.series → {(signature_index, variant, series-label): fault_window_s}。
    companion 排除；同鍵重複 = 對帳歧義 → fail-loud。"""
    idx: dict[tuple, list] = {}
    for entry in report["metadata"].get("series") or []:
        if entry.get("expects") == "companion":
            continue
        key = (entry["signature_index"], entry["variant"],
               (entry.get("labels") or {}).get("series"))
        if key in idx:
            raise ScoreInputError(
                f"inject 報告 {path} metadata 對帳歧義：重複 series 鍵 {key}——"
                f"fault_window 血緣無法唯一對應")
        idx[key] = entry["fault_window_s"]
    return idx


# ── 計分核心（純函式；unit-testable） ─────────────────────────────────

def score_case(rec: dict, fault_window, span_s: int, unattributed_nonempty: bool,
               tol: dict) -> dict:
    """單一 must_detect case 的 temporal-match 判定（carve-out 由 caller 先攔）。

    回傳 case dict：status ∈ {hit, fn, indeterminate} + hits 明細（含每筆生效
    容差與來源）+ fn_reason / indeterminate_reason。"""
    case = {
        "signature_index": rec["signature_index"],
        "fault_class": rec["fault_class"],
        "metric": rec["metric"],
        "variant": rec["variant"],
        "series": rec.get("series"),
        "expects": rec["expects"],
        "fault_window_s": list(fault_window) if fault_window is not None else None,
        "hits": [],
        "misses": [],
    }
    if fault_window is None:
        case["status"] = "indeterminate"
        case["indeterminate_reason"] = (
            "fault_window 無法定義（staleness_tail 截斷吃光 hold 段；見 pack "
            "auto_adjustments）——無窗可對、顯性列出待人工驗證")
        return case

    start = fault_window[0]
    end = fault_window[1] if fault_window[1] is not None else span_s
    case["effective_window_s"] = [start, end]
    for a in rec.get("alerts") or []:
        severity = (a.get("labels") or {}).get("severity")
        tol_s, source = tolerance_for(a["alertname"], severity, tol)
        entry = {
            "alertname": a["alertname"],
            "fire_offset_s": a["fire_offset_s"],
            "resolve_offset_s": a.get("resolve_offset_s"),  # 只揭露不計分（§5）
            "tolerance_s": tol_s,
            "tolerance_source": source,
            "labels": a.get("labels") or {},
        }
        if start <= a["fire_offset_s"] <= end + tol_s:
            case["hits"].append(entry)
        else:
            entry["outside"] = (
                {"late_by_s": a["fire_offset_s"] - (end + tol_s)}
                if a["fire_offset_s"] > end + tol_s
                else {"early_by_s": start - a["fire_offset_s"]})
            case["misses"].append(entry)

    if case["hits"]:
        case["status"] = "hit"
    elif unattributed_nonempty:
        case["status"] = "indeterminate"
        case["indeterminate_reason"] = (
            "unattributed alerts 非空（聚合型規則剝離歸因 label）——可能有 alert "
            "接住但無法歸因到本 case；記 indeterminate 非 FN（契約①），"
            "顯性列出待人工驗證")
    else:
        case["status"] = "fn"
        case["fn_reason"] = ("no_fire" if not rec.get("alerts")
                             else "fired_outside_window")
    return case


def _percentiles(values: list[int]) -> dict:
    """fan-out 分位數（p50/p90/max；nearest-rank，無魔術警示閾值——只揭露）。"""
    if not values:
        return {"p50": None, "p90": None, "max": None}
    v = sorted(values)

    def rank(p: float):
        return v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))]

    return {"p50": rank(0.5), "p90": rank(0.9), "max": v[-1]}


def score(reports: list[tuple[str, dict]], tol: dict, *,
          tolerances_path: str | None = None, schema_path: str | None = None) -> dict:
    """全量計分 → score 報告 dict（verdict 由 caller 讀 summary 判 exit）。

    守恆（no-silent-caps，D8.4）：任何 case 不得靜默蒸發——違反丟 ScoreToolBug。"""
    cases: list[dict] = []
    carved: list[dict] = []
    seen_case_keys: set[tuple] = set()
    inputs_meta = []
    total_must_detect = 0
    not_scored = 0

    for path, report in reports:
        win_idx = _metadata_window_index(report, path)
        span_s = report["window"]["span_s"]
        unattributed = report.get("unattributed_alerts") or []
        inputs_meta.append({
            "path": path,
            "pack_id": report.get("pack_id"),
            "window_span_s": span_s,
            "records_total": len(report["records"]),
            "unattributed_count": len(unattributed),
        })
        for rec in report["records"]:
            if rec["expects"] != "must_detect":
                not_scored += 1
                continue  # probe / informational 不入分母（顯性計數於 summary）
            total_must_detect += 1
            key = (path, report.get("pack_id"), rec["signature_index"],
                   rec["variant"], rec.get("series"))
            if key in seen_case_keys:
                raise ScoreInputError(
                    f"同一 case 被重複輸入：{key}——reports 參數是否把同一份報告"
                    f"傳了兩次？每份 inject 報告只能傳一次")
            seen_case_keys.add(key)

            mkey = (rec["signature_index"], rec["variant"], rec.get("series"))
            if mkey not in win_idx:
                raise ScoreInputError(
                    f"inject 報告 {path}: record {mkey} 在 metadata 找不到對應 "
                    f"fault_window——records↔metadata 血緣斷裂（報告損壞？）")
            if rec["fault_class"] in tol["carve_outs"]:
                carved.append({
                    "report": path,
                    "signature_index": rec["signature_index"],
                    "fault_class": rec["fault_class"],
                    "variant": rec["variant"],
                    "series": rec.get("series"),
                    "reason": tol["carve_outs"][rec["fault_class"]]["reason"],
                })
                continue
            case = score_case(rec, win_idx[mkey], span_s, bool(unattributed), tol)
            case["report"] = path
            case["pack_id"] = report.get("pack_id")
            cases.append(case)

    hits = [c for c in cases if c["status"] == "hit"]
    fns = [c for c in cases if c["status"] == "fn"]
    indeterminate = [c for c in cases if c["status"] == "indeterminate"]

    # ── 守恆 assert（違反 = 工具自身 bug → exit 2，絕不靜默出報告） ──
    scored = len(hits) + len(fns)
    if scored + len(indeterminate) + len(carved) != total_must_detect:
        raise ScoreToolBug(
            f"對帳守恆違反：scored({scored}) + indeterminate({len(indeterminate)}) "
            f"+ carved({len(carved)}) != 總 must_detect case 數({total_must_detect})"
            f"——有 case 靜默蒸發，scorer 自身 bug")
    if len(cases) + len(carved) != total_must_detect:
        raise ScoreToolBug("cases+carved 總數與 must_detect 分母不守恆——scorer 自身 bug")
    if scored == 0 and not indeterminate:
        # 全 carve-out（或空集）致零分母 = 設定面問題 → operational（exit 2）；
        # indeterminate 致零分母改走 INDETERMINATE verdict（偵測面真相、exit 1）。
        raise ScoreInputError(
            f"scored 分母為 0（總 must_detect={total_must_detect}、carve-out "
            f"{len(carved)}、indeterminate 0）——零分母的 catch-rate 是 vacuous "
            f"green，不得產出 verdict；檢查 carve-out 清單/注入報告")

    if fns:
        verdict = "FAIL"                       # FN 優先於 indeterminate
        verdict_reason = (f"{len(fns)} 筆 must_detect case 漏接"
                          f"（D8.1 FN=0 硬門檻）")
    elif indeterminate:
        verdict = "INDETERMINATE"
        verdict_reason = (
            f"{len(indeterminate)} 筆 case 因聚合規則剝 label 無法自動歸因——"
            f"需人工覆核、不得偽裝 PASS（契約修正 (b)）；診斷逃生門＝驗證期把 "
            f"waveform_signature 加入該規則的 by() 子句（修改後規則≠生產規則）")
    else:
        verdict = "PASS"
        verdict_reason = "FN == 0 且 indeterminate == 0（injected-set 內）"

    # FIX-10：不同檔名但同 (pack_id, seed) 的多份報告 = 同一注入重複入分母
    # （膨脹風險）——只警示不擋（可能是刻意重跑，但必須顯性）。
    warnings: list[str] = []
    seen_pack_seed: dict[tuple, str] = {}
    for path, report in reports:
        pkey = (report.get("pack_id"), report.get("seed"))
        if pkey in seen_pack_seed and seen_pack_seed[pkey] != path:
            warnings.append(
                f"重複 pack 輸入：{path} 與 {seen_pack_seed[pkey]} 同 (pack_id, "
                f"seed)={pkey}——同一注入的 case 會重複膨脹分母；確認非誤傳")
        else:
            seen_pack_seed.setdefault(pkey, path)
    fanout_values = [len(c["hits"]) for c in hits]
    effective = sorted(
        {(h["alertname"], h["tolerance_s"], h["tolerance_source"])
         for c in cases for h in c["hits"] + c["misses"]})
    return {
        "tool": "waveform-score",
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "warnings": warnings,
        "scope": {
            "injected_set_only": True,
            "disclaimer": SCOPE_DISCLAIMER,
            "not_covered": NOT_COVERED,
        },
        "inputs": inputs_meta,
        "tolerances": {
            "path": tolerances_path,
            "schema_path": schema_path,
            "defaults": tol["defaults"],
            "overrides": sorted(tol["overrides"].values(),
                                key=lambda o: o["alert_class"]),
            "carve_outs": sorted(tol["carve_outs"].values(),
                                 key=lambda c: c["fault_class"]),
        },
        "effective_tolerances": [
            {"alert_class": a, "tolerance_s": t, "source": s}
            for a, t, s in effective],
        "summary": {
            "must_detect_total": total_must_detect,
            "carved_out": len(carved),
            "indeterminate": len(indeterminate),
            "scored_denominator": scored,
            "not_scored_probe_informational": not_scored,
            "hits": len(hits),
            "false_negatives": len(fns),
            "catch_rate": (len(hits) / scored) if scored else None,
            "fanout_ratio": _percentiles(fanout_values),
        },
        "cases": cases,
        "false_negatives": fns,
        "indeterminate_cases": indeterminate,
        "carve_outs_applied": carved,
    }


# ── CLI ──────────────────────────────────────────────────────────────

def _print_human(report: dict) -> None:
    s = report["summary"]
    rate = "" if s["catch_rate"] is None else " = {:.1%}".format(s["catch_rate"])
    print(f"verdict: {report['verdict']}  "
          f"(catch {s['hits']}/{s['scored_denominator']}{rate}, "
          f"FN={s['false_negatives']})")
    print(f"  reason: {report['verdict_reason']}")
    print(f"  must_detect 總數 {s['must_detect_total']} = scored "
          f"{s['scored_denominator']} + indeterminate {s['indeterminate']} + "
          f"carve-out {s['carved_out']}（守恆）；probe/informational 不入分母 "
          f"{s['not_scored_probe_informational']} 筆")
    for c in report["false_negatives"]:
        print(f"  [FN] {c['pack_id']} sig{c['signature_index']} "
              f"{c['fault_class']} {c['variant']}"
              f"{'/' + c['series'] if c['series'] else ''} — {c['fn_reason']}")
    if report["indeterminate_cases"]:
        print(f"  ⚠️ indeterminate {s['indeterminate']} case——聚合規則剝 label、"
              f"無法自動歸因；需人工覆核（verdict 不得偽裝 PASS；診斷逃生門見 "
              f"reason 行——詳 --json）")
    if report["carve_outs_applied"]:
        print(f"  carve-out 排除 {s['carved_out']} case（dual-control 人工審）")
    fr = s["fanout_ratio"]
    print(f"  fan-out ratio: p50={fr['p50']} p90={fr['p90']} max={fr['max']}"
          f"（只揭露不 gate）")
    print(f"  scope: {report['scope']['disclaimer']}")


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="waveform catch-rate 計分器（ADR-030 PR-3）：inject JSON 報告 + "
                    "容差矩陣 → temporal-match → catch-rate + FN + verdict"
                    "（0=PASS / 1=FAIL / 2=operational）")
    parser.add_argument("reports", nargs="+",
                        help="inject_waveform.py 產出的 JSON 報告路徑（1..N 份）")
    parser.add_argument("--tolerances", required=True,
                        help="容差矩陣 YAML（D5 兩段式：defaults 天花板 + overrides "
                             "+ carve_outs；schema: docs/schemas/waveform-tolerances"
                             ".schema.json）")
    parser.add_argument("--schema", default=_DEFAULT_SCHEMA,
                        help="容差矩陣 JSON Schema 路徑（預設 docs/schemas/"
                             "waveform-tolerances.schema.json）")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="stdout 輸出機器可讀 JSON（R2-4 結構化 baseline）")
    parser.add_argument("--out", help="另將 JSON 報告寫到檔案（write_text_secure）")
    args = parser.parse_args()

    # Lazy import：--help / bad-flag 路徑在無 jsonschema 環境也要動（sweep 契約）。
    try:
        import jsonschema
    except ImportError:
        print("ERROR: jsonschema not installed — `pip install jsonschema` "
              "(CI installs it in the Python Tests dep step).", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        with open(args.schema, encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot load schema {args.schema}: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        tol = load_tolerances(args.tolerances, schema, jsonschema)
        reports = [(p, load_report(p)) for p in args.reports]
        result = score(reports, tol, tolerances_path=args.tolerances,
                       schema_path=args.schema)
    except (ScoreInputError, ScoreToolBug) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except (KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: inject 報告/容差檔 shape 異常（{type(exc).__name__}: {exc}）"
              f"——報告版本不容或檔案損壞", file=sys.stderr)
        return EXIT_CALLER_ERROR

    for w in result.get("warnings") or []:
        print(f"WARNING: {w}", file=sys.stderr)

    try:
        report_json = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.out:
            write_text_secure(args.out, report_json + "\n")
        if args.json_output:
            print(report_json)
        else:
            _print_human(result)
    except OSError as exc:
        print(f"ERROR: 報告輸出失敗: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    return EXIT_OK if result["verdict"] == "PASS" else EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
