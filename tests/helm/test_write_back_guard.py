"""test_write_back_guard.py — helm/tenant-api writeBack (#2855)

The chart used to pass no --write-mode, so every chart install ran `direct`:
commits kept in the pod's /conf.d, never pushed, never delivered to
threshold-exporter (which reads the threshold-config ConfigMap) and, with the
default emptyDir, lost on restart. The chart now refuses to render until
writeBack.mode is chosen, and wires the PR modes end to end: forge flags, the
token from an existing Secret, and git HTTPS credentials for clone and push
that never put the token in a URL, ConfigMap or plain env value.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/tenant-api"
_REPO_ROOT = Path(__file__).parent.parent.parent

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")

_GITHUB = [
    "writeBack.mode=pr-github",
    "gitRepoUrl=https://github.com/acme/conf.git",
    "writeBack.token.secretName=vcs",
    "writeBack.github.repo=acme/conf",
]
_GITLAB = [
    "writeBack.mode=pr-gitlab",
    "gitRepoUrl=https://gitlab.example/acme/conf.git",
    "writeBack.token.secretName=vcs",
    "writeBack.gitlab.project=acme/conf",
]
_DIRECT_ACK = ["writeBack.mode=direct", "writeBack.directAcknowledgeNotDelivered=true"]


def _render(*set_args: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(_REPO_ROOT / _CHART)]
    for kv in set_args:
        cmd += ["--set", kv]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def _containers(stdout: str) -> dict[str, dict]:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "Deployment":
            spec = doc["spec"]["template"]["spec"]
            return {c["name"]: c for c in spec.get("initContainers", []) + spec["containers"]}
    raise AssertionError("no Deployment rendered")


def _env(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


# ── static layer ────────────────────────────────────────────────────────────
def test_values_ship_no_default_mode():
    values = yaml.safe_load((_REPO_ROOT / _CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["writeBack"]["mode"] == "", "writeBack.mode must have no default (#2855)"
    assert values["writeBack"]["directAcknowledgeNotDelivered"] is False


# ── render layer ────────────────────────────────────────────────────────────
@_needs_helm
@pytest.mark.parametrize("args, needle", [
    ((), "writeBack.mode must be pr-github, pr-gitlab or direct"),
    (("writeBack.mode=pr",), "writeBack.mode must be pr-github, pr-gitlab or direct"),
    (("writeBack.mode=direct",), "directAcknowledgeNotDelivered"),
    (tuple(a for a in _GITHUB if not a.startswith("gitRepoUrl")), "needs gitRepoUrl"),
    (tuple(a for a in _GITHUB if not a.startswith("writeBack.token")), "needs writeBack.token.secretName"),
    (tuple(a for a in _GITHUB if not a.startswith("writeBack.github")), "needs writeBack.github.repo"),
    (tuple(a for a in _GITLAB if not a.startswith("writeBack.gitlab")), "needs writeBack.gitlab.project"),
])
def test_render_refuses(args, needle):
    res = _render(*args)
    assert res.returncode != 0, f"render with {args} should fail"
    assert needle in res.stderr, res.stderr


@_needs_helm
def test_acknowledged_direct_is_explicit_and_carries_no_forge_wiring():
    res = _render(*_DIRECT_ACK)
    assert res.returncode == 0, res.stderr
    cs = _containers(res.stdout)
    assert "git-clone" not in cs  # no gitRepoUrl
    args = cs["tenant-api"]["args"]
    assert "--write-mode=direct" in args
    assert not [a for a in args if a.startswith(("--github-", "--gitlab-", "--git-base-branch"))]
    env = _env(cs["tenant-api"])
    assert not {"TA_GITHUB_TOKEN", "TA_GITLAB_TOKEN", "GIT_FORGE_TOKEN", "GIT_CONFIG_COUNT"} & env.keys()


@_needs_helm
@pytest.mark.parametrize("args, mode, flags, token_env, user", [
    (_GITHUB, "pr-github", ["--github-repo=acme/conf", "--github-base-branch=main"], "TA_GITHUB_TOKEN", "x-access-token"),
    (_GITLAB, "pr-gitlab", ["--gitlab-project=acme/conf", "--gitlab-target-branch=main"], "TA_GITLAB_TOKEN", "oauth2"),
])
def test_pr_mode_is_wired_end_to_end(args, mode, flags, token_env, user):
    res = _render(*args)
    assert res.returncode == 0, res.stderr
    cs = _containers(res.stdout)
    main_args = cs["tenant-api"]["args"]
    assert f"--write-mode={mode}" in main_args
    assert "--git-base-branch=main" in main_args
    for f in flags:
        assert f in main_args
    env = _env(cs["tenant-api"])
    assert env[token_env]["valueFrom"]["secretKeyRef"] == {"name": "vcs", "key": "token"}
    # clone and push authenticate the same way, from the Secret
    for name in ("git-clone", "tenant-api"):
        e = _env(cs[name])
        assert e["GIT_FORGE_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "vcs", "key": "token"}, name
        assert e["GIT_FORGE_USER"]["value"] == user, name
        assert e["GIT_CONFIG_KEY_0"]["value"] == "credential.helper", name
        assert e["GIT_TERMINAL_PROMPT"]["value"] == "0", name


@_needs_helm
def test_the_credential_helper_answers_get_from_the_environment(tmp_path):
    """The helper the chart renders, run by a real git: `credential fill`
    returns the user and token from the environment, nothing else."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    res = _render(*_GITHUB)
    assert res.returncode == 0, res.stderr
    env = {k: v["value"] for k, v in _env(_containers(res.stdout)["tenant-api"]).items()
           if k.startswith("GIT_") and "value" in v}
    env.update({"GIT_FORGE_TOKEN": "s3cr3t", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "username=x-access-token" in out.stdout
    assert "password=s3cr3t" in out.stdout


@_needs_helm
def test_the_token_never_appears_in_rendered_text():
    """Only secretKeyRef points at the token; no rendered value carries one,
    and the clone URL ConfigMap holds the bare URL."""
    res = _render(*_GITHUB, "writeBack.token.secretKey=ghp-key")
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if doc and doc.get("kind") == "ConfigMap" and "GIT_REPO_URL" in doc.get("data", {}):
            assert doc["data"]["GIT_REPO_URL"] == "https://github.com/acme/conf.git"
    assert res.stdout.count("ghp-key") == 3  # TA_GITHUB_TOKEN + GIT_FORGE_TOKEN ×2, all secretKeyRef keys
