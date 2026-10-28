"""ADR-036 §12: the tenant-api container gets every setting that has a TA_*
variable as env, never as a container arg.

`kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show` (and
`state restore`, `config check`) is a new process: it sees the container's
environment, not the server's argv. A setting the chart passes as an arg is
invisible to it — `state show` would not find the state store, `state
restore` would merge the AccountID registry without the federation store's
live tokens and high-water mark. So every flag tenant-api registers with a
TA_* default must reach the container as that variable.

The flag → variable list is derived from the Go source
(`_render_baseline.tenant_api_flag_envs`: `"flag", envOrDefault("TA_…"` and the
other env* helpers), never written by hand: a flag that gains a TA_* variable
joins the check by itself.

The other half: tenant-api refuses (exit 2) a TA_* variable it does not read
(cmd/server/env_check.go), so every TA_* the chart renders must be in the list
the build accepts (cmd/server/known_env.go) — else the chart would stop its
own image.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
import re

from _render_baseline import baseline_args, tenant_api_flag_envs, tenant_api_known_envs

_REPO = Path(__file__).resolve().parents[2]
_CHART = "helm/tenant-api"
_HAS_HELM = shutil.which("helm") is not None
_REQUIRE_HELM = helm_required()

#: Renders that between them turn on every conditional tenant-api setting.
_RENDERS = {
    "default": [],
    "everything-on": [
        "federation.enabled=true", "machineIdentity.enabled=true", "humanSocket.enabled=true",
        "rbac.metadataScopeEnforce=true", "rbac.metadataWriteScopeEnforce=true",
        "rbac.orgScopeEnforce=true", "identity.claimHeaders.org=X-Org",
        "generator.policy.existingConfigMap=da-policy",
    ],
    "pr-github": ["writeBack.mode=pr-github", "gitRepoUrl=https://github.com/acme/conf.git",
                  "writeBack.token.secretName=vcs", "writeBack.github.repo=acme/conf"],
    "pr-gitlab": ["writeBack.mode=pr-gitlab", "gitRepoUrl=https://gitlab.example/acme/conf.git",
                  "writeBack.token.secretName=vcs", "writeBack.gitlab.project=acme/conf"],
}

#: What `tenant-api state show|restore` needs from the environment
#: (registerStateFlags, registerFederationStoreFlags in cmd/server).
_STATE_SUBCOMMAND_FLAGS = ("config-dir", "state-store", "state-configmap", "state-namespace",
                           "state-dir", "federation-key", "federation-store",
                           "federation-namespace")


def _render(name: str, *extra: str) -> subprocess.CompletedProcess:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail(f"{_REQUIRE_HELM}: but no `helm` on PATH")
        pytest.skip("helm CLI not on PATH")
    cmd = ["helm", "template", "t", str(_REPO / _CHART), *baseline_args(_CHART)]
    for kv in [*_RENDERS[name], *extra]:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def _tenant_api(name: str) -> dict:
    if not _HAS_HELM:
        if _REQUIRE_HELM:
            pytest.fail(f"{_REQUIRE_HELM}: but no `helm` on PATH")
        pytest.skip("helm CLI not on PATH")
    cmd = ["helm", "template", "t", str(_REPO / _CHART), *baseline_args(_CHART)]
    for kv in _RENDERS[name]:
        cmd += ["--set", kv]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=60)
    assert res.returncode == 0, res.stderr
    for d in yaml.safe_load_all(res.stdout):
        if d and d.get("kind") == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "tenant-api":
                    return c
    raise AssertionError("no tenant-api container rendered")


def test_the_derived_list_is_not_vacuous():
    envs = tenant_api_flag_envs()
    # Every setting the state subcommand reads has a TA_* variable, so the env
    # route reaches it (none needs a new variable).
    missing = [f for f in _STATE_SUBCOMMAND_FLAGS if f not in envs]
    assert not missing, f"state subcommand settings with no TA_* variable: {missing}"
    assert envs["federation-key"] == "TA_FEDERATION_KEY"


@pytest.mark.parametrize("render", sorted(_RENDERS))
def test_no_arg_has_a_ta_variable(render):
    envs = tenant_api_flag_envs()
    container = _tenant_api(render)
    as_args = sorted({a[2:].split("=", 1)[0] for a in container.get("args") or []
                      if isinstance(a, str) and a.startswith("--")} & envs.keys())
    assert not as_args, (
        f"{render}: tenant-api args carry flags that have a TA_* variable: {as_args} — pass "
        f"them as env ({', '.join(envs[f] for f in as_args)}) so `kubectl exec ... tenant-api "
        "state show|restore` and `config check` see them")


def test_the_settings_reach_the_env():
    envs = tenant_api_flag_envs()
    container = _tenant_api("everything-on")
    env = {e["name"] for e in container.get("env") or []}
    for flag in ("addr", "config-dir", "git-dir", "rbac", "write-mode", "generator-socket",
                 "deploy-policy", "federation-key", "federation-store", "federation-namespace",
                 "machine-identity-audit", "machine-identity-audience", "human-socket",
                 "rbac-metadata-scope-enforce", "rbac-metadata-write-scope-enforce",
                 "rbac-org-scope-enforce", "identity-claim-headers", "state-store",
                 "state-configmap"):
        assert envs[flag] in env, f"--{flag} ({envs[flag]}) is not in the tenant-api env"


@pytest.mark.parametrize("render", sorted(_RENDERS))
def test_every_rendered_ta_variable_is_one_tenant_api_reads(render):
    known = tenant_api_known_envs()
    env = {e["name"] for e in _tenant_api(render).get("env") or []}
    unknown = sorted(n for n in env if n.startswith("TA_") and n not in known)
    assert not unknown, (f"{render}: the chart sets TA_* variables tenant-api does not read: "
                         f"{unknown} — the image would refuse to start (env_check.go)")


def test_every_ta_variable_the_template_can_set_is_known():
    # Statically, so a variable behind a values switch no render above turns
    # on is checked too.
    raw = (_REPO / _CHART / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    names = set(re.findall(r"-\s+name:\s+(TA_[A-Z0-9_]+)", raw))
    assert len(names) > 20, names
    unknown = sorted(names - tenant_api_known_envs())
    assert not unknown, f"deployment.yaml can set TA_* variables tenant-api does not read: {unknown}"


def test_a_malformed_token_ttl_refuses_the_render():
    # As a flag a malformed TTL stopped the start; the env is read leniently,
    # so the render refuses it instead.
    res = _render("everything-on", "federation.tokenTTL=4hours")
    assert res.returncode != 0 and "federation.tokenTTL" in res.stderr, res.stderr
    assert _render("everything-on", "federation.tokenTTL=1h30m").returncode == 0


#: (render, the value set empty): tenant-api reads an empty TA_* as unset and
#: falls back to its default, where the old empty flag was an explicit value —
#: so the chart refuses the empty value at render time.
_EMPTY_REFUSED = [
    ("pr-github", "writeBack.baseBranch"),
    ("pr-gitlab", "writeBack.baseBranch"),
    ("pr-github", "writeBack.github.targetBranch"),
    ("pr-gitlab", "writeBack.gitlab.targetBranch"),
    ("default", "state.configMapName"),
    ("pr-github", "state.configMapName"),
]


@pytest.mark.parametrize("render,key", _EMPTY_REFUSED, ids=[f"{r}:{k}" for r, k in _EMPTY_REFUSED])
def test_an_empty_value_refuses_the_render(render, key):
    res = _render(render, f"{key}=")
    assert res.returncode != 0 and key in res.stderr, (res.returncode, res.stderr)


def test_direct_mode_does_not_need_a_base_branch():
    # writeBack.baseBranch is read only by the PR modes.
    assert _render("default", "writeBack.baseBranch=").returncode == 0
