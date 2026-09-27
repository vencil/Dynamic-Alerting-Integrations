"""test_portal_simulate_not_provided.py — portal 部署明確宣告「不提供 simulate」（#2125）

``POST /api/v1/tenants/simulate`` 由 threshold-exporter 提供，出貨的 portal
部署（image 內建 ``components/da-portal/nginx.conf`` 與 Helm
``helm/da-portal/templates/configmap-nginx.yaml``）都不代理它。兩份設定因此以
exact-match ``location = /api/v1/tenants/simulate`` 回固定 501 JSON，
simulate-preview widget 只認「501 + 這個 ``code``」並顯示「此部署不提供」。

本檔守三件事：
  * 兩份 nginx 設定都有這個 location、回 501、body 是 JSON 且 ``code`` 與
    widget 讀的常數**同一個字串**（drift 斷言：常數從 JSX 原始碼抽出，不在
    這裡另寫一份）；
  * location 內不得有 ``add_header``（nginx 規則：location 只要有任一
    ``add_header``，server 層的安全標頭就整批不繼承），Content-Type 由
    ``default_type application/json`` 提供；
  * Helm 版本不在任何 ``{{ if }}`` 之內 —— Tier-1（無 tenant-api upstream）
    與 Tier-2 都要渲染。實際 ``helm template`` 的 render 層斷言見
    tests/helm/test_portal_static_tier_guard.py。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WIDGET = REPO_ROOT / "tools/portal/src/interactive/tools/simulate-preview.jsx"
IMAGE_CONF = REPO_ROOT / "components/da-portal/nginx.conf"
HELM_CONF = REPO_ROOT / "helm/da-portal/templates/configmap-nginx.yaml"

# 區塊以「獨立一行的 `}`」收尾 —— body 是 JSON，內含 `}`，不能用 [^}]* 切。
_LOCATION_RE = re.compile(r"location\s*=\s*/api/v1/tenants/simulate\s*\{(.*?)\n\s*\}", re.S)
_RETURN_RE = re.compile(r"return\s+(\d{3})\s+'([^']*)'\s*;")
_WIDGET_CODE_RE = re.compile(r"^const SIMULATE_NOT_PROVIDED_CODE = '([^']+)';$", re.M)

CONFS = {
    "image-nginx.conf": IMAGE_CONF,
    "helm-configmap-nginx.yaml": HELM_CONF,
}


def _widget_code() -> str:
    matches = _WIDGET_CODE_RE.findall(WIDGET.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"{WIDGET.relative_to(REPO_ROOT)} 應恰有一個 SIMULATE_NOT_PROVIDED_CODE 常數，得到 {matches}"
    )
    return matches[0]


def _location_block(path: Path) -> str:
    blocks = _LOCATION_RE.findall(path.read_text(encoding="utf-8"))
    assert len(blocks) == 1, (
        f"{path.relative_to(REPO_ROOT)} 應恰有一個 `location = /api/v1/tenants/simulate`，"
        f"得到 {len(blocks)} 個 —— 少了它，POST 會落到 /api/v1/ prefix 轉給 tenant-api（405）"
    )
    return blocks[0]


@pytest.mark.parametrize("name", sorted(CONFS))
def test_simulate_location_returns_501_with_widget_code(name: str) -> None:
    block = _location_block(CONFS[name])
    returns = _RETURN_RE.findall(block)
    assert len(returns) == 1, f"{name}: simulate location 應恰有一個 `return <code> '<body>';`，得到 {returns}"
    status, body = returns[0]
    assert status == "501", f"{name}: simulate location 應回 501，得到 {status}"
    payload = json.loads(body)
    assert payload.get("code") == _widget_code(), (
        f"{name}: 501 body 的 code {payload.get('code')!r} 與 widget 常數 {_widget_code()!r} 不一致"
        " —— widget 只認完全相同的 code，不一致就會退回通用錯誤"
    )
    assert isinstance(payload.get("error"), str) and payload["error"], f"{name}: body 應帶非空 error 說明"
    assert "proxy_pass" not in block, f"{name}: simulate location 不得代理到任何 upstream"


@pytest.mark.parametrize("name", sorted(CONFS))
def test_simulate_location_keeps_server_security_headers(name: str) -> None:
    block = _location_block(CONFS[name])
    assert re.search(r"default_type\s+application/json\s*;", block), (
        f"{name}: simulate location 應以 default_type application/json 設 Content-Type"
    )
    assert "add_header" not in block, (
        f"{name}: simulate location 內不得有 add_header —— 否則 server 層的安全標頭"
        "（CSP / HSTS / X-Frame-Options …）在這個回應上整批消失"
    )


def test_helm_simulate_location_is_unconditional() -> None:
    """Helm 版本不得包在任何 `{{ if }}` / `{{ with }}` / `{{ range }}` 之內。"""
    text = HELM_CONF.read_text(encoding="utf-8")
    loc = _LOCATION_RE.search(text)
    assert loc is not None
    depth = 0
    for m in re.finditer(r"\{\{-?\s*(if|with|range|end)\b", text[: loc.start()]):
        depth += -1 if m.group(1) == "end" else 1
    assert depth == 0, (
        "configmap-nginx.yaml 的 simulate location 位於條件區塊內（巢狀深度 "
        f"{depth}）—— Tier-1 / Tier-2 都必須渲染它"
    )
