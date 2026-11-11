"""test_portal_static_tier_guard.py — da-portal chart 只剩純靜態部署（ADR-027 §5）

需要登入的 portal 在 tenant-api 的 pod 裡；da-portal chart 只服務靜態檔：

  * render 出的 pod 只有一個 nginx 容器，沒有 oauth2-proxy；沒有 Ingress、
    沒有 Secret；NetworkPolicy 不放行任何 egress；
  * render 出的 nginx 設定沒有任何轉送指令、不碰身分 header（§9 第 3 項
    「portal 容器實際載入的 nginx 設定沒有任何轉送」——image 那份見
    tests/shared/test_portal_image_nginx_conf.py）；
  * simulate 路徑仍固定回 501（#2125）；
  * 仍設著已刪除 key 的舊 values 檔讓 render 失敗，而不是被默默忽略（§6）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required

_REPO = Path(__file__).resolve().parents[2]
_CHART = _REPO / "helm" / "da-portal"
_HAS_HELM = shutil.which("helm") is not None
_REQUIRE_HELM = helm_required()

#: The value sets every render-time assertion runs against.
VALUE_SETS = (None, "ci/pss-all-on-values.yaml", "ci/pss-custom-data-emptydir-values.yaml")

#: nginx directives that hand a request (or a header) to somewhere else.
FORWARDING_DIRECTIVES = ("proxy_pass", "fastcgi_pass", "uwsgi_pass", "grpc_pass",
                         "scgi_pass", "proxy_set_header", "js_import", "js_set")

#: Keys the static-only chart removed, the `--set` that sets one, and the name
#: the schema refusal carries.
REMOVED_SETS = (
    ("oauth2Proxy.enabled=true", "oauth2Proxy"),
    ("ingress.enabled=true", "ingress"),
    ("portal.tenantApiUrl=http://tenant-api.tenant-api.svc:8080", "tenantApiUrl"),
    ("portal.recipePreviewUrl=http://recipe-preview.monitoring.svc:8082", "recipePreviewUrl"),
    ("portal.relayToken.enabled=false", "relayToken"),
    ("service.internalPort=8080", "internalPort"),
    ("networkPolicy.egress.enabled=true", "egress"),
    ("resources.nginx.requests.cpu=10m", "nginx"),
)


def _helm_or_skip() -> None:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail("helm is required in this environment but not on PATH")
        pytest.skip("helm CLI not on PATH")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "t", str(_CHART), "-n", "monitoring", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def _render(values_file: str | None) -> list[dict]:
    res = _run(*(["-f", str(_CHART / values_file)] if values_file else []))
    assert res.returncode == 0, res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if d]


def _nginx_conf(manifests: list[dict]) -> str:
    for doc in manifests:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("nginx-config"):
            return doc["data"]["default.conf"]
    raise AssertionError("da-portal nginx-config ConfigMap not found in render output")


def _code(conf: str) -> str:
    """The config without comments (descriptions must not trip the check)."""
    return "\n".join(line.split("#", 1)[0] for line in conf.splitlines())


@pytest.mark.parametrize("values_file", VALUE_SETS, ids=lambda v: v or "default")
def test_rendered_nginx_conf_forwards_nothing(values_file: str | None) -> None:
    _helm_or_skip()
    code = _code(_nginx_conf(_render(values_file)))
    assert "location / {" in code  # vacuity guard: this is the server block
    for directive in FORWARDING_DIRECTIVES:
        assert directive not in code, directive
    assert "x-forwarded-" not in code.lower()
    assert "x_forwarded_" not in code.lower()


@pytest.mark.parametrize("values_file", VALUE_SETS, ids=lambda v: v or "default")
def test_pod_is_one_nginx_container_and_nothing_else_ships(values_file: str | None) -> None:
    _helm_or_skip()
    manifests = _render(values_file)
    kinds = sorted(d["kind"] for d in manifests)
    assert "Ingress" not in kinds and "Secret" not in kinds, kinds
    deploy = next(d for d in manifests if d["kind"] == "Deployment")
    pod = deploy["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["nginx"]
    assert not pod.get("initContainers")
    # No projected ServiceAccount token: nothing here talks to tenant-api.
    assert all("projected" not in v for v in pod["volumes"]), pod["volumes"]


def test_networkpolicy_allows_no_egress() -> None:
    _helm_or_skip()
    netpol = next(d for d in _render(None) if d["kind"] == "NetworkPolicy")
    assert "Egress" in netpol["spec"]["policyTypes"]
    assert netpol["spec"]["egress"] == []


@pytest.mark.parametrize("values_file", VALUE_SETS, ids=lambda v: v or "default")
def test_simulate_not_provided_location_renders(values_file: str | None) -> None:
    """#2125：simulate 固定回 501；code 與 widget 的 drift 斷言見
    tests/shared/test_portal_simulate_not_provided.py。"""
    _helm_or_skip()
    conf = _nginx_conf(_render(values_file))
    # 區塊以獨立一行的 `}` 收尾（body 是 JSON，內含 `}`）。
    m = re.search(r"location = /api/v1/tenants/simulate \{(.*?)\n\s*\}", conf, re.S)
    assert m is not None, "render 缺 simulate not-provided location"
    ret = re.search(r"return 501 '(\{.*\})';", m.group(1))
    assert ret is not None, f"simulate location 應回 501，實得 {m.group(1)!r}"
    assert json.loads(ret.group(1))["code"] == "SIMULATE_NOT_PROVIDED"


@pytest.mark.parametrize("set_arg,name", REMOVED_SETS, ids=[s for s, _ in REMOVED_SETS])
def test_a_removed_key_fails_the_render(set_arg: str, name: str) -> None:
    _helm_or_skip()
    res = _run("--set", set_arg)
    assert res.returncode != 0, f"--set {set_arg} rendered"
    assert name in res.stderr, res.stderr


def test_values_yaml_carries_no_removed_key() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "oauth2Proxy" not in values and "ingress" not in values
    assert sorted(values["portal"]) == ["listenPort"]
    assert sorted(values["service"]) == ["port", "type"]
    assert sorted(values["networkPolicy"]) == ["allowedNamespaces", "enabled"]
    # The tier overlays are gone: there is one profile.
    assert sorted(p.name for p in _CHART.glob("values*")) == ["values.schema.json", "values.yaml"]


# ── What the rendered server actually serves (CodeRabbit on #3744) ──────────
#
# Checked once against a real nginx serving a copy of the image's tree: before
# these fixes `/` answered 404/403 (no index.html at the document root), a
# customData platform-data.json was never served (mounted at /custom, read
# from /assets), and every static response lost the security headers (an
# add_header in a nested location drops the server-level ones).

_DOCKERFILE = _REPO / "components" / "da-portal" / "Dockerfile"
_DOCROOT = "/usr/share/nginx/html"


def _image_has(path: str) -> bool:
    """True when the da-portal image ships <docroot><path>: a Dockerfile COPY
    whose destination covers it, from a source that exists in the repo."""
    want = _DOCROOT + path
    for m in re.finditer(r"^COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)\s*$",
                         _DOCKERFILE.read_text(encoding="utf-8"), re.M):
        src, dst = m.groups()
        if dst.endswith("/") and want.startswith(dst):
            if (_REPO / src / want[len(dst):]).is_file():
                return True
        elif want == dst and (_REPO / src).is_file():
            return True
    return False


def _location_root_block(code: str) -> str:
    start = re.search(r"location\s+/\s*\{", code)
    assert start, "no `location /` block"
    depth, i = 1, start.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        i += 1
    return code[start.end():i - 1]


@pytest.mark.parametrize("values_file", VALUE_SETS)
def test_the_entry_page_and_spa_fallback_are_files_the_image_ships(values_file: str | None) -> None:
    _helm_or_skip()
    code = _code(_nginx_conf(_render(values_file)))
    block = _location_root_block(code)
    index = re.search(r"^\s*index\s+(\S+);", block, re.M)
    fallback = re.search(r"^\s*try_files\s+\$uri\s+\$uri/\s+(\S+);", block, re.M)
    assert index and fallback, block
    assert _image_has("/" + index.group(1)), f"index {index.group(1)} is not in the image"
    assert _image_has(fallback.group(1)), f"SPA fallback {fallback.group(1)} is not in the image"


@pytest.mark.parametrize("values_file", VALUE_SETS)
def test_no_location_drops_the_security_headers(values_file: str | None) -> None:
    """An add_header inside a location replaces every server-level add_header
    for that response, so all of them live at server level."""
    _helm_or_skip()
    code = _code(_nginx_conf(_render(values_file)))
    depth, bad = 0, []
    for n, line in enumerate(code.splitlines(), 1):
        if depth >= 2 and re.match(r"\s*add_header\b", line):
            bad.append(f"line {n}: {line.strip()}")
        depth += line.count("{") - line.count("}")
    assert re.search(r"^\s*add_header\s+X-Content-Type-Options", code, re.M), "no security headers at all"
    assert not bad, "add_header inside a location:\n" + "\n".join(bad)


def test_custom_data_replaces_the_files_the_tools_read() -> None:
    _helm_or_skip()
    block = _location_root_block(_code(_nginx_conf(_render("ci/pss-custom-data-emptydir-values.yaml"))))
    regexes = re.findall(r"location\s+~\*?\s+(\S+)\s*\{([^}]*)\}", block)
    assert regexes, block
    first, body = regexes[0]
    # First among the regex locations, or an earlier one (\.json$) answers
    # /assets/platform-data.json from the image instead.
    assert first.startswith("^/assets/"), f"custom-data override is not first: {first}"
    assert re.search(r"try_files\s+/custom/\$1\s+\$uri\s+=404;", body), body
    assert re.fullmatch(first, "/assets/platform-data.json") or re.match(first, "/assets/platform-data.json")
    assert not re.search(r"/custom/", _code(_nginx_conf(_render(None)))), "override without customData"
