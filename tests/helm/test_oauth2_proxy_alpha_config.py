"""test_oauth2_proxy_alpha_config.py — ADR-027 §2, §6, §9 items 3 and 6, §10.

The tenant-api chart runs oauth2-proxy with a structured config file
(--alpha-config) and hands the SAME file to tenant-api, which reads its
injectRequestHeaders to decide which identity headers it trusts. Pinned here:

  * oauth2-proxy and tenant-api mount the same ConfigMap at the same path,
    and that path is what --alpha-config and TA_OAUTH2_PROXY_CONFIG name;
  * the client secret is a file mounted into oauth2-proxy ONLY;
  * no legacy flag the file replaces is left in the args (oauth2-proxy
    refuses to start with them beside --alpha-config);
  * the rendered injectRequestHeaders has exactly the shape tenant-api
    accepts (one claimSource each, no prefix, no fixed value, client copy
    stripped), with oauth2Proxy.claimHeaders as the extra claims;
  * the removed identity.claimHeaders key fails the render and
    TA_IDENTITY_CLAIM_HEADERS is gone (ADR-027 §6);
  * a '$' in a value the chart writes into the config file fails the render:
    oauth2-proxy expands environment references in the file and tenant-api
    refuses such a file (a value that becomes a flag, such as redirectUrl,
    is not in the file and is not checked).

tests/oauth2-proxy/ loads the same render with oauth2-proxy's own code, and
CI runs `--config-test` in the pinned image (ADR-027 §9 item 9).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
from _render_baseline import baseline_args

_REPO = Path(__file__).resolve().parents[2]
_CHART = _REPO / "helm" / "tenant-api"
_HAS_HELM = shutil.which("helm") is not None
_REQUIRE_HELM = helm_required()

#: oauth2-proxy flags the config file replaces; with --alpha-config each one
#: stops oauth2-proxy ("unknown flag").
LEGACY_FLAGS = ("--provider", "--oidc-issuer-url", "--scope", "--gitlab-group", "--upstream",
                "--http-address", "--pass-user-headers", "--set-xauthrequest", "--client-id",
                "--client-secret", "--client-secret-file")


def _helm_or_skip() -> None:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail("helm is required in this environment but not on PATH")
        pytest.skip("helm CLI not on PATH")


def _render(*set_args: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(_CHART), *baseline_args(_CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def _parts(*set_args: str) -> tuple[dict, dict, dict, dict]:
    """(oauth2-proxy config, config ConfigMap, tenant-api container,
    oauth2-proxy container) and the pod spec's volumes under key 'volumes'."""
    res = _render(*set_args)
    assert res.returncode == 0, res.stderr
    cm = pod = None
    for doc in yaml.safe_load_all(res.stdout):
        if not doc:
            continue
        if doc.get("kind") == "ConfigMap" and "-oauth2-proxy-" in doc["metadata"]["name"]:
            cm = doc
        if doc.get("kind") == "Deployment":
            pod = doc["spec"]["template"]["spec"]
    assert cm is not None and pod is not None, "no oauth2-proxy ConfigMap or Deployment"
    containers = {c["name"]: c for c in pod["containers"]}
    containers["volumes"] = {v["name"]: v for v in pod["volumes"]}
    (key,) = cm["data"]
    return yaml.safe_load(cm["data"][key]), cm, containers["tenant-api"], containers


def _mounts_of(container: dict, volume: str) -> list[dict]:
    return [m for m in container.get("volumeMounts") or [] if m["name"] == volume]


# adr-check: ADR-027/9.3e
def test_both_containers_read_the_same_file() -> None:
    _helm_or_skip()
    _, cm, ta, c = _parts()
    proxy = c["oauth2-proxy"]
    vol = [n for n, v in c["volumes"].items()
           if (v.get("configMap") or {}).get("name") == cm["metadata"]["name"]]
    assert len(vol) == 1, vol
    ta_mounts, proxy_mounts = _mounts_of(ta, vol[0]), _mounts_of(proxy, vol[0])
    assert len(ta_mounts) == len(proxy_mounts) == 1
    assert ta_mounts[0]["mountPath"] == proxy_mounts[0]["mountPath"]
    assert ta_mounts[0].get("readOnly") and proxy_mounts[0].get("readOnly")
    (key,) = cm["data"]
    path = f"{proxy_mounts[0]['mountPath']}/{key}"
    assert f"--alpha-config={path}" in proxy["args"], proxy["args"]
    env = {e["name"]: e.get("value") for e in ta.get("env") or []}
    assert env.get("TA_OAUTH2_PROXY_CONFIG") == path, env
    # No other container mounts it: the file decides identity.
    others = [n for n, ctr in c.items() if n not in ("tenant-api", "oauth2-proxy", "volumes")
              and _mounts_of(ctr, vol[0])]
    assert others == [], others


def test_client_secret_reaches_oauth2_proxy_only() -> None:
    _helm_or_skip()
    cfg, _, ta, c = _parts()
    (provider,) = cfg["providers"]
    assert "clientSecret" not in provider, "the client secret must not be in the ConfigMap"
    secret_vols = [n for n, v in c["volumes"].items() if "secret" in v
                   and any(i["key"] == "OAUTH2_PROXY_CLIENT_SECRET" for i in v["secret"].get("items", []))]
    assert len(secret_vols) == 1, secret_vols
    (mount,) = _mounts_of(c["oauth2-proxy"], secret_vols[0])
    (item,) = c["volumes"][secret_vols[0]]["secret"]["items"]
    assert provider["clientSecretFile"] == f"{mount['mountPath']}/{item['path']}"
    assert _mounts_of(ta, secret_vols[0]) == []
    env = [e["name"] for e in c["oauth2-proxy"].get("env") or []]
    assert env == ["OAUTH2_PROXY_COOKIE_SECRET"], env


@pytest.mark.parametrize("render", [[], ["oauth2Proxy.provider=gitlab",
                                         "oauth2Proxy.oidcIssuerUrl=https://gitlab.example",
                                         "oauth2Proxy.gitlabGroups={a/b}",
                                         "oauth2Proxy.scope=openid email"]])
def test_no_legacy_flag_beside_the_file(render: list[str]) -> None:
    _helm_or_skip()
    _, _, _, c = _parts(*render)
    args = c["oauth2-proxy"]["args"]
    assert not [a for a in args if a.split("=", 1)[0] in LEGACY_FLAGS], args
    # Vacuity guard: the args that stay are still there.
    assert any(a.startswith("--redirect-url=") for a in args), args


def test_injected_headers_have_the_shape_tenant_api_accepts() -> None:
    _helm_or_skip()
    cfg, *_ = _parts("oauth2Proxy.claimHeaders.region=X-Auth-Request-Region",
                     "oauth2Proxy.claimHeaders.org=X-Auth-Request-Org")
    assert cfg["injectRequestHeaders"] == [
        {"name": h, "values": [{"claimSource": {"claim": claim}}]}
        for h, claim in [("X-Forwarded-Email", "email"), ("X-Forwarded-Groups", "groups"),
                         ("X-Auth-Request-Org", "org"), ("X-Auth-Request-Region", "region")]
    ]
    assert "injectResponseHeaders" not in cfg


def test_default_injects_email_and_groups_only() -> None:
    _helm_or_skip()
    cfg, *_ = _parts()
    assert [h["name"] for h in cfg["injectRequestHeaders"]] == ["X-Forwarded-Email", "X-Forwarded-Groups"]


@pytest.mark.parametrize("bad, says", [
    ("oauth2Proxy.claimHeaders.email=X-Other", "email and groups are always injected"),
    ("oauth2Proxy.claimHeaders.org=X_Org", "claimHeaders"),
    ("oauth2Proxy.claimHeaders.access_token=X-A", "not an identity claim"),
    ("oauth2Proxy.claimHeaders.org=x-forwarded-email", "already carries email"),
    ("oauth2Proxy.clientID=", "oauth2Proxy.clientID is required"),
    ("oauth2Proxy.redirectUrl=https://x/$HOME/cb", ""),
    ("oauth2Proxy.clientID=abc$X", "holds '$'"),
    ("identity.claimHeaders.org=X-Org", "identity"),
])
def test_refused_values_fail_the_render(bad: str, says: str) -> None:
    _helm_or_skip()
    res = _render(bad)
    if bad.startswith("oauth2Proxy.redirectUrl"):
        # redirectUrl is a flag, not in the file: a '$' there is oauth2-proxy's
        # business, and the render must still succeed.
        assert res.returncode == 0, res.stderr
        return
    assert res.returncode != 0, f"{bad} rendered"
    assert says in res.stderr, res.stderr


def test_removed_variable_is_not_rendered() -> None:
    _helm_or_skip()
    _, _, ta, _ = _parts("oauth2Proxy.claimHeaders.org=X-Org")
    names = [e["name"] for e in ta.get("env") or []]
    assert "TA_OAUTH2_PROXY_CONFIG" in names  # vacuity guard
    assert "TA_IDENTITY_CLAIM_HEADERS" not in names


def test_config_map_is_immutable_and_renamed_on_change() -> None:
    """Both containers read the file at start only; an in-place change and one
    container restarting would split what the proxy strips from what
    tenant-api trusts. So the object is immutable and a change renames it."""
    _helm_or_skip()
    _, cm, _, _ = _parts()
    assert cm.get("immutable") is True
    _, cm2, ta2, _ = _parts("oauth2Proxy.claimHeaders.org=X-Org")
    assert cm2["metadata"]["name"] != cm["metadata"]["name"]
    names = [(v.get("configMap") or {}).get("name") for v in _parts("oauth2Proxy.claimHeaders.org=X-Org")[3]["volumes"].values()]
    assert cm2["metadata"]["name"] in names


def test_values_yaml_carries_no_identity_block() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "identity" not in values
    assert values["oauth2Proxy"]["claimHeaders"] == {}
