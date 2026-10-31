"""test_threshold_exporter_namespace_render.py — #1425 who owns threshold-config, and where

Two defects, one chart:

  1. Every resource template wrote `namespace: monitoring` literally, so
     `helm install -n obs` (and the `--deploy helm` pipeline `init` generates,
     which passes `-n ${MONITORING_NS}`) put the Deployment, Service, PDB and
     ConfigMap in `monitoring` while the release record sat in `obs`.
  2. `da-tools init --deploy kustomize` builds a ConfigMap named
     `threshold-config` from conf.d/, and the chart that supplies the exporter
     renders its own ConfigMap of the same name from its values — two writers,
     each apply overwriting the other. `thresholdConfig.existingConfigMap` is
     the single switch that says the ConfigMap belongs to someone else.

Static checks run everywhere; render checks need helm and, like
test_optional_overrides_render.py, carry their own fail-closed guard so a job
that selects only this file still reddens when helm is missing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART = _REPO_ROOT / "helm" / "threshold-exporter"
_DEFAULT_CM = "threshold-config"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
_REQUIRE_HELM = helm_required()


def test_helm_present_when_required() -> None:
    """Fail-closed guard against silent disarmament."""
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            f"{_REQUIRE_HELM}: but no `helm` on PATH — every render assertion "
            "in this file would have skipped silently"
        )


def _render(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "t", str(_CHART), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def _docs(*args: str) -> list[dict]:
    proc = _render(*args)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _config_volume(docs: list[dict]) -> str:
    dep = [d for d in docs if d["kind"] == "Deployment"]
    assert len(dep) == 1, [d["kind"] for d in docs]
    vols = {v["name"]: v for v in dep[0]["spec"]["template"]["spec"]["volumes"]}
    return vols["config"]["configMap"]["name"]


# ── static layer ────────────────────────────────────────────────────────────

def test_no_template_pins_a_namespace_of_its_own():
    """Every `metadata.namespace` in the chart is the release namespace."""
    seen = {}
    for tpl in sorted((_CHART / "templates").glob("*.yaml")):
        for line in tpl.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s{2}namespace:", line):
                seen[tpl.name] = line.split(":", 1)[1].strip()
    assert {"configmap.yaml", "deployment.yaml", "service.yaml", "pdb.yaml",
            "servicemonitor.yaml"} <= set(seen), seen
    bad = {k: v for k, v in seen.items() if v != "{{ .Release.Namespace }}"}
    assert not bad, f"chart templates pin a namespace of their own: {bad}"


def test_existing_configmap_is_declared_empty_in_values():
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["thresholdConfig"]["existingConfigMap"] == ""


# ── render layer ────────────────────────────────────────────────────────────

@_needs_helm
@pytest.mark.parametrize("mode", ["configmap", "operator"])
def test_every_resource_lands_in_the_release_namespace(mode):
    docs = _docs("-n", "obs", "--set", f"rules.mode={mode}")
    kinds = {d["kind"] for d in docs}
    assert {"Deployment", "Service", "PodDisruptionBudget", "ConfigMap"} <= kinds
    if mode == "operator":
        assert "ServiceMonitor" in kinds
    wrong = {(d["kind"], d["metadata"].get("namespace")) for d in docs
             if d["metadata"].get("namespace") != "obs"}
    assert not wrong, wrong


@_needs_helm
def test_default_renders_exactly_one_threshold_config_and_mounts_it():
    """Control: unset switch = the chart owns the ConfigMap, as before."""
    docs = _docs("-n", "obs")
    cms = [d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap"]
    assert cms == [_DEFAULT_CM]
    assert _config_volume(docs) == _DEFAULT_CM


@_needs_helm
@pytest.mark.parametrize("mode", ["configmap", "operator"])
def test_existing_configmap_renders_none_and_mounts_the_named_one(mode):
    docs = _docs("-n", "obs", "--set", f"rules.mode={mode}",
                 "--set", "thresholdConfig.existingConfigMap=foo")
    assert not [d for d in docs if d["kind"] == "ConfigMap"]
    assert _config_volume(docs) == "foo"


@_needs_helm
def test_existing_configmap_named_like_the_default_still_renders_none():
    """The kustomize case: same NAME, different owner."""
    docs = _docs("--set", f"thresholdConfig.existingConfigMap={_DEFAULT_CM}")
    assert not [d for d in docs if d["kind"] == "ConfigMap"]
    assert _config_volume(docs) == _DEFAULT_CM


@_needs_helm
def test_existing_configmap_name_that_reads_as_a_boolean_stays_a_string():
    docs = _docs("--set-string", "thresholdConfig.existingConfigMap=true")
    assert _config_volume(docs) == "true"


@_needs_helm
def test_non_string_existing_configmap_is_refused():
    proc = _render("--set", "thresholdConfig.existingConfigMap=true")
    assert proc.returncode != 0
    assert "existingConfigMap must be a ConfigMap NAME" in proc.stderr


# ── the exporter's NetworkPolicy follows the release (#1425) ──────────────
# It used to be two raw manifests pinned to `monitoring`
# (k8s/03-monitoring/network-policies.yaml): an exporter installed anywhere else
# had no ingress isolation at all, and nothing said so.

def _policies(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}


def test_the_raw_manifests_no_longer_pin_the_exporters_policies():
    """One owner: the chart. A raw copy in `monitoring` would be a second."""
    raw = (_REPO_ROOT / "k8s" / "03-monitoring" / "network-policies.yaml"
           ).read_text(encoding="utf-8")
    docs = [d for d in yaml.safe_load_all(raw) if d]
    selectors = [((d.get("spec") or {}).get("podSelector") or {}).get("matchLabels")
                 for d in docs]
    assert {"app": "threshold-exporter"} not in selectors, (
        "a raw NetworkPolicy still selects the exporter pods")


_INGRESS_PORT = [{"protocol": "TCP", "port": "http"}]
_REPO_OVERLAYS = ["local", "ci"]


@_needs_helm
def test_by_default_the_policy_restricts_the_port_not_the_source():
    """allowExternal defaults to true: the chart cannot know the scraper's pod
    labels (kube-prometheus-stack's are `app.kubernetes.io/name: prometheus`,
    not `app: prometheus`), and a guessed `from` silently drops its scrape."""
    pols = _policies(_docs("-n", "obs"))
    assert set(pols) == {"threshold-exporter-ingress", "threshold-exporter-egress"}
    assert {p["metadata"]["namespace"] for p in pols.values()} == {"obs"}
    rule = pols["threshold-exporter-ingress"]["spec"]["ingress"]
    assert rule == [{"ports": _INGRESS_PORT}], rule
    egress = pols["threshold-exporter-egress"]["spec"]["egress"]
    assert [p["port"] for r in egress for p in r["ports"]] == [53, 53], egress


@_needs_helm
@pytest.mark.parametrize("env", _REPO_OVERLAYS)
def test_the_repo_overlays_admit_only_the_repo_prometheus(env):
    """The repo's own installs (`-f environments/<env>/...`) know their
    Prometheus — k8s/03-monitoring/deployment-prometheus.yaml — so they isolate."""
    overlay = _REPO_ROOT / "environments" / env / "threshold-exporter.yaml"
    pols = _policies(_docs("-n", "monitoring", "-f", str(overlay)))
    rule = pols["threshold-exporter-ingress"]["spec"]["ingress"]
    assert rule == [{
        "from": [{"namespaceSelector": {"matchLabels": {
                      "kubernetes.io/metadata.name": "monitoring"}},
                  "podSelector": {"matchLabels": {"app": "prometheus"}}}],
        "ports": _INGRESS_PORT,
    }], rule


def test_the_repo_overlays_name_the_repo_prometheus_labels():
    """The overlays' selector must match the Prometheus the repo deploys, or
    the repo's own exporter scrape is cut."""
    dep = yaml.safe_load((_REPO_ROOT / "k8s" / "03-monitoring" /
                          "deployment-prometheus.yaml").read_text(encoding="utf-8"))
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    for env in _REPO_OVERLAYS:
        np = yaml.safe_load((_REPO_ROOT / "environments" / env /
                             "threshold-exporter.yaml").read_text(encoding="utf-8")
                            )["networkPolicy"]
        assert dep["metadata"]["namespace"] == np["prometheusNamespace"], env
        assert np["prometheusPodLabels"].items() <= pod_labels.items(), env


@_needs_helm
def test_prometheus_location_is_a_value():
    pols = _policies(_docs("--set", "networkPolicy.allowExternal=false",
                           "--set", "networkPolicy.prometheusNamespace=obs-mon",
                           "--set", "networkPolicy.prometheusPodLabels.app=prom"))
    peer = pols["threshold-exporter-ingress"]["spec"]["ingress"][0]["from"][0]
    assert peer["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "obs-mon"}
    assert peer["podSelector"]["matchLabels"] == {"app": "prom"}


@_needs_helm
def test_an_empty_prometheus_selector_is_refused_when_isolating():
    """`matchLabels: {}` admits every pod in the namespace — refused, not widened."""
    proc = _render("--set", "networkPolicy.allowExternal=false",
                   "--set", "networkPolicy.prometheusNamespace=obs-mon")
    assert proc.returncode != 0
    assert "prometheusPodLabels must be a non-empty label selector" in proc.stderr


@_needs_helm
def test_an_empty_prometheus_namespace_is_refused_when_isolating():
    proc = _render("--set", "networkPolicy.allowExternal=false",
                   "--set", "networkPolicy.prometheusPodLabels.app=prom")
    assert proc.returncode != 0
    assert "prometheusNamespace must name the namespace" in proc.stderr


@_needs_helm
@pytest.mark.parametrize("value", ['"false"', '"no"', "0"])
def test_a_non_boolean_allow_external_is_refused(tmp_path, value):
    """A quoted "false" in a values file is a non-empty string, which Helm reads
    as true: the policy would silently admit every source. Refused instead."""
    values = tmp_path / "v.yaml"
    values.write_text(
        "networkPolicy:\n"
        f"  allowExternal: {value}\n"
        "  prometheusNamespace: monitoring\n"
        "  prometheusPodLabels: {app: prometheus}\n", encoding="utf-8")
    proc = _render("-f", str(values))
    assert proc.returncode != 0, f"allowExternal: {value} rendered"
    assert "networkPolicy.allowExternal must be true or false" in proc.stderr, proc.stderr


@_needs_helm
def test_disabled_renders_no_policy():
    assert not _policies(_docs("--set", "networkPolicy.enabled=false"))


@_needs_helm
def test_the_scrape_service_carries_the_platform_label():
    """Prometheus' `platform-components` keeps by this label in any namespace;
    tests/lint/test_check_scrape_reachability.py pins the job side."""
    svc = next(d for d in _docs("-n", "obs") if d["kind"] == "Service")
    assert svc["metadata"]["labels"]["dynamic-alerting.io/scrape"] == "platform"
