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


def _meta(sig=0, variant="base", series=None, expects="must_detect", fw=_FW):
    labels = {"series": series} if series else {}
    return {"signature_index": sig, "variant": variant, "labels": labels,
            "expects": expects,
            "fault_window_s": (list(fw) if fw is not None else None)}


def _report(records, metas, unattributed=(), span=12000):
    return {"tool": "inject-waveform", "pack_id": "synthetic-pack",
            "records": records, "window": {"span_s": span},
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
