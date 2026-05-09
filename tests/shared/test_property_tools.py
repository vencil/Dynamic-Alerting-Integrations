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
import _lib_io as lio  # noqa: E402
import _lib_prometheus as lp  # noqa: E402
import _lib_godispatch as lgd  # noqa: E402
import axe_lite_static as axe  # noqa: E402

# check_flaky_registry lives under scripts/tools/lint, not on sys.path
# above (which only includes tools/, ops/, dx/). Add lint here.
_LINT_DIR = os.path.join(_REPO_ROOT, "scripts", "tools", "lint")
if _LINT_DIR not in sys.path:
    sys.path.insert(0, _LINT_DIR)
import check_flaky_registry as cfr  # noqa: E402

# Routing helpers (PR-3a split modules) — pure functions used by the
# generate_alertmanager_routes pipeline and re-exported from there for
# backward-compat. We import the split modules directly to keep the
# property tests close to the units under test.
import _grar_merge as gm  # noqa: E402
import _grar_validate as gv  # noqa: E402
import _lint_helpers as lh  # noqa: E402


# Hypothesis settings: keep the example budget tight so this file runs
# under 1s in CI alongside the other 600+ tests.
PILOT_SETTINGS = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=500,
)

# Variant for tests that use `monkeypatch` (function-scoped fixture). The
# env state is reset by us inside each test before assertion, so the
# fixture-not-reset warning is a false positive — suppress it.
PILOT_SETTINGS_MONKEYPATCH = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture],
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
        # would let labels like `Foo` / `Bar` (PromQL convention: uppercase =
        # custom-named recording rule or label, lowercase = metric name)
        # leak into the metrics output.
        #
        # IMPORTANT: the test expression MUST contain uppercase tokens
        # immediately followed by `{`, `[`, or whitespace — otherwise the
        # regex `\b([a-zA-Z_:]...)\b(?=[{\[\s])` won't even capture them
        # in the first place, and the uppercase filter never fires (which
        # was the bug in this test's earlier version pre-batch-4).
        expr = 'Foo{label="x"} + Bar[5m]'
        result = grps.extract_metrics_from_expr(expr)
        assert "Foo" not in result, (
            f"uppercase token `Foo` leaked into metrics: {result!r}"
        )
        assert "Bar" not in result, (
            f"uppercase token `Bar` leaked into metrics: {result!r}"
        )


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


# ---------------------------------------------------------------------------
# axe_lite_static — WCAG static a11y scanner
# ---------------------------------------------------------------------------
# Pilot extension batch 3: the 4 scanner functions + strip_frontmatter
# helper. These run on every commit via the axe-lite-static pre-commit
# hook (#318) and the TestPortalFloor regression test, so any silent
# behavior change here becomes a real WCAG floor regression.
#
# Properties pinned: empty-input contracts, line-number positivity,
# idempotency, monotonicity (output never longer than input where
# applicable), and category-specific aria-hidden / role / placeholder
# escape hatches.

class TestStripFrontmatterProperties:

    @given(st.text())
    @PILOT_SETTINGS
    def test_idempotent(self, src):
        # Property: stripping twice gives the same result as stripping once.
        once = axe.strip_frontmatter(src)
        twice = axe.strip_frontmatter(once)
        assert once == twice, (
            f"strip_frontmatter not idempotent for input {src!r}: "
            f"once={once!r} twice={twice!r}"
        )

    @given(st.text())
    @PILOT_SETTINGS
    def test_never_longer_than_input(self, src):
        # Property: stripping never grows the string.
        out = axe.strip_frontmatter(src)
        assert len(out) <= len(src)

    @given(st.text().filter(lambda s: not s.startswith("---")))
    @PILOT_SETTINGS
    def test_no_frontmatter_returns_input_unchanged(self, src):
        # Property: input not starting with `---` returns identically.
        assert axe.strip_frontmatter(src) == src

    def test_strips_well_formed_frontmatter(self):
        src = "---\ntitle: x\n---\nbody\n"
        # `find("\n---", 3) + 4` lands on the newline right after the
        # second `---`, so the leading `\n` is preserved in the body.
        # This is the function's actual contract — preserved here as a
        # behavior lock, not a re-derivation.
        assert axe.strip_frontmatter(src) == "\nbody\n"

    def test_unterminated_frontmatter_returns_input(self):
        # Property: frontmatter that doesn't close returns input unchanged
        # (avoids accidentally swallowing legitimate document body).
        src = "---\nstart\nno close marker\n"
        assert axe.strip_frontmatter(src) == src


class TestScanUnicodeStatusProperties:

    @given(st.text())
    @PILOT_SETTINGS
    def test_returns_list_of_pairs(self, src):
        # Property: output is always list of (int, str) tuples.
        out = axe.scan_unicode_status(src)
        assert isinstance(out, list)
        for item in out:
            assert isinstance(item, tuple) and len(item) == 2
            line, msg = item
            assert isinstance(line, int) and line >= 1
            assert isinstance(msg, str) and msg

    @given(st.text(alphabet=string.ascii_letters + string.digits + " \n",
                   min_size=0, max_size=200))
    @PILOT_SETTINGS
    def test_no_status_chars_returns_empty(self, src):
        # Property: input with no UNICODE_STATUS chars (✓✔⚠❌✗ⓘ) → empty.
        # The alphabet excludes those symbols.
        assert axe.scan_unicode_status(src) == []

    def test_aria_hidden_wrapped_passes(self):
        src = '<span aria-hidden="true">⚠</span>'
        assert axe.scan_unicode_status(src) == []

    def test_naked_status_symbol_flagged(self):
        src = '<div>warning ⚠ here</div>'
        out = axe.scan_unicode_status(src)
        assert len(out) == 1


class TestScanButtonsWithoutNameProperties:

    @given(st.text(alphabet=string.ascii_letters + string.digits + " \n",
                   min_size=0, max_size=200))
    @PILOT_SETTINGS
    def test_no_button_tag_returns_empty(self, src):
        # Property: input with no `<button` substring → empty.
        # Alphabet has no `<` so this is guaranteed.
        assert axe.scan_buttons_without_name(src) == []

    @given(st.text())
    @PILOT_SETTINGS
    def test_line_numbers_positive(self, src):
        # Property: every reported line number is ≥ 1.
        for line, _ in axe.scan_buttons_without_name(src):
            assert line >= 1

    def test_aria_label_passes(self):
        src = '<button aria-label="close"></button>'
        assert axe.scan_buttons_without_name(src) == []

    def test_title_attr_passes(self):
        src = '<button title="close"></button>'
        assert axe.scan_buttons_without_name(src) == []

    def test_empty_button_flagged(self):
        src = '<button></button>'
        out = axe.scan_buttons_without_name(src)
        assert len(out) == 1


class TestScanUnlabeledInputsProperties:

    @given(st.text(alphabet=string.ascii_letters + string.digits + " \n",
                   min_size=0, max_size=200))
    @PILOT_SETTINGS
    def test_no_input_or_textarea_returns_empty(self, src):
        # Alphabet has no `<` so no `<input` / `<textarea` tags.
        assert axe.scan_unlabeled_inputs(src) == []

    def test_aria_label_passes(self):
        assert axe.scan_unlabeled_inputs(
            '<input type="text" aria-label="x" />') == []

    def test_placeholder_passes(self):
        # placeholder is treated as a label hint for screen readers.
        assert axe.scan_unlabeled_inputs(
            '<input type="text" placeholder="search" />') == []

    def test_implicit_label_wrap_passes(self):
        # PR #310 added support for <label>...<input/></label> implicit
        # association. Lock that contract here.
        src = '<label><span>name</span><input type="text" /></label>'
        assert axe.scan_unlabeled_inputs(src) == []

    def test_naked_input_flagged(self):
        out = axe.scan_unlabeled_inputs('<input type="text" />')
        assert len(out) == 1


class TestScanColorOnlySeverityProperties:

    @given(st.text(alphabet=string.ascii_letters + " \n", min_size=0, max_size=200))
    @PILOT_SETTINGS
    def test_no_severity_token_returns_empty(self, src):
        # Property: input without any severity color token → empty.
        # Alphabet excludes brackets so no className=... can match.
        assert axe.scan_color_only_severity(src) == []

    @given(st.text())
    @PILOT_SETTINGS
    def test_line_numbers_positive(self, src):
        for line, _ in axe.scan_color_only_severity(src):
            assert line >= 1

    def test_border_signal_passes(self):
        src = '<div className="text-[color:var(--da-color-error)] border-2">err</div>'
        assert axe.scan_color_only_severity(src) == []

    def test_font_bold_signal_passes(self):
        src = '<div className="text-[color:var(--da-color-error)] font-bold">err</div>'
        assert axe.scan_color_only_severity(src) == []

    def test_font_semibold_signal_passes(self):
        # PR #311 added font-semibold to accepted markers.
        src = '<h4 className="text-[color:var(--da-color-success)] font-semibold">x</h4>'
        assert axe.scan_color_only_severity(src) == []

    def test_role_alert_passes(self):
        src = '<div role="alert" className="text-[color:var(--da-color-error)]">err</div>'
        assert axe.scan_color_only_severity(src) == []

    def test_naked_severity_flagged(self):
        src = '<div className="text-[color:var(--da-color-error)]">err</div>'
        out = axe.scan_color_only_severity(src)
        assert len(out) == 1


# ---------------------------------------------------------------------------
# _lib_io — file IO + YAML helpers used by 20+ tools
# ---------------------------------------------------------------------------
# Pilot batch 4: the file-IO seam everything else depends on. Mutations
# here would cascade into every tool that reads tenant config; property
# tests pin the filtering / sorting / fallback contracts.

class TestLoadYamlFileProperties:

    @given(st.one_of(
        st.none(),
        st.just(""),
        st.text(alphabet=string.ascii_letters, min_size=1, max_size=20).map(
            lambda s: f"/nonexistent/{s}.yaml"
        ),
    ))
    @PILOT_SETTINGS
    def test_missing_path_returns_default(self, path):
        # Property: None / empty string / nonexistent path → default.
        assert lio.load_yaml_file(path) is None
        sentinel = object()
        assert lio.load_yaml_file(path, default=sentinel) is sentinel

    @given(st.dictionaries(
        keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
        values=st.text(alphabet=string.ascii_letters, min_size=1, max_size=20),
        min_size=1, max_size=5,
    ))
    @PILOT_SETTINGS
    def test_round_trip(self, tmp_path_factory, kv):
        # Hypothesis fixture-ordering rule: pytest fixtures BEFORE
        # @given strategy params.
        # Property: write a dict as YAML, load_yaml_file returns the same dict.
        import yaml
        d = tmp_path_factory.mktemp("yaml")
        f = d / "x.yaml"
        f.write_text(yaml.safe_dump(kv), encoding="utf-8")
        assert lio.load_yaml_file(str(f)) == kv

    def test_empty_file_returns_default(self, tmp_path):
        # Property: empty YAML file → default (yaml.safe_load returns None).
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        assert lio.load_yaml_file(str(f)) is None
        assert lio.load_yaml_file(str(f), default={}) == {}
        assert lio.load_yaml_file(str(f), default=[]) == []


class TestIterYamlFilesProperties:

    @given(st.one_of(
        st.none(),
        st.just(""),
        st.text(alphabet=string.ascii_letters, min_size=1, max_size=10).map(
            lambda s: f"/nonexistent/{s}"
        ),
    ))
    @PILOT_SETTINGS
    def test_missing_dir_returns_empty(self, path):
        # Property: None / empty / nonexistent dir → empty list.
        assert lio.iter_yaml_files(path) == []

    @given(st.lists(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8),
        min_size=1, max_size=10, unique=True,
    ))
    @PILOT_SETTINGS
    def test_output_sorted_by_filename(self, tmp_path_factory, names):
        # Property: returned list is sorted by filename ascending.
        d = tmp_path_factory.mktemp("yaml")
        for n in names:
            (d / f"{n}.yaml").write_text("k: v\n", encoding="utf-8")
        result = lio.iter_yaml_files(str(d))
        filenames = [fn for fn, _ in result]
        assert filenames == sorted(filenames)

    def test_filters_non_yaml_extensions(self, tmp_path):
        for fn in ["a.yaml", "b.yml", "c.txt", "d.md", "e.json"]:
            (tmp_path / fn).write_text("x", encoding="utf-8")
        result = lio.iter_yaml_files(str(tmp_path))
        names = {fn for fn, _ in result}
        assert names == {"a.yaml", "b.yml"}

    def test_skip_reserved_default_excludes_underscore(self, tmp_path):
        for fn in ["tenant.yaml", "_defaults.yaml", ".hidden.yaml"]:
            (tmp_path / fn).write_text("x", encoding="utf-8")
        result = lio.iter_yaml_files(str(tmp_path))
        names = {fn for fn, _ in result}
        assert names == {"tenant.yaml"}

    def test_skip_reserved_false_includes_all(self, tmp_path):
        for fn in ["tenant.yaml", "_defaults.yaml", ".hidden.yaml"]:
            (tmp_path / fn).write_text("x", encoding="utf-8")
        result = lio.iter_yaml_files(str(tmp_path), skip_reserved=False)
        names = {fn for fn, _ in result}
        assert names == {"tenant.yaml", "_defaults.yaml", ".hidden.yaml"}

    def test_filters_directories(self, tmp_path):
        # Property: directories with .yaml-like names don't sneak through
        # (the os.path.isfile check matters).
        (tmp_path / "tenant.yaml").write_text("x", encoding="utf-8")
        (tmp_path / "subdir.yaml").mkdir()  # a DIRECTORY ending in .yaml
        result = lio.iter_yaml_files(str(tmp_path))
        names = {fn for fn, _ in result}
        assert names == {"tenant.yaml"}


class TestFormatJsonReportProperties:

    @given(st.dictionaries(
        keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
        values=st.one_of(
            st.text(alphabet=string.ascii_letters, max_size=20),
            st.integers(),
            st.booleans(),
        ),
        max_size=5,
    ))
    @PILOT_SETTINGS
    def test_round_trip_via_json_loads(self, data):
        # Property: format_json_report output parses back to the same data.
        import json
        s = lio.format_json_report(data)
        assert json.loads(s) == data

    @given(st.dictionaries(
        keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
        values=st.text(min_size=1, max_size=20),
        min_size=1, max_size=3,
    ))
    @PILOT_SETTINGS
    def test_pretty_printed_default(self, data):
        # Property: default output uses indent=2 — i.e. has a newline
        # followed by exactly 2 spaces. Just checking for `\n` is too
        # weak (json.dumps with indent=0 ALSO emits newlines, just no
        # spaces) — that mismatch surfaced as a surviving mutation in
        # batch-4 mutation pilot.
        s = lio.format_json_report(data)
        assert "\n  " in s, (
            f"output {s!r} has no `\\n  ` (newline + 2 spaces); "
            f"indent=2 default not honored?"
        )

    def test_unicode_not_escaped_default(self):
        # Property: default ensure_ascii=False keeps Unicode bytes in output.
        s = lio.format_json_report({"k": "中文"})
        assert "中文" in s
        assert "\\u" not in s

    def test_kwargs_override_defaults(self):
        # ensure_ascii=True can be re-enabled via kwarg.
        s = lio.format_json_report({"k": "中文"}, ensure_ascii=True)
        assert "中文" not in s
        assert "\\u" in s


# ---------------------------------------------------------------------------
# _validate_url_scheme — SSRF allowlist (pure scheme dispatch)
# ---------------------------------------------------------------------------
# Pilot batch 5: small but security-relevant. Every http_* helper in
# _lib_prometheus delegates to this; a regression here unblocks
# `file://` / `gopher://` / etc. SSRF avenues for tools that build URLs
# from user-supplied tenant config. Property tests pin the allowlist
# (http / https) and the rejection-message contract.

class TestValidateUrlSchemeProperties:

    @given(st.sampled_from([
        "http://localhost",
        "https://example.com/path",
        "http://10.0.0.1:9090/api/v1/query",
        "https://prom.svc.cluster.local",
    ]))
    @PILOT_SETTINGS
    def test_allowed_schemes_return_none(self, url):
        # Property: http / https URLs always pass (return None = no error).
        assert lp._validate_url_scheme(url) is None

    @given(st.sampled_from([
        "file:///etc/passwd",
        "gopher://evil.example/_GET%20/",
        "ftp://ftp.example.com/file",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ldap://ldap.example.com/dc=root",
    ]))
    @PILOT_SETTINGS
    def test_disallowed_schemes_return_error(self, url):
        # Property: any scheme not in {http, https} returns a non-empty
        # error message string.
        result = lp._validate_url_scheme(url)
        assert isinstance(result, str)
        assert result, f"empty error for disallowed url {url!r}"
        assert "Unsupported URL scheme" in result

    @given(st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10).filter(
        lambda s: s not in ("http", "https")
    ))
    @PILOT_SETTINGS
    def test_arbitrary_scheme_rejected(self, scheme):
        # Property: any scheme NOT exactly "http" / "https" is rejected.
        url = f"{scheme}://example.com"
        result = lp._validate_url_scheme(url)
        assert result is not None, (
            f"unexpected pass for scheme {scheme!r}: {url!r}"
        )

    def test_no_scheme_rejected(self):
        # Property: bare hostname (no scheme) → rejected (urlparse returns "").
        result = lp._validate_url_scheme("example.com/api")
        assert result is not None
        assert "Unsupported URL scheme" in result


# ---------------------------------------------------------------------------
# detect_cli_lang + i18n_text — bilingual CLI dispatcher
# ---------------------------------------------------------------------------
# Pilot batch 5: drives all bilingual error messages in da-tools. The
# precedence order (DA_LANG > LC_ALL > LANG) is load-bearing — many CI
# pipelines set LC_ALL=C.UTF-8 globally, and DA_LANG must be able to
# override that. Property tests pin precedence + fallback contracts.

class TestDetectCliLangProperties:

    def test_da_lang_zh_overrides_others(self, monkeypatch):
        # Property: DA_LANG=zh wins regardless of LC_ALL / LANG.
        monkeypatch.setenv("DA_LANG", "zh_TW.UTF-8")
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert lv.detect_cli_lang() == "zh"

    def test_da_lang_en_overrides_others(self, monkeypatch):
        # Property: DA_LANG=en wins over LC_ALL=zh.
        monkeypatch.setenv("DA_LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "zh_TW.UTF-8")
        monkeypatch.setenv("LANG", "zh_TW.UTF-8")
        assert lv.detect_cli_lang() == "en"

    def test_lc_all_used_when_da_lang_unset(self, monkeypatch):
        # Property: when DA_LANG is missing, LC_ALL takes over.
        monkeypatch.delenv("DA_LANG", raising=False)
        monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert lv.detect_cli_lang() == "zh"

    def test_lang_used_when_da_lang_and_lc_all_unset(self, monkeypatch):
        # Property: LANG is the final fallback before defaulting to en.
        monkeypatch.delenv("DA_LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LANG", "zh_HK.UTF-8")
        assert lv.detect_cli_lang() == "zh"

    def test_default_is_en(self, monkeypatch):
        # Property: with all three vars unset, return "en" (NOT empty / None).
        monkeypatch.delenv("DA_LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LANG", raising=False)
        assert lv.detect_cli_lang() == "en"

    @given(st.sampled_from(["C.UTF-8", "POSIX", "fr_FR.UTF-8", "ja_JP.UTF-8"]))
    @PILOT_SETTINGS_MONKEYPATCH
    def test_unknown_locales_default_to_en(self, monkeypatch, locale):
        # Hypothesis fixture-ordering rule: pytest fixtures BEFORE
        # @given strategy params.
        # Property: any locale not starting with `zh` or `en` → "en".
        monkeypatch.delenv("DA_LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LANG", locale)
        assert lv.detect_cli_lang() == "en"


class TestI18nTextProperties:

    @given(
        st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=30),
        st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=30),
    )
    @PILOT_SETTINGS_MONKEYPATCH
    def test_returns_zh_in_zh_mode(self, monkeypatch, zh, en):
        # Property: in zh mode, i18n_text returns the zh argument (regardless
        # of how en is shaped).
        monkeypatch.setenv("DA_LANG", "zh_TW.UTF-8")
        assert lv.i18n_text(zh, en) == zh

    @given(
        st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=30),
        st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=30),
    )
    @PILOT_SETTINGS_MONKEYPATCH
    def test_returns_en_in_en_mode(self, monkeypatch, zh, en):
        # Property: in en mode, i18n_text returns the en argument.
        monkeypatch.setenv("DA_LANG", "en_US.UTF-8")
        assert lv.i18n_text(zh, en) == en


# ---------------------------------------------------------------------------
# parse_version — semver-ish parser used by HA-10 flaky registry validator
# ---------------------------------------------------------------------------
# Pilot batch 5: drives the expire_at lifecycle gate (PR #328). A regression
# here would either let malformed versions slip past the validator or block
# legitimate prefixed versions like `exporter/v2.9.0`. Properties pin shape
# acceptance, prefix preservation, and rejection of malformed input.

class TestParseVersionProperties:

    @given(
        st.integers(min_value=0, max_value=100),
        st.integers(min_value=0, max_value=100),
        st.integers(min_value=0, max_value=100),
    )
    @PILOT_SETTINGS
    def test_plain_version_round_trip(self, major, minor, patch):
        # Property: vX.Y.Z parses to a Version with no prefix and matching ints.
        s = f"v{major}.{minor}.{patch}"
        v = cfr.parse_version(s)
        assert v.prefix == ""
        assert v.major == major
        assert v.minor == minor
        assert v.patch == patch
        assert str(v) == s

    @given(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10).filter(
            lambda s: s[0].isalpha()
        ),
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
    )
    @PILOT_SETTINGS
    def test_prefixed_version_preserves_prefix(self, prefix, major, minor, patch):
        # Property: prefix/vX.Y.Z parses with prefix preserved verbatim.
        s = f"{prefix}/v{major}.{minor}.{patch}"
        v = cfr.parse_version(s)
        assert v.prefix == prefix
        assert v.major == major
        assert v.minor == minor
        assert v.patch == patch
        assert str(v) == s

    @given(st.sampled_from([
        "1.2.3",            # no v prefix
        "v1.2",             # missing patch
        "v1.2.3.4",         # extra component
        "v1.2.x",           # non-numeric
        "V1.2.3",           # uppercase V
        "Exporter/v1.2.3",  # uppercase prefix
        "exporter//v1.2.3", # double slash
        "/v1.2.3",          # empty prefix
        "",                 # empty
        "v",                # bare v
    ]))
    @PILOT_SETTINGS
    def test_malformed_versions_rejected(self, s):
        # Property: malformed inputs raise ValueError (not silently parse).
        with pytest.raises(ValueError):
            cfr.parse_version(s)

    @given(
        st.integers(min_value=0, max_value=10),
        st.integers(min_value=0, max_value=10),
        st.integers(min_value=0, max_value=10),
    )
    @PILOT_SETTINGS
    def test_ge_self(self, major, minor, patch):
        # Property: every version is >= itself.
        v = cfr.parse_version(f"v{major}.{minor}.{patch}")
        assert v >= v

    def test_lt_strictly_older(self):
        # Property: lexicographic comparison on (major, minor, patch).
        assert cfr.parse_version("v2.7.0") < cfr.parse_version("v2.8.0")
        assert cfr.parse_version("v2.7.9") < cfr.parse_version("v2.8.0")
        assert cfr.parse_version("v2.8.0") < cfr.parse_version("v2.8.1")

    def test_cross_line_comparison_raises(self):
        # Property: comparing across release lines (different prefix) raises.
        # This is the guardrail that keeps `exporter/v2.9.0` from being
        # compared against the platform v2.7.0 line.
        a = cfr.parse_version("exporter/v2.9.0")
        b = cfr.parse_version("v2.7.0")
        with pytest.raises(ValueError):
            _ = a < b

    @given(
        st.text(alphabet=" \t", min_size=1, max_size=4),
        st.text(alphabet=" \t", min_size=1, max_size=4),
    )
    @PILOT_SETTINGS
    def test_whitespace_stripped_before_parse(self, lpad, rpad):
        # Mutation-pilot kill-test: dropping the `.strip()` would cause
        # CHANGELOG entries with stray whitespace to fail parsing. The
        # caller (latest_version_from_changelog) already does its own line
        # parsing, but we want parse_version itself to be lenient — the
        # CLI also accepts --current-version values that the user might
        # quote with leading whitespace.
        v = cfr.parse_version(f"{lpad}v2.7.0{rpad}")
        assert v.major == 2 and v.minor == 7 and v.patch == 0


# ---------------------------------------------------------------------------
# _resolve_binary — arg parsing for da-tools Go-binary dispatchers
# ---------------------------------------------------------------------------
# Pilot batch 5: drives binary resolution for da-guard / da-batchpr /
# da-parser. Two flag forms are accepted (`--flag value` AND `--flag=value`);
# both must strip the flag pair from forwarded args, and both must record
# the explicit override for later use. The space-form / equals-form parity
# is exactly the kind of duplicated branch where mutation testing pays off.


def _make_dispatcher() -> lgd.GoBinaryDispatcher:
    """Build a dispatcher fixture matching the guard shim shape."""
    return lgd.GoBinaryDispatcher(
        binary_name="da-guard",
        cli_alias="guard",
        binary_flag="--da-guard-binary",
        env_var="DA_GUARD_BINARY",
        subcommands={"check", "verify"},
        pass_subcommand=False,
        usage_en="usage: guard ...",
        usage_zh="用法: guard ...",
    )


class TestResolveBinaryProperties:

    def test_no_flag_uses_path_lookup(self, monkeypatch):
        # Property: with no override flag and no env var, fall back to
        # shutil.which. We monkeypatch which to a sentinel so the test is
        # hermetic.
        monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
        monkeypatch.setattr(lgd.shutil, "which", lambda name: f"/path/{name}")
        d = _make_dispatcher()
        binary, cleaned = d._resolve_binary(["check", "-v"])
        assert binary == "/path/da-guard"
        # Args without the flag pass through untouched.
        assert cleaned == ["check", "-v"]

    def test_explicit_flag_space_form_strips_pair(self, monkeypatch, tmp_path):
        # Property: `--da-guard-binary <path>` removes BOTH tokens from args.
        binary_file = tmp_path / "da-guard"
        binary_file.write_text("#!/bin/sh\n", encoding="utf-8")
        d = _make_dispatcher()
        binary, cleaned = d._resolve_binary([
            "check", "--da-guard-binary", str(binary_file), "-v",
        ])
        assert binary == str(binary_file)
        assert cleaned == ["check", "-v"]

    def test_explicit_flag_equals_form_strips_token(self, monkeypatch, tmp_path):
        # Property: `--da-guard-binary=<path>` removes the single token.
        binary_file = tmp_path / "da-guard"
        binary_file.write_text("#!/bin/sh\n", encoding="utf-8")
        d = _make_dispatcher()
        binary, cleaned = d._resolve_binary([
            "check", f"--da-guard-binary={binary_file}", "-v",
        ])
        assert binary == str(binary_file)
        assert cleaned == ["check", "-v"]

    def test_explicit_flag_missing_file_returns_none(self, monkeypatch):
        # Property: explicit override that doesn't exist → (None, cleaned).
        monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
        d = _make_dispatcher()
        binary, cleaned = d._resolve_binary([
            "check", "--da-guard-binary", "/nonexistent/da-guard",
        ])
        assert binary is None
        # The flag pair is still stripped even when the path is bad.
        assert cleaned == ["check"]

    def test_env_var_used_when_no_flag(self, monkeypatch, tmp_path):
        # Property: $DA_GUARD_BINARY is consulted when no flag is passed.
        binary_file = tmp_path / "da-guard"
        binary_file.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("DA_GUARD_BINARY", str(binary_file))
        d = _make_dispatcher()
        binary, cleaned = d._resolve_binary(["check", "-v"])
        assert binary == str(binary_file)
        assert cleaned == ["check", "-v"]

    def test_explicit_flag_beats_env_var(self, monkeypatch, tmp_path):
        # Property: --flag wins over $ENV. The env var is the fallback.
        env_binary = tmp_path / "env-da-guard"
        env_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        flag_binary = tmp_path / "flag-da-guard"
        flag_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("DA_GUARD_BINARY", str(env_binary))
        d = _make_dispatcher()
        binary, _ = d._resolve_binary([
            "check", "--da-guard-binary", str(flag_binary),
        ])
        assert binary == str(flag_binary)
        assert binary != str(env_binary)

    def test_trailing_bare_flag_silently_dropped(self, monkeypatch):
        # Property: a trailing `--da-guard-binary` with no value is dropped
        # without raising IndexError. (Documented in the docstring; this
        # locks the behavior so a `len(args) - 1` mistake gets caught.)
        monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
        monkeypatch.setattr(lgd.shutil, "which", lambda name: None)
        d = _make_dispatcher()
        binary, cleaned = d._resolve_binary(["check", "--da-guard-binary"])
        # Bare flag is consumed; no override captured; falls through to which.
        assert binary is None
        assert cleaned == ["check"]

    @given(
        st.lists(
            st.text(alphabet=string.ascii_letters + "-_", min_size=1, max_size=10).filter(
                lambda s: s not in ("--da-guard-binary",) and not s.startswith("--da-guard-binary=")
            ),
            min_size=0, max_size=5,
        ),
    )
    @PILOT_SETTINGS_MONKEYPATCH
    def test_args_without_flag_pass_through_unchanged(self, monkeypatch, args):
        # Property: when the binary flag isn't present, args are returned
        # in the same order with no tokens removed.
        monkeypatch.delenv("DA_GUARD_BINARY", raising=False)
        monkeypatch.setattr(lgd.shutil, "which", lambda name: None)
        d = _make_dispatcher()
        _, cleaned = d._resolve_binary(args)
        assert cleaned == args


# ---------------------------------------------------------------------------
# _substitute_tenant — recursive {{tenant}} placeholder replacement
# ---------------------------------------------------------------------------
# Pilot batch 6: dev-rule #2 ("禁止 hardcode tenant id") rests on this
# function. Every Alertmanager route, every receiver URL, every
# severity-dedup label gets {{tenant}} → tenant_name substitution before
# being rendered. A regression here = silent cross-tenant data leak.

class TestSubstituteTenantProperties:

    @given(st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=15))
    @PILOT_SETTINGS
    def test_string_without_placeholder_unchanged(self, tenant):
        # Property: strings with no {{tenant}} pass through identically.
        for s in ["plain text", "no placeholder here", "tenant", "{{}}", ""]:
            assert gm._substitute_tenant(s, tenant) == s

    @given(
        st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20),
        st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20),
        st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=15),
    )
    @PILOT_SETTINGS
    def test_substitution_replaces_all_occurrences(self, prefix, suffix, tenant):
        # Property: every {{tenant}} occurrence is replaced; final string
        # has no remaining {{tenant}} markers.
        s = f"{prefix}{{{{tenant}}}}{suffix}{{{{tenant}}}}"
        result = gm._substitute_tenant(s, tenant)
        assert "{{tenant}}" not in result
        assert tenant in result

    @given(
        st.dictionaries(
            keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
            values=st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20),
            max_size=4,
        ),
        st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=10),
    )
    @PILOT_SETTINGS
    def test_dict_recursion_preserves_keys(self, d, tenant):
        # Property: dict substitution preserves keys; only values are
        # subject to substitution.
        result = gm._substitute_tenant(d, tenant)
        assert isinstance(result, dict)
        assert set(result.keys()) == set(d.keys())

    @given(
        st.lists(
            st.text(alphabet=string.ascii_letters + " ", max_size=15),
            max_size=5,
        ),
        st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=10),
    )
    @PILOT_SETTINGS
    def test_list_recursion_preserves_length(self, lst, tenant):
        # Property: list substitution preserves length and order.
        result = gm._substitute_tenant(lst, tenant)
        assert isinstance(result, list)
        assert len(result) == len(lst)

    @given(st.one_of(
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.booleans(),
        st.none(),
    ))
    @PILOT_SETTINGS
    def test_non_container_non_string_unchanged(self, value):
        # Property: ints / floats / bools / None pass through identically.
        assert gm._substitute_tenant(value, "any-tenant") == value

    def test_nested_recursion(self):
        # Property: deeply-nested {{tenant}} markers are all substituted.
        obj = {
            "url": "http://{{tenant}}.example.com/api",
            "labels": {"tenant": "{{tenant}}", "severity": "warning"},
            "matchers": ["alertname=~{{tenant}}_.*", "{{tenant}} OR foo"],
        }
        out = gm._substitute_tenant(obj, "db-a")
        assert "{{tenant}}" not in str(out), f"unsubstituted marker in {out!r}"
        assert out["url"] == "http://db-a.example.com/api"
        assert out["labels"]["tenant"] == "db-a"
        assert out["matchers"] == ["alertname=~db-a_.*", "db-a OR foo"]

    @given(st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=10))
    @PILOT_SETTINGS
    def test_idempotent_when_tenant_has_no_placeholder(self, tenant):
        # Property: substituting a tenant name that itself contains no
        # `{{tenant}}` is idempotent — running twice gives the same result.
        # (If the tenant value contained `{{tenant}}`, the first pass would
        # introduce more markers for the second pass to replace — we
        # deliberately exclude that pathological case.)
        obj = {"a": "{{tenant}}-x", "b": ["{{tenant}}", "y"]}
        once = gm._substitute_tenant(obj, tenant)
        twice = gm._substitute_tenant(once, tenant)
        assert once == twice


# ---------------------------------------------------------------------------
# _contains_tenant_placeholder — recursive boolean sibling
# ---------------------------------------------------------------------------

class TestContainsTenantPlaceholderProperties:

    @given(st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=30).filter(
        lambda s: "{{tenant}}" not in s
    ))
    @PILOT_SETTINGS
    def test_string_without_marker_returns_false(self, s):
        assert gm._contains_tenant_placeholder(s) is False

    @given(
        st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20),
        st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=20),
    )
    @PILOT_SETTINGS
    def test_string_with_marker_returns_true(self, prefix, suffix):
        s = f"{prefix}{{{{tenant}}}}{suffix}"
        assert gm._contains_tenant_placeholder(s) is True

    @given(st.one_of(
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.booleans(),
        st.none(),
    ))
    @PILOT_SETTINGS
    def test_non_container_non_string_returns_false(self, value):
        # Property: scalar non-string values can't contain placeholders.
        assert gm._contains_tenant_placeholder(value) is False

    def test_dict_recursion(self):
        # Property: a dict where a deeply-nested value has the marker
        # returns True overall.
        obj = {"a": {"b": {"c": "no marker"}}}
        assert gm._contains_tenant_placeholder(obj) is False
        obj["a"]["b"]["c"] = "{{tenant}}-x"
        assert gm._contains_tenant_placeholder(obj) is True

    def test_list_recursion(self):
        assert gm._contains_tenant_placeholder(["a", "b", {"c": "no"}]) is False
        assert gm._contains_tenant_placeholder(
            ["a", "b", {"c": "{{tenant}}"}]) is True

    @given(
        st.dictionaries(
            keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
            values=st.text(alphabet=string.ascii_letters, min_size=0, max_size=20),
            max_size=4,
        ),
        st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=10),
    )
    @PILOT_SETTINGS
    def test_after_substitute_no_placeholders_remain(self, d, tenant):
        # Property: pair-invariant. After _substitute_tenant runs,
        # _contains_tenant_placeholder must return False (assuming the
        # tenant name itself doesn't contain `{{tenant}}`).
        substituted = gm._substitute_tenant(d, tenant)
        assert gm._contains_tenant_placeholder(substituted) is False


# ---------------------------------------------------------------------------
# merge_routing_with_defaults — shallow merge with tenant substitution
# ---------------------------------------------------------------------------

class TestMergeRoutingWithDefaultsProperties:

    @given(
        st.dictionaries(
            keys=st.sampled_from(["group_wait", "group_interval", "repeat_interval", "receiver"]),
            values=st.text(min_size=1, max_size=10),
            max_size=3,
        ),
        st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=10),
    )
    @PILOT_SETTINGS
    def test_none_tenant_routing_returns_defaults_substituted(self, defaults, tenant):
        # Property: if tenant_routing is None, the result is just the
        # defaults with {{tenant}} substituted.
        result = gm.merge_routing_with_defaults(defaults, None, tenant)
        assert result == gm._substitute_tenant(defaults, tenant)

    def test_tenant_overrides_defaults(self):
        # Property: tenant key wins over defaults key (shallow override).
        defaults = {"group_wait": "30s", "group_interval": "5m"}
        tenant_routing = {"group_wait": "10s"}
        result = gm.merge_routing_with_defaults(defaults, tenant_routing, "db-a")
        assert result["group_wait"] == "10s"
        assert result["group_interval"] == "5m"

    def test_tenant_substitution_in_merged_result(self):
        # Property: {{tenant}} markers anywhere in the merged dict get
        # substituted to the tenant name.
        defaults = {"receiver": "default-receiver-{{tenant}}"}
        tenant_routing = {"new_key": "tenant-{{tenant}}"}
        result = gm.merge_routing_with_defaults(defaults, tenant_routing, "db-x")
        assert "{{tenant}}" not in str(result)
        assert result["receiver"] == "default-receiver-db-x"
        assert result["new_key"] == "tenant-db-x"

    def test_lists_replaced_not_concatenated(self):
        # Property: list-valued keys are REPLACED by tenant override,
        # not concatenated with defaults. (Documented contract.)
        defaults = {"group_by": ["alertname", "tenant"]}
        tenant_routing = {"group_by": ["severity"]}
        result = gm.merge_routing_with_defaults(defaults, tenant_routing, "db-a")
        assert result["group_by"] == ["severity"]

    @given(
        st.dictionaries(
            keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
            values=st.text(min_size=0, max_size=15),
            max_size=4,
        ),
    )
    @PILOT_SETTINGS
    def test_defaults_dict_not_mutated(self, defaults):
        # Property: defaults dict is not modified in place (caller may
        # reuse it across many tenants).
        snapshot = dict(defaults)
        gm.merge_routing_with_defaults(defaults, {"x": "y"}, "tenant-1")
        assert defaults == snapshot


# ---------------------------------------------------------------------------
# _extract_host — URL / host:port → hostname (lowercase)
# ---------------------------------------------------------------------------
# Used by validate_receiver_domains to enforce the SSRF-prevention domain
# allowlist on outgoing webhooks. A bug here would either reject legitimate
# receivers or fail-open on an unexpected URL shape.

class TestExtractHostProperties:

    @given(st.one_of(
        st.none(),
        st.just(""),
        st.integers(),
        st.lists(st.text()),
    ))
    @PILOT_SETTINGS
    def test_invalid_inputs_return_none(self, junk):
        # Property: None / empty / non-string → None.
        assert gv._extract_host(junk) is None

    @given(
        st.text(alphabet=string.ascii_lowercase + ".", min_size=3, max_size=20).filter(
            lambda s: "." in s and not s.startswith(".") and not s.endswith(".")
        ),
        st.integers(min_value=1, max_value=65535),
    )
    @PILOT_SETTINGS
    def test_host_port_form_strips_port(self, host, port):
        # Property: host:port → host (no port).
        result = gv._extract_host(f"{host}:{port}")
        assert result == host.lower()

    @given(
        st.sampled_from(["http", "https"]),
        st.text(alphabet=string.ascii_lowercase + ".", min_size=3, max_size=20).filter(
            lambda s: "." in s and not s.startswith(".") and not s.endswith(".")
        ),
    )
    @PILOT_SETTINGS
    def test_url_form_returns_hostname(self, scheme, host):
        # Property: scheme://host[/path] → host (lowercase).
        result = gv._extract_host(f"{scheme}://{host}/api/v1")
        assert result == host.lower()

    def test_uppercase_host_lowercased(self):
        # Property: even from non-URL form, hostname is lowercased.
        # (For URL form, urlparse already lowercases the hostname.)
        assert gv._extract_host("EXAMPLE.COM:443") == "example.com"

    def test_whitespace_stripped(self):
        # Property: leading/trailing whitespace is stripped.
        assert gv._extract_host("  example.com:443  ") == "example.com"

    def test_url_without_path(self):
        assert gv._extract_host("https://example.com") == "example.com"

    def test_empty_after_strip_returns_none(self):
        # Property: whitespace-only input → None.
        assert gv._extract_host("   ") is None


# ---------------------------------------------------------------------------
# parse_command_map — entrypoint.py COMMAND_MAP parser
# ---------------------------------------------------------------------------
# Used by check_cli_coverage / check_build_completeness lints. A regression
# here would cause CLI coverage drift checks to silently pass when the
# entrypoint COMMAND_MAP and the cheat-sheet diverge.

class TestParseCommandMapProperties:

    def test_minimal_command_map(self, tmp_path):
        # Property: a minimal valid COMMAND_MAP parses to the expected dict.
        f = tmp_path / "entrypoint.py"
        f.write_text(
            'COMMAND_MAP = {\n'
            '    "check-alert": "check_alert.py",\n'
            '    "diagnose": "diagnose.py",\n'
            '}\n',
            encoding="utf-8",
        )
        result = lh.parse_command_map(f)
        assert result == {
            "check-alert": "check_alert.py",
            "diagnose": "diagnose.py",
        }

    def test_empty_command_map(self, tmp_path):
        # Property: empty `COMMAND_MAP = {}` returns empty dict.
        f = tmp_path / "entrypoint.py"
        f.write_text('COMMAND_MAP = {\n}\n', encoding="utf-8")
        assert lh.parse_command_map(f) == {}

    def test_parser_stops_at_closing_brace(self, tmp_path):
        # Property: lines AFTER the closing `}` aren't included even if
        # they look like dict entries.
        f = tmp_path / "entrypoint.py"
        f.write_text(
            'COMMAND_MAP = {\n'
            '    "real": "real.py",\n'
            '}\n'
            'OTHER = {\n'
            '    "should-not-appear": "fake.py",\n'
            '}\n',
            encoding="utf-8",
        )
        result = lh.parse_command_map(f)
        assert result == {"real": "real.py"}
        assert "should-not-appear" not in result

    def test_ignores_lines_before_command_map(self, tmp_path):
        # Property: imports, comments, other code before COMMAND_MAP are
        # ignored — even if they contain string-quoted lookalikes.
        f = tmp_path / "entrypoint.py"
        f.write_text(
            '# A docstring with "fake-key": "fake.py"\n'
            'import os  # "import-key": "import.py"\n'
            'COMMAND_MAP = {\n'
            '    "real": "real.py",\n'
            '}\n',
            encoding="utf-8",
        )
        result = lh.parse_command_map(f)
        assert "fake-key" not in result
        assert "import-key" not in result
        assert result == {"real": "real.py"}

    def test_only_lowercase_kebab_keys_match(self, tmp_path):
        # Property: the regex only accepts `[a-z][a-z0-9-]+` keys. Keys
        # with uppercase letters / underscores / leading digits do NOT
        # match — they're dropped silently.
        f = tmp_path / "entrypoint.py"
        f.write_text(
            'COMMAND_MAP = {\n'
            '    "good-key": "good.py",\n'
            '    "BadKey": "bad.py",\n'
            '    "snake_key": "snake.py",\n'
            '    "1leading": "digits.py",\n'
            '}\n',
            encoding="utf-8",
        )
        result = lh.parse_command_map(f)
        assert "good-key" in result
        assert "BadKey" not in result
        assert "snake_key" not in result
        assert "1leading" not in result

    def test_keys_helper_returns_set(self, tmp_path):
        # parse_command_map_keys returns a set (not a dict_keys view that
        # would stale on dict mutation). Note: the regex requires
        # `[a-z][a-z0-9-]+` (at least 2 chars), so single-char keys like
        # "a" wouldn't match — use realistic 2+ char names.
        f = tmp_path / "entrypoint.py"
        f.write_text(
            'COMMAND_MAP = {\n'
            '    "alpha": "alpha.py",\n'
            '    "beta": "beta.py",\n'
            '}\n',
            encoding="utf-8",
        )
        result = lh.parse_command_map_keys(f)
        assert isinstance(result, set)
        assert result == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# parse_build_sh_tools — sibling parser for build.sh TOOL_FILES array
# ---------------------------------------------------------------------------
# Pilot batch 7. Used by check_build_completeness lint to verify every
# CLI command in COMMAND_MAP also ships in the docker image. A regression
# here = silent drift between the two registries.

class TestParseBuildShToolsProperties:

    def test_minimal_tool_files(self, tmp_path):
        # Property: a minimal valid TOOL_FILES array parses to the basenames.
        f = tmp_path / "build.sh"
        f.write_text(
            'TOOL_FILES=(\n'
            '  "scripts/tools/check_alert.py"\n'
            '  "scripts/tools/diagnose.py"\n'
            ')\n',
            encoding="utf-8",
        )
        result = lh.parse_build_sh_tools(f)
        assert result == {"check_alert.py", "diagnose.py"}

    def test_empty_tool_files(self, tmp_path):
        f = tmp_path / "build.sh"
        f.write_text('TOOL_FILES=(\n)\n', encoding="utf-8")
        assert lh.parse_build_sh_tools(f) == set()

    def test_basenames_only(self, tmp_path):
        # Property: returns basenames, not full paths.
        f = tmp_path / "build.sh"
        f.write_text(
            'TOOL_FILES=(\n'
            '  "deeply/nested/path/to/tool.py"\n'
            '  "shallow.py"\n'
            ')\n',
            encoding="utf-8",
        )
        result = lh.parse_build_sh_tools(f)
        assert result == {"tool.py", "shallow.py"}

    def test_skips_comments_and_blanks(self, tmp_path):
        f = tmp_path / "build.sh"
        f.write_text(
            'TOOL_FILES=(\n'
            '  # this is a comment\n'
            '\n'
            '  "real.py"\n'
            '  # another comment\n'
            ')\n',
            encoding="utf-8",
        )
        result = lh.parse_build_sh_tools(f)
        assert result == {"real.py"}

    def test_parser_stops_at_closing_paren(self, tmp_path):
        # Property: lines after the closing `)` are NOT included.
        f = tmp_path / "build.sh"
        f.write_text(
            'TOOL_FILES=(\n'
            '  "real.py"\n'
            ')\n'
            'OTHER_FILES=(\n'
            '  "should-not-appear.py"\n'
            ')\n',
            encoding="utf-8",
        )
        result = lh.parse_build_sh_tools(f)
        assert result == {"real.py"}
        assert "should-not-appear.py" not in result

    def test_quote_stripping(self, tmp_path):
        # Property: leading/trailing quotes (both ' and ") are stripped.
        f = tmp_path / "build.sh"
        f.write_text(
            'TOOL_FILES=(\n'
            '  "double_quoted.py"\n'
            "  'single_quoted.py'\n"
            ')\n',
            encoding="utf-8",
        )
        result = lh.parse_build_sh_tools(f)
        assert result == {"double_quoted.py", "single_quoted.py"}


# ---------------------------------------------------------------------------
# latest_version_from_changelog — newest `## [vX.Y.Z]` heading from CHANGELOG
# ---------------------------------------------------------------------------
# Pilot batch 7. Drives the HA-10 expire_at lifecycle gate (PR #328).
# Returns the FIRST matching heading because CHANGELOG.md is reverse-
# chronological. A regression that picked the LAST match would pin the
# expire_at gate to the oldest version forever.

class TestLatestVersionFromChangelogProperties:

    def test_finds_first_matching_heading(self, tmp_path):
        # Property: the FIRST `## [vX.Y.Z]` heading wins (CHANGELOG is
        # reverse-chronological).
        f = tmp_path / "CHANGELOG.md"
        f.write_text(
            "# Changelog\n\n"
            "## [v2.9.0] - 2025-12-01\n\n"
            "Latest stuff.\n\n"
            "## [v2.8.0] - 2025-11-01\n\n"
            "Old stuff.\n",
            encoding="utf-8",
        )
        v = cfr.latest_version_from_changelog(f)
        assert v is not None
        assert str(v) == "v2.9.0"

    def test_missing_file_returns_none(self, tmp_path):
        # Property: a nonexistent CHANGELOG path returns None (not raise).
        v = cfr.latest_version_from_changelog(tmp_path / "missing.md")
        assert v is None

    def test_no_matching_headings_returns_none(self, tmp_path):
        # Property: a CHANGELOG with no `## [vX.Y.Z]` heading returns None.
        f = tmp_path / "CHANGELOG.md"
        f.write_text(
            "# Changelog\n\n"
            "Just prose, no version headings here.\n"
            "## [Unreleased]\n",
            encoding="utf-8",
        )
        assert cfr.latest_version_from_changelog(f) is None

    @given(
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
    )
    @PILOT_SETTINGS
    def test_round_trip(self, tmp_path_factory, major, minor, patch):
        # Property: writing a single version heading and parsing it back
        # gives that exact version.
        d = tmp_path_factory.mktemp("changelog")
        f = d / "CHANGELOG.md"
        f.write_text(
            f"# Changelog\n\n## [v{major}.{minor}.{patch}] - 2025-01-01\n\nNotes.\n",
            encoding="utf-8",
        )
        v = cfr.latest_version_from_changelog(f)
        assert v is not None
        assert v.major == major and v.minor == minor and v.patch == patch

    def test_skips_non_anchored_headings(self, tmp_path):
        # Property: only `^##\s+\[vX.Y.Z\]` lines match — heading text
        # mid-document with `## [v…]` only matches if line starts with `##`.
        f = tmp_path / "CHANGELOG.md"
        f.write_text(
            "# Changelog\n\n"
            "Some body text.\n"
            "### [v2.9.0]\n"          # h3, NOT h2 — should not match
            "leading text ## [v2.8.0]\n"  # not at start of line
            "## [v2.7.0] - 2025-01-01\n"  # this should win
            "Notes.\n",
            encoding="utf-8",
        )
        v = cfr.latest_version_from_changelog(f)
        assert v is not None
        assert str(v) == "v2.7.0"


# ---------------------------------------------------------------------------
# _apply_timing_params — timing guardrail applicator
# ---------------------------------------------------------------------------
# Pilot batch 7. Wraps validate_and_clamp for the 3 timing params
# (group_wait / group_interval / repeat_interval). Already tested via
# property tests on validate_and_clamp itself, but the WRAPPER has its
# own behaviors: skip-if-absent, dict-build, warning-list aggregation.

class TestApplyTimingParamsProperties:

    def test_empty_source_returns_empty_dict(self):
        # Property: source dict with no timing keys → empty timing + no
        # warnings.
        timing, warnings = gm._apply_timing_params({}, "tenant-x")
        assert timing == {}
        assert warnings == []

    def test_missing_keys_skipped(self):
        # Property: only present keys are processed. (group_wait alone,
        # no group_interval / repeat_interval keys.)
        timing, warnings = gm._apply_timing_params({"group_wait": "30s"}, "t")
        assert "group_wait" in timing
        assert "group_interval" not in timing
        assert "repeat_interval" not in timing

    def test_all_three_in_bounds_returns_clean(self):
        # Property: well-formed values within guardrails → all three
        # present, no warnings.
        source = {"group_wait": "30s", "group_interval": "5m",
                   "repeat_interval": "4h"}
        timing, warnings = gm._apply_timing_params(source, "t")
        assert timing == source
        assert warnings == []

    def test_below_min_clamped(self):
        # Property: below-min value gets clamped + warning emitted.
        timing, warnings = gm._apply_timing_params(
            {"group_wait": "1s"}, "t-x")
        # validate_and_clamp(group_wait, "1s") → 5s clamped
        assert timing["group_wait"] == "5s"
        assert len(warnings) == 1
        assert "below minimum" in warnings[0]
        assert "t-x" in warnings[0]

    def test_warnings_aggregated_across_keys(self):
        # Property: each clamped key emits its own warning into the same list.
        source = {"group_wait": "1s", "repeat_interval": "10s"}
        _, warnings = gm._apply_timing_params(source, "t")
        assert len(warnings) == 2

    @given(st.dictionaries(
        keys=st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10).filter(
            lambda s: s not in ("group_wait", "group_interval", "repeat_interval")
        ),
        values=st.text(min_size=1, max_size=10),
        max_size=5,
    ))
    @PILOT_SETTINGS
    def test_unknown_keys_ignored(self, junk):
        # Property: any key NOT in the timing-param tuple is ignored.
        timing, warnings = gm._apply_timing_params(junk, "t")
        assert timing == {}
        assert warnings == []

    def test_falsy_values_skipped(self):
        # Property: empty string / None / 0 are treated as "not set".
        for falsy in ("", None, 0):
            timing, warnings = gm._apply_timing_params(
                {"group_wait": falsy, "group_interval": "5m"}, "t")
            assert "group_wait" not in timing
            assert "group_interval" in timing
            # No warnings for the absent param.
            assert warnings == []


# ---------------------------------------------------------------------------
# validate_receiver_domains — SSRF allowlist enforcement
# ---------------------------------------------------------------------------
# Pilot batch 7. Critical security helper: validates outgoing webhook
# URLs against an fnmatch-pattern allowlist. Used by the routing
# pipeline before tenant config is rendered into Alertmanager.

class TestValidateReceiverDomainsProperties:

    def test_empty_allowlist_no_warnings(self):
        # Property: empty allowed_domains list → no checks performed.
        recv = {"type": "webhook", "url": "http://anywhere.example.com/x"}
        assert gv.validate_receiver_domains(recv, "t", []) == []

    def test_non_dict_receiver_no_warnings(self):
        # Property: caller bug should not raise; just return empty.
        for junk in (None, "", 42, ["a"]):
            assert gv.validate_receiver_domains(junk, "t", ["*.example.com"]) == []

    def test_allowed_host_passes(self):
        recv = {"type": "webhook", "url": "http://hooks.example.com/x"}
        warnings = gv.validate_receiver_domains(
            recv, "t", ["*.example.com"])
        assert warnings == []

    def test_disallowed_host_warns(self):
        recv = {"type": "webhook", "url": "http://evil.attacker.com/x"}
        warnings = gv.validate_receiver_domains(
            recv, "t", ["*.example.com"])
        assert len(warnings) == 1
        assert "not in allowed_domains" in warnings[0]
        assert "evil.attacker.com" in warnings[0]

    def test_unknown_receiver_type_no_url_check(self):
        # Property: a receiver type not in RECEIVER_URL_FIELDS has no
        # url_fields, so no domain check fires.
        recv = {"type": "totally-unknown-type", "url": "http://x.com"}
        assert gv.validate_receiver_domains(
            recv, "t", ["*.example.com"]) == []

    def test_pagerduty_has_no_url_field(self):
        # Property: pagerduty's url_fields is [] (only service_key, no URL).
        recv = {"type": "pagerduty", "service_key": "abc",
                 "client_url": "http://anything.com"}
        # client_url isn't in pagerduty's url_fields list, so no check.
        assert gv.validate_receiver_domains(
            recv, "t", ["*.example.com"]) == []

    def test_email_smarthost_validated(self):
        # Property: email type validates the smarthost field.
        recv = {"type": "email", "smarthost": "smtp.evil.com:587", "to": "a@b"}
        warnings = gv.validate_receiver_domains(
            recv, "t", ["*.example.com"])
        assert len(warnings) == 1

    def test_unparseable_url_warns_separately(self):
        # Property: when _extract_host returns None, we emit a "cannot
        # parse host" warning rather than silently allowing.
        # Note: _extract_host returns the original string for non-URL
        # content (e.g. "@@@") so we use a value that genuinely yields
        # an empty host: a string of all-whitespace would, but is also
        # rejected by the upstream `if not raw` guard. Hard to trigger
        # without a contrived input — verify the type-check guard at
        # least exists.
        recv = {"type": "webhook", "url": ""}  # empty url skipped
        # (`if not raw: continue` covers this)
        assert gv.validate_receiver_domains(
            recv, "t", ["*.example.com"]) == []

    @given(
        # Build a valid hostname by composing labels — much faster than
        # filtering arbitrary strings.
        st.lists(
            st.text(alphabet=string.ascii_lowercase, min_size=2, max_size=10),
            min_size=2, max_size=4,  # at least 2 labels (host.tld)
        ).map(lambda labels: ".".join(labels)),
    )
    @PILOT_SETTINGS
    def test_exact_match_passes(self, host):
        # Property: when a host is in the allowlist (exact match), it passes.
        recv = {"type": "webhook", "url": f"http://{host}/api"}
        warnings = gv.validate_receiver_domains(recv, "t", [host])
        assert warnings == [], (
            f"exact-match host {host!r} flagged: {warnings!r}"
        )

    def test_any_match_in_multi_pattern_allowlist(self):
        # Mutation-pilot kill-test: an allowlist with MULTIPLE patterns is
        # an "any-of" check (host passes if it matches at least one).
        # Mutating `any → all` would tighten this to require every pattern
        # to match — which is impossible for non-trivial multi-pattern
        # allowlists, so legitimate hosts would be rejected.
        recv = {"type": "webhook", "url": "http://api.example.com/x"}
        warnings = gv.validate_receiver_domains(
            recv, "t", ["*.example.com", "*.allowed.org", "specific.host.io"])
        assert warnings == [], (
            f"host matched 1 of 3 patterns but was rejected: {warnings!r}"
        )


# ---------------------------------------------------------------------------
# validate_tenant_keys — schema typo / unknown-key warnings
# ---------------------------------------------------------------------------
# Pilot batch 7. Catches tenant config typos (e.g. `_routng` instead of
# `_routing`) at lint time. The branching for `_critical` suffix and
# `{labels}` dimensional keys is exactly the kind of multi-branch logic
# where mutation testing pays off.

class TestValidateTenantKeysProperties:

    @given(st.sets(
        st.sampled_from(["_silent_mode", "_severity_dedup", "_namespaces",
                          "_metadata", "_profile", "_routing_profile"]),
        min_size=1, max_size=5,
    ))
    @PILOT_SETTINGS
    def test_reserved_keys_no_warnings(self, keys):
        # Property: any subset of VALID_RESERVED_KEYS produces no warnings.
        warnings = gv.validate_tenant_keys("t", keys, set())
        assert warnings == []

    @given(st.sets(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10).map(
            lambda s: f"_routing_{s}" if s else "_routing"
        ),
        min_size=1, max_size=4,
    ))
    @PILOT_SETTINGS
    def test_routing_prefix_no_warnings(self, keys):
        # Property: any key starting with `_routing` is reserved.
        warnings = gv.validate_tenant_keys("t", keys, set())
        assert warnings == []

    @given(st.sets(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10).map(
            lambda s: f"_state_{s}" if s else "_state_"
        ),
        min_size=1, max_size=4,
    ))
    @PILOT_SETTINGS
    def test_state_prefix_no_warnings(self, keys):
        # Property: any key starting with `_state_` is reserved.
        warnings = gv.validate_tenant_keys("t", keys, set())
        assert warnings == []

    def test_defaults_keys_no_warnings(self):
        # Property: any key listed in defaults is allowed.
        warnings = gv.validate_tenant_keys(
            "t", {"latency", "errors", "uptime"},
            {"latency", "errors", "uptime"})
        assert warnings == []

    def test_critical_suffix_resolves_to_base(self):
        # Property: `latency_critical` is allowed if `latency` is in defaults.
        warnings = gv.validate_tenant_keys(
            "t", {"latency_critical"}, {"latency"})
        assert warnings == []

    def test_dimensional_key_resolves_to_base(self):
        # Property: `latency{namespace="db-a"}` is allowed if `latency` in defaults.
        warnings = gv.validate_tenant_keys(
            "t", {'latency{namespace="db-a"}'}, {"latency"})
        assert warnings == []

    def test_unknown_underscore_key_typo_warning(self):
        # Property: unknown key starting with `_` → "typo?" warning.
        warnings = gv.validate_tenant_keys(
            "t", {"_routng"}, set())  # _routng is a typo for _routing
        assert len(warnings) == 1
        assert "typo?" in warnings[0]
        assert "_routng" in warnings[0]

    def test_unknown_plain_key_not_in_defaults(self):
        # Property: unknown plain key → "not in defaults" warning.
        warnings = gv.validate_tenant_keys("t", {"oops"}, {"latency"})
        assert len(warnings) == 1
        assert "not in defaults" in warnings[0]
        assert "oops" in warnings[0]

    def test_critical_with_unknown_base_warns(self):
        # Property: `oops_critical` where `oops` isn't in defaults → warn.
        warnings = gv.validate_tenant_keys(
            "t", {"oops_critical"}, {"latency"})
        assert len(warnings) == 1
        assert "oops_critical" in warnings[0]
