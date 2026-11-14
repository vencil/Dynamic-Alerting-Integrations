"""test_config_validation_cronjob.py — threshold-exporter chart (#3353)

The optional CronJob that runs `da-tools validate-config` on the exporter's
ConfigMap, so a tree changed without CI (direct mode) is judged too, and a
FAIL row fails the Job for CronJobLastRunFailed to report. Pins:

  * off by default; on, it judges the ConfigMap the exporter mounts — one
    name (_helpers.tpl), whatever thresholdConfig.existingConfigMap says;
  * its memory is the exporter's resources.limits.memory × memoryFactor
    (the owner ruling: one knob), and the render refuses a quantity it
    cannot multiply or a missing limit rather than guess;
  * nothing that selects exporter pods (Deployment, Service, PDB,
    NetworkPolicy) selects its pod;
  * the shapes CronJobLastRunFailed depends on: not suspended, no
    ttlSecondsAfterFinished (a deleted failed Job silences the rule), no
    retries of a deterministic FAIL.

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from _helm_gate import helm_required

_CHART = "helm/threshold-exporter"
_REPO = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and helm_required() is None, reason="helm CLI not on PATH")


def _render(*args: str) -> subprocess.CompletedProcess:
    assert shutil.which("helm"), f"helm is required here: {helm_required()}"
    return subprocess.run(["helm", "template", "t", str(_REPO / _CHART), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def _docs(*args: str) -> list[dict]:
    res = _render(*args)
    assert res.returncode == 0, res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if d]


def _one(docs: list[dict], kind: str) -> dict:
    (doc,) = [d for d in docs if d["kind"] == kind]
    return doc


_ON = ("--set", "configValidation.enabled=true")


def test_off_by_default():
    assert [d for d in _docs() if d["kind"] == "CronJob"] == []


@pytest.mark.parametrize("args", [(), ("--set", "thresholdConfig.existingConfigMap=my-conf")])
def test_it_judges_the_configmap_the_exporter_mounts(args):
    docs = _docs(*_ON, *args)
    dep = _one(docs, "Deployment")["spec"]["template"]["spec"]
    job = _one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    mount = {m["name"]: m["mountPath"] for m in dep["containers"][0]["volumeMounts"]}["config"]
    dep_cm = {v["name"]: v for v in dep["volumes"]}["config"]["configMap"]["name"]
    job_cm = {v["name"]: v for v in job["volumes"]}["config"]["configMap"]["name"]
    assert job_cm == dep_cm
    assert job["containers"][0]["args"] == ["validate-config", "--config-dir", mount]


@pytest.mark.parametrize("args,memory", [
    ((), "512Mi"),                                                       # 128Mi × 4
    (("--set", "resources.limits.memory=1Gi"), "4Gi"),
    (("--set", "resources.limits.memory=300M", "--set", "configValidation.memoryFactor=3"), "900M"),
    (("--set", "resources.limits.memory=268435456"), "1073741824"),
    (("--set", "resources.limits.memory=500k"), "2000k"),            # decimal kilo is lowercase
])
def test_memory_is_the_exporter_limit_times_the_factor(args, memory):
    c = _one(_docs(*_ON, *args), "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["memory"] == memory
    assert c["resources"]["requests"]["memory"] == memory


@pytest.mark.parametrize("args,needle", [
    (("--set", "resources.limits.memory=1.5Gi"), "integer with an optional"),
    (("--set", "resources.limits.memory=500K"), "integer with an optional"),  # not a Kubernetes suffix
    (("--set", "resources.limits=null"), "needs resources.limits.memory"),
    (("--set", "configValidation.memoryFactor=0"), "memoryFactor"),
    (("--set", "configValidation.memoryFactor=2.5"), "memoryFactor"),
])
def test_a_memory_it_cannot_derive_fails_the_render(args, needle):
    res = _render(*_ON, *args)
    assert res.returncode != 0 and needle in res.stderr, res.stderr


def _selects(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def test_nothing_that_selects_exporter_pods_selects_its_pod():
    docs = _docs(*_ON)
    pod_labels = _one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    exporter_labels = _one(docs, "Deployment")["spec"]["template"]["metadata"]["labels"]
    selectors = []
    for d in docs:
        spec = d.get("spec") or {}
        if d["kind"] in ("Deployment", "PodDisruptionBudget"):
            selectors.append(spec["selector"]["matchLabels"])
        elif d["kind"] == "Service":
            selectors.append(spec["selector"])
        elif d["kind"] == "NetworkPolicy":
            selectors.append(spec["podSelector"]["matchLabels"])
    assert selectors, "no exporter selector rendered: the check compares nothing"
    for sel in selectors:
        assert _selects(sel, exporter_labels), f"fixture: {sel} does not select the exporter"
        assert not _selects(sel, pod_labels), f"{sel} selects the validation Job's pod {pod_labels}"


def test_the_shapes_the_job_failure_alert_depends_on():
    cj = _one(_docs(*_ON), "CronJob")
    job = cj["spec"]["jobTemplate"]["spec"]
    assert cj["spec"]["suspend"] is False
    # A failed Job deleted at once (limit 0) or after a TTL takes its
    # kube_job_failed with it, and the rule never fires.
    assert cj["spec"]["failedJobsHistoryLimit"] >= 1
    assert "ttlSecondsAfterFinished" not in job
    assert job["backoffLimit"] == 0
    assert job["template"]["spec"]["restartPolicy"] == "Never"
