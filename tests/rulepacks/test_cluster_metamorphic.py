"""Every rule-pack promtool test, replayed with a cluster label (#4097, ADR-006).

The rule packs keep `da_cluster` wherever they keep an identity label
(scripts/tools/lint/check_cluster_identity.py reads that off the expressions).
This is the behavioural half: take each shipped promtool test and replay it

  one-cluster   every cluster-scoped input series gets da_cluster="east";
                the same alerts must fire — the cluster-scoped ones now
                carrying da_cluster="east". A join against threshold-exporter
                series that started matching on da_cluster goes silent here.
  two-clusters  every cluster-scoped input series exists once in "east" and
                once in "west", same values; each cluster-scoped alert must
                fire once per cluster, with the single-cluster value in its
                annotations, and each flat alert once. A grouping that merges
                the clusters changes the value or the count; an arithmetic or
                comparison matching that drops the label is a many-to-many
                error. (`and` / `unless` that drop it are legal and, with the
                same values in both clusters, look the same: those are the
                lint's, and rule-pack-cluster-masking_test.yaml's.)

Which series and which alerts are cluster-scoped is not listed here: it is
`check_cluster_identity.rule_scopes()`, the same reading the lint uses.

Not covered (so it does not look covered):
  * of the `promql_expr_test` cases only `count by (…) (ALERTS{alertname="X", …})`
    is replayed (the armed-key tests are written that way): the count is the
    same with one cluster and doubles with two when X is cluster-scoped. Every
    other expression's expected label set is written by hand, and is left out.
  * masking by a HEALTHY second cluster needs a fixture that knows what
    healthy looks like; those cases are written out in
    rule-pack-cluster-masking_test.yaml.
  * tests whose rule_files are not all under rule-packs/, and test blocks
    that already carry da_cluster, are skipped.
  * a pack the lint's baseline still lists (a rule that does not keep the
    label yet): a replay that differs there is an expected failure, not a pass.
  * an alert no shipped test makes FIRE is replayed to no effect.
    `test_what_the_replay_reaches` counts them, so the number is on record
    and does not grow unseen.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
import check_cluster_identity as cci  # noqa: E402

PROMTOOL = shutil.which("promtool")
TESTS = sorted((ROOT / "tests" / "rulepacks").glob("*_test.yaml"))
_SERIES = re.compile(r"^\s*(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)?\s*(?:\{(?P<labels>.*)\})?\s*$", re.S)
_LABEL = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?')

_ALERT_COUNT = re.compile(
    r'''^\s*count\s+by\s*\(([^)]*)\)\s*\(\s*ALERTS\{[^}]*alertname="([^"]+)"[^}]*\}\s*\)\s*$''')

#: How many cluster-scoped alerts no replayable test fires (see the docstring).
#: Held both ways: a new rule without a firing test raises the count, a new
#: firing test lowers it, and either fails `test_what_the_replay_reaches` until
#: this number says what the tree does. (#4097 tracks bringing it down.)
UNREACHED_CEILING = 48


@pytest.fixture(scope="module")
def scopes():
    if not os.environ.get("DA_READ_BINARY") and not (ROOT / ".build" / "da-read").exists():
        pytest.skip("da-read is not built (make da-read-build)")
    return cci.rule_scopes()


def _parse(series: str) -> "tuple[str, dict[str, str]]":
    m = _SERIES.match(series)
    assert m, f"cannot read the series {series!r}"
    labels = dict(_LABEL.findall(m["labels"] or ""))
    return m["name"] or labels.get("__name__", ""), labels


def _is_cluster(series: str, records: "dict[str, str]") -> bool:
    name, labels = _parse(series)
    node = {"name": name, "matchers": [{"name": k, "type": "=", "value": v} for k, v in labels.items()]}
    return cci.selector_scope(node, records) == cci.CLUSTER


def _with_cluster(series: str, cluster: str) -> str:
    name, _ = _parse(series)
    body = series.strip()
    if "{" in body:
        head, rest = body.split("{", 1)
        rest = rest.rstrip()[:-1].strip()
        sep = ", " if rest else ""
        return f'{head}{{{rest}{sep}da_cluster="{cluster}"}}'
    return f'{name}{{da_cluster="{cluster}"}}'


def _replay(doc: dict, records: dict, alerts: dict, clusters: "list[str]", src: Path) -> "tuple[dict, int]":
    """→ (the replayed test document, how many test blocks were kept)."""
    out = copy.deepcopy(doc)
    out["rule_files"] = [str((src.parent / f).resolve()) for f in doc.get("rule_files") or []]
    kept = []
    for block in out.get("tests") or []:
        series = block.get("input_series") or []
        if any("da_cluster" in (s.get("series") or "") for s in series):
            continue
        new_series = []
        for s in series:
            if _is_cluster(s["series"], records):
                new_series += [dict(s, series=_with_cluster(s["series"], c)) for c in clusters]
            else:
                new_series.append(s)
        block["input_series"] = new_series
        exprs = []
        for case in block.pop("promql_expr_test", None) or []:
            m = _ALERT_COUNT.match(case.get("expr") or "")
            if not m or "da_cluster" in m.group(1):
                continue
            times = len(clusters) if alerts.get(m.group(2)) == {cci.CLUSTER} else 1
            case = copy.deepcopy(case)
            for s in case.get("exp_samples") or []:
                s["value"] = s["value"] * times
            exprs.append(case)
        if exprs:
            block["promql_expr_test"] = exprs
        cases = block.get("alert_rule_test") or []
        if not cases and not exprs:
            continue
        for case in cases:
            scope = alerts.get(case["alertname"], {cci.FLAT})
            expected = []
            for exp in case.get("exp_alerts") or []:
                if scope == {cci.CLUSTER}:
                    for c in clusters:
                        e = copy.deepcopy(exp)
                        e.setdefault("exp_labels", {})["da_cluster"] = c
                        expected.append(e)
                else:
                    expected.append(exp)
            case["exp_alerts"] = expected
        kept.append(block)
    out["tests"] = kept
    return out, len(kept)


def _rule_files(src: Path) -> "set[str]":
    doc = yaml.safe_load(src.read_text(encoding="utf-8"))
    return {Path(f).name for f in doc.get("rule_files") or []}


def _under_rule_packs(doc: dict, src: Path) -> bool:
    packs = (ROOT / "rule-packs").resolve()
    files = [(src.parent / f).resolve() for f in doc.get("rule_files") or []]
    return bool(files) and all(packs in f.parents for f in files)


def _failing_alerts(output: str) -> "set[str]":
    return (set(re.findall(r"alertname: (\S+?),? ", output))
            | set(re.findall(r'alertname=\\?"([^"\\]+)', output)))


def _run(src: Path, clusters: "list[str]", scopes, tmp_path: Path) -> "tuple[int, str, int]":
    records, alerts = scopes
    doc = yaml.safe_load(src.read_text(encoding="utf-8"))
    if not _under_rule_packs(doc, src):
        pytest.skip("its rule_files are not all under rule-packs/")
    replay, kept = _replay(doc, records, alerts, clusters, src)
    if not kept:
        pytest.skip("nothing in it to replay (no alert_rule_test, no ALERTS count, or already da_cluster)")
    target = tmp_path / src.name
    target.write_text(yaml.safe_dump(replay, allow_unicode=True, sort_keys=False, width=10000), encoding="utf-8")
    r = subprocess.run([PROMTOOL, "test", "rules", str(target)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, r.stdout + r.stderr, kept


@pytest.mark.skipif(PROMTOOL is None, reason="promtool is not installed")
@pytest.mark.parametrize("clusters", [["east"], ["east", "west"]], ids=["one-cluster", "two-clusters"])
@pytest.mark.parametrize("src", TESTS, ids=[p.name for p in TESTS])
def test_replay_with_cluster_label(src, clusters, scopes, tmp_path):
    rc, output, _kept = _run(src, clusters, scopes, tmp_path)
    if rc == 0:
        return
    if rc != 0:
        listed = _rule_files(src) & set(cci.load_baseline())
        if listed:
            # the lint's own list of rules that do not keep the label yet: the
            # replay differing there is that debt, seen as behaviour
            pytest.xfail(f"{sorted(listed)} still in cluster_identity_baseline.yaml")
    assert rc == 0, (
        f"{src.name} replayed in {clusters} differs for {sorted(_failing_alerts(output)) or '?'}:"
        f"\n{output[-3000:]}")


def _fired_in_replayable_tests(alerts: dict) -> "set[str]":
    fired: "set[str]" = set()
    for src in TESTS:
        doc = yaml.safe_load(src.read_text(encoding="utf-8"))
        if not _under_rule_packs(doc, src):
            continue
        for block in doc.get("tests") or []:
            if any("da_cluster" in (s.get("series") or "") for s in block.get("input_series") or []):
                continue
            for case in block.get("alert_rule_test") or []:
                if case.get("exp_alerts"):
                    fired.add(case["alertname"])
            for case in block.get("promql_expr_test") or []:
                m = _ALERT_COUNT.match(case.get("expr") or "")
                if m and case.get("exp_samples"):
                    fired.add(m.group(2))
    return fired


def test_what_the_replay_reaches(scopes):
    _records, alerts = scopes
    cluster = {name for name, sc in alerts.items() if sc == {cci.CLUSTER}}
    unreached = sorted(cluster - _fired_in_replayable_tests(alerts))
    assert len(unreached) == UNREACHED_CEILING, (
        f"{len(unreached)} cluster-scoped alert(s) fire in no replayable test (ceiling "
        f"{UNREACHED_CEILING}): {unreached}")
