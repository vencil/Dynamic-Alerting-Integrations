"""The db_type → exporter-presence table is the one source (#3771).

* Its keys equal the ``_metadata.db_type`` enum of the tenant schema, in both
  directions — a db_type a tenant can declare with no presence metric would page
  forever, and a table row the schema refuses is a dead row.
* The liveness pack's generated block is exactly what the table renders, and the
  pack's ``tenant:exporter_present`` rules (parsed, not grepped) are exactly the
  table's — one per db_type, each reading its metric(s) under
  ``job="tenant-exporters"``.
* TenantExporterAbsent and MassExporterOutage join on ``(tenant, db_type)``
  against that record; a regression to ``on(tenant)`` lets one kind's exporter
  mask another's outage.
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


def test_generated_block_is_fresh():
    text = LIVENESS.read_text(encoding="utf-8")
    assert presence.committed_block(text) == presence.render_block(), (
        "rule-pack-liveness.yaml's exporter-presence block is stale: "
        "python3 scripts/tools/_lib_exporter_presence.py --write")


def test_pack_presence_rules_equal_table():
    got = [r for r in _rules(LIVENESS) if r.get("record") == presence.PRESENCE_RECORD]
    want = presence.presence_rules()
    assert [(r["expr"], r.get("labels")) for r in got] == [(r["expr"], r["labels"]) for r in want]
    for rule in got:
        db_type = rule["labels"]["db_type"]
        for metric in presence.EXPORTER_PRESENCE[db_type]:
            assert f'{metric}{{job="tenant-exporters"}}' in rule["expr"]


def test_cli_check_mode_reports_fresh(capsys):
    assert presence.main(["--root", str(ROOT)]) == 0


def test_cli_check_mode_reports_stale(tmp_path):
    pack = tmp_path / presence.LIVENESS_PACK
    pack.parent.mkdir(parents=True)
    text = LIVENESS.read_text(encoding="utf-8")
    pack.write_text(text.replace('mysql_up{job="tenant-exporters"}', 'mysql_up', 1), encoding="utf-8")
    assert presence.main(["--root", str(tmp_path)]) == 1
    assert presence.main(["--root", str(tmp_path), "--write"]) == 0
    assert pack.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("path, alert", [
    (LIVENESS, "TenantExporterAbsent"),
    (PLATFORM, "MassExporterOutage"),
])
def test_absence_joins_on_tenant_and_db_type(path, alert):
    rule = next(r for r in _rules(path) if r.get("alert") == alert)
    joins = re.findall(r"unless\s+on\s*\(([^)]*)\)\s*tenant:exporter_present", rule["expr"])
    assert joins, f"{alert} does not anti-join tenant:exporter_present"
    for j in joins:
        assert sorted(x.strip() for x in j.split(",")) == ["db_type", "tenant"], (alert, j)


def test_cross_pack_records_are_liveness_records_on_the_tenant_exporters_job():
    """Operator mode keeps a platform rule that reads a CROSS_PACK_RECORDS name
    (MassExporterOutage reads tenant:exporter_present). That holds only while
    the liveness pack records it, and every input of its rules selects the
    tenant-exporters job the Operator doc's ServiceMonitor sets."""
    rules = [r for r in _rules(LIVENESS) if "record" in r]
    for name in ms.CROSS_PACK_RECORDS:
        mine = [r for r in rules if r["record"] == name]
        assert mine, f"{name}: not recorded by {LIVENESS.name}"
        for rule in mine:
            for metric in ms.extract_metrics(rule["expr"]):
                jobs = ms._job_matchers(metric, rule["expr"])
                assert jobs and all(j == ("=", ms.TENANT_EXPORTER_JOB) for j in jobs), (
                    name, metric, jobs)


_PRESENCE_GONE_TEST = """\
rule_files:
  - {liveness}
  - {platform}
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: 'tenant_expected_exporter{{tenant="t-a", db_type="mariadb"}}'
        values: '1x20'
      - series: 'up{{job="tenant-exporters", tenant="t-a", instance="10.6.0.1:9104"}}'
        values: '0x20'
    promql_expr_test:
      # Which of the two alerts is firing, by name only (their annotations are
      # pinned by the promtool suites; here only firing / not matters).
      - expr: count by(alertname) (ALERTS{{alertstate="firing", alertname=~"TenantExporterAbsent|ExporterPresenceRulesNotLoaded"}})
        eval_time: 15m
        exp_samples:
          - labels: '{{alertname="{firing}"}}'
            value: 1
"""


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
@pytest.mark.parametrize("presence_loaded", [True, False], ids=["whole-pack", "presence-gone"])
def test_absent_is_guarded_on_the_presence_sentinel(tmp_path, presence_loaded):
    """The liveness pack with its generated presence rules cut out (a hand-split
    copy, an old projection): TenantExporterAbsent must stay silent instead of
    paging for every declared pair, and ExporterPresenceRulesNotLoaded must
    name the state. The whole pack, same input: Absent fires (the input does
    exercise it) and the sentinel alert does not."""
    text = LIVENESS.read_text(encoding="utf-8")
    if not presence_loaded:
        before, _block, after = presence._split(text)
        text = before + after
    liveness = tmp_path / "liveness.yaml"
    liveness.write_text(text, encoding="utf-8")
    test = tmp_path / "t_test.yaml"
    test.write_text(_PRESENCE_GONE_TEST.format(
        liveness=liveness, platform=PLATFORM,
        firing="TenantExporterAbsent" if presence_loaded else "ExporterPresenceRulesNotLoaded",
    ), encoding="utf-8")
    proc = subprocess.run(["promtool", "test", "rules", str(test)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
