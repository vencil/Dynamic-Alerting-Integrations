"""test_waveform_compile.py — fault-waveform 編譯器（ADR-030 決策層驗證）測試。

Covers（設計稿 §5）:
  * 決定性：同 seed 兩次 --compile bitwise 相同 + golden fixture 釘值（跨版守門）。
  * 三變體 always-on：base/noise/oscillation/fanout（boolean 改 flapping/absence）。
  * counter 積分單調、boolean 變體形態、噪音幅度硬界內、dropout `_` 位置。
  * 負向全套：SME 欄缺→exit 1「退回 SME」、平台欄缺→「平台補填」、
    dips_back 無 dip_detail、malformed YAML→2、attestation false→1、
    self-test-seed 無 --allow-selftest→1、未知 shape/kind→1。
  * CLI 契約：--help→0、壞 flag→2、--compile 缺 --out→2。
  * promtool round-trip：skipif + WAVEFORM_PROMTOOL_REQUIRE=1 旋鈕
    （REQUIRE 時缺 promtool 是 FAIL 不是 skip——防 CI 永遠 skip-to-green）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("jsonschema")

import _waveform_lib as wf  # noqa: E402  (sys.path via tests/conftest.py)

_REPO = Path(__file__).resolve().parents[2]
_CLI = _REPO / "scripts" / "tools" / "dx" / "waveform_compile.py"
_SCHEMA = _REPO / "docs" / "schemas" / "waveform-pack.schema.json"
_FIXDIR = Path(__file__).parent / "fixtures" / "waveform"

_DISK = _FIXDIR / "selftest_disk_used_percent.yaml"
_ERRORS = _FIXDIR / "selftest_request_errors_total.yaml"
_SERVICE = _FIXDIR / "selftest_service_up.yaml"
_RATIO = _FIXDIR / "selftest_bufferpool_hit_percent.yaml"
_ALL_FIXTURES = sorted(_FIXDIR.glob("*.yaml"))


def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def _series_of(pack_path: Path, **kwargs) -> list:
    return wf.synthesize_pack(wf.load_pack(str(pack_path)), **kwargs)


def _mutate_fixture(tmp_path: Path, base: Path, mutate) -> Path:
    with open(base, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    mutate(pack)
    out = tmp_path / "mutated.yaml"
    out.write_text(yaml.safe_dump(pack, allow_unicode=True), encoding="utf-8")
    return out


# ── CLI 契約（exit-code convention） ──────────────────────────────────

def test_help_exits_zero():
    assert _run("--help").returncode == 0


def test_bad_flag_exits_two():
    assert _run("--this-flag-does-not-exist-xyz").returncode == 2


def test_compile_without_out_exits_two():
    r = _run("--compile", str(_DISK), "--allow-selftest")
    assert r.returncode == 2


# ── 正向：seed packs 全綠 ─────────────────────────────────────────────

def test_fixtures_present():
    """Guard against an empty glob silently passing (echo-chamber)."""
    assert len(_ALL_FIXTURES) == 4, [p.name for p in _ALL_FIXTURES]


def test_check_all_seed_packs_pass():
    r = _run("--check", "--allow-selftest", *[str(p) for p in _ALL_FIXTURES])
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_check_json_output_shape():
    r = _run("--check", "--allow-selftest", "--json", str(_DISK))
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["pass"] is True
    assert data["check"] == "waveform-pack"
    assert data["issues"] == []


def test_seed_packs_governance_hygiene():
    """Seed 治理：全部 source=self-test-seed、metric 用 selftest_ 假名。"""
    for p in _ALL_FIXTURES:
        pack = wf.load_pack(str(p))
        for sig in pack["signatures"]:
            assert sig["source"] == "self-test-seed", p.name
            assert sig["metric"].startswith("selftest_"), p.name
            for comp in sig.get("companion_series") or []:
                assert comp["metric"].startswith("selftest_"), p.name


# ── 治理閘：self-test-seed / attestation ─────────────────────────────

def test_selftest_seed_without_flag_exits_one():
    r = _run("--check", str(_DISK))
    assert r.returncode == 1
    assert "self-test-seed" in r.stderr


def test_attestation_false_exits_one(tmp_path):
    bad = _mutate_fixture(
        tmp_path, _DISK,
        lambda p: p["pack"].__setitem__("independent_of_rule_conversion", False))
    r = _run("--check", "--allow-selftest", str(bad))
    assert r.returncode == 1
    assert "independent_of_rule_conversion" in r.stderr


# ── 負向：兩層欄位切（退回 SME vs 平台補填） ──────────────────────────

def test_missing_sme_field_says_return_to_sme(tmp_path):
    bad = _mutate_fixture(
        tmp_path, _DISK, lambda p: p["signatures"][0].pop("typical_wobble"))
    r = _run("--check", "--allow-selftest", str(bad))
    assert r.returncode == 1
    assert "退回 SME" in r.stderr
    assert "typical_wobble" in r.stderr


def test_missing_platform_field_says_platform_fills(tmp_path):
    bad = _mutate_fixture(
        tmp_path, _DISK, lambda p: p["signatures"][0].pop("metric_kind"))
    r = _run("--check", "--allow-selftest", str(bad))
    assert r.returncode == 1
    assert "平台補填" in r.stderr
    assert "metric_kind" in r.stderr


def test_dips_back_without_dip_detail_exits_one(tmp_path):
    def mutate(p):
        p["signatures"][0].pop("dip_detail")
    bad = _mutate_fixture(tmp_path, _RATIO, mutate)
    r = _run("--check", "--allow-selftest", str(bad))
    assert r.returncode == 1
    assert "退回 SME" in r.stderr
    assert "dip_detail" in r.stderr


def test_boolean_wobble_exemption_is_schema_legal():
    """boolean 噪音欄豁免：selftest_service_up 無 typical_wobble 仍合法。"""
    pack = wf.load_pack(str(_SERVICE))
    assert "typical_wobble" not in pack["signatures"][0]
    r = _run("--check", "--allow-selftest", str(_SERVICE))
    assert r.returncode == 0, r.stderr


def test_unknown_shape_class_exits_one(tmp_path):
    bad = _mutate_fixture(
        tmp_path, _DISK, lambda p: p["signatures"][0].__setitem__("shape_class", "sawtooth"))
    r = _run("--check", "--allow-selftest", str(bad))
    assert r.returncode == 1
    assert "shape_class" in r.stderr


def test_unknown_metric_kind_exits_one(tmp_path):
    bad = _mutate_fixture(
        tmp_path, _DISK, lambda p: p["signatures"][0].__setitem__("metric_kind", "histogram"))
    r = _run("--check", "--allow-selftest", str(bad))
    assert r.returncode == 1


def test_malformed_yaml_exits_two(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("pack: [unclosed\n  nope::\n", encoding="utf-8")
    r = _run("--check", str(bad))
    assert r.returncode == 2
    assert "ERROR" in r.stderr


def test_missing_schema_file_exits_two(tmp_path):
    r = _run("--check", "--schema", str(tmp_path / "nope.json"), str(_DISK))
    assert r.returncode == 2


# ── 決定性 ────────────────────────────────────────────────────────────

def test_compile_same_seed_is_bitwise_identical(tmp_path):
    out1, out2 = tmp_path / "run1", tmp_path / "run2"
    for out in (out1, out2):
        r = _run("--compile", "--out", str(out), "--allow-selftest",
                 *[str(p) for p in _ALL_FIXTURES])
        assert r.returncode == 0, r.stderr
    names1 = sorted(p.name for p in out1.iterdir())
    names2 = sorted(p.name for p in out2.iterdir())
    assert names1 == names2 and len(names1) == 12  # 4 packs × (promtool+vm+metadata)
    for name in names1:
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes(), name


def test_different_seed_changes_noise(tmp_path):
    s1 = _series_of(_DISK, seed=1)
    s2 = _series_of(_DISK, seed=2)
    n1 = next(s for s in s1 if s.variant == "noise")
    n2 = next(s for s in s2 if s.variant == "noise")
    assert n1.samples != n2.samples


def test_golden_pinned_values(tmp_path):
    """Golden 釘值（seed=1 預設）：釘 PRNG 流 + jitter + 數字格式，
    跨 Python 版本 / 平台漂移時此測試先紅。"""
    out = tmp_path / "golden"
    r = _run("--compile", "--out", str(out), "--allow-selftest",
             str(_DISK), str(_ERRORS))
    assert r.returncode == 0, r.stderr

    prom = (out / "selftest-disk-used-percent.promtool.yaml").read_text(encoding="utf-8")
    # base 包絡（決定性、無 RNG）：lead 42、dropout `_` 在 index 10、onset 起步 43.8
    assert "values: '42 42 42 42 42 42 42 42 42 42 _ 43.8 44.7" in prom
    # noise 變體開頭三值（owned Box-Muller、seed=1 的釘值）
    assert "'41.165874 42.189549 42.254414" in prom

    vm = (out / "selftest-request-errors-total.vm.txt").read_text(encoding="utf-8")
    # counter 積分（rate 0.1/s × 30s = 3/step）+ jitter 釘值（seed=1）
    assert ('selftest_request_errors_total{endpoint="selftest-api",'
            'instance="selftest-host-01",waveform_signature="0",'
            'waveform_variant="base"} 3 1699999997957') in vm


# ── 變體語義 ──────────────────────────────────────────────────────────

def test_three_variants_always_on_and_fanout_count():
    series = _series_of(_DISK)
    variants = sorted({s.variant for s in series})
    assert variants == ["base", "fanout", "noise", "oscillation"]
    fans = [s for s in series if s.variant == "fanout"]
    assert len(fans) == wf.DEFAULT_FANOUT
    assert sorted(s.labels["series"] for s in fans) == ["f01", "f02", "f03"]

    series5 = _series_of(_DISK, fanout=5)
    assert len([s for s in series5 if s.variant == "fanout"]) == 5


def test_noise_amplitude_within_hard_bound():
    series = _series_of(_DISK)
    base = next(s for s in series if s.variant == "base")
    noise = next(s for s in series if s.variant == "noise")
    wobble = 1.5
    assert len(base.samples) == len(noise.samples)
    for b, n in zip(base.samples, noise.samples):
        if b is None or n is None:
            assert b is None and n is None  # dropout 位置一致
            continue
        assert abs(n - b) <= wobble + 1e-9


def test_probe_oscillation_full_depth_and_expects():
    """dips_back=false → 機械震盪探針：全深度 dip 回 normal_level、expects=probe。"""
    series = _series_of(_DISK)
    osc = next(s for s in series if s.variant == "oscillation")
    assert osc.expects == "probe"
    assert any("probe" in n for n in osc.auto_adjustments)
    # fault 窗（ramp: lead 10 + onset 60 → hold 從 index 70 起，240 步）
    fw0, fw1 = 70, 309
    for i in range(fw0, fw1 + 1):
        if (i - fw0) % wf.PROBE_DIP_PERIOD_STEPS == 0 and osc.samples[i] is not None:
            assert osc.samples[i] == 42  # normal_level（全深度）
    # 其餘 hold 樣本維持 fault_level
    assert osc.samples[fw0 + 1] == 96


def test_declared_dips_inherit_must_detect():
    """dips_back=true → 用 SME dip_detail、繼承 must_detect。
    MED-4 回歸守護：fault onset 首樣本乾淨（不被 dip 汙染），dip 只落在
    fault window 內部——否則 onset/for-duration 規則永遠看到被 dip 抵銷的首格。"""
    series = _series_of(_RATIO)
    osc = next(s for s in series
               if s.variant == "oscillation" and s.expects != "companion")
    assert osc.expects == "must_detect"
    # normal 95 > fault 40 → dip 往上 depth 30 → 70；period 5m = 10 steps
    fw0 = 30  # lead 10 + onset 20
    # MED-4: 首樣本乾淨 fault_level（onset 不被 dip 汙染）
    assert osc.samples[fw0] == 40
    assert osc.samples[fw0 + 1] == 40
    # 內部 period 位置仍有 dip（reset-trap 覆蓋保留）
    assert osc.samples[fw0 + 10] == 70


def test_base_and_noise_inherit_must_detect():
    series = _series_of(_DISK)
    for variant in ("base", "noise", "fanout"):
        for s in series:
            if s.variant == variant:
                assert s.expects == "must_detect", (variant, s.expects)


# ── counter 語義 ──────────────────────────────────────────────────────

def test_counter_samples_are_monotone():
    series = _series_of(_ERRORS)
    counters = [s for s in series if s.metric == "selftest_request_errors_total"]
    assert counters
    for s in counters:
        vals = [v for v in s.samples if v is not None]
        assert all(b >= a for a, b in zip(vals, vals[1:])), (s.variant, s.labels)


# ── boolean 語義 ──────────────────────────────────────────────────────

def test_boolean_variant_shapes():
    series = _series_of(_SERVICE)
    variants = {s.variant for s in series}
    assert variants == {"base", "flapping", "staleness_absence", "fanout"}
    assert "noise" not in variants and "oscillation" not in variants

    base = next(s for s in series if s.variant == "base")
    flap = next(s for s in series if s.variant == "flapping")
    absence = next(s for s in series if s.variant == "staleness_absence")

    # 噪音豁免有 auto-justification 留痕
    assert any("exemption" in n for n in base.auto_adjustments)
    # flapping：fault 窗（step: lead 10 + onset 1 → hold 從 11 起）內 0/1 交替
    fw0 = 11
    assert flap.samples[fw0] == 0
    assert flap.samples[fw0 + 1] == 1
    assert flap.samples[fw0 + 2] == 0
    # absence：樣本提早結束（staleness = down 是 absent 不是 0 的可注入測項）
    assert absence.truncated is True
    assert len(absence.samples) < len(base.samples)
    # agent_keeps_reporting=false → absence 是 SME 宣告行為 → 繼承 must_detect
    assert absence.expects == "must_detect"


def test_boolean_absence_is_probe_when_agent_keeps_reporting():
    pack = wf.load_pack(str(_SERVICE))
    pack["signatures"][0]["agent_keeps_reporting"] = True
    series = wf.synthesize_pack(pack)
    absence = next(s for s in series if s.variant == "staleness_absence")
    assert absence.expects == "probe"


# ── 時間軸擾動 ────────────────────────────────────────────────────────

def test_dropout_gap_positions_in_promtool_materialization():
    series = _series_of(_DISK)
    base = next(s for s in series if s.variant == "base")
    frag = wf.materialize_promtool([base])
    values_line = next(l for l in frag.splitlines() if "values:" in l)
    tokens = values_line.split("'")[1].split()
    for i, tok in enumerate(tokens):
        if i > 0 and i % 10 == 0:
            assert tok == "_", f"index {i} 應為 dropout gap，得到 {tok}"
        else:
            assert tok != "_", f"index {i} 不應為 gap"


def test_jitter_annotation_and_vm_only():
    """jitter 只進物化 (b)；(a) 對含 jitter 的 series 全 gap 遮罩（盲區1 修正：
    不可假裝有可對帳資料，見 test_promtool_masks_jittered_series 覆蓋細節）。"""
    err_series = _series_of(_ERRORS)
    frag = wf.materialize_promtool(err_series)
    assert "jitter" in frag  # per-series 遮罩註記仍提及 jitter
    disk_series = _series_of(_DISK)
    assert "jitter" not in wf.materialize_promtool(disk_series)

    vm = wf.materialize_vm(err_series)
    base = next(s for s in err_series if s.variant == "base")
    label_str = wf._fmt_labels(base.labels)
    ts_list = [int(line.rsplit(" ", 1)[1]) for line in vm.splitlines()
               if line.startswith(f"{base.metric}{label_str} ")]
    assert ts_list
    off_grid = 0
    for i, ts in enumerate(ts_list):
        nominal = (wf.T0 + i * wf.STEP) * 1000
        assert abs(ts - nominal) <= 5000 + 1  # jitter_s=5 界內
        if ts != nominal:
            off_grid += 1
    assert off_grid > 0  # jitter 確實發生


# ── companion series ─────────────────────────────────────────────────

def test_companion_series_per_variant_with_matching_labels():
    series = _series_of(_RATIO)
    mains = [s for s in series if s.metric == "selftest_bufferpool_hit_percent"]
    comps = [s for s in series if s.metric == "selftest_bufferpool_reads_total_rate"]
    assert len(mains) == len(comps) == 6  # base+noise+osc+fanout×3
    for c in comps:
        assert c.expects == "companion"
        assert c.labels["resource"] == "selftest-pool"
        assert c.labels["waveform_variant"] in {"base", "noise", "oscillation", "fanout"}
        assert all(v in (None, 1200.0) for v in c.samples)
    # 每條主 series 都有同 variant/series-label 的分母可 join
    def _key(s):
        return tuple(sorted((k, v) for k, v in s.labels.items()
                            if k in ("waveform_variant", "series")))
    assert sorted(map(_key, mains)) == sorted(map(_key, comps))


# ── 兩物化 value-domain parity（無 jitter 時） ────────────────────────

def test_materialization_value_parity_without_jitter(tmp_path):
    out = tmp_path / "parity"
    r = _run("--compile", "--out", str(out), "--allow-selftest", str(_DISK))
    assert r.returncode == 0, r.stderr
    frag = yaml.safe_load(
        (out / "selftest-disk-used-percent.promtool.yaml").read_text(encoding="utf-8"))
    vm_lines = [l for l in
                (out / "selftest-disk-used-percent.vm.txt").read_text(encoding="utf-8").splitlines()
                if l and not l.startswith("#")]
    vm_map: dict[str, dict[int, str]] = {}
    for line in vm_lines:
        series_id, value, ts = line.rsplit(" ", 2)
        vm_map.setdefault(series_id, {})[int(ts)] = value
    assert frag["interval"] == "30s"
    for entry in frag["input_series"]:
        sid = entry["series"].strip("'")
        tokens = str(entry["values"]).split()
        for i, tok in enumerate(tokens):
            ts_ms = (wf.T0 + i * wf.STEP) * 1000
            if tok == "_":
                assert ts_ms not in vm_map.get(sid, {}), (sid, i)
            else:
                assert vm_map[sid][ts_ms] == tok, (sid, i)


# ── metadata 留痕 ─────────────────────────────────────────────────────

def test_metadata_provenance(tmp_path):
    out = tmp_path / "meta"
    r = _run("--compile", "--out", str(out), "--allow-selftest", "--seed", "7",
             str(_ERRORS))
    assert r.returncode == 0, r.stderr
    meta = json.loads(
        (out / "selftest-request-errors-total.metadata.json").read_text(encoding="utf-8"))
    assert meta["seed"] == 7
    assert meta["step_seconds"] == wf.STEP
    assert meta["t0_epoch_seconds"] == wf.T0
    assert meta["materializations"]["promtool_fixture"]["includes_jitter"] is False
    assert meta["materializations"]["vm_import"]["includes_jitter"] is True
    for s in meta["series"]:
        assert s["source"] == "self-test-seed"
        assert s["expects"] in {"must_detect", "informational", "probe", "companion"}
    probes = [s for s in meta["series"] if s["expects"] == "probe"]
    assert probes and all(s["auto_adjustments"] for s in probes)


# ── readback ─────────────────────────────────────────────────────────

def test_render_readback_output():
    r = _run("--render-readback", "--allow-selftest", str(_SERVICE))
    assert r.returncode == 0, r.stderr
    assert "回讀" in r.stdout
    assert "半夜想被叫醒" in r.stdout
    assert any(ch in r.stdout for ch in wf._SPARK_BLOCKS)


def test_render_readback_refuses_invalid_pack(tmp_path):
    bad = _mutate_fixture(
        tmp_path, _DISK, lambda p: p["signatures"][0].pop("must_detect"))
    r = _run("--render-readback", "--allow-selftest", str(bad))
    assert r.returncode == 1
    assert "退回 SME" in r.stderr


# ── duration parser ──────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("30s", 30), ("5m", 300), ("2h", 7200), ("1h30m", 5400), ("1d", 86400),
])
def test_parse_duration(text, expected):
    assert wf.parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "5", "m5", "5x", "-5m", None, 5])
def test_parse_duration_rejects_bad_input(bad):
    with pytest.raises(wf.WaveformInputError):
        wf.parse_duration(bad)


# ── promtool round-trip（skipif + REQUIRE 旋鈕） ──────────────────────

def _promtool_or_skip() -> str:
    path = shutil.which("promtool")
    if path is None:
        if os.environ.get("WAVEFORM_PROMTOOL_REQUIRE") == "1":
            pytest.fail("WAVEFORM_PROMTOOL_REQUIRE=1 但 promtool 不在 PATH——"
                        "REQUIRE 模式下缺 promtool 必須 fail、不得 skip")
        pytest.skip("promtool not on PATH（設 WAVEFORM_PROMTOOL_REQUIRE=1 可改為硬性）")
    return path


def test_promtool_roundtrip_reads_materialization_a(tmp_path):
    promtool = _promtool_or_skip()
    out = tmp_path / "rt"
    r = _run("--compile", "--out", str(out), "--allow-selftest", str(_DISK))
    assert r.returncode == 0, r.stderr
    frag = yaml.safe_load(
        (out / "selftest-disk-used-percent.promtool.yaml").read_text(encoding="utf-8"))
    base_series = ('selftest_disk_used_percent{instance="selftest-host-01",'
                   'mount="selftest-data",waveform_signature="0",'
                   'waveform_variant="base"}')
    test_doc = {
        "rule_files": [],
        "evaluation_interval": frag["interval"],
        "tests": [{
            "interval": frag["interval"],
            "input_series": [
                {"series": e["series"], "values": e["values"]}
                for e in frag["input_series"]
            ],
            "promql_expr_test": [{
                # eval_time 0：base 包絡在 index 0 恰為 normal_level（免 RNG 依賴）
                "expr": 'selftest_disk_used_percent{waveform_variant="base"}',
                "eval_time": "0m",
                "exp_samples": [{"labels": base_series, "value": 42}],
            }],
        }],
    }
    test_file = tmp_path / "roundtrip_test.yaml"
    test_file.write_text(yaml.safe_dump(test_doc, allow_unicode=True), encoding="utf-8")
    result = subprocess.run(
        [promtool, "test", "rules", str(test_file)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


# ── 對抗 review 修正回歸 teeth（HIGH-1 / MED-2/3/4 / LOW-6/7） ──────────

def _vm_timestamps_by_series(text: str) -> dict:
    """Parse materialize_vm output → {series_key: [ts_ms, ...]} in emit order."""
    from collections import defaultdict
    out: defaultdict = defaultdict(list)
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _val, ts = line.rsplit(" ", 2)  # 'metric{labels} value ts_ms'
        out[key].append(int(ts))
    return out


def test_vm_timestamps_strictly_increasing(tmp_path):
    """HIGH-1: materialization (b) 每條 series 的 ts 嚴格遞增。jitter 逆序會被
    counter 的 rate() 誤判為 reset → 靜默污染 catch-rate 權威檔。"""
    def mut(pack):
        pack["signatures"][0].setdefault("time_axis", {})["jitter_s"] = 14  # 上界內最大
    p = _mutate_fixture(tmp_path, _ERRORS, mut)  # counter fixture
    text = wf.materialize_vm(_series_of(p))
    ts_by_series = _vm_timestamps_by_series(text)
    assert ts_by_series
    for key, tss in ts_by_series.items():
        assert all(b > a for a, b in zip(tss, tss[1:])), (key, "非單調 ts")


def test_jitter_over_cap_rejected(tmp_path):
    """HIGH-1: jitter_s >= STEP/2 (15) 被 schema 擋（第一道防線）。"""
    def mut(pack):
        pack["signatures"][0].setdefault("time_axis", {})["jitter_s"] = 20
    p = _mutate_fixture(tmp_path, _DISK, mut)
    assert _run("--check", str(p), "--allow-selftest").returncode == 1


def test_staleness_tail_exceeding_series_fails_loud(tmp_path):
    """MED-2: staleness_tail >= 整條序列 → fail-loud（D8.4），不靜默 no-op。"""
    def mut(pack):
        pack["signatures"][0].setdefault("time_axis", {})["staleness_tail"] = "10h"
    p = _mutate_fixture(tmp_path, _DISK, mut)
    with pytest.raises(wf.WaveformInputError, match="staleness_tail"):
        _series_of(p)


def test_declared_dip_overshoot_records_clamp_note(tmp_path):
    """MED-3: declared dip depth 溢出 normal_level → clamp 且 auto_adjustments 留痕。"""
    def mut(pack):
        pack["signatures"][0]["dip_detail"]["depth"] = 999  # 遠超 normal-fault 落差
    p = _mutate_fixture(tmp_path, _RATIO, mut)
    osc = next(s for s in _series_of(p)
               if s.variant == "oscillation" and s.expects != "companion")
    assert any("clamp" in n for n in osc.auto_adjustments), osc.auto_adjustments


def test_normal_equals_fault_rejected(tmp_path):
    """LOW-6: normal_level == fault_level → 無故障可注入、稀釋分母，退回 SME。"""
    def mut(pack):
        pack["signatures"][0]["fault_level"] = pack["signatures"][0]["normal_level"]
    p = _mutate_fixture(tmp_path, _DISK, mut)
    r = _run("--check", str(p), "--allow-selftest")
    assert r.returncode == 1
    assert "normal_level == fault_level" in r.stderr


def test_noise_actually_perturbs():
    """LOW-7 強化：wobble>0 時 noise 變體確實偏離 base（非恆等）——σ 用錯 /
    Box-Muller 壞掉會被抓（原界內斷言在 wobble→0 時 vacuous）。"""
    series = _series_of(_DISK)
    base = next(s for s in series if s.variant == "base")
    noise = next(s for s in series if s.variant == "noise")
    diffs = [abs(n - b) for b, n in zip(base.samples, noise.samples)
             if b is not None and n is not None]
    assert any(d > 1e-9 for d in diffs), "noise 變體與 base 完全相同 = 公式失效"


# ── Gemini 外審盲區回歸 teeth（jitter 雙軌 / counter companion / 負值 gauge） ──

def test_promtool_masks_jittered_series(tmp_path):
    """Gemini 盲區1: 含 jitter 的 series 在 promtool 物化 (a) 以全 gap 呈現
    （不可對帳），而非假裝無 jitter 的資料——否則 (a)/(b) 對帳誤判引擎差異。"""
    import re
    def mut(pack):
        pack["signatures"][0].setdefault("time_axis", {})["jitter_s"] = 10
    p = _mutate_fixture(tmp_path, _DISK, mut)
    text = wf.materialize_promtool(_series_of(p))
    value_lines = [ln for ln in text.splitlines() if re.match(r"\s*values: '", ln)]
    assert value_lines
    for ln in value_lines:
        toks = re.match(r"\s*values: '(.*)'", ln).group(1).split()
        assert toks and all(t == "_" for t in toks), f"jittered series 應全 gap: {ln}"


def test_counter_companion_integrates_to_ramp(tmp_path):
    """Gemini 盲區2: counter 角色的 companion 積分成累積斜線（rate 非零），
    否則常數直線 → rate()=0 → 比值除以零（+Inf/NaN），告警永不誠實計分。"""
    def mut(pack):
        pack["signatures"][0]["companion_series"][0]["metric_kind"] = "counter"
    p = _mutate_fixture(tmp_path, _RATIO, mut)
    comp = next(s for s in _series_of(p) if s.expects == "companion")
    vals = [v for v in comp.samples if v is not None]
    assert vals and all(b > a for a, b in zip(vals, vals[1:])), "counter companion 非單調"
    assert any("counter" in n for n in comp.auto_adjustments)


def test_min_value_clamps_negative_gauge(tmp_path):
    """Gemini 盲區3: min_value 設定時，gauge 合成的負值 clamp 至下界 + 留痕
    （非負指標物理保護；不設則不動，避免對可負指標誤傷）。"""
    def mut(pack):
        sig = pack["signatures"][0]
        sig["normal_level"] = 5
        sig["fault_level"] = 3
        sig["typical_wobble"] = 20   # 大 wobble → noise 會壓到負
        sig["min_value"] = 0
    p = _mutate_fixture(tmp_path, _DISK, mut)
    noise = next(s for s in _series_of(p) if s.variant == "noise")
    vals = [v for v in noise.samples if v is not None]
    assert all(v >= 0 for v in vals), "min_value clamp 後不應有負值"
    assert any("min_value" in n for n in noise.auto_adjustments), "clamp 須留痕"


# ── fault_window 血緣（PR-3 temporal-match 的秒級窗；additive 欄） ──────

def test_fault_window_value_variants_share_onset_to_hold_end():
    series = _series_of(_DISK)
    base = next(s for s in series if s.variant == "base")
    # ramp: lead 10 → 窗下界 = onset 起點（LEAD_STEPS*STEP，非 hold 起點——
    # for:-gated 告警常在 onset 段開火、屬提早接住）；上界 = hold 末樣本
    # index 309（lead10 + onset60 + hold240 - 1）
    assert base.fault_window == (wf.LEAD_STEPS * wf.STEP, 309 * wf.STEP)
    for s in series:
        assert s.fault_window == base.fault_window, s.variant  # 全 value 變體共用


def test_fault_window_absence_open_ended():
    series = _series_of(_SERVICE)
    absence = next(s for s in series if s.variant == "staleness_absence")
    # boolean step: lead 10 + onset 1 → hold 自 index 11；absence 自截斷點
    # 開放至觀測窗尾（end=None，scorer 以報告窗長收尾）
    assert absence.fault_window == (11 * wf.STEP, None)
    base = next(s for s in series if s.variant == "base")
    assert base.fault_window[1] is not None


def test_fault_window_companion_inherits():
    series = _series_of(_RATIO)
    comp = next(s for s in series if s.expects == "companion")
    main = next(s for s in series
                if s.variant == comp.variant and s.expects != "companion")
    assert comp.fault_window == main.fault_window


def test_fault_window_clamped_by_staleness_tail(tmp_path):
    def mut(pack):
        pack["signatures"][0].setdefault("time_axis", {})["staleness_tail"] = "2h"
    p = _mutate_fixture(tmp_path, _DISK, mut)
    base = next(s for s in _series_of(p) if s.variant == "base")
    # 380 樣本 - 240 截 = 140 → 上界 clamp 到末存活樣本 index 139（不越過截斷）
    assert base.fault_window == (wf.LEAD_STEPS * wf.STEP, 139 * wf.STEP)


def test_fault_window_absence_respects_staleness_tail(tmp_path):
    """FIX-2 回歸：absence 變體被 time_axis.staleness_tail 二次截斷時，窗下界
    須對齊「實際」死亡點（截斷後序列結尾），非 hold 起點常數——否則下界落在
    資料還在的區段 → scorer 端假 FN（血緣破口）。"""
    def mut(pack):
        pack["signatures"][0].setdefault("time_axis", {})["staleness_tail"] = "5m"
    p = _mutate_fixture(tmp_path, _SERVICE, mut)
    series = _series_of(p)
    absence = next(s for s in series if s.variant == "staleness_absence")
    # 下界 = 截斷後序列結尾（len*STEP），且嚴格早於無 tail 時的 hold 起點（11*STEP）
    assert absence.fault_window == (len(absence.samples) * wf.STEP, None)
    assert absence.fault_window[0] < 11 * wf.STEP


def test_fault_window_metadata_lineage():
    series = _series_of(_DISK)
    meta = wf.build_metadata(wf.load_pack(str(_DISK)), series, seed=1, fanout=3)
    assert meta["series"]
    for entry in meta["series"]:
        assert entry["fault_window_s"] == [wf.LEAD_STEPS * wf.STEP, 309 * wf.STEP]


def test_fault_window_none_when_truncation_eats_whole_hold():
    """value 變體的 fault-hold 段被 staleness_tail 完整截光（截斷後存活樣本
    少於 onset 起點）→ _fault_window_s 回 None + 留痕，下游 scorer 據此判
    indeterminate（不靜默計 hit/miss）。CodeRabbit #1045 nitpick：end_idx <
    fw[0] 分支此前無測試。"""
    notes: list = []
    # fw=(10,20) 的 hold 段，但截斷後只剩 5 個樣本（n_samples=5）→ end_idx=4 < 10
    fw = wf._fault_window_s("base", (10, 20), 5, notes)
    assert fw is None
    assert any("fault-hold" in n for n in notes)   # 留痕（no-silent-caps）
    # sanity：未截斷（n_samples 夠）時同輸入回正常窗、不觸發 None 分支
    assert wf._fault_window_s("base", (10, 20), 21, []) == (
        wf.LEAD_STEPS * wf.STEP, 20 * wf.STEP)


# ── hold_start_s 血緣（PR-3 early-onset 過敏標記；G-2 additive 欄） ──────

def test_hold_start_s_value_variants():
    """value 變體 hold_start_s = fw[0]*STEP（hold 段起點，較窗下界 onset 起點晚）。"""
    series = _series_of(_DISK)
    base = next(s for s in series if s.variant == "base")
    # ramp: fault-hold 段自 index 70 起（lead 10 + onset 60）→ hold_start = 70*STEP
    assert base.hold_start_s == 70 * wf.STEP
    assert base.hold_start_s > base.fault_window[0]     # 較 onset 起點晚
    for s in series:
        if s.expects != "companion":
            assert s.hold_start_s == base.hold_start_s, s.variant


def test_hold_start_s_absence_equals_window_lower_bound():
    """staleness_absence hold_start_s = 窗下界（early_onset 對 absence 永不觸發）。"""
    series = _series_of(_SERVICE)
    absence = next(s for s in series if s.variant == "staleness_absence")
    assert absence.hold_start_s == absence.fault_window[0]


def test_hold_start_s_none_when_fault_window_none():
    """截斷吃光 hold 段（fault_window=None）→ hold_start_s=None（無窗可標）。"""
    assert wf._hold_start_s("base", (10, 20), None) is None
    # sanity：正常窗回 fw[0]*STEP
    assert wf._hold_start_s(
        "base", (10, 20), (wf.LEAD_STEPS * wf.STEP, 20 * wf.STEP)) == 10 * wf.STEP


def test_hold_start_s_companion_inherits():
    series = _series_of(_RATIO)
    comp = next(s for s in series if s.expects == "companion")
    main = next(s for s in series
                if s.variant == comp.variant and s.expects != "companion")
    assert comp.hold_start_s == main.hold_start_s


def test_hold_start_s_metadata_lineage():
    series = _series_of(_DISK)
    meta = wf.build_metadata(wf.load_pack(str(_DISK)), series, seed=1, fanout=3)
    assert meta["series"]
    for entry in meta["series"]:
        assert entry["hold_start_s"] == 70 * wf.STEP
