"""Release immutability wiring guard (#3848).

`scripts/tools/dx/check_release_immutable.py` decides, per job, whether a
version may be published; it protects nothing unless every step that writes
to the registry obeys it. This parses `release.yaml` as YAML (not a grep over
its text — a step's name or comment naming `helm push` is not a call) and, for
every job, asserts

  1. every publishing step — `helm push`, `imagetools create`, `docker push`,
     `docker build … --push`, image signing (`cosign sign`, not `sign-blob`) —
     carries exactly `if: steps.<id>.outputs.publish == 'true'`, where <id> is
     an EARLIER step of the same job that runs `check_release_immutable.py`
     of the matching kind (`chart` for `helm push`, `image` for the rest),
     about the same artefact (same image `IMG`, same chart archive name);
  2. that check step runs unconditionally: no `if:` (a skipped check leaves
     `publish` empty, so publishing is skipped and the job is green having
     published nothing) and no `continue-on-error` (its rc 1 / 2 must fail
     the job);
  3. `docker/build-push-action` pushes only candidate tags — a final tag
     pushed at build time cannot be gated by anything;
  4. every image build that pushes sets the label
     `org.opencontainers.image.revision=${{ github.sha }}` — without it a
     re-run of the same commit reads as "published from something else".

Vacuity: the publish kinds found and the jobs covered are asserted
non-empty; an unparseable workflow is an error (the test fails), never a skip.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"

TOOL = "scripts/tools/dx/check_release_immutable.py"
BUILD_ACTION = "docker/build-push-action"
REVISION_LABEL = "org.opencontainers.image.revision=${{ github.sha }}"

#: kind of publish → (pattern over the comment-stripped `run:`, check subcommand)
PUBLISH = {
    "helm-push": (re.compile(r"\bhelm\s+push\b"), "chart"),
    "promote": (re.compile(r"\bimagetools\s+create\b"), "image"),
    "docker-push": (re.compile(r"\bdocker\s+push\b"), "image"),
    "build-push": (re.compile(r"\bdocker\s+(?:buildx\s+)?build\b[^\n]*--push\b"), "image"),
    "image-sign": (re.compile(r"\bcosign\s+sign\s"), "image"),
}
_CHECK = re.compile(re.escape(TOOL) + r"\s+(image|chart)\b")
_GATE = re.compile(r"^steps\.([A-Za-z_][\w-]*)\.outputs\.publish == 'true'$")
_TGZ = re.compile(r"\.build/([A-Za-z0-9._-]+?)-\$")


def _strip_comments(script: str) -> str:
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def _script(step: dict) -> str:
    return _strip_comments(str(step.get("run") or ""))


def _gate(step: dict) -> str:
    cond = str(step.get("if") or "").strip()
    if cond.startswith("${{") and cond.endswith("}}"):
        cond = cond[3:-2].strip()
    return re.sub(r"\s+", " ", cond)


def _load(path: Path = WORKFLOW) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and isinstance(doc.get("jobs"), dict), f"{path}: not a workflow"
    return doc


def _artefact(kind: str, step: dict) -> str:
    """What a step is about: the image (`env.IMG`) or the chart archive name."""
    if kind == "chart":
        m = _TGZ.search(_script(step))
        return m.group(1) if m else ""
    return str((step.get("env") or {}).get("IMG") or "")


def check_wiring(doc: dict) -> tuple[list[str], set[str]]:
    """(violations, publish kinds seen)."""
    problems: list[str] = []
    seen: set[str] = set()
    for job_name, job in doc["jobs"].items():
        steps = (job or {}).get("steps") or []
        where = f"release.yaml::{job_name}"
        checks: dict[str, tuple[int, str, dict]] = {}
        for i, step in enumerate(steps):
            m = _CHECK.search(_script(step))
            if not m:
                continue
            sid = step.get("id")
            if not sid:
                problems.append(f"{where}: check step #{i} has no `id`; nothing can read its decision")
                continue
            if sid in checks:
                problems.append(f"{where}: two steps share the id {sid!r}")
            checks[sid] = (i, m.group(1), step)
            if "if" in step:
                problems.append(f"{where}: check step {sid!r} has an `if:`; a skipped check "
                                "publishes nothing and stays green")
            if step.get("continue-on-error") not in (None, False):
                problems.append(f"{where}: check step {sid!r} has continue-on-error; its "
                                "rc 1 / 2 must fail the job")

        for i, step in enumerate(steps):
            name = step.get("name") or f"#{i}"
            uses = str(step.get("uses") or "")
            with_ = step.get("with") or {}
            if uses.startswith(BUILD_ACTION) and str(with_.get("push")).lower() == "true":
                seen.add("build-action")
                tags = [t.strip() for t in str(with_.get("tags") or "").splitlines() if t.strip()]
                for tag in tags or ["<no tags>"]:
                    if ":candidate-" not in tag:
                        problems.append(f"{where}: {name!r} pushes {tag!r} at build time — a "
                                        "final tag must only appear by a gated promotion")
                labels = [x.strip() for x in str(with_.get("labels") or "").splitlines()]
                if REVISION_LABEL not in labels:
                    problems.append(f"{where}: {name!r} does not set the label {REVISION_LABEL} — "
                                    "a re-run could not show the published image is this commit")
            script = _script(step)
            for kind, (pattern, want) in PUBLISH.items():
                if not pattern.search(script):
                    continue
                seen.add(kind)
                g = _GATE.fullmatch(_gate(step))
                if not g:
                    problems.append(f"{where}: {kind} step {name!r} is not gated by "
                                    f"`if: steps.<check>.outputs.publish == 'true'` "
                                    f"(has {step.get('if')!r})")
                    continue
                found = checks.get(g.group(1))
                if found is None:
                    problems.append(f"{where}: {kind} step {name!r} reads {g.group(1)!r}, which is "
                                    f"not a {TOOL} step of this job")
                    continue
                at, got, check = found
                if got != want:
                    problems.append(f"{where}: {kind} step {name!r} is gated by a `{got}` check; "
                                    f"it needs a `{want}` check")
                if at >= i:
                    problems.append(f"{where}: {kind} step {name!r} (#{i}) runs before its check "
                                    f"{g.group(1)!r} (#{at})")
                if got == want and _artefact(want, check) != _artefact(want, step):
                    problems.append(f"{where}: {kind} step {name!r} publishes "
                                    f"{_artefact(want, step)!r} but its check asked about "
                                    f"{_artefact(want, check)!r}")
    return problems, seen


def test_every_publish_is_gated_by_the_immutability_check() -> None:
    problems, seen = check_wiring(_load())
    assert not problems, "\n".join(problems)
    for kind in ("helm-push", "promote", "image-sign", "build-action"):
        assert kind in seen, f"no {kind} step found — the pattern drifted and the guard is vacuous"


def test_every_job_that_builds_an_image_checks_it() -> None:
    doc = _load()
    builders = {n for n, j in doc["jobs"].items()
                if any(str(s.get("uses") or "").startswith(BUILD_ACTION) for s in j.get("steps") or [])}
    checked = {n for n, j in doc["jobs"].items()
               if any(_CHECK.search(_script(s)) for s in j.get("steps") or [])}
    assert builders, "no job builds an image — guard is vacuous"
    assert builders <= checked, f"jobs that build without an immutability check: {builders - checked}"


def _job(doc: dict, name: str) -> list:
    return doc["jobs"][name]["steps"]


def _step(steps: list, pred) -> dict:
    return next(s for s in steps if pred(s))


def _promote(s): return "imagetools create" in str(s.get("run") or "")
def _helm_push(s): return "helm push" in str(s.get("run") or "")
def _image_check(s): return s.get("id") == "image_immutable"
def _chart_check(s): return s.get("id") == "chart_immutable"


def _mutate(doc: dict, bad: str) -> None:
    exp = _job(doc, "release-exporter")
    if bad == "promote-ungated":
        del _step(exp, _promote)["if"]
    elif bad == "helm-push-ungated":
        del _step(exp, _helm_push)["if"]
    elif bad == "sign-ungated":
        del _step(_job(doc, "release-da-tools"), lambda s: "cosign sign --yes" in str(s.get("run")))["if"]
    elif bad == "gate-on-wrong-output":
        _step(exp, _promote)["if"] = "steps.image_immutable.outputs.digest == 'true'"
    elif bad == "gate-or-always":
        _step(exp, _promote)["if"] = "steps.image_immutable.outputs.publish == 'true' || always()"
    elif bad == "helm-push-gated-by-image-check":
        _step(exp, _helm_push)["if"] = "steps.image_immutable.outputs.publish == 'true'"
    elif bad == "check-after-promote":
        check = _step(exp, _image_check)
        exp.remove(check)
        exp.insert(exp.index(_step(exp, _promote)) + 1, check)
    elif bad == "check-conditional":
        _step(exp, _chart_check)["if"] = "github.run_attempt == 1"
    elif bad == "check-continue-on-error":
        _step(exp, _image_check)["continue-on-error"] = True
    elif bad == "check-of-another-image":
        _step(exp, _image_check)["env"]["IMG"] = "ghcr.io/vencil/da-tools"
    elif bad == "check-of-another-chart":
        c = _step(exp, _chart_check)
        c["run"] = c["run"].replace("threshold-exporter-", "da-portal-")
    elif bad == "label-missing":
        build = _step(_job(doc, "release-tenant-api"), lambda s: s.get("id") == "build")
        del build["with"]["labels"]
    elif bad == "final-tag-at-build":
        build = _step(exp, lambda s: s.get("id") == "build")
        build["with"]["tags"] += "\nghcr.io/vencil/threshold-exporter:v1.2.3\n"
    elif bad == "raw-docker-push":
        exp.append({"name": "Push", "run": "docker push ghcr.io/vencil/x:v1"})
    else:  # pragma: no cover
        raise AssertionError(bad)


@pytest.mark.parametrize("bad", [
    "promote-ungated", "helm-push-ungated", "sign-ungated", "gate-on-wrong-output",
    "gate-or-always", "helm-push-gated-by-image-check", "check-after-promote",
    "check-conditional", "check-continue-on-error", "check-of-another-image",
    "check-of-another-chart", "label-missing", "final-tag-at-build", "raw-docker-push",
])
def test_guard_rejects_known_regressions(bad: str) -> None:
    doc = copy.deepcopy(_load())
    assert not check_wiring(doc)[0]
    _mutate(doc, bad)
    assert check_wiring(doc)[0], bad


def test_an_unparseable_workflow_fails_not_skips(tmp_path: Path) -> None:
    broken = tmp_path / "release.yaml"
    broken.write_text("jobs: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        _load(broken)
    broken.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _load(broken)
