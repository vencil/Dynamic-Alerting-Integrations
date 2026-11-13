"""ADR-027 §4 / §9 item 5: threshold-govern's write path stays removed.

The only machine caller tenant-api ever had was threshold-govern's ``--apply``
(a weekly CronJob that opened PRs through tenant-api's port 8080). ADR-027 §4
removed it: the CronJob, its NetworkPolicy allowances and its staleness alert.
These checks read the manifests the way their consumers do (YAML documents,
not substrings), so a re-added CronJob, allowance or alert turns this red.
"""
from __future__ import annotations

import yaml

from _tree import REPO_ROOT, repo_files

_NAME = "threshold-govern"
_LABEL = ("component", "threshold-govern")
_ALERT = "ThresholdGovernanceStale"


def _docs(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if isinstance(d, dict)]


def _k8s_docs():
    files = [p for p in repo_files(".yaml", ".yml")
             if p.relative_to(REPO_ROOT).parts[0] == "k8s"]
    assert files, "no k8s/ manifests enumerated — the scan would pass on nothing"
    return [(p, d) for p in files for d in _docs(p)]


def _selectors(node):
    """Every matchLabels mapping anywhere under *node*."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "matchLabels" and isinstance(v, dict):
                yield v
            else:
                yield from _selectors(v)
    elif isinstance(node, list):
        for v in node:
            yield from _selectors(v)


def _alert_names(rule_doc):
    for group in (rule_doc.get("groups") or []):
        for rule in (group.get("rules") or []):
            if isinstance(rule, dict) and "alert" in rule:
                yield rule["alert"]


# adr-check: ADR-027/9.5c
def test_no_threshold_govern_cronjob():
    found = [str(p.relative_to(REPO_ROOT)) for p, d in _k8s_docs()
             if d.get("kind") == "CronJob"
             and (d.get("metadata") or {}).get("name") == _NAME]
    assert found == [], found


# adr-check: ADR-027/9.5c
def test_no_network_policy_admits_the_threshold_govern_pod():
    docs = _k8s_docs()
    policies = [(p, d) for p, d in docs if d.get("kind") == "NetworkPolicy"]
    assert policies, "no NetworkPolicy parsed under k8s/ — the scan would pass on nothing"
    found = [(str(p.relative_to(REPO_ROOT)), d["metadata"]["name"]) for p, d in policies
             if any(sel.get(_LABEL[0]) == _LABEL[1] for sel in _selectors(d))]
    assert found == [], found


# adr-check: ADR-027/9.5c
def test_tenant_api_chart_has_no_threshold_govern_allowance():
    values = yaml.safe_load((REPO_ROOT / "helm" / "tenant-api" / "values.yaml")
                            .read_text(encoding="utf-8"))
    allow = values["networkPolicy"]["internalPortAllow"]
    assert allow, "internalPortAllow is empty — the check would pass on nothing"
    assert "thresholdGovern" not in allow
    assert dict([_LABEL]) not in allow.values(), allow


# adr-check: ADR-027/9.5c
def test_no_threshold_governance_stale_alert():
    packs = [p for p in repo_files(".yaml")
             if p.relative_to(REPO_ROOT).parts[0] == "rule-packs"
             and p.name.startswith("rule-pack-")]
    assert packs, "no rule packs found"
    names = {n for p in packs for d in _docs(p) for n in _alert_names(d)}
    assert "CronJobLastRunFailed" in names, "positive control: the platform pack was not read"
    assert _ALERT not in names
    # …nor in the generated copy Prometheus mounts.
    [cm] = _docs(REPO_ROOT / "k8s" / "03-monitoring" / "configmap-rules-platform.yaml")
    embedded = {n for text in (cm.get("data") or {}).values()
                for d in yaml.safe_load_all(text) if isinstance(d, dict)
                for n in _alert_names(d)}
    assert "CronJobLastRunFailed" in embedded, "positive control: the ConfigMap was not read"
    assert _ALERT not in embedded


# adr-check: ADR-027/9.5c
def test_no_chart_mentions_threshold_govern():
    """The scans above read k8s/ as YAML; chart templates are Go templates, so
    a CronJob or alert re-added to a chart would not parse there. Read every
    file under helm/ as text instead: none names the CronJob or its alert.
    Broader than a parse on purpose — no chart has a reason to mention it."""
    files = [p for p in repo_files() if p.relative_to(REPO_ROOT).parts[0] == "helm"]
    assert files, "no helm/ files enumerated — the scan would pass on nothing"
    found = [str(p.relative_to(REPO_ROOT)) for p in files
             if _NAME in (t := p.read_text(encoding="utf-8", errors="replace")) or _ALERT in t]
    assert found == [], found
