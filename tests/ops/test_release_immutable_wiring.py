"""Release immutability wiring guard (#3848).

The decision "may this version be published" lives INSIDE the publish:
`scripts/tools/dx/release_publish.py` (image / chart / assets) checks the
registry (or the GitHub Release) and only then writes. That protects nothing
if any other step of `release.yaml` writes too, if the wrapper's failure can
be swallowed, or if what it is handed is not what the job built and scanned.
So this parses the workflow as YAML and asserts, with every `run:` read after
joining `\\⏎` line continuations and dropping comment lines:

  1. no step except a wrapper step runs a publisher: `imagetools` (any use),
     `helm … push`, `docker … push` / `--push` / `type=registry` / `push=…`,
     `cosign` other than `sign-blob` / `verify-blob`, `crane`, `skopeo`,
     `oras`, `regctl`, `podman`, `buildah`, `nerdctl`, `kaniko`, `buildctl`,
     `gh … release … upload|create|edit|delete…`, `gh api`, `gh workflow`,
     `gh alias`, `curl`, `wget`, `http|xh PUT|POST|…`, `ko build|publish…`,
     `docker … bake`, `git push`;
  2. a wrapper step is exactly one command whose arguments are fixed
     (`image --image "$IMG" --digest "$DIGEST" --revision "$REVISION" --tag
     "v${VERSION}" --tag latest [--sign]`, `chart --tgz
     ".build/<name>-${VERSION}.tgz" --registry "$CHARTS"`, `assets --tag
     "$GITHUB_REF_NAME" --title … "$RUNNER_TEMP/…"…`), whose `env:` holds only
     the variables it names, each bound to its one source — DIGEST is
     `${{ steps.<this job's build id>.outputs.digest }}`, IMG the repository
     the build step pushed its candidate to, REVISION `${{ github.sha }}` —
     and which has no key but `name` / `id` / `env` / `run` (no `if:`,
     `continue-on-error`, `shell:`, `working-directory:`, `timeout-minutes:`);
  3. the code the wrapper runs is this commit's: each job's first step is the
     one `actions/checkout` (no `with:`), no other checkout; no step writes
     under `scripts/` or `.github/` (`sed -i`, `patch`, `git checkout|restore|
     apply|…`, a redirect, `cp|mv|tee|…` into them, `cd` into `scripts/`);
     no step touches `$GITHUB_ENV` / `$GITHUB_PATH` (none does today, so
     there is no allowlist); no step has `shell:` or `working-directory:`;
  4. `--clobber` appears in no step (Release assets are never replaced);
  5. every `uses:` is one of the actions listed here; `docker/build-push-
     action` has a literal `push:`, no `outputs:` and no `cache-to:`, every
     tag (split on commas and newlines) is literally
     `<registry>/<owner>/<repo>:candidate-${{ github.run_id }}-${{
     github.run_attempt }}`, and `org.opencontainers.image.revision=${{
     github.sha }}` is set exactly once;
  6. every job that builds an image has exactly one `… image` step, every job
     that runs `helm package` exactly one `… chart` step, and every job's
     `if:` is `startsWith(github.ref, 'refs/tags/<line>/')`;
  7. top-level `concurrency: {group: release-${{ github.ref }},
     cancel-in-progress: false}`, no job overrides it, no `defaults:`.

⛔ Threat model. This catches honest mistakes and structural drift: a step
added the obvious way, a gate weakened by a well-meant edit, a refactor that
moves a publish out of the wrapper. It does NOT stop deliberate obfuscation,
and is not meant to — anyone who can edit release.yaml can also edit this
test. Out of scope, by design: quote-splitting (`doc''ker push`), variable
indirection (`G=gh; $G release …`), interpreters given code inline
(`python3 -c`, `node -e`, `bash -c '…'`), a `make` target or repo script that
publishes, `pip install` of a publishing package, a publisher binary not
listed above, a local or composite action (refused only because it is not
on the action list). The order of the publish relative to the Trivy scan and
the da-tools smoke is test_release_scan_before_publish.py /
test_release_da_tools_smoke.py.

Vacuity: wrapper kinds and the build action are asserted found; an
unparseable workflow is an error (the test fails), never a skip.
"""
from __future__ import annotations

import copy
import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"

WRAPPER = "scripts/tools/dx/release_publish.py"
BUILD_ACTION = "docker/build-push-action"
CHECKOUT = "actions/checkout"
REVISION_KEY = "org.opencontainers.image.revision"
REVISION_LABEL = REVISION_KEY + "=${{ github.sha }}"
CONCURRENCY = {"group": "release-${{ github.ref }}", "cancel-in-progress": False}
REPO_PREFIX = "${{ env.REGISTRY }}/${{ env.IMAGE_OWNER }}/"
_CANDIDATE_TAG = re.compile(re.escape(REPO_PREFIX) + r"([a-z0-9][a-z0-9._-]*)"
                            r":candidate-\$\{\{ github\.run_id \}\}-\$\{\{ github\.run_attempt \}\}")
_JOB_IF = re.compile(r"startsWith\(github\.ref, 'refs/tags/[a-z][a-z-]*/'\)")

ALLOWED_ACTIONS = frozenset({
    CHECKOUT, "actions/setup-go", "actions/setup-python",
    "docker/login-action", "docker/setup-qemu-action", "docker/setup-buildx-action",
    BUILD_ACTION, "azure/setup-helm", "aquasecurity/trivy-action",
    "sigstore/cosign-installer", "anchore/sbom-action/download-syft",
})

#: env every wrapper kind may carry, each with the one value it may have
#: (None: checked separately — IMG and DIGEST depend on the job's build step).
_COMMON = {"REGISTRY_USERNAME": "${{ github.actor }}",
           "REGISTRY_PASSWORD": "${{ secrets.GITHUB_TOKEN }}",
           "VERSION": "${{ steps.version.outputs.VERSION }}"}
WRAPPER_ENV = {
    "image": {**_COMMON, "IMG": None, "DIGEST": None, "REVISION": "${{ github.sha }}",
              "COSIGN_EXPERIMENTAL": "1"},
    "chart": {**_COMMON, "CHARTS": "oci://${{ env.REGISTRY }}/${{ env.IMAGE_OWNER }}/charts"},
    "assets": {"VERSION": _COMMON["VERSION"], "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"},
}
WRAPPER_STEP_KEYS = frozenset({"name", "id", "env", "run"})
_IMAGE_ARGS = ["--image", "$IMG", "--digest", "$DIGEST", "--revision", "$REVISION",
               "--tag", "v${VERSION}", "--tag", "latest"]
_CHART_TGZ = re.compile(r"\.build/[a-z0-9][a-z0-9-]*-\$\{VERSION\}\.tgz")
_ASSET_FILE = re.compile(r"\$RUNNER_TEMP/[A-Za-z0-9_./*${}-]+")

_SEG = r"[^\n;&|]*"     # within one simple command
PUBLISHERS = {
    "imagetools": re.compile(r"\bimagetools\b"),
    "helm push": re.compile(rf"\bhelm\b{_SEG}\b(?:push|cm-push)\b"),
    "docker push": re.compile(rf"\bdocker\b{_SEG}(?:\bpush\b|\bbake\b|type=registry|push=)"),
    "registry output": re.compile(r"type=registry|\bpush=|--destination\b"),
    "cosign write": re.compile(r"\bcosign\s+(?!sign-blob\b|verify-blob\b)\S"),
    "registry client": re.compile(
        r"\b(?:crane|skopeo|oras|regctl|podman|buildah|nerdctl|kaniko|buildctl)\b"
        r"|\bko\s+(?:build|publish|resolve|apply)\b"),
    "gh release write": re.compile(
        rf"\bgh\b{_SEG}\brelease\b{_SEG}\b(?:upload|create|edit|delete|delete-asset)\b"),
    "gh api / workflow / alias": re.compile(r"\bgh\s+(?:api|workflow|alias)\b"),
    "http client": re.compile(r"\b(?:curl|wget)\b|\b(?:https?|xh)\s+(?:PUT|POST|PATCH|DELETE)\b"),
    "git push": re.compile(rf"\bgit\b{_SEG}\bpush\b"),
}
#: Writes that would change the code the wrapper runs (or the workflow).
TREE_WRITES = {
    "sed -i": re.compile(rf"\bsed\b{_SEG}\s-i"),
    "patch": re.compile(r"\bpatch\b"),
    "git checkout/restore/apply…": re.compile(
        r"\bgit\s+(?:checkout|restore|apply|am|cherry-pick|reset|stash|pull|merge|rebase|"
        r"switch|worktree|mv|rm)\b"),
    "redirect into scripts/ or .github/": re.compile(r">\s*[\"']?(?:\./)?(?:scripts|\.github)/"),
    "copy into scripts/ or .github/": re.compile(
        rf"\b(?:cp|mv|ln|install|rsync|tee|touch|rm|chmod|truncate|dd|unzip|tar)\b{_SEG}"
        r"(?:^|[\s\"'=])(?:\./)?(?:scripts|\.github)/"),
    "cd into scripts/": re.compile(r"\bcd\s+[\"']?(?:\./)?scripts\b"),
    "$GITHUB_ENV / $GITHUB_PATH": re.compile(r"GITHUB_ENV|GITHUB_PATH"),
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
    for key in ("if", "continue-on-error"):
        if key in step:
            out.append(f"carries `{key}:` — the build whose digest is published must run")
    for key in ("outputs", "cache-to"):
        if key in with_:
            out.append(f"sets `{key}:`, which can write to a registry regardless of `push:`")
    if push in (True, "true") or "outputs" in with_:
        tags = _split(with_.get("tags"))
        if not tags:
            out.append("pushes with no tags")
        out.extend(f"pushes {t!r} at build time — every tag must be literally "
                   f"{REPO_PREFIX}<repo>:candidate-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
                   for t in tags if not _CANDIDATE_TAG.fullmatch(t))
    revision = [x.strip() for x in str(with_.get("labels") or "").splitlines()
                if x.strip().startswith(REVISION_KEY + "=")]
    if revision != [REVISION_LABEL]:
        out.append(f"sets {revision or 'no'} {REVISION_KEY} label(s), not exactly "
                   f"{REVISION_LABEL!r} — a re-run could not show the published image is "
                   "this commit")
    return out


def check_wrapper(job: dict, step: dict, script: str, build: dict | None) -> list[str]:
    out = []
    m = _WRAPPER_CALL.fullmatch(script)
    if not m or "$(" in script:
        return [f"must be exactly one `python3 -X utf8 {WRAPPER} <image|chart|assets> …` "
                f"command with nothing chained, piped or redirected; it is {script!r}"]
    kind = m.group(1)
    extra = set(step) - WRAPPER_STEP_KEYS
    if extra:
        out.append(f"carries {sorted(extra)} — a publish step has only name/id/env/run: it "
                   "must run, here, and its exit code must be the step's")
    if job.get("continue-on-error") not in (None, False):
        out.append("is in a job with continue-on-error")
    env = step.get("env") or {}
    allowed = WRAPPER_ENV[kind]
    for key, value in env.items():
        if key not in allowed:
            out.append(f"sets env {key!r}, which a `{kind}` publish does not take")
        elif allowed[key] is not None and str(value) != allowed[key]:
            out.append(f"binds {key} to {value!r}, not {allowed[key]!r}")
    args = shlex.split(script)[5:]
    if kind == "image":
        build_id = (build or {}).get("id")
        repos = {_CANDIDATE_TAG.fullmatch(t).group(1) for t in _split((build or {}).get("with", {})
                 .get("tags")) if _CANDIDATE_TAG.fullmatch(t)} if build else set()
        want_digest = f"${{{{ steps.{build_id}.outputs.digest }}}}"
        if build is None or not build_id:
            out.append("has no build step with an id in its job to take the digest from")
        elif str(env.get("DIGEST")) != want_digest:
            out.append(f"binds DIGEST to {env.get('DIGEST')!r}, not {want_digest!r} — the "
                       "published digest must be the one this job built and scanned")
        if len(repos) != 1 or str(env.get("IMG")) != REPO_PREFIX + next(iter(repos), "?"):
            out.append(f"binds IMG to {env.get('IMG')!r}, not the repository its build step "
                       f"pushed ({sorted(repos)})")
        if env.get("REVISION") is None:
            out.append("does not bind REVISION")
        if args not in (_IMAGE_ARGS, _IMAGE_ARGS + ["--sign"]):
            out.append(f"arguments {args} are not {' '.join(_IMAGE_ARGS)} [--sign]")
    elif kind == "chart":
        if not (len(args) == 4 and args[0] == "--tgz" and _CHART_TGZ.fullmatch(args[1])
                and args[2:] == ["--registry", "$CHARTS"]):
            out.append(f"arguments {args} are not --tgz .build/<name>-${{VERSION}}.tgz "
                       "--registry $CHARTS")
        if "CHARTS" not in env:
            out.append("does not bind CHARTS")
    else:
        if not (len(args) > 4 and args[:2] == ["--tag", "$GITHUB_REF_NAME"]
                and args[2] == "--title" and all(_ASSET_FILE.fullmatch(a) for a in args[4:])):
            out.append(f"arguments {args} are not --tag $GITHUB_REF_NAME --title <t> "
                       "$RUNNER_TEMP/<file>…")
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
        if not _JOB_IF.fullmatch(str(job.get("if") or "")):
            problems.append(f"{where}: `if: {job.get('if')!r}` is not "
                            "startsWith(github.ref, 'refs/tags/<line>/')")
        checkouts = [i for i, s in enumerate(steps)
                     if str(s.get("uses") or "").split("@", 1)[0] == CHECKOUT]
        if checkouts != [0] or set(steps[0]) != {"uses"}:
            problems.append(f"{where}: the first step must be the job's only `{CHECKOUT}`, "
                            f"with nothing but `uses:` (found at {checkouts}) — the wrapper "
                            "must run this commit's code")
        builds = [s for s in steps if action_is_build(str(s.get("uses") or ""))]
        build = builds[0] if len(builds) == 1 else None
        kinds: list[str] = []
        packages = 0
        for i, step in enumerate(steps):
            name = step.get("name") or f"#{i}"
            uses = str(step.get("uses") or "")
            for key in ("shell", "working-directory"):
                if key in step:
                    problems.append(f"{where}: {name!r} has `{key}:`")
            if uses and uses.split("@", 1)[0] not in ALLOWED_ACTIONS:
                problems.append(f"{where}: {name!r} uses {uses!r}, not an action this guard "
                                "knows does not publish — add it to ALLOWED_ACTIONS knowingly")
            if action_is_build(uses):
                seen.add("build-action")
                problems.extend(f"{where}: {name!r} {p}" for p in check_build(step))
            script = _normalize(str(step.get("run") or ""))
            if "--clobber" in script:
                problems.append(f"{where}: {name!r} passes --clobber — a released asset is "
                                "never replaced")
            for label, pattern in TREE_WRITES.items():
                if pattern.search(script):
                    problems.append(f"{where}: {name!r} {label} — the code the wrapper runs "
                                    "must be this commit's")
            if re.search(r"\bhelm\s+package\b", script):
                packages += 1
            if "release_publish.py" in script:
                problems.extend(f"{where}: {name!r} {p}"
                                for p in check_wrapper(job, step, script, build))
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
        if not builds and "image" in kinds:
            problems.append(f"{where}: publishes an image it did not build")
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
    # round 2 (#3848 review): what the build writes
    "build-cache-to-final-tag": lambda d: _build(d)["with"].update(
        {"cache-to": "type=registry,ref=ghcr.io/vencil/threshold-exporter:v2.9.0,mode=max"}),
    "build-tag-expression-naming-candidate": lambda d: _build(d)["with"].update(
        tags="${{ ':candidate-' != '' && 'ghcr.io/vencil/threshold-exporter:v2.9.0' || '' }}"),
    "build-tag-candidate-suffix-literal": lambda d: _build(d)["with"].update(
        tags=_build(d)["with"]["tags"] + "\n" + REPO_PREFIX + "threshold-exporter:candidate-1\n"),
    # round 2: what the wrapper is handed
    "wrapper-digest-literal": lambda d: _wrap(d)["env"].update(
        DIGEST="sha256:" + "1" * 64, X="${{ steps.build.outputs.digest }}"),
    "wrapper-digest-other-step": lambda d: _wrap(d)["env"].update(
        DIGEST="${{ steps.other.outputs.digest }}"),
    "wrapper-revision-literal": lambda d: _wrap(d)["env"].update(REVISION="0" * 40),
    "wrapper-img-other-repo": lambda d: _wrap(d)["env"].update(IMG=REPO_PREFIX + "da-tools"),
    "wrapper-env-pythonpath": lambda d: _wrap(d)["env"].update(PYTHONPATH="/tmp/evil"),
    "wrapper-env-github-output": lambda d: _wrap(d)["env"].update(GITHUB_OUTPUT="/dev/null"),
    "wrapper-extra-tag-arg": lambda d: _append(_wrap(d), ' --tag "$EXTRA"'),
    "wrapper-other-version-tag": lambda d: _wrap(d).update(run=_wrap(d)["run"].replace(
        '"v${VERSION}"', '"v${VERSION}-x"')),
    "wrapper-chart-other-registry": lambda d: _wrap(d, "chart").update(run=_wrap(d, "chart")["run"]
        .replace('"$CHARTS"', '"oci://ghcr.io/vencil/charts2"')),
    "wrapper-assets-file-outside-runner-temp": lambda d: _append(
        _wrap(d, "assets", "release-da-tools"), " /etc/passwd"),
    "wrapper-working-directory": lambda d: _wrap(d).update({"working-directory": "old"}),
    "wrapper-timeout": lambda d: _wrap(d).update({"timeout-minutes": 0}),
    "image-wrapper-in-job-without-build": lambda d: d["jobs"].update(extra={
        "if": "startsWith(github.ref, 'refs/tags/x/')",
        "steps": [{"uses": "actions/checkout@v6"},
                  {"name": "p", "run": f"python3 -X utf8 {WRAPPER} image " + " ".join(_IMAGE_ARGS)}]}),
    # round 2: the code the wrapper runs
    "second-checkout-other-ref": lambda d: _steps(d).insert(
        3, {"uses": "actions/checkout@v6", "with": {"ref": "v2.8.0", "path": "old"}}),
    "checkout-with-ref": lambda d: _steps(d)[0].update({"with": {"ref": "main"}}),
    "sed-i-decision-module": lambda d: _add(
        d, "sed -i 's/raise Violation(/print(/' scripts/tools/dx/check_release_immutable.py"),
    "git-checkout-old-scripts": lambda d: _add(d, "git checkout origin/old -- scripts/tools/dx"),
    "redirect-into-scripts": lambda d: _add(d, "echo x > scripts/tools/dx/release_publish.py"),
    "cp-into-scripts": lambda d: _add(d, "cp /tmp/x.py scripts/tools/dx/release_publish.py"),
    "github-env-bash-env": lambda d: _add(d, 'echo "BASH_ENV=x.sh" >> "$GITHUB_ENV"'),
    "github-path-shadow": lambda d: _add(d, 'echo "$PWD/bin" >> "$GITHUB_PATH"'),
    "shell-field": lambda d: _steps(d).append(
        {"name": "s", "run": "echo hi", "shell": "bash -c 'docker push x:v1' {0}"}),
    "job-if-always": lambda d: d["jobs"]["release-exporter"].update({"if": "always()"}),
    # round 2: publishers not listed before
    "nerdctl": lambda d: _add(d, "nerdctl push ghcr.io/vencil/x:v1"),
    "buildx-output-push-1": lambda d: _add(
        d, "docker buildx build --output type=image,name=ghcr.io/vencil/x:v1,push=1 ."),
    "kaniko-destination": lambda d: _add(
        d, "docker run gcr.io/kaniko-project/executor --destination=ghcr.io/vencil/x:v1"),
    "git-push-tag": lambda d: _add(d, "git push -f origin HEAD:refs/tags/exporter/v2.9.0"),
    "gh-workflow-run": lambda d: _add(d, "gh workflow run release.yaml --ref tools/v2.9.0"),
    "helm-cm-push": lambda d: _add(d, "helm cm-push x.tgz repo"),
    "buildx-bake-file": lambda d: _add(d, "docker buildx bake -f ci/bake.hcl release"),
    "ko-build": lambda d: _add(d, "ko build --tags v1 ./cmd/x"),
    "httpie-put": lambda d: _add(d, "http PUT https://ghcr.io/v2/vencil/x/manifests/v1"),
    "build-if-false": lambda d: _build(d).update({"if": "false"}),
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
