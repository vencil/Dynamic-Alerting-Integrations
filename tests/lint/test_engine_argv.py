"""The exact command each IaC lint runs its engine with, binary and docker (#3611).

Moving the four lints onto ``_lib_engine`` changed how their argv is built;
the docker fallback translates host paths to container paths (``/repo``,
``/scan``, ``/work``), sets ``-w`` and an entrypoint. None of that shows in a
run on a machine with the binary, so it is pinned here: the command is
captured at ``subprocess.run`` and compared with the one ``origin/main``
built before the move. Two differences are deliberate: the binary is run by
the path ``which`` found (main ran pint by bare name), and hadolint's image
gets ``--entrypoint hadolint`` where main passed ``hadolint`` as the command
(the image has no ENTRYPOINT and /bin/hadolint on PATH, so both run it).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
sys.path.insert(0, str(_TOOLS / "lint"))
sys.path.insert(0, str(_TOOLS))

import _lib_engine  # noqa: E402
import check_iac_helm as helm  # noqa: E402
import check_iac_vibe_rules as vibe  # noqa: E402
import check_k8s_manifests as k8s  # noqa: E402
import check_pint as pint  # noqa: E402

REPO = helm.REPO_ROOT.as_posix()
_KL_REPORT = '{"Checks":[],"Reports":null,"Summary":{}}'


@pytest.fixture
def capture(monkeypatch):
    """Record every engine command; answer with output its proof accepts."""
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "cwd": kw.get("cwd")})
        joined = " ".join(cmd)
        if "kube-linter" in joined:
            out, err = _KL_REPORT, ""
        elif "hadolint" in joined:
            out, err = "[]", ""
        elif "pint" in joined:
            out, err = "", f"entries={pint._MIN_PINT_ENTRIES + 1}"
        else:
            out, err = "kind: ConfigMap\n", ""
        return subprocess.CompletedProcess(cmd, 0, out, err)

    monkeypatch.setattr(_lib_engine.subprocess, "run", fake_run)
    return calls


def _only(monkeypatch, *names):
    paths = {n: f"/opt/bin/{n}" for n in names}
    monkeypatch.setattr(_lib_engine.shutil, "which", lambda n: paths.get(n))


# ── docker fallback: no engine binary, docker on PATH ────────────────────────

def test_helm_template_via_docker(monkeypatch, capture):
    _only(monkeypatch, "docker")
    assert isinstance(helm.helm_render("helm/vector", "helm/vector/values-x.yaml"),
                      _lib_engine.Ran)
    assert capture[-1] == {"cwd": None, "cmd": [
        "docker", "run", "--rm", "-v", f"{REPO}:/repo", helm.HELM_IMAGE,
        "template", "r", "/repo/helm/vector", "-f", "/repo/helm/vector/values-x.yaml",
        "--namespace", "lint-test"]}


def test_kube_linter_on_a_render_via_docker(monkeypatch, capture):
    _only(monkeypatch, "docker")
    assert isinstance(helm.kube_linter_lint("kind: x\n"), _lib_engine.Ran)
    cmd = capture[-1]["cmd"]
    scan = cmd[4].rsplit(":", 1)[0]          # the temp dir mounted at /scan
    assert cmd == ["docker", "run", "--rm", "-v", f"{scan}:/scan", helm.KUBE_LINTER_IMAGE,
                   "lint", "--format", "json", "--config", "/scan/.kube-linter.yaml",
                   "/scan/manifest.yaml"]
    assert capture[-1]["cwd"] is None


def test_kube_linter_on_k8s_via_docker(monkeypatch, capture):
    _only(monkeypatch, "docker")
    assert isinstance(k8s.kube_linter_lint_dir(), _lib_engine.Ran)
    assert capture[-1] == {"cwd": None, "cmd": [
        "docker", "run", "--rm", "-v", f"{REPO}:/repo", helm.KUBE_LINTER_IMAGE,
        "lint", "--format", "json", "--config", "/repo/.kube-linter.yaml", "/repo/k8s"]}


def test_hadolint_via_docker(monkeypatch, capture):
    _only(monkeypatch, "docker")
    assert isinstance(vibe.run_hadolint(["a/Dockerfile"]), _lib_engine.Ran)
    assert capture[-1] == {"cwd": None, "cmd": [
        "docker", "run", "--rm", "-v", f"{REPO}:/repo", "-w", "/repo",
        "--entrypoint", "hadolint", vibe.HADOLINT_IMAGE,
        "--config", ".hadolint.yaml", "--no-fail", "--format", "json", "a/Dockerfile"]}


def test_pint_via_docker(monkeypatch, capture, capsys):
    _only(monkeypatch, "docker")
    monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
    assert pint.main() == 0
    assert capture[-1] == {"cwd": None, "cmd": [
        "docker", "run", "--rm", "-v", f"{REPO}:/work", "-w", "/work",
        "--entrypoint", "pint", pint.PINT_IMAGE, *pint._PINT_ARGS]}


# ── binary on PATH ───────────────────────────────────────────────────────────

def test_helm_template_binary(monkeypatch, capture):
    _only(monkeypatch, "helm")
    helm.helm_render("helm/vector", None)
    assert capture[-1] == {"cwd": REPO, "cmd": [
        "/opt/bin/helm", "template", "r", f"{REPO}/helm/vector",
        "--namespace", "lint-test"]}


def test_kube_linter_on_a_render_binary(monkeypatch, capture):
    _only(monkeypatch, "kube-linter")
    helm.kube_linter_lint("kind: x\n")
    cmd = capture[-1]["cmd"]
    td = cmd[-1].rsplit("/", 1)[0]
    assert cmd == ["/opt/bin/kube-linter", "lint", "--format", "json",
                   "--config", f"{td}/.kube-linter.yaml", f"{td}/manifest.yaml"]


def test_kube_linter_on_k8s_binary(monkeypatch, capture):
    _only(monkeypatch, "kube-linter")
    k8s.kube_linter_lint_dir()
    assert capture[-1] == {"cwd": REPO, "cmd": [
        "/opt/bin/kube-linter", "lint", "--format", "json",
        "--config", f"{REPO}/.kube-linter.yaml", f"{REPO}/k8s"]}


def test_hadolint_binary(monkeypatch, capture):
    _only(monkeypatch, "hadolint")
    vibe.run_hadolint(["a/Dockerfile"])
    assert capture[-1] == {"cwd": REPO, "cmd": [
        "/opt/bin/hadolint", "--config", f"{REPO}/.hadolint.yaml",
        "--no-fail", "--format", "json", "a/Dockerfile"]}


def test_pint_binary(monkeypatch, capture, capsys):
    _only(monkeypatch, "pint")
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert pint.main() == 0
    assert capture[-1] == {"cwd": REPO, "cmd": ["/opt/bin/pint", *pint._PINT_ARGS]}
