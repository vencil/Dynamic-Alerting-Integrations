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
  3. every publishing step — promotion to formal tags, `helm push`, image
     signing (all three inside `release_publish.py image|chart` since #3848),
     `cosign sign-blob`, `syft` SBOM generation and the Release upload
     (`release_publish.py assets`) — sits AFTER that Trivy step;
  4. promotion and image signing name the same build-step digest.

Ordering is worth nothing if the scan cannot fail the job (#3870): `if:
false` on the Trivy step, `continue-on-error: true`, or `exit-code: 0` each
left the scan running and the release publishing, with this guard green. So
`check_scan_can_fail` reads the same parsed YAML and asserts, per job

  5. the Trivy step has no key but `name` / `uses` / `with` — no `if:`,
     `continue-on-error`, `env:`, `id:` (nothing may read its outcome);
  6. the job has no `continue-on-error`, and no `env:` at job or workflow
     level sets a `TRIVY_*` variable. Measured in trivy-action v0.36.0: an
     input equal to the action's default is not exported, so the caller's
     environment wins for it, and `TRIVY_CMD` replaces the binary outright;
  7. `with:` holds only vetted keys: `_trivy_contract.NEUTRAL_KEYS`,
     `exit-code` as the literal `1`, the shared `SCAN_FILTER` exactly
     (`severity: CRITICAL,HIGH`, `ignore-unfixed: true` — the stance is
     written in tests/_trivy_contract.py), and `trivyignores` only as the
     literal `components/<repo>/.trivyignore.yaml` of the very image the step
     scans. Any other input (`scanners`, `vuln-type`, `skip-dirs`,
     `trivy-config`, `skip-setup-trivy`, …) narrows or replaces the scan;
  8. no step after the scan has an `if:` calling `always()` / `failure()` /
     `cancelled()` — those run after a failed scan;
  9. the repo tracks no `trivy.yaml` at its root: Trivy loads it from the
     working directory by default, and it can set anything the action does
     not export (scanners, pkg types, skip dirs, ignore file).

What a waiver file may contain (id, statement, justification, a real
`expired_at`; never a plain-text `.trivyignore`) is governed in ONE place for
every workflow: test_every_waiver_entry_has_expiry_and_justification in
tests/ops/test_nightly_scan_matrix_drift.py. Rule 7 only decides which
register a release scan may read.

⛔ Threat model, as in test_release_immutable_wiring.py: this catches honest
mistakes and structural drift, not deliberate obfuscation. Out of scope: a
step that writes `trivy.yaml` / `.trivyignore` at run time, a `TRIVY_*`
variable injected some other way than a literal `env:` key, an edit to the
action's pinned sha (test_release_immutable_wiring.py allows the action, the
pin is Renovate's).

Vacuity: a guard that finds no publishing job passes while covering nothing,
so the discovered job set and the per-kind publish-step counts are asserted
non-empty.
"""
from __future__ import annotations

import re
from pathlib import Path

import copy
import subprocess

import pytest
import yaml

from _trivy_contract import NEUTRAL_KEYS, SCAN_FILTER, filter_mismatches

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"

_BUILD_ACTION = "docker/build-push-action"
_TRIVY_ACTION = "aquasecurity/trivy-action"

# Kinds of publishing step, keyed by how the step does it. Matched against the
# `run:` script of a step (shell commands), never against its name or comments.
# Since #3848 promotion, image signing, `helm push` and the Release upload run
# inside scripts/tools/dx/release_publish.py (the only publisher
# test_release_immutable_wiring.py allows), so its subcommands are what is
# matched here; the raw commands are kept so a step that bypassed the wrapper
# would still be ordered.
_PUBLISH_RUN_PATTERNS = {
    "promote": re.compile(r"\bimagetools\s+create\b|release_publish\.py\s+image\b"),
    "helm-push": re.compile(r"\bhelm\s+push\b|release_publish\.py\s+chart\b"),
    "cosign": re.compile(r"\bcosign\s+sign(-blob)?\b|release_publish\.py\s+image\b.*--sign\b"),
    "sbom": re.compile(r"\bsyft\s"),
    "gh-release": re.compile(r"\bgh\s+release\s+(create|upload)\b|release_publish\.py\s+assets\b"),
}
_IMAGE_SIGN = re.compile(r"\bcosign\s+sign\s|release_publish\.py\s+image\b.*--sign\b")


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
            if "promote" in kinds or _IMAGE_SIGN.search(script):
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


# ── the scan must be able to fail the job (#3870) ───────────────────────────

_TRIVY_STEP_KEYS = frozenset({"name", "uses", "with"})
_RELEASE_TRIVY_KEYS = NEUTRAL_KEYS | set(SCAN_FILTER) | {"exit-code", "trivyignores"}
_STATUS_FN = re.compile(r"\b(?:always|failure|cancelled)\s*\(")
_REGISTER = re.compile(r"components/([a-z0-9][a-z0-9-]*)/\.trivyignore\.yaml")
_SCANNED_REPO = re.compile(r"/([a-z0-9][a-z0-9._-]*)@\$\{\{")


def _trivy_env(env, where: str) -> list[str]:
    return [
        f"{where}: env sets {key} — trivy-action does not export an input equal to its "
        "default, so this overrides the scan (TRIVY_CMD replaces the binary)"
        for key in (env or {}) if str(key).upper().startswith("TRIVY_")
    ]


def _is_literal_one(value) -> bool:
    # `True == 1` in Python, so the type is checked, not just the value.
    return (type(value) is int and value == 1) or value == "1"


def check_scan_can_fail(doc: dict) -> list[str]:
    """Return every way the Trivy step could run without being able to stop the release."""
    problems = _trivy_env(doc.get("env"), "release.yaml")
    scanned_jobs = 0
    for job_name, job in (doc.get("jobs") or {}).items():
        steps = (job or {}).get("steps") or []
        trivy = [(i, s) for i, s in enumerate(steps) if _uses(s).startswith(_TRIVY_ACTION)]
        if not trivy:
            continue
        scanned_jobs += 1
        where = f"release.yaml::{job_name}"
        if "continue-on-error" in job:
            problems.append(f"{where}: job sets continue-on-error — a failed scan would not fail the release")
        problems += _trivy_env(job.get("env"), where)

        for scan_idx, scan in trivy:
            extra = sorted(set(scan) - _TRIVY_STEP_KEYS)
            if extra:
                problems.append(
                    f"{where}: Trivy step carries {extra} — only {sorted(_TRIVY_STEP_KEYS)} are allowed "
                    "(`if:` skips it, `continue-on-error` swallows it, `env:` overrides it, "
                    "an `id:` lets a later step read its outcome)"
                )
            with_ = scan.get("with") or {}
            unknown = sorted(set(with_) - _RELEASE_TRIVY_KEYS)
            if unknown:
                problems.append(
                    f"{where}: Trivy step passes unvetted input(s) {unknown} — inputs like "
                    "scanners / vuln-type / skip-dirs / trivy-config narrow or replace the scan"
                )
            if not _is_literal_one(with_.get("exit-code")):
                problems.append(
                    f"{where}: Trivy exit-code is {with_.get('exit-code')!r}, not the literal 1 — "
                    "a finding would not fail the job"
                )
            problems += [f"{where}: Trivy {m} — see tests/_trivy_contract.py" for m in filter_mismatches(with_)]
            if "trivyignores" in with_:
                ignores = with_["trivyignores"]
                scanned = _SCANNED_REPO.findall(str(with_.get("image-ref") or ""))
                m = _REGISTER.fullmatch(ignores) if isinstance(ignores, str) else None
                if not m or not scanned or m.group(1) != scanned[0]:
                    problems.append(
                        f"{where}: trivyignores {ignores!r} is not the literal "
                        f"components/<image>/.trivyignore.yaml of the image this step scans {scanned} — "
                        "a waiver register is scoped to the image it was written for"
                    )
                elif not (ROOT / ignores).is_file():
                    problems.append(f"{where}: trivyignores names a missing file {ignores!r}")

            for i, s in enumerate(steps[scan_idx + 1:], start=scan_idx + 1):
                cond = s.get("if")
                if cond is not None and _STATUS_FN.search(str(cond)):
                    problems.append(
                        f"{where}: step {s.get('name')!r} (#{i}) after the Trivy scan has "
                        f"`if: {cond}` — it runs even when the scan failed"
                    )
    if not scanned_jobs:
        problems.append("no job runs a Trivy step — guard would be vacuous")
    return problems


def root_trivy_config_problems(root: Path = ROOT) -> list[str]:
    """A `trivy.yaml` tracked at the repo root, which Trivy loads from its working directory."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "trivy.yaml"],
        cwd=root, capture_output=True, encoding="utf-8", timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return [
        f"{p} is tracked at the repo root — Trivy loads it by default and it can set what the "
        "action does not export (scanners, pkg types, skip dirs, an ignore file)"
        for p in out.stdout.split("\0") if p
    ]


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
        push = next(s for s in steps if "release_publish.py chart" in str(s.get("run") or ""))
        steps.remove(push)
        steps.insert(scan_idx, push)
    elif bad == "scan-by-tag":
        steps[scan_idx]["with"]["image-ref"] = "ghcr.io/vencil/threshold-exporter:v1.2.3"
    assert check_release_workflow(doc)


def test_the_release_scan_can_fail_the_release() -> None:
    problems = check_scan_can_fail(_load())
    assert not problems, "\n".join(problems)


def test_no_trivy_config_file_at_the_repo_root() -> None:
    """Trivy reads `trivy.yaml` from its working directory (the checkout root) by default.

    It can set what the action does not export — `scan.scanners`, `pkg.types`,
    `scan.skip-dirs`, an ignore file — and narrow the release gate with no
    change to release.yaml at all.
    """
    problems = root_trivy_config_problems()
    assert not problems, "\n".join(problems)


def _scan(doc: dict, job: str = "release-exporter") -> dict:
    return next(s for s in doc["jobs"][job]["steps"] if _uses(s).startswith(_TRIVY_ACTION))


def _after_scan(doc: dict, job: str = "release-exporter") -> dict:
    steps = doc["jobs"][job]["steps"]
    return next(s for s in steps if "release_publish.py chart" in str(s.get("run") or ""))


#: Each regression shape → a fragment of the message the rule that must catch it emits.
SCAN_MUTATIONS = {
    "step-if-false": (lambda d: _scan(d).update({"if": "false"}), "Trivy step carries ['if']"),
    "step-if-expr": (lambda d: _scan(d).update({"if": "${{ github.event_name == 'push' }}"}),
                     "Trivy step carries ['if']"),
    "step-continue-on-error": (lambda d: _scan(d).update({"continue-on-error": True}),
                               "Trivy step carries ['continue-on-error']"),
    "step-env-trivy-cmd": (lambda d: _scan(d).update(env={"TRIVY_CMD": "true"}),
                           "Trivy step carries ['env']"),
    "step-id": (lambda d: _scan(d).update(id="scan"), "Trivy step carries ['id']"),
    "job-continue-on-error": (lambda d: d["jobs"]["release-exporter"].update({"continue-on-error": True}),
                              "job sets continue-on-error"),
    "job-env-trivy": (lambda d: d["jobs"]["release-exporter"].update(env={"TRIVY_SCANNERS": "secret"}),
                      "env sets TRIVY_SCANNERS"),
    "workflow-env-trivy": (lambda d: d["env"].update(TRIVY_CMD="true"), "env sets TRIVY_CMD"),
    "exit-code-0": (lambda d: _scan(d)["with"].update({"exit-code": 0}), "exit-code is 0"),
    "exit-code-expr": (lambda d: _scan(d)["with"].update({"exit-code": "${{ vars.TRIVY_EXIT }}"}),
                       "not the literal 1"),
    "exit-code-true": (lambda d: _scan(d)["with"].update({"exit-code": True}), "not the literal 1"),
    "exit-code-removed": (lambda d: _scan(d)["with"].pop("exit-code"), "exit-code is None"),
    "severity-critical-only": (lambda d: _scan(d)["with"].update(severity="CRITICAL"), "severity='CRITICAL'"),
    "severity-low": (lambda d: _scan(d)["with"].update(severity="LOW"), "severity='LOW'"),
    "severity-removed": (lambda d: _scan(d)["with"].pop("severity"), "severity=None"),
    "ignore-unfixed-flipped": (lambda d: _scan(d)["with"].update({"ignore-unfixed": False}),
                               "ignore-unfixed=False"),
    "scanners": (lambda d: _scan(d)["with"].update(scanners="secret"), "unvetted input(s) ['scanners']"),
    "trivy-config": (lambda d: _scan(d)["with"].update({"trivy-config": "trivy.yaml"}),
                     "unvetted input(s) ['trivy-config']"),
    "skip-setup": (lambda d: _scan(d)["with"].update({"skip-setup-trivy": True}),
                   "unvetted input(s) ['skip-setup-trivy']"),
    "borrowed-register": (lambda d: _scan(d)["with"].update(
        trivyignores="components/recipe-preview/.trivyignore.yaml"), "of the image this step scans"),
    "register-expression": (lambda d: _scan(d, "release-recipe-preview")["with"].update(
        trivyignores="${{ 'components/recipe-preview/.trivyignore.yaml' }}"), "is not the literal"),
    "register-two-files": (lambda d: _scan(d, "release-recipe-preview")["with"].update(
        trivyignores="components/recipe-preview/.trivyignore.yaml,.trivyignore"), "is not the literal"),
    "register-missing": (lambda d: _scan(d, "release-exporter")["with"].update(
        trivyignores="components/threshold-exporter/.trivyignore.yaml"), "missing file"),
    "publish-if-always": (lambda d: _after_scan(d).update({"if": "always()"}), "runs even when the scan failed"),
    "publish-if-not-cancelled": (lambda d: _after_scan(d).update({"if": "${{ !cancelled() }}"}),
                                 "runs even when the scan failed"),
    "publish-if-failure": (lambda d: _after_scan(d).update({"if": "success() || failure()"}),
                           "runs even when the scan failed"),
}


def test_the_real_recipe_preview_register_is_accepted() -> None:
    """The one waiver the release reads today must pass rule 7 — else the mutations prove a broken rule."""
    with_ = _scan(_load(), "release-recipe-preview")["with"]
    assert with_.get("trivyignores") == "components/recipe-preview/.trivyignore.yaml"


@pytest.mark.parametrize("bad", sorted(SCAN_MUTATIONS))
def test_scan_guard_rejects_known_regressions(bad: str) -> None:
    """Each shape that disarms the scan must be refused BY ITS OWN RULE, not by a neighbour."""
    mutate, expected = SCAN_MUTATIONS[bad]
    doc = copy.deepcopy(_load())
    mutate(doc)
    problems = check_scan_can_fail(doc)
    assert any(expected in p for p in problems), f"{bad}: no problem mentions {expected!r}: {problems}"


def test_scan_guard_is_not_vacuous() -> None:
    doc = copy.deepcopy(_load())
    for job in doc["jobs"].values():
        job["steps"] = [s for s in job.get("steps") or [] if not _uses(s).startswith(_TRIVY_ACTION)]
    assert check_scan_can_fail(doc) == ["no job runs a Trivy step — guard would be vacuous"]


def test_root_trivy_config_probe_sees_a_tracked_file(tmp_path: Path) -> None:
    """The probe must answer from git's index, so a tracked file is seen and an untracked one is not."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / "trivy.yaml").write_text("scan:\n  scanners: [secret]\n", encoding="utf-8")
    assert root_trivy_config_problems(tmp_path) == []
    subprocess.run(["git", "-C", str(tmp_path), "add", "trivy.yaml"], check=True, timeout=60)
    problems = root_trivy_config_problems(tmp_path)
    assert len(problems) == 1 and problems[0].startswith("trivy.yaml is tracked"), problems
