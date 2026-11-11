"""Every documented tenant-exporter scrape config keeps the contract the rule packs select on (#3720 / #3680).

The tenant rule packs read `up{job="tenant-exporters"}` per `tenant`. Before
#3720 the BYO doc taught `job_name: "tenant-db-exporters"` and the Operator
doc a ServiceMonitor whose job is the Service name, so a customer following
either got `TenantExporterAbsent` firing for every healthy tenant: the
anti-join's right side was empty. The lab config scraped the Service VIP with
`instance` overwritten to the namespace (#3680), so two targets merged into
one series and a healthy one masked a dead one.

One predicate decides both: `check_scrape_reachability.tenant_scrape_contract`
judges the repo's raw config (in the scrape gate) and, here, every scrape
config snippet in the integration docs. The Operator ServiceMonitor is not a
prometheus.yml, so it is held to the same three facts directly: it writes
`tenant`, sets `job` to the one name, and leaves `instance` alone.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
_SCRIPT = REPO / "scripts" / "tools" / "lint" / "check_scrape_reachability.py"
_spec = importlib.util.spec_from_file_location("check_scrape_reachability", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
sys.path.insert(0, str(REPO / "scripts" / "tools"))
import _lib_metric_sources  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_read_env")

JOB = _lib_metric_sources.TENANT_EXPORTER_JOB
DOCS = REPO / "docs" / "integration"
SCRAPE_DOCS = [f"{name}{lang}.md" for name in ("byo-prometheus-integration", "federation-integration")
               for lang in ("", ".en")]
OPERATOR_DOCS = ["operator-prometheus-integration.md", "operator-prometheus-integration.en.md"]
_YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)


def _blocks(doc: str) -> list[str]:
    return _YAML_BLOCK.findall((DOCS / doc).read_text(encoding="utf-8"))


def test_the_name_is_the_one_the_packs_select():
    liveness = (REPO / "rule-packs" / "rule-pack-liveness.yaml").read_text(encoding="utf-8")
    assert f'(up{{job="{JOB}"}} == 1)' in liveness


@pytest.mark.parametrize("doc", SCRAPE_DOCS)
def test_every_documented_scrape_config_keeps_the_contract(doc):
    jobs = [j for text in _blocks(doc) if "scrape_configs:" in text
            for j in gate.parse_scrape_jobs(text)]
    tenant_jobs = [j for j in jobs if j["writes_tenant"]]
    assert tenant_jobs, f"{doc}: no scrape config writing `tenant` found — did the snippet move?"
    assert gate.tenant_scrape_contract(jobs, every_tenant_job=True) == [], doc


def _tenant_service_monitors(doc: str) -> list[dict]:
    out = []
    for text in _blocks(doc):
        for obj in yaml.safe_load_all(text):
            if not isinstance(obj, dict) or obj.get("kind") != "ServiceMonitor":
                continue
            for ep in (obj.get("spec") or {}).get("endpoints") or []:
                rel = ep.get("relabelings") or []
                if any(r.get("targetLabel") == "tenant" for r in rel):
                    out.append(ep)
    return out


@pytest.mark.parametrize("doc", OPERATOR_DOCS)
def test_the_operator_service_monitor_keeps_the_contract(doc):
    endpoints = _tenant_service_monitors(doc)
    assert endpoints, f"{doc}: no ServiceMonitor endpoint writing `tenant` found"
    for ep in endpoints:
        rel = ep["relabelings"]
        jobs = [r for r in rel if r.get("targetLabel") == "job"]
        # prometheus-operator names the job after the Service unless relabeled;
        # the last write wins.
        last = jobs[-1] if jobs else {}
        assert (last.get("replacement") == JOB and not last.get("sourceLabels")
                and last.get("action", "replace") == "replace"
                and last.get("regex", "(.*)") == "(.*)"), (doc, jobs)
        assert not any(r.get("targetLabel") == "instance" for r in rel), doc


_EXTRA = {
    "job relabeled to the Service name": (
        "      - source_labels: [__meta_kubernetes_service_name]\n        target_label: job\n",
        "TENANT-JOB-NAME"),
    "instance overwritten": (
        "      - source_labels: [__meta_kubernetes_namespace]\n        target_label: instance\n",
        "TENANT-INSTANCE"),
    "Service labels mapped (one may be named instance)": (
        "      - action: labelmap\n        regex: __meta_kubernetes_service_label_(.+)\n",
        "TENANT-INSTANCE"),
    "tenant written, then dropped": (
        "      - action: labeldrop\n        regex: tenant\n",
        "TENANT-LABEL"),
}


@pytest.mark.parametrize("label,snippet,code", [
    ("another job name", 'job_name: "tenant-db-exporters"', "TENANT-JOB-NAME"),
    ("the service role", "role: service", "TENANT-PER-POD"),
] + [(label, None, code) for label, (_rc, code) in _EXTRA.items()])
def test_the_contract_turns_red(label, snippet, code):
    # The acceptance case of #3720: the snippet the BYO doc taught before.
    text = (
        "scrape_configs:\n"
        f"  - job_name: \"{JOB}\"\n"
        "    kubernetes_sd_configs:\n"
        "      - role: endpointslice\n"
        "    relabel_configs:\n"
        "      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]\n"
        "        action: keep\n        regex: \"true\"\n"
        "      - source_labels: [__meta_kubernetes_namespace]\n"
        "        action: keep\n        regex: \"db-.+\"\n"
        "      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_port]\n"
        "        action: keepequal\n        target_label: __meta_kubernetes_endpointslice_port\n"
        "      - source_labels: [__meta_kubernetes_namespace]\n"
        "        target_label: tenant\n")
    assert gate.tenant_scrape_contract(gate.parse_scrape_jobs(text)) == []
    if snippet == 'job_name: "tenant-db-exporters"':
        text = text.replace(f'job_name: "{JOB}"', snippet)
    elif snippet:
        text = text.replace("role: endpointslice", snippet)
    else:
        text += _EXTRA[label][0]
    errors = gate.tenant_scrape_contract(gate.parse_scrape_jobs(text))
    assert any(e.startswith(code) for e in errors), (label, errors)
