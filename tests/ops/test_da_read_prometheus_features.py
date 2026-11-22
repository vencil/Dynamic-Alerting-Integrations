"""da-read parses rules with the --enable-feature options the platform's
Prometheus runs with (#4151).

Prometheus builds its PromQL parser from ``--enable-feature``: an option such
as ``promql-experimental-functions`` changes which rule expressions load. da-read
reads rules offline (it is handed a customer's rule file, never the manifest
that runs the Prometheus), so it is built from options written once in Go —
``deployedFeatures``, ``components/da-read/prometheus_features.go`` — and this
test holds them to what every Prometheus the repo runs passes.

The Prometheus servers are found, not listed: every YAML file that names a
Prometheus image (``prom/prometheus`` or ``prometheus/prometheus``, with or
without a registry) or a Prometheus Operator ``kind: Prometheus`` is read as
YAML. Each mapping whose ``image`` is one — a string, or Helm's
``{repository, tag}`` — is a Prometheus whose ``command`` and ``args`` are
read; an Operator ``Prometheus`` contributes its ``spec.enableFeatures``. A
file that names one but cannot be read as YAML fails the test rather than
being skipped. The deployment manifest must be among those found, so a
discovery that finds nothing cannot pass.

How each option bears on what da-read reads is da-read's answer
(``da-read prometheus-features``), not a list kept here:

* each Prometheus's ``parser`` options must be exactly da-read's ``parser``
  list — every one, not their union: one Prometheus with an option and one
  without cannot both parse as da-read does;
* ``not-read`` options (storage, scraping, the UI, sample values) are fine;
* ``not-modelled`` and ``unknown`` options fail: da-read would judge rules
  differently from the Prometheus that runs them.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _tree import REPO_ROOT as _REPO, repo_files  # noqa: E402

_DEPLOYMENT = "k8s/03-monitoring/deployment-prometheus.yaml"
_IMAGE = re.compile(r"(?:^|[/\s\"'])(?:prom|prometheus)/prometheus(?:[:@\s\"']|$)", re.M)
_OPERATOR_KIND = re.compile(r"^kind:\s*Prometheus\s*$", re.M)


def _argv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    assert isinstance(value, list), f"command/args is {type(value).__name__}, not a list or a string"
    return [str(v) for v in value]


def _is_prometheus_image(image) -> bool:
    if isinstance(image, dict):  # Helm's {repository, tag}
        image = image.get("repository")
    return isinstance(image, str) and _IMAGE.search(image) is not None


def _prometheus_servers(node):
    """The --enable-feature values of each Prometheus under node, one list
    per server."""
    if isinstance(node, dict):
        if node.get("kind") == "Prometheus" and str(node.get("apiVersion", "")).startswith("monitoring.coreos.com/"):
            spec = node.get("spec") or {}
            yield [str(f) for f in spec.get("enableFeatures") or []]
        if _is_prometheus_image(node.get("image")):
            yield enable_feature_values(_argv(node.get("command")) + _argv(node.get("args")))
        for v in node.values():
            yield from _prometheus_servers(v)
    elif isinstance(node, list):
        for v in node:
            yield from _prometheus_servers(v)


def enable_feature_values(argv: list[str]) -> list[str]:
    """The --enable-feature values of one command line: ``--enable-feature=X``,
    ``--enable-feature X`` and repeats (kingpin's two spellings; the
    comma split is da-read's, as it is Prometheus')."""
    values = []
    for i, arg in enumerate(argv):
        flag, eq, value = arg.partition("=")
        if flag not in ("--enable-feature", "-enable-feature"):
            continue
        if not eq:
            assert i + 1 < len(argv), f"{arg} ends the command line without a value"
            value = argv[i + 1]
        values.append(value)
    return values


def deployed_prometheus_features() -> dict[str, list[list[str]]]:
    """Per file that runs a Prometheus, the --enable-feature values of each
    Prometheus in it."""
    found: dict[str, list[list[str]]] = {}
    unreadable = []
    for path in repo_files(".yaml", ".yml"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not (_IMAGE.search(text) or _OPERATOR_KIND.search(text)):
            continue
        rel = path.relative_to(_REPO).as_posix()
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            unreadable.append(f"{rel}: {exc}")
            continue
        for doc in docs:
            for values in _prometheus_servers(doc):
                found.setdefault(rel, []).append(values)
    assert not unreadable, (
        "files that name a prom/prometheus image but cannot be read as YAML, so their "
        "--enable-feature options are unmeasured:\n" + "\n".join(unreadable))
    return found


def test_enable_feature_spellings():
    assert enable_feature_values(
        ["--config.file=x", "--enable-feature=a,b", "--enable-feature", "c", "--enable-featurex=d"]
    ) == ["a,b", "c"]


@pytest.mark.parametrize("doc,servers", [
    ({"image": "docker.io/prom/prometheus:v3.16.0", "args": ["--enable-feature=a"]}, [["a"]]),
    ({"image": "quay.io/prometheus/prometheus:v3.16.0"}, [[]]),
    ({"image": {"repository": "prom/prometheus", "tag": "v3.16.0"}, "command": "--enable-feature b"}, [["b"]]),
    ({"apiVersion": "monitoring.coreos.com/v1", "kind": "Prometheus",
      "spec": {"enableFeatures": ["c"]}}, [["c"]]),
    ({"image": "prom/alertmanager:v0.34.2"}, []),
    ({"ref": "prom/prometheus:v3.16.0"}, []),
])
def test_server_spellings(doc, servers):
    assert list(_prometheus_servers(doc)) == servers


def test_discovery_finds_the_deployment():
    found = deployed_prometheus_features()
    assert _DEPLOYMENT in found, f"the deployed Prometheus was not found among {sorted(found)}"


def test_da_read_parses_with_the_deployed_features(da_read_binary):
    found = deployed_prometheus_features()
    values = [v for servers in found.values() for vs in servers for v in vs]
    unexpanded = [v for v in values if "$" in v]
    assert not unexpanded, f"--enable-feature values set from the environment cannot be read: {unexpanded}"
    proc = subprocess.run(
        [da_read_binary, "prometheus-features", "-"], input=json.dumps(values),
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    (doc,) = json.loads(proc.stdout)["documents"]
    bearing = doc["features"]
    wrong = {o: b for o, b in bearing.items() if b in ("not-modelled", "unknown")}
    assert not wrong, (
        f"a Prometheus the repo runs passes --enable-feature options da-read cannot follow: {wrong} "
        f"(files: {sorted(f for f, servers in found.items() if any(servers))})")
    drifted = {}
    for rel, servers in found.items():
        for i, vs in enumerate(servers):
            options = {o for v in vs for o in v.split(",")}
            parser = sorted(o for o in options if bearing[o] == "parser")
            if parser != doc["parser"]:
                drifted[f"{rel} #{i + 1}"] = parser
    assert not drifted, (
        f"these Prometheus servers parse rules with other options than da-read's {doc['parser']}: "
        f"{drifted}. Set deployedFeatures in components/da-read/prometheus_features.go to the "
        "deployed parser options, and pass the same ones to every Prometheus")


@pytest.mark.parametrize("servers,problem", [
    ([["type-and-unit-labels"]], "cannot follow"),
    ([["no-such-option"]], "cannot follow"),
    ([["promql-experimental-functions"]], "Set deployedFeatures"),
    # one Prometheus with the option, one without: the union would hide it
    ([["promql-experimental-functions"], []], "Set deployedFeatures"),
    ([["old-ui,promql-unquoted-utf8-names"], []], "Set deployedFeatures"),
])
def test_a_drifted_deployment_is_caught(da_read_binary, monkeypatch, servers, problem):
    monkeypatch.setattr(sys.modules[__name__], "deployed_prometheus_features",
                        lambda: {_DEPLOYMENT: servers})
    with pytest.raises(AssertionError, match=problem):
        test_da_read_parses_with_the_deployed_features(da_read_binary)
