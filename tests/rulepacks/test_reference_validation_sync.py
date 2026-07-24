"""reference-validation candidate rules must stay aligned with the shipped packs.

WHY this exists (TRK-339 WS4a-1, ADR-030 harness fidelity):
  The catch-rate harness measures the DIRECT-PREDICATE candidates under
  tests/rulepacks/reference-validation/candidate-*.rules.yaml, not the shipped
  rule packs. run#1 shipped with silent fidelity drift — candidate `for:` values
  invented (5m/6m where shipped says 30s/60s/3m), no namespace selectors while
  the shipped pack scopes everything to namespace=~"db-.+", and predicates that
  the shipped pack has since changed (#1184 sort-overflow rate-ratio, #1188
  lock-wait alert). Every such drift silently distorts the catch-rate numbers
  the epic's threshold decisions (#1175/#1176/#1196) will be based on.

Contract (exit-lock shape, mirroring the #1195 KNOWN_UNWIRED pattern):
  For every candidate alert, `for:` / severity / raw-metric selectors must equal
  the shipped values, OR the divergence must be declared in
  reference-validation/divergence-ledger.yaml. Ledger entries are themselves
  asserted against reality — an entry whose divergence no longer exists (alert
  added to candidate, shipped evidence text gone, matcher no longer present)
  FAILS the gate and must be removed. Neither side can drift silently.

Deliberately NOT asserted here: threshold VALUES (run-policy dependent — see
README §threshold 注入政策) and full expression equivalence (the direct-predicate
form is a declared structural divergence; pipeline correctness is covered by the
promtool fixtures + vmalert parity gate A against the real rules).
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

import pytest
import yaml

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REF_DIR = os.path.join(_REPO, "tests", "rulepacks", "reference-validation")
_LEDGER = os.path.join(_REF_DIR, "divergence-ledger.yaml")

PACKS = {
    "oracle": (
        os.path.join(_REPO, "rule-packs", "rule-pack-oracle.yaml"),
        os.path.join(_REF_DIR, "candidate-oracle.rules.yaml"),
    ),
    "db2": (
        os.path.join(_REPO, "rule-packs", "rule-pack-db2.yaml"),
        os.path.join(_REF_DIR, "candidate-db2.rules.yaml"),
    ),
    "kubernetes": (
        os.path.join(_REPO, "rule-packs", "rule-pack-kubernetes.yaml"),
        os.path.join(_REF_DIR, "candidate-k8s.rules.yaml"),
    ),
}

# PromQL function/keyword tokens that are NOT metric names.
_NON_METRIC = {
    "rate", "irate", "increase", "delta", "idelta", "abs", "absent",
    "ignoring", "on", "by", "without", "and", "or", "unless", "bool",
    "group_left", "group_right", "sum", "max", "min", "avg", "count",
    "topk", "bottomk", "quantile", "vector", "scalar", "label_replace",
    "label_join", "max_over_time", "min_over_time", "avg_over_time",
    "sum_over_time", "count_over_time", "last_over_time", "changes",
    "clamp_max", "clamp_min", "time", "histogram_quantile", "predict_linear",
}

_DUR = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _dur_s(v):
    s = str(v).strip()
    m = re.fullmatch(r"(\d+)(ms|s|m|h|d)", s)
    assert m, f"unparseable duration {v!r}"
    return int(m.group(1)) * _DUR[m.group(2)]


def _norm_matchers(braces_text):
    """'{a = "b", c!=""}' -> frozenset({'a="b"', 'c!=""'}) (values contain no commas here)."""
    inner = braces_text.strip()[1:-1].strip()
    if not inner:
        return frozenset()
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    return frozenset(re.sub(r'\s*(=~|!~|!=|=)\s*', r"\1", p) for p in parts)


def _metric_occurrences(metric, exprs):
    """All matcher-sets attached to bare-name occurrences of `metric` in exprs.

    Lookarounds exclude word chars AND ':' so `tenant:db2_x:max` record names never
    count as occurrences of `db2_x` (the substring-match trap — see memory of #1189).
    """
    pat = re.compile(r"(?<![\w:])" + re.escape(metric) + r"(?![\w:])\s*(\{[^}]*\})?")
    out = []
    for expr in exprs:
        for m in pat.finditer(expr):
            out.append(_norm_matchers(m.group(1)) if m.group(1) else frozenset())
    return out


def _candidate_metrics(expr):
    """Raw metric names referenced by a candidate expr (brace/range bodies stripped)."""
    stripped = re.sub(r"\{[^}]*\}", "", expr)
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stripped))
    return {n for n in names if n not in _NON_METRIC and not n.isdigit()}


def _walk_alerts(doc):
    out = {}
    for g in doc.get("groups", []):
        for r in g.get("rules", []):
            if "alert" in r:
                assert r["alert"] not in out, f"duplicate alert {r['alert']}"
                out[r["alert"]] = r
    return out


@lru_cache(maxsize=None)
def _load(pack):
    shipped_path, cand_path = PACKS[pack]
    with open(shipped_path, encoding="utf-8") as f:
        shipped_raw = f.read()
    with open(cand_path, encoding="utf-8") as f:
        cand_raw = f.read()
    shipped_doc = yaml.safe_load(shipped_raw)
    cand_doc = yaml.safe_load(cand_raw)
    shipped_exprs = [
        str(r["expr"])
        for g in shipped_doc.get("groups", [])
        for r in g.get("rules", [])
    ]
    return {
        "shipped_raw": shipped_raw,
        "shipped_doc": shipped_doc,
        "shipped_alerts": _walk_alerts(shipped_doc),
        "shipped_exprs": shipped_exprs,
        "cand_raw": cand_raw,
        "cand_doc": cand_doc,
        "cand_alerts": _walk_alerts(cand_doc),
    }


@lru_cache(maxsize=None)
def _ledger():
    with open(_LEDGER, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ledger_absent(pack):
    return {e["alert"]: e for e in (_ledger()["absent_from_candidate"].get(pack) or [])}


def _ledger_alert_div(pack):
    out = {}
    for e in (_ledger().get("alert_divergence", {}) or {}).get(pack) or []:
        out[(e["alert"], e["field"])] = e
    return out


def _ledger_inlined(pack):
    out = {}
    for e in (_ledger().get("inlined_matchers", {}) or {}).get(pack) or []:
        key = (e["alert"], e["metric"], re.sub(r'\s*(=~|!~|!=|=)\s*', r"\1", e["matcher"]))
        out[key] = e
    return out


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_candidate_alerts_exist_in_shipped(pack):
    d = _load(pack)
    unknown = set(d["cand_alerts"]) - set(d["shipped_alerts"])
    assert not unknown, (
        f"[{pack}] candidate alerts with no shipped counterpart (rename drift?): {sorted(unknown)}"
    )


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_shipped_alert_coverage_or_ledger(pack):
    d = _load(pack)
    absent = _ledger_absent(pack)
    # forward: every shipped alert is mirrored or declared absent
    missing = set(d["shipped_alerts"]) - set(d["cand_alerts"]) - set(absent)
    assert not missing, (
        f"[{pack}] shipped alerts neither mirrored in candidate nor declared in "
        f"divergence-ledger.yaml absent_from_candidate: {sorted(missing)}"
    )
    # exit-lock: declared-absent entries must still be real
    for name in absent:
        assert name in d["shipped_alerts"], (
            f"[{pack}] ledger absent_from_candidate entry {name!r} does not exist in the "
            "shipped pack any more — remove the stale entry"
        )
        assert name not in d["cand_alerts"], (
            f"[{pack}] ledger absent_from_candidate entry {name!r} IS now in the candidate "
            "file — remove the stale entry"
        )


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_for_and_severity_aligned(pack):
    d = _load(pack)
    div = _ledger_alert_div(pack)
    for name, cand in d["cand_alerts"].items():
        shipped = d["shipped_alerts"].get(name)
        if shipped is None:  # covered by test_candidate_alerts_exist_in_shipped
            continue
        for field, cand_val, shipped_val in (
            ("for", _dur_s(cand.get("for", "0s")), _dur_s(shipped.get("for", "0s"))),
            ("severity", cand.get("labels", {}).get("severity"),
             shipped.get("labels", {}).get("severity")),
        ):
            entry = div.get((name, field))
            if entry is not None:
                # exit-lock: the declared divergence must still describe reality
                assert cand_val != shipped_val, (
                    f"[{pack}] {name} {field}: ledger declares a divergence but candidate "
                    "and shipped now AGREE — remove the stale ledger entry"
                )
                continue
            assert cand_val == shipped_val, (
                f"[{pack}] {name} {field} drift: candidate={cand_val!r} shipped={shipped_val!r} "
                "— align the candidate or declare it in divergence-ledger.yaml"
            )


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_selector_alignment(pack):
    d = _load(pack)
    inlined = _ledger_inlined(pack)
    used_inline_keys = set()
    for name, cand in d["cand_alerts"].items():
        if name not in d["shipped_alerts"]:
            continue
        cexpr = str(cand["expr"])
        for metric in sorted(_candidate_metrics(cexpr)):
            shipped_sets = _metric_occurrences(metric, d["shipped_exprs"])
            assert shipped_sets, (
                f"[{pack}] {name}: candidate references metric {metric!r} which never "
                "appears in the shipped pack — candidate is not measuring shipped logic"
            )
            cand_sets = _metric_occurrences(metric, [cexpr])
            invariant = frozenset.intersection(*shipped_sets)
            union = frozenset().union(*shipped_sets)
            for cset in cand_sets:
                missing = invariant - cset
                assert not missing, (
                    f"[{pack}] {name}/{metric}: candidate selector is missing matchers the "
                    f"shipped pack ALWAYS applies to this metric: {sorted(missing)} "
                    f"(candidate has {sorted(cset)})"
                )
                for extra in sorted(cset - union):
                    key = (name, metric, extra)
                    assert key in inlined, (
                        f"[{pack}] {name}/{metric}: candidate matcher {extra!r} never appears "
                        "on this metric in the shipped pack and is not declared in "
                        "divergence-ledger.yaml inlined_matchers"
                    )
                    used_inline_keys.add(key)
    # exit-lock: every inlined_matchers entry must still be live on BOTH sides
    for key, e in inlined.items():
        name, metric, matcher = key
        assert key in used_inline_keys, (
            f"[{pack}] ledger inlined_matchers entry {matcher!r} on {name}/{metric} is no "
            "longer used by the candidate — remove the stale entry"
        )
        assert e["matcher"] in d["shipped_raw"], (
            f"[{pack}] ledger inlined_matchers {e['matcher']!r} no longer appears verbatim "
            "in the shipped pack — shipped filter changed; re-align the candidate matcher "
            "and update the ledger"
        )


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_predicate_structure_divergences_still_real(pack):
    d = _load(pack)
    for (name, field), e in _ledger_alert_div(pack).items():
        assert name in d["shipped_alerts"], (
            f"[{pack}] ledger alert_divergence references unknown shipped alert {name!r}"
        )
        if field != "predicate_structure":
            continue
        assert name in d["cand_alerts"], (
            f"[{pack}] ledger predicate_structure entry for {name!r} but the alert is not "
            "in the candidate file — move it to absent_from_candidate or remove it"
        )
        evidence = e.get("shipped_evidence")
        assert evidence, f"[{pack}] {name}: predicate_structure entry needs shipped_evidence"
        assert evidence in d["shipped_raw"], (
            f"[{pack}] {name}: shipped_evidence {evidence!r} no longer present in the shipped "
            "pack — the declared divergence may have disappeared; re-verify and update the ledger"
        )


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_group_interval_matches_ledger_declaration(pack):
    d = _load(pack)
    entry = next(
        e for e in _ledger()["pack_level"] if e["id"] == "group-interval-30s"
    )
    assert pack in entry["packs"]
    declared = str(entry["candidate_value"])
    for g in d["cand_doc"]["groups"]:
        assert str(g.get("interval")) == declared, (
            f"[{pack}] candidate group {g['name']!r} interval={g.get('interval')!r} != ledger "
            f"declaration {declared!r} — update both together"
        )
    # exit-lock on the shipped side of the declaration: alert groups carry no
    # explicit interval (engine default). If shipped starts pinning one, this
    # ledger entry is stale and the candidate cadence must be revisited.
    for g in d["shipped_doc"]["groups"]:
        if any("alert" in r for r in g.get("rules", [])):
            assert "interval" not in g, (
                f"[{pack}] shipped alert group {g['name']!r} now declares an explicit "
                f"interval={g['interval']!r} — divergence-ledger group-interval-30s is stale"
            )


def test_ledger_pack_keys_valid():
    led = _ledger()
    for section in ("absent_from_candidate", "alert_divergence", "inlined_matchers"):
        for pack in (led.get(section) or {}):
            assert pack in PACKS, f"ledger section {section}: unknown pack key {pack!r}"
    for e in led["pack_level"]:
        for pack in e["packs"]:
            assert pack in PACKS, f"ledger pack_level {e['id']}: unknown pack key {pack!r}"
