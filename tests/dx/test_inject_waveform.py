"""test_inject_waveform.py — waveform 注入執行器（ADR-030 PR-2）測試。

單元（免 VM）：CLI 契約 / 0-1-2 exit-code 語義 / 純函式
（rules 解析、ts 平移、window slot、紀錄組裝、identity 歸因、resolve 計算、
在場驗證、殘留 pre-check——後兩者以 stub client 驗失敗路徑）。

e2e（需 VM + vmalert）：skip-if-no-VM + ``WAVEFORM_INJECT_REQUIRE=1`` 旋鈕
（語義照抄 #968 ``VM_REPLAY_REQUIRE``：REQUIRE 時缺依賴 = FAIL 非 skip，
防 CI/守門永遠 skip-to-green）。CI 的 Python Tests job 無 VM → e2e 乾淨 auto-skip。

poison teeth（D8.4 自證）：
  (1) 藏 vmalert binary（--vmalert 指向不存在路徑）→ exit 2 非綠（免 VM）；
  (2) 必不 fire 的候選規則 → fired:false + exit 0（「沒接住」是資料、非錯誤）；
  (3) should-fire seed 配對應規則 → fired:true + offset 合理 + 跨 run 決定性；
  (4) explicit offset 指回殘留窗 → exit 2（跨 run 隔離 fail-loud）；
  (5) record→alert 鏈式規則真 fire（rulesDelay/flushInterval 釘死成 regression）。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("jsonschema")

import inject_waveform as iw  # noqa: E402  (sys.path via tests/conftest.py)
import _waveform_lib as wf  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_CLI = _REPO / "scripts" / "tools" / "dx" / "inject_waveform.py"
_FIXDIR = Path(__file__).parent / "fixtures" / "waveform"
_DISK = _FIXDIR / "selftest_disk_used_percent.yaml"
_RATIO = _FIXDIR / "selftest_bufferpool_hit_percent.yaml"
_RULES = _FIXDIR / "rules" / "selftest_disk.rules.yaml"
_CHAINED_RULES = _FIXDIR / "rules" / "selftest_disk_chained.rules.yaml"

# 共用 #968 harness（同 CLI 的依賴；tests/rulepacks 無 __init__.py → 直接插路徑）
sys.path.insert(0, str(_REPO / "tests" / "rulepacks"))
import vm_harness  # noqa: E402


def _run(*args: str, timeout: int = 420) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def _mutate_pack(tmp_path: Path, base: Path, mutate) -> Path:
    with open(base, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    mutate(pack)
    out = tmp_path / "mutated.yaml"
    out.write_text(yaml.safe_dump(pack, allow_unicode=True), encoding="utf-8")
    return out


class _StubVM:
    """query_range stub（在場驗證 / 殘留 pre-check 的免 VM 失敗路徑測試用）。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list = []

    def query_range(self, expr, start, end, step):
        self.calls.append((expr, start, end, step))
        return self.responder(expr, start, end, step)


# ── CLI 契約（exit-code convention，對齊 waveform_compile） ───────────

def test_help_exits_zero():
    assert _run("--help").returncode == 0


def test_bad_flag_exits_two():
    assert _run("--this-flag-does-not-exist-xyz").returncode == 2


def test_missing_rules_flag_exits_two():
    r = _run(str(_DISK))
    assert r.returncode == 2
    assert "--rules" in r.stderr


def test_window_offset_not_int_exits_two():
    r = _run(str(_DISK), "--rules", str(_RULES), "--window-offset-s", "abc")
    assert r.returncode == 2
    assert "--window-offset-s" in r.stderr


def test_rules_delay_below_flush_interval_exits_two():
    r = _run(str(_DISK), "--rules", str(_RULES), "--rules-delay-s", "0")
    assert r.returncode == 2
    assert "--rules-delay-s" in r.stderr


# ── pack 驗證閘（與 compile 同一套：1 = pack 問題） ───────────────────

def test_selftest_seed_without_flag_exits_one():
    r = _run(str(_DISK), "--rules", str(_RULES))
    assert r.returncode == 1
    assert "self-test-seed" in r.stderr


def test_missing_sme_field_exits_one(tmp_path):
    bad = _mutate_pack(tmp_path, _DISK,
                       lambda p: p["signatures"][0].pop("typical_wobble"))
    r = _run(str(bad), "--rules", str(_RULES), "--allow-selftest")
    assert r.returncode == 1
    assert "退回 SME" in r.stderr


def test_malformed_pack_yaml_exits_two(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("pack: [unclosed\n  nope::\n", encoding="utf-8")
    r = _run(str(bad), "--rules", str(_RULES))
    assert r.returncode == 2
    assert "YAML" in r.stderr


# ── 候選規則載入（operational error = 2；免 VM） ──────────────────────

def test_rules_path_missing_exits_two(tmp_path):
    r = _run(str(_DISK), "--rules", str(tmp_path / "nope.yaml"), "--allow-selftest")
    assert r.returncode == 2
    assert "--rules" in r.stderr


def test_rules_dir_empty_exits_two(tmp_path):
    r = _run(str(_DISK), "--rules", str(tmp_path), "--allow-selftest")
    assert r.returncode == 2
    assert "規則檔" in r.stderr


def test_rules_without_alerts_exits_two(tmp_path):
    rules = tmp_path / "records_only.yaml"
    rules.write_text(
        "groups:\n  - name: records-only\n    rules:\n"
        "      - record: selftest:noop\n        expr: vector(0)\n",
        encoding="utf-8")
    r = _run(str(_DISK), "--rules", str(rules), "--allow-selftest")
    assert r.returncode == 2
    assert "alert" in r.stderr


def test_duplicate_group_name_across_files_exits_two(tmp_path):
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(
            "groups:\n  - name: dup-group\n    rules:\n"
            "      - alert: X\n        expr: vector(1)\n",
            encoding="utf-8")
    r = _run(str(_DISK), "--rules", str(tmp_path), "--allow-selftest")
    assert r.returncode == 2
    assert "dup-group" in r.stderr


def test_sentinel_name_collision_exits_two(tmp_path):
    rules = tmp_path / "clash.yaml"
    rules.write_text(
        f"groups:\n  - name: clash\n    rules:\n"
        f"      - alert: {iw.SENTINEL_ALERT}\n        expr: vector(1)\n",
        encoding="utf-8")
    r = _run(str(_DISK), "--rules", str(rules), "--allow-selftest")
    assert r.returncode == 2
    # 有牙斷言（mutation 曾證 rc==2 可能來自環境缺依賴而非 guard）：
    # 訊息必須指名 sentinel 保留名，證明是撞名 guard 在擋。
    assert iw.SENTINEL_ALERT in r.stderr
    assert "保留" in r.stderr


def test_reserved_label_key_in_static_labels_exits_two(tmp_path):
    rules = tmp_path / "forge.yaml"
    rules.write_text(
        "groups:\n  - name: forge\n    rules:\n"
        "      - alert: Forge\n        expr: vector(1)\n"
        "        labels:\n          waveform_variant: base\n",
        encoding="utf-8")
    r = _run(str(_DISK), "--rules", str(rules), "--allow-selftest")
    assert r.returncode == 2
    assert "保留鍵" in r.stderr
    assert "waveform_variant" in r.stderr


def test_sentinel_masquerade_via_alertname_label_exits_two(tmp_path):
    rules = tmp_path / "masq.yaml"
    rules.write_text(
        f"groups:\n  - name: masq\n    rules:\n"
        f"      - alert: Innocent\n        expr: vector(1)\n"
        f"        labels:\n          alertname: {iw.SENTINEL_ALERT}\n",
        encoding="utf-8")
    r = _run(str(_DISK), "--rules", str(rules), "--allow-selftest")
    assert r.returncode == 2
    assert iw.SENTINEL_ALERT in r.stderr
    assert "偽裝" in r.stderr


# ── poison (1)：藏 vmalert binary → exit 2 非綠（免 VM——binary 檢查在
#    任何 VM 接觸之前，D8.4 fail-loud） ─────────────────────────────────

def test_missing_vmalert_binary_exits_two(tmp_path):
    r = _run(str(_DISK), "--rules", str(_RULES), "--allow-selftest",
             "--vmalert", str(tmp_path / "no-such-vmalert"))
    assert r.returncode == 2
    assert "不存在" in r.stderr


# ── 零注入行 fail-loud（免 VM——guard 在 harness/VM 接觸之前） ──────────

def test_all_gap_pack_zero_lines_exits_two(tmp_path):
    bad = _mutate_pack(
        tmp_path, _DISK,
        lambda p: p["signatures"][0].setdefault("time_axis", {}).__setitem__(
            "dropout_pattern", list(range(0, 500))))
    r = _run(str(bad), "--rules", str(_RULES), "--allow-selftest")
    assert r.returncode == 2
    assert "零可注入行" in r.stderr


# ── 純函式：rules 解析 ───────────────────────────────────────────────

def test_parse_rules_extracts_names_and_intervals():
    parsed = iw.parse_rules([
        ("f1", "groups:\n  - name: g1\n    rules:\n"
               "      - alert: B\n        expr: up == 0\n"
               "      - record: r:x\n        expr: vector(1)\n"),
        ("f2", "groups:\n  - name: g2\n    interval: 15s\n    rules:\n"
               "      - alert: A\n        expr: up == 1\n"),
    ])
    assert parsed["alertnames"] == ["A", "B"]
    assert parsed["record_names"] == ["r:x"]
    assert [g["name"] for g in parsed["groups"]] == ["g1", "g2"]
    # interval 誠實化：缺省 = vmalert 預設 1m；不正規化
    assert parsed["groups_meta"] == [
        {"name": "g1", "interval_s": 60},
        {"name": "g2", "interval_s": 15},
    ]


def test_parse_rules_rejects_non_groups_doc():
    with pytest.raises(iw.InjectError, match="groups"):
        iw.parse_rules([("f", "alerting:\n  rules: []\n")])


def test_parse_rules_rejects_unparseable_interval():
    with pytest.raises(iw.InjectError, match="interval"):
        iw.parse_rules([("f", "groups:\n  - name: g\n    interval: soon\n    rules:\n"
                              "      - alert: A\n        expr: vector(1)\n")])


def test_build_rules_text_appends_sentinel_group_last():
    parsed = iw.parse_rules([
        ("f", "groups:\n  - name: g1\n    rules:\n"
              "      - alert: A\n        expr: up == 1\n")])
    doc = yaml.safe_load(iw.build_rules_text(parsed["groups"]))
    assert [g["name"] for g in doc["groups"]] == ["g1", iw.SENTINEL_GROUP]
    sentinel_rules = doc["groups"][-1]["rules"]
    assert sentinel_rules[0]["alert"] == iw.SENTINEL_ALERT
    assert sentinel_rules[0]["expr"] == "vector(1)"


# ── 純函式：ts 平移 / window slot / 在場 bounds ──────────────────────

def test_shift_import_lines_moves_ts_only():
    text = ("# comment line\n"
            'm{a="b c"} 1.5 1700000000000\n'
            "\n"
            "bare_metric 2 1700000030000\n")
    assert iw.shift_import_lines(text, 3600) == [
        'm{a="b c"} 1.5 1700003600000',
        "bare_metric 2 1700003630000",
    ]
    # offset 0 = 恆等（值/label 不動）
    assert iw.shift_import_lines(text, 0)[0] == 'm{a="b c"} 1.5 1700000000000'


def test_offset_stride_exceeds_span_plus_margin():
    for span in (60, 3600, 10230, 86400):
        assert iw.offset_stride_s(span) >= span + 3600, span
        assert iw.offset_stride_s(span) % 3600 == 0


def test_series_ts_bounds_first_and_last():
    lines = [
        'm{a="x"} 1 1700000060000',
        'm{a="x"} 2 1700000000000',
        'm{a="x"} 3 1700000030000',
        "other 9 1700009900000",
    ]
    assert iw.series_ts_bounds(lines) == {
        'm{a="x"}': (1700000000000, 1700000060000),
        "other": (1700009900000, 1700009900000),
    }


# ── 純函式：紀錄組裝 / identity 歸因 / resolve 計算 ───────────────────

def test_build_records_shape_and_excludes_companions():
    series = wf.synthesize_pack(wf.load_pack(str(_RATIO)))
    recs = iw.build_records(series)
    assert len(recs) == 6  # base + noise + oscillation + fanout×3（companion 排除）
    assert all(r["expects"] != "companion" for r in recs)
    for r in recs:
        assert set(r) == {"signature_index", "fault_class", "metric", "variant",
                          "series", "expects", "labels", "fired", "alerts"}
        assert r["fired"] is False and r["alerts"] == []
        # 合成期 identity 鍵已注入（FIX-1 治本）
        assert r["labels"]["waveform_signature"] == "0"
    fanouts = [r for r in recs if r["variant"] == "fanout"]
    assert sorted(r["series"] for r in fanouts) == ["f01", "f02", "f03"]
    assert all(r["series"] is None for r in recs if r["variant"] != "fanout")


def test_summarize_alert_series_offsets_and_resolve():
    labels = {"__name__": "ALERTS", "alertname": "X", "alertstate": "firing",
              "waveform_variant": "base"}
    s = iw.summarize_alert_series(labels, [480, 510, 540], span_s=10000, step_s=30)
    assert s["alertname"] == "X"
    assert s["fire_offset_s"] == 480
    assert s["last_fire_offset_s"] == 540
    assert s["resolve_offset_s"] == 570
    assert s["firing_sample_count"] == 3
    assert "__name__" not in s["labels"] and "alertstate" not in s["labels"]
    # 窗尾仍 firing → resolve 未知（None），不可偽造 resolve 時間
    s2 = iw.summarize_alert_series(labels, [9990], span_s=10000, step_s=30)
    assert s2["resolve_offset_s"] is None


def _rec(sig_idx, variant, series, labels):
    return {"signature_index": sig_idx, "fault_class": "fc", "metric": "m",
            "variant": variant, "series": series, "expects": "must_detect",
            "labels": labels, "fired": False, "alerts": []}


def _alert(labels, offset=60):
    return {"alertname": labels.get("alertname", "A"), "fire_offset_s": offset,
            "last_fire_offset_s": offset, "resolve_offset_s": offset + 30,
            "firing_sample_count": 1, "labels": labels}


def test_attribute_alerts_identity_exact_match():
    records = [
        _rec(0, "base", None, {"instance": "i1", "waveform_signature": "0",
                               "waveform_variant": "base"}),
        _rec(0, "fanout", "f01", {"instance": "i1", "series": "f01",
                                  "waveform_signature": "0",
                                  "waveform_variant": "fanout"}),
        _rec(0, "fanout", "f02", {"instance": "i1", "series": "f02",
                                  "waveform_signature": "0",
                                  "waveform_variant": "fanout"}),
    ]
    hit_base = _alert({"alertname": "A", "instance": "i1", "waveform_signature": "0",
                       "waveform_variant": "base",
                       "severity": "warning"})           # rule 靜態 label 不妨礙歸因
    hit_f01 = _alert({"alertname": "A", "instance": "i1", "series": "f01",
                      "waveform_signature": "0", "waveform_variant": "fanout"})
    aggregated = _alert({"alertname": "A"})              # sum() 剝光 label → 不可歸因
    no_signature = _alert({"alertname": "A", "instance": "i1",
                           "waveform_variant": "base"})  # 缺 signature 鍵 → 不可歸因
    wrong_topo = _alert({"alertname": "A", "instance": "OTHER",
                         "waveform_signature": "0",
                         "waveform_variant": "base"})    # topology sanity 矛盾
    un = iw.attribute_alerts(records, [hit_base, hit_f01, aggregated,
                                       no_signature, wrong_topo])
    assert records[0]["fired"] is True and records[0]["alerts"] == [hit_base]
    assert records[1]["fired"] is True and records[1]["alerts"] == [hit_f01]
    assert records[2]["fired"] is False                  # f02 沒有 alert
    assert un == [aggregated, no_signature, wrong_topo]


def test_attribution_regression_two_sigs_same_labels_diff_metric():
    """FIX-1 回歸（外審 probe 場景）：兩個 signature 同 topology labels、異 metric
    ——修前 ALERTS 無 __name__ 可判別，一條 alert 同時歸因到兩個 signature 的同
    variant 紀錄且 unattributed==0 靜默；修後 identity 鍵 exact-match 單射，只落
    正確 signature。"""
    with open(_DISK, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    sig2 = copy.deepcopy(pack["signatures"][0])
    sig2["metric"] = "selftest_disk_used_percent_other"  # 同 labels、異 metric
    pack["signatures"].append(sig2)
    series = wf.synthesize_pack(pack)
    records = iw.build_records(series)
    assert len(records) == 12  # 2 signatures × 6

    # 模擬「只有 signature 0 的 base 開火」的 ALERTS labels（含合成 identity 鍵）
    alert = _alert({"alertname": "A", "instance": "selftest-host-01",
                    "mount": "selftest-data", "waveform_signature": "0",
                    "waveform_variant": "base"})
    un = iw.attribute_alerts(records, [alert])
    fired = [(r["signature_index"], r["variant"]) for r in records if r["fired"]]
    assert fired == [(0, "base")]  # 修前這裡是 [(0,'base'),(1,'base')] 雙重歸因
    assert un == []


def test_assert_unique_series_identities():
    # 真 pack 合成後應唯一通過
    iw.assert_unique_series_identities(wf.synthesize_pack(wf.load_pack(str(_RATIO))))
    # 人造撞名 → fail-loud
    dup = wf.Series(metric="m", labels={"a": "b"}, samples=[1.0], variant="base",
                    expects="must_detect", signature_index=0, fault_class="fc",
                    source="self-test-seed")
    with pytest.raises(iw.InjectError, match="identity 撞名"):
        iw.assert_unique_series_identities([dup, copy.deepcopy(dup)])


# ── 在場驗證 / 殘留 pre-check（stub client，免 VM） ────────────────────

def _disk_series_and_lines():
    series = wf.synthesize_pack(wf.load_pack(str(_DISK)))
    lines = iw.shift_import_lines(wf.materialize_vm(series), iw.AUTO_SLOT_BASE_S)
    ws = wf.T0 + iw.AUTO_SLOT_BASE_S
    we = ws + iw.data_span_s(series) + 600
    return series, lines, ws, we


def test_verify_ingest_missing_series_fails_loud():
    series, lines, ws, we = _disk_series_and_lines()
    stub = _StubVM(lambda expr, s, e, st: [])  # VM 什麼都沒收到（ingest 靜默丟）
    with pytest.raises(iw.InjectError, match="在場驗證失敗"):
        iw.verify_ingest(stub, series, lines, ws, we)


def test_verify_ingest_passes_when_all_present():
    series, lines, ws, we = _disk_series_and_lines()
    bounds = iw.series_ts_bounds(lines)
    by_sid = {f"{s.metric}{wf._fmt_labels(s.labels)}": s for s in series}

    def responder(expr, s_, e_, st_):
        if expr == iw.ALL_SERIES_SELECTOR:
            return [{}] * len(series)          # 總數吻合
        s = by_sid[expr]
        lo_ms, hi_ms = bounds[expr]
        metric = {"__name__": s.metric, **{k: str(v) for k, v in s.labels.items()}}
        return [{"metric": metric,
                 "values": [[lo_ms // 1000, "1"], [hi_ms // 1000 + 1, "1"]]}]

    iw.verify_ingest(_StubVM(responder), series, lines, ws, we)  # 不應 raise


def test_verify_ingest_flags_series_count_mismatch():
    series, lines, ws, we = _disk_series_and_lines()
    bounds = iw.series_ts_bounds(lines)
    by_sid = {f"{s.metric}{wf._fmt_labels(s.labels)}": s for s in series}

    def responder(expr, s_, e_, st_):
        if expr == iw.ALL_SERIES_SELECTOR:
            return [{}] * (len(series) + 1)    # 窗內多一條（並發寫入者/TOCTOU）
        s = by_sid[expr]
        lo_ms, hi_ms = bounds[expr]
        metric = {"__name__": s.metric, **{k: str(v) for k, v in s.labels.items()}}
        return [{"metric": metric,
                 "values": [[lo_ms // 1000, "1"], [hi_ms // 1000 + 1, "1"]]}]

    with pytest.raises(iw.InjectError, match="series 總數"):
        iw.verify_ingest(_StubVM(responder), series, lines, ws, we)


def test_resolve_window_explicit_dirty_fails_with_series_names():
    stub = _StubVM(lambda expr, s, e, st: [
        {"metric": {"__name__": "residual_metric"}, "values": [[1, "1"]]}])
    with pytest.raises(iw.InjectError) as exc:
        iw._resolve_window(stub, 1000, "0")
    assert "殘留" in str(exc.value)
    assert "residual_metric" in str(exc.value)  # 列出殘留 metric 名（可 actionable）


def test_resolve_window_auto_slides_past_dirty_slot():
    span = 1000
    slot0_ws = wf.T0 + iw.AUTO_SLOT_BASE_S

    def responder(expr, start, end, step):
        if start == slot0_ws:  # slot 0 髒 → 應滑到 slot 1
            return [{"metric": {"__name__": "residual_metric"}, "values": [[1, "1"]]}]
        return []

    off = iw._resolve_window(_StubVM(responder), span, "auto")
    assert off == iw.offset_stride_s(span)


# ── 報告輸出失敗 = operational error（exit 2 非 1、非 traceback） ──────

def test_out_write_failure_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(iw, "run_pipeline", lambda a, p, r: {"stub": True})
    monkeypatch.setattr(sys, "argv", [
        "inject_waveform.py", str(_DISK), "--rules", str(_RULES),
        "--allow-selftest", "--json",
        "--out", str(tmp_path / "no_such_dir" / "report.json")])
    assert iw.main() == 2
    assert "報告輸出失敗" in capsys.readouterr().err


# ── T0/STEP 雙 pin tripwire（_waveform_lib 與 vm_harness 各自 pin，
#    CLI 橋接兩者——任何一邊漂移，窗計算與物化時間軸就會靜默錯位） ─────

def test_t0_step_pins_match_vm_harness():
    assert wf.T0 == vm_harness.T0
    assert wf.STEP == vm_harness.STEP


# ── replay 引擎失敗 fail-loud wiring（Gemini #1043 盲區1 disposition）：
#    v1.146.0 實測 rule eval error → 5 retries → Fatalf rc=255、無 "replay
#    succeed" → 雙因子斷言必攔——引擎跑不動絕不偽裝成「規則沒 fire」的資料。
#    引擎行為 pin 見 vm_harness.replay docstring；VM pin 升版時重驗。 ─────────

def test_replay_engine_failure_raises_not_silent(tmp_path):
    # sys.executable 當假 vmalert：吃不下 -rule= flag → 非零退出、無 succeed 字串
    with pytest.raises(AssertionError, match="replay failed"):
        vm_harness.replay(sys.executable, "groups: []", vm_harness.T0,
                          vm_harness.T0 + 60, tmp_path, "poison",
                          datasource_url="http://127.0.0.1:1")


def test_pipeline_assertion_failure_exits_two(monkeypatch, capsys):
    def boom(a, p, r):
        raise AssertionError("vmalert -replay failed for poison: fatal")
    monkeypatch.setattr(iw, "run_pipeline", boom)
    monkeypatch.setattr(sys, "argv", [
        "inject_waveform.py", str(_DISK), "--rules", str(_RULES),
        "--allow-selftest"])
    assert iw.main() == 2
    assert "ERROR" in capsys.readouterr().err


def test_unattributed_alerts_emit_stderr_warning(monkeypatch, capsys):
    # Gemini #1043 盲區2：unattributed 顯性警告（indeterminate 非 FN），不只藏 JSON
    monkeypatch.setattr(iw, "run_pipeline", lambda a, p, r: {
        "unattributed_alerts": [{"alertname": "AggAlert"}]})
    monkeypatch.setattr(sys, "argv", [
        "inject_waveform.py", str(_DISK), "--rules", str(_RULES),
        "--allow-selftest", "--json"])
    assert iw.main() == 0
    captured = capsys.readouterr()
    assert "無法自動歸因" in captured.err and "indeterminate" in captured.err


# ── e2e（需 VM + vmalert；skip-if-no-VM + REQUIRE 旋鈕，語義照抄 #968） ──

_VM_URL = os.environ.get("WAVEFORM_INJECT_VM_URL", "http://localhost:8428")
_REQUIRE = os.environ.get("WAVEFORM_INJECT_REQUIRE") == "1"
_VM = vm_harness.VMClient(_VM_URL)
_VMALERT = vm_harness.find_vmalert()
_missing = (
    "no VictoriaMetrics" if not _VM.reachable() else
    "no vmalert binary" if _VMALERT is None else None
)
_needs_vm = pytest.mark.skipif(
    not _REQUIRE and _missing is not None,
    reason=f"{_missing} (on-demand inject e2e; start a pinned vmsingle "
           f"-retentionPeriod=100y + vmalert, or WAVEFORM_INJECT_REQUIRE=1 to force)",
)


def _require_deps_or_fail() -> None:
    if _REQUIRE and _missing is not None:
        pytest.fail(f"WAVEFORM_INJECT_REQUIRE=1 but {_missing} — inject e2e 不得"
                    f"靜默 skip-to-green（vmsingle 沒起 / binary 缺？）")


@_needs_vm
def test_e2e_should_fire_rerun_isolation_and_dirty_window():
    """poison (3)+(4)：should-fire 規則 → fired:true + offset 合理；auto 位移
    rerun → 絕對窗不同（隔離）、相對 fire offset 相同（決定性）；explicit offset
    指回殘留窗 → exit 2 fail-loud。"""
    _require_deps_or_fail()
    r1 = _run(str(_DISK), "--rules", str(_RULES), "--allow-selftest",
              "--json", "--vm-url", _VM_URL)
    assert r1.returncode == 0, r1.stderr
    rep1 = json.loads(r1.stdout)
    assert rep1["sentinel"]["fired"] is True
    assert rep1["window"]["slot_base_s"] == iw.AUTO_SLOT_BASE_S
    assert rep1["rules"]["groups"] == [
        {"name": "selftest-disk-candidates", "interval_s": 30}]
    recs = {(r["variant"], r["series"]): r for r in rep1["records"]}

    base = recs[("base", None)]
    assert base["expects"] == "must_detect"
    assert base["fired"] is True, base
    fire1 = min(a["fire_offset_s"] for a in base["alerts"])
    # ramp 42→96 過 80 需一段 onset + for:2m —— 不可能瞬間 fire，也必在窗內
    assert 120 <= fire1 <= rep1["window"]["span_s"], fire1
    assert {a["alertname"] for a in base["alerts"]} == {"SelftestDiskUsedHigh"}
    # 合成 identity 鍵存活到 ALERTS（歸因路徑真走 exact-match）
    assert base["alerts"][0]["labels"]["waveform_signature"] == "0"
    assert base["alerts"][0]["labels"]["waveform_variant"] == "base"

    assert recs[("oscillation", None)]["expects"] == "probe"
    assert len([k for k in recs if k[0] == "fanout"]) == 3
    assert rep1["unattributed_alerts"] == []
    assert rep1["window"]["step_s"] == wf.STEP

    # rerun：auto slot 滑到新窗（跨 run 隔離），相對 offset 不變（無 jitter pack）
    r2 = _run(str(_DISK), "--rules", str(_RULES), "--allow-selftest",
              "--json", "--vm-url", _VM_URL)
    assert r2.returncode == 0, r2.stderr
    rep2 = json.loads(r2.stdout)
    assert rep2["window"]["start_epoch_s"] != rep1["window"]["start_epoch_s"]
    base2 = {(r["variant"], r["series"]): r for r in rep2["records"]}[("base", None)]
    assert min(a["fire_offset_s"] for a in base2["alerts"]) == fire1

    # poison (4)：explicit offset 指回 run1 的窗 → 殘留 pre-check exit 2
    r3 = _run(str(_DISK), "--rules", str(_RULES), "--allow-selftest",
              "--window-offset-s", str(rep1["window"]["offset_s"]),
              "--vm-url", _VM_URL)
    assert r3.returncode == 2
    assert "殘留" in r3.stderr


@_needs_vm
def test_e2e_never_fire_is_data_not_error(tmp_path):
    """poison (2)：必不 fire 的規則 → 全部 fired:false、exit 0（合法資料）；
    sentinel 正控仍須 fire（證明 replay 鏈路活著、fired:false 非鏈路 no-op；
    ingest 層由注入後在場驗證把關——通過才走得到這裡）。"""
    _require_deps_or_fail()
    rules = tmp_path / "never.rules.yaml"
    rules.write_text(
        "groups:\n  - name: never-fire-candidates\n    interval: 30s\n    rules:\n"
        "      - alert: SelftestNeverFire\n"
        "        expr: selftest_disk_used_percent > 100000000\n"
        "        for: 0s\n",
        encoding="utf-8")
    out = tmp_path / "report.json"
    r = _run(str(_DISK), "--rules", str(rules), "--allow-selftest",
             "--json", "--out", str(out), "--vm-url", _VM_URL)
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert rep["sentinel"]["fired"] is True
    assert rep["records"] and all(
        rec["fired"] is False and rec["alerts"] == [] for rec in rep["records"])
    assert rep["unattributed_alerts"] == []
    # --out 檔案與 stdout JSON 一致
    assert json.loads(out.read_text(encoding="utf-8")) == rep


@_needs_vm
def test_e2e_chained_record_alert_fires(tmp_path):
    """poison (5) / FIX-3：record→alert 鏈式規則真 fire——replay 模式下鏈可見性
    依賴 -remoteWrite.flushInterval=1s + -replay.rulesDelay>=flushInterval（顯式
    pin，不靠預設偶然）；此 e2e 把 race 行為釘死成 regression。record 恆等式保留
    identity labels → 歸因鍵存活。"""
    _require_deps_or_fail()
    r = _run(str(_DISK), "--rules", str(_CHAINED_RULES), "--allow-selftest",
             "--json", "--vm-url", _VM_URL)
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert rep["rules"]["record_names"] == ["selftest:disk_used_percent:current"]
    base = {(x["variant"], x["series"]): x for x in rep["records"]}[("base", None)]
    assert base["fired"] is True, base
    assert {a["alertname"] for a in base["alerts"]} == {"SelftestDiskChainedHigh"}
    assert base["alerts"][0]["labels"]["waveform_signature"] == "0"
    assert rep["unattributed_alerts"] == []
