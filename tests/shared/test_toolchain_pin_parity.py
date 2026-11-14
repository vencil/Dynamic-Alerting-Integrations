"""Toolchain pin parity — the dev container and CI run the SAME toolchain (#1337).

The per-tool parity gates that already existed (actionlint, vector, promtool,
golangci-lint) each bind one binary. Everything else drifted freely, and the
drift was measured, not hypothetical, before this gate:

  * ``node-version`` was 18 ×2, 20 ×3 and 24 ×1 across workflows, while the dev
    container asked for ``"lts"`` — a value that changes major on its own.
  * ``python-version`` was 3.13 almost everywhere, 3.11 and 3.12 once each, with
    no comment making either a deliberate floor test.
  * Trivy in CI was whatever trivy-action's *default* was (v0.69.3 on the
    2026-07 release run); ``make pre-tag`` used whatever was on PATH and the dev
    container had none — so the local and CI CVE lists came from different
    detection engines.
  * Seven of eight ``azure/setup-helm`` steps had no ``version:``; the action's
    ``latest`` had quietly carried them onto Helm 4 while the Container SAST
    job stayed on 3.16.4 and the dev container floated on its own ``latest``.
  * The same action appeared at two majors (setup-node v5/v6, setup-python
    v5/v6, upload-artifact v4/v7, github-script v7/v9).
  * CI jobs said ``ubuntu-latest``, the dev container ``ubuntu-22.04``.

Each test below states what must match what and derives both sides from the
files — never from a number written here — so bumping a pin in one place and
not the others is a red test, not a silent split.

⛔ Workflows are read with ``yaml.safe_load`` and walked as jobs/steps, not
grepped: a version string in a comment or a ``run:`` script is not a pin, and a
pin written with different quoting is still a pin. Every walker asserts it found
at least one site, so a refactor that moves the pins out of reach reads as a
failure here instead of as "nothing disagrees".

Pure file parsing — no network, no binaries.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from _tree import repo_files

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"
_DEVCONTAINER = _REPO / ".devcontainer" / "devcontainer.json"
_INSTALL_TRIVY = _REPO / ".devcontainer" / "install-trivy.sh"
_MAKEFILE = _REPO / "Makefile"
_IAC_HELM = _REPO / "scripts" / "tools" / "lint" / "check_iac_helm.py"
_SESSION_START = _REPO / ".claude" / "hooks" / "session-start.sh"
_PRECOMMIT = _REPO / ".pre-commit-config.yaml"
_CONSTRAINTS = _REPO / "requirements" / "ci-constraints.txt"
_PYTHON_VERSION_FILE = _REPO / ".python-version"

_RUNNER = "ubuntu-24.04"

# Devcontainer features allowed to float, each with the reason. Anything not
# listed here that says "latest"/"lts"/"stable" is a red test.
_FLOATING_ALLOWED = {
    "docker-in-docker": "resolved against the base distro's moby apt repo; CI's "
                        "docker comes from the runner image — neither side is pinned",
}


# ── readers ─────────────────────────────────────────────────────────────────


def _workflow_files() -> list[Path]:
    files = sorted(list(_WORKFLOWS.glob("*.yml")) + list(_WORKFLOWS.glob("*.yaml")))
    assert files, f"no workflow files under {_WORKFLOWS}"
    return files


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    return data


def _jobs() -> list[tuple[str, str, dict]]:
    """(workflow file name, job id, job mapping) for every job in every workflow."""
    out = []
    for p in _workflow_files():
        for job_id, job in (_load(p).get("jobs") or {}).items():
            assert isinstance(job, dict), f"{p.name}:{job_id} is not a mapping"
            out.append((p.name, job_id, job))
    assert out, "no jobs found in any workflow"
    return out


def _steps() -> list[tuple[str, str, dict, dict]]:
    """(file, job id, job, step) for every step that has a `uses:`."""
    out = []
    for fname, job_id, job in _jobs():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "uses" in step:
                out.append((fname, job_id, job, step))
    assert out, "no `uses:` steps found in any workflow"
    return out


def _action_steps(action: str) -> list[tuple[str, str, dict, dict]]:
    found = [s for s in _steps() if str(s[3]["uses"]).split("@", 1)[0] == action]
    assert found, (
        f"no `{action}` step found in any workflow — the pin this test binds "
        f"moved out of reach (renamed action? composite?). Re-point the test; "
        f"do not delete it.")
    return found


def _devcontainer() -> dict:
    # Same JSONC rule as scripts/tools/lint/check_devcontainer_dep_parity.py:
    # only `//` that starts a line, so URLs inside strings survive.
    text = re.sub(r"^[ \t]*//.*$", "", _DEVCONTAINER.read_text(encoding="utf-8"),
                  flags=re.MULTILINE)
    return json.loads(text)


def _feature(name: str) -> dict:
    feats = _devcontainer()["features"]
    hits = [v for k, v in feats.items() if k.rsplit("/", 1)[-1].split(":")[0] == name]
    assert len(hits) == 1, f"expected exactly one `{name}` feature in devcontainer.json, got {len(hits)}"
    return hits[0]


def _one(pattern: str, text: str, what: str) -> str:
    matches = re.findall(pattern, text, re.MULTILINE)
    assert len(matches) == 1, f"expected exactly one {what}, found {len(matches)}: {matches}"
    return matches[0]


# ── runner OS ───────────────────────────────────────────────────────────────


def test_every_job_runs_on_the_pinned_runner_and_the_container_matches() -> None:
    """`ubuntu-latest` moves to the next LTS without a diff; the dev container
    would then silently be a different OS from CI. Both sides are explicit."""
    image = _devcontainer()["image"]
    assert image.endswith(f":{_RUNNER}"), (
        f"devcontainer.json base image {image!r} is not the CI runner OS "
        f"({_RUNNER}). Move both together.")
    bad = [f"{f}:{j} runs-on={job.get('runs-on')!r}" for f, j, job in _jobs()
           if "uses" not in job and job.get("runs-on") != _RUNNER]
    assert not bad, (
        f"jobs not on {_RUNNER} (the dev container's OS):\n  " + "\n  ".join(bad))


# ── Node ────────────────────────────────────────────────────────────────────


def test_node_version_is_one_value_and_matches_the_dev_container() -> None:
    dc = str(_feature("node")["version"])
    assert re.fullmatch(r"\d+", dc), (
        f"devcontainer node feature version is {dc!r}; it must be a major number. "
        f"'lts'/'latest' change major on their own while CI stays put.")
    seen = {}
    for f, j, _job, step in _action_steps("actions/setup-node"):
        with_ = step.get("with") or {}
        v = with_.get("node-version")
        assert v is not None and "node-version-file" not in with_, (
            f"{f}:{j} setup-node has no literal node-version — this gate cannot "
            f"compare it. Pin a literal major.")
        seen[f"{f}:{j}"] = str(v)
    bad = {k: v for k, v in seen.items() if v != dc}
    assert not bad, (
        f"node-version must equal the dev container's Node {dc} everywhere; "
        f"drifted: {bad}")


_NODE_IMAGE_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:docker\.io/)?(?:library/)?node:(\S+)",
    re.MULTILINE | re.IGNORECASE)


def test_every_node_image_is_the_dev_container_major() -> None:
    """`FROM node:<tag>` builds the shipped portal bundles (da-portal's
    `portal-build` stage, #3738) — the same esbuild run `make portal-build` does
    in the dev container and CI's setup-node does. A different Node major there
    is a different npm resolving the lockfile, which is how a shared chunk got
    re-hashed before (the reason CI pins setup-node to the dev container's major).
    The tag must start with `<major>-` or `<major>.` (or be exactly the major);
    a digest is allowed after it, a floating `lts`/`alpine` is not."""
    dc = str(_feature("node")["version"])
    sites, bad = [], []
    for path in repo_files("Dockerfile"):
        rel = path.relative_to(_REPO).as_posix()
        for ref in _NODE_IMAGE_RE.findall(path.read_text(encoding="utf-8")):
            tag = ref.split("@", 1)[0]
            sites.append(rel)
            if not (tag == dc or tag.startswith((dc + "-", dc + "."))):
                bad.append(f"{rel}: node:{ref}")
    assert sites, ("no `FROM node:` in any Dockerfile — da-portal's portal-build "
                   "stage left the walker's reach; re-derive this test's floor")
    assert not bad, (
        f"node images must be the dev container's Node {dc}:\n  " + "\n  ".join(bad))


# ── Python ──────────────────────────────────────────────────────────────────
#
# ⛔ ONE source: `.python-version` (exact patch). Every consumer either READS it
# (setup-python's `python-version-file`) or must spell the same value where the
# consumer needs a literal (devcontainer feature, `FROM python:<tag>` /
# `image: python:<tag>` — Renovate's docker manager only bumps literals, and its
# pyenv manager bumps `.python-version` from the same docker datasource, so
# renovate.json groups them into one PR).
#
# Why exact patch and not "3.13": a minor-only pin let setup-python pick the
# runner's newest patch, so one CI run had shards on 3.13.17 and 3.13.18 while
# the images shipped 3.13.15 — three Pythons, each judged by a different gate.


def _python_version() -> str:
    raw = _PYTHON_VERSION_FILE.read_text(encoding="utf-8")
    v = raw.strip()
    assert raw == v + "\n" and re.fullmatch(r"3\.\d+\.\d+", v), (
        f".python-version must be exactly one line `3.X.Y`, got {raw!r}")
    return v


def test_every_setup_python_reads_the_version_file() -> None:
    """A literal `python-version:` is a second copy of the version. setup-python
    lets `python-version` win over `python-version-file`, so both together is
    still a second copy."""
    _python_version()
    bad = []
    for f, j, _job, step in _action_steps("actions/setup-python"):
        w = step.get("with") or {}
        if "python-version" in w:
            bad.append(f"{f}:{j} has python-version={w['python-version']!r}")
        if w.get("python-version-file") != ".python-version":
            bad.append(f"{f}:{j} python-version-file={w.get('python-version-file')!r}")
    assert not bad, (
        "every actions/setup-python must read `python-version-file: .python-version` "
        "and carry no `python-version`:\n  " + "\n  ".join(bad))


def test_the_dev_container_python_is_the_version_file() -> None:
    """A bare minor here is resolved to the newest patch at container build
    time — a different interpreter than CI's as soon as upstream ships one."""
    dc = str(_feature("python")["version"])
    want = _python_version()
    assert dc == want, f"devcontainer python {dc!r} != .python-version {want!r}"


_PY_IMAGE_RE = re.compile(
    r"^\s*(?:FROM\s+(?:--platform=\S+\s+)?|-?\s*image:\s*[\"']?)"
    r"(?:docker\.io/)?(?:library/)?python:([^\s\"'@]+)", re.MULTILINE)


def test_every_python_image_is_the_version_file() -> None:
    """`FROM python:<tag>` in every Dockerfile and `image: python:<tag>` in every
    YAML (compose stubs and doc examples included — each is a copy someone runs)
    must carry the .python-version patch as its version part."""
    want = _python_version()
    sites, bad = [], []
    for path in repo_files("Dockerfile", ".yaml", ".yml"):
        for tag in _PY_IMAGE_RE.findall(path.read_text(encoding="utf-8")):
            rel = path.relative_to(_REPO).as_posix()
            sites.append(rel)
            if tag != want and not tag.startswith(want + "-"):
                bad.append(f"{rel}: python:{tag}")
    assert sites, "no python image found anywhere — the walker lost its sites"
    assert not bad, (
        f"python image tags must start with .python-version {want!r}:\n  " + "\n  ".join(bad))


_PIP_PIN_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\&;]+)")


def _constraints() -> dict[str, str]:
    out = {}
    for line in _CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line.strip())
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    assert out, "requirements/ci-constraints.txt yielded no pins"
    return out


def test_image_pip_pins_are_the_ci_constraints() -> None:
    """An image's Python deps are either installed with `-c` against the CI
    constraints file (preferred: nothing to keep in step) or, where the build
    context cannot see `requirements/`, pinned to the SAME version. The literal
    form drifted before: promql-parser 0.7.0 in da-tools while CI tested 0.10.0,
    because nothing bumps a `RUN pip install` line."""
    want = _constraints()
    seen, bad = 0, []
    for path in repo_files("Dockerfile"):
        rel = path.relative_to(_REPO).as_posix()
        for name, ver in _PIP_PIN_RE.findall(path.read_text(encoding="utf-8")):
            key = re.sub(r"[-_.]+", "-", name).lower()
            seen += 1
            if want.get(key) != ver:
                bad.append(f"{rel}: {name}=={ver} (ci-constraints: {want.get(key)!r})")
    assert seen, "no literal pip pin left in any Dockerfile — drop this test's floor deliberately"
    assert not bad, "Dockerfile pip pins must equal requirements/ci-constraints.txt:\n  " + "\n  ".join(bad)


# ── Trivy ───────────────────────────────────────────────────────────────────


def test_trivy_binary_is_pinned_and_identical_in_ci_makefile_and_container() -> None:
    """Different trivy versions = different detection engines = local and CI CVE
    lists that disagree about the same image. The action's default binary moves
    with every action bump, so each step must pin `version:` itself."""
    ci = {}
    for f, j, _job, step in _action_steps("aquasecurity/trivy-action"):
        v = (step.get("with") or {}).get("version")
        assert v, (f"{f}:{j} trivy-action has no `version:` — it runs the action's "
                   f"default binary, which nothing here pins.")
        ci[f"{f}:{j}"] = str(v)
    assert len(set(ci.values())) == 1, f"trivy `version:` differs between CI steps: {ci}"
    ci_ver = next(iter(ci.values()))

    mk = _one(r"^TRIVY_VERSION\s*:?=\s*(\S+)\s*$",
              _MAKEFILE.read_text(encoding="utf-8"), "Makefile TRIVY_VERSION")
    dc = _one(r"^TRIVY_VERSION=(\S+)\s*$",
              _INSTALL_TRIVY.read_text(encoding="utf-8"), "install-trivy.sh TRIVY_VERSION")
    assert ci_ver == f"v{mk}" == f"v{dc}", (
        f"trivy skew: CI {ci_ver}, Makefile {mk}, install-trivy.sh {dc}. "
        f"Bump all three together (and install-trivy.sh's TRIVY_SHA256).")
    _one(r"^TRIVY_SHA256=([0-9a-f]{64})\s*$",
         _INSTALL_TRIVY.read_text(encoding="utf-8"), "install-trivy.sh TRIVY_SHA256")
    assert "install-trivy.sh" in _devcontainer()["postCreateCommand"], (
        "install-trivy.sh exists but postCreateCommand never runs it — the "
        "container would still have no trivy.")


# ── Helm ────────────────────────────────────────────────────────────────────


def _runs_iac_helm_wrapper(job: dict) -> bool:
    return any("check_iac_helm.py" in str(s.get("run", ""))
               for s in job.get("steps") or [] if isinstance(s, dict))


def test_helm_is_pinned_everywhere_and_matches_the_dev_container() -> None:
    """Every setup-helm pins a version. Jobs that run check_iac_helm.py must use
    that wrapper's own HELM_VERSION (its Docker fallback is digest-pinned to it);
    every other job uses the dev container's helm."""
    dc = str(_feature("kubectl-helm-minikube")["helm"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", dc), (
        f"devcontainer helm is {dc!r}; pin an unprefixed x.y.z (the feature adds `v`).")
    wrapper = _one(r'^HELM_VERSION\s*=\s*"([0-9.]+)"',
                   _IAC_HELM.read_text(encoding="utf-8"), "check_iac_helm.py HELM_VERSION")

    bad = []
    wrapper_jobs = 0
    for f, j, job, step in _action_steps("azure/setup-helm"):
        v = (step.get("with") or {}).get("version")
        if not v or str(v) == "latest":
            bad.append(f"{f}:{j} setup-helm version={v!r} (the action's `latest` is "
                       f"how CI crossed Helm 3→4 unannounced)")
            continue
        want = wrapper if _runs_iac_helm_wrapper(job) else dc
        wrapper_jobs += _runs_iac_helm_wrapper(job)
        if str(v) != f"v{want}":
            bad.append(f"{f}:{j} setup-helm {v} ≠ v{want}")
    assert wrapper_jobs, (
        "no setup-helm job runs check_iac_helm.py — the wrapper-pin branch of "
        "this test is dead; re-derive which job renders for the wrapper.")
    assert not bad, "helm pin drift:\n  " + "\n  ".join(bad)


# ── remote-session bootstrap ────────────────────────────────────────────────


@pytest.mark.parametrize("pin,feature,option", [
    ("HELM", "kubectl-helm-minikube", "helm"),
    ("GOLANGCI_LINT", "go", "golangciLintVersion"),
])
def test_session_start_installs_the_dev_container_version(pin: str, feature: str,
                                                          option: str) -> None:
    """The remote-session bootstrap installs helm and golangci-lint itself
    (#2955). Behind the dev container it would bring back the split this file
    exists to stop — a test run on a different helm, a lint run that cannot
    read the config — in exactly the sessions that have no dev container."""
    src = _SESSION_START.read_text(encoding="utf-8")
    ver = _one(rf"^{pin}_VERSION=(\S+)\s*$", src, f"session-start.sh {pin}_VERSION")
    _one(rf"^{pin}_SHA256=([0-9a-f]{{64}})\s*$", src, f"session-start.sh {pin}_SHA256")
    dc = str(_feature(feature)[option])
    assert ver == dc, (
        f"session-start.sh {pin}_VERSION={ver} but devcontainer.json "
        f"{feature}.{option}={dc}. Bump both, and re-verify {pin}_SHA256 against "
        f"the upstream checksum for the new tarball.")
    assert f"${{{pin}_VERSION}}" in src and f'"${pin}_SHA256"' in src, (
        f"session-start.sh no longer builds its download from {pin}_VERSION and "
        f"checks {pin}_SHA256 — the pin this test binds is not what gets installed.")


def test_session_start_node_major_is_the_dev_container_major() -> None:
    """session-start pins an exact node release so the download can be
    verified (#3611); the SSOT is still the dev container's MAJOR, and this is
    what keeps the two from splitting."""
    src = _SESSION_START.read_text(encoding="utf-8")
    ver = _one(r"^NODE_VERSION=(\d+\.\d+\.\d+)\s*$", src, "session-start.sh NODE_VERSION")
    _one(r"^NODE_SHA256=([0-9a-f]{64})\s*$", src, "session-start.sh NODE_SHA256")
    dc = str(_feature("node")["version"])
    assert ver.split(".")[0] == dc, (
        f"session-start.sh NODE_VERSION={ver} but the dev container's node is {dc}. "
        f"Pin a {dc}.x release and its SHA-256 from nodejs.org's SHASUMS256.txt.")
    assert "v${NODE_VERSION}" in src and '"$NODE_SHA256"' in src, (
        "session-start.sh no longer builds its node download from NODE_VERSION and "
        "checks NODE_SHA256 — the pin this test binds is not what gets installed.")


# ── CI binary vs lint-wrapper Docker fallback ──────────────────────────────


@pytest.mark.parametrize("ci_var,wrapper,const", [
    ("HADOLINT_VERSION", "check_iac_vibe_rules.py", "HADOLINT_VERSION"),
    ("KL", "check_iac_helm.py", "KUBE_LINTER_VERSION"),
])
def test_ci_lint_binary_matches_the_wrapper_fallback(ci_var: str, wrapper: str, const: str) -> None:
    """ci.yml installs the binary; the wrapper falls back to a digest-pinned
    image of ITS OWN version when the binary is missing (Windows hosts, a failed
    CI download). Two versions = findings that depend on which path ran. The
    comments in ci.yml said "pin matches"; nothing checked it until now."""
    ci = set(re.findall(rf"^\s*{ci_var}=(v[0-9][^\s\"'#]*)",
                        (_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"), re.MULTILINE))
    assert len(ci) == 1, f"expected one ci.yml {ci_var} value, got {sorted(ci)}"
    src = (_REPO / "scripts" / "tools" / "lint" / wrapper).read_text(encoding="utf-8")
    pinned = _one(rf'^{const}\s*=\s*"([^"]+)"', src, f"{wrapper} {const}")
    assert ci == {pinned}, (
        f"ci.yml {ci_var}={ci.pop()} but {wrapper} {const}={pinned}. Bump both "
        f"(and the wrapper's image digest) together.")


_VIBE_RULES = _REPO / "scripts" / "tools" / "lint" / "check_iac_vibe_rules.py"


def test_ci_hadolint_checksum_matches_the_wrapper() -> None:
    """The wrapper is the SSOT for the hadolint binary's checksum as well as its
    version (#3627): the remote-session bootstrap reads it from there. ci.yml
    still writes its own copy in the install step; it must be the same bytes,
    or CI and a remote session verify different binaries."""
    ci = set(re.findall(r"^\s*HADOLINT_SHA256=([0-9a-f]{64})\s*$",
                        (_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"), re.MULTILINE))
    assert len(ci) == 1, f"expected one ci.yml HADOLINT_SHA256 value, got {sorted(ci)}"
    pinned = _one(r'^HADOLINT_SHA256\s*=\s*"([0-9a-f]{64})"\s*$',
                  _VIBE_RULES.read_text(encoding="utf-8"),
                  "check_iac_vibe_rules.py HADOLINT_SHA256")
    assert ci == {pinned}, (
        f"ci.yml HADOLINT_SHA256={ci.pop()} but check_iac_vibe_rules.py has {pinned}. "
        f"Re-verify the upstream checksum of hadolint-Linux-x86_64 and write it in both.")


def test_session_start_reads_the_hadolint_pin_from_the_wrapper() -> None:
    """session-start installs hadolint (#3627) from the wrapper's pin read at
    run time, so it has no copy to fall behind. This binds that shape: it names
    the wrapper as its pin source, reads both constants out of it, and assigns
    neither a literal of its own (a copy written later would silently win)."""
    src = _SESSION_START.read_text(encoding="utf-8")
    pin_src = _one(r"^HADOLINT_PIN_SRC=(\S+)\s*$", src, "session-start.sh HADOLINT_PIN_SRC")
    assert _REPO / pin_src == _VIBE_RULES, (
        f"session-start.sh reads the hadolint pin from {pin_src}, not from the wrapper "
        f"that owns it ({_VIBE_RULES.relative_to(_REPO)}).")
    for name in ("HADOLINT_VERSION", "HADOLINT_SHA256"):
        assigns = re.findall(rf"^\s*{name}=(.*)$", src, re.MULTILINE)
        assert assigns == [f'"$(py_pin {name} "$HADOLINT_PIN_SRC")"'], (
            f"session-start.sh must set {name} only from the wrapper "
            f"(`py_pin {name} \"$HADOLINT_PIN_SRC\"`); found {assigns}.")
    assert '${HADOLINT_VERSION}' in src and '"$HADOLINT_SHA256"' in src, (
        "session-start.sh no longer builds its hadolint download from HADOLINT_VERSION "
        "and checks HADOLINT_SHA256 — the pin this test binds is not what gets installed.")


@pytest.mark.parametrize("ci_var,wrapper,const", [
    ("KL_SHA256", "check_iac_helm.py", "KUBE_LINTER_SHA256"),
    ("PINT_SHA256", "check_pint.py", "PINT_SHA256"),
])
def test_ci_lint_checksum_matches_the_wrapper(ci_var: str, wrapper: str, const: str) -> None:
    """kube-linter and pint as hadolint (#3611): the wrapper owns the release
    tarball's checksum, session-start reads it from there, and ci.yml's own
    copy in its install step must be the same bytes — or CI and a remote
    session verify different archives."""
    ci = set(re.findall(rf"^\s*{ci_var}=([0-9a-f]{{64}})\s*$",
                        (_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"), re.MULTILINE))
    assert len(ci) == 1, f"expected one ci.yml {ci_var} value, got {sorted(ci)}"
    src = (_REPO / "scripts" / "tools" / "lint" / wrapper).read_text(encoding="utf-8")
    pinned = _one(rf'^{const}\s*=\s*"([0-9a-f]{{64}})"\s*$', src, f"{wrapper} {const}")
    assert ci == {pinned}, (
        f"ci.yml {ci_var}={ci.pop()} but {wrapper} {const}={pinned}. Re-verify the "
        f"upstream checksum and write it in both.")


@pytest.mark.parametrize("src_var,owner,reader,names", [
    ("KUBE_LINTER_PIN_SRC", "scripts/tools/lint/check_iac_helm.py", "py_pin",
     (("KUBE_LINTER_VERSION", "KUBE_LINTER_VERSION"), ("KUBE_LINTER_SHA256", "KUBE_LINTER_SHA256"))),
    ("PINT_PIN_SRC", "scripts/tools/lint/check_pint.py", "py_pin",
     (("PINT_VERSION", "PINT_VERSION"), ("PINT_SHA256", "PINT_SHA256"))),
    ("PROMTOOL_PIN_SRC", ".devcontainer/install-promtool.sh", "sh_pin",
     (("PROMTOOL_VERSION", "PROM_VERSION"), ("PROMTOOL_SHA256", "PROM_SHA256"))),
])
def test_session_start_reads_owned_pins_from_their_ssot(src_var, owner, reader, names) -> None:
    """kube-linter, pint and promtool (#3611), as hadolint: session-start names
    the file that owns each pin, reads both values out of it, and assigns no
    literal of its own (a copy written later would silently win)."""
    src = _SESSION_START.read_text(encoding="utf-8")
    pin_src = _one(rf"^{src_var}=(\S+)\s*$", src, f"session-start.sh {src_var}")
    assert pin_src == owner, f"session-start.sh reads {src_var} from {pin_src}, not {owner}"
    for var, const in names:
        assigns = re.findall(rf"^\s*{var}=(.*)$", src, re.MULTILINE)
        assert assigns == [f'"$({reader} {const} "${src_var}")"'], (
            f"session-start.sh must set {var} only from {owner} "
            f"(`{reader} {const} \"${src_var}\"`); found {assigns}.")
        assert f"${var}" in src or f"${{{var}}}" in src


# ── kubectl / kind / no floating devcontainer features ─────────────────────


def test_devcontainer_features_do_not_float() -> None:
    """A feature at "latest"/"lts" is whatever was current the day the image was
    built — two contributors, two toolchains, one devcontainer.json."""
    floating = []
    for key, opts in _devcontainer()["features"].items():
        name = key.rsplit("/", 1)[-1].split(":")[0]
        if name in _FLOATING_ALLOWED:
            continue
        for opt, val in (opts or {}).items():
            if str(val).lower() in {"latest", "lts", "stable", "current"}:
                floating.append(f"{name}.{opt}={val!r}")
    assert not floating, (
        "floating versions in devcontainer.json features: " + ", ".join(floating)
        + ". Pin them, or add the feature to _FLOATING_ALLOWED with the reason.")


def test_kubectl_is_pinned_through_the_option_the_feature_actually_reads() -> None:
    """In kubectl-helm-minikube, `version` IS kubectl; a `kubectl` key is not an
    option and is silently ignored (an earlier revision pinned it there and got
    `latest`)."""
    feat = _feature("kubectl-helm-minikube")
    assert "kubectl" not in feat, (
        "`kubectl` is not an option of the kubectl-helm-minikube feature — it is "
        "ignored. Pin kubectl through `version`.")
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(feat.get("version"))), (
        f"kubectl (feature `version`) is {feat.get('version')!r}; pin an x.y.z.")


# ── kind node image (= the Kubernetes version of every local cluster) ────────

_KIND_NODE_ENV = _REPO / "scripts" / "ops" / "kind-node-image.env"
_KIND_NODE_RE = r"kindest/node:v(\d+)\.(\d+)\.(\d+)@sha256:[0-9a-f]{64}"
# Where a cluster can be created from. Markdown is out of scope: runbooks show
# commands to a reader, they do not create the clusters `make setup` or CI use.
_KIND_CREATE_SURFACES = (".sh", ".bash", "Makefile", ".mk", ".yml", ".yaml", ".py", ".ps1")
_KIND_CREATE = re.compile(
    r"\bkind\s+create\s+cluster\b"                                # shell
    r"|[\"']kind[\"']\s*,\s*[\"']create[\"']\s*,\s*[\"']cluster[\"']")   # argv list
_KIND_IMAGE_ARG = re.compile(r'--image[ =]"?\$\{?KIND_NODE_IMAGE\b(?![A-Za-z0-9_])\}?"?')
_SOURCES_SSOT = re.compile(r"^\s*(source|\.)\s+[^#\n]*kind-node-image\.env\b", re.MULTILINE)


def _kind_node_image() -> str:
    """The value as bash sees it after `source`, in an empty environment, so an
    exported KIND_NODE_IMAGE cannot stand in for a missing line in the file."""
    import subprocess
    out = subprocess.run(
        ["env", "-i", "bash", "--noprofile", "--norc", "-c",
         'set -eu; source "$1"; printf %s "${KIND_NODE_IMAGE:-}"', "_", str(_KIND_NODE_ENV)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    assert out, f"{_KIND_NODE_ENV} does not set KIND_NODE_IMAGE"
    return out


def _kind_node_minor() -> tuple[int, int]:
    m = re.fullmatch(_KIND_NODE_RE, _kind_node_image())
    assert m, f"KIND_NODE_IMAGE must be kindest/node:vX.Y.Z@sha256:<64 hex>, got {_kind_node_image()!r}"
    return int(m.group(1)), int(m.group(2))


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations, so a command split over lines is one line."""
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def test_kind_node_image_is_digest_pinned() -> None:
    """A tag alone can be re-pushed; without an image at all the cluster runs
    whatever the installed kind binary defaults to (#1018)."""
    _kind_node_minor()


def test_every_kind_cluster_uses_the_pinned_node_image() -> None:
    """Each `kind create cluster` (shell, or an argv list in Python) passes
    `--image "${KIND_NODE_IMAGE}"`, and its file sources the SSOT on a line of
    its own. A workflow using helm/kind-action must pass `node_image` from
    KIND_NODE_IMAGE. Lines that only print the command (echo/printf) and
    comments are not cluster creations.

    Limit: this reads text. A command assembled from variables
    (`K=kind; $K create cluster`) is not recognised."""
    sites, bad = [], []
    for f in repo_files(*_KIND_CREATE_SURFACES):
        if f.resolve() == Path(__file__).resolve():
            continue  # this guard names the pattern it looks for
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = f.relative_to(_REPO)
        if f.suffix in (".yml", ".yaml") and "kind-action" in text:
            data = yaml.safe_load(text)
            for job in (data or {}).get("jobs", {}).values() if isinstance(data, dict) else []:
                for step in (job or {}).get("steps", []) or []:
                    if "kind-action" in str(step.get("uses", "")):
                        sites.append(f"{rel} (kind-action)")
                        if "KIND_NODE_IMAGE" not in str((step.get("with") or {}).get("node_image", "")):
                            bad.append(f"{rel}: helm/kind-action step without node_image from KIND_NODE_IMAGE")
        hits = [ln for ln in _logical_lines(text)
                if _KIND_CREATE.search(ln)
                and not ln.lstrip().startswith(("#", "echo ", "printf ", "//"))]
        if not hits:
            continue
        sites.append(str(rel))
        if f.suffix != ".py" and not _SOURCES_SSOT.search(text):
            bad.append(f"{rel}: no `source .../kind-node-image.env` line")
        for ln in hits:
            if f.suffix == ".py":
                if "KIND_NODE_IMAGE" not in ln:
                    bad.append(f"{rel}: {ln.strip()}  (no KIND_NODE_IMAGE in the argv)")
            elif not _KIND_IMAGE_ARG.search(ln):
                bad.append(f"{rel}: {ln.strip()}  (no --image \"${{KIND_NODE_IMAGE}}\")")
    assert sites, "found no kind cluster creation at all: the scan is broken, not the repo clean"
    assert not bad, "kind clusters not on the pinned node image:\n  " + "\n  ".join(bad)


def test_kubectl_is_within_one_minor_of_the_node_image() -> None:
    """Kubernetes version-skew policy: kubectl is supported within one minor of
    the API server. The API server is the kind node image."""
    major, minor = _kind_node_minor()
    kubectl = str(_feature("kubectl-helm-minikube").get("version"))
    km = re.fullmatch(r"(\d+)\.(\d+)\.\d+", kubectl)
    assert km, f"kubectl version {kubectl!r} is not x.y.z"
    assert int(km.group(1)) == major and abs(int(km.group(2)) - minor) <= 1, (
        f"kubectl {kubectl} is more than one minor away from the kind node image "
        f"v{major}.{minor} ({_KIND_NODE_ENV.relative_to(_REPO)})")


def test_kind_is_pinned_and_checksum_verified() -> None:
    cmd = _devcontainer()["postCreateCommand"]
    urls = re.findall(r"kind/releases/download/(v\d+\.\d+\.\d+)/kind-linux-amd64", cmd)
    assert len(urls) == 1, f"expected one pinned kind download in postCreateCommand, got {urls}"
    assert re.search(r"[0-9a-f]{64}\s+\./kind'\s*\|\s*sha256sum -c -", cmd), (
        "the kind download is not sha256-verified before install")


# ── one version per action ──────────────────────────────────────────────────


def test_each_action_is_used_at_a_single_version() -> None:
    """Two majors of the same action = two behaviours, and the difference shows
    up as 'this workflow does X, that one does Y' during an incident."""
    refs: dict[str, set[str]] = defaultdict(set)
    where: dict[str, list[str]] = defaultdict(list)
    for f, j, _job, step in _steps():
        uses = str(step["uses"])
        if uses.startswith(("./", "docker://")) or "@" not in uses:
            continue
        action, ref = uses.split("@", 1)
        refs[action].add(ref)
        where[action].append(f"{f}:{j}@{ref}")
    assert refs, "no versioned `uses:` found"
    split = {a: sorted(r) for a, r in refs.items() if len(r) > 1}
    assert not split, (
        "actions used at more than one version:\n  "
        + "\n  ".join(f"{a}: {r}  ({', '.join(where[a])})" for a, r in split.items()))


# ── pre-commit hook envs ↔ ci-constraints ──────────────────────────────────


def _pip_name(name: str) -> str:
    """PEP 503 normalisation: `PyYAML`, `pyyaml`, `py_yaml` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _constraint_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(\S+)", line)
        if m:
            pins[_pip_name(m.group(1))] = m.group(2)
    assert pins, f"no `name==version` pin read from {_CONSTRAINTS}"
    return pins


def test_precommit_hook_dependency_pins_match_ci_constraints() -> None:
    """#3483: a hook's isolated env installs its `additional_dependencies`
    itself, outside `-c requirements/ci-constraints.txt`. A hook dep pinned
    with `==` whose package ci-constraints also pins must be the same
    version, or the hook and CI's pytest of the same lint run two parsers."""
    pins = _constraint_pins()
    config = _load(_PRECOMMIT)
    compared: list[str] = []
    split: list[str] = []
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            for dep in hook.get("additional_dependencies", []) or []:
                m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*(\S+)",
                                 str(dep).strip())
                if not m or _pip_name(m.group(1)) not in pins:
                    continue
                name, ver = _pip_name(m.group(1)), m.group(2)
                compared.append(f"{hook['id']}: {name}")
                if ver != pins[name]:
                    split.append(f"{hook['id']}: {name}=={ver} "
                                 f"(ci-constraints: {pins[name]})")
    assert compared, ("no `==`-pinned additional_dependencies shared with "
                      "ci-constraints found — the walker no longer reaches them")
    assert not split, ("pre-commit hook deps disagree with "
                       "requirements/ci-constraints.txt:\n  " + "\n  ".join(split))


@pytest.mark.parametrize("path", [_DEVCONTAINER, _INSTALL_TRIVY, _MAKEFILE, _IAC_HELM,
                                  _SESSION_START, _PRECOMMIT, _CONSTRAINTS, _KIND_NODE_ENV])
def test_inputs_exist(path: Path) -> None:
    assert path.is_file(), f"missing {path}"
