"""test_waveform_score.py — catch-rate 計分器（ADR-030 PR-3）測試。

單元（免 VM）：temporal-match 邊界三態（窗內／窗尾+tolerance 內／超天花板）、
FN / indeterminate / carve-out 分類、override>ceiling fail-loud、守恆檢查、
fan-out 分位數、容差 schema 驗證、報告版本 guard——多以合成 inject 報告 dict
直打純函式 ``score()``。

e2e（需 VM + vmalert；skip-if-no-VM + ``WAVEFORM_SCORE_REQUIRE=1`` 旋鈕，語義
照抄 #968）：PR-2 inject（決定性 seed）→ score 全鏈。

poison teeth：拔容差檔 → exit 2；never-fire 規則 inject→score → verdict FAIL
（exit 1）非 crash；tolerance 設 0 → 晚 fire 的 case 變 FN（釘「天花板真的咬」）。
容差示意值只進 selftest fixture / 測試暫存檔（R2-2）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("jsonschema")
import jsonschema  # noqa: E402

import waveform_score as ws  # noqa: E402  (sys.path via tests/conftest.py)

_REPO = Path(__file__).resolve().parents[2]
_SCORE_CLI = _REPO / "scripts" / "tools" / "dx" / "waveform_score.py"
_INJECT_CLI = _REPO / "scripts" / "tools" / "dx" / "inject_waveform.py"
_FIXDIR = Path(__file__).parent / "fixtures" / "waveform"
_DISK = _FIXDIR / "selftest_disk_used_percent.yaml"
_RULES = _FIXDIR / "rules" / "selftest_disk.rules.yaml"
_TOL = _FIXDIR / "tolerances" / "selftest_tolerances.yaml"
_SCHEMA = _REPO / "docs" / "schemas" / "waveform-tolerances.schema.json"

sys.path.insert(0, str(_REPO / "tests" / "rulepacks"))
import vm_harness  # noqa: E402


def _run_score(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCORE_CLI), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def _run_inject(*args: str, timeout: int = 420) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_INJECT_CLI), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


# ── 合成 inject 報告 builder（免 VM 的計分單元測試素材） ────────────────

# 單元測試用容差（示意值只在測試素材；R2-2）
_TOLS = {"defaults": {"critical": 300, "warning": 600, "default": 900},
         "overrides": {}, "carve_outs": {}}
_FW = (300, 9270)   # 合成 case 的故障窗（onset 起點, hold 末樣本）


def _alert(name="CandidateA", fire=1000, severity="warning"):
    labels = {"alertname": name, "waveform_signature": "0",
              "waveform_variant": "base"}
    if severity is not None:
        labels["severity"] = severity
    return {"alertname": name, "fire_offset_s": fire,
            "last_fire_offset_s": fire + 60, "resolve_offset_s": fire + 90,
            "firing_sample_count": 3, "labels": labels}


def _record(sig=0, variant="base", series=None, expects="must_detect",
            fault_class="selftest-fault", alerts=()):
    return {"signature_index": sig, "fault_class": fault_class,
            "metric": "selftest_metric", "variant": variant, "series": series,
            "expects": expects, "labels": {}, "fired": bool(alerts),
            "alerts": list(alerts)}


def _meta(sig=0, variant="base", series=None, expects="must_detect", fw=_FW,
          hold_start=None):
    labels = {"series": series} if series else {}
    return {"signature_index": sig, "variant": variant, "labels": labels,
            "expects": expects,
            "fault_window_s": (list(fw) if fw is not None else None),
            "hold_start_s": hold_start}


def _report(records, metas, unattributed=(), span=12000, step=30):
    return {"tool": "inject-waveform", "pack_id": "synthetic-pack",
            "records": records, "window": {"span_s": span, "step_s": step},
            "metadata": {"series": metas},
            "unattributed_alerts": list(unattributed)}


def _score_one(rec, meta, unattributed=(), tol=_TOLS, span=12000):
    rep = _report([rec], [meta], unattributed, span)
    return ws.score([("synthetic.json", rep)], tol)


# ── temporal-match 邊界三態（窗內 / 窗尾+tolerance 內 / 超天花板） ─────

def test_hit_inside_window_boundaries():
    for fire in (300, 1000, 9270):          # 窗下界（onset 起點）含、窗尾含
        out = _score_one(_record(alerts=[_alert(fire=fire)]), _meta())
        assert out["cases"][0]["status"] == "hit", fire
        assert out["verdict"] == "PASS"


def test_hit_within_tolerance_after_window_end():
    # warning ceiling 600 → 窗尾 9270 + 600 = 9870 含（晚但接住）
    out = _score_one(_record(alerts=[_alert(fire=9870)]), _meta())
    case = out["cases"][0]
    assert case["status"] == "hit"
    assert case["hits"][0]["tolerance_s"] == 600
    assert case["hits"][0]["tolerance_source"] == "default:warning"


def test_miss_beyond_tolerance_ceiling_is_fn():
    # 9871 > 窗尾+tolerance —— D5：晚於天花板 = miss，不得洗成「晚但接住」
    out = _score_one(_record(alerts=[_alert(fire=9871)]), _meta())
    case = out["cases"][0]
    assert case["status"] == "fn"
    assert case["fn_reason"] == "fired_outside_window"
    assert case["misses"][0]["outside"] == {"late_by_s": 1}
    assert out["verdict"] == "FAIL"
    assert out["summary"]["false_negatives"] == 1


def test_early_fire_before_onset_is_not_a_hit():
    out = _score_one(_record(alerts=[_alert(fire=299)]), _meta())
    case = out["cases"][0]
    assert case["status"] == "fn"
    assert case["misses"][0]["outside"] == {"early_by_s": 1}


def test_no_fire_is_fn_and_verdict_fail():
    out = _score_one(_record(alerts=[]), _meta())
    assert out["cases"][0]["status"] == "fn"
    assert out["cases"][0]["fn_reason"] == "no_fire"
    assert out["verdict"] == "FAIL"


# ── indeterminate（契約①）/ carve-out / 分母語義 ──────────────────────

def test_unattributed_shadow_makes_nohit_indeterminate_not_fn():
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),   # hit（保分母>0）
            _record(sig=1, alerts=[])]                    # no-hit + unattributed
    metas = [_meta(sig=0), _meta(sig=1)]
    rep = _report(recs, metas, unattributed=[{"alertname": "Aggregated",
                                              "labels": {}}])
    out = ws.score([("r.json", rep)], _TOLS)
    by_sig = {c["signature_index"]: c for c in out["cases"]}
    assert by_sig[0]["status"] == "hit"           # 有 hit 的 case 不被遮蔽
    assert by_sig[1]["status"] == "indeterminate"
    assert "unattributed" in by_sig[1]["indeterminate_reason"]
    # 契約修正 (b)：indeterminate 不計 FN（契約①保留）但 verdict 三態=INDETERMINATE
    assert out["verdict"] == "INDETERMINATE"
    assert out["summary"]["false_negatives"] == 0
    assert out["summary"]["indeterminate"] == 1   # 顯性計數
    assert "人工覆核" in out["verdict_reason"]
    assert "waveform_signature" in out["verdict_reason"]   # 診斷逃生門


def test_carve_out_excluded_from_denominator_and_listed():
    tol = dict(_TOLS)
    tol["carve_outs"] = {"compliance-audit": {"fault_class": "compliance-audit",
                                              "reason": "合規類走 dual-control"}}
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=1, fault_class="compliance-audit", alerts=[])]
    rep = _report(recs, [_meta(sig=0), _meta(sig=1)])
    out = ws.score([("r.json", rep)], tol)
    assert out["summary"]["carved_out"] == 1
    assert out["summary"]["scored_denominator"] == 1
    assert out["summary"]["false_negatives"] == 0  # carve-out 不會變 FN
    assert out["carve_outs_applied"][0]["reason"] == "合規類走 dual-control"
    assert out["verdict"] == "PASS"


def test_probe_and_informational_not_in_denominator():
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=0, variant="oscillation", expects="probe", alerts=[]),
            _record(sig=1, expects="informational", alerts=[])]
    metas = [_meta(sig=0), _meta(sig=0, variant="oscillation", expects="probe"),
             _meta(sig=1, expects="informational")]
    out = ws.score([("r.json", _report(recs, metas))], _TOLS)
    assert out["summary"]["must_detect_total"] == 1
    assert out["summary"]["scored_denominator"] == 1


def test_conservation_identity_holds_across_mix():
    tol = dict(_TOLS)
    tol["carve_outs"] = {"cc": {"fault_class": "cc", "reason": "x"}}
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),          # hit
            _record(sig=1, alerts=[]),                            # FN
            _record(sig=2, fault_class="cc", alerts=[]),          # carved
            _record(sig=3, variant="noise", alerts=[])]           # indeterminate
    metas = [_meta(sig=0), _meta(sig=1), _meta(sig=2),
             _meta(sig=3, variant="noise")]
    rep = _report(recs, metas, unattributed=[])
    rep2 = _report([recs[3]], [metas[3]],
                   unattributed=[{"alertname": "Agg", "labels": {}}])
    out = ws.score([("a.json", rep), ("b.json", rep2)], tol)
    s = out["summary"]
    assert (s["scored_denominator"] + s["indeterminate"] + s["carved_out"]
            == s["must_detect_total"] == 5)
    assert s["hits"] + s["false_negatives"] == s["scored_denominator"]


def test_zero_scored_denominator_is_operational_error():
    """全部 case 被排除 → 零分母 catch-rate = vacuous green，不得產 PASS。"""
    tol = dict(_TOLS)
    tol["carve_outs"] = {"selftest-fault": {"fault_class": "selftest-fault",
                                            "reason": "x"}}
    rep = _report([_record(alerts=[])], [_meta()])
    with pytest.raises(ws.ScoreInputError, match="vacuous"):
        ws.score([("r.json", rep)], tol)


def test_absence_open_window_ends_at_report_span():
    rec = _record(alerts=[_alert(fire=11990)])
    out = _score_one(rec, _meta(fw=(330, None)), span=12000)
    assert out["cases"][0]["status"] == "hit"     # end=span → 窗內
    assert out["cases"][0]["effective_window_s"] == [330, 12000]
    rec2 = _record(alerts=[_alert(fire=12601)])   # span+600 之外 → late miss
    out2 = _score_one(rec2, _meta(fw=(330, None)), span=12000)
    assert out2["cases"][0]["status"] == "fn"


def test_null_fault_window_is_indeterminate():
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=1, alerts=[_alert(fire=1000)])]
    metas = [_meta(sig=0), _meta(sig=1, fw=None)]
    out = ws.score([("r.json", _report(recs, metas))], _TOLS)
    by_sig = {c["signature_index"]: c for c in out["cases"]}
    assert by_sig[1]["status"] == "indeterminate"
    assert "fault_window" in by_sig[1]["indeterminate_reason"]


def test_metadata_join_break_is_operational_error():
    rep = _report([_record(sig=7, alerts=[])], [_meta(sig=0)])
    with pytest.raises(ws.ScoreInputError, match="血緣"):
        ws.score([("r.json", rep)], _TOLS)


# ── tolerance 解析（override / severity row / fallback） ───────────────

def test_tolerance_for_default_rows():
    assert ws.tolerance_for("X", "critical", _TOLS) == (300, "default:critical")
    assert ws.tolerance_for("X", None, _TOLS) == (
        900, "default:default(no-severity-label)")


def test_unknown_severity_fails_loud_not_default_row():
    """FIX-5：severity label 存在但不在 defaults rows（如大小寫錯的 Critical）
    → fail-loud，不得靜默落最寬 default row（900s 洗綠路徑）。"""
    with pytest.raises(ws.ScoreInputError, match="Critical"):
        ws.tolerance_for("X", "Critical", _TOLS)
    # 經 score 全鏈也要炸（alert 帶未知 severity）
    rep_doc = _report([_record(alerts=[_alert(fire=1000, severity="Critical")])],
                      [_meta()])
    with pytest.raises(ws.ScoreInputError, match="severity"):
        ws.score([("r.json", rep_doc)], _TOLS)


def test_tolerance_override_applied_and_mismatch_fails_loud():
    tol = dict(_TOLS)
    tol["overrides"] = {"X": {"alert_class": "X", "severity": "warning",
                              "tolerance_s": 120, "justification": "j",
                              "approved_by": "a"}}
    assert ws.tolerance_for("X", "warning", tol) == (120, "override(warning)")
    with pytest.raises(ws.ScoreInputError, match="矛盾"):
        ws.tolerance_for("X", "critical", tol)   # 宣告 row 與實際 label 矛盾


def _write_tol(tmp_path, doc):
    p = tmp_path / "tol.yaml"
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return p


def _load_tol(path):
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    return ws.load_tolerances(str(path), schema, jsonschema)


def test_override_above_ceiling_fails_loud(tmp_path):
    p = _write_tol(tmp_path, {
        "defaults": {"warning": 600, "default": 900},
        "overrides": [{"alert_class": "X", "severity": "warning",
                       "tolerance_s": 601, "justification": "j",
                       "approved_by": "a"}]})
    with pytest.raises(ws.ScoreInputError, match="天花板"):
        _load_tol(p)


def test_tolerances_schema_violations_fail_loud(tmp_path):
    # 缺 default fallback row
    with pytest.raises(ws.ScoreInputError, match="schema"):
        _load_tol(_write_tol(tmp_path, {"defaults": {"warning": 600}}))
    # override 缺 justification（審計軌跡必填）
    with pytest.raises(ws.ScoreInputError, match="schema"):
        _load_tol(_write_tol(tmp_path, {
            "defaults": {"default": 900},
            "overrides": [{"alert_class": "X", "severity": "default",
                           "tolerance_s": 100, "approved_by": "a"}]}))
    # 重複 override alert_class
    with pytest.raises(ws.ScoreInputError, match="重複"):
        _load_tol(_write_tol(tmp_path, {
            "defaults": {"default": 900},
            "overrides": [
                {"alert_class": "X", "severity": "default", "tolerance_s": 1,
                 "justification": "j", "approved_by": "a"},
                {"alert_class": "X", "severity": "default", "tolerance_s": 2,
                 "justification": "j", "approved_by": "a"}]}))


def test_selftest_tolerances_fixture_is_schema_valid():
    tol = _load_tol(_TOL)
    assert "default" in tol["defaults"]


# ── fan-out 分位數（只揭露不 gate） ────────────────────────────────────

def test_percentiles():
    assert ws._percentiles([]) == {"p50": None, "p90": None, "max": None}
    assert ws._percentiles([3]) == {"p50": 3, "p90": 3, "max": 3}
    assert ws._percentiles([1, 2, 3])["p50"] == 2
    # nearest-rank（round(p*(n-1))）：偶數長度取下中位——揭露用途、非統計精算
    out = ws._percentiles(list(range(1, 11)))
    assert out["p50"] == 5 and out["p90"] == 9 and out["max"] == 10


def test_fanout_ratio_reported():
    rec = _record(alerts=[_alert(name=f"A{i}", fire=1000) for i in range(4)])
    out = _score_one(rec, _meta())
    assert out["summary"]["fanout_ratio"]["max"] == 4   # 1 episode ×4 series 命中
    assert out["verdict"] == "PASS"                     # 不 gate


# ── CLI 契約 + poison（免 VM） ─────────────────────────────────────────

def _write_report(tmp_path, report, name="rep.json"):
    p = tmp_path / name
    p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return p


def test_help_exits_zero():
    assert _run_score("--help").returncode == 0


def test_bad_flag_exits_two():
    assert _run_score("--this-flag-does-not-exist-xyz").returncode == 2


def test_missing_tolerances_flag_exits_two(tmp_path):
    p = _write_report(tmp_path, _report([_record()], [_meta()]))
    r = _run_score(str(p))
    assert r.returncode == 2
    assert "--tolerances" in r.stderr


def test_missing_tolerances_file_exits_two(tmp_path):
    """poison：拔容差檔 → exit 2（operational），非 crash 非預設容差。"""
    p = _write_report(tmp_path, _report([_record()], [_meta()]))
    r = _run_score(str(p), "--tolerances", str(tmp_path / "nope.yaml"))
    assert r.returncode == 2
    assert "容差" in r.stderr


def test_missing_report_file_exits_two(tmp_path):
    r = _run_score(str(tmp_path / "nope.json"), "--tolerances", str(_TOL))
    assert r.returncode == 2


def test_old_report_without_fault_window_exits_two(tmp_path):
    rep = _report([_record()], [_meta()])
    del rep["metadata"]["series"][0]["fault_window_s"]
    p = _write_report(tmp_path, rep)
    r = _run_score(str(p), "--tolerances", str(_TOL))
    assert r.returncode == 2
    assert "fault_window_s" in r.stderr and "重產" in r.stderr


def test_cli_fn_exit1_report_still_emitted(tmp_path):
    """verdict FAIL = exit 1（D8.1 violation 語義）且報告照常輸出、非 crash。"""
    p = _write_report(tmp_path, _report([_record(alerts=[])], [_meta()]))
    out_file = tmp_path / "score.json"
    r = _run_score(str(p), "--tolerances", str(_TOL), "--json",
                   "--out", str(out_file))
    assert r.returncode == 1
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "FAIL"
    assert doc["summary"]["false_negatives"] == 1
    assert json.loads(out_file.read_text(encoding="utf-8")) == doc


def test_cli_pass_exit0_with_scope_disclaimer(tmp_path):
    p = _write_report(tmp_path, _report([_record(alerts=[_alert(fire=1000)])],
                                        [_meta()]))
    r = _run_score(str(p), "--tolerances", str(_TOL), "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "PASS"
    assert doc["schema_version"] == ws.SCHEMA_VERSION
    assert doc["scope"]["injected_set_only"] is True
    assert "long tail" in doc["scope"]["disclaimer"]


# ── FIX-1 三態 verdict（CRITICAL regression：遮蔽不得偽裝 PASS） ────────

def test_critical_regression_mass_miss_shadow_is_indeterminate_not_pass(tmp_path):
    """盲審 B 實測攻擊：21 must_detect、20 全漏 + 1 筆無關聚合 alert →
    修前 catch 1/1=100% PASS exit 0；修後 INDETERMINATE exit 1。"""
    recs = [_record(sig=0, alerts=[_alert(fire=1000)])]
    metas = [_meta(sig=0)]
    for i in range(1, 21):
        recs.append(_record(sig=i, alerts=[]))
        metas.append(_meta(sig=i))
    rep_doc = _report(recs, metas,
                      unattributed=[{"alertname": "Aggregated", "labels": {}}])
    out = ws.score([("r.json", rep_doc)], _TOLS)
    assert out["verdict"] == "INDETERMINATE"
    assert out["summary"]["indeterminate"] == 20
    assert out["summary"]["false_negatives"] == 0     # 契約①：不計 FN
    # CLI：exit 1（非 0）、報告照常輸出
    p = _write_report(tmp_path, rep_doc)
    r = _run_score(str(p), "--tolerances", str(_TOL), "--json")
    assert r.returncode == 1
    assert json.loads(r.stdout)["verdict"] == "INDETERMINATE"


def test_all_indeterminate_zero_denominator_is_indeterminate_exit1(tmp_path):
    """零分母分流：indeterminate 致零分母 = 偵測面真相 → INDETERMINATE exit 1
    （非 operational exit 2）。"""
    rep_doc = _report([_record(alerts=[])], [_meta()],
                      unattributed=[{"alertname": "Agg", "labels": {}}])
    out = ws.score([("r.json", rep_doc)], _TOLS)
    assert out["verdict"] == "INDETERMINATE"
    assert out["summary"]["scored_denominator"] == 0
    assert out["summary"]["catch_rate"] is None
    p = _write_report(tmp_path, rep_doc)
    r = _run_score(str(p), "--tolerances", str(_TOL), "--json")
    assert r.returncode == 1


def test_fn_takes_priority_over_indeterminate():
    """FN 與 indeterminate 並存 → FAIL（FN 優先；D8.1 硬門檻不被遮蔽稀釋）。"""
    rep_clean = _report([_record(sig=0, alerts=[])], [_meta(sig=0)])   # FN
    rep_shadow = _report([_record(sig=1, alerts=[])], [_meta(sig=1)],
                         unattributed=[{"alertname": "Agg", "labels": {}}])
    out = ws.score([("a.json", rep_clean), ("b.json", rep_shadow)], _TOLS)
    assert out["verdict"] == "FAIL"
    assert out["summary"]["false_negatives"] == 1
    assert out["summary"]["indeterminate"] == 1


def test_clean_report_is_pass_with_reason():
    out = _score_one(_record(alerts=[_alert(fire=1000)]), _meta())
    assert out["verdict"] == "PASS"
    assert "indeterminate == 0" in out["verdict_reason"]


# ── FIX-6：盲審 B mutation 補牙 ─────────────────────────────────────────

def test_fractional_catch_rate():
    """釘 catch_rate = hits/scored 的分母（mutation 曾證 survive）。"""
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=1, alerts=[])]
    out = ws.score([("r.json", _report(recs, [_meta(sig=0), _meta(sig=1)]))],
                   _TOLS)
    assert out["summary"]["catch_rate"] == 0.5
    assert out["verdict"] == "FAIL"


def test_conservation_teeth_case_vanishes_is_tool_bug(tmp_path, monkeypatch,
                                                      capsys):
    """FIX-6b fault-injection：讓 score_case 回傳三態之外的 status（case 靜默
    蒸發）→ 守恆 assert 觸發 ScoreToolBug；CLI 層 exit 2。"""
    monkeypatch.setattr(ws, "score_case",
                        lambda *a, **k: {"status": "vanished",
                                         "hits": [], "misses": []})
    rep_doc = _report([_record(alerts=[_alert(fire=1000)])], [_meta()])
    with pytest.raises(ws.ScoreToolBug, match="守恆"):
        ws.score([("r.json", rep_doc)], _TOLS)
    # in-process main → exit 2（operational，非 crash）
    p = _write_report(tmp_path, rep_doc)
    monkeypatch.setattr(sys, "argv", ["waveform_score.py", str(p),
                                      "--tolerances", str(_TOL)])
    assert ws.main() == 2
    assert "守恆" in capsys.readouterr().err


# ── FIX-3/4：審計欄 code 層強制（schema 可被換掉＝縱深而非唯一防線） ────

def test_audit_fields_enforced_even_with_empty_schema(tmp_path):
    """空 schema {} 經 --schema 餵入（schema 層全放行）→ code 層仍咬缺審計欄。"""
    empty_schema = tmp_path / "empty.json"
    empty_schema.write_text("{}", encoding="utf-8")
    tol = _write_tol(tmp_path, {
        "defaults": {"default": 900},
        "overrides": [{"alert_class": "X", "severity": "default",
                       "tolerance_s": 100, "approved_by": "a"}]})  # 缺 justification
    p = _write_report(tmp_path, _report([_record(alerts=[_alert(fire=1000)])],
                                        [_meta()]))
    r = _run_score(str(p), "--tolerances", str(tol), "--schema",
                   str(empty_schema))
    assert r.returncode == 2
    assert "justification" in r.stderr


def test_carve_out_missing_approved_by_fails_loud(tmp_path):
    """FIX-4：carve_outs 補 approved_by——schema + code 層雙防。"""
    with pytest.raises(ws.ScoreInputError):        # 真 schema：required 擋
        _load_tol(_write_tol(tmp_path, {
            "defaults": {"default": 900},
            "carve_outs": [{"fault_class": "cc", "reason": "r"}]}))
    empty_schema = {"type": "object"}              # 降級 schema → code 層擋
    tolfile = _write_tol(tmp_path, {
        "defaults": {"default": 900},
        "carve_outs": [{"fault_class": "cc", "reason": "r"}]})
    with pytest.raises(ws.ScoreInputError, match="approved_by"):
        ws.load_tolerances(str(tolfile), empty_schema, jsonschema)


# ── CodeRabbit #1045 硬化：malformed 容差/報告輸入 fail-loud（非崩） ──────

def test_duplicate_defaults_key_rejected(tmp_path):
    """CR-1(a)：defaults 重複 severity key 被 yaml.safe_load 靜默取最後、可悄悄
    抬高 D5 天花板 → strict loader 拒重複 key（exit 2）。"""
    p = tmp_path / "dup.yaml"
    p.write_text("defaults:\n  default: 300\n  critical: 300\n  critical: 999\n",
                 encoding="utf-8")
    with pytest.raises(ws.ScoreInputError, match="重複 key"):
        _load_tol(p)


def test_non_mapping_tolerances_doc_fails_loud_under_permissive_schema(tmp_path):
    """CR-1(b)：寬鬆 --schema（{}）下非映射 YAML（[]/42/null）漏過 schema 驗證 →
    code 層 isinstance(doc,dict) 擋、回 ScoreInputError（非 doc.get AttributeError 崩）。"""
    for raw in ("[]", "42", "null"):
        p = tmp_path / "nonmap.yaml"
        p.write_text(raw, encoding="utf-8")
        with pytest.raises(ws.ScoreInputError, match="頂層必須是映射"):
            ws.load_tolerances(str(p), {}, jsonschema)   # {} = 全放行 schema


def test_malformed_report_metadata_shape_exits_two(tmp_path):
    """CR-2：手改報告的 metadata/series/entry 非預期形狀 → ScoreInputError
    exit 2（非 AttributeError 未捕捉崩潰）。"""
    def _set_meta(d): d["metadata"] = "x"
    def _set_series(d): d["metadata"]["series"] = {"k": 1}
    def _set_entry(d): d["metadata"]["series"][0] = 42
    for mutate, tag in ((_set_meta, "meta-str"), (_set_series, "series-dict"),
                        (_set_entry, "entry-int")):
        rep_doc = _report([_record()], [_meta()])
        mutate(rep_doc)
        p = _write_report(tmp_path, rep_doc, name=f"bad_{tag}.json")
        r = _run_score(str(p), "--tolerances", str(_TOL))
        assert r.returncode == 2, tag


# ── FIX-7：報告血緣最小完整性 ──────────────────────────────────────────

def test_fault_window_shape_validation_exits_two(tmp_path):
    for bad_fw, tag in (([300], "len1"), ([500, 300], "start>end"),
                        (["x", 500], "non-num"), ({"s": 1}, "not-list")):
        rep_doc = _report([_record()], [_meta()])
        rep_doc["metadata"]["series"][0]["fault_window_s"] = bad_fw
        p = _write_report(tmp_path, rep_doc, name=f"bad_{tag}.json")
        r = _run_score(str(p), "--tolerances", str(_TOL))
        assert r.returncode == 2, tag
        assert "fault_window_s" in r.stderr, tag


# ── FIX-8：not_scored 顯性計數 ────────────────────────────────────────

def test_not_scored_probe_informational_counted():
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=0, variant="oscillation", expects="probe", alerts=[]),
            _record(sig=1, expects="informational", alerts=[])]
    metas = [_meta(sig=0), _meta(sig=0, variant="oscillation", expects="probe"),
             _meta(sig=1, expects="informational")]
    out = ws.score([("r.json", _report(recs, metas))], _TOLS)
    assert out["summary"]["not_scored_probe_informational"] == 2


# ── FIX-10：重複 (pack_id, seed) 警示（不擋） ─────────────────────────

def test_duplicate_pack_seed_inputs_warn_but_do_not_block(tmp_path):
    rep_doc = _report([_record(alerts=[_alert(fire=1000)])], [_meta()])
    rep_doc["seed"] = 1
    p1 = _write_report(tmp_path, rep_doc, name="a.json")
    p2 = _write_report(tmp_path, rep_doc, name="b.json")
    r = _run_score(str(p1), str(p2), "--tolerances", str(_TOL), "--json")
    assert r.returncode == 0                       # 不擋
    assert "重複 pack" in r.stderr                  # 但顯性警示
    doc = json.loads(r.stdout)
    assert doc["warnings"] and "重複 pack" in doc["warnings"][0]
    assert doc["summary"]["scored_denominator"] == 2   # 膨脹如實呈現


# ── G-1 drain-then-shadow 未歸因 allowlist（CRITICAL-preservation 鐵律） ──

def _tol_with_ignored(*names):
    tol = dict(_TOLS)
    tol["ignored_unattributed"] = {
        n: {"alertname": n, "justification": "已知平台聚合雜音",
            "approved_by": "sre-oncall"} for n in names}
    return tol


def test_g1_a_no_allowlist_aggregate_still_shadows_indeterminate():
    """G-1 (a)：無 allowlist（既有行為）→ 聚合 unattributed 仍遮蔽成 INDETERMINATE
    （CRITICAL 防線不動）。"""
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=1, alerts=[])]
    rep = _report(recs, [_meta(sig=0), _meta(sig=1)],
                  unattributed=[{"alertname": "Aggregated", "labels": {}}])
    out = ws.score([("r.json", rep)], _TOLS)   # 無 ignored_unattributed
    by_sig = {c["signature_index"]: c for c in out["cases"]}
    assert by_sig[1]["status"] == "indeterminate"
    assert out["verdict"] == "INDETERMINATE"
    assert out["unattributed_ignored"] == []
    assert len(out["unattributed_effective"]) == 1


def test_g1_b_allowlist_drains_shadow_real_miss_surfaces_as_fn():
    """G-1 (b) CRITICAL-preservation：把遮蔽用的聚合 alert 加進 ignored_unattributed
    → 20 個 no-hit case 從 INDETERMINATE 變 FN → verdict FAIL（allowlist 讓真 miss
    浮現、**絕不**洗成 PASS）。"""
    recs = [_record(sig=0, alerts=[_alert(fire=1000)])]
    metas = [_meta(sig=0)]
    for i in range(1, 21):
        recs.append(_record(sig=i, alerts=[]))
        metas.append(_meta(sig=i))
    rep = _report(recs, metas,
                  unattributed=[{"alertname": "Aggregated", "labels": {}}])
    out = ws.score([("r.json", rep)], _tol_with_ignored("Aggregated"))
    assert out["verdict"] == "FAIL"                    # 非 PASS、非 INDETERMINATE
    assert out["summary"]["false_negatives"] == 20     # 真 miss 浮現
    assert out["summary"]["indeterminate"] == 0        # 遮蔽解除
    assert len(out["unattributed_ignored"]) == 1       # 顯性列出（no-silent-caps）
    assert out["unattributed_ignored"][0]["ignored_by"]["approved_by"] == "sre-oncall"
    assert out["unattributed_effective"] == []         # drain 到剩空


def test_g1_c_all_hit_with_only_ignored_noise_is_pass():
    """G-1 (c)：全 hit + 只有被 allowlist drain 的雜音 → PASS。"""
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=1, alerts=[_alert(fire=1000)])]
    rep = _report(recs, [_meta(sig=0), _meta(sig=1)],
                  unattributed=[{"alertname": "Aggregated", "labels": {}}])
    out = ws.score([("r.json", rep)], _tol_with_ignored("Aggregated"))
    assert out["verdict"] == "PASS"
    assert out["summary"]["false_negatives"] == 0
    assert out["summary"]["indeterminate"] == 0
    assert len(out["unattributed_ignored"]) == 1


def test_g1_d_unknown_noise_not_in_allowlist_still_indeterminate():
    """G-1 (d) CRITICAL 保留：未在 allowlist 的未知雜音仍觸發遮蔽 → no-hit case
    仍 INDETERMINATE（不重開 CRITICAL）。"""
    recs = [_record(sig=0, alerts=[_alert(fire=1000)]),
            _record(sig=1, alerts=[])]
    rep = _report(recs, [_meta(sig=0), _meta(sig=1)],
                  unattributed=[{"alertname": "UnknownNoise", "labels": {}}])
    # allowlist 只有 Aggregated；UnknownNoise 不在 → 仍遮蔽
    out = ws.score([("r.json", rep)], _tol_with_ignored("Aggregated"))
    by_sig = {c["signature_index"]: c for c in out["cases"]}
    assert by_sig[1]["status"] == "indeterminate"
    assert out["verdict"] == "INDETERMINATE"
    assert out["summary"]["false_negatives"] == 0
    assert out["unattributed_ignored"] == []           # 未 drain
    assert len(out["unattributed_effective"]) == 1
    assert out["unattributed_effective"][0]["alert"]["alertname"] == "UnknownNoise"


def test_g1_e_ignored_unattributed_missing_approved_by_fails_loud(tmp_path):
    """G-1 (e)：ignored_unattributed 缺 approved_by → exit 2（schema + code 雙防）。"""
    with pytest.raises(ws.ScoreInputError):             # 真 schema：required 擋
        _load_tol(_write_tol(tmp_path, {
            "defaults": {"default": 900},
            "ignored_unattributed": [{"alertname": "Agg", "justification": "j"}]}))
    empty_schema = {"type": "object"}                  # 降級 schema → code 層擋
    tolfile = _write_tol(tmp_path, {
        "defaults": {"default": 900},
        "ignored_unattributed": [{"alertname": "Agg", "justification": "j"}]})
    with pytest.raises(ws.ScoreInputError, match="approved_by"):
        ws.load_tolerances(str(tolfile), empty_schema, jsonschema)


def test_g1_ignored_unattributed_loads_and_dedups(tmp_path):
    """G-1：合法 allowlist load 進 tol dict；重複 alertname fail-loud。"""
    tol = _load_tol(_write_tol(tmp_path, {
        "defaults": {"default": 900},
        "ignored_unattributed": [
            {"alertname": "Agg", "justification": "j", "approved_by": "a"}]}))
    assert "Agg" in tol["ignored_unattributed"]
    with pytest.raises(ws.ScoreInputError, match="重複"):
        _load_tol(_write_tol(tmp_path, {
            "defaults": {"default": 900},
            "ignored_unattributed": [
                {"alertname": "Agg", "justification": "j", "approved_by": "a"},
                {"alertname": "Agg", "justification": "k", "approved_by": "b"}]}))


# ── G-2 early-onset 過敏標記（揭露不 gate） ────────────────────────────

def test_g2_early_onset_fire_flagged_and_counted():
    """G-2：規則在 onset 段（fire < hold 起點）就開火 → hit 標 early_onset_fire +
    summary 計數；hold 內開火 → 無旗標。皆不改 verdict。"""
    # 窗 (300, 9270)、hold 起點 2100 → fire 500 落 [300, 2100) = early onset
    early = _score_one(_record(alerts=[_alert(fire=500)]), _meta(hold_start=2100))
    h = early["cases"][0]["hits"][0]
    assert h["early_onset_fire"] is True
    assert h["early_by_onset_s"] == 1600               # 2100 - 500
    assert early["summary"]["early_onset_fires"] == 1
    assert early["verdict"] == "PASS"                  # 不改 verdict
    # hold 內開火（fire 3000 >= 2100）→ 無旗標
    normal = _score_one(_record(alerts=[_alert(fire=3000)]), _meta(hold_start=2100))
    assert "early_onset_fire" not in normal["cases"][0]["hits"][0]
    assert normal["summary"]["early_onset_fires"] == 0


def test_g2_no_hold_start_no_marking():
    """G-2：metadata 無 hold_start_s（舊報告 / absence）→ 不標記（graceful）。"""
    out = _score_one(_record(alerts=[_alert(fire=500)]), _meta(hold_start=None))
    assert "early_onset_fire" not in out["cases"][0]["hits"][0]
    assert out["summary"]["early_onset_fires"] == 0


# ── G-3 flapping 偵測（揭露不 gate） ──────────────────────────────────

def _flap_alert(fire, last, cnt, severity="warning"):
    return {"alertname": "A", "fire_offset_s": fire, "last_fire_offset_s": last,
            "resolve_offset_s": None, "firing_sample_count": cnt,
            "labels": {"alertname": "A", "severity": severity}}


def test_g3_flapping_suspected_flagged():
    """G-3：firing_sample_count 遠少於 [fire, last_fire] 連續應有樣本數（缺口 ≥2）
    → hit 標 flapping_suspected + firing_gap_samples + summary 計數 + stderr WARNING；
    連續 firing → 無旗標。皆不改 verdict。"""
    # fire=400, last=1300, step=30 → expected=(900/30)+1=31；cnt=5 → gap=26
    out = _score_one(_record(alerts=[_flap_alert(400, 1300, 5)]), _meta())
    h = out["cases"][0]["hits"][0]
    assert h["flapping_suspected"] is True
    assert h["firing_gap_samples"] == 26
    assert out["summary"]["flapping_suspected"] == 1
    assert out["verdict"] == "PASS"                    # 不改 verdict
    assert any("flapping" in w for w in out["warnings"])
    # 連續 firing（cnt == expected）→ 無旗標
    out2 = _score_one(_record(alerts=[_flap_alert(400, 1300, 31)]), _meta())
    assert "flapping_suspected" not in out2["cases"][0]["hits"][0]
    assert out2["summary"]["flapping_suspected"] == 0
    assert out2["warnings"] == []


def test_g3_flapping_off_by_one_not_flagged():
    """G-3 門檻：缺口僅 1 個樣本（off-by-one）不算 flapping（避免誤報）。"""
    # expected 31、cnt 30 → gap 1 < 2
    out = _score_one(_record(alerts=[_flap_alert(400, 1300, 30)]), _meta())
    assert "flapping_suspected" not in out["cases"][0]["hits"][0]
    assert out["summary"]["flapping_suspected"] == 0


def test_g3_flapping_needs_step_and_last_fire():
    """G-3：step_s 缺（舊報告）或 last_fire<=fire → 無法計算、跳過（不誤報）。"""
    # 報告缺 step_s → 跳過
    rep = _report([_record(alerts=[_flap_alert(400, 1300, 5)])], [_meta()])
    del rep["window"]["step_s"]
    out = ws.score([("r.json", rep)], _TOLS)
    assert "flapping_suspected" not in out["cases"][0]["hits"][0]
    # last_fire == fire（單一樣本）→ 跳過
    out2 = _score_one(_record(alerts=[_flap_alert(400, 400, 1)]), _meta())
    assert "flapping_suspected" not in out2["cases"][0]["hits"][0]


# ── e2e（需 VM + vmalert；skip-if-no-VM + REQUIRE 旋鈕，語義照抄 #968） ──

_VM_URL = os.environ.get("WAVEFORM_SCORE_VM_URL", "http://localhost:8428")
_REQUIRE = os.environ.get("WAVEFORM_SCORE_REQUIRE") == "1"
_VM = vm_harness.VMClient(_VM_URL)
_VMALERT = vm_harness.find_vmalert()
_missing = (
    "no VictoriaMetrics" if not _VM.reachable() else
    "no vmalert binary" if _VMALERT is None else None
)
_needs_vm = pytest.mark.skipif(
    not _REQUIRE and _missing is not None,
    reason=f"{_missing} (on-demand score e2e; start a pinned vmsingle "
           f"-retentionPeriod=100y + vmalert, or WAVEFORM_SCORE_REQUIRE=1 to force)",
)


def _require_deps_or_fail() -> None:
    if _REQUIRE and _missing is not None:
        pytest.fail(f"WAVEFORM_SCORE_REQUIRE=1 but {_missing} — score e2e 不得"
                    f"靜默 skip-to-green（vmsingle 沒起 / binary 缺？）")


def _inject_report(tmp_path, rules_path, name="inject.json") -> Path:
    out = tmp_path / name
    r = _run_inject(str(_DISK), "--rules", str(rules_path), "--allow-selftest",
                    "--out", str(out), "--vm-url", _VM_URL)
    assert r.returncode == 0, r.stderr
    return out


@_needs_vm
def test_e2e_inject_then_score_pass(tmp_path):
    """PR-2 inject（決定性 seed）→ score → PASS；驗 fault_window 血緣真通。"""
    _require_deps_or_fail()
    rep = _inject_report(tmp_path, _RULES)
    r = _run_score(str(rep), "--tolerances", str(_TOL), "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "PASS"
    s = doc["summary"]
    # disk pack：must_detect = base + noise + fanout×3（oscillation 是 probe）
    assert s["must_detect_total"] == 5
    assert s["scored_denominator"] == 5 and s["hits"] == 5
    assert s["catch_rate"] == 1.0
    assert s["indeterminate"] == 0 and s["carved_out"] == 0
    # 血緣：case 的 fault_window 來自 inject 報告 metadata（合成期導出）
    base = next(c for c in doc["cases"] if c["variant"] == "base")
    assert base["fault_window_s"] == [300, 9270]
    assert base["hits"][0]["tolerance_source"] == "default:warning"
    assert doc["summary"]["fanout_ratio"]["max"] >= 1


@_needs_vm
def test_e2e_late_fire_tolerance_ceiling_bites(tmp_path):
    """poison：同一份 inject 報告、兩套容差——寬容差=晚但接住（hit）、
    tolerance 0 = FN（釘「天花板真的咬」、D5 不可洗綠）。"""
    _require_deps_or_fail()
    late_rules = tmp_path / "late.rules.yaml"
    late_rules.write_text(
        "groups:\n  - name: selftest-disk-late\n    interval: 30s\n    rules:\n"
        "      - alert: SelftestDiskLate\n"
        "        expr: selftest_disk_used_percent > 80\n"
        "        for: 135m\n"                       # 開火點落在 hold 段之後（晚 fire）
        "        labels:\n          severity: warning\n",
        encoding="utf-8")
    rep = _inject_report(tmp_path, late_rules)

    r_ok = _run_score(str(rep), "--tolerances", str(_TOL), "--json")
    assert r_ok.returncode == 0, r_ok.stderr
    doc_ok = json.loads(r_ok.stdout)
    base = next(c for c in doc_ok["cases"] if c["variant"] == "base")
    assert base["status"] == "hit"
    assert base["hits"][0]["fire_offset_s"] > base["fault_window_s"][1]  # 真的晚

    zero_tol = tmp_path / "zero.yaml"
    zero_tol.write_text(
        "defaults:\n  critical: 0\n  warning: 0\n  default: 0\n", encoding="utf-8")
    r_fn = _run_score(str(rep), "--tolerances", str(zero_tol), "--json")
    assert r_fn.returncode == 1, r_fn.stderr          # verdict FAIL、非 crash
    doc_fn = json.loads(r_fn.stdout)
    assert doc_fn["verdict"] == "FAIL"
    base_fn = next(c for c in doc_fn["cases"] if c["variant"] == "base")
    assert base_fn["status"] == "fn"
    assert base_fn["misses"][0]["outside"]["late_by_s"] > 0


@_needs_vm
def test_e2e_never_fire_fn_exit1_not_crash(tmp_path):
    """poison：never-fire 規則 inject→score → verdict FAIL（exit 1）非 crash。"""
    _require_deps_or_fail()
    never = tmp_path / "never.rules.yaml"
    never.write_text(
        "groups:\n  - name: never-fire\n    interval: 30s\n    rules:\n"
        "      - alert: SelftestNeverFire\n"
        "        expr: selftest_disk_used_percent > 100000000\n"
        "        for: 0s\n",
        encoding="utf-8")
    rep = _inject_report(tmp_path, never)
    r = _run_score(str(rep), "--tolerances", str(_TOL), "--json")
    assert r.returncode == 1, r.stderr
    doc = json.loads(r.stdout)
    assert doc["verdict"] == "FAIL"
    assert doc["summary"]["false_negatives"] == 5     # 全 must_detect 都 FN
    assert all(c["fn_reason"] == "no_fire" for c in doc["false_negatives"])


# ── --redact 出關安全（air-gapped / 受限 egress） ─────────────────────

def test_redact_strips_all_planted_identifiers():
    """出關安全鐵律：planted 識別項一個都不得出現在 redacted 序列化輸出——
    alertname / metric / fault_class / labels 拓撲 / pack_id / 容差 approved_by /
    檔案路徑全剝。allowlist 重建故未來新欄也不洩（denylist-after-merge 是 fail-open）。"""
    a = _alert(name="SEKRETALERT")
    a["labels"]["instance"] = "sekrethost01"
    rec = _record(fault_class="SEKRETFAULTCLASS", alerts=[a])
    rec["metric"] = "sekretmetric"
    rep = _report([rec], [_meta()])
    rep["pack_id"] = "SEKRETPACK"
    tol = {"defaults": {"warning": 600, "default": 900},
           "overrides": {"SEKRETALERT": {
               "alert_class": "SEKRETALERT", "severity": "warning",
               "tolerance_s": 100, "justification": "j",
               "approved_by": "SEKRETAPPROVER"}},
           "carve_outs": {}, "ignored_unattributed": {}}
    full = ws.score([("SEKRETPATH.json", rep)], tol,
                    tolerances_path="SEKRETPATH.yaml", schema_path="SEKRETSCHEMA.json")
    planted = ["SEKRETALERT", "sekretmetric", "SEKRETFAULTCLASS", "sekrethost01",
               "SEKRETPACK", "SEKRETAPPROVER", "SEKRETPATH", "SEKRETSCHEMA"]
    # 前提健全：這些識別項確實在「完整」報告內（否則測試 vacuous）
    blob_full = json.dumps(full, ensure_ascii=False)
    assert all(s in blob_full for s in planted), "poison 前提失效：識別項未進完整報告"
    redacted = ws.redact_report(full)
    blob = json.dumps(redacted, ensure_ascii=False)
    for s in planted:
        assert s not in blob, f"LEAK: {s!r} 出現在 redacted 輸出"


def test_redact_is_allowlist_rebuild_not_denylist():
    """redacted 頂層鍵 ⊆ 白名單；完整報告被塞未知敏感頂層/summary 鍵也不外洩
    （若改黑名單 del，未來新欄會預設洩）。"""
    full = _score_one(_record(alerts=[_alert(fire=1000)]), _meta())
    full["FUTURE_LEAKY_KEY"] = "sekret_future_infra"
    full["summary"]["FUTURE_SUMMARY_LEAK"] = "sekret_summary_infra"
    redacted = ws.redact_report(full)
    allowed = {"tool", "schema_version", "redacted", "verdict",
               "verdict_reason", "scope", "summary"}
    assert set(redacted) <= allowed
    blob = json.dumps(redacted, ensure_ascii=False)
    assert "sekret_future_infra" not in blob
    assert "sekret_summary_infra" not in blob


def test_redact_preserves_verdict_and_counts():
    """redact 不損失決策資訊：verdict + catch_rate + 計數逐一保留。"""
    full = _score_one(_record(alerts=[_alert(fire=1000)]), _meta())
    r = ws.redact_report(full)
    assert r["redacted"] is True
    assert r["verdict"] == full["verdict"]
    assert r["verdict_reason"] == full["verdict_reason"]
    assert r["summary"]["catch_rate"] == full["summary"]["catch_rate"]
    assert r["summary"]["false_negatives"] == full["summary"]["false_negatives"]
    assert r["scope"]["disclaimer"] == full["scope"]["disclaimer"]


def test_cli_redact_json_has_no_identifiers(tmp_path):
    """CLI --redact --json：stdout 無識別項、redacted:true、verdict 保留、exit 0。"""
    a = _alert(name="SEKRETCLI", severity=None)   # 無 severity → 落 default row（避 FIX-5 fail-loud）
    rec = _record(fault_class="SEKRETCLIFAULT", alerts=[a])
    rec["metric"] = "sekretclimetric"
    rep = _report([rec], [_meta()])
    rep["pack_id"] = "SEKRETCLIPACK"
    p = _write_report(tmp_path, rep, name="rep.json")
    r = _run_score(str(p), "--tolerances", str(_TOL), "--json", "--redact")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["redacted"] is True and doc["verdict"] == "PASS"
    for s in ("SEKRETCLI", "sekretclimetric", "SEKRETCLIFAULT", "SEKRETCLIPACK"):
        assert s not in r.stdout, f"LEAK: {s}"


def test_cli_redact_human_no_percase_detail(tmp_path):
    """CLI --redact（human）：印 [REDACTED]、無 per-case 名字，stderr 也不洩。"""
    rec = _record(fault_class="SEKRETHUMANFAULT", alerts=[])   # no fire → FN → FAIL
    rep = _report([rec], [_meta()])
    rep["pack_id"] = "SEKRETHUMANPACK"
    p = _write_report(tmp_path, rep, name="rep.json")
    r = _run_score(str(p), "--tolerances", str(_TOL), "--redact")
    assert r.returncode == 1                       # FN → FAIL
    assert "REDACTED" in r.stdout
    combined = r.stdout + r.stderr
    for s in ("SEKRETHUMANFAULT", "SEKRETHUMANPACK"):
        assert s not in combined, f"LEAK: {s}"


def test_cli_redact_error_path_no_path_leak(tmp_path):
    """對抗 review HIGH：--redact 下 CLI 錯誤路徑（含版本 skew 缺 fault_window_s、
    報告缺欄）的 stderr 不得夾客戶檔案路徑/pack_id——只印通用抑制訊息。"""
    # (A) 缺必要欄位：報告路徑編了「客戶」名
    bad = tmp_path / "tenant_ACMEBANK_prod.json"
    bad.write_text(json.dumps({"tool": "inject-waveform"}), encoding="utf-8")
    r = _run_score(str(bad), "--tolerances", str(_TOL), "--redact")
    assert r.returncode == 2
    assert "ACMEBANK" not in r.stderr and "tenant_ACMEBANK_prod" not in r.stderr
    assert "--redact 抑制" in r.stderr
    # (B) 版本 skew：缺 fault_window_s（code 明列的預期情境）
    rep = _report([_record(alerts=[_alert()])], [_meta()])
    del rep["metadata"]["series"][0]["fault_window_s"]
    skew = _write_report(tmp_path, rep, name="site_TOKYO_DC_run.json")
    r2 = _run_score(str(skew), "--tolerances", str(_TOL), "--redact")
    assert r2.returncode == 2
    assert "TOKYO_DC" not in r2.stderr and "synthetic-pack" not in r2.stderr
    # 對照：不加 --redact 時完整訊息（含路徑）本地可見
    r3 = _run_score(str(skew), "--tolerances", str(_TOL))
    assert r3.returncode == 2 and "site_TOKYO_DC_run" in r3.stderr


def test_cli_redact_argparse_error_no_path_leak(tmp_path):
    """post-fix re-review HIGH：argparse 錯誤（interleaved positional）發生在 args.redact
    存在前，繞過 _emit_error → 修 _RedactAwareParser。--redact 下 stderr 不得回顯客戶路徑。"""
    # interleaved：flag 夾在兩個 report positional 之間 → argparse unrecognized arguments
    r = _run_score("rep1.json", "--tolerances", str(_TOL),
                   "tenant_LEAKYNAME_dc2.json", "--redact")
    assert r.returncode == 2
    assert "LEAKYNAME" not in r.stderr and "unrecognized" not in r.stderr
    assert "--redact" in r.stderr        # 通用抑制訊息
    # 對照：非 --redact 時完整訊息本地可見（含路徑）
    r2 = _run_score("rep1.json", "--tolerances", str(_TOL),
                    "tenant_LEAKYNAME_dc2.json")
    assert r2.returncode == 2 and "LEAKYNAME" in r2.stderr


def test_cli_redact_malformed_schema_no_traceback(tmp_path):
    """post-fix re-review MEDIUM：--schema 指向語法合法但結構非法的 JSON-Schema，
    jsonschema 內部 AttributeError 逃出 tuple → 修 except Exception。--redact 下不得吐
    traceback、走通用訊息 exit 2。"""
    bad = tmp_path / "LEAKYSCHEMA.json"
    bad.write_text('{"type":"object","properties":"X"}', encoding="utf-8")
    rec = _report([_record(alerts=[_alert()])], [_meta()])
    p = _write_report(tmp_path, rec, name="r.json")
    r = _run_score(str(p), "--tolerances", str(_TOL), "--schema", str(bad), "--redact")
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "AttributeError" not in r.stderr
    assert "LEAKYSCHEMA" not in r.stderr


def test_redact_summary_whitelist_complete():
    """B4：白名單完整性——redacted summary 必須含全部 INCLUDE 計數且值不變。刪
    _REDACT_SUMMARY_KEYS 任一鍵 → 該 INCLUDE 計數靜默不出關、此測試即紅（防 mutation
    全綠）。"""
    full = _score_one(_record(alerts=[_alert(fire=1000)]), _meta())
    r = ws.redact_report(full)
    assert r["summary"] == {k: full["summary"][k] for k in ws._REDACT_SUMMARY_KEYS}
    # 且白名單確實涵蓋完整 summary（無遺漏 owner 要的計數）
    assert set(r["summary"]) == set(full["summary"])
