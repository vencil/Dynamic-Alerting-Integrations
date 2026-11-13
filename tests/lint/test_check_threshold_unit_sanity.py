"""Hermetic tests for check_threshold_unit_sanity.

Each test injects a synthetic registry + a tmp_path repo root, so no branch
depends on the real artifacts staying in a particular state. The final test is
the one exception: a smoke assertion that the gate is GREEN on the real repo,
which is what makes a future regression show up here rather than in production.

Two of these pin regressions the gate itself had while being written — the
kind that make a gate quietly wrong rather than loudly broken:

  * a blanket "threshold must be > 0" rule flagged the shipped-and-correct
    `kafka_under_replicated_partitions: 0` (`> 0` = alert if ANY);
  * feeding the recommendation file's PERCENTILES into tier ordering flagged
    `mysql_connection_usage_percent_critical` for sharing its base key's observed
    distribution — which is correct data, not an inversion.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest
import yaml

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "tools", "lint"))

import check_threshold_unit_sanity as gate  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


def _registry(**keys) -> dict:
    """Build a registry doc shaped like the real one (packs → key → spec)."""
    return {"version": 1, "packs": {"t": {k: v for k, v in keys.items()}}}


def _spec(value, unit, tier="defaults", **extra) -> dict:
    return {"value": value, "unit": unit, "tier": tier, **extra}


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _conf_d(root, body: dict):
    _write(root, "components/threshold-exporter/config/conf.d/_defaults.yaml",
           yaml.safe_dump(body, allow_unicode=True))


def _values(node):
    """(key, number) for every threshold literal in *node*, read the way
    `_collect` reads them: `_walk_literals`, then one `_judged` run."""
    return [(k, n) for _, k, n in gate._judged([(None, k, v) for k, v in gate._walk_literals(node)])]


def _errors(registry, root):
    return gate.run_check(registry=registry, root=str(root))["errors"]


# ------------------------------------------------------------ classification


def test_unclassified_unit_is_a_violation_not_a_skip(tmp_path):
    """An unknown unit would silently drop a key from coverage — fail loud."""
    errs = _errors(_registry(foo_bar=_spec(1, "furlongs/fortnight")), tmp_path)
    assert any("UNCLASSIFIED-UNIT" in e and "furlongs/fortnight" in e for e in errs)


def test_unclassified_unit_short_circuits_other_checks(tmp_path):
    """Classification is a precondition: don't report domain errors computed
    from a unit whose dimension we do not actually know."""
    _conf_d(tmp_path, {"defaults": {"pct": 999}})
    errs = _errors(
        _registry(pct=_spec(50, "%"), weird=_spec(1, "??")), tmp_path)
    assert all("UNCLASSIFIED-UNIT" in e for e in errs)


# -------------------------------------------------------------- unit drift


def test_unit_drift_between_asset_and_registry(tmp_path):
    _write(tmp_path, "docs/assets/recommendation-data.json", json.dumps(
        {"recommendations": {"pct": {"recommended": 50, "unit": "count"}}}))
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("UNIT-DRIFT" in e and "'count'" in e and "'%'" in e for e in errs)


def test_matching_unit_is_clean(tmp_path):
    _write(tmp_path, "docs/assets/recommendation-data.json", json.dumps(
        {"recommendations": {"pct": {"recommended": 50, "unit": "%"}}}))
    assert _errors(_registry(pct=_spec(80, "%")), tmp_path) == []


# ------------------------------------------------------------ value domains


@pytest.mark.parametrize("unit,value,needle", [
    ("%", 300, "NEVER cross"),
    ("% of max_connections", 120, "NEVER cross"),
    ("ratio", 95, "0-1 quantity"),
    ("bytes (4GB)", 4, "unit slip"),
    ("0=green,1=yellow,2=red", 7, "enumerates"),
])
def test_out_of_domain_values(tmp_path, unit, value, needle):
    _conf_d(tmp_path, {"defaults": {"k": value}})
    errs = _errors(_registry(k=_spec(1 if unit == "ratio" else 50, unit)), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and needle in e for e in errs), errs


@pytest.mark.parametrize("rel,expected", [
    ("conf.d/.hidden.yaml", 0),
    ("conf.d/.draft/x.yaml", 0),
    ("conf.d/visible.yaml", 1),
])
def test_hidden_carriers_are_not_linted(tmp_path, rel, expected):
    """#1630 — a `.`-prefixed file, or anything under a `.`-prefixed dir, is
    never merged by the exporter (`config_hierarchy.go` skip / `SkipDir`).

    WHY: an out-of-domain value there cannot reach any tenant, so a red gate
    for it sends an operator to fix a file that is not part of the config.
    Measured before the fix: 1 error for both hidden shapes. The visible
    arm is the control — same body, must still be 1 error.
    """
    _write(tmp_path, "components/threshold-exporter/config/" + rel,
           "defaults:\n  k: 300\n")
    errs = _errors(_registry(k=_spec(50, "%")), tmp_path)
    assert len([e for e in errs if "OUT-OF-DOMAIN" in e]) == expected, errs


def test_zero_is_legal_for_count_but_not_percent(tmp_path):
    """REGRESSION: `kafka_under_replicated_partitions: 0` (`> 0` = alert if ANY)
    is shipped and correct; a blanket `> 0` rule wrongly flagged it."""
    _conf_d(tmp_path, {"defaults": {"parts": 0, "pct": 0}})
    errs = _errors(
        _registry(parts=_spec(0, "count"), pct=_spec(80, "%")), tmp_path)
    assert not any("parts" in e for e in errs), errs
    assert any("pct" in e and "fires permanently" in e for e in errs), errs


def test_negative_threshold_is_rejected(tmp_path):
    _conf_d(tmp_path, {"defaults": {"k": -1}})
    errs = _errors(_registry(k=_spec(5, "count")), tmp_path)
    assert any("never crossable" in e for e in errs)


def test_unbounded_dimensions_are_not_second_guessed(tmp_path):
    """count / duration / rate have no static right magnitude — only sign."""
    _conf_d(tmp_path, {"defaults": {"c": 999999, "d": 86400, "r": 5000}})
    errs = _errors(_registry(
        c=_spec(80, "count"), d=_spec(30, "seconds"), r=_spec(1, "req/s")), tmp_path)
    assert errs == []


# ------------------------------------------------------------ value shapes


def test_nested_scheduled_form_is_reached(tmp_path):
    """#1217 blind spot: a line-oriented scan misses a value nested under the key.

    The scheduled form the exporter serves (#3206: a shape it cannot serve is
    a tree no conf.d tool reads): a tenant's `default` plus `overrides`."""
    _conf_d(tmp_path, {"defaults": {"pct": 10}, "tenants": {"t": {
        "pct": {"default": "50",
                "overrides": [{"window": "09:00-18:00", "value": "300"}]}}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("300" in e for e in errs), errs
    assert not any("= 50 " in e for e in errs), errs


def test_inline_severity_suffix_and_disable_sentinel(tmp_path):
    """`"300:critical"` is a threshold; `"disable"` is not a number at all."""
    _conf_d(tmp_path, {"tenants": {"t": {"pct": "300:critical", "other": "disable"}}})
    errs = _errors(
        _registry(pct=_spec(80, "%"), other=_spec(5, "count")), tmp_path)
    assert any("pct = 300" in e for e in errs), errs
    assert not any("other" in e for e in errs), errs


@pytest.mark.parametrize("value", ["-Inf", "Inf", "+Inf", "-inf:critical", "1e999"])
def test_non_finite_tenant_string_stays_out_of_domain(tmp_path, value):
    """#3140 (owner ruling D): the exporter refuses a non-finite threshold,
    but this lint still judges it — ±Inf is OUT-OF-DOMAIN (`--ci` rc 1),
    not skipped as "not a number"."""
    _conf_d(tmp_path, {"tenants": {"t": {"pct": value}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and "pct = " in e and "inf" in e for e in errs), errs


@pytest.mark.parametrize("value, shown", [("1_000", "1000"), ("-1_0", "-10"), ("1_0e2:critical", "1000")])
def test_an_underscored_number_is_judged_as_the_exporter_reads_it(tmp_path, value, shown):
    """#3140: Go's ParseFloat reads `1_000` as 1000, and so did this lint
    (Python's `float`) before #3140; a refusal of `_` here let an
    out-of-domain value through (`--ci` 1 → 0)."""
    _conf_d(tmp_path, {"tenants": {"t": {"pct": value}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and f"pct = {shown} " in e for e in errs), errs


@pytest.mark.parametrize("value, shown", [("0x1p10", "1024"), ("0X1P10:critical", "1024"),
                                          ("0x1p1024", "inf"), ("-0x1p1024", "-inf")])
def test_a_hex_float_is_judged_as_the_exporter_reads_it(tmp_path, value, shown):
    """#3267 (owner-approved, the one intended change): the exporter reads
    `0x1p10` (1024), and every value is now read by the exporter
    (`da-guard threshold-values`), so a hex float is judged — it was skipped
    while the lint read values with a Python copy (`--ci` 0 → 1)."""
    _conf_d(tmp_path, {"tenants": {"t": {"pct": value}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and f"pct = {shown} " in e for e in errs), errs


def test_an_in_domain_hex_float_is_clean(tmp_path):
    _conf_d(tmp_path, {"tenants": {"t": {"pct": "0x1p4"}}})
    assert _errors(_registry(pct=_spec(80, "%")), tmp_path) == []


def test_every_value_is_read_in_one_da_guard_run(tmp_path, monkeypatch):
    """#3267: one `threshold-values` run per check, every source batched."""
    import _lib_parse_tree
    calls = []
    real = _lib_parse_tree.threshold_values

    def spy(items, *a, **kw):
        items = list(items)
        calls.append(items)
        return real(items, *a, **kw)

    monkeypatch.setattr(_lib_parse_tree, "threshold_values", spy)
    _conf_d(tmp_path, {"tenants": {"t": {"pct": "300", "other": "1_000"}}})
    _write(tmp_path, "docs/assets/platform-data.json",
           '{"rulePacks": {"p": {"defaults": {"pct": {"value": 50, "unit": "%"}}}}}')
    _errors(_registry(pct=_spec(80, "%"), other=_spec(5, "count")), tmp_path)
    assert len(calls) == 1, calls
    assert ("pct", "300") in calls[0] and ("other", "1_000") in calls[0] and ("pct", "50.0") in calls[0]


# ------------------------------------------------- exit codes, #3267 rulings
#
# Owner rulings on #3267: once the exporter reads every value, these edge
# cases change exit code on purpose. Each row asserts the new `--ci` rc;
# the old one is in the comment.

_ENUM = "0=green,1=yellow,2=red"
_TIERS = dict(conn=_spec(80, "count"), conn_critical=_spec(120, "count", critical_of="conn"))


def _main_rc(tmp_path, monkeypatch, registry, argv=("--ci",)):
    monkeypatch.setattr(gate, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(gate.registry_lib, "load_registry", lambda path=None: registry)
    return emit_result(gate.main(list(argv)))


@pytest.mark.parametrize("files, registry, rc", [
    # hex out of domain (old 0)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    pct: '0x1p10'\n"}, _registry(pct=_spec(80, "%")), 1),
    # hex overflow, ±Inf (old 0)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    pct: '0x1p1024'\n"}, _registry(pct=_spec(80, "%")), 1),
    # a hex percentile in recommendation-data (old 0)
    ({"docs/assets/recommendation-data.json":
      '{"recommendations": {"pct": {"p50": "0x1p10", "unit": "%"}}}'}, _registry(pct=_spec(80, "%")), 1),
    # a hex value outside an enum (old 0)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    h: '0x1.8p1'\n"}, _registry(h=_spec(0, _ENUM)), 1),
    # a signed NaN: Go reads no number, so nothing is judged (old 1: Python read NaN)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    h: '+nan'\n"}, _registry(h=_spec(0, _ENUM)), 0),
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    h: '-NaN:critical'\n"}, _registry(h=_spec(0, _ENUM)), 0),
    # an unsigned NaN is still judged (unchanged, 1)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    h: 'NaN'\n"}, _registry(h=_spec(0, _ENUM)), 1),
    # an overflow in non-ASCII digits: Go reads none (old 1: Python read inf)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    pct: '\u0661e999'\n"}, _registry(pct=_spec(80, "%")), 0),
    # a `+nan` written last no longer hides a tier inversion (old 0)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t1:\n    conn: '100'\n    conn_critical: '50'\n"
      "  t2:\n    conn_critical: '+nan'\n"}, _registry(**_TIERS), 1),
    # `<key>_critical: "50:critical"`: the number written is judged (unchanged, 1)
    ({"components/threshold-exporter/config/conf.d/_defaults.yaml":
      "tenants:\n  t:\n    conn: '100'\n    conn_critical: '50:critical'\n"}, _registry(**_TIERS), 1),
    # an int no float holds: the crash it always was (unchanged, 2)
    ({"docs/assets/platform-data.json":
      '{"rulePacks": {"p": {"defaults": {"pct": {"value": 1' + "0" * 400 + ', "unit": "%"}}}}}'},
     _registry(pct=_spec(80, "%")), 2),
], ids=["hex", "hex-overflow", "hex-percentile", "hex-enum", "plus-nan", "minus-nan-suffix",
        "nan", "arabic-overflow", "nan-hid-inversion", "critical-suffix", "huge-int"])
def test_ci_exit_code(tmp_path, monkeypatch, files, registry, rc):
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    assert _main_rc(tmp_path, monkeypatch, registry) == rc


@pytest.mark.parametrize("value, rc", [("300", 2), ("50", 2)])
def test_no_da_guard_is_exit_2_even_without_conf_d(tmp_path, monkeypatch, capsys, value, rc):
    """#3267 ruling: every value is read through da-guard, so a tree with
    only docs JSON needs it too (old: 1 / 0 without da-guard)."""
    _write(tmp_path, "docs/assets/platform-data.json",
           '{"rulePacks": {"p": {"defaults": {"pct": {"value": %s, "unit": "%%"}}}}}' % value)
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    assert _main_rc(tmp_path, monkeypatch, _registry(pct=_spec(80, "%"))) == rc
    assert "da-guard threshold-values" in capsys.readouterr().err


def test_no_values_needs_no_da_guard(tmp_path, monkeypatch):
    """Nothing to read, no run: an empty tree stays 0 without da-guard."""
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    assert _main_rc(tmp_path, monkeypatch, _registry(pct=_spec(80, "%"))) == 0


_ANSWER = {"key": "pct", "text": "50.0", "number": 50, "non_finite": "", "severity": "warning", "refused": ""}


@pytest.mark.parametrize("answer", [
    [],                                                   # too few
    [_ANSWER, _ANSWER],                                   # too many
    [{**_ANSWER, "text": "60.0"}],                        # not the value asked
    [{**_ANSWER, "key": "other"}],                        # not the key asked
    [{**_ANSWER, "number": "50"}],                        # number not a number
    [{**_ANSWER, "number": True}],
    [{**_ANSWER, "non_finite": "inf"}],                   # not one of the spellings
    [{**_ANSWER, "extra": 1}],                            # a field beyond the six
    {"schema": "x"},                                      # not an array
], ids=["few", "many", "text", "key", "num-str", "num-bool", "non-finite", "extra", "object"])
def test_an_answer_that_is_not_one_reading_per_value_is_exit_2(tmp_path, monkeypatch, capsys, answer):
    """#3267: an answer read wrong anywhere is unusable whole — exit 2,
    never a partial judgement."""
    fake = tmp_path / "bin" / "da-guard"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '" + json.dumps(answer) + "'\n")
    fake.chmod(0o755)
    _write(tmp_path, "docs/assets/platform-data.json",
           '{"rulePacks": {"p": {"defaults": {"pct": {"value": 50, "unit": "%"}}}}}')
    monkeypatch.setenv("DA_GUARD_BINARY", str(fake))
    assert _main_rc(tmp_path, monkeypatch, _registry(pct=_spec(80, "%"))) == 2
    assert "wrote no answer this tool can read" in capsys.readouterr().err


def test_a_string_utf8_cannot_carry_is_not_a_value_and_hides_nothing(tmp_path, monkeypatch, capsys):
    """#3267 review: a lone surrogate (JSON `"\\ud800"`) is no text a
    reader reads as a number — skipped, as before — so it does not turn the
    run into exit 2 and hide a real violation beside it (old rc 1)."""
    _write(tmp_path, "docs/assets/recommendation-data.json",
           '{"recommendations": {"pct": {"p50": "\\ud800", "p95": "500", "unit": "%"}}}')
    assert _main_rc(tmp_path, monkeypatch, _registry(pct=_spec(80, "%"))) == 1
    err = capsys.readouterr().err
    assert "recommendation-data.json[p95]: pct = 500 " in err, err


def test_a_da_guard_older_than_this_tool_is_exit_2(tmp_path, monkeypatch, capsys):
    """#3267 ruling: a da-guard that does not know `threshold-values` (it
    exits 2 on it, `-h` included) is named as older, exit 2 (old: 0)."""
    fake = tmp_path / "bin" / "da-guard"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'da-guard: --config-dir is required' >&2\nexit 2\n")
    fake.chmod(0o755)
    _write(tmp_path, "docs/assets/platform-data.json",
           '{"rulePacks": {"p": {"defaults": {"pct": {"value": 50, "unit": "%"}}}}}')
    monkeypatch.setenv("DA_GUARD_BINARY", str(fake))
    assert _main_rc(tmp_path, monkeypatch, _registry(pct=_spec(80, "%"))) == 2
    assert "older than this tool" in capsys.readouterr().err


def test_non_finite_number_in_platform_data_and_a_rule_pack_stays_out_of_domain(tmp_path):
    """#3140 (owner ruling D): a JSON `-Infinity` in platform-data and a YAML
    `.inf` in a structured artifact are numbers already; they stay judged."""
    _write(tmp_path, "docs/assets/platform-data.json",
           '{"rulePacks": {"p": {"defaults": {"pct": {"value": -Infinity, "unit": "%"}}}}}')
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and "platform-data.json" in e and "-inf" in e for e in errs), errs


def test_a_values_file_helm_cannot_read_is_named_and_exit_2(tmp_path, capsys):
    """#3206: the values file is read the way Helm reads it (da-crdecode).
    Helm refuses `pct: .inf` (JSON has no infinity), so this gate cannot
    judge the file: exit 2, the file named — never a silent skip (PyYAML
    read the float and judged it OUT-OF-DOMAIN; a skip would pass it)."""
    _write(tmp_path, "helm/threshold-exporter/values.yaml", "thresholds:\n  pct: .inf\n")
    with pytest.raises(gate.ArtifactRefused) as exc:
        _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert [rel for rel, _ in exc.value.refused] == ["helm/threshold-exporter/values.yaml"]


def test_a_values_file_is_read_as_helm_reads_it(tmp_path):
    """#3206: control for the test above — a values file Helm reads (through
    da-crdecode) is still judged: an out-of-domain number in it is reported."""
    _write(tmp_path, "helm/threshold-exporter/values.yaml", "thresholds:\n  pct: -5\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and "values.yaml" in e and "pct = -5" in e for e in errs), errs


def test_dimensional_key_resolves_to_its_base(tmp_path):
    _conf_d(tmp_path, {"tenants": {"t": {'pct{version="v2"}': "300"}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("pct = 300" in e for e in errs), errs


# ---------------------------------------------------------- tier ordering


def test_tier_inversion_is_flagged(tmp_path):
    _conf_d(tmp_path, {"defaults": {"k": 90, "k_critical": 80}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert any("TIER-INVERSION" in e for e in errs), errs


def test_equal_tiers_are_flagged(tmp_path):
    _conf_d(tmp_path, {"defaults": {"k": 80, "k_critical": 80}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert any("TIER-INVERSION" in e for e in errs), errs


def test_dimensional_spellings_are_no_tier_pair(tmp_path):
    """#3658: `k{version="v9"}` and `k_critical{version="v3"}` select disjoint
    series — not one pair, so no TIER-INVERSION. The values are still
    domain-checked (test_dimensional_key_resolves_to_its_base)."""
    _conf_d(tmp_path, {"tenants": {"t": {'k{version="v9"}': "99",
                                         'k_critical{version="v3"}': "98"}}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert not any("TIER-INVERSION" in e for e in errs), errs


def test_a_dimensional_spelling_does_not_mask_the_plain_pair(tmp_path):
    """#3658: a later dimensional spelling of the base used to overwrite the
    plain value, hiding an inversion of the plain pair."""
    _conf_d(tmp_path, {"tenants": {"t": {"k": "99", "k_critical": "98",
                                         'k{pod="zzz"}': "50"}}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert any("TIER-INVERSION" in e for e in errs), errs


def test_a_dimensional_warning_is_held_to_the_plain_critical(tmp_path):
    """#3658 review: the critical tier is served without dimensions, so a
    plain `k_critical` is the critical tier of `k{version="v2"}` too."""
    _conf_d(tmp_path, {"tenants": {"t": {"k": "80", "k_critical": "95",
                                         'k{version="v2"}': "99"}}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert any("TIER-INVERSION" in e and "dimensional warning" in e for e in errs), errs


def test_a_dimensional_critical_value_is_no_warning(tmp_path):
    """`k{instance="x"}: "500:critical"` is a critical row itself, not a
    warning the plain critical must exceed."""
    _conf_d(tmp_path, {"tenants": {"t": {"k": "80", "k_critical": "95",
                                         'k{instance="x"}': "500:critical"}}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert not any("TIER-INVERSION" in e for e in errs), errs


def test_percentiles_do_not_feed_tier_ordering(tmp_path):
    """REGRESSION: p50/p95/p99 are OBSERVATIONS — a critical key legitimately
    shares its base key's distribution, and that is not an inversion."""
    _write(tmp_path, "docs/assets/recommendation-data.json", json.dumps(
        {"recommendations": {
            "k": {"p50": 45, "p95": 78, "recommended": 80, "unit": "count"},
            "k_critical": {"p50": 45, "p95": 78, "recommended": 150,
                           "unit": "count"}}}))
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert errs == [], errs


def test_tier_ordering_is_per_surface(tmp_path):
    """A tenant may raise the warning above the PLATFORM critical default; only
    an inversion WITHIN one artifact is a real dedup hazard."""
    _conf_d(tmp_path, {"tenants": {"t": {"k": 200}}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert errs == [], errs


# ------------------------------------------------------- per-surface parsing

# `_collect` has five surface branches. conf.d YAML and recommendation-data are
# exercised throughout the tests above; these three pin the rest. Without them a
# regression in one parser (a broken `tpl.get("yaml")`, a changed surface label,
# the markdown fence regex) would only surface if the REAL repo happened to hold
# a violating example that day — green-on-present-corpus, which is precisely
# where omission bugs hide.


def test_template_data_embedded_yaml_is_scanned(tmp_path):
    """Each template's `yaml` string is parsed and labelled per template id.

    This is the branch that caught the shipped `pg_connections` defect in
    `saas-backend` / `event-driven` / `finance-compliance`.
    """
    _write(tmp_path, "docs/assets/template-data.json", json.dumps(
        {"templates": [
            {"id": "demo", "yaml": 'pg_connections: "300"'},
            {"id": "fine", "yaml": 'pg_connections: "80"'},
        ]}))
    errs = _errors(_registry(pg_connections=_spec(80, "% of max_connections")), tmp_path)
    assert any("template-data.json[demo]" in e and "OUT-OF-DOMAIN" in e for e in errs), errs
    assert not any("[fine]" in e for e in errs), errs


def test_template_data_unreadable_yaml_is_named_not_fatal(tmp_path):
    """A malformed template must not take the whole gate down with it — nor
    pass unseen: its text is tenant conf.d, read as conf.d is read (da-guard
    parse-tree, #3416), and one parse-tree refuses is an UNREADABLE finding
    (its values were not checked). The gate still reaches the next one."""
    _write(tmp_path, "docs/assets/template-data.json", json.dumps(
        {"templates": [
            {"id": "broken", "yaml": "key: [unclosed"},
            {"id": "demo", "yaml": 'pg_connections: "300"'},
        ]}))
    errs = _errors(_registry(pg_connections=_spec(80, "% of max_connections")), tmp_path)
    assert any("template-data.json[demo]" in e and "OUT-OF-DOMAIN" in e for e in errs), errs
    assert any(e.startswith("UNREADABLE: docs/assets/template-data.json[broken]")
               and "syntax_error" in e for e in errs), errs


def test_template_data_is_read_as_conf_d_reads_it(tmp_path):
    """#3416: a repeated key is a file the exporter refuses whole; PyYAML
    took the last value (here an in-domain one) and judged that."""
    _write(tmp_path, "docs/assets/template-data.json", json.dumps(
        {"templates": [{"id": "dup", "yaml": 'pct: "300"\npct: "30"'}]}))
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("UNREADABLE" in e and "[dup]" in e and "duplicate_key" in e
               for e in errs), errs


def test_platform_data_value_and_declared_unit_are_scanned(tmp_path):
    """platform-data is the portal's RUNTIME rule-pack source — it carries both
    a value and a unit, so both invariants must reach it."""
    _write(tmp_path, "docs/assets/platform-data.json", json.dumps(
        {"rulePacks": {"pg": {"defaults": {
            "pg_connections": {"value": 300, "unit": "count"}}}}}))
    errs = _errors(_registry(pg_connections=_spec(80, "% of max_connections")), tmp_path)
    assert any("UNIT-DRIFT" in e and "platform-data.json" in e for e in errs), errs
    assert any("OUT-OF-DOMAIN" in e and "platform-data.json" in e for e in errs), errs


def test_markdown_fenced_yaml_is_scanned(tmp_path):
    """Doc examples are a real surface: #1217's residue lived mostly in them."""
    _write(tmp_path, "docs/getting-started/example.md",
           "設定範例：\n\n```yaml\ndefaults:\n  pct: 300\n```\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("example.md" in e and "OUT-OF-DOMAIN" in e for e in errs), errs


def test_markdown_units_are_scanned_one_documented_file_at_a_time(tmp_path):
    """#3416: a fence that documents two files is two units. Read whole (as
    PyYAML read the fence before), the second `tenants:` replaced the first,
    and the first tenant's value was never looked at."""
    _write(tmp_path, "docs/x.md",
           "```yaml\n# a.yaml\ntenants:\n  t1:\n    pct: \"300\"\n\n"
           "# b.yaml\ntenants:\n  t2:\n    pct: \"30\"\n```\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any(e.startswith("OUT-OF-DOMAIN: docs/x.md:2: pct = 300") for e in errs), errs


@pytest.mark.parametrize("info", ["{.confd}", "{.helm-values}", "{.k8s}"])
def test_an_attributed_fence_stays_in_scope(tmp_path, info):
    """#3416: declaring a fence must not take it out of this gate — every
    consumer's reading is scanned, not only conf.d's (a Helm values file as
    Helm reads it, da-crdecode)."""
    body = {"{.confd}": "defaults:\n  pct: 300\n",
            "{.helm-values}": "thresholdConfig:\n  defaults:\n    pct: 300\n",
            "{.k8s}": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n"
                      "data:\n  pct: \"300\"\n"}[info]
    _write(tmp_path, "docs/x.md", f"```yaml {info}\n{body}```\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any(e.startswith("OUT-OF-DOMAIN: docs/x.md:2: pct = 300") for e in errs), errs


def test_an_illustrative_fence_is_out_of_scope(tmp_path):
    """The deliberate narrowing: an `.illustrative` fence is judged by no
    gate — and named, with its reason, by every report, this one included."""
    _write(tmp_path, "docs/x.md",
           '```yaml {.illustrative reason="r"}\ndefaults:\n  pct: 300\n```\n')
    res = gate.run_check(registry=_registry(pct=_spec(80, "%")), root=str(tmp_path))
    assert res["errors"] == []
    assert res["exempted"] == [("docs/x.md:2", "r")]


@pytest.mark.parametrize("as_json", [False, True])
def test_main_names_every_illustrative_unit(tmp_path, monkeypatch, capsys, as_json):
    """#3416 review (CodeRabbit): "every report prints the exemptions" holds
    for this gate too — on stderr, so `--json` stays one document on stdout
    (which carries them as well)."""
    _write(tmp_path, "docs/x.md",
           '```yaml {.illustrative reason="shown on purpose"}\na: 1\n```\n')
    monkeypatch.setattr(gate, "PROJECT_ROOT", str(tmp_path))
    assert emit_result(gate.main(["--ci", "--json"] if as_json else ["--ci"])) == gate.EXIT_OK
    out = capsys.readouterr()
    assert "Units exempted (.illustrative): 1" in out.err
    assert "docs/x.md:2 — shown on purpose" in out.err
    if as_json:
        assert json.loads(out.out)["exempted"] == [
            {"unit": "docs/x.md:2", "reason": "shown on purpose"}]


def test_markdown_non_yaml_and_fragment_blocks_are_tolerated(tmp_path):
    """Docs legitimately contain YAML fragments that are not standalone
    documents, and fences in other languages. Neither may crash the gate nor
    stop the scan from reaching a later, valid block."""
    _write(tmp_path, "docs/getting-started/example.md",
           "```bash\nkubectl apply -f x.yaml\n```\n\n"
           "```yaml\n  - broken: [unclosed\n```\n\n"
           "```yaml\ndefaults:\n  pct: 300\n```\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("example.md" in e for e in errs), errs


# -------------------------------------------------------------- real repo


def test_registry_metadata_numbers_are_not_read_as_thresholds(monkeypatch):
    """A NUMBER stored *about* a key is not a threshold FOR that key.

    The walker's rule — "once inside a known registry key, every numeric leaf
    beneath it is a threshold literal" (#1217's nested scheduled form) — held
    only while every metadata field was non-numeric. #1176 added
    `value_counterexample: {issue: 1176, …}` and the gate promptly reported
    `oracle_pga_allocated_bytes = 1176 … < 1 MiB, looks like a unit slip`: a
    real false positive produced by a real contract change.

    Pinned with a bytes key on purpose — bytes is the one dimension with a
    lower bound, so a stray small integer is guaranteed to trip it if the
    exclusion regresses.
    """
    # `_REGISTRY_KEYS` is populated by run_check(); seed it so the walker
    # recognises the key (monkeypatch restores it, keeping this hermetic).
    monkeypatch.setattr(gate, "_REGISTRY_KEYS", {"oracle_pga_allocated_bytes"})
    entry = {
        "pack": "p", "tier": "defaults", "unit": "bytes (4GB)",
        "desc": "d", "metric_class": "capacity",
        "value": 4294967296,
        "value_counterexample": {
            "issue": 1176, "direction": "over_fire", "observed": "x",
        },
    }
    found = dict(_values({"oracle_pga_allocated_bytes": entry}))
    assert found == {"oracle_pga_allocated_bytes": 4294967296}, found

    # ...and the exclusion is DERIVED from the registry's own field list, so a
    # future metadata field is covered without editing this gate.
    assert "value" not in gate._REGISTRY_METADATA_FIELDS
    assert "value_counterexample" in gate._REGISTRY_METADATA_FIELDS


def test_scheduled_nested_thresholds_still_collected(monkeypatch):
    """Counter-case for the exclusion above: the #1217 nested form carries REAL
    threshold literals under names that are not registry metadata, and must
    keep being collected. An over-broad skip would silently un-cover it."""
    monkeypatch.setattr(gate, "_REGISTRY_KEYS", {"mysql_threads_running"})
    nested = {"mysql_threads_running": {"default": {"during_business_hours": "30"}}}
    assert dict(_values(nested)) == {"mysql_threads_running": 30}


def test_real_repo_is_clean():
    """Smoke: the shipped artifacts satisfy the gate. If this goes red, an
    asset drifted from the registry — read the message, don't relax the gate."""
    assert gate.run_check()["errors"] == []


#: The registry-key values written in docs/**/*.md that this gate does not
#: look at: each sits in a unit no reader judges — an `.illustrative`
#: fence, a unit its reader refuses, a unit with an inventory error (#3416).
#: Listed one by one, so the narrowing is deliberate: a new entry here is a
#: value a doc change took out of scope, and is decided in review.
DROPPED_SINCE_INVENTORY = {
    # The flat-shape ❌ counter-example (`.illustrative`): wrong in shape,
    # not in value. The one entry the scan before the inventory looked at
    # (PyYAML over every fence, the old ignore marker not consulted).
    ("docs/internal/testing-playbook.md", "mysql_connection_usage_percent", 70.0),
    # ADR-036's two-document example ({.confd .expect-refused=
    # multiple_documents}) left this list with #3778: it is written in
    # `mysql_connections`, which is no longer a registry key (retired), so
    # the scan no longer counts its values as registry-key values.
}


def test_values_out_of_scope_are_the_listed_ones():
    """Every registry-key value in a docs unit the inventory does not judge,
    read leniently (PyYAML, every document, in this test only), is in
    DROPPED_SINCE_INVENTORY — and every listed one is still such a value."""
    import _lib_doc_yaml_inventory as doc_inventory
    registry = gate.registry_lib.load_registry()
    gate.run_check(registry=registry, root=_ROOT)  # seeds _REGISTRY_KEYS
    inv = doc_inventory.build(_ROOT)
    judged = {id(u) for u in inv.judged()}
    dropped = set()
    for u in inv.units:
        if id(u) in judged:
            continue
        try:
            docs = list(yaml.safe_load_all(u.text))
        except yaml.YAMLError:
            continue
        for doc in docs:
            for key, val in _values(doc):
                dropped.add((u.path, key, val))
    assert dropped == DROPPED_SINCE_INVENTORY, (
        f"newly out of scope: {sorted(dropped - DROPPED_SINCE_INVENTORY)}; "
        f"listed but back in scope (drop the row): {sorted(DROPPED_SINCE_INVENTORY - dropped)}")


def test_main_names_a_values_file_helm_cannot_read_and_exits_2(tmp_path, monkeypatch, capsys):
    """#3206: the same refusal through `main` — exit 2 (an input the gate
    could not judge, `_lib_exitcodes`' malformed input), the file named."""
    _write(tmp_path, "helm/threshold-exporter/values.yaml", "thresholds:\n  pct: .inf\n")
    monkeypatch.setattr(gate, "PROJECT_ROOT", str(tmp_path))
    assert gate.main(["--ci"]) == gate.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "helm/threshold-exporter/values.yaml" in err and "refuses it" in err


def test_main_names_a_renderer_that_cannot_be_had_and_exits_2(tmp_path, monkeypatch, capsys):
    """#3814: the docs' examples are found by the site's renderer. When it
    cannot be had (a package missing), main names it and exits 2 — its own
    branch, not the generic "crashed" one."""
    _write(tmp_path, "docs/x.md", "```yaml\na: 1\n```\n")

    def no_renderer(root):
        raise gate.doc_inventory.RendererError("simulated: no pygments")

    monkeypatch.setattr(gate.doc_inventory, "Renderer", no_renderer)
    assert _main_rc(tmp_path, monkeypatch, {}) == gate.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "site's renderer, which cannot be had: simulated: no pygments" in err, err
    assert "crashed" not in err, err
