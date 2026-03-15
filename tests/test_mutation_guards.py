#!/usr/bin/env python3
"""test_mutation_guards.py — 手動 mutation guard 測試。

驗證關鍵函式的 assertion 足以偵測常見 mutation：
  - 邊界值 off-by-one（>= vs >, <= vs <）
  - 回傳值倒反（True ↔ False）
  - 運算符替換（+ vs -, * vs /）
  - None 回傳遺漏

這些測試針對 _lib_python.py 中的核心函式，補強 property test 之外
的「mutation 偵測力」。
"""

import pytest

from _lib_python import (
    parse_duration_seconds,
    format_duration,
    validate_and_clamp,
    is_disabled,
    load_yaml_file,
    GUARDRAILS,
)


# ── parse_duration_seconds: off-by-one 偵測 ──────────────────


class TestParseDurationMutationGuards:
    """確保邊界值改變會被偵測到。"""

    @pytest.mark.parametrize("input_val,expected", [
        ("1s", 1),
        ("2s", 2),
        ("59s", 59),
        ("60s", 60),
        ("1m", 60),
        ("2m", 120),
        ("1h", 3600),
        ("24h", 86400),
    ])
    def test_exact_value_not_off_by_one(self, input_val, expected):
        """每個換算結果精確比對，防止 off-by-one mutation。"""
        result = parse_duration_seconds(input_val)
        assert result == expected, f"parse_duration_seconds({input_val!r}) = {result}, 預期 {expected}"

    def test_zero_seconds(self):
        """0s 回傳 0，不是 None 或 1。"""
        assert parse_duration_seconds("0s") == 0
        assert parse_duration_seconds("0s") is not None

    def test_integer_passthrough_not_negated(self):
        """整數直接回傳，不會被取負號。"""
        assert parse_duration_seconds(42) == 42
        assert parse_duration_seconds(0) == 0
        assert parse_duration_seconds(1) == 1

    def test_none_for_invalid_not_zero(self):
        """無效輸入回傳 None，不是 0。"""
        result = parse_duration_seconds("invalid")
        assert result is None
        assert result != 0

    def test_unit_multipliers_differ(self):
        """s/m/h 的乘數不同——防止 mutation 把 m 的 60 改成 1。"""
        s = parse_duration_seconds("1s")
        m = parse_duration_seconds("1m")
        h = parse_duration_seconds("1h")
        assert s < m < h
        assert m == 60 * s
        assert h == 60 * m


# ── format_duration: 反向驗證 ────────────────────────────────


class TestFormatDurationMutationGuards:
    """確保 format_duration 不會因 mutation 而輸出錯誤單位。"""

    @pytest.mark.parametrize("seconds,expected", [
        (1, "1s"),
        (30, "30s"),
        (60, "1m"),
        (90, "90s"),       # 非整除 → 用最小單位 s
        (3600, "1h"),
        (7200, "2h"),
        (300, "5m"),
    ])
    def test_exact_format(self, seconds, expected):
        """精確比對輸出字串——mutation 改變乘除法會導致失敗。"""
        assert format_duration(seconds) == expected

    def test_roundtrip_identity(self):
        """format → parse 必須回到原始秒數。"""
        for secs in [1, 30, 60, 90, 300, 3600, 7200]:
            formatted = format_duration(secs)
            parsed = parse_duration_seconds(formatted)
            assert parsed == secs, f"Roundtrip failed: {secs} → {formatted!r} → {parsed}"


# ── validate_and_clamp: 邊界精確偵測 ─────────────────────────


class TestValidateAndClampMutationGuards:
    """確保 clamp 的上下界精確，mutation 改變邊界會被偵測。"""

    @pytest.mark.parametrize("field", ["group_wait", "group_interval", "repeat_interval"])
    def test_minimum_boundary_exact(self, field):
        """最小值恰好在邊界時不被 clamp。"""
        min_sec, _, _ = GUARDRAILS[field]
        min_val = format_duration(min_sec)
        result, warnings = validate_and_clamp(field, min_val, "test-tenant")
        assert parse_duration_seconds(result) == min_sec
        assert warnings == []

    @pytest.mark.parametrize("field", ["group_wait", "group_interval", "repeat_interval"])
    def test_maximum_boundary_exact(self, field):
        """最大值恰好在邊界時不被 clamp。"""
        _, max_sec, _ = GUARDRAILS[field]
        max_val = format_duration(max_sec)
        result, warnings = validate_and_clamp(field, max_val, "test-tenant")
        assert parse_duration_seconds(result) == max_sec
        assert warnings == []

    @pytest.mark.parametrize("field", ["group_wait", "group_interval", "repeat_interval"])
    def test_below_minimum_clamped_up(self, field):
        """低於最小值時 clamp 到最小值，不是 0 或更低。"""
        result, warnings = validate_and_clamp(field, "0s", "test-tenant")
        min_sec, _, _ = GUARDRAILS[field]
        assert parse_duration_seconds(result) == min_sec
        assert len(warnings) == 1

    @pytest.mark.parametrize("field", ["group_wait", "group_interval", "repeat_interval"])
    def test_above_maximum_clamped_down(self, field):
        """高於最大值時 clamp 到最大值，不是更高。"""
        result, warnings = validate_and_clamp(field, "9999h", "test-tenant")
        _, max_sec, _ = GUARDRAILS[field]
        assert parse_duration_seconds(result) == max_sec
        assert len(warnings) == 1

    def test_clamp_direction_not_inverted(self):
        """clamp 方向正確——低值不會被 clamp 到 max，高值不會被 clamp 到 min。"""
        low_result, _ = validate_and_clamp("group_wait", "0s", "t")
        high_result, _ = validate_and_clamp("group_wait", "9999h", "t")
        assert parse_duration_seconds(low_result) < parse_duration_seconds(high_result)


# ── is_disabled: 回傳值不翻轉 ────────────────────────────────


class TestIsDisabledMutationGuards:
    """確保 is_disabled 不會因 not mutation 而翻轉。"""

    @pytest.mark.parametrize("value", ["disable", "disabled", "Disable", "DISABLE", "DISABLED"])
    def test_known_disabled_values_true(self, value):
        """已知 disable 變體一律回傳 True。"""
        assert is_disabled(value) is True

    @pytest.mark.parametrize("value", ["enable", "enabled", "true", "yes", 1, 42, "custom"])
    def test_non_disabled_values_false(self, value):
        """非 disable 值一律回傳 False。"""
        assert is_disabled(value) is False

    def test_none_returns_false(self):
        """None 回傳 False，不是 True。"""
        assert is_disabled(None) is False

    def test_empty_string_returns_false(self):
        """空字串回傳 False。"""
        assert is_disabled("") is False


# ── load_yaml_file: 安全性 mutation guards ────────────────────


class TestLoadYamlSafeMutationGuards:
    """確保 load_yaml_file 不會被 mutation 繞過安全檢查。"""

    def test_returns_none_for_nonexistent(self):
        """不存在的檔案回傳 None 而不是空 dict。"""
        result = load_yaml_file("/nonexistent/path/to/file.yaml")
        assert result is None

    def test_returns_dict_for_valid(self, config_dir):
        """有效 YAML 回傳 dict 類型。"""
        from factories import write_yaml
        path = write_yaml(config_dir, "test.yaml", "key: value\n")
        result = load_yaml_file(path)
        assert isinstance(result, dict)
        assert result["key"] == "value"
