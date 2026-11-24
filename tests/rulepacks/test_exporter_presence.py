"""The exporter kinds and their presence metrics have one source: the registry (#3771, #4097).

* A KIND is a pack of rule-packs/threshold-registry.yaml with ``presence:``.
  Every other list of kinds is that set, held in both directions: the
  ``_expected_exporters`` enum of the tenant schema (a kind a tenant can
  declare with no presence metric would page forever; a kind the schema
  refuses is a dead row), and the exporter's own allowed-kind table (generated;
  a kind the exporter refuses would be the key verdict's error on a value the
  schema accepts). ``_lib_constants.JOB_DB_MAP`` names only kinds.
* The three alerts that need presence — TenantExporterAbsent and
  TenantExporterUndeclared (liveness pack), MassExporterOutage (platform pack) —
  carry the expression the registry renders, inline (parsed, not grepped); no
  recording rule stands between them and the raw tenant-labelled series, so a
  pack loaded alone or the federation split's central half still works.
* TenantExporterAbsent and MassExporterOutage join on ``(tenant, pack)``;
  a regression to ``on(tenant)`` lets one kind's exporter mask another's outage.
* TenantExporterAbsent covers every kind (jvm and nginx included);
  TenantExporterUndeclared every kind but those marked
  ``undeclared_notice: false``.
* An entry may be ``kind@cluster`` (#4097). The schema's entry is a kind of
  the enum or ``<kind>@<cluster>`` whose kinds are that enum, both ways, and
  whose cluster part is the exporter's rule (the same cases as
  expected_exporters_test.go). The per-cluster branch of TenantExporterAbsent
  matches on ``(tenant, pack, da_cluster)``; the presence series with no
  ``da_cluster`` are the same table with one more matcher; the label names
  are the exporter's and the cluster-identity lint's. What the rules DO is
  promtool's (rule-pack-liveness-per-cluster_test.yaml,
  platform-mass-outage-per-cluster_test.yaml).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "lint"))

import _lib_exporter_presence as presence  # noqa: E402
from _lib_consumer_read import RULE_PACKS  # noqa: E402
import _lib_constants  # noqa: E402
import _lib_metric_sources as ms  # noqa: E402
import _registry_lib  # noqa: E402
import _grar_validate  # noqa: E402
import check_cluster_identity  # noqa: E402

SCHEMA = ROOT / "docs" / "schemas" / "tenant-config.schema.json"
LIVENESS = ROOT / presence.LIVENESS_PACK
PLATFORM = ROOT / "rule-packs" / "rule-pack-platform.yaml"
GO_KINDS = (ROOT / "components" / "threshold-exporter" / "app" / "pkg" / "config"
            / "expected_exporter_kinds_gen.go")


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def _schema_enum() -> list[str]:
    return _schema()["definitions"]["exporterKind"]["enum"]


def _registry_packs() -> dict:
    return yaml.safe_load((ROOT / "rule-packs" / "threshold-registry.yaml").read_text(
        encoding="utf-8"))["packs"]


def _registry_kinds() -> set[str]:
    """The kinds, read off the registry file itself (not through the lib)."""
    return {name for name, meta in _registry_packs().items() if meta.get("presence")}


def _rules(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g.get("rules", [])]


def test_the_kinds_are_the_registry_packs_with_presence():
    kinds = _registry_kinds()
    assert set(presence.presence_table()) == kinds
    # The hole `_metadata.db_type` left: a tenant running only nginx or a JVM
    # could declare nothing. kubernetes has no exporter of the tenant's.
    assert {"nginx", "jvm", "mariadb"} <= kinds
    assert "kubernetes" not in kinds
    assert _registry_lib.expected_exporter_kinds(_registry_lib.load_registry()) == sorted(kinds)


def test_schema_enum_equals_the_registry_kinds():
    enum = _schema_enum()
    assert len(enum) == len(set(enum))
    kinds = _registry_kinds()
    missing = sorted(kinds - set(enum))
    extra = sorted(set(enum) - kinds)
    assert not missing, f"registry kinds the schema refuses: {missing}"
    assert not extra, f"kinds the schema accepts with no presence metric: {extra}"


def test_exporter_allowed_kinds_equal_the_registry_kinds():
    """The Go table the exporter judges `_expected_exporters` with, parsed
    from the generated file — and that file is what the registry renders."""
    text = GO_KINDS.read_text(encoding="utf-8")
    body = text[text.index("var expectedExporterKinds = []string{"):]
    table = re.findall(r'^\t"([^"]+)",$', body, flags=re.M)
    assert table == sorted(_registry_kinds())
    doc = _registry_lib.load_registry()
    assert _registry_lib.check_expected_kinds_go(doc) is None
    assert text == _registry_lib.render_expected_kinds_go(doc)


def test_job_db_map_names_only_kinds():
    """`_lib_constants.JOB_DB_MAP` (job name -> kind, blind-spot-discovery's
    inference) may name fewer kinds than the registry — not every kind can be
    told from a job name — never one the registry does not have."""
    values = set(_lib_constants.JOB_DB_MAP.values())
    assert values, "JOB_DB_MAP is empty: the subset check would pass on nothing"
    assert values <= _registry_kinds(), sorted(values - _registry_kinds())


def _schema_entry_pattern() -> str:
    """The `kind@cluster` pattern of the schema's entry, after holding the
    entry to its shape: a kind of the enum, or that pattern — nothing else."""
    entry = _schema()["definitions"]["expectedExporterEntry"]
    assert set(entry) - {"title", "description", "$comment"} == {"anyOf"}
    bare, per_cluster = entry["anyOf"]
    assert bare == {"$ref": "#/definitions/exporterKind"}
    assert set(per_cluster) == {"type", "pattern", "not"} and per_cluster["type"] == "string"
    # A validator whose `$` also matches before a final newline (Python's)
    # would take `kind@cluster\n` on the pattern alone.
    assert per_cluster["not"] == {"pattern": r"\n"}
    return per_cluster["pattern"]


_ENTRY_PATTERN = re.compile(r"\^\(\?:(?P<kinds>[a-z0-9|]+)\)@(?P<cluster>.+)\$")


def test_schema_expected_exporters_is_a_list_of_entries():
    doc = _schema()
    prop = doc["definitions"]["tenantConfig"]["properties"]["_expected_exporters"]
    assert prop["type"] == ["array", "null"]
    assert prop["items"] == {"$ref": "#/definitions/expectedExporterEntry"}
    assert prop["uniqueItems"] is True


def test_schema_entry_kinds_equal_the_registry_kinds():
    """The kinds before `@` are the registry's, both ways: a kind the pattern
    lacks cannot be declared per cluster; one it has beyond the registry
    would be accepted here and refused by the exporter."""
    m = _ENTRY_PATTERN.fullmatch(_schema_entry_pattern())
    assert m, _schema_entry_pattern()
    kinds = m["kinds"].split("|")
    assert len(kinds) == len(set(kinds))
    assert set(kinds) == _registry_kinds(), sorted(set(kinds) ^ _registry_kinds())
    assert kinds == _schema_enum(), "the pattern's kinds and the enum are two lists of one thing"


#: Cluster ids the exporter reads / refuses — the cases of
#: TestIsExpectedClusterID (pkg/config/expected_exporters_test.go).
_CLUSTER_OK = ["a", "0", "east", "edge-1", "eu.west_1-b", "A9", "x" * 63]
_CLUSTER_BAD = ["", "x" * 64, "-a", "a-", ".a", "a.", "_a", "a b", "a@b", "a/b", 'a"b', "a\nb",
                "é", "a,b", "east\n"]


def test_schema_entry_cluster_part_is_the_exporters_rule():
    pattern = _schema_entry_pattern()
    # ECMA-262 `$` has no "before a final newline" reading; Python's does, so
    # the schema's pattern is tested the way a JSON Schema validator runs it
    # on a whole string: \Z.
    assert pattern.endswith("$")
    rx = re.compile(pattern[:-1] + r"\Z")
    for cluster in _CLUSTER_OK:
        assert rx.match(f"mariadb@{cluster}"), cluster
    for cluster in _CLUSTER_BAD:
        assert not rx.match(f"mariadb@{cluster}"), cluster
    assert not rx.match("mariadb") and not rx.match("@east") and not rx.match("MySQL@east")
    assert not rx.match("mariadb@east@west")
    # The schema itself, as a validator runs it (the trailing newline included).
    import jsonschema  # noqa: PLC0415
    validator = jsonschema.Draft7Validator(_schema())
    def refused(entry: str) -> bool:
        return bool(list(validator.iter_errors({"tenants": {"t-1": {"_expected_exporters": [entry]}}})))
    for cluster in _CLUSTER_OK:
        assert not refused(f"mariadb@{cluster}"), cluster
    for cluster in _CLUSTER_BAD:
        assert refused(f"mariadb@{cluster}"), cluster
    # … and the Go rule is spelt in the file the schema's comment points to.
    go = (GO_KINDS.parent / "expected_exporters.go").read_text(encoding="utf-8")
    assert "^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$" in go
    assert "(?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$" in pattern


def test_schema_metadata_has_no_db_type():
    """`_metadata.db_type` is removed, no alias (#4097): the schema refuses it
    (additionalProperties: false) and no definition of its values is left."""
    doc = _schema()
    meta = doc["definitions"]["metadata"]
    assert "db_type" not in meta["properties"]
    assert meta["additionalProperties"] is False
    assert "dbTypeValue" not in doc["definitions"]


def test_every_kind_has_a_presence_selector_without_a_job_matcher():
    table = presence.presence_table()
    assert table
    for kind, metrics in table.items():
        assert metrics, kind
        for m in metrics:
            assert re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\{[a-z_]+="[a-z0-9_]+"\})?', m), (
                kind, m)
            assert "job" not in m, (kind, m)
    assert _registry_lib.presence_problems(_registry_lib.load_registry()) == []


def test_a_presence_selector_with_a_job_matcher_is_refused():
    doc = {"packs": {"x": {"presence": ['x_up{job="other"}']},
                     "y": {"undeclared_notice": False}}}
    problems = _registry_lib.presence_problems(doc)
    assert any("'x'" in p and "job" in p for p in problems), problems
    assert any("'y'" in p and "undeclared_notice without presence" in p for p in problems), problems


def test_undeclared_notice_skips_exactly_the_packs_marked_false():
    packs = _registry_packs()
    marked = {name for name, meta in packs.items() if meta.get("undeclared_notice") is False}
    assert marked == {"jvm"}, marked
    assert set(presence.undeclared_kinds()) == _registry_kinds() - marked
    absent = next(r for r in _rules(LIVENESS) if r.get("alert") == "TenantExporterAbsent")
    undeclared = next(r for r in _rules(LIVENESS) if r.get("alert") == "TenantExporterUndeclared")
    for kind in _registry_kinds():
        term = f'"pack", "{kind}", "", ""'
        assert term in absent["expr"], f"TenantExporterAbsent does not cover {kind}"
        assert (term in undeclared["expr"]) == (kind not in marked), kind


def test_alert_exprs_are_fresh():
    """The three alerts' expr in the packs are exactly what the table renders
    (parsed YAML, not text)."""
    for (pack, alert), want in presence.alert_exprs().items():
        rule = next(r for r in _rules(ROOT / pack) if r.get("alert") == alert)
        assert rule["expr"] == want + "\n", (
            f"{pack}: {alert} is stale: python3 scripts/tools/_lib_exporter_presence.py --write")


def test_presence_reads_every_table_metric_on_the_tenant_exporters_job():
    undeclared = set(presence.undeclared_kinds())
    for kind, metrics in presence.presence_table().items():
        expr = presence.presence_expr()
        assert f'"pack", "{kind}", "", ""' in expr, kind
        for metric in metrics:
            assert presence.selector(metric) in expr
            assert 'job="tenant-exporters"' in presence.selector(metric)
            assert (f'max_over_time({presence.selector(metric)}[1h])' in
                    presence.presence_over_time_expr()) == (kind in undeclared)


def test_no_rule_reads_a_record_another_rule_must_load_first():
    """#3771 review: the liveness alerts and MassExporterOutage read raw series
    only — no recording rule of their own, none of another pack."""
    for path in (LIVENESS, PLATFORM):
        records = {r["record"] for r in _rules(path) if "record" in r}
        assert not {x for x in records if x.startswith("tenant:exporter")}, path
    for (pack, alert), _ in presence.alert_exprs().items():
        rule = next(r for r in _rules(ROOT / pack) if r.get("alert") == alert)
        assert "tenant:" not in rule["expr"], (alert, "reads a recording rule")


def test_cli_check_mode_reports_fresh(capsys):
    assert presence.main(["--root", str(ROOT)]) == 0


def test_cli_check_mode_reports_stale(tmp_path):
    texts = {}
    # Every pack the generator writes must be under --root (#1200 D-A A3 added
    # <prefix>ThresholdNotConfigured to the packs with unset keys).
    for pack in sorted({p for p, _alert in presence.alert_exprs()}):
        dst = tmp_path / pack
        dst.parent.mkdir(parents=True, exist_ok=True)
        texts[dst] = (ROOT / pack).read_text(encoding="utf-8")
        stale = (texts[dst].replace('mysql_up{job="tenant-exporters"}', "mysql_up")
                 if pack in (presence.LIVENESS_PACK, presence.PLATFORM_PACK) else texts[dst])
        dst.write_text(stale, encoding="utf-8")
    assert presence.main(["--root", str(tmp_path)]) == 1
    assert presence.main(["--root", str(tmp_path), "--write"]) == 0
    for dst, text in texts.items():
        assert dst.read_text(encoding="utf-8") == text


def _presence_joins(expr: str) -> list[tuple[list[str], bool]]:
    """Every anti-join against a presence block in *expr*, in order: (the
    `on(...)` labels sorted, whether the block is the unlabelled one)."""
    out = []
    for m in re.finditer(r"unless\s+on\s*\(([^)]*)\)\s*\(\s*(label_replace\([^\n]*)", expr):
        out.append((sorted(x.strip() for x in m[1].split(",")), 'da_cluster=""' in m[2]))
    return out


@pytest.mark.parametrize("path, alert, copies", [
    (LIVENESS, "TenantExporterAbsent", 1),
    (PLATFORM, "MassExporterOutage", 2),   # the absent pairs are written twice: floor and ratio
])
def test_absence_joins_on_tenant_and_pack(path, alert, copies):
    """Every presence anti-join names tenant and pack (a regression to
    `on(tenant)` lets one kind's exporter mask another's outage), and the
    three of one copy are, in order: per cluster on (tenant, pack,
    da_cluster) against all presence; (tenant, pack) against the presence
    that carries NO cluster; (tenant, pack) against all presence for the
    entries written without a cluster."""
    rule = next(r for r in _rules(path) if r.get("alert") == alert)
    joins = _presence_joins(rule["expr"])
    one = [(["da_cluster", "pack", "tenant"], False), (["pack", "tenant"], True),
           (["pack", "tenant"], False)]
    assert joins == one * copies, (alert, joins)


def test_the_cluster_labels_are_the_exporters_and_the_lints():
    """`expected_cluster` is the label collector.go writes for `kind@cluster`;
    `da_cluster` is the one the cluster-identity lint judges."""
    assert presence.CLUSTER_LABEL == check_cluster_identity.CLUSTER_LABEL == "da_cluster"
    collector = (ROOT / "components" / "threshold-exporter" / "app" / "internal" / "scrape"
                 / "collector.go").read_text(encoding="utf-8")
    assert f'[]string{{"tenant", "pack", "{presence.EXPECTED_CLUSTER_LABEL}"}}' in collector
    assert presence.EXPECTED_CLUSTER_LABEL != presence.CLUSTER_LABEL


def test_unlabelled_presence_is_the_same_table_with_one_more_matcher():
    """The presence a per-cluster entry is satisfied by anywhere — series
    with no `da_cluster` — is generated from the table, never a second list:
    line for line the presence expression, each selector with da_cluster=""."""
    full = presence.presence_expr().split("\n")
    bare = presence.presence_expr(unlabelled=True).split("\n")
    assert len(full) == len(bare) == len(presence.presence_table())
    for a, b in zip(full, bare):
        assert b == a.replace('job="tenant-exporters"}', 'job="tenant-exporters", da_cluster=""}'), (a, b)
        assert b.count('da_cluster=""') == a.count('job="tenant-exporters"') >= 1
    assert 'da_cluster' not in presence.presence_expr()
    for metric in ("mysql_up", 'query_interval{query="oracle_liveness"}'):
        assert presence.selector(metric, unlabelled=True).endswith(', da_cluster=""}')


def test_per_cluster_rows_are_reduced_before_the_cluster_is_copied():
    """Two copies of one `kind@cluster` row that differ only in the
    `da_cluster` an edge stamped must not both reach label_replace — the
    alert reduces `by (tenant, pack, expected_cluster)` and drops
    `expected_cluster`; MassExporterOutage reduces `without (…, da_cluster)`
    and has no `by (… tenant …)` at all, which the platform-alert guard would
    read as a tenant label on a platform alert."""
    absent = presence.absent_expr()
    assert ('max by(tenant, pack, expected_cluster) (tenant_expected_exporter{expected_cluster!=""}),\n'
            '      "da_cluster", "$1", "expected_cluster", "(.+)"),\n'
            '    "expected_cluster", "", "", "")') in absent
    assert 'max without(instance, pod) (tenant_expected_exporter{expected_cluster=""})' in absent
    mass = presence.mass_outage_expr()
    assert ('max without(instance, pod, da_cluster) (tenant_expected_exporter{expected_cluster!=""}),'
            in mass)
    assert not re.search(r"\bby\s*\(", mass), "a `by (...)` in the platform alert's expr"
    assert not _grar_validate._EXPR_TENANT_AGG_RE.search(mass)
    assert _grar_validate._EXPR_TENANT_AGG_RE.search(absent), "the guard's pattern no longer sees `by (tenant…)`"


def test_mass_outage_counts_pairs_on_both_sides():
    """Numerator and denominator are both reduced to (tenant, pack) pairs —
    `expected_cluster` aggregated away — before they are counted; and the
    `da_cluster` of the Prometheus a declaration came through too, so floor
    and ratio are the fleet's, not one per source."""
    mass = presence.mass_outage_expr()
    pairs = "max without (instance, pod, expected_cluster, da_cluster) (tenant_expected_exporter)"
    assert presence.DECLARED_PAIRS == pairs
    assert f"count without (tenant, pack) ({pairs})" in mass            # the denominator
    assert mass.count(pairs + "\n") == 2                                 # the two numerators' pairs
    assert mass.count("and on(tenant, pack) (") == 2
    assert "tenant_expected_exporter)" not in mass.replace(pairs, "")    # no row-level read left


def test_undeclared_is_judged_per_cluster():
    expr = presence.undeclared_expr()
    assert 'unless on(tenant, pack) tenant_expected_exporter{expected_cluster=""}\n' in expr
    assert ('unless on(tenant, pack, da_cluster) (\n'
            '  label_replace(group by(tenant, pack, expected_cluster) '
            '(tenant_expected_exporter{expected_cluster!=""}),\n'
            '    "da_cluster", "$1", "expected_cluster", "(.+)")\n'
            '  or group by(tenant, pack) (tenant_expected_exporter{expected_cluster!=""})\n'
            ')\n') in expr
    assert expr.startswith("group by(tenant, da_cluster, pack) (\n")
    assert expr.endswith('and on(tenant) tenant_metadata_info\nunless on(tenant)\n'
                         '(user_state_filter{filter="maintenance"} == 1)')


def test_mass_exporter_outage_is_portable_to_operator_mode():
    """It reads tenant exporters' own series under the tenant-exporters job,
    which an Operator install scrapes as that job (#3720)."""
    rule = next(r for r in _rules(PLATFORM) if r.get("alert") == "MassExporterOutage")
    assert ms.operator_unportable(ms._lib_consumer_read.promql_shape([rule["expr"]])[0]) == []


_SPLIT_CENTRAL_TEST = """\
rule_files:
  - {central}
evaluation_interval: 1m
tests:
  - interval: 1m
    # What the central Prometheus has: only series that carry a tenant label
    # (federation match[] {{tenant!=""}}; docs/integration/federation-integration.md).
    input_series:
      - series: 'tenant_expected_exporter{{tenant="t-a", pack="mariadb", cluster="edge-1"}}'
        values: '1x20'
      - series: 'tenant_expected_exporter{{tenant="t-a", pack="redis", cluster="edge-1"}}'
        values: '1x20'
      - series: 'redis_up{{job="tenant-exporters", tenant="t-a", instance="10.7.0.2:9121", cluster="edge-1"}}'
        values: '1x20'
      - series: 'up{{job="tenant-exporters", tenant="t-a", instance="10.7.0.2:9121", cluster="edge-1"}}'
        values: '1x20'
    promql_expr_test:
      - expr: count by(alertname, pack) (ALERTS{{alertstate="firing", alertname="TenantExporterAbsent"}})
        eval_time: 10m
        exp_samples:
          - labels: '{{alertname="TenantExporterAbsent", pack="mariadb"}}'
            value: 1
"""


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
def test_federation_split_central_absent_fires(tmp_path):
    """#3771 review: the federation split puts recording rules on the edge and
    alerts on the central Prometheus, which pulls only tenant-labelled series.
    The split's own central liveness file must still page for a declared kind
    whose exporter is gone (and not for the healthy one)."""
    out = tmp_path / "split"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "tools" / "ops" / "generate_rule_pack_split.py"),
         "--rule-packs-dir", str(ROOT / "rule-packs"), "--output-dir", str(out), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    central = out / "central-rules" / presence.LIVENESS_PACK.name
    assert central.is_file()
    # The liveness pack has no recording rule, so the split writes no edge
    # file for it: nothing the central alerts need lives on the edge.
    assert not (out / "edge-rules" / presence.LIVENESS_PACK.name).exists()
    test = tmp_path / "central_test.yaml"
    test.write_text(_SPLIT_CENTRAL_TEST.format(central=central), encoding="utf-8")
    run = subprocess.run(["promtool", "test", "rules", str(test)], capture_output=True,
                         text=True, encoding="utf-8", timeout=120, check=False)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]


# --- <prefix>ThresholdNotConfigured (#1200 D-A A3) --------------------------

def _pack_alerts(pack: str) -> list[str]:
    return [r["alert"] for r in _rules(ROOT / RULE_PACKS.directory / RULE_PACKS.file_name(pack)) if "alert" in r]


def test_the_unset_notice_is_on_exactly_the_packs_with_unset_keys():
    """Every pack reading a key the platform ships no value for carries
    <prefix>ThresholdNotConfigured, and no other pack carries one — both
    directions, derived from the registry. A pack without it would leave its
    unset keys silent; a pack with it and no such key would join a series the
    exporter never serves."""
    packs = set(presence.unset_notice_packs())
    assert packs, "the registry has no operator key: the derivation is broken"
    carrying = set()
    for path in RULE_PACKS.files_in(ROOT / RULE_PACKS.directory):
        if any(r.get("alert", "").endswith(presence.UNSET_NOTICE_SUFFIX) for r in _rules(path)):
            carrying.add(path.stem.removeprefix("rule-pack-"))
    assert carrying == packs
    for pack in packs:
        assert _pack_alerts(pack).count(presence.unset_notice_alert(pack)) == 1, pack


def test_alert_prefix_is_the_packs_own():
    """Each ALERT_PREFIX entry is the prefix every alert of that pack already
    carries, so the generated name follows the pack's convention."""
    for pack, prefix in presence.ALERT_PREFIX.items():
        alerts = _pack_alerts(pack)
        assert alerts, pack
        assert all(a.startswith(prefix) for a in alerts), (pack, [a for a in alerts if not a.startswith(prefix)])


def test_every_unset_notice_pack_has_a_presence_metric():
    for pack in presence.unset_notice_packs():
        assert presence.pack_presence(pack), pack


def test_a_pack_with_unset_keys_and_no_presence_metric_is_refused(monkeypatch):
    monkeypatch.setattr(presence, "presence_table", lambda: {})
    with pytest.raises(ValueError, match="no presence metric"):
        presence.unset_notice_expr("jvm")


def test_threshold_not_configured_joins_the_pack_on_tenant_outside_maintenance():
    expr = presence.unset_notice_expr("redis")
    assert 'max without(instance, pod) (user_threshold_unset{pack="redis"})' in expr
    assert "and on(tenant)" in expr
    assert 'redis_up{job="tenant-exporters"}' in expr
    assert 'unless on(tenant)\n(user_state_filter{filter="maintenance"} == 1)' in expr


def _shipped_state_filter_copies() -> dict[str, dict]:
    """Every place the platform ships `state_filters` from, as parsed."""
    sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))
    import init_project  # noqa: PLC0415
    import scaffold_tenant  # noqa: PLC0415

    def load(rel: str) -> dict:
        return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))

    copies = {
        "helm values": load("helm/threshold-exporter/values.yaml")["thresholdConfig"]["state_filters"],
        "environments/local": load("environments/local/threshold-exporter.yaml")["thresholdConfig"]["state_filters"],
        "dev conf.d": load("components/threshold-exporter/config/conf.d/_defaults.yaml")["state_filters"],
        "scaffold": scaffold_tenant.RULE_PACKS["kubernetes"]["state_filters"],
    }
    copies["init"] = init_project._defaults_file(["kubernetes"], "monitoring").data["state_filters"]
    return copies


def test_every_shipped_state_filter_copy_offers_the_unset_notice_off():
    """The unset notice is opt-in (owner 2026-11-15): every root the platform
    ships carries the `unset_thresholds` filter — without it a tenant cannot
    subscribe — and every copy defaults it off."""
    for where, filters in _shipped_state_filter_copies().items():
        f = filters.get("unset_thresholds")
        assert f, f"{where}: no unset_thresholds state filter"
        assert f.get("default_state") == "disable", (where, f)
