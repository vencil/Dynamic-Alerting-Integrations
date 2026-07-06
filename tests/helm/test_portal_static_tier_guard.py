"""test_portal_static_tier_guard.py — tier1 靜態 portal 須渲染出可開機的 nginx config

values-tier1.yaml 依 GHSA-3g2h-rf85-5rrv 開放代理防護（networkpolicy.yaml
render-time guard）要求 ``portal.tenantApiUrl`` / ``portal.recipePreviewUrl``
皆為空字串。configmap-nginx.yaml 的兩個 reverse-proxy location 區塊因此必須
「URL 非空才渲染」：空值內插會產出無 scheme 的 ``proxy_pass /api/v1/;``，
``nginx -t`` 直接拒絕（[emerg] invalid URL prefix）→ 靜態 portal pod 啟動即
crashloop，tier1 profile 實際上不可部署（pre-existing，於 #962 PR review 揪出）。

三層守法：
  * tier1 render → 完全不含 proxy_pass / 兩個 proxy location；
  * default / tier2 render → 兩個 proxy 區塊俱在（條件化不可誤吞正常路徑）；
  * 任何 render 出的 proxy_pass target 必須帶 http(s):// scheme（通用防線，
    防未來新 upstream 值重蹈空字串內插）。

模板層（無 helm CLI 也會跑）的對應守門見
tests/shared/test_helm_portal.py::TestNginxConfigmapHasProxy::test_nginx_proxy_blocks_are_conditional。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")

_PROXY_PASS_RE = re.compile(r"proxy_pass\s+(\S+?);")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _render(chart_dir: Path, values_file: Path | None = None) -> list[dict]:
    cmd = ["helm", "template", "t", str(chart_dir), "-n", "monitoring"]
    if values_file is not None:
        cmd += ["-f", str(values_file)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _nginx_conf(manifests: list[dict]) -> str:
    """Return the rendered default.conf of the da-portal nginx ConfigMap."""
    for doc in manifests:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("nginx-config"):
            return doc["data"]["default.conf"]
    raise AssertionError("da-portal nginx-config ConfigMap not found in render output")


@_needs_helm
def test_tier1_static_render_has_no_proxy_locations(repo_root: Path) -> None:
    """tier1（兩個 upstream URL 皆空）→ 渲染出的 nginx conf 不得含任何 proxy 設定。"""
    chart = repo_root / "helm/da-portal"
    conf = _nginx_conf(_render(chart, values_file=chart / "values-tier1.yaml"))

    assert "proxy_pass" not in conf, (
        "tier1 static-only render 不應含 proxy_pass —— 空 URL 內插會產出無 scheme 的 "
        "`proxy_pass /api/v1/;`，nginx -t fail → pod crashloop"
    )
    assert "location /api/v1/" not in conf, "tier1 render 不應含 /api/v1/ location 區塊"
    assert "location = /preview" not in conf, "tier1 render 不應含 /preview location 區塊"
    # 靜態服務主體必須仍在（條件化不可誤傷 static / healthz 路徑）
    assert "location / {" in conf
    assert "location = /healthz" in conf


@_needs_helm
@pytest.mark.parametrize("values_file", [None, "values-tier2.yaml"])
def test_full_render_keeps_both_proxy_locations(repo_root: Path, values_file: str | None) -> None:
    """default / tier2（URL 非空）→ 兩個 proxy 區塊俱在且指向 http(s) upstream。"""
    chart = repo_root / "helm/da-portal"
    conf = _nginx_conf(_render(chart, values_file=chart / values_file if values_file else None))

    assert "location /api/v1/" in conf
    assert "location = /preview" in conf
    targets = _PROXY_PASS_RE.findall(conf)
    assert len(targets) == 2, f"應恰有 2 個 proxy_pass，得到 {targets}"


@_needs_helm
@pytest.mark.parametrize("values_file", [None, "values-tier1.yaml", "values-tier2.yaml"])
def test_rendered_proxy_pass_targets_have_scheme(repo_root: Path, values_file: str | None) -> None:
    """通用防線：任何渲染出的 proxy_pass target 必須帶 http(s):// scheme。

    無 scheme 的 proxy_pass（多半來自空值/錯值內插）通不過 ``nginx -t``。
    """
    chart = repo_root / "helm/da-portal"
    conf = _nginx_conf(_render(chart, values_file=chart / values_file if values_file else None))

    for target in _PROXY_PASS_RE.findall(conf):
        assert re.match(r"^https?://", target), (
            f"proxy_pass target {target!r} 缺 http(s):// scheme —— nginx -t 會以 "
            f"[emerg] invalid URL prefix 拒絕此設定"
        )
