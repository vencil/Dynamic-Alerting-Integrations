#!/usr/bin/env python3
"""inject_waveform.py — fault-waveform 注入執行器（ADR-030 決策層驗證 PR-2）

管線：waveform pack（SME 盲寫、PR-1 schema）→ 函式級呼叫 PR-1 編譯
（validate → synthesize → materialize_vm）→ 注入隔離 vmsingle →
``vmalert -replay`` 跑候選規則集 → 讀回 ``ALERTS`` → 輸出 per-signature×variant
開火紀錄（PR-3 catch-rate 的原料）。本工具**不算 catch-rate、不做 temporal-match**
（fault-window 不進 records——``metadata`` 的 ``fault_window_s`` 直帶秒級窗，
PR-1 合成期導出、PR-3 scorer 直接消費）。

輸出紀錄（--json / --out）每筆至少含：
  signature_index / fault_class / metric / variant / series(fanout) / expects /
  fired / alerts[{alertname, fire_offset_s, resolve_offset_s, labels}]。
offset 一律相對注入原點（window start == 波形 index 0），跨 run 可比。
pack schema 沒有 expected-alert 欄位（盲寫治理：SME 不知規則）——期望語義只有
``expects``（must_detect / informational / probe），配對哪條 alert 是 PR-3 的事。

歸因（attribution）：合成期每條 series 注入 ``waveform_signature`` +
``waveform_variant``（fanout 另有 ``series``）identity labels；讀回以此三元組
exact-match 歸因（對 records 単射、無雙重歸因），topology labels 留 sanity。
聚合型規則（``sum by`` 等）剝掉 identity labels → 進 unattributed（誠實限制）。

讀回網格誠實化：讀回取樣網格 = STEP（30s）；規則 eval cadence = 各 group 自己的
``interval``（**不正規化**——eval cadence 是受測遷移保真的一部分；缺省 = vmalert
預設 1m）。非 30s group 的 ``firing_sample_count`` / ``resolve_offset_s`` 有
讀回網格偏差（PR-3 須感知；報告的 ``rules.groups[].interval_s`` 供對帳）。

跨 run 隔離（explicit trade-off）：
  * 沿用 #968 的結論：``delete_series`` 有 async-tombstone race、run_id label 會被
    聚合型規則剝離 → 對任意候選規則的 ALERTS 讀回不 robust。
  * 本工具改用 **時間窗位移 + 注入前全量殘留 pre-check（fail-loud）**：
    - pre-check：目標窗 ``{__name__!=""}`` query_range **必須為空**——封掉
      metric 改名殘留、非本 pack 來源殘留、候選規則引用任意 metric 整類逃逸；
      非空 → exit 2 + 教人換窗/換 VM（絕不靜默混樣本）。
    - slot 空間高位分區：窗位移一律基於 ``AUTO_SLOT_BASE_S``（T0+6,000 萬秒，
      2025-10），與 #968 parity/replay 測試的 slot 空間（T0 起、worker×1000×3600
      內）結構性隔離；``--window-offset-s``（auto 或整數）語義=**相對此 base**。
      窗一律須整段落在過去（now-1h 前）——VM 對未來時間戳查詢回空，未來窗會被
      殘留掃描誤判為乾淨 → guard fail-loud。
    - ``auto``（預設）由 slot 0 起以 stride（span 取整小時 +1h 邊距）遞增探測第
      一個乾淨窗（同 VM 狀態 + 同輸入 ⇒ 同 offset；與測試 harness 併用時自動跳窗）。
    - 殘餘風險：**同一台 vmsingle 勿並發多個 inject**（pre-check→import 間
      TOCTOU）；slot 掃描上限 512，掃完仍髒 → exit 2 換 VM。

D8.4 fail-loud：schema 無效(1)；binary 缺 / VM 不可達 / 零注入行 / import・flush・
replay 失敗 / 殘留窗 / 注入後在場驗證失敗 / replay 正控（sentinel）沒 fire →
一律 exit 2 + 明確 stderr。「候選規則沒 fire」是合法資料（fired:false、exit 0），
與管線壞掉（exit 2）絕不混淆。兩道正控分工：
  * 注入後在場驗證——逐 series 驗首/末樣本時間戳在場 + 窗內 series 總數==注入數
    （ingest 靜默丟樣本 → vacuous green 的防線）；
  * ``vector(1)`` sentinel alert——只證 replay→remoteWrite→查詢鏈路活著（防
    「全部 fired:false」的鏈路 no-op），防不到 ingest 層，兩者缺一不可。

Exit codes (scripts/tools/_lib_exitcodes.py，與 waveform_compile.py 對齊):
  0  OK（管線完整跑完；有沒有 fire 都是資料）
  1  schema violation / governance gate（pack 問題，退回修 pack）
  2  bad invocation / 環境・管線 operational error

Usage:
  python3 scripts/tools/dx/inject_waveform.py pack.yaml --rules rules.d/ --json
  python3 scripts/tools/dx/inject_waveform.py pack.yaml --rules cand.yaml \
      --vm-url http://localhost:8428 --seed 1 --out report.json

依賴：tests/rulepacks/vm_harness.py（importlib 檔案路徑載入；不得在 ``python -O``
下執行——harness 以 assert 訊號失敗，-O 會把失敗變靜默，工具會直接拒跑）。
"""
from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
import _waveform_lib as wf  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import write_text_secure  # noqa: E402

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

# Repo-root-relative defaults: dx -> tools -> scripts -> <root>
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", ".."))
_DEFAULT_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "waveform-pack.schema.json")
_VM_HARNESS_PATH = os.path.join(_REPO_ROOT, "tests", "rulepacks", "vm_harness.py")

DEFAULT_VM_URL = "http://localhost:8428"
DEFAULT_TAIL_S = 600           # 資料結束後的觀測尾巴（staleness / for: 延後開火）
DEFAULT_REPLAY_TIMEOUT_S = 300
MAX_AUTO_SLOTS = 512           # 位移最多探測的 slot 數（掃完仍髒 → 換 VM）

# 注入窗高位分區 base（相對 T0，秒）：#968 parity/replay 測試佔用 T0 起的低位
# slot 空間（worker_offset×3600 + slot×3600，gw≤16 實務 < 5,760 萬秒；實測 slot 0
# 就有 #968 殘留）——inject 一律墊高到 T0+6,000 萬秒（≈1.9 年、2025-10）之後，
# 結構性分區。⚠️ 不能再高到跨過 now：VM 對「未來時間戳」的查詢回空
# （-search.latencyOffset 語義，親測 2029 窗 query_range 空回）——未來窗會被
# 殘留掃描誤判為乾淨、在場驗證必炸；_assert_window_in_past 把這條釘成 guard。
AUTO_SLOT_BASE_S = 60_000_000
FUTURE_GUARD_MARGIN_S = 3600   # 窗尾距 now 的最小安全邊距（latencyOffset/時鐘偏斜）

# 全量殘留 / 在場驗證用 selector：窗內任何 series（含 ALERTS）都要現形。
ALL_SERIES_SELECTOR = '{__name__!=""}'

# replay 正控 sentinel（防 vacuous green）；與候選規則撞名/偽裝 → exit 2
SENTINEL_GROUP = "vibe-waveform-inject-sentinel"
SENTINEL_ALERT = "VibeWaveformInjectSentinel"

# 歸因 identity 保留鍵：候選規則靜態 labels 不得覆寫（偽造歸因）。
RESERVED_LABEL_KEYS = ("waveform_signature", "waveform_variant", "series")

# record→alert 鏈可見性：vmalert 官方僅要求 rulesDelay >= flushInterval，但
# vmsingle 剛 ingest 的樣本要 ~ -inmemoryDataFlushInterval（預設 5s）才可搜尋——
# 親測 rulesDelay=2s 鏈式 alert 不 fire、>=6s fire。預設取 6（顯式 pin，不靠
# 引擎預設偶然）；e2e 鏈式 case 把此行為釘成 regression。
REMOTE_WRITE_FLUSH_INTERVAL_S = 1
DEFAULT_RULES_DELAY_S = 6


class InjectError(Exception):
    """Operational / pipeline error — maps to EXIT_CALLER_ERROR (2)."""


# ── candidate rules loading ──────────────────────────────────────────

def collect_rule_files(rules_path: str) -> list[str]:
    """--rules 可為單一規則檔或目錄（目錄取排序後的 *.yml / *.yaml）。"""
    p = Path(rules_path)
    if p.is_file():
        return [str(p)]
    if p.is_dir():
        files = sorted(str(f) for f in list(p.glob("*.yaml")) + list(p.glob("*.yml")))
        if not files:
            raise InjectError(f"--rules 目錄 {rules_path} 內沒有任何 *.yaml / *.yml 規則檔")
        return files
    raise InjectError(f"--rules 路徑不存在: {rules_path}")


def _parse_group_interval(name: str, gname: str, grp: dict) -> int:
    """group `interval` → 秒。缺省 = vmalert 預設 -evaluationInterval=1m（60）。
    不正規化候選 interval——eval cadence 是受測遷移保真的一部分（讀回網格偏差
    見 module docstring）。"""
    raw = grp.get("interval")
    if raw is None:
        return 60
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    try:
        return wf.parse_duration(str(raw))
    except wf.WaveformInputError as exc:
        raise InjectError(
            f"候選規則檔 {name} group {gname!r}: interval {raw!r} 無法解析: {exc}") from exc


def _scan_reserved_labels(name: str, gname: str, rule: dict) -> None:
    """規則靜態 labels 偽裝掃描——alertname 頂 sentinel 名、或覆寫歸因 identity
    保留鍵（waveform_signature/waveform_variant/series）→ fail-loud（exit 2）。"""
    static = rule.get("labels")
    if not isinstance(static, dict):
        return
    if str(static.get("alertname", "")) == SENTINEL_ALERT:
        raise InjectError(
            f"候選規則檔 {name} group {gname!r}: 靜態 labels 以 alertname 偽裝保留名 "
            f"{SENTINEL_ALERT}（replay 正控 sentinel）——讀回正控會被汙染；請移除")
    bad = sorted(k for k in static if k in RESERVED_LABEL_KEYS)
    if bad:
        raise InjectError(
            f"候選規則檔 {name} group {gname!r}: 靜態 labels 使用保留鍵 {bad}"
            f"（waveform 歸因 identity 鍵）——會偽造/汙染 per-signature 歸因；請改名")


def parse_rules(named_texts: list[tuple[str, str]]) -> dict:
    """解析候選規則檔 → {groups, groups_meta, alertnames, record_names}。

    fail-loud：malformed YAML / 頂層非 groups 列表 / 跨檔重複 group name /
    沒有任何 alert 規則 / 與 sentinel 撞名 / 保留 label 偽裝 / interval 無法解析
    → InjectError（exit 2）。多檔以 groups 串接合併成單一 rules 文件（vmalert
    -rule 吃一個檔）。groups_meta 記各 group 的 interval_s（讀回網格偏差對帳）。
    """
    groups: list[dict] = []
    groups_meta: list[dict] = []
    alertnames: set[str] = set()
    record_names: set[str] = set()
    seen_groups: set[str] = set()
    for name, text in named_texts:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise InjectError(f"候選規則檔 {name} 不是有效 YAML: {exc}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("groups"), list) or not doc["groups"]:
            raise InjectError(
                f"候選規則檔 {name} 不是有效 vmalert 規則檔（頂層需非空 `groups:` 列表）")
        for grp in doc["groups"]:
            if not isinstance(grp, dict) or not grp.get("name"):
                raise InjectError(f"候選規則檔 {name} 內含缺 name 的 group")
            gname = str(grp["name"])
            if gname in seen_groups:
                raise InjectError(
                    f"候選規則集跨檔重複 group name {gname!r}（{name}）——合併後 vmalert "
                    f"行為未定義；請改名")
            seen_groups.add(gname)
            for rule in grp.get("rules") or []:
                if not isinstance(rule, dict):
                    raise InjectError(f"候選規則檔 {name} group {gname!r} 內含非 mapping 的 rule")
                _scan_reserved_labels(name, gname, rule)
                if rule.get("alert"):
                    alertnames.add(str(rule["alert"]))
                if rule.get("record"):
                    record_names.add(str(rule["record"]))
            groups.append(grp)
            groups_meta.append({"name": gname,
                                "interval_s": _parse_group_interval(name, gname, grp)})
    if not alertnames:
        raise InjectError(
            "候選規則集沒有任何 alert 規則——沒有可評估的開火目標（純 record 規則集"
            "無法產生 catch 紀錄）；請確認 --rules 指向候選『告警』規則")
    if SENTINEL_ALERT in alertnames or SENTINEL_GROUP in seen_groups:
        raise InjectError(
            f"候選規則集使用了保留名 {SENTINEL_ALERT} / {SENTINEL_GROUP}"
            f"（本工具的 replay 正控 sentinel）——請改名")
    return {"groups": groups, "groups_meta": groups_meta,
            "alertnames": sorted(alertnames), "record_names": sorted(record_names)}


def build_rules_text(groups: list[dict]) -> str:
    """合併 groups + 附掛 sentinel group（正控），dump 成單一 vmalert 規則文件。"""
    sentinel = {
        "name": SENTINEL_GROUP,
        "interval": f"{wf.STEP}s",
        "rules": [{"alert": SENTINEL_ALERT, "expr": "vector(1)", "for": "0s"}],
    }
    return yaml.safe_dump({"groups": list(groups) + [sentinel]},
                          allow_unicode=True, sort_keys=False)


# ── window / import-line arithmetic ──────────────────────────────────

def shift_import_lines(vm_text: str, offset_s: int) -> list[str]:
    """materialize_vm 輸出（`metric{labels} value ts_ms`）整體平移 offset_s 秒。
    丟棄註解/空行；值與 label 原封不動（波形資料本身不因位移而變）。"""
    out: list[str] = []
    for line in vm_text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        head, ts = line.rsplit(" ", 1)
        out.append(f"{head} {int(ts) + offset_s * 1000}")
    return out


def data_span_s(series_list: list) -> int:
    """波形資料本身跨越的秒數（最後一個樣本的相對位移）。"""
    max_len = max(len(s.samples) for s in series_list)
    return (max_len - 1) * wf.STEP


def offset_stride_s(window_span_s: int) -> int:
    """位移的 slot stride：窗長向上取整小時 + 1h 邊距（>> VM staleness）。"""
    return ((window_span_s // 3600) + 2) * 3600


def series_ts_bounds(lines: list[str]) -> dict[str, tuple[int, int]]:
    """import 行 → {series-id 字串: (首樣本 ts_ms, 末樣本 ts_ms)}（在場驗證用）。"""
    bounds: dict[str, tuple[int, int]] = {}
    for line in lines:
        head, ts = line.rsplit(" ", 1)
        sid = head.rsplit(" ", 1)[0]
        t = int(ts)
        lo, hi = bounds.get(sid, (t, t))
        bounds[sid] = (min(lo, t), max(hi, t))
    return bounds


# ── readback record assembly（純函式，unit-testable） ─────────────────

def assert_unique_series_identities(series_list: list) -> None:
    """Belt-and-suspenders：合成後全 series 完整 identity
    ``(metric, frozenset(labels))`` 必須唯一——撞名的兩條 series import 後在 VM 內
    合併成一條，讀回歸因與在場驗證全部失真。waveform_signature 注入後理論上不可撞；
    此處防未來合成路徑回歸（如 companion labels 覆寫出重複組合）。"""
    seen: dict[tuple, int] = {}
    for i, s in enumerate(series_list):
        ident = (s.metric, frozenset(s.labels.items()))
        if ident in seen:
            raise InjectError(
                f"合成 series identity 撞名：series #{seen[ident]} 與 #{i} 同為 "
                f"{s.metric}{wf._fmt_labels(s.labels)}——import 會靜默合併、歸因失真"
                f"（companion labels 覆寫或 fanout 組合撞名？）")
        seen[ident] = i


def build_records(series_list: list) -> list[dict]:
    """每條非 companion 合成 series 一筆紀錄骨架（companion 是配角、非偵測標的）。"""
    recs = []
    for s in series_list:
        if s.expects == "companion":
            continue
        recs.append({
            "signature_index": s.signature_index,
            "fault_class": s.fault_class,
            "metric": s.metric,
            "variant": s.variant,
            "series": s.labels.get("series"),
            "expects": s.expects,
            "labels": dict(sorted(s.labels.items())),
            "fired": False,
            "alerts": [],
        })
    return recs


def summarize_alert_series(metric_labels: dict, offsets: list[int],
                           span_s: int, step_s: int) -> dict:
    """一條 ALERTS(firing) series → 摘要。offset 相對注入原點（讀回樣本網格
    = STEP 精度；非 30s group 有網格偏差，見 module docstring）；
    resolve_offset_s = 最後一個 firing 樣本 + step；窗尾仍在 firing → None。"""
    labels = {k: v for k, v in metric_labels.items()
              if k not in ("__name__", "alertstate")}
    first, last = offsets[0], offsets[-1]
    resolved = last + step_s <= span_s
    return {
        "alertname": labels.get("alertname", ""),
        "fire_offset_s": first,
        "last_fire_offset_s": last,
        "resolve_offset_s": (last + step_s) if resolved else None,
        "firing_sample_count": len(offsets),
        "labels": labels,
    }


def attribute_alerts(records: list[dict], alerts: list[dict]) -> list[dict]:
    """把 alert 歸因到 (signature, variant[, fanout series]) 紀錄。

    歸因鍵 = **exact match** 合成期注入的 identity labels 三元組
    ``(waveform_signature, waveform_variant[, series])``——對 records 單射
    （signature_index×variant 唯一、fanout 由 series 消歧），單一 alert 至多歸因
    一筆，根除「雙簽章同 labels 異 metric → 雙重歸因」假陽性。
    簽章 topology labels 留 sanity：identity 鍵合但 topology 矛盾 → 不歸因。
    聚合型規則（``sum by`` 等）剝掉 identity labels → 進 unattributed
    （誠實限制不變）。回傳 unattributed 清單。

    ⚠️ PR-3 計分契約（Gemini #1043 盲區2 disposition）：unattributed = 「成功
    開火但歸因不明」（indeterminate），**不得**因 signature 找不到對應 alert 而
    逕判 0% catch（假 FN）。出路＝人工驗證；或（僅供歸因診斷）暫時把
    ``waveform_signature`` 加入該規則的 ``by()``/``on()`` 子句——修改後規則 ≠
    生產規則，其結果只用於歸因、不得回寫 catch-rate。"""
    unattributed = []
    for a in alerts:
        lb = a["labels"]
        matched = False
        for rec in records:
            if lb.get("waveform_signature") != str(rec["signature_index"]):
                continue
            if lb.get("waveform_variant") != rec["variant"]:
                continue
            if rec["series"] is not None and lb.get("series") != rec["series"]:
                continue
            sig_labels = {k: v for k, v in rec["labels"].items()
                          if k not in RESERVED_LABEL_KEYS}
            if any(lb.get(k) != v for k, v in sig_labels.items()):
                continue  # topology sanity：identity 合但 topology 矛盾 → 不歸因
            rec["alerts"].append(a)
            rec["fired"] = True
            matched = True
        if not matched:
            unattributed.append(a)
    return unattributed


# ── vm_harness loading（importlib 檔案路徑載入；非 pytest 環境） ──────

def _load_vm_harness():
    if not os.path.isfile(_VM_HARNESS_PATH):
        raise InjectError(
            f"vm_harness 不存在: {_VM_HARNESS_PATH}（本工具需 repo checkout 的 "
            f"tests/rulepacks/vm_harness.py——共用 #968 VM harness）")
    spec = importlib.util.spec_from_file_location("vm_harness", _VM_HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── pipeline ─────────────────────────────────────────────────────────

def _assert_window_in_past(ws: int, span_s: int) -> None:
    """窗一律須整段落在過去：VM 對未來時間戳的查詢回空（-search.latencyOffset
    語義）——未來窗會讓殘留掃描恆判「乾淨」、在場驗證與讀回全部 vacuous。"""
    now = int(time.time())
    if ws + span_s > now - FUTURE_GUARD_MARGIN_S:
        raise InjectError(
            f"注入窗跨到未來（窗尾 {ws + span_s} > now-{FUTURE_GUARD_MARGIN_S}s）——"
            f"VM 對未來時間戳查詢回空，讀回/在場驗證會失真；請用較小的 "
            f"--window-offset-s，或換一台乾淨 VM（auto 掃描空間已抵 now 邊界）")


def _resolve_window(client, span_s: int, offset_arg: str) -> int:
    """回傳通過殘留 pre-check 的 window offset（秒、相對 AUTO_SLOT_BASE_S）。

    pre-check = 目標窗 ``{__name__!=""}`` 全量空斷言（嚴格強於逐 selector 檢查：
    任何 series——含改名殘留、他 pack 殘留、ALERTS——都會現形）。dirty →
    InjectError（列出前幾個殘留 metric 名）。"""
    def residue(off: int) -> list[str]:
        ws = wf.T0 + AUTO_SLOT_BASE_S + off
        _assert_window_in_past(ws, span_s)
        res = client.query_range(ALL_SERIES_SELECTOR, ws, ws + span_s, wf.STEP)
        return sorted({r["metric"].get("__name__", "?") for r in res})

    if offset_arg != "auto":
        off = int(offset_arg)
        dirty = residue(off)
        if dirty:
            raise InjectError(
                f"目標時間窗 [base+{off}s, +{span_s}s] 已有殘留 series（如: "
                f"{', '.join(dirty[:5])}）——任何殘留樣本都會混進讀回、汙染 catch "
                f"紀錄。請改 --window-offset-s auto（自動找乾淨窗）、指定其他 "
                f"offset，或換一台乾淨 vmsingle")
        return off

    stride = offset_stride_s(span_s)
    for k in range(MAX_AUTO_SLOTS):
        off = k * stride
        if not residue(off):
            return off
    raise InjectError(
        f"auto 位移掃描 {MAX_AUTO_SLOTS} 個 slot（stride={stride}s）都有殘留——"
        f"這台 vmsingle 太髒；請換一台乾淨 VM 再注入")


def verify_ingest(client, series_list: list, lines: list[str], ws: int, we: int) -> None:
    """注入後在場驗證：flush 後、replay 前，逐 imported series identity 驗
    (a) exact labelset 查得到、(b) 首/末樣本時間戳在場（不比樣本數——query_range
    會 gap-fill 灌水），且 (c) 窗內 series 總數 == 注入 series 數（抓並發寫入者/
    TOCTOU）。缺 → InjectError（exit 2）。

    解析度誠實聲明：檢查走查詢網格 + staleness 填值——整條 / 頭 / 尾大段遺失
    （retention 靜默丟棄的典型形態）必抓；單一末樣本在 staleness 填值下可能漏抓
    （已知網格解析度限制）。sentinel 防不到這層（它只證 replay 鏈路）。"""
    bounds = series_ts_bounds(lines)
    problems = []
    for s in series_list:
        sid = f"{s.metric}{wf._fmt_labels(s.labels)}"
        b = bounds.get(sid)
        if b is None:
            problems.append(f"{sid}: 物化後沒有任何 import 行（該 series 全 gap？）")
            continue
        lo_ms, hi_ms = b
        expected_metric = {"__name__": s.metric,
                           **{k: str(v) for k, v in s.labels.items()}}
        matches = [r for r in client.query_range(sid, ws, we, wf.STEP)
                   if r["metric"] == expected_metric]
        if len(matches) != 1:
            problems.append(f"{sid}: 注入後查無此 series（ingest 靜默丟樣本？retention？）")
            continue
        ts_s = sorted(int(v[0]) for v in matches[0]["values"])
        if not ts_s or ts_s[0] * 1000 > lo_ms + wf.STEP * 1000:
            problems.append(f"{sid}: 首樣本（{lo_ms}ms）不在場——序列頭被丟")
        elif ts_s[-1] * 1000 < hi_ms - 1000:
            problems.append(f"{sid}: 末樣本（{hi_ms}ms）不在場——序列尾被丟")
    if not problems:
        total = len(client.query_range(ALL_SERIES_SELECTOR, ws, we, wf.STEP))
        if total != len(series_list):
            problems.append(
                f"窗內 series 總數 {total} != 注入 {len(series_list)}"
                f"（並發寫入者？pre-check 之後有人動了這個窗？）")
    if problems:
        raise InjectError(
            "注入後在場驗證失敗（ingest 靜默丟樣本 → 讀回將 vacuous green）：\n  - "
            + "\n  - ".join(problems)
            + "\n  （檢查 vmsingle -retentionPeriod=100y / 勿並發注入同一台 VM）")


def run_pipeline(args, pack: dict, parsed_rules: dict) -> dict:
    """驗證後管線：synthesize → 找窗 → import → 在場驗證 → replay → 讀回。
    回傳 report dict。任何環節失敗丟 InjectError / harness AssertionError
    （caller 轉 exit 2）。"""
    try:
        series = wf.synthesize_pack(pack, seed=args.seed, fanout=args.fanout)
        vm_text = wf.materialize_vm(series)
    except wf.WaveformInputError as exc:
        raise InjectError(f"pack 合成失敗: {exc}") from exc

    # 零注入行 fail-loud（對齊上游 staleness_tail 全截斷前例）——否則 pre-check
    # 「窗必空」對空注入恆真、後續全鏈 vacuous。
    if not shift_import_lines(vm_text, 0):
        raise InjectError(
            "pack 物化後零可注入行（全部樣本皆 gap？檢查 time_axis 的 "
            "dropout_pattern / staleness_tail 設定）——空注入的讀回毫無意義")
    assert_unique_series_identities(series)

    harness = _load_vm_harness()
    vmalert_bin = args.vmalert or harness.find_vmalert()
    if args.vmalert and not os.path.isfile(args.vmalert):
        raise InjectError(f"--vmalert 指定的 binary 不存在: {args.vmalert}")
    if vmalert_bin is None:
        raise InjectError(
            "找不到 vmalert binary（$VMALERT / PATH / /tmp/vm/vmalert-prod 皆無）——"
            "replay 無法執行；dev-container 內應有 /tmp/vm/vmalert-prod")

    client = harness.VMClient(args.vm_url)
    if not client.reachable():
        raise InjectError(
            f"VictoriaMetrics 不可達: {args.vm_url}/health——先起一台隔離 vmsingle"
            f"（需 -retentionPeriod=100y，見 #968），或用 --vm-url 指定")

    span_s = data_span_s(series) + args.tail_s
    offset = _resolve_window(client, span_s, args.window_offset_s)
    ws = wf.T0 + AUTO_SLOT_BASE_S + offset
    we = ws + span_s

    lines = shift_import_lines(vm_text, AUTO_SLOT_BASE_S + offset)
    client.import_prometheus(lines)
    client.flush()
    verify_ingest(client, series, lines, ws, we)

    rules_text = build_rules_text(parsed_rules["groups"])
    with tempfile.TemporaryDirectory(prefix="waveform_inject_") as tmp:
        harness.replay(vmalert_bin, rules_text, ws, we, Path(tmp),
                       pack["pack"]["id"], datasource_url=args.vm_url,
                       timeout_s=args.replay_timeout_s,
                       rules_delay_s=args.rules_delay_s,
                       remote_write_flush_interval_s=REMOTE_WRITE_FLUSH_INTERVAL_S)
    client.flush()

    res = client.query_range('ALERTS{alertstate="firing"}', ws, we, wf.STEP)
    alerts = []
    sentinel_fired = False
    for item in res:
        offsets = sorted(int(v[0]) - ws for v in item["values"])
        summary = summarize_alert_series(item["metric"], offsets, span_s, wf.STEP)
        if summary["alertname"] == SENTINEL_ALERT:
            sentinel_fired = True
            continue
        alerts.append(summary)
    if not sentinel_fired:
        raise InjectError(
            f"replay 正控失敗：sentinel alert {SENTINEL_ALERT}（vector(1)）沒有讀回任何 "
            f"firing 樣本——replay→remoteWrite→查詢鏈路 no-op，所有 fired:false 都不可信"
            f"（replay 沒寫回？remoteWrite 沒 flush？）")

    records = build_records(series)
    unattributed = attribute_alerts(records, alerts)
    return {
        "tool": "inject-waveform",
        "pack": args.pack,
        "pack_id": pack["pack"]["id"],
        "readback_signed_off": bool(pack["pack"].get("readback_signed_off")),
        "seed": args.seed,
        "fanout": args.fanout,
        "vm_url": args.vm_url,
        "rules": {
            "alertnames": parsed_rules["alertnames"],
            "record_names": parsed_rules["record_names"],
            "group_count": len(parsed_rules["groups"]),
            "groups": parsed_rules["groups_meta"],
        },
        "window": {
            "t0_epoch_s": wf.T0,
            "step_s": wf.STEP,
            "slot_base_s": AUTO_SLOT_BASE_S,
            "offset_s": offset,
            "start_epoch_s": ws,
            "end_epoch_s": we,
            "span_s": span_s,
            "tail_s": args.tail_s,
        },
        "series_imported": len(series),
        "companion_series": sum(1 for s in series if s.expects == "companion"),
        "sentinel": {"alertname": SENTINEL_ALERT, "fired": True},
        "records": records,
        "unattributed_alerts": unattributed,
        "metadata": wf.build_metadata(pack, series, seed=args.seed, fanout=args.fanout),
    }


# ── CLI ──────────────────────────────────────────────────────────────

def _print_human(report: dict) -> None:
    w = report["window"]
    print(f"OK: pack {report['pack_id']} 注入完成 "
          f"(window offset={w['offset_s']}s, span={w['span_s']}s, "
          f"start={w['start_epoch_s']}, seed={report['seed']})")
    print(f"  series 注入 {report['series_imported']} 條"
          f"（含 companion {report['companion_series']}）；"
          f"候選 alert 規則 {len(report['rules']['alertnames'])} 條")
    for rec in report["records"]:
        tag = f"[{rec['expects']}]"
        sid = f"sig{rec['signature_index']} {rec['fault_class']} {rec['variant']}"
        if rec["series"]:
            sid += f"/{rec['series']}"
        if rec["fired"]:
            first = min(a["fire_offset_s"] for a in rec["alerts"])
            names = ",".join(sorted({a["alertname"] for a in rec["alerts"]}))
            print(f"  {tag:<15} {sid:<40} FIRED @+{first}s ({names})")
        else:
            print(f"  {tag:<15} {sid:<40} no fire")
    print(f"  未歸因 alerts: {len(report['unattributed_alerts'])}"
          + ("（labels 被聚合剝離？詳 --json）" if report["unattributed_alerts"] else ""))


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="fault-waveform 注入執行器（ADR-030 PR-2）：pack → 編譯 → 注入隔離 "
                    "vmsingle → vmalert -replay 候選規則 → per-signature 開火紀錄"
                    "（PR-3 catch-rate 原料；不算 catch-rate、不做 temporal-match）")
    parser.add_argument("pack", help="waveform pack YAML 路徑（單一 pack）")
    parser.add_argument("--rules", required=True,
                        help="候選規則檔或目錄（vmalert 格式；規則來源與本工具解耦）")
    parser.add_argument("--vm-url", default=DEFAULT_VM_URL,
                        help=f"隔離 vmsingle base URL（預設 {DEFAULT_VM_URL}；"
                             f"需 -retentionPeriod=100y）")
    parser.add_argument("--vmalert",
                        help="vmalert binary 路徑（預設 $VMALERT → PATH → /tmp/vm/vmalert-prod）")
    parser.add_argument("--seed", type=int, default=wf.DEFAULT_SEED,
                        help=f"透傳 PRNG seed（決定性；預設 {wf.DEFAULT_SEED}）")
    parser.add_argument("--fanout", type=int, default=wf.DEFAULT_FANOUT,
                        help=f"透傳 fan-out 變體數（預設 {wf.DEFAULT_FANOUT}）")
    parser.add_argument("--schema", default=_DEFAULT_SCHEMA,
                        help="pack JSON Schema 路徑（預設 docs/schemas/waveform-pack.schema.json）")
    parser.add_argument("--allow-selftest", action="store_true",
                        help="放行 source: self-test-seed（僅供工具自測；"
                             "self-test seed 不得進入 catch-rate 素材）")
    parser.add_argument("--window-offset-s", default="auto",
                        help="注入時間窗位移（秒、相對高位分區 base T0+6e7s）或 auto"
                             "（預設）：auto 由 slot 0 起探測第一個無殘留的乾淨窗；"
                             "指定整數則 pre-check 髒即 exit 2")
    parser.add_argument("--tail-s", type=int, default=DEFAULT_TAIL_S,
                        help=f"資料結束後的觀測尾巴秒數（staleness / for: 延後開火；"
                             f"預設 {DEFAULT_TAIL_S}）")
    parser.add_argument("--rules-delay-s", type=int, default=DEFAULT_RULES_DELAY_S,
                        help="-replay.rulesDelay 秒數（record→alert 鏈可見性；須 >= "
                             f"remoteWrite.flushInterval={REMOTE_WRITE_FLUSH_INTERVAL_S}s；"
                             f"預設 {DEFAULT_RULES_DELAY_S}——vmsingle 剛 ingest 樣本 ~5s 才可搜尋）")
    parser.add_argument("--replay-timeout-s", type=int, default=DEFAULT_REPLAY_TIMEOUT_S,
                        help=f"vmalert -replay 子行程 timeout（預設 {DEFAULT_REPLAY_TIMEOUT_S}s）")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="stdout 輸出機器可讀 JSON（PR-3 消費格式）")
    parser.add_argument("--out", help="另將 JSON 報告寫到檔案（write_text_secure）")
    args = parser.parse_args()

    if sys.flags.optimize:
        print("ERROR: 禁止在 python -O 下執行——vm_harness 以 assert 訊號管線失敗，"
              "-O 會把失敗變靜默（D8.4 fail-loud 破功）", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if args.fanout < 1:
        parser.error("--fanout must be >= 1")
    if args.tail_s < 0:
        parser.error("--tail-s must be >= 0")
    if args.rules_delay_s < REMOTE_WRITE_FLUSH_INTERVAL_S:
        parser.error(f"--rules-delay-s 必須 >= {REMOTE_WRITE_FLUSH_INTERVAL_S}"
                     f"（vmalert 要求 rulesDelay >= remoteWrite.flushInterval）")
    if args.window_offset_s != "auto":
        try:
            off = int(args.window_offset_s)
        except ValueError:
            parser.error("--window-offset-s 需為整數秒或 auto")
        if off < 0:
            parser.error("--window-offset-s 不可為負")

    # Lazy import：--help / bad-flag 路徑（exit-code sweep）在無 jsonschema 環境也要動
    # （waveform_compile.py / check_confd_schema.py precedent）。
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
        pack = wf.load_pack(args.pack)
    except wf.WaveformInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # 與 waveform_compile 同一套驗證閘（schema 兩層切 + selftest 治理 + 語義檢查）。
    issues = wf.validate_pack(pack, schema, jsonschema)
    issues.extend(wf.selftest_gate_issues(pack, args.allow_selftest))
    issues.extend(wf.semantic_issues(pack))
    if issues:
        for issue in issues:
            print(f"{args.pack}: {issue['message']}", file=sys.stderr)
        print(f"\n{len(issues)} violation(s) — 不注入無效 pack。", file=sys.stderr)
        return EXIT_VIOLATION

    try:
        rule_files = collect_rule_files(args.rules)
        named_texts = []
        for path in rule_files:
            try:
                named_texts.append((path, Path(path).read_text(encoding="utf-8")))
            except OSError as exc:
                raise InjectError(f"候選規則檔讀取失敗 {path}: {exc}") from exc
        parsed_rules = parse_rules(named_texts)
        report = run_pipeline(args, pack, parsed_rules)
    except (InjectError, AssertionError, OSError, ValueError, KeyError,
            subprocess.TimeoutExpired, http.client.HTTPException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # 聚合型規則剝除 identity labels 的開火進 unattributed——顯性警告、不只藏在
    # JSON（Gemini #1043 盲區2）；計分語義見 attribute_alerts docstring。
    if report.get("unattributed_alerts"):
        print(f"WARNING: {len(report['unattributed_alerts'])} 筆開火無法自動歸因"
              "（聚合型規則剝除 identity labels）——屬 indeterminate 非 FN，"
              "PR-3 計分不得當漏報；詳 attribute_alerts docstring。", file=sys.stderr)

    # 報告輸出也是管線的一環——寫檔失敗（--out 目錄不存在等）是 operational
    # error（exit 2），不得帶著成功報告假綠或炸 traceback。
    try:
        report_json = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if args.out:
            write_text_secure(args.out, report_json + "\n")
        if args.json_output:
            print(report_json)
        else:
            _print_human(report)
    except OSError as exc:
        print(f"ERROR: 報告輸出失敗: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
