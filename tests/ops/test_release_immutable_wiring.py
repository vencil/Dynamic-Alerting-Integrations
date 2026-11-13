"""Release immutability wiring guard (#3848).

The decision "may this version be published" lives INSIDE the publish:
`scripts/tools/dx/release_publish.py` (image / chart / assets) checks the
registry (or the GitHub Release) and only then writes. That protects nothing
if any other step of `release.yaml` writes too, or if the wrapper's failure
can be swallowed. So this parses the workflow as YAML and asserts, with every
`run:` read after joining `\\⏎` line continuations and dropping comment lines:

  1. no step except a wrapper step runs a publisher: `imagetools` (any use),
     `helm … push`, `docker … push` / `--push` / `type=registry` / `push=true`,
     `cosign` other than `sign-blob` / `verify-blob`, `crane`, `skopeo`,
     `oras`, `regctl`, `podman`, `buildah`, `gh … release … upload|create|
     edit|delete…`, `gh api`, `curl`, `wget`;
  2. a wrapper step (any `run:` naming `release_publish.py`) is exactly one
     command — `python3 -X utf8 scripts/tools/dx/release_publish.py
     <image|chart|assets> …` with no `;` `&` `|` `<` `>` backtick `$(` — and
     has no `if:`, `continue-on-error` or `shell:`; its job has no
     `continue-on-error`; the workflow and its jobs have no `defaults:`;
  3. `--clobber` appears in no step (Release assets are never replaced);
  4. every `uses:` is one of the actions listed here (a new action could
     publish on its own); `docker/build-push-action` has a literal `push:`,
     no `outputs:`, pushes only candidate tags (tags split on commas and
     newlines) and sets `org.opencontainers.image.revision=${{ github.sha }}`
     exactly once;
  5. every job that builds an image has exactly one `release_publish.py image`
     step, every job that runs `helm package` exactly one `… chart` step;
  6. the workflow has top-level `concurrency: {group: release-${{ github.ref }},
     cancel-in-progress: false}` and no job overrides it.

⛔ Out of scope, by design: a publisher reached INDIRECTLY — a repo script a
`run:` calls (`bash scripts/ops/x.sh`), a local or composite action (refused
by rule 4 only because it is not on the list), a command assembled from
`${{ }}` expressions or environment variables whose text never names the
publisher. This reads the workflow, not what it executes. The order of the
publish relative to the Trivy scan and the da-tools smoke is
test_release_scan_before_publish.py / test_release_da_tools_smoke.py.

Vacuity: wrapper kinds and the build action are asserted found; an
unparseable workflow is an error (the test fails), never a skip.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"

WRAPPER = "scripts/tools/dx/release_publish.py"
BUILD_ACTION = "docker/build-push-action"
REVISION_KEY = "org.opencontainers.image.revision"
REVISION_LABEL = REVISION_KEY + "=${{ github.sha }}"
CONCURRENCY = {"group": "release-${{ github.ref }}", "cancel-in-progress": False}

ALLOWED_ACTIONS = frozenset({
    "actions/checkout", "actions/setup-go", "actions/setup-python",
    "docker/login-action", "docker/setup-qemu-action", "docker/setup-buildx-action",
    BUILD_ACTION, "azure/setup-helm", "aquasecurity/trivy-action",
    "sigstore/cosign-installer", "anchore/sbom-action/download-syft",
})

_SEG = r"[^\n;&|]*"     # within one simple command
PUBLISHERS = {
    "imagetools": re.compile(r"\bimagetools\b"),
    "helm push": re.compile(rf"\bhelm\b{_SEG}\bpush\b"),
    "docker push": re.compile(rf"\bdocker\b{_SEG}(?:\bpush\b|type=registry|push=true)"),
    "registry output": re.compile(r"type=registry|push=true"),
    "cosign write": re.compile(r"\bcosign\s+(?!sign-blob\b|verify-blob\b)\S"),
    "registry client": re.compile(r"\b(?:crane|skopeo|oras|regctl|podman|buildah)\b"),
    "gh release write": re.compile(
        rf"\bgh\b{_SEG}\brelease\b{_SEG}\b(?:upload|create|edit|delete|delete-asset)\b"),
    "gh api": re.compile(r"\bgh\s+api\b"),
    "http client": re.compile(r"\b(?:curl|wget)\b"),
}
_WRAPPER_CALL = re.compile(
    r"python3 -X utf8 " + re.escape(WRAPPER) + r" (image|chart|assets)(?: [^;&|<>`\n]*)?")


def _normalize(script: str) -> str:
    """The commands of a `run:`: continuations joined, comment lines dropped."""
    joined = re.sub(r"\\\n", " ", script)
    lines = [ln.strip() for ln in joined.splitlines()]
    return "\n".join(ln for ln in lines if ln and not ln.startswith("#"))


def _load(path: Path = WORKFLOW) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and isinstance(doc.get("jobs"), dict), f"{path}: not a workflow"
    return doc


def _split(value) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n]", str(value or "")) if t.strip()]


def action_is_build(uses: str) -> bool:
    return uses.split("@", 1)[0] == BUILD_ACTION


def check_build(step: dict) -> list[str]:
    out = []
    with_ = step.get("with") or {}
    push = with_.get("push", False)
    if push not in (True, False, "true", "false"):
        out.append(f"has `push: {push!r}` — only a literal true/false can be read")
    if "outputs" in with_:
        out.append("sets `outputs:`, which can push regardless of `push:`")
    if push in (True, "true") or "outputs" in with_:
        tags = _split(with_.get("tags"))
        if not tags:
            out.append("pushes with no tags")
        out.extend(f"pushes {t!r} at build time — a release tag appears only through "
                   f"{WRAPPER}" for t in tags if ":candidate-" not in t)
    revision = [x.strip() for x in str(with_.get("labels") or "").splitlines()
                if x.strip().startswith(REVISION_KEY + "=")]
    if revision != [REVISION_LABEL]:
        out.append(f"sets {revision or 'no'} {REVISION_KEY} label(s), not exactly "
                   f"{REVISION_LABEL!r} — a re-run could not show the published image is "
                   "this commit")
    return out


def check_wrapper(job: dict, step: dict, script: str) -> list[str]:
    out = []
    if not _WRAPPER_CALL.fullmatch(script) or "$(" in script:
        out.append(f"must be exactly one `python3 -X utf8 {WRAPPER} <image|chart|assets> …` "
                   f"command with nothing chained, piped or redirected; it is {script!r}")
    for key in ("if", "continue-on-error", "shell"):
        if key in step:
            out.append(f"carries `{key}: {step[key]!r}` — the publish step must run, and "
                       "its exit code must be the step's")
    if job.get("continue-on-error") not in (None, False):
        out.append("is in a job with continue-on-error")
    return out


def check_wiring(doc: dict) -> tuple[list[str], set[str]]:
    """(violations, what was found: wrapper kinds and `build-action`)."""
    problems: list[str] = []
    seen: set[str] = set()
    if doc.get("concurrency") != CONCURRENCY:
        problems.append(f"release.yaml: top-level concurrency is {doc.get('concurrency')!r}, "
                        f"not {CONCURRENCY!r} — two runs for one tag would both publish")
    if "defaults" in doc:
        problems.append("release.yaml: top-level `defaults:` changes how every run: executes")
    for job_name, job in doc["jobs"].items():
        job = job or {}
        steps = job.get("steps") or []
        where = f"release.yaml::{job_name}"
        for key in ("defaults", "concurrency"):
            if key in job:
                problems.append(f"{where}: job-level `{key}:`")
        kinds: list[str] = []
        builds = packages = 0
        for i, step in enumerate(steps):
            name = step.get("name") or f"#{i}"
            uses = str(step.get("uses") or "")
            if uses and uses.split("@", 1)[0] not in ALLOWED_ACTIONS:
                problems.append(f"{where}: {name!r} uses {uses!r}, not an action this guard "
                                "knows does not publish — add it to ALLOWED_ACTIONS knowingly")
            if action_is_build(uses):
                builds += 1
                seen.add("build-action")
                problems.extend(f"{where}: {name!r} {p}" for p in check_build(step))
            script = _normalize(str(step.get("run") or ""))
            if "--clobber" in script:
                problems.append(f"{where}: {name!r} passes --clobber — a released asset is "
                                "never replaced")
            if re.search(r"\bhelm\s+package\b", script):
                packages += 1
            if "release_publish.py" in script:
                problems.extend(f"{where}: {name!r} {p}" for p in check_wrapper(job, step, script))
                m = _WRAPPER_CALL.fullmatch(script)
                if m:
                    kinds.append(m.group(1))
                    seen.add(m.group(1))
                continue
            for label, pattern in PUBLISHERS.items():
                if pattern.search(script):
                    problems.append(f"{where}: {name!r} runs a publisher ({label}) outside "
                                    f"{WRAPPER} — nothing checks what it writes")
        if builds and kinds.count("image") != 1:
            problems.append(f"{where}: builds an image but has {kinds.count('image')} "
                            "`release_publish.py image` steps (want 1)")
        if packages and kinds.count("chart") != 1:
            problems.append(f"{where}: packages a chart but has {kinds.count('chart')} "
                            "`release_publish.py chart` steps (want 1)")
    return problems, seen


# ── the workflow as it is ───────────────────────────────────────────────────

def test_the_workflow_publishes_only_through_the_wrapper() -> None:
    problems, seen = check_wiring(_load())
    assert not problems, "\n".join(problems)
    for kind in ("image", "chart", "assets", "build-action"):
        assert kind in seen, f"no {kind} found — the guard is vacuous for it"


def test_an_unparseable_workflow_fails_not_skips(tmp_path: Path) -> None:
    broken = tmp_path / "release.yaml"
    broken.write_text("jobs: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        _load(broken)
    broken.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _load(broken)


# ── every regression shape is refused ───────────────────────────────────────

def _steps(doc: dict, job: str = "release-exporter") -> list:
    return doc["jobs"][job]["steps"]


def _wrap(doc: dict, kind: str = "image", job: str = "release-exporter") -> dict:
    return next(s for s in _steps(doc, job) if f"release_publish.py {kind}" in str(s.get("run") or ""))


def _build(doc: dict, job: str = "release-exporter") -> dict:
    return next(s for s in _steps(doc, job) if s.get("id") == "build")


def _add(doc: dict, run: str, job: str = "release-exporter") -> None:
    _steps(doc, job).append({"name": "added", "run": run})


def _append(step: dict, text: str) -> None:
    step["run"] = step["run"] + text


MUTATIONS = {
    # the wrapper's own exit code swallowed or bypassed
    "wrapper-or-true": lambda d: _append(_wrap(d), " || true"),
    "wrapper-or-force-output": lambda d: _append(
        _wrap(d), ' || echo "publish=true" >> "$GITHUB_OUTPUT"'),
    "wrapper-then-command": lambda d: _append(_wrap(d), "\necho done"),
    # assets is not counted per job, so only the shape rule sees this one
    "assets-wrapper-or-true": lambda d: _append(_wrap(d, "assets", "release-da-tools"), " || true"),
    "wrapper-semicolon": lambda d: _append(_wrap(d), "; true"),
    "wrapper-in-echo": lambda d: _wrap(d).update(
        run=f"echo {WRAPPER} image; docker buildx imagetools create --tag x:v1 x@sha256:1"),
    "wrapper-subshell": lambda d: _wrap(d).update(
        run=f"python3 -X utf8 {WRAPPER} image --image $(echo x) --tag v1"),
    "wrapper-shell-override": lambda d: _wrap(d).update(shell="bash {0}"),
    "wrapper-if": lambda d: _wrap(d).update({"if": "false"}),
    "wrapper-continue-on-error": lambda d: _wrap(d, "chart").update({"continue-on-error": True}),
    "job-continue-on-error": lambda d: d["jobs"]["release-exporter"].update(
        {"continue-on-error": True}),
    "defaults-shell": lambda d: d.update(defaults={"run": {"shell": "bash {0}"}}),
    "wrapper-removed": lambda d: _steps(d).remove(_wrap(d)),
    "chart-wrapper-removed": lambda d: _steps(d).remove(_wrap(d, "chart")),
    # a publisher outside the wrapper
    "imagetools-continued": lambda d: _add(
        d, 'docker buildx imagetools \\\n  create --tag "${IMG}:v1" "${IMG}@${DIGEST}"'),
    "imagetools-in-variable": lambda d: _add(d, 'CMD="imagetools"; docker buildx $CMD create x'),
    "helm-flag-before-push": lambda d: _add(d, "helm --debug push x.tgz oci://ghcr.io/o/charts"),
    "helm-push-continued": lambda d: _add(d, "helm \\\n  push x.tgz oci://ghcr.io/o/charts"),
    "docker-push": lambda d: _add(d, "docker push ghcr.io/vencil/x:v1"),
    "buildx-push": lambda d: _add(d, "docker buildx build --push -t ghcr.io/vencil/x:v1 ."),
    "buildx-output-registry": lambda d: _add(d, "docker buildx build --output type=registry ."),
    "cosign-sign-continued": lambda d: _add(d, 'cosign sign \\\n --yes "${IMG}@${DIGEST}"'),
    "cosign-attest": lambda d: _add(d, 'cosign attest --yes --predicate p.json "${IMG}@${DIGEST}"'),
    "crane": lambda d: _add(d, 'crane tag "${IMG}@${DIGEST}" v1'),
    "skopeo": lambda d: _add(d, "skopeo copy --all docker://a docker://b"),
    "oras": lambda d: _add(d, "oras push ghcr.io/vencil/charts/x:1 x.tgz"),
    "gh-release-upload": lambda d: _add(d, 'gh release upload "$TAG" x.tgz'),
    "gh-release-clobber": lambda d: _add(d, 'gh release upload "$TAG" --clobber x.tgz'),
    "wrapper-clobber": lambda d: _append(_wrap(d, "assets", "release-da-tools"), " --clobber"),
    "gh-api": lambda d: _add(d, "gh api -X POST repos/o/r/releases"),
    "curl-upload": lambda d: _add(d, "curl -T x.tgz https://uploads.github.com/x"),
    # actions
    "unknown-action": lambda d: _build(d).update(uses="mr-smithers-excellent/docker-build-push@v6"),
    "local-action": lambda d: _steps(d).append({"name": "x", "uses": "./.github/actions/push"}),
    "build-push-expression": lambda d: _build(d)["with"].update(push="${{ true }}"),
    "build-outputs-registry": lambda d: _build(d)["with"].update(push=False, outputs="type=registry"),
    "build-final-tag": lambda d: _build(d)["with"].update(
        tags=_build(d)["with"]["tags"] + "\nghcr.io/vencil/threshold-exporter:latest\n"),
    "build-comma-tags": lambda d: _build(d)["with"].update(
        tags="ghcr.io/vencil/x:candidate-1,ghcr.io/vencil/x:v2.9.0"),
    "build-fork-ref-final-tag": lambda d: (_build(d).update(uses="docker/build-push-action@v6"),
                                           _build(d)["with"].update(tags="ghcr.io/vencil/x:latest")),
    "label-missing": lambda d: _build(d, "release-tenant-api")["with"].pop("labels"),
    "label-other-value": lambda d: _build(d)["with"].update(labels=REVISION_KEY + "=deadbeef\n"),
    "label-twice": lambda d: _build(d)["with"].update(
        labels=REVISION_LABEL + "\n" + REVISION_KEY + "=deadbeef\n"),
    # concurrency
    "concurrency-removed": lambda d: d.pop("concurrency"),
    "concurrency-cancels": lambda d: d["concurrency"].update({"cancel-in-progress": True}),
    "job-concurrency": lambda d: d["jobs"]["release-exporter"].update(concurrency={"group": "x"}),
}


@pytest.mark.parametrize("bad", sorted(MUTATIONS))
def test_guard_rejects_known_regressions(bad: str) -> None:
    doc = copy.deepcopy(_load())
    assert not check_wiring(doc)[0]
    MUTATIONS[bad](doc)
    assert check_wiring(doc)[0], bad
