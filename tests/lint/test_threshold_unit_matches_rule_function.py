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
     `ratio` — and then the scaling has to agree with it:
       - `percent`: the WHOLE expression is `100 * <one aggregation>` —
         `100 * max by(…) (…)`, nothing before, nothing after. A `100 *`
         somewhere else is not a scaling (`clamp_max(…, 100 * 1)`,
         `… > 100 * 0`), and `100 * 60 * …` is not a percent;
       - `ratio`, or a per-second unit (`s/s`): no numeric factor in front of
         or behind the expression.
     A percent unit on an unscaled share is off by 100, the same silent miss
     as the 300 above. A percent or ratio unit on the rate of anything that
     is not a seconds counter stays an error, as before.
     WHICH aggregation it is (sum / max / avg, by what) is not this
     contract's business: it changes what the number means, not its unit;
     the pack's promtool tests pin that.
  * a `rate()` whose counter this test cannot read off (`rate(` not followed
    by a plain `name[`/`name{` selector) is an error, not a skip: it could
    not tell whether the rate is a share of time;
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
_RATE_OF = re.compile(r"\bi?rate\s*\(\s*([A-Za-z_:][A-Za-z0-9_:]*)\s*[\[{]")
_SECONDS_COUNTER = re.compile(r"_seconds_(?:sum|total)$")
_AGGREGATION = re.compile(
    r"100\s*\*\s*(?:sum|max|min|avg|count|group|stddev|stdvar|topk|bottomk|quantile)\b\s*")
_GROUPING = re.compile(r"(?:by|without)\s*(?=\()")
_FACTOR = re.compile(r"^\(*\s*[0-9.][0-9.eE_]*\s*\*|\*\s*[0-9.][0-9.eE_]*\s*\)*$")


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
    then seconds per second, a share of time."""
    counters = _RATE_OF.findall(expr)
    return bool(counters) and all(_SECONDS_COUNTER.search(c) for c in counters)


def unread_rates(expr: str) -> int:
    """How many `rate(` / `irate(` in *expr* are not followed by a plain
    selector, so that the counter's name cannot be read off."""
    return len(_PER_SECOND.findall(expr)) - len(_RATE_OF.findall(expr))


def _after_parens(text: str) -> int | None:
    """*text* starts with `(`: the index just past its matching `)`, skipping
    quoted strings; None when it does not start with one or never closes."""
    if not text.startswith("("):
        return None
    depth, i, quote = 0, 0, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote != "`":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _skip_grouping(text: str) -> str | None:
    """*text* without a leading `by (…)` / `without (…)`; None if one opens
    and never closes."""
    m = _GROUPING.match(text)
    if not m:
        return text
    end = _after_parens(text[m.end():])
    return None if end is None else text[m.end() + end:].lstrip()


def is_percent_of_one_aggregation(expr: str) -> bool:
    """Whether the WHOLE of *expr* is `100 * <aggregation>(…)` (grouping
    clause before or after the arguments), and nothing else."""
    text = expr.strip()
    m = _AGGREGATION.match(text)
    if not m:
        return False
    rest = _skip_grouping(text[m.end():])
    end = _after_parens(rest) if rest is not None else None
    if end is None:
        return False
    rest = _skip_grouping(rest[end:].lstrip())
    return rest is not None and rest.strip() == ""


def has_outer_factor(expr: str) -> bool:
    """Whether *expr* opens or closes with a numeric factor (`100 * …`, `… * 60`)."""
    return bool(_FACTOR.search(expr.strip()))


def problems(units: dict[str, str], observed: dict[str, str], records: dict[str, str]) -> list[str]:
    out = []
    for key, unit in sorted(units.items()):
        expr = records.get(observed.get(key, ""), "")
        function = rule_function(expr)
        dimension = unit_sanity.UNIT_DIMENSION.get(unit)
        if function == "rate" and unread_rates(expr):
            out.append(f"{key}: {observed[key]} takes rate() of something this test cannot read "
                       "a counter name from — whether it is a share of time is unknown; write "
                       "the rate over a plain selector")
        elif function == "rate" and is_time_share(expr) and dimension in ("percent", "ratio", "rate"):
            if dimension == "percent" and not is_percent_of_one_aggregation(expr):
                out.append(f"{key}: unit {unit!r} (percent) but {observed[key]} is not "
                           "`100 * <one aggregation>` as a whole — a share of time is a percent "
                           "only when exactly that scales it")
            elif dimension != "percent" and has_outer_factor(expr):
                out.append(f"{key}: unit {unit!r} ({dimension}) but {observed[key]} multiplies its "
                           "share of time by a number — a ratio or per-second unit takes no factor")
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
    ("% of time", "max by(tenant) (rate(x_seconds_sum[5m]) or rate(y_seconds_total[5m])) * 100", 1),
    ("% of time", "100 * max by(tenant, c) (\n  sum without(a) (rate(x_seconds_sum{gc!~\".*(Cy)\"}[5m]))\n  or sum without(a) (rate(y_seconds_total[5m]))\n)\n", 0),
    ("% of time", "100 * max(rate(x_seconds_sum[5m])) by (tenant)", 0),
    ("% of time", "clamp_max(max by(tenant) (rate(x_seconds_sum[5m])), 100 * 1)", 1),
    ("% of time", "max by(tenant) (rate(x_seconds_sum[5m])) > 100 * 0", 1),
    ("% of time", "100 * 60 * max by(tenant) (rate(x_seconds_sum[5m]))", 1),
    ("% of time", "100 * max by(tenant) (rate(x_seconds_sum[5m])) > 5", 1),
    ("% of time", "100 * rate(x_seconds_sum[5m])", 1),
    ("% of time", "100 * max by(tenant) (rate({__name__=\"x_seconds_sum\"}[5m]))", 1),
    ("s/s", "sum by(tenant) (rate(label_replace(x_seconds_sum, \"a\", \"b\", \"\", \"\")[5m:]))", 1),
    ("ratio", "max by(tenant) (rate(x_seconds_sum[5m])) * 60", 1),
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
        "share-percent-ok", "share-percent-trailing-factor", "share-percent-ok-multiline",
        "share-percent-ok-grouping-after", "share-percent-100-inside-clamp",
        "share-percent-100-in-comparison", "share-percent-100-times-60",
        "share-percent-then-compared", "share-percent-no-aggregation",
        "rate-of-a-nameless-selector", "rate-of-a-subquery", "share-ratio-trailing-factor",
        "share-percent-unscaled",
        "share-ratio-ok", "share-ratio-scaled", "share-per-second-scaled",
        "percent-of-a-plain-counter", "percent-of-a-mixed-rate", "share-as-duration",
        "share-percent-wrong-factor"])
def test_the_rule_can_fail(unit, expr, expected) -> None:
    assert len(problems({"k": unit}, {"k": "rec"}, {"rec": expr})) == expected


def test_a_division_inside_a_range_is_not_a_quotient() -> None:
    assert rule_function("rate(x_total[5m:1m])") == "rate"
    assert rule_function("rate (x_total[5m])") == "rate"
    assert rule_function("rate(a[5m]) / rate(b[5m])") is None
