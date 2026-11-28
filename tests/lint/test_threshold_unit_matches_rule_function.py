"""A threshold key's unit agrees with what its rule computes (#4032, #4116).

The JVM GC key was declared `seconds/5m` while its alert compared
`rate(jvm_gc_pause_seconds_sum[5m])` — seconds per SECOND. The two readings of
the same number differ by 300, and nothing compared the registry's unit with
the rule: check_threshold_unit_sanity.py types a unit into a dimension and
checks VALUES against it, but never asks whether the dimension is the rule's.

The contract, for every registry key that the observed map
(scripts/tools/ops/metric_observed_map.yaml)
ties to a recording rule (its `observed_series`):

  * the record takes `rate()` / `irate()` and is not a quotient
      -> the unit's dimension is `rate` (per second);
     except a TIME SHARE (#4116): when every counter under `rate()` counts
     seconds (`*_seconds_sum` / `*_seconds_total`), seconds per second is a
     dimensionless share of time, so the unit may also be a `percent` or a
     `ratio` — and then the scaling has to agree with it: `percent` takes a
     `100 *`, `ratio` and a per-second unit (`s/s`) do not. A percent unit on
     an unscaled share is off by 100, the same silent miss as the 300 above;
  * the record takes `increase()` / `delta()` and is not a quotient
      -> the unit's dimension is not `rate` (it is an amount per window).

A record with a division anywhere in it is left alone: a quotient of two such
terms (a hit ratio, an average latency) cancels the window, and telling that
from `rate(x[5m]) / 1000` takes a parser this test does not have.

What this does NOT see: a key the observed map does not list, a record that
only refers to another record (the function is one hop away), any record with
a `/` in it — a scaling (`rate(x[5m]) / 1000`), a label matcher or a comment
holding one included — and `deriv` / `idelta` / `changes`. The floor below
keeps "nothing matched" from passing, and the key this was written for is
named, so taking it out of the observed map is not a way round.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/tools"))
sys.path.insert(0, str(ROOT / "scripts/tools/lint"))

import _lib_consumer_read  # noqa: E402  (rule packs as Prometheus reads them)
import check_threshold_unit_sanity as unit_sanity  # noqa: E402

_PER_SECOND = re.compile(r"\bi?rate\s*\(")
_PER_WINDOW = re.compile(r"\b(?:increase|delta)\s*\(")
_RANGE = re.compile(r"\[[^\]]*\]")
_RATE_OF = re.compile(r"\bi?rate\s*\(\s*([A-Za-z_:][A-Za-z0-9_:]*)")
_SECONDS_COUNTER = re.compile(r"_seconds_(?:sum|total)$")
_TIMES_100 = re.compile(r"(?<![\w.])100\s*\*|\*\s*100(?![\w.])")


def rule_function(expr: str) -> str | None:
    """`rate` / `increase` when *expr* takes one and is not a quotient, else None."""
    if "/" in _RANGE.sub("", expr):
        return None
    if _PER_SECOND.search(expr):
        return "rate"
    if _PER_WINDOW.search(expr):
        return "increase"
    return None


def is_time_share(expr: str) -> bool:
    """Whether every counter *expr* takes a rate of counts seconds: the rate is
    then seconds per second, a share of time. False when no counter is found
    (`rate(` on something that is not a plain selector)."""
    counters = _RATE_OF.findall(expr)
    return bool(counters) and all(_SECONDS_COUNTER.search(c) for c in counters)


def problems(units: dict[str, str], observed: dict[str, str], records: dict[str, str]) -> list[str]:
    out = []
    for key, unit in sorted(units.items()):
        expr = records.get(observed.get(key, ""), "")
        function = rule_function(expr)
        dimension = unit_sanity.UNIT_DIMENSION.get(unit)
        if function == "rate" and is_time_share(expr) and dimension in ("percent", "ratio", "rate"):
            scaled = bool(_TIMES_100.search(_RANGE.sub("", expr)))
            if scaled != (dimension == "percent"):
                out.append(
                    f"{key}: unit {unit!r} ({dimension}) but {observed[key]} "
                    f"{'multiplies' if scaled else 'does not multiply'} its share of time by 100 — "
                    "a percent unit takes `100 *`, a ratio or per-second unit does not")
        elif function == "rate" and dimension != "rate":
            out.append(f"{key}: unit {unit!r} ({dimension}) but {observed[key]} takes rate() — "
                       "the threshold is compared with a per-second value")
        if function == "increase" and dimension == "rate":
            out.append(f"{key}: unit {unit!r} is per second but {observed[key]} takes "
                       "increase() — the threshold is compared with an amount per window")
    return out


def _shipped() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    registry = yaml.safe_load((ROOT / "rule-packs/threshold-registry.yaml").read_text(encoding="utf-8"))
    units = {key: entry["unit"] for key, entry in registry["keys"].items()}
    observed_map = yaml.safe_load(
        (ROOT / "scripts/tools/ops/metric_observed_map.yaml").read_text(encoding="utf-8"))
    observed = {key: entry["observed_series"] for key, entry in observed_map["keys"].items()
                if isinstance(entry, dict) and entry.get("observed_series")}
    records: dict[str, str] = {}
    packs = _lib_consumer_read.RULE_PACKS.files_under(ROOT)
    for rule_files in _lib_consumer_read.rule_files_in(packs, keep_refusals=True).values():
        for rule_file in rule_files:
            assert rule_file.problem is None, rule_file.problem
            for group in rule_file.groups:
                for rule in group.get("rules") or []:
                    if rule.get("record") and isinstance(rule.get("expr"), str):
                        records[rule["record"]] = rule["expr"]
    return units, observed, records


def test_every_key_on_a_rate_or_increase_rule_has_a_matching_unit() -> None:
    units, observed, records = _shipped()
    judged = [key for key in units if rule_function(records.get(observed.get(key, ""), ""))]
    assert len(judged) >= 10, (
        f"only {len(judged)} keys sit on a rate()/increase() record — 11 on 2026-11-20; if the "
        "packs changed, lower this floor in the same change so an empty match cannot pass")
    assert {"jvm_gc_time_percent", "jvm_gc_time_percent_critical"} <= set(judged), (
        "the keys #4032 / #4116 are about are no longer judged — their observed_series or record moved")
    assert is_time_share(records[observed["jvm_gc_time_percent"]]), (
        "tenant:jvm_gc_time:percent is no longer read as a share of time — the 100x check "
        "on it has stopped running")
    found = problems(units, observed, records)
    assert not found, "\n  ".join([""] + found)


@pytest.mark.parametrize("unit, expr, expected", [
    ("s/s", "sum by(tenant) (rate(x_seconds_sum[5m]))", 0),
    ("seconds", "sum by(tenant) (rate(x_seconds_sum[5m]))", 1),
    ("count", "sum by(tenant) (irate(x_total[5m]))", 1),
    ("count/5m", "sum by(tenant) (increase(x_total[5m]))", 0),
    ("req/s", "sum by(tenant) (increase(x_total[5m]))", 1),
    ("ops/s", "sum by(tenant) (delta(x[5m]))", 1),
    ("ratio", "rate(a_total[5m]) / rate(b_total[5m])", 0),
    ("ms", "1000 * rate(a_sum[5m]) / rate(a_count[5m])", 0),
    ("count", "max by(tenant) (x)", 0),
    ("% of time", "100 * max by(tenant) (rate(x_seconds_sum[5m]))", 0),
    ("% of time", "max by(tenant) (rate(x_seconds_sum[5m]) or rate(y_seconds_total[5m])) * 100", 0),
    ("% of time", "max by(tenant) (rate(x_seconds_sum[5m]))", 1),
    ("ratio", "max by(tenant) (rate(x_seconds_sum[5m]))", 0),
    ("ratio", "100 * max by(tenant) (rate(x_seconds_sum[5m]))", 1),
    ("s/s", "100 * max by(tenant) (rate(x_seconds_sum[5m]))", 1),
    ("%", "100 * sum by(tenant) (rate(x_total[5m]))", 1),
    ("%", "100 * max by(tenant) (rate(x_seconds_sum[5m]) or rate(y_total[5m]))", 1),
    ("seconds", "100 * sum by(tenant) (rate(x_seconds_sum[5m]))", 1),
    ("% of time", "1100 * max by(tenant) (rate(x_seconds_sum[5m]))", 1),
], ids=["rate-ok", "rate-as-duration", "irate-as-count", "increase-ok", "increase-as-rate",
        "delta-as-rate", "quotient", "quotient-latency", "plain-gauge",
        "share-percent-ok", "share-percent-ok-trailing", "share-percent-unscaled",
        "share-ratio-ok", "share-ratio-scaled", "share-per-second-scaled",
        "percent-of-a-plain-counter", "percent-of-a-mixed-rate", "share-as-duration",
        "share-percent-wrong-factor"])
def test_the_rule_can_fail(unit, expr, expected) -> None:
    assert len(problems({"k": unit}, {"k": "rec"}, {"rec": expr})) == expected


def test_a_division_inside_a_range_is_not_a_quotient() -> None:
    assert rule_function("rate(x_total[5m:1m])") == "rate"
    assert rule_function("rate (x_total[5m])") == "rate"
    assert rule_function("rate(a[5m]) / rate(b[5m])") is None
