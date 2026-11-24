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
    that already carry da_cluster, are skipped. So are blocks whose input
    declares a cluster (`expected_cluster`, a `kind@cluster` entry of
    `_expected_exporters`): what such a block asserts depends on which of
    its series carry which cluster, which is what it writes out itself
    (rule-pack-liveness-per-cluster_test.yaml).
  * the TEXT of an alert whose annotations name the cluster (they read
    `$labels.da_cluster`: `CLUSTER_NAMING`). Its annotations are not the
    same with a cluster label as without one, by design, so the replay
    checks which of them fire and with which labels — through
    `ALERTS{alertname="X", alertstate="firing"}` — and leaves the text to
    the tests that write the cluster out.
  * an alert that is per cluster for one kind of input and per tenant for
    another (its scopes are {cluster, flat}: TenantExporterAbsent) is
    replayed as the shipped tests have it — every one of them declares the
    kind alone, so its alerts are the per-tenant ones.
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
import _lib_consumer_read  # noqa: E402

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

#: Alerts whose annotations name the cluster the alert is for. Read off the
#: rule packs (`_cluster_naming`) and held equal to this, so one cannot be
#: added without the replay's reduced check being a decision.
CLUSTER_NAMING = frozenset({"TenantExporterAbsent", "TenantExporterUndeclared",
                            "CustomRecipeDiskInert", "VersionAwareThresholdInert"})
_NAMES_CLUSTER = re.compile(r"\{\{\s*with\s+\$labels\.da_cluster\s*\}\}")


def _cluster_naming() -> "set[str]":
    out = set()
    for path in _lib_consumer_read.RULE_PACKS.files_under(ROOT):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for group in doc.get("groups") or []:
            for rule in group.get("rules") or []:
                if "alert" in rule and any(_NAMES_CLUSTER.search(str(v))
                                           for v in (rule.get("annotations") or {}).values()):
                    out.add(rule["alert"])
    return out


def test_the_alerts_that_name_the_cluster_are_the_listed_ones():
    assert _cluster_naming() == CLUSTER_NAMING


def _declares_a_cluster(series: "list[dict]") -> bool:
    return any("da_cluster" in (s.get("series") or "") or "expected_cluster" in (s.get("series") or "")
               for s in series)


def _firing_labels_case(case: dict, clusters: "list[str]") -> dict:
    """*case* (an alert_rule_test of a cluster-scoped alert) as a
    promql_expr_test: the alerts that fire and their whole label sets, one
    per cluster, without their annotations."""
    name = case["alertname"]
    samples = []
    for exp in case.get("exp_alerts") or []:
        for c in clusters:
            labels = dict(exp.get("exp_labels") or {}, alertname=name, alertstate="firing", da_cluster=c)
            body = ", ".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
            samples.append({"labels": f"ALERTS{{{body}}}", "value": 1})
    return {"expr": f'ALERTS{{alertname="{name}", alertstate="firing"}}',
            "eval_time": case["eval_time"], "exp_samples": samples}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


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
        if _declares_a_cluster(series):
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
        named = [c for c in cases if c["alertname"] in CLUSTER_NAMING
                 and alerts.get(c["alertname"]) == {cci.CLUSTER}]
        if named:
            cases = [c for c in cases if c not in named]
            exprs += [_firing_labels_case(c, clusters) for c in named]
            block["promql_expr_test"] = exprs
            block["alert_rule_test"] = cases
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
            if _declares_a_cluster(block.get("input_series") or []):
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
