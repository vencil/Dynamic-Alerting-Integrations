"""Release scan-before-publish ordering guard (#1278).

`release.yaml` used to build and push every component image straight to its
formal tags (`:v<version>` + `:latest`), push the Helm chart, and only THEN run
the Trivy hard gate. A fixable HIGH/CRITICAL therefore turned the Actions run
red while the image — and for four jobs the chart — was already in the
registry. The fix is structural, so this guard is too: it parses the workflow
YAML (not a grep over its text) and, for every job that pushes an image with
`docker/build-push-action`, asserts

  1. the build step pushes ONLY candidate tags (`:candidate-…`), never a
     formal one — nothing a customer pulls exists before the scan;
  2. the job has exactly one Trivy step, and its `image-ref` is the build
     step's own digest (`<image>@${{ steps.<build>.outputs.digest }}`), so the
     scanned object IS the object that gets promoted — not a tag that could be
     re-pointed between scan and publish;
  3. every publishing step — promotion to formal tags (`imagetools create`),
     `helm push`, `cosign sign` / `sign-blob`, `syft` SBOM generation and
     `gh release` — sits AFTER that Trivy step;
  4. promotion and image signing name the same build-step digest.

Vacuity: a guard that finds no publishing job passes while covering nothing,
so the discovered job set and the per-kind publish-step counts are asserted
non-empty.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"

_BUILD_ACTION = "docker/build-push-action"
_TRIVY_ACTION = "aquasecurity/trivy-action"

# Kinds of publishing step, keyed by how the step does it. Matched against the
# `run:` script of a step (shell commands), never against its name or comments.
_PUBLISH_RUN_PATTERNS = {
    "promote": re.compile(r"\bimagetools\s+create\b"),
    "helm-push": re.compile(r"\bhelm\s+push\b"),
    "cosign": re.compile(r"\bcosign\s+sign(-blob)?\b"),
    "sbom": re.compile(r"\bsyft\s"),
    "gh-release": re.compile(r"\bgh\s+release\s+(create|upload)\b"),
}


def _strip_comments(script: str) -> str:
    """Drop whole-line shell comments so a comment naming `helm push` is not a call."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _load(path: Path = WORKFLOW) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _uses(step: dict) -> str:
    return str(step.get("uses") or "")


def _publishing_jobs(doc: dict) -> dict:
    jobs = {}
    for name, job in (doc.get("jobs") or {}).items():
        steps = (job or {}).get("steps") or []
        if any(
            _uses(s).startswith(_BUILD_ACTION)
            and str((s.get("with") or {}).get("push")).lower() == "true"
            for s in steps
        ):
            jobs[name] = steps
    return jobs


def check_release_workflow(doc: dict) -> list[str]:
    """Return every violation of the scan-before-publish contract."""
    problems: list[str] = []
    jobs = _publishing_jobs(doc)
    if not jobs:
        return ["no job pushes an image with docker/build-push-action — guard would be vacuous"]

    seen_kinds: set[str] = set()
    for job_name, steps in jobs.items():
        where = f"release.yaml::{job_name}"

        builds = [
            (i, s) for i, s in enumerate(steps)
            if _uses(s).startswith(_BUILD_ACTION)
            and str((s.get("with") or {}).get("push")).lower() == "true"
        ]
        build_ids = set()
        for i, s in builds:
            sid = s.get("id")
            if not sid:
                problems.append(f"{where}: build step #{i} has no `id`, so nothing can name its digest")
                sid = f"#{i}"
            else:
                build_ids.add(sid)
            tags =[t.strip() for t in str((s.get("with") or {}).get("tags") or "").splitlines() if t.strip()]
            if not tags:
                problems.append(f"{where}: build step {sid!r} pushes with no tags")
            for tag in tags:
                if ":candidate-" not in tag:
                    problems.append(
                        f"{where}: build step {sid!r} pushes non-candidate tag {tag!r} "
                        "before the Trivy scan — a formal tag must only appear by promotion"
                    )

        trivy = [(i, s) for i, s in enumerate(steps) if _uses(s).startswith(_TRIVY_ACTION)]
        if len(trivy) != 1:
            problems.append(f"{where}: expected exactly one Trivy step, found {len(trivy)}")
            continue
        scan_idx, scan = trivy[0]
        ref = str((scan.get("with") or {}).get("image-ref") or "")
        digest_refs = set(re.findall(r"@\$\{\{\s*steps\.([\w-]+)\.outputs\.digest\s*\}\}", ref))
        if not digest_refs or not digest_refs <= build_ids:
            problems.append(
                f"{where}: Trivy image-ref {ref!r} is not `<image>@${{{{ steps.<build>.outputs.digest }}}}` "
                f"of this job's build step {sorted(build_ids)} — scanned object may differ from the published one"
            )

        for i, s in enumerate(steps):
            script = _strip_comments(str(s.get("run") or ""))
            if not script:
                continue
            kinds = [k for k, pat in _PUBLISH_RUN_PATTERNS.items() if pat.search(script)]
            for kind in kinds:
                seen_kinds.add(kind)
                if i < scan_idx:
                    problems.append(
                        f"{where}: {kind} step {s.get('name')!r} (#{i}) runs before the Trivy scan (#{scan_idx})"
                    )
            # Image signing (`cosign sign`, not `sign-blob`) and promotion must
            # both name the build-step digest — the object Trivy scanned.
            if "promote" in kinds or re.search(r"\bcosign\s+sign\s", script):
                blob = yaml.safe_dump({"env": s.get("env"), "run": s.get("run")})
                named = set(re.findall(r"steps\.([\w-]+)\.outputs\.digest", blob))
                if not named or not named <= build_ids:
                    problems.append(
                        f"{where}: step {s.get('name')!r} promotes/signs an image without naming "
                        f"this job's build digest (steps.<{sorted(build_ids)}>.outputs.digest)"
                    )

    for kind in ("promote", "helm-push", "cosign", "sbom", "gh-release"):
        if kind not in seen_kinds:
            problems.append(f"no {kind} step found in any publishing job — pattern drifted, guard is vacuous for it")
    return problems


def test_release_scans_before_it_publishes() -> None:
    problems = check_release_workflow(_load())
    assert not problems, "\n".join(problems)


def test_every_publishing_job_is_discovered() -> None:
    """The set of image-pushing jobs must equal the set of jobs with a tag trigger arm.

    A job that stops using build-push-action (e.g. switches to raw `docker
    buildx build --push`) would silently drop out of the guard above.
    """
    doc = _load()
    jobs_with_trivy = {
        name for name, job in doc["jobs"].items()
        if any(_uses(s).startswith(_TRIVY_ACTION) for s in (job or {}).get("steps") or [])
    }
    assert jobs_with_trivy, "no Trivy step in release.yaml at all"
    assert set(_publishing_jobs(doc)) == jobs_with_trivy


@pytest.mark.parametrize("bad", ["formal-tag-at-build", "chart-before-scan", "scan-by-tag"])
def test_guard_rejects_known_regressions(bad: str) -> None:
    """Each regression shape the guard exists for must produce a violation."""
    doc = _load()
    job = doc["jobs"]["release-exporter"]
    steps = job["steps"]
    build = next(s for s in steps if _uses(s).startswith(_BUILD_ACTION))
    scan_idx = next(i for i, s in enumerate(steps) if _uses(s).startswith(_TRIVY_ACTION))
    if bad == "formal-tag-at-build":
        build["with"]["tags"] += "\nghcr.io/vencil/threshold-exporter:latest\n"
    elif bad == "chart-before-scan":
        push = next(s for s in steps if "helm push" in str(s.get("run") or ""))
        steps.remove(push)
        steps.insert(scan_idx, push)
    elif bad == "scan-by-tag":
        steps[scan_idx]["with"]["image-ref"] = "ghcr.io/vencil/threshold-exporter:v1.2.3"
    assert check_release_workflow(doc)
