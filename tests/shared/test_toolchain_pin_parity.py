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

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"
_DEVCONTAINER = _REPO / ".devcontainer" / "devcontainer.json"
_INSTALL_TRIVY = _REPO / ".devcontainer" / "install-trivy.sh"
_MAKEFILE = _REPO / "Makefile"
_IAC_HELM = _REPO / "scripts" / "tools" / "lint" / "check_iac_helm.py"

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


# ── Python ──────────────────────────────────────────────────────────────────


def _python_versions(job: dict, raw: object) -> list[str]:
    """Resolve a setup-python `python-version` to literal versions.

    `${{ matrix.python }}` is expanded from the job's own matrix list; any other
    expression is returned verbatim so it FAILS the comparison (unresolvable is
    reported, never skipped).
    """
    s = str(raw)
    m = re.fullmatch(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", s)
    if not m:
        return [s]
    vals = ((job.get("strategy") or {}).get("matrix") or {}).get(m.group(1))
    if not isinstance(vals, list) or not vals:
        return [f"<unresolvable {s}>"]
    return [str(v) for v in vals]


def test_python_version_matches_the_dev_container() -> None:
    """One Python across CI and the container. A deliberate floor/matrix test
    would be a new, commented matrix entry — and a conscious edit here."""
    dc = str(_feature("python")["version"])
    assert re.fullmatch(r"3\.\d+", dc), f"devcontainer python version {dc!r} is not a pinned 3.x"
    bad = []
    for f, j, job, step in _action_steps("actions/setup-python"):
        raw = (step.get("with") or {}).get("python-version")
        if raw is None:
            bad.append(f"{f}:{j} setup-python without python-version")
            continue
        bad += [f"{f}:{j} python-version={v!r}" for v in _python_versions(job, raw) if v != dc]
    assert not bad, (
        f"python-version must be the dev container's {dc}:\n  " + "\n  ".join(bad))


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


@pytest.mark.parametrize("path", [_DEVCONTAINER, _INSTALL_TRIVY, _MAKEFILE, _IAC_HELM])
def test_inputs_exist(path: Path) -> None:
    assert path.is_file(), f"missing {path}"
