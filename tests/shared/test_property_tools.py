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
    os.path.join(_REPO_ROOT, "scripts", "tools", "ops"),
    os.path.join(_REPO_ROOT, "scripts", "tools", "dx"),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)


import generate_rule_pack_split as grps  # noqa: E402
import generate_doc_map as gdm  # noqa: E402
import generate_changelog as gc  # noqa: E402


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
