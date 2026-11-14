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

hadolint's command has since changed on purpose (#3643): its levels and
trusted registries are flags generated from the wrapper's registry, the
config is a temp file holding ``{}``, and a canary Dockerfile goes in on
stdin (``-``; ``docker run -i``). The registry's own import-time check is
pinned here too.
"""
from __future__ import annotations

import importlib.util
import json
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
#: What hadolint prints for the canary alone: every registered rule on stdin
#: (`-`) at its registered level — the run the #3643 proof accepts.
_HADOLINT_CANARY_REPORT = json.dumps([
    {"file": "-", "line": 1, "column": 1, "code": code, "level": level, "message": "canary"}
    for code, levels in vibe.hadolint_canary_expectation().items() for level in levels])
#: ⛔ The registry spelled out as flags, by hand: this literal is what turns a
#: changed level or registry in HADOLINT_RULES / HADOLINT_TRUSTED_REGISTRIES
#: red where no hadolint is installed (the Python Tests job).
_HADOLINT_REGISTRY_FLAGS = [
    "--error", "DL3007", "--warning", "DL3018", "--error", "DL3026",
    "--trusted-registry", "docker.io", "--trusted-registry", "gcr.io",
    "--trusted-registry", "ghcr.io"]


class _Calls(list):
    """The captured ``{cmd, cwd}`` rows, plus each call's full kwargs."""

    def __init__(self):
        super().__init__()
        self.kwargs: list[dict] = []


@pytest.fixture
def capture(monkeypatch):
    """Record every engine command; answer with output its proof accepts."""
    calls = _Calls()

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "cwd": kw.get("cwd")})
        calls.kwargs.append(kw)
        joined = " ".join(cmd)
        if "kube-linter" in joined:
            out, err = _KL_REPORT, ""
        elif "hadolint" in joined:
            out, err = _HADOLINT_CANARY_REPORT, ""
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
    cmd = capture[-1]["cmd"]
    cfg = cmd[5].removesuffix(":/hadolint-config:ro")   # the temp dir holding `{}`
    # -i: the canary is sent on stdin, which docker closes without it (#3643).
    assert capture[-1] == {"cwd": None, "cmd": [
        "docker", "run", "--rm", "-i", "-v", f"{cfg}:/hadolint-config:ro",
        "-v", f"{REPO}:/repo", "-w", "/repo",
        "--entrypoint", "hadolint", vibe.HADOLINT_IMAGE,
        "--config", "/hadolint-config/hadolint.yaml", "--disable-ignore-pragma",
        "--no-fail", "--format", "json", *_HADOLINT_REGISTRY_FLAGS, "a/Dockerfile", "-"]}
    assert capture.kwargs[-1]["input"] == vibe.hadolint_canary()


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
    monkeypatch.setenv("HADOLINT_IGNORE", "DL3007")
    monkeypatch.setenv("HADOLINT_OVERRIDE_INFO", "DL3007")
    monkeypatch.setenv("hadolint_trusted_registries", "untrusted.invalid")
    seen: dict = {}
    real_tmp = vibe.tempfile.TemporaryDirectory

    class _Tmp(real_tmp):
        """Reads the config file while it still exists (it is gone on return)."""

        def __exit__(self, *exc):
            seen["config"] = Path(self.name, "hadolint.yaml").read_text(encoding="utf-8")
            return super().__exit__(*exc)

    monkeypatch.setattr(vibe.tempfile, "TemporaryDirectory", _Tmp)
    assert isinstance(vibe.run_hadolint(["a/Dockerfile"]), _lib_engine.Ran)
    cmd = capture[-1]["cmd"]
    cfg = cmd[2]
    assert capture[-1] == {"cwd": REPO, "cmd": [
        "/opt/bin/hadolint", "--config", cfg, "--disable-ignore-pragma",
        "--no-fail", "--format", "json", *_HADOLINT_REGISTRY_FLAGS, "a/Dockerfile", "-"]}
    # The config is a temp file holding `{}` — not a repo file, not /dev/null,
    # not empty (both of which hadolint answers by dropping the config).
    assert not cfg.startswith(REPO) and seen["config"].strip() == "{}"
    kw = capture.kwargs[-1]
    assert kw["input"] == vibe.hadolint_canary()
    # No HADOLINT_* variable reaches the engine (they override the flags),
    # and the rest of the environment does.
    assert not [k for k in kw["env"] if k.upper().startswith("HADOLINT_")], kw["env"]
    assert kw["env"]["PATH"] == vibe.os.environ["PATH"]


def test_hadolint_canary_covers_every_registered_rule_and_registry():
    """The canary is built from the registry, so a new row cannot be left out
    of it: one instruction per rule, each under an ignore pragma for its own
    code, and one FROM per trusted registry."""
    canary = vibe.hadolint_canary().splitlines()
    assert canary[0] == "# hadolint global ignore=" + ",".join(vibe.HADOLINT_RULES)
    for code, rule in vibe.HADOLINT_RULES.items():
        at = canary.index(rule.canary)
        assert canary[at - 1] == f"# hadolint ignore={code}", canary
        assert canary.count(rule.canary) == 1
    for registry in vibe.HADOLINT_TRUSTED_REGISTRIES:
        assert [ln for ln in canary if ln.startswith(f"FROM {registry}/canary/")], canary
    assert set(vibe.hadolint_canary_expectation()) == set(vibe.HADOLINT_RULES)


@pytest.mark.parametrize("damage, why", [
    (lambda r: r.update(DL3007=vibe.HadolintRule("error", "error", "FROM a:latest")),
     "no row differs"),
    (lambda r: r.update(DL3007=vibe.HadolintRule("error", "warning", "")), "canary"),
    (lambda r: r.update(DL3007=vibe.HadolintRule("error", "warning", "FROM a\nRUN b")), "canary"),
    (lambda r: r.update(DL3007=vibe.HadolintRule("error", "warning", "# FROM a")), "canary"),
    (lambda r: r.update(DL3007=vibe.HadolintRule("fatal", "warning", "FROM a:latest")), "level"),
    (lambda r: r.pop("DL3026"), "DL3026"),
], ids=["no-non-default-row", "empty-canary", "two-line-canary", "comment-canary",
        "unknown-level", "no-dl3026-row"])
def test_a_registry_that_cannot_prove_itself_does_not_import(monkeypatch, damage, why):
    """The import-time check: every row has a one-instruction canary, some row
    is off hadolint's default (the evidence the level flags applied), and
    DL3026 is there to show the trusted registries applied."""
    rules = dict(vibe.HADOLINT_RULES)
    damage(rules)
    monkeypatch.setattr(vibe, "HADOLINT_RULES", rules)
    with pytest.raises(ValueError, match=why):
        vibe._check_hadolint_registry()
    monkeypatch.undo()
    vibe._check_hadolint_registry()   # the shipped registry passes


@pytest.mark.parametrize("name", [".hadolint.yaml", ".hadolint.yml"])
def test_the_repo_carries_no_hadolint_config(name):
    """The wrapper would not read it (it passes its own `{}`), so the file
    would only tell a reader, and a bare `hadolint`, something the gate does
    not enforce. The levels are HADOLINT_RULES (#3643)."""
    assert not (vibe.REPO_ROOT / name).exists(), (
        f"{name} is back at the repo root: put the level in HADOLINT_RULES in "
        "scripts/tools/lint/check_iac_vibe_rules.py and delete the file")


def _load_copy(monkeypatch, tmp_path, name, source):
    """Import *source* as a module of its own (dataclasses needs it in
    sys.modules while its body runs)."""
    copy = tmp_path / f"{name}.py"
    copy.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, copy)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_the_registry_check_runs_at_import(monkeypatch, tmp_path):
    """The check above is called when the module loads, not only by tests: a
    copy of the wrapper whose DL3018 row has no canary cannot be imported —
    and an unchanged copy can (the control: the copy route itself loads)."""
    src = Path(vibe.__file__).read_text(encoding="utf-8")
    row = 'canary="RUN apk add --no-cache curl"'
    assert src.count(row) == 1
    assert _load_copy(monkeypatch, tmp_path, "wrapper_copy", src).HADOLINT_RULES
    with pytest.raises(ValueError, match=r"HADOLINT_RULES\[DL3018\]: canary"):
        _load_copy(monkeypatch, tmp_path, "damaged_wrapper", src.replace(row, 'canary=""'))


def test_no_trusted_registry_does_not_import(monkeypatch):
    monkeypatch.setattr(vibe, "HADOLINT_TRUSTED_REGISTRIES", ())
    with pytest.raises(ValueError, match="at least one registry"):
        vibe._check_hadolint_registry()


def test_pint_binary(monkeypatch, capture, capsys):
    _only(monkeypatch, "pint")
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert pint.main() == 0
    assert capture[-1] == {"cwd": REPO, "cmd": ["/opt/bin/pint", *pint._PINT_ARGS]}
