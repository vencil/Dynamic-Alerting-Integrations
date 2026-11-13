"""The db_type → exporter-presence table is the one source (#3771).

* Its keys equal the ``_metadata.db_type`` enum of the tenant schema, in both
  directions — a db_type a tenant can declare with no presence metric would page
  forever, and a table row the schema refuses is a dead row.
* The three alerts that need presence — TenantExporterAbsent and
  TenantExporterUndeclared (liveness pack), MassExporterOutage (platform pack) —
  carry the expression the table renders, inline (parsed, not grepped); no
  recording rule stands between them and the raw tenant-labelled series, so a
  pack loaded alone or the federation split's central half still works.
* TenantExporterAbsent and MassExporterOutage join on ``(tenant, db_type)``;
  a regression to ``on(tenant)`` lets one kind's exporter mask another's outage.
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

import _lib_exporter_presence as presence  # noqa: E402
import _lib_metric_sources as ms  # noqa: E402

SCHEMA = ROOT / "docs" / "schemas" / "tenant-config.schema.json"
LIVENESS = ROOT / presence.LIVENESS_PACK
PLATFORM = ROOT / "rule-packs" / "rule-pack-platform.yaml"


def _schema_enum() -> list[str]:
    doc = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return doc["definitions"]["dbTypeValue"]["enum"]


def _rules(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g.get("rules", [])]


def test_table_keys_equal_schema_enum():
    enum = _schema_enum()
    table = list(presence.EXPORTER_PRESENCE)
    assert len(enum) == len(set(enum))
    missing = sorted(set(enum) - set(table))
    extra = sorted(set(table) - set(enum))
    assert not missing, f"db_type the schema accepts with no presence metric: {missing}"
    assert not extra, f"presence rows for a db_type the schema refuses: {extra}"


def test_schema_db_type_takes_string_or_list_of_the_enum():
    doc = json.loads(SCHEMA.read_text(encoding="utf-8"))
    prop = doc["definitions"]["metadata"]["properties"]["db_type"]
    ref = {"$ref": "#/definitions/dbTypeValue"}
    assert prop["oneOf"][0] == ref
    arr = prop["oneOf"][1]
    assert arr["type"] == "array" and arr["items"] == ref
    assert arr["minItems"] == 1 and arr["uniqueItems"] is True


def test_every_db_type_has_a_presence_metric():
    for db_type, metrics in presence.EXPORTER_PRESENCE.items():
        assert metrics, db_type
        for m in metrics:
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m), (db_type, m)


def test_alert_exprs_are_fresh():
    """The three alerts' expr in the packs are exactly what the table renders
    (parsed YAML, not text)."""
    for (pack, alert), want in presence.alert_exprs().items():
        rule = next(r for r in _rules(ROOT / pack) if r.get("alert") == alert)
        assert rule["expr"] == want + "\n", (
            f"{pack}: {alert} is stale: python3 scripts/tools/_lib_exporter_presence.py --write")


def test_presence_reads_every_table_metric_on_the_tenant_exporters_job():
    for db_type, metrics in presence.EXPORTER_PRESENCE.items():
        expr = presence.presence_expr()
        assert f'"db_type", "{db_type}", "", ""' in expr, db_type
        for metric in metrics:
            assert f'{metric}{{job="tenant-exporters"}}' in expr
            assert f'max_over_time({metric}{{job="tenant-exporters"}}[1h])' in (
                presence.presence_over_time_expr())


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
    for pack in (presence.LIVENESS_PACK, presence.PLATFORM_PACK):
        dst = tmp_path / pack
        dst.parent.mkdir(parents=True, exist_ok=True)
        texts[dst] = (ROOT / pack).read_text(encoding="utf-8")
        dst.write_text(texts[dst].replace('mysql_up{job="tenant-exporters"}', "mysql_up"),
                       encoding="utf-8")
    assert presence.main(["--root", str(tmp_path)]) == 1
    assert presence.main(["--root", str(tmp_path), "--write"]) == 0
    for dst, text in texts.items():
        assert dst.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("path, alert", [
    (LIVENESS, "TenantExporterAbsent"),
    (PLATFORM, "MassExporterOutage"),
])
def test_absence_joins_on_tenant_and_db_type(path, alert):
    rule = next(r for r in _rules(path) if r.get("alert") == alert)
    joins = re.findall(r"unless\s+on\s*\(([^)]*)\)\s*\(\s*label_replace", rule["expr"])
    assert joins, f"{alert} does not anti-join the presence expression"
    for j in joins:
        assert sorted(x.strip() for x in j.split(",")) == ["db_type", "tenant"], (alert, j)


def test_mass_exporter_outage_is_portable_to_operator_mode():
    """It reads tenant exporters' own series under the tenant-exporters job,
    which an Operator install scrapes as that job (#3720)."""
    rule = next(r for r in _rules(PLATFORM) if r.get("alert") == "MassExporterOutage")
    assert ms.operator_unportable(rule["expr"]) == []


_SPLIT_CENTRAL_TEST = """\
rule_files:
  - {central}
evaluation_interval: 1m
tests:
  - interval: 1m
    # What the central Prometheus has: only series that carry a tenant label
    # (federation match[] {{tenant!=""}}; docs/integration/federation-integration.md).
    input_series:
      - series: 'tenant_expected_exporter{{tenant="t-a", db_type="mariadb", cluster="edge-1"}}'
        values: '1x20'
      - series: 'tenant_expected_exporter{{tenant="t-a", db_type="redis", cluster="edge-1"}}'
        values: '1x20'
      - series: 'redis_up{{job="tenant-exporters", tenant="t-a", instance="10.7.0.2:9121", cluster="edge-1"}}'
        values: '1x20'
      - series: 'up{{job="tenant-exporters", tenant="t-a", instance="10.7.0.2:9121", cluster="edge-1"}}'
        values: '1x20'
    promql_expr_test:
      - expr: count by(alertname, db_type) (ALERTS{{alertstate="firing", alertname="TenantExporterAbsent"}})
        eval_time: 10m
        exp_samples:
          - labels: '{{alertname="TenantExporterAbsent", db_type="mariadb"}}'
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
