"""test_generator_container.py — tenant-api chart's generator container (#2902, #2903)

ADR-036 §3/§5: the chart runs a da-tools sidecar, `da-tools generator-service`,
which tenant-api asks before every content-changing write. Since #2903 it is
REQUIRED — the `enabled` switch is gone. Its trust boundary and its
correctness both live in the chart, so the chart is what these tests pin:

  * required: no switch; tenant-api is passed the generator's socket
    (--generator-socket) and, with a deployment policy, the same file
    (--deploy-policy = the generator's --policy).
  * same uid: the generator and tenant-api run as one uid, counting a
    container-level `runAsUser` (git refuses a repository another uid owns).
  * the socket directory is mounted by EXACTLY two containers — the generator
    and tenant-api — out of every container and init container the chart can
    render (the service's SO_PEERCRED check admits any same-uid peer, so the
    mount is the boundary).
  * the repository: the generator reads the conf-dir volume read-only, and its
    --repo / --config-subdir agree with tenant-api's --git-dir / --config-dir.
  * work-dir is a DISK emptyDir; the deployment policy ConfigMap key is mounted
    at the same path in both containers and passed as --policy.
  * request = limit (render fails otherwise); gc.auto=0 on every repository
    shape (clone, existing pvc/hostPath repo), with the init scripts RUN here.
  * body limits follow tenant-api's: a tenant-api cap whose base64 form does
    not fit the generator's default raises the generator's two limits.

Helm-gated like its siblings; CI installs helm via setup-helm.
"""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml
from _helm_gate import helm_required
from _render_baseline import baseline_args

_REPO_ROOT = Path(__file__).resolve().parents[2]
# ⛔ One literal path string per chart (verify_diff.py text_map, #1313).
_CHART = _REPO_ROOT / "helm/tenant-api"
_GO_LIMITS = _REPO_ROOT / "components/tenant-api/internal/handler/middleware.go"
_GENERATOR_SERVICE = _REPO_ROOT / "scripts/tools/ops/generator_service.py"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
_REQUIRE_HELM = helm_required()  # tests/_helm_gate.py: CI or a remote session
_SH = shutil.which("sh")
_GIT = shutil.which("git")
_needs_sh_git = pytest.mark.skipif(_SH is None or _GIT is None, reason="no sh / git on PATH")

# The generator is always on (#2903); a clone shape on top of the render
# baseline's hostPath conf.d.
_ON = ("gitRepoUrl=https://git.example.com/org/conf.git",)
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"


def test_helm_present_when_required() -> None:
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            f"{_REQUIRE_HELM}: but no `helm` on PATH — every render assertion "
            "in this file would have skipped silently")


def test_sh_and_git_present_on_posix() -> None:
    if os.name != "nt":
        assert _SH is not None and _GIT is not None, (
            "no sh / git on a POSIX host — the init-script tests would skip silently")


# ── helpers ──────────────────────────────────────────────────────────────────
def _helm(*overrides: str, values: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "t", str(_CHART), *baseline_args(_CHART)]
    if values is not None:
        cmd += ["-f", str(values)]
    for o in overrides:
        cmd += ["--set", o]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90)


def _render(*overrides: str, values: Path | None = None) -> str:
    out = _helm(*overrides, values=values)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _pod(stdout: str) -> dict:
    for doc in yaml.safe_load_all(stdout):
        if doc and doc.get("kind") == "Deployment":
            return doc["spec"]["template"]["spec"]
    raise AssertionError("no Deployment in the render")


def _all_containers(pod: dict) -> list[dict]:
    return list(pod.get("initContainers") or []) + list(pod["containers"])


def _container(pod: dict, name: str) -> dict:
    found = [c for c in _all_containers(pod) if c["name"] == name]
    assert len(found) == 1, (name, [c["name"] for c in _all_containers(pod)])
    return found[0]


def _flag(container: dict, name: str) -> str | None:
    """Value of `--name=value` in args; None when absent."""
    values = [a.split("=", 1)[1] for a in container.get("args", [])
              if isinstance(a, str) and a.startswith(f"--{name}=")]
    assert len(values) <= 1, (name, values)
    return values[0] if values else None


def _mounts(container: dict, volume: str) -> list[dict]:
    return [m for m in container.get("volumeMounts") or [] if m["name"] == volume]


def _volume(pod: dict, name: str) -> dict:
    found = [v for v in pod["volumes"] if v["name"] == name]
    assert len(found) == 1, (name, [v["name"] for v in pod["volumes"]])
    return found[0]


def _values_file(tmp_path: Path, tree: dict) -> Path:
    vf = tmp_path / "v.yaml"
    vf.write_text(yaml.safe_dump(tree), encoding="utf-8")
    return vf


# ── required: no switch ──────────────────────────────────────────────────────
def test_values_required_with_explicit_pin() -> None:
    """No `enabled` switch any more (#2903), no policy escape hatch, and the
    image tag is a written fact — the capability gate and bump_docs both read
    it from values.yaml."""
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    gen = values["generator"]
    assert "enabled" not in gen
    assert "policy" not in values, "policy.unavailableOpen is removed (#2903, ADR-036 §5)"
    assert gen["image"]["repository"] == "ghcr.io/vencil/da-tools"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", gen["image"]["tag"])
    assert gen["resources"]["requests"] == gen["resources"]["limits"] == {
        "cpu": "1", "memory": "512Mi"}  # ADR-036 §3


@_needs_helm
@pytest.mark.parametrize("shape", [(), _ON, ("confDir.type=pvc",)], ids=["hostPath", "clone", "pvc"])
def test_generator_always_rendered_and_wired_to_tenant_api(shape) -> None:
    """Every repository shape renders the generator, and tenant-api dials
    exactly the socket the generator listens on."""
    pod = _pod(_render(*shape))
    gen, ta = _container(pod, "generator"), _container(pod, "tenant-api")
    assert _flag(ta, "generator-socket") == _flag(gen, "socket")
    assert _flag(ta, "generator-socket").startswith(_mounts(ta, "generator-sock")[0]["mountPath"] + "/")
    assert _flag(ta, "deploy-policy") is None
    assert "--policy-unavailable-open" not in ta["args"]


@_needs_helm
def test_tenant_api_reads_the_policy_file_the_generator_judges_with() -> None:
    pod = _pod(_render("generator.policy.existingConfigMap=da-policy"))
    gen, ta = _container(pod, "generator"), _container(pod, "tenant-api")
    assert _flag(ta, "deploy-policy") == _flag(gen, "policy") is not None


@_needs_helm
def test_removed_policy_escape_hatch_value_is_not_read() -> None:
    """`policy.unavailableOpen` (#2486) is gone: setting it renders nothing."""
    ta = _container(_pod(_render("policy.unavailableOpen=true")), "tenant-api")
    assert not any("unavailable-open" in a for a in ta["args"])


# ── uid ──────────────────────────────────────────────────────────────────────
def _effective_uid(pod: dict, container: dict):
    return ((container.get("securityContext") or {}).get("runAsUser")
            or (pod.get("securityContext") or {}).get("runAsUser"))


@_needs_helm
def test_generator_and_tenant_api_share_one_uid() -> None:
    """ADR-036 §3: git refuses a repository another uid owns. The effective uid
    counts a container-level runAsUser over the pod's."""
    pod = _pod(_render(*_ON))
    ta, gen = _container(pod, "tenant-api"), _container(pod, "generator")
    assert _effective_uid(pod, ta) is not None
    assert _effective_uid(pod, ta) == _effective_uid(pod, gen)
    # …and neither overrides the pod's: one place decides it.
    for c in (ta, gen):
        assert "runAsUser" not in (c.get("securityContext") or {}), c["name"]


@_needs_helm
def test_no_pod_uid_fails_the_render() -> None:
    """Without a pod-level uid each container runs as its image's USER
    (da-tools: 10001), which cannot be shown equal to tenant-api's."""
    out = _helm(*_ON, "podSecurityContext.runAsUser=null")
    assert out.returncode != 0
    assert "podSecurityContext.runAsUser" in out.stderr


# ── the socket directory: exactly two containers ─────────────────────────────
# Every optional container the chart can render, switched on at once.
_EVERYTHING = (*_ON, "humanSocket.enabled=true", "federation.enabled=true",
               "machineIdentity.enabled=true", "oauth2Proxy.enabled=true",
               "generator.policy.existingConfigMap=da-policy")


@_needs_helm
@pytest.mark.parametrize("shape", [
    (), ("confDir.type=pvc", "gitRepoUrl="), ("confDir.type=hostPath", "confDir.hostPath=/x", "gitRepoUrl="),
], ids=["clone", "pvc", "hostPath"])
def test_only_generator_and_tenant_api_mount_the_socket_dir(shape) -> None:
    pod = _pod(_render(*_EVERYTHING, *shape))
    names = [c["name"] for c in _all_containers(pod)]
    assert len(names) >= 4, names  # the scan saw the sidecars and an init container
    mounting = sorted(c["name"] for c in _all_containers(pod) if _mounts(c, "generator-sock"))
    assert mounting == ["generator", "tenant-api"], mounting
    gen, ta = _container(pod, "generator"), _container(pod, "tenant-api")
    sock = _flag(gen, "socket")
    (gm,), (tm,) = _mounts(gen, "generator-sock"), _mounts(ta, "generator-sock")
    assert gm["mountPath"] == tm["mountPath"] == posixpath.dirname(sock)
    assert tm.get("readOnly") is True and not gm.get("readOnly")
    assert _volume(pod, "generator-sock")["emptyDir"]["medium"] == "Memory"


# ── the repository ───────────────────────────────────────────────────────────
@_needs_helm
def test_generator_reads_tenant_api_repository_read_only() -> None:
    """--repo / --config-subdir must name what tenant-api's --git-dir /
    --config-dir name. Today conf.d IS the repository root; if tenant-api's two
    flags move, this fails until the generator's follow."""
    pod = _pod(_render(*_ON))
    ta, gen = _container(pod, "tenant-api"), _container(pod, "generator")
    git_dir, config_dir = _flag(ta, "git-dir"), _flag(ta, "config-dir")
    assert git_dir and config_dir
    assert _flag(gen, "repo") == git_dir
    rel = posixpath.relpath(config_dir, git_dir)
    assert _flag(gen, "config-subdir") == ("" if rel == "." else rel)
    # the same volume tenant-api writes, read-only, at the path --repo names
    (ta_conf,) = [m for m in ta["volumeMounts"] if m["mountPath"] == git_dir]
    (gen_conf,) = _mounts(gen, ta_conf["name"])
    assert gen_conf["mountPath"] == git_dir and gen_conf.get("readOnly") is True
    assert gen["args"][0] == "generator-service"
    assert "command" not in gen  # the image's entrypoint dispatches it


@_needs_helm
def test_work_dir_is_disk_and_tmp_is_writable() -> None:
    pod = _pod(_render(*_ON, "generator.workDir.sizeLimit=3Gi"))
    gen = _container(pod, "generator")
    work = _volume(pod, "generator-work")["emptyDir"]
    assert "medium" not in work, "Memory would count against the container's limit"
    assert work["sizeLimit"] == "3Gi"
    paths = {m["mountPath"] for m in _mounts(gen, "generator-work")}
    assert paths == {_flag(gen, "work-dir")}
    # /tmp is a volume of its own, never a subPath (kubelet may create a
    # missing subPath directory root-owned, unwritable by the pod's uid)
    (tmp,) = [m for m in gen["volumeMounts"] if m["mountPath"] == "/tmp"]
    assert "subPath" not in tmp and "subPathExpr" not in tmp
    assert tmp["name"] not in {m["name"] for m in gen["volumeMounts"]
                               if m["mountPath"] != "/tmp"}
    tmp_volume = _volume(pod, tmp["name"])["emptyDir"]
    assert "medium" not in tmp_volume
    assert tmp_volume["sizeLimit"] == re.search(
        r'define "tenant-api\.generatorTmpSizeLimit" -\}\}([^{]+)\{\{',
        (_CHART / "templates/_helpers.tpl").read_text(encoding="utf-8")).group(1)
    assert all("subPath" not in m for m in gen["volumeMounts"])
    assert gen["securityContext"]["readOnlyRootFilesystem"] is True
    assert gen["securityContext"]["allowPrivilegeEscalation"] is False
    assert gen["securityContext"]["capabilities"]["drop"] == ["ALL"]


@_needs_helm
def test_service_account_token_is_shadowed_in_the_generator_only() -> None:
    pod = _pod(_render(*_ON))
    (m,) = [m for m in _container(pod, "generator")["volumeMounts"]
            if m["mountPath"] == _SA_TOKEN_PATH]
    assert "emptyDir" in _volume(pod, m["name"])
    assert not [m for m in _container(pod, "tenant-api")["volumeMounts"]
                if m["mountPath"] == _SA_TOKEN_PATH]


# ── deployment policy ────────────────────────────────────────────────────────
@_needs_helm
def test_policy_is_one_configmap_key_at_one_path_in_both_containers() -> None:
    pod = _pod(_render(*_ON, "generator.policy.existingConfigMap=da-policy",
                       "generator.policy.key=custom-rule-policy.yaml"))
    gen, ta = _container(pod, "generator"), _container(pod, "tenant-api")
    (gm,), (tm,) = _mounts(gen, "generator-policy"), _mounts(ta, "generator-policy")
    assert gm == tm and gm.get("readOnly") is True
    cm = _volume(pod, "generator-policy")["configMap"]
    assert cm["name"] == "da-policy"
    (item,) = cm["items"]
    assert item["key"] == "custom-rule-policy.yaml"
    assert _flag(gen, "policy") == posixpath.join(gm["mountPath"], item["path"])
    assert not cm.get("optional"), "a missing ConfigMap must stop the pod, not judge without it"


@_needs_helm
def test_no_policy_configured_means_no_flag_and_no_mount() -> None:
    pod = _pod(_render(*_ON))
    assert _flag(_container(pod, "generator"), "policy") is None
    assert all(not _mounts(c, "generator-policy") for c in _all_containers(pod))
    assert "generator-policy" not in [v["name"] for v in pod["volumes"]]


@_needs_helm
def test_policy_configmap_without_key_fails() -> None:
    out = _helm(*_ON, "generator.policy.existingConfigMap=da-policy", "generator.policy.key=")
    assert out.returncode != 0 and "generator.policy.key" in out.stderr


# ── resources: request = limit ───────────────────────────────────────────────
@_needs_helm
def test_default_resources_are_guaranteed() -> None:
    res = _container(_pod(_render(*_ON)), "generator")["resources"]
    assert res["requests"] == res["limits"] == {"cpu": "1", "memory": "512Mi"}


@_needs_helm
@pytest.mark.parametrize("override", [
    "generator.resources.limits.memory=1Gi",
    "generator.resources.requests.cpu=500m",
    "generator.resources.requests.memory=null",
])
def test_request_not_equal_to_limit_fails(override: str) -> None:
    out = _helm(*_ON, override)
    assert out.returncode != 0
    assert "generator.resources" in out.stderr


# ── git: a repository must exist, gc.auto=0 ──────────────────────────────────
@_needs_helm
def test_emptydir_without_git_repo_url_fails() -> None:
    out = _helm("confDir.type=emptyDir")
    assert out.returncode != 0 and "gitRepoUrl" in out.stderr


def _script(container: dict, workdir: Path) -> str:
    assert container["command"][:2] == ["sh", "-c"]
    return container["command"][2].replace("/conf.d", str(workdir))


def _run(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([_SH, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})}, timeout=60)


def _repo(path: Path) -> Path:
    subprocess.run([_GIT, "init", "-q", str(path)], check=True, timeout=60)
    return path


def _gc_auto(path: Path) -> str:
    return subprocess.run([_GIT, "-C", str(path), "config", "--get", "gc.auto"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60).stdout.strip()


@_needs_helm
@_needs_sh_git
def test_clone_init_sets_gc_auto_and_keeps_a_failed_pull_failing(tmp_path: Path) -> None:
    """The clone path, run: an existing repository gets gc.auto=0 after the
    pull; a failing pull still fails the init container (the appended `git
    config` must not turn its status into 0)."""
    git_clone = _container(_pod(_render(*_ON)), "git-clone")
    repo = _repo(tmp_path / "conf")
    # pull has no remote here: it fails — and must keep failing the script
    failed = _run(_script(git_clone, repo))
    assert failed.returncode != 0, failed
    assert _gc_auto(repo) == ""
    # with pull stubbed to succeed, gc.auto is set
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "git").write_text(
        f'#!/bin/sh\nfor a in "$@"; do [ "$a" = pull ] && exit 0; done\nexec {_GIT} "$@"\n',
        encoding="utf-8")
    (stub / "git").chmod(0o755)
    ok = _run(_script(git_clone, repo), {"PATH": f"{stub}:{os.environ['PATH']}"})
    assert ok.returncode == 0, ok
    assert _gc_auto(repo) == "0"


@_needs_helm
@pytest.mark.parametrize("shape", [("confDir.type=pvc",),
                                   ("confDir.type=hostPath", "confDir.hostPath=/x")],
                         ids=["pvc", "hostPath"])
def test_existing_repo_shapes_get_a_gc_off_init(shape) -> None:
    pod = _pod(_render(*shape))
    names = [c["name"] for c in pod["initContainers"]]
    assert names == ["git-gc-off"], names
    assert "gc.auto 0" in _container(pod, "git-gc-off")["command"][2]


@_needs_helm
@_needs_sh_git
def test_gc_off_init_sets_it_or_skips_without_failing(tmp_path: Path) -> None:
    gc_off = _container(_pod(_render("confDir.type=pvc")), "git-gc-off")
    empty = tmp_path / "empty"
    empty.mkdir()
    skipped = _run(_script(gc_off, empty))
    assert skipped.returncode == 0, skipped
    assert "no git repository" in skipped.stdout
    repo = _repo(tmp_path / "conf")
    done = _run(_script(gc_off, repo))
    assert done.returncode == 0, done
    assert _gc_auto(repo) == "0"


# ── body limits follow tenant-api's ──────────────────────────────────────────
def _go_const(name: str) -> int:
    m = re.search(rf"const {name} int64 = (\d+) << (\d+)",
                  _GO_LIMITS.read_text(encoding="utf-8"))
    assert m, name
    return int(m.group(1)) << int(m.group(2))


def _chart_const(name: str) -> int:
    m = re.search(r'define "tenant-api\.' + name + r'" -\}\}(\d+)\{\{',
                  (_CHART / "templates/_helpers.tpl").read_text(encoding="utf-8"))
    assert m, name
    return int(m.group(1))


def test_chart_copies_of_the_binary_defaults_match() -> None:
    """The chart's copies of the two binaries' defaults are pinned to the
    sources they copy."""
    import generator_service as gs  # noqa: PLC0415 (tests/conftest.py: scripts/tools/ops)
    assert Path(gs.__file__).resolve() == _GENERATOR_SERVICE.resolve()
    assert _chart_const("taDefaultMaxBodyBytes") == _go_const("DefaultMaxBodyBytes")
    assert _chart_const("taDefaultMaxBatchBodyBytes") == _go_const("DefaultMaxBatchBodyBytes")
    assert _chart_const("generatorDefaultMaxBodyBytes") == gs.DEFAULT_MAX_BODY_BYTES
    assert (_chart_const("generatorDefaultMaxInflightBodyBytes")
            == gs.DEFAULT_MAX_INFLIGHT_BODY_BYTES)


def _needed(n: int) -> int:
    return 4 * ((n + 2) // 3) + _chart_const("generatorJsonEnvelopeBytes")


@_needs_helm
def test_default_tenant_api_limits_pass_no_body_flags() -> None:
    gen = _container(_pod(_render(*_ON)), "generator")
    assert _flag(gen, "max-body-bytes") is None
    assert _flag(gen, "max-inflight-body-bytes") is None
    # …because the default's base64 form fits the generator's own default
    assert _needed(_chart_const("taDefaultMaxBodyBytes")) <= _chart_const(
        "generatorDefaultMaxBodyBytes")


@_needs_helm
@pytest.mark.parametrize("body,batch,expect_from", [
    (8 * 1024 * 1024, None, "body"),            # --set path (int64)
    ("8388608", None, "body"),                  # a string, as the env accepts it
    (None, 16 * 1024 * 1024, "batch"),          # the larger of the two wins
    (3 * 1024 * 1024 + 1, None, "body"),        # just past the generator default
], ids=["int", "string", "batch-larger", "boundary"])
def test_raised_tenant_api_limit_raises_the_generators(tmp_path: Path, body, batch, expect_from) -> None:
    server = {}
    if body is not None:
        server["maxBodyBytes"] = body
    if batch is not None:
        server["maxBatchBodyBytes"] = batch
    vf = _values_file(tmp_path, {"tenantApi": {"server": server}})
    gen = _container(_pod(_render(*_ON, values=vf)), "generator")
    n = int(body if expect_from == "body" else batch)
    max_body = int(_flag(gen, "max-body-bytes"))
    inflight = int(_flag(gen, "max-inflight-body-bytes"))
    assert max_body == _needed(n)
    assert max_body >= -(-4 * n // 3), "below the base64 size of a request tenant-api accepts"
    assert inflight >= max_body


def _go_parse_int(raw: str, default: int) -> int:
    """MaxBodyBytesFromEnv: strings.TrimSpace + strconv.ParseInt(v, 10, 64);
    an error, 0 or a negative value → default."""
    v = raw.strip()
    if not re.fullmatch(r"[+-]?[0-9]+", v):
        return default
    n = int(v, 10)
    return n if 0 < n <= 2**63 - 1 else default


@_needs_helm
@pytest.mark.parametrize("raw", [
    "0010485760", "+8388608", "010000000", " 8388608 ",
    "99999999999999999999", "9223372036854775808", "-8388608",
], ids=["leading-zeros", "plus", "not-octal", "spaces", "too-long",
        "past-int64", "negative"])
def test_string_limit_is_parsed_like_go(tmp_path: Path, raw: str) -> None:
    """The chart reads a string limit the way tenant-api does: DECIMAL (a
    leading 0 is not octal), `+` and leading zeros accepted, out of range or
    not positive → the default."""
    vf = _values_file(tmp_path, {"tenantApi": {"server": {"maxBodyBytes": raw}}})
    gen = _container(_pod(_render(*_ON, values=vf)), "generator")
    n = _go_parse_int(raw, _chart_const("taDefaultMaxBodyBytes"))
    expected = _needed(n)
    if expected <= _chart_const("generatorDefaultMaxBodyBytes"):
        assert _flag(gen, "max-body-bytes") is None
    else:
        assert int(_flag(gen, "max-body-bytes")) == expected


@_needs_helm
@pytest.mark.parametrize("value", ["", "0", "garbage", None], ids=["empty", "zero", "malformed", "null"])
def test_unusable_tenant_api_limit_is_read_as_its_default(tmp_path: Path, value) -> None:
    """The binary replaces these with its default; so does the derivation."""
    vf = _values_file(tmp_path, {"tenantApi": {"server": {"maxBodyBytes": value}}})
    gen = _container(_pod(_render(*_ON, values=vf)), "generator")
    assert _flag(gen, "max-body-bytes") is None


# ── probes ───────────────────────────────────────────────────────────────────
_PROBES = {  # (periodSeconds, timeoutSeconds, failureThreshold)
    "startupProbe": (2, 5, 60),
    "readinessProbe": (10, 5, 3),
    "livenessProbe": (20, 5, 3),
}


def _probe_script(gen: dict) -> str:
    commands = [gen[name]["exec"]["command"] for name in _PROBES]
    assert commands[0] == commands[1] == commands[2], "the three probes drifted apart"
    assert commands[0][:2] == ["python3", "-c"]
    return commands[0][2]


@_needs_helm
def test_probe_timings() -> None:
    gen = _container(_pod(_render(*_ON)), "generator")
    for name, (period, timeout, failures) in _PROBES.items():
        probe = gen[name]
        assert (probe["periodSeconds"], probe["timeoutSeconds"],
                probe["failureThreshold"]) == (period, timeout, failures), name


@_needs_helm
def test_probe_dials_the_generator_socket() -> None:
    gen = _container(_pod(_render(*_ON)), "generator")
    connects = re.findall(r'connect\("([^"]+)"\)', _probe_script(gen))
    assert connects == [_flag(gen, "socket")]


def _run_probe(script: str, socket_path: str) -> int:
    rendered = re.findall(r'connect\("([^"]+)"\)', script)
    assert len(rendered) == 1
    local = script.replace(f'connect("{rendered[0]}")', f'connect("{socket_path}")')
    return subprocess.run([sys.executable, "-c", local], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=30).returncode


def _short_dir() -> str:
    # AF_UNIX paths are limited to ~107 bytes; a test tmp_path can exceed it.
    return tempfile.mkdtemp(prefix="g2902-", dir="/tmp" if os.path.isdir("/tmp") else None)


@_needs_helm
@_needs_sh_git
def test_probe_passes_on_the_real_service_and_fails_without_it(tmp_path: Path) -> None:
    script = _probe_script(_container(_pod(_render(*_ON)), "generator"))
    repo = _repo(tmp_path / "conf")
    (repo / "t1.yaml").write_text("tenants: {}\n", encoding="utf-8")
    subprocess.run([_GIT, "-C", str(repo), "add", "."], check=True, timeout=60)
    subprocess.run([_GIT, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], check=True, timeout=60)
    sock_dir = _short_dir()
    sock = os.path.join(sock_dir, "generator.sock")
    service = subprocess.Popen(
        [sys.executable, str(_GENERATOR_SERVICE), "--socket", sock, "--repo", str(repo),
         "--config-subdir=", "--work-dir", str(tmp_path / "work")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not os.path.exists(sock):
            assert service.poll() is None, service.stderr.read()
            assert time.monotonic() < deadline, "the service never created its socket"
            time.sleep(0.1)
        assert _run_probe(script, sock) == 0
    finally:
        service.terminate()
        service.wait(timeout=30)
        service.stderr.close()
    assert _run_probe(script, sock) != 0, "no service listening must fail the probe"
    shutil.rmtree(sock_dir, ignore_errors=True)


@_needs_helm
@pytest.mark.parametrize("status_line", [
    b"HTTP/1.1 503 Service Unavailable", b"HTTP/1.1 2000 Odd", b"HTTP/1.0 200 OK",
], ids=["503", "2000", "http-1.0"])
def test_probe_fails_on_anything_but_a_200_status_line(status_line: bytes) -> None:
    script = _probe_script(_container(_pod(_render(*_ON)), "generator"))
    sock_dir = _short_dir()
    sock = os.path.join(sock_dir, "generator.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock)
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(status_line + b"\r\nContent-Length: 0\r\n\r\n")

    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    try:
        assert _run_probe(script, sock) != 0, status_line
    finally:
        worker.join(timeout=10)
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)
