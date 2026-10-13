"""test_numeric_env_render.py — helm/tenant-api numeric TA_* env vars (#2862)

Helm decodes a whole number from a values file as float64, and `quote` prints
a float64 of 1e6 or more in exponent form: `maxBodyBytes: 2097152` rendered as
TA_MAX_BODY_BYTES="2.097152e+06". tenant-api's integer parse rejects that,
warns and falls back to its default, so the configured value was silently
ignored. Pins, for every numeric env key the chart writes:

  * a value >= 1e6 from a values FILE (the float64 path) and from `--set`
    (the int64 path) renders as a decimal integer string;
  * a fractional number, or one outside the int64 range (#2927), fails the
    render instead of reaching the binary;
  * a string is passed through unchanged, so a malformed one still reaches the
    binary's own warn-and-default path (never coerced to 0 by the chart —
    for TA_WRITE_QUEUE_DEPTH, 0 means shed every queued write).

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/tenant-api"
_REPO_ROOT = Path(__file__).parent.parent.parent

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")

_DIRECT_ACK = ["--set", "writeBack.mode=direct",
               "--set", "writeBack.directAcknowledgeNotDelivered=true"]

# values path -> env var. Every numeric key the deployment template writes as
# an env value; a new one belongs here.
_NUMERIC_ENV = {
    "tenantApi.writeQueueDepth": "TA_WRITE_QUEUE_DEPTH",
    "tenantApi.server.maxBodyBytes": "TA_MAX_BODY_BYTES",
    "tenantApi.server.maxBatchBodyBytes": "TA_MAX_BATCH_BODY_BYTES",
    "tenantApi.server.maxTenantDocBytes": "TA_MAX_TENANT_DOC_BYTES",
}

_BIG = 2097152  # >= 1e6: the size at which a float64 prints in exponent form


def _render(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "t", str(_REPO_ROOT / _CHART), *_DIRECT_ACK, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _env(stdout: str) -> dict[str, str]:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "Deployment":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "tenant-api":
                    return {e["name"]: e.get("value") for e in c.get("env", [])}
    raise AssertionError("no tenant-api container in the render")


def _values_file(tmp_path: Path, path: str, value) -> Path:
    tree: dict = {}
    node = tree
    *parents, leaf = path.split(".")
    for p in parents:
        node = node.setdefault(p, {})
    node[leaf] = value
    vf = tmp_path / "v.yaml"
    vf.write_text(yaml.safe_dump(tree), encoding="utf-8")
    return vf


@_needs_helm
@pytest.mark.parametrize("path,env", sorted(_NUMERIC_ENV.items()))
def test_large_value_renders_as_decimal_integer(tmp_path: Path, path: str, env: str):
    vf = _values_file(tmp_path, path, _BIG)
    for args in (("-f", str(vf)), ("--set", f"{path}={_BIG}")):
        res = _render(*args)
        assert res.returncode == 0, res.stderr
        got = _env(res.stdout).get(env)
        assert got == str(_BIG), f"{env} via {args[0]}: {got!r}"
        assert re.fullmatch(r"[0-9]+", got)


@_needs_helm
@pytest.mark.parametrize("path", sorted(_NUMERIC_ENV))
def test_fractional_value_fails_the_render(tmp_path: Path, path: str):
    res = _render("-f", str(_values_file(tmp_path, path, 1.5)))
    assert res.returncode != 0
    assert path in res.stderr


@_needs_helm
@pytest.mark.parametrize("path", sorted(_NUMERIC_ENV))
def test_out_of_range_value_fails_the_render(tmp_path: Path, path: str):
    # int64 wraps 1e19 to an integer unrelated to the input, which the binary
    # accepts without a warning (#2927).
    res = _render("-f", str(_values_file(tmp_path, path, 1e19)))
    assert res.returncode != 0, _env(res.stdout)
    assert path in res.stderr


@_needs_helm
@pytest.mark.parametrize("path,env", sorted(_NUMERIC_ENV.items()))
def test_string_value_is_passed_through(tmp_path: Path, path: str, env: str):
    res = _render("-f", str(_values_file(tmp_path, path, "not-a-number")))
    assert res.returncode == 0, res.stderr
    assert _env(res.stdout).get(env) == "not-a-number"


@_needs_helm
def test_default_render_keeps_its_values():
    res = _render()
    assert res.returncode == 0, res.stderr
    env = _env(res.stdout)
    assert env["TA_MAX_BODY_BYTES"] == "1048576"
    assert env["TA_MAX_BATCH_BODY_BYTES"] == "262144"
    assert env["TA_MAX_TENANT_DOC_BYTES"] == "65536"
    assert "TA_WRITE_QUEUE_DEPTH" not in env, "empty-string default must emit nothing"
