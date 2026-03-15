#!/usr/bin/env python3
"""test_property.py — Property-based testing (Hypothesis)。

使用隨機輸入模糊測試核心函式的不變式：
  1. parse_duration_seconds() — 任何合法 duration 字串必回傳正數
  2. validate_and_clamp() — 任何輸入必回傳在 guardrail 範圍內的值
  3. format_duration() — 與 parse_duration_seconds 互為反函式
  4. is_disabled() — 布林行為不變式
"""

import pytest
from hypothesis import given, assume, settings, example
from hypothesis import strategies as st

pytestmark = pytest.mark.slow

import _lib_python as lib


# ============================================================
# parse_duration_seconds properties
# ============================================================

class TestParseDurationProperties:
    """parse_duration_seconds() 不變式。"""

    @given(value=st.integers(min_value=0, max_value=10**9))
    @settings(max_examples=200)
    def test_int_passthrough(self, value):
        """任何非負整數直接回傳原值。"""
        result = lib.parse_duration_seconds(value)
        assert result == value

    @given(value=st.floats(min_value=0, max_value=10**9, allow_nan=False,
                            allow_infinity=False))
    @settings(max_examples=200)
    def test_float_truncated_to_int(self, value):
        """浮點數輸入截斷為 int（回傳型別 Optional[int]）。"""
        result = lib.parse_duration_seconds(value)
        assert result == int(value)

    @given(n=st.integers(min_value=1, max_value=10**6),
           unit=st.sampled_from(["s", "m", "h"]))
    @settings(max_examples=300)
    def test_valid_duration_returns_positive(self, n, unit):
        """任何 {n}{unit} 格式回傳正數。"""
        duration = f"{n}{unit}"
        result = lib.parse_duration_seconds(duration)
        assert result is not None
        assert result > 0

    @given(n=st.integers(min_value=1, max_value=10**6),
           unit=st.sampled_from(["s", "m", "h"]))
    @settings(max_examples=200)
    def test_unit_scaling_correct(self, n, unit):
        """單位換算倍率正確：s=1, m=60, h=3600。"""
        duration = f"{n}{unit}"
        result = lib.parse_duration_seconds(duration)
        multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
        assert result == n * multiplier

    @given(text=st.text(min_size=0, max_size=20))
    @settings(max_examples=500)
    def test_never_raises(self, text):
        """任何字串輸入都不應拋出例外（回傳值或 None）。"""
        result = lib.parse_duration_seconds(text)
        assert result is None or isinstance(result, (int, float))

    @given(value=st.one_of(
        st.none(),
        st.lists(st.integers()),
        st.dictionaries(st.text(), st.integers()),
        st.booleans(),
    ))
    @settings(max_examples=100)
    def test_non_string_non_numeric_returns_none(self, value):
        """非字串非數值輸入回傳 None。"""
        assume(not isinstance(value, (int, float)))
        result = lib.parse_duration_seconds(value)
        assert result is None


# ============================================================
# validate_and_clamp properties
# ============================================================

class TestValidateAndClampProperties:
    """validate_and_clamp() 不變式。"""

    @given(seconds=st.integers(min_value=1, max_value=10**7))
    @settings(max_examples=300)
    def test_group_wait_always_in_bounds(self, seconds):
        """group_wait 結果永遠在 5s–300s (5m) 範圍內。"""
        val, warnings = lib.validate_and_clamp("group_wait", f"{seconds}s", "test")
        result_seconds = lib.parse_duration_seconds(val)
        assert result_seconds is not None
        assert 5 <= result_seconds <= 300

    @given(seconds=st.integers(min_value=1, max_value=10**7))
    @settings(max_examples=300)
    def test_group_interval_always_in_bounds(self, seconds):
        """group_interval 結果永遠在 5s–300s (5m) 範圍內。"""
        val, warnings = lib.validate_and_clamp("group_interval", f"{seconds}s", "test")
        result_seconds = lib.parse_duration_seconds(val)
        assert result_seconds is not None
        assert 5 <= result_seconds <= 300

    @given(seconds=st.integers(min_value=1, max_value=10**7))
    @settings(max_examples=300)
    def test_repeat_interval_always_in_bounds(self, seconds):
        """repeat_interval 結果永遠在 60s–259200s (72h) 範圍內。"""
        val, warnings = lib.validate_and_clamp("repeat_interval", f"{seconds}s", "test")
        result_seconds = lib.parse_duration_seconds(val)
        assert result_seconds is not None
        assert 60 <= result_seconds <= 259200

    @given(seconds=st.integers(min_value=5, max_value=300))
    @settings(max_examples=200)
    def test_within_bounds_no_warning(self, seconds):
        """group_wait 在合法範圍內無 warning。"""
        val, warnings = lib.validate_and_clamp("group_wait", f"{seconds}s", "test")
        assert warnings == []
        assert val == f"{seconds}s"

    @given(param=st.sampled_from(["group_wait", "group_interval", "repeat_interval"]),
           text=st.text(min_size=0, max_size=20))
    @settings(max_examples=300)
    def test_invalid_input_never_crashes(self, param, text):
        """任何字串輸入都不應拋出例外。"""
        val, warnings = lib.validate_and_clamp(param, text, "test")
        # 回傳值永遠是字串或數值
        assert val is not None


# ============================================================
# format_duration ↔ parse_duration_seconds roundtrip
# ============================================================

class TestFormatDurationRoundtrip:
    """format_duration 與 parse_duration_seconds 往返。"""

    @given(seconds=st.integers(min_value=0, max_value=10**6))
    @settings(max_examples=300)
    def test_format_then_parse_identity(self, seconds):
        """format_duration → parse_duration_seconds 回到原值。"""
        formatted = lib.format_duration(seconds)
        parsed = lib.parse_duration_seconds(formatted)
        assert parsed == seconds

    @given(n=st.integers(min_value=1, max_value=10**6),
           unit=st.sampled_from(["s", "m", "h"]))
    @settings(max_examples=200)
    def test_parse_then_format_then_parse_stable(self, n, unit):
        """parse → format → parse 結果穩定。"""
        original = f"{n}{unit}"
        seconds = lib.parse_duration_seconds(original)
        formatted = lib.format_duration(seconds)
        reparsed = lib.parse_duration_seconds(formatted)
        assert reparsed == seconds


# ============================================================
# is_disabled properties
# ============================================================

class TestIsDisabledProperties:
    """is_disabled() 不變式。"""

    @given(text=st.text(min_size=0, max_size=50))
    @settings(max_examples=500)
    def test_always_returns_bool(self, text):
        """任何字串輸入永遠回傳布林值。"""
        result = lib.is_disabled(text)
        assert isinstance(result, bool)

    @given(variant=st.sampled_from([
        "disable", "disabled", "off", "false",
        "DISABLE", "DISABLED", "OFF", "FALSE",
        "Disable", "Disabled", "Off", "False",
    ]))
    def test_known_disabled_variants(self, variant):
        """已知 disabled 變體永遠回傳 True。"""
        assert lib.is_disabled(variant) is True
        # 含前後空白也要成立
        assert lib.is_disabled(f"  {variant}  ") is True
