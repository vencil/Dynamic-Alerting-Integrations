"""Property-based pilot — Hypothesis tests on pure functions in scripts/tools.

Audit §④ ("更好的測試手法") flagged property-based testing as untouched
(stuck at 5% all session). This file is the proof-of-concept: 4 pure
functions across the codebase, each pinned by INVARIANTS that must hold
for any input — not by hand-picked example outputs.

The existing `tests/shared/test_property.py` is `@slow`-marked and only
exercises `_lib_python` helpers locally; it's skipped in CI. This file
runs in CI (default fast tier) with `max_examples` clamped low so the
runtime stays under a second.

Picks:
  - extract_metrics_from_expr  (scripts/tools/ops/generate_rule_pack_split)
    Pure regex over PromQL. Strong properties: builtin-fn filter,
    uppercase-token filter, set-of-str output type.
  - _parse_front_matter         (scripts/tools/dx/generate_doc_map)
    Pure YAML-ish parser. Properties: empty-input contract, no-`---`
    start contract, list/scalar value type, key-extraction soundness.
  - _audience_str               (scripts/tools/dx/generate_doc_map)
    Pure mapping over slug list. Properties: empty → "All",
    multi-item → contains commas, output is str.
  - parse_commit                (scripts/tools/dx/generate_changelog)
    Conventional-commit parser. Properties: result is None or has the
    full key shape; `breaking` is always bool; `desc` non-empty when
    regex matched.

Why this is a pilot, not a full migration:
  - Not all functions have crisp invariants — counterexample-finding
    only pays off when the property is well-defined.
  - Hypothesis adds a small per-test-file fixed cost (strategy setup);
    blanket-applying it across 600+ tests would slow CI for marginal
    benefit. Use it where the function is pure AND has clear invariants.
"""
from __future__ import annotations

import os
import sys
import string

import pytest
from hypothesis import assume, given, settings, HealthCheck
from hypothesis import strategies as st


# ── sys.path: tools subdirs (mirrors conftest.py) ──────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
for _path in (
    os.path.join(_REPO_ROOT, "scripts", "tools"),
    os.path.join(_REPO_ROOT, "scripts", "tools", "ops"),
    os.path.join(_REPO_ROOT, "scripts", "tools", "dx"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)


import generate_rule_pack_split as grps  # noqa: E402
import generate_doc_map as gdm  # noqa: E402
import generate_changelog as gc  # noqa: E402
import _lib_validation as lv  # noqa: E402


# Hypothesis settings: keep the example budget tight so this file runs
# under 1s in CI alongside the other 600+ tests.
PILOT_SETTINGS = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=500,
)


# ---------------------------------------------------------------------------
# extract_metrics_from_expr — pure regex over PromQL identifiers
# ---------------------------------------------------------------------------
class TestExtractMetricsFromExprProperties:
    @given(st.text())
    @PILOT_SETTINGS
    def test_returns_set_for_any_input(self, expr):
        # Property: output is always a set, regardless of input.
        result = grps.extract_metrics_from_expr(expr)
        assert isinstance(result, set)

    @given(st.text())
    @PILOT_SETTINGS
    def test_no_builtin_funcs_in_output(self, expr):
        # Property: built-in PromQL functions are filtered out.
        result = grps.extract_metrics_from_expr(expr)
        builtins = {
            "rate", "sum", "max", "min", "avg", "count", "topk", "bottomk",
            "histogram_quantile", "increase", "delta", "irate", "group",
            "on", "ignoring", "group_left", "group_right", "unless", "by",
        }
        assert builtins.isdisjoint(result), (
            f"builtin leaked: {builtins & result!r} for input {expr!r}"
        )

    @given(st.text())
    @PILOT_SETTINGS
    def test_no_uppercase_starting_tokens(self, expr):
        # Property: tokens beginning with an uppercase letter are
        # treated as labels and excluded from the metrics output.
        result = grps.extract_metrics_from_expr(expr)
        for token in result:
            assert token, f"empty token in output for {expr!r}"
            assert not token[0].isupper(), (
                f"uppercase-starting token in output: {token!r}"
            )

    @given(st.one_of(st.none(), st.integers(), st.lists(st.integers()), st.dictionaries(st.text(), st.text())))
    @PILOT_SETTINGS
    def test_non_string_input_returns_empty_set(self, non_str):
        assert grps.extract_metrics_from_expr(non_str) == set()

    @given(st.text(alphabet=string.ascii_letters + string.digits + "_:"))
    @PILOT_SETTINGS
    def test_output_subset_of_input_tokens(self, expr):
        # Property: every output token appears as a substring of the input.
        result = grps.extract_metrics_from_expr(expr)
        for token in result:
            assert token in expr, (
                f"output token {token!r} not in input {expr!r}"
            )

    def test_uppercase_label_tokens_excluded(self):
        # Mutation-pilot kill-test: dropping the `not m[0].isupper()` filter
        # would let labels like `Tenant` (PromQL convention: uppercase =
        # label, lowercase = metric) leak into the metrics output. The
        # property test_no_uppercase_starting_tokens covers this in
        # principle, but Hypothesis's random text strategy doesn't reliably
        # generate uppercase-prefixed tokens followed by `{`/`[`/whitespace.
        # This deterministic case pins the behavior.
        expr = 'rate(http_requests_total{Tenant="db-a"}[5m])'
        result = grps.extract_metrics_from_expr(expr)
        assert "Tenant" not in result, (
            f"uppercase label `Tenant` leaked into metrics: {result!r}"
        )
        # And confirm the lowercase metric does pass through.
        assert "http_requests_total" in result


# ---------------------------------------------------------------------------
# _parse_front_matter — YAML-ish frontmatter parser
# ---------------------------------------------------------------------------
class TestParseFrontMatterProperties:
    @given(st.text())
    @PILOT_SETTINGS
    def test_returns_dict_for_any_input(self, content):
        # Property: output is always a dict.
        assert isinstance(gdm._parse_front_matter(content), dict)

    @given(st.text().filter(lambda s: not s.startswith("---")))
    @PILOT_SETTINGS
    def test_non_frontmatter_input_returns_empty_dict(self, content):
        # Property: input not starting with `---` always returns {}.
        assert gdm._parse_front_matter(content) == {}

    @given(st.text(alphabet=string.printable, min_size=0, max_size=50))
    @PILOT_SETTINGS
    def test_unterminated_frontmatter_returns_empty(self, body):
        # Property: input that opens with `---\n` but never reaches a
        # closing `\n---` returns {}.
        assume("\n---" not in body)
        content = "---\n" + body
        assert gdm._parse_front_matter(content) == {}

    @given(st.dictionaries(
        keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
        values=st.text(alphabet=string.ascii_letters, min_size=1, max_size=20),
        min_size=1,
        max_size=5,
    ))
    @PILOT_SETTINGS
    def test_round_trip_simple_keys(self, kv):
        # Property: a hand-built simple frontmatter parses back to the
        # same key/value pairs.
        body = "\n".join(f"{k}: {v}" for k, v in kv.items())
        content = f"---\n{body}\n---\nbody"
        parsed = gdm._parse_front_matter(content)
        for k, v in kv.items():
            assert parsed.get(k) == v, (
                f"round-trip lost {k}={v!r}; got {parsed!r}"
            )

    def test_unterminated_list_value_stays_string(self):
        # Mutation-pilot kill-test: dropping the `endswith("]")` check would
        # cause `key: [a, b` (no closing bracket) to be parsed as a list
        # `["a"]` (because val[1:-1] then split by comma drops the trailing
        # `b`). With both startswith+endswith, it stays a string.
        content = "---\nkey: [a, b\n---\n"
        parsed = gdm._parse_front_matter(content)
        # Value should remain a string (no closing bracket = not a list)
        assert isinstance(parsed["key"], str), (
            f"unterminated list `[a, b` was parsed as {type(parsed['key']).__name__}: "
            f"{parsed['key']!r}"
        )


# ---------------------------------------------------------------------------
# _audience_str — slug list → display string
# ---------------------------------------------------------------------------
class TestAudienceStrProperties:
    @given(st.sampled_from(["zh", "en", "fr", "ja", "es"]))
    @PILOT_SETTINGS
    def test_empty_list_always_returns_all(self, lang):
        # Property: empty audience list always renders as "All",
        # regardless of language.
        assert gdm._audience_str([], lang) == "All"

    @given(
        st.lists(
            st.text(alphabet=string.ascii_lowercase + "-",
                    min_size=1, max_size=15),
            min_size=1,
            max_size=5,
        ),
        st.sampled_from(["zh", "en"]),
    )
    @PILOT_SETTINGS
    def test_output_is_string(self, slugs, lang):
        result = gdm._audience_str(slugs, lang)
        assert isinstance(result, str)
        assert result, "empty output for non-empty slugs"

    @given(
        st.lists(
            st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10),
            min_size=2,
            max_size=5,
            unique=True,
        ),
    )
    @PILOT_SETTINGS
    def test_multi_item_output_contains_commas(self, slugs):
        result = gdm._audience_str(slugs, "en")
        # Multi-item output is comma-joined.
        assert ", " in result, (
            f"multi-item slugs {slugs!r} produced no commas: {result!r}"
        )


# ---------------------------------------------------------------------------
# parse_commit — conventional-commit parser
# ---------------------------------------------------------------------------
class TestParseCommitProperties:
    @given(st.text())
    @PILOT_SETTINGS
    def test_result_is_none_or_full_dict(self, subject):
        # Property: parse_commit returns None OR a dict with the full
        # 4-key shape — never a partial dict.
        result = gc.parse_commit(subject)
        if result is not None:
            assert set(result.keys()) == {"type", "scope", "breaking", "desc"}

    @given(st.text())
    @PILOT_SETTINGS
    def test_breaking_is_always_bool_when_parsed(self, subject):
        # Property: when a result is returned, `breaking` is always bool.
        result = gc.parse_commit(subject)
        if result is not None:
            assert isinstance(result["breaking"], bool)

    @given(
        st.sampled_from(["feat", "fix", "docs", "style", "refactor",
                          "test", "chore", "perf", "ci", "build"]),
        st.text(alphabet=string.ascii_lowercase + "-",
                min_size=1, max_size=15),
        st.text(alphabet=string.printable,
                min_size=1, max_size=80).filter(
                    lambda s: "\n" not in s and "\r" not in s),
    )
    @PILOT_SETTINGS
    def test_well_formed_subjects_parse(self, type_, scope, desc):
        # Property: well-formed type(scope): desc subjects always parse.
        subject = f"{type_}({scope}): {desc.strip() or 'a'}"
        result = gc.parse_commit(subject)
        assert result is not None, f"failed to parse: {subject!r}"
        assert result["type"] == type_
        assert result["scope"] == scope
        assert result["breaking"] is False  # no `!` marker

    def test_missing_scope_returns_empty_string_not_none(self):
        # Mutation-pilot kill-test: removing the `or ""` fallback in
        # `m.group("scope") or ""` would let scope=None leak through when
        # the commit subject has no `(scope)` group. Downstream code in
        # generate_changelog assumes scope is a string (uses `c["scope"]`
        # in f-strings); a None there would render as the literal "None".
        result = gc.parse_commit("feat: add new feature")
        assert result is not None
        assert result["scope"] == "", (
            f"missing-scope commit returned scope={result['scope']!r}; "
            f"expected '' for downstream string concat to work"
        )
        assert isinstance(result["scope"], str)

    @given(
        st.sampled_from(["feat", "fix", "docs", "refactor", "perf"]),
        st.text(alphabet=string.printable,
                min_size=1, max_size=60).filter(
                    lambda s: "\n" not in s and "\r" not in s),
    )
    @PILOT_SETTINGS
    def test_breaking_marker_detected(self, type_, desc):
        # Property: `type!: desc` form sets breaking=True.
        subject = f"{type_}!: {desc.strip() or 'a'}"
        result = gc.parse_commit(subject)
        if result is not None:
            assert result["breaking"] is True


# ---------------------------------------------------------------------------
# parse_duration_seconds — Prometheus-style duration parser
# ---------------------------------------------------------------------------
# Pilot extension batch 2 (paired with mutation pilot extension): four pure
# functions in _lib_validation.py that drive timing guardrails throughout
# the platform. Each function gets property tests here AND mutation entries
# in tests/shared/_mutation_pilot.py. Together they pin parse↔format
# round-trip, monotonicity, idempotency, and clamp invariants.
class TestParseDurationSecondsProperties:

    @given(st.integers(min_value=0, max_value=10**6))
    @PILOT_SETTINGS
    def test_int_input_returned_as_int(self, n):
        # Property: int input passes through as int.
        result = lv.parse_duration_seconds(n)
        assert result == n
        assert isinstance(result, int)

    @given(st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False))
    @PILOT_SETTINGS
    def test_float_input_truncated_to_int(self, x):
        # Property: float input is converted via int() (truncation).
        result = lv.parse_duration_seconds(x)
        assert result == int(x)
        assert isinstance(result, int)

    @given(st.one_of(
        st.none(),
        st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(), st.text(), max_size=2),
    ))
    @PILOT_SETTINGS
    def test_invalid_input_returns_none(self, junk):
        # Property: anything that isn't a duration string or numeric → None.
        # The text strategy is constrained to alphabet only (no digits) so
        # it can't accidentally produce a valid duration.
        result = lv.parse_duration_seconds(junk)
        assert result is None

    @given(st.sampled_from([
        ("5s", 5), ("30s", 30), ("60s", 60),
        ("1m", 60), ("5m", 300),
        ("1h", 3600), ("4h", 14400),
        ("1d", 86400),
    ]))
    @PILOT_SETTINGS
    def test_known_durations(self, kv):
        # Property: well-known examples from the docstring.
        s, expected = kv
        assert lv.parse_duration_seconds(s) == expected

    @given(st.integers(min_value=1, max_value=1000))
    @PILOT_SETTINGS
    def test_seconds_unit_monotonic(self, n):
        # Property: more seconds (same unit) → larger output.
        a = lv.parse_duration_seconds(f"{n}s")
        b = lv.parse_duration_seconds(f"{n + 1}s")
        assert a is not None and b is not None
        assert b > a


# ---------------------------------------------------------------------------
# format_duration — seconds → "Ns" / "Nm" / "Nh"
# ---------------------------------------------------------------------------
class TestFormatDurationProperties:

    @given(st.integers(min_value=0, max_value=10**6))
    @PILOT_SETTINGS
    def test_output_ends_in_smh(self, n):
        # Property: output suffix is always s, m, or h (never d).
        out = lv.format_duration(n)
        assert out[-1] in ("s", "m", "h")
        assert "d" not in out, f"format_duration({n}) emitted day unit: {out!r}"

    @given(st.integers(min_value=0, max_value=10**6))
    @PILOT_SETTINGS
    def test_round_trip_via_parse(self, n):
        # Property: format → parse round-trips.
        formatted = lv.format_duration(n)
        parsed = lv.parse_duration_seconds(formatted)
        assert parsed == n, (
            f"round-trip failed: {n} → {formatted!r} → {parsed}"
        )

    @given(st.integers(min_value=1, max_value=3599))
    @PILOT_SETTINGS
    def test_sub_hour_uses_minute_or_second(self, n):
        # Property: values < 3600 never use 'h' (no whole hours present).
        out = lv.format_duration(n)
        assert not out.endswith("h"), (
            f"format_duration({n}) chose hour unit despite n<3600: {out!r}"
        )


# ---------------------------------------------------------------------------
# is_disabled — three-state disabled detection
# ---------------------------------------------------------------------------
class TestIsDisabledProperties:

    @given(st.sampled_from(["disable", "disabled", "off", "false"]))
    @PILOT_SETTINGS
    def test_canonical_values_disabled(self, val):
        assert lv.is_disabled(val) is True

    @given(st.sampled_from(["disable", "disabled", "off", "false"]))
    @PILOT_SETTINGS
    def test_case_insensitive(self, val):
        # Property: case doesn't matter.
        assert lv.is_disabled(val.upper()) is True
        assert lv.is_disabled(val.title()) is True

    @given(
        st.sampled_from(["disable", "disabled", "off", "false"]),
        st.text(alphabet=" \t", min_size=0, max_size=5),
        st.text(alphabet=" \t", min_size=0, max_size=5),
    )
    @PILOT_SETTINGS
    def test_whitespace_stripped(self, val, lpad, rpad):
        # Property: leading/trailing whitespace is stripped.
        assert lv.is_disabled(lpad + val + rpad) is True

    @given(st.one_of(
        st.none(),
        st.integers(),
        st.lists(st.text()),
        st.dictionaries(st.text(), st.text()),
    ))
    @PILOT_SETTINGS
    def test_non_string_returns_false(self, junk):
        # Property: non-string input always returns False.
        assert lv.is_disabled(junk) is False

    @given(st.text(alphabet=string.ascii_letters,
                   min_size=1, max_size=15).filter(
                       lambda s: s.strip().lower()
                                  not in {"disable", "disabled", "off", "false"}))
    @PILOT_SETTINGS
    def test_arbitrary_strings_not_disabled(self, s):
        # Property: any string that ISN'T one of the canonical disable
        # values returns False.
        assert lv.is_disabled(s) is False


# ---------------------------------------------------------------------------
# validate_and_clamp — guardrail enforcement on timing parameters
# ---------------------------------------------------------------------------
class TestValidateAndClampProperties:

    @given(
        st.text(alphabet=string.ascii_letters + "_",
                min_size=1, max_size=20).filter(
                    lambda s: s not in
                              {"group_wait", "group_interval", "repeat_interval"}),
        st.integers(min_value=0, max_value=10**5),
    )
    @PILOT_SETTINGS
    def test_unknown_param_passes_through_unchanged(self, param, value):
        # Property: any param not in GUARDRAILS returns input unchanged
        # with empty warnings.
        clamped, warnings = lv.validate_and_clamp(param, value, "test-tenant")
        assert clamped == value
        assert warnings == []

    @given(st.sampled_from([
        ("group_wait", "30s"),
        ("group_interval", "5m"),
        ("repeat_interval", "4h"),
    ]))
    @PILOT_SETTINGS
    def test_in_bounds_value_unchanged(self, kv):
        # Property: a value within [min, max] passes through unchanged.
        param, value = kv
        clamped, warnings = lv.validate_and_clamp(param, value, "test-tenant")
        assert clamped == value
        assert warnings == []

    @given(st.sampled_from([
        ("group_wait", "1s"),       # below 5s min
        ("group_interval", "1s"),   # below 5s min
        ("repeat_interval", "10s"), # below 60s min
    ]))
    @PILOT_SETTINGS
    def test_below_min_clamped_to_min(self, kv):
        # Property: values below min get clamped to the min, with a warning.
        from _lib_constants import GUARDRAILS
        param, value = kv
        clamped, warnings = lv.validate_and_clamp(param, value, "test-tenant")
        min_sec = GUARDRAILS[param][0]
        assert lv.parse_duration_seconds(clamped) == min_sec
        assert len(warnings) == 1
        assert "below minimum" in warnings[0]

    @given(st.sampled_from([
        ("group_wait", "10m"),       # above 300s max
        ("group_interval", "10m"),   # above 300s max
        ("repeat_interval", "100h"), # above 259200s max (72h)
    ]))
    @PILOT_SETTINGS
    def test_above_max_clamped_to_max(self, kv):
        # Property: values above max get clamped to the max, with a warning.
        from _lib_constants import GUARDRAILS
        param, value = kv
        clamped, warnings = lv.validate_and_clamp(param, value, "test-tenant")
        max_sec = GUARDRAILS[param][1]
        assert lv.parse_duration_seconds(clamped) == max_sec
        assert len(warnings) == 1
        assert "above maximum" in warnings[0]
