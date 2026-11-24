"""Each exporter's metric namespace has one source: the registry's ``metric_prefixes`` (#4270).

``packs.<pack>.metric_prefixes`` in rule-packs/threshold-registry.yaml is what
the scrape gate and the split validator take as a tenant exporter's metric
(``_registry_lib.tenant_exporter_prefixes``) and what the onboarding
heuristics map a metric to a pack by (``_registry_lib.pack_of_metric``). Four
hand copies of it used to disagree; every one wrote ClickHouse as
``clickhouse_``, which no ClickHouse metric is named.

A hand-written prefix holds only if it is pinned both ways, here:

* every raw metric a registry pack's own rules read is under one of its
  prefixes, a query-exporter built-in (a pack with ``queries:``), or a
  platform family (``_lib_metric_sources.FAMILY_TABLE``) — a pack reading
  another exporter's metric, or one no prefix covers, turns this red;
* every prefix is read by the pack's own rules or presence — a prefix no
  metric of the pack is named under (``clickhouse_``) turns this red;
* presence selectors and a query-exporter pack's own metrics are under its
  prefixes;
* no two namespaces overlap, nor overlap the platform's families, so a
  metric has one pack.

What a rule reads comes from Prometheus' parse tree
(``_lib_consumer_read.promql_metric_names``), never its text: a text scan
skipped capitalised names, so ClickHouse's reads were never seen.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))

import _lib_consumer_read as cr  # noqa: E402
import _lib_metric_sources as ms  # noqa: E402
import _registry_lib  # noqa: E402
import generate_rule_pack_split as grps  # noqa: E402
from _lib_yaml_strict import strict_safe_load  # noqa: E402

#: Series Prometheus writes itself: no exporter's namespace.
_MANUFACTURED = frozenset({"up"}) | ms.ALERT_SERIES


def _packs() -> dict:
    return _registry_lib.load_registry()["packs"]


def _selector_name(selector: str) -> str:
    return selector.split("{", 1)[0]


def _under(metric: str, prefixes) -> bool:
    return metric.startswith(tuple(prefixes))


@pytest.fixture(scope="module")
def raw_reads() -> "dict[str, set[str]]":
    """pack -> the raw metrics its own rules read (no recording rule, no
    series Prometheus writes), off the parse tree."""
    packs = _packs()
    files = [ROOT / meta["rule_pack_file"] for meta in packs.values()]
    out: "dict[str, set[str]]" = {}
    for name, rule_file in zip(packs, cr.prometheus_rule_files(files)):
        rules = [r for g in rule_file.groups for r in g.get("rules") or []]
        records = {r["record"] for r in rules if "record" in r}
        names: "set[str]" = set()
        for read in cr.promql_metric_names([str(r.get("expr", "")) for r in rules]):
            if isinstance(read, cr.InputRefused):
                raise read  # a rule file Prometheus loaded parses
            names |= set(read)
        out[name] = {m for m in names
                     if ":" not in m and m not in records and m not in _MANUFACTURED}
    return out


def _builtins(meta: dict) -> frozenset:
    return grps._QUERY_EXPORTER_BUILTINS if meta.get("queries") else frozenset()


def test_every_exporter_kind_declares_its_namespace():
    missing = [n for n, m in _packs().items() if m.get("presence") and not m.get("metric_prefixes")]
    assert not missing, missing


def test_every_raw_read_of_a_pack_is_under_its_own_namespace(raw_reads):
    stray = []
    for name, meta in _packs().items():
        own = meta.get("metric_prefixes") or ()
        for m in sorted(raw_reads[name]):
            if not (_under(m, own) or m in _builtins(meta) or ms.resolve_family(m)):
                stray.append(f"{name}: {m}")
    assert not stray, (
        "a registry pack reads a raw metric outside its own metric_prefixes "
        f"(another exporter's, or a namespace not declared): {stray}")


def test_every_prefix_is_read_by_its_own_pack(raw_reads):
    unread = []
    for name, meta in _packs().items():
        read = raw_reads[name] | {_selector_name(s) for s in meta.get("presence") or ()}
        for prefix in meta.get("metric_prefixes") or ():
            if not any(m.startswith(prefix) for m in read):
                unread.append(f"{name}: {prefix}")
    assert not unread, (
        "a declared prefix names no metric its own pack reads — a namespace "
        f"nothing is named under (as `clickhouse_` was): {unread}")


def test_presence_is_under_its_own_namespace():
    stray = [f"{n}: {s}" for n, m in _packs().items() for s in m.get("presence") or ()
             if not (_under(_selector_name(s), m["metric_prefixes"])
                     or _selector_name(s) in _builtins(m))]
    assert not stray, stray


def test_a_query_exporter_packs_metrics_are_under_its_namespace():
    stray, checked = [], 0
    for name, meta in _packs().items():
        if not meta.get("queries"):
            continue
        doc = strict_safe_load((ROOT / meta["queries"]).read_text(encoding="utf-8"))
        for metric in doc.get("metrics") or {}:
            checked += 1
            if not _under(metric, meta["metric_prefixes"]):
                stray.append(f"{name}: {metric}")
    assert checked, "no query-exporter metric was checked"
    assert not stray, stray


def test_no_two_namespaces_overlap_nor_a_platform_family():
    kinds = {n: m["metric_prefixes"] for n, m in _packs().items() if m.get("presence")}
    flat = [(n, p) for n, ps in kinds.items() for p in ps]
    clash = [f"{a}:{p} / {b}:{q}" for i, (a, p) in enumerate(flat) for (b, q) in flat[i + 1:]
             if p.startswith(q) or q.startswith(p)]
    platform = ([prefix for prefix, _k, _s in ms.FAMILY_TABLE] + list(ms.FAMILY_EXACT)
                + list(ms.PER_TARGET_PREFIXES) + list(ms.PER_TARGET_EXACT)
                + list(grps._QUERY_EXPORTER_BUILTINS))
    clash += [f"{n}:{p} / platform {f}" for n, p in flat for f in platform
              if f.startswith(p) or p.startswith(f)]
    assert not clash, clash


def test_a_platform_scraped_packs_namespace_is_a_modelled_source():
    """A pack without presence (kubernetes: cAdvisor and kube-state-metrics,
    scraped by the platform) is no tenant exporter: each of its prefixes must
    be a family the scrape gate already models."""
    table = {prefix for prefix, _k, _s in ms.FAMILY_TABLE}
    stray = [f"{n}: {p}" for n, m in _packs().items() if not m.get("presence")
             for p in m.get("metric_prefixes") or () if p not in table]
    assert not stray, stray


def test_the_tenant_exporter_prefixes_are_the_kinds_namespaces():
    want = tuple(p for m in _packs().values() if m.get("presence") for p in m["metric_prefixes"])
    assert _registry_lib.tenant_exporter_prefixes() == want
    assert grps._FEDERATED_EXPORTER_PREFIXES == want
    assert not any(re.fullmatch(r"(?:container|kube|kubelet)_", p) for p in want)


@pytest.mark.parametrize("metric,ignore_case,kinds_only,want", [
    ("ClickHouseMetrics_TCPConnection", False, False, "clickhouse"),
    ("ClickHouseProfileEvents_Query", False, False, "clickhouse"),
    ("clickhousemetrics_tcpconnection", True, False, "clickhouse"),
    ("clickhouse_query_total", False, False, None),
    ("MySQL_Global_Status_Uptime", True, False, "mariadb"),
    ("oracledb_up", False, False, "oracle"),
    ("kube_pod_info", False, False, "kubernetes"),
    ("kube_pod_info", False, True, None),
    ("node_cpu_seconds_total", False, False, None),
])
def test_pack_of_metric(metric, ignore_case, kinds_only, want):
    assert _registry_lib.pack_of_metric(metric, ignore_case=ignore_case,
                                        kinds_only=kinds_only) == want
