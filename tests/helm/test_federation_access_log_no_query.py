"""test_federation_access_log_no_query.py — the gateway access log must not record query strings (#2469)

``jwt_authn`` reads the token from the ``Authorization`` header only, so a client
that puts ``?access_token=`` in the URL is rejected with 401. The access log
still runs for that request: if it records ``%REQ(:PATH)%`` the token lands in
both sinks (stdout and the audit-metrics mirror). The path field therefore uses
``%REQ_WITHOUT_QUERY(:PATH)%`` — an extension formatter that Envoy refuses to
load unless the same ``log_format`` declares ``envoy.formatter.req_without_query``.

Two layers (mirrors test_victorialogs_gateway_guard.py): a static layer that
needs no helm, and a render layer gated on the helm CLI that checks every
access logger in every mode.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/federation-gateway"
_ENVOY_FILE = "helm/federation-gateway/files/envoy.yaml"
_FORMATTER = "envoy.formatter.req_without_query"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── static layer (no helm) ───────────────────────────────────────────────────
def test_envoy_source_never_logs_the_full_path(repo_root: Path):
    txt = (repo_root / _ENVOY_FILE).read_text(encoding="utf-8")
    assert "%REQ(:PATH)" not in txt, "access log must not record :PATH with its query string"
    assert "%REQ_WITHOUT_QUERY(:PATH)" in txt, "access log path field must strip the query string"
    assert _FORMATTER in txt, f"REQ_WITHOUT_QUERY needs the {_FORMATTER} formatter declared"


# ── render layer (helm-gated) ────────────────────────────────────────────────
def _render(repo_root: Path, sets: dict[str, str]) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(repo_root / _CHART)]
    for k, v in sets.items():
        cmd += ["--set", f"{k}={v}"]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def _envoy_config(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            data = doc.get("data") or {}
            if "envoy.yaml" in data:
                return yaml.safe_load(data["envoy.yaml"])
    raise AssertionError("no ConfigMap carrying envoy.yaml in render output")


def _log_formats(node) -> list[dict]:
    """Every log_format of every access logger, anywhere in the parsed config."""
    out: list[dict] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "access_log" and isinstance(v, list):
                for logger in v:
                    fmt = ((logger or {}).get("typed_config") or {}).get("log_format")
                    if isinstance(fmt, dict):
                        out.append(fmt)
            else:
                out += _log_formats(v)
    elif isinstance(node, list):
        for v in node:
            out += _log_formats(v)
    return out


@_needs_helm
@pytest.mark.parametrize(
    "sets, n_loggers",
    [
        ({}, 2),
        ({"auditLog.enabled": "false"}, 1),
        ({"mode": "vm-cluster"}, 2),
        ({"mode": "victorialogs", "jwt.audience": "tenant-federation-logs"}, 2),
    ],
    ids=["prom-label-proxy", "audit-mirror-off", "vm-cluster", "victorialogs"],
)
def test_every_access_logger_strips_the_query_string(repo_root: Path, sets, n_loggers):
    res = _render(repo_root, sets)
    assert res.returncode == 0, f"render must succeed: {res.stderr}"
    formats = _log_formats(_envoy_config(res.stdout))
    assert len(formats) == n_loggers, f"expected {n_loggers} access loggers, got {len(formats)}"
    for fmt in formats:
        values = [str(v) for v in (fmt.get("json_format") or {}).values()]
        assert not any("%REQ(:PATH)" in v for v in values), f"a field logs the full path: {values}"
        assert any("%REQ_WITHOUT_QUERY(:PATH)" in v for v in values), f"no query-less path field: {values}"
        names = [f.get("name") for f in (fmt.get("formatters") or [])]
        assert _FORMATTER in names, f"log_format uses REQ_WITHOUT_QUERY without declaring it: {names}"
