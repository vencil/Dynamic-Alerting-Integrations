"""Drift guard for the nightly third-party scan matrix (#902 L1-A drift guard).

Closes the dual-SSOT gap raised in #907 review: the `scan-thirdparty` matrix in
nightly-image-scan.yaml hardcodes the 15 third-party refs, while the actual
deployment refs live in helm values / k8s manifests. If a maintainer bumps a
manifest (e.g. grafana 12.4.2 -> 12.5.0) but forgets the scan matrix, the scan
would keep reporting the OLD version as "safe" while prod runs the new one —
false security ("scanning a parallel universe").

This guard makes the L1-B extractor (which reads the real values/manifests) the
single source of truth and fails CI on drift:
  * scan-thirdparty matrix refs MUST equal `check_image_refs_resolve.py --list`
  * the report's EXPECTED counts MUST equal the matrix sizes (so the "X/Y images"
    + degraded-scan logic stays correct).

It also carries the GitHub-API contract guard added after the 33-night alerting
outage: `file_cve_report.sh` composed a label description of 111/117 characters
against a 100-character API cap, the 422 was swallowed by `|| true`, and the
third-party bucket filed zero alerts for a month. The behavioural test for that
script is a shell script that nothing ran (pytest does not collect `.sh`), so the
guard lives HERE — in a Python test the existing CI job already executes — and
checks the REAL call sites in the workflow rather than a synthetic fixture.

Those API-contract guards are now DISCOVERY-BASED (`nightly-*.y*ml` plus the
helper files each one references, plus the named extras in `_EXTRA_SCANNED`)
rather than pointed at nightly-image-scan.yaml + file_cve_report.sh by name. The
hardcoded form had the failure mode this file exists to prevent: it read like a
repo-wide rule while covering exactly one workflow, so #1275's nightly-race.yaml
/ file_race_report.py — a second label-keyed tracking issue, i.e. the same blast
radius — landed outside it by default.

Discovery brings a vacuity risk with it: a guard that finds NOTHING passes, and
reads as broad coverage while providing none. Three assertions exist solely to
make that impossible, and all are load-bearing rather than decoration:
  * `test_nightly_discovery_is_not_vacuous` pins the known workflow names, and
    requires every GLOBBED one to resolve at least one helper file (a global
    "some workflow resolved something" floor let a whole workflow sit uncovered).
  * The same test asserts each `_EXTRA_SCANNED` name really exists — a named
    entry that resolves to nothing is the same vacuity a bad glob causes.
  * `assert checked` in the description guard counts only descriptions that were
    actually LENGTH-CHECKED. Counting templated ones too let the assertion stay
    satisfied by strings it had deliberately skipped: an anti-vacuity guard that
    was itself vacuous.

Network-free (uses --list), so it runs in the plain Python Tests CI job.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS_DIR / "nightly-image-scan.yaml"
EXTRACTOR = ROOT / "scripts" / "ops" / "check_image_refs_resolve.py"
REPORT_SH = ROOT / "scripts" / "ops" / "file_cve_report.sh"

# Every nightly workflow, DISCOVERED rather than listed. The API-contract guards
# at the bottom of this file used to read only nightly-image-scan.yaml +
# file_cve_report.sh, so the next scheduled workflow to grow an issue-filing step
# landed outside them by default — the guard covered exactly one file while
# reading like a repo-wide rule. (nightly-race.yaml's file_race_report.py, added
# in #1275, is precisely such a file.)
#
# try-local-smoke.yaml is NAMED rather than globbed. It is a scheduled workflow
# with the same blast radius (a label-keyed tracking issue), it just lacks the
# `nightly-` prefix. It is listed because the sibling commit fixes its swallowed
# `gh label create` — and a fix that stays outside the guard is a fix nobody
# stops you from reverting.
#
# Still NOT a repo-wide glob, and the reason is no longer "there is an unfixed bug
# out there". It is that these guards cannot yet tell a label that is a DEDUP KEY
# (must fail loudly — losing it loses the alert) from a label that is DECORATION
# (should fail open — losing it must not discard the report).
# scripts/tools/ops/bench_report_pr.sh is the latter, so a repo-wide glob would
# flag it and be wrong. Making that distinction is its own design problem.
_EXTRA_SCANNED = ("try-local-smoke.yaml",)

NIGHTLY_WORKFLOWS = sorted(
    set(WORKFLOWS_DIR.glob("nightly-*.y*ml"))
    | {WORKFLOWS_DIR / name for name in _EXTRA_SCANNED}
)

# Helper files a nightly workflow references. BOTH scripts/ and tests/ — an
# earlier draft matched only `scripts/`, which made nightly-mutation-pilot.yaml's
# real helpers (`tests/shared/_mutation_pilot.py`, invoked at :96) structurally
# invisible while the guard still reported coverage of that workflow.
#
# Over-inclusive on purpose: a bare mention in a comment also matches, so an
# unrelated file can get scanned. That direction is safe (extra coverage); the
# opposite direction is the bug this whole file exists to prevent. The flip side
# is that coverage then depends on comment text, so the per-workflow assertion in
# test_nightly_discovery_is_not_vacuous is what keeps a deleted reference from
# silently shrinking the scanned set.
_SCRIPT_REF_RE = re.compile(r"((?:scripts|tests)/[\w./-]+\.(?:sh|py))")

# Widest suffix file_cve_report.sh appends to the base title at runtime:
# " — <total> fixable, <missing> unscanned". Bounded generously so the guard
# stays meaningful without re-implementing the shell string building.
TITLE_SUFFIX_BUDGET = 48


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _matrix_include(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["strategy"]["matrix"]["include"]


def _aggregate_run() -> str:
    steps = _workflow()["jobs"]["report"]["steps"]
    agg = next(s for s in steps if "Aggregate" in (s.get("name") or ""))
    return agg["run"]


def test_thirdparty_matrix_equals_deployed_refs() -> None:
    """scan-thirdparty matrix == the refs the extractor finds in values/manifests."""
    matrix_refs = {e["ref"] for e in _matrix_include("scan-thirdparty")}

    proc = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--root", str(ROOT), "--list"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    deployed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    assert matrix_refs == deployed, (
        "scan-thirdparty matrix drifted from the deployed third-party image set.\n"
        f"  only in scan matrix : {sorted(matrix_refs - deployed)}\n"
        f"  only in deployed    : {sorted(deployed - matrix_refs)}\n"
        "Sync the scan-thirdparty matrix in .github/workflows/nightly-image-scan.yaml "
        "with the chart values / k8s manifests (or adjust the extractor skip-lists)."
    )


def _third_party_pins_in_helm_values() -> dict[str, set[str]]:
    """Every third-party image ref pinned in ANY `helm/*/values*.yaml`, by file.

    ⛔ Independent of the extractor in exactly ONE respect — FILE SELECTION — and
    that is the respect that matters. The ref extraction and the skip lists are
    deliberately reused (`_refs_from_node` / `_repo_of` / `LOCAL_BUILT_IMAGES` /
    `SKIP_REPO_PREFIXES`), because re-implementing them here would just create a
    second contract to drift. What is NOT reused is `SOURCE_GLOBS`: this helper
    globs the tree itself, so a narrowing there cannot make this check narrow
    with it. #1302 was precisely that failure — both sides of the existing
    set-comparison came from one blind glob, so they stayed equal while an
    unscanned image shipped.

    ⚠️ So do not read this as "an independent second opinion on what a ref is".
    It is a second opinion on WHICH FILES get looked at, nothing more.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_extractor", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extractor"] = mod          # frozen dataclasses need this
    spec.loader.exec_module(mod)

    scanned = sorted(ROOT.glob("helm/*/values*.yaml"))
    # ⛔ Literal paths, and load-bearing for TWO reasons.
    # (1) Vacuity: a glob that stops matching would make every assertion below
    #     pass by finding nothing.
    # (2) verify_diff's text_map is built by scanning test files for LITERAL
    #     path strings. Without these, editing an overlay does not select the
    #     test written for it — measured: both were `None` in the map while
    #     `helm/da-portal/README.md` WAS registered, purely because a docstring
    #     happened to name it. Same trap as #1313.
    for known in ("helm/da-portal/values-tier1.yaml", "helm/da-portal/values-tier2.yaml"):
        assert ROOT / known in scanned, f"{known} is no longer in the sampling face"

    def _merge(base, over):
        """`helm -f` semantics: deep, key BY KEY. The whole point of #1302."""
        if isinstance(base, dict) and isinstance(over, dict):
            out = dict(base)
            for k, v in over.items():
                out[k] = _merge(base.get(k), v)
            return out
        return over

    out: dict[str, set[str]] = {}
    for path in scanned:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        # ⛔ An overlay is MERGED onto its base before extraction, because that is
        # what actually deploys — and because reading it standalone has a blind
        # spot that recreates #1302 through a MORE idiomatic path than the one
        # #1302 fixed. `_refs_from_node` shape-A needs `repository` AND `tag` in
        # the same node (check_image_refs_resolve.py), so an overlay that
        # overrides only `tag:` and inherits `repository:` extracts to NOTHING.
        # That is exactly the drift this module exists to catch: the merged ref
        # is `<base repo>:<overlay tag>@<base digest>` — a tag and a digest that
        # disagree, shipping unscanned. Verified: without this merge, injecting a
        # bare `oauth2Proxy.image.tag` into an overlay leaves every test green.
        if path.name != "values.yaml":
            base_path = path.parent / "values.yaml"
            if base_path.is_file():
                doc = _merge(yaml.safe_load(base_path.read_text(encoding="utf-8")), doc)
        refs = {r for r in mod._refs_from_node(doc)
                if mod._repo_of(r) not in mod.LOCAL_BUILT_IMAGES
                and not mod._repo_of(r).startswith(mod.SKIP_REPO_PREFIXES)}
        if refs:
            out[path.relative_to(ROOT).as_posix()] = refs
    return out


def test_every_helm_values_overlay_pin_is_in_the_scan_matrix() -> None:
    """⛔ #1302 — the SAMPLING FACE, not just the two sets.

    `test_thirdparty_matrix_equals_deployed_refs` compares the matrix against
    whatever the extractor finds. That is only a coverage guarantee while the
    extractor LOOKS at every place a deployed image can be pinned — and it did
    not: its glob was `helm/*/values.yaml`, so a `-f values-tier2.yaml` overlay
    (a documented install profile, helm/da-portal/README.md) was invisible to
    BOTH sides. Two sets derived from the same blind extractor stay equal while
    an image nobody scans ships.

    Measured before the fix: injecting a pin into `values-tier2.yaml` left that
    test GREEN; after widening the glob it goes RED. This assertion pins the
    property directly so the glob cannot quietly narrow again.

    ⚠️ Deliberately NOT asserting anything about `values-*.yaml` being allowed
    to pin at all — that is a chart-design question. The rule here is only:
    whatever they pin must be scanned.

    ⚠️ One known cost of that rule: `helm/vector/values-projection.yaml` says of
    itself that it is "NOT a deployment profile" — a synthetic render variant
    that exists so kube-linter can see the projection-gate container. It pins no
    image today, so this is inert; but if a future lint variant ever needs a
    pinned tag to exercise some render branch, that SYNTHETIC value would be
    required to sit in the real nightly CVE matrix. Prefer moving such a fixture
    out of `values*.yaml` over widening this rule.
    """
    matrix_refs = {e["ref"] for e in _matrix_include("scan-thirdparty")}
    missing: dict[str, set[str]] = {}
    for path, refs in _third_party_pins_in_helm_values().items():
        gap = refs - matrix_refs
        if gap:
            missing[path] = gap
    assert not missing, (
        "third-party image refs pinned in helm values are NOT in the nightly "
        "scan-thirdparty matrix — they deploy but are never CVE-scanned:\n"
        + "\n".join(f"  {p}: {sorted(g)}" for p, g in sorted(missing.items()))
        + "\nAdd them to .github/workflows/nightly-image-scan.yaml, or stop "
          "pinning them in the overlay so it inherits values.yaml (#1302)."
    )


def test_hardcoded_template_image_refs_are_covered_by_the_scan() -> None:
    """⛔ #1302, second sampling hole: a concrete ref written straight into a
    HELM TEMPLATE.

    The extractor cannot glob `helm/*/templates/**` — those files are not YAML
    until the Go-template actions are stripped, so `yaml.safe_load` throws. That
    is a sound reason to exclude them, but it leaves any ref hardcoded there
    outside the sampling face entirely. `helm/tenant-api/templates/deployment.yaml`
    has exactly such a ref (the alpine/git init-container), and today it happens
    to equal the copy in `k8s/04-tenant-api/deployment.yaml` — the one that IS
    extracted and IS in the matrix. Nothing enforced that equality: bump one and
    the nightly scan covers the other.

    Asserted as "must be in the scan matrix" rather than "must equal the k8s
    copy", because that is the property that actually matters and it keeps
    holding if the k8s manifest ever goes away.
    """
    # ⛔ NO `/` requirement, and BOTH YAML extensions. An earlier cut of this
    # regex demanded at least one slash, so a perfectly ordinary Docker Hub
    # official ref (`image: alpine:3.20`) was invisible to it — this gate would
    # have shipped with the same blind spot it exists to close. Neither shape
    # occurs in the repo today (the only hardcoded template ref is alpine/git,
    # and helm/ has no `.yml` templates), so this is coverage for the next one,
    # not a fix for a live miss. CodeRabbit caught it on #1302.
    # A trailing `# comment` and a registry PORT (`myreg.local:5000/x/y:1.0`)
    # both used to fall outside the tail anchor — measured, and the comment form
    # is the likelier of the two in this repo. Still NOT covered: a value on the
    # following line. That needs a real YAML parse, which is the thing templates
    # cannot have; stated here rather than left as a silent hole.
    concrete = re.compile(
        r"image:\s*[\"']?([a-z0-9][\w.\-]*(?::\d+)?(?:/[\w.\-]+)*:[\w.\-]+"
        r"(?:@sha256:[0-9a-f]{64})?)[\"']?\s*(?:#.*)?$")
    matrix_refs = {e["ref"] for e in _matrix_include("scan-thirdparty")}
    # ⛔ First-party detection is the `ghcr.io/vencil/` prefix and nothing else.
    # An earlier cut also excused refs found in the self-built `scan` matrix —
    # dead code: those entries carry `name`/`context`/`dockerfile` and no `ref`
    # at all, so the set was `{None}` and the branch never fired. Asserted, so
    # nobody re-adds it believing it works.
    assert {e.get("ref") for e in _matrix_include("scan")} == {None}, (
        "the self-built scan matrix grew a `ref` key — first-party exemption "
        "here is prefix-based on purpose; revisit before relying on it")

    # ⛔ A LIST, not a dict keyed by path: a template with two offending refs
    # would silently report only the last one — the same "truncated finding
    # list" shape this module exists to prevent.
    offenders: list[tuple[str, str]] = []
    templates = sorted(
        p for p in ROOT.glob("helm/*/templates/**/*")
        if p.suffix in (".yaml", ".yml"))
    assert templates, "no helm templates found — this scan would be a no-op"
    for path in templates:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # A fully commented-out line is not a deployed ref. Without this the
            # scan over-detects in the ANNOYING direction: a dead example ref in
            # a template comment would be demanded into the real CVE matrix,
            # polluting a matrix whose own workflow header warns about training
            # readers to ignore its nightly issue.
            if stripped.startswith("#"):
                continue
            m = concrete.search(stripped)
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith("ghcr.io/vencil/"):
                continue  # first-party: the release pipeline owns its currency
            if ref not in matrix_refs:
                offenders.append((path.relative_to(ROOT).as_posix(), ref))
    assert not offenders, (
        "a third-party image ref is hardcoded in a Helm TEMPLATE and is not in "
        "the nightly scan matrix — the extractor cannot see templates (they are "
        "not parseable YAML), so nothing else will catch it:\n"
        + "\n".join(f"  {p}: {r}" for p, r in sorted(offenders))
        + "\nEither add it to the matrix, or move the pin into values.yaml where "
          "the extractor samples it (#1302)."
    )


def test_the_extractor_samples_overlay_values_files() -> None:
    """⛔ The ONLY thing that catches the extractor's glob narrowing again.

    Not obvious, and worth spelling out: neither of the two set-comparisons can
    catch it. `test_thirdparty_matrix_equals_deployed_refs` derives BOTH sides
    from the extractor, so a narrower glob shrinks them together and stays
    green — that is #1302 itself. The overlay guard above globs the tree
    independently, so it also stays green while the extractor goes blind.

    Asserted BEHAVIOURALLY — the extractor's own globs are resolved against the
    real tree and compared to the files this module knows must be sampled. An
    earlier cut just grepped the source for the literal `"helm/*/values*.yaml"`,
    which is the same syntax-vs-behaviour weakness CodeRabbit flagged on the
    trigger test in this very PR: `helm/*/values*.yml` or a chart-scoped glob
    would satisfy the string check while sampling nothing.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_extractor_globs", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extractor_globs"] = mod
    spec.loader.exec_module(mod)

    sampled = {p for pattern in mod.SOURCE_GLOBS for p in ROOT.glob(pattern)}
    must_sample = sorted(ROOT.glob("helm/*/values*.yaml"))
    assert must_sample, "no helm values files found — this assertion would be vacuous"
    missing = [p.relative_to(ROOT).as_posix() for p in must_sample if p not in sampled]
    assert not missing, (
        f"check_image_refs_resolve.py's SOURCE_GLOBS no longer sample: {missing}\n"
        f"  globs: {mod.SOURCE_GLOBS}\n"
        "Overlay profiles (`-f values-tier2.yaml`) pin real deployed images; a "
        "glob that misses them makes those images invisible to the nightly scan "
        "AND to both set-comparisons in this module, which is #1302 (#1302)."
    )


def _gha_path_matches(pattern: str, target: str) -> bool:
    """GitHub Actions path-filter semantics: `**` spans separators, `*` does not.

    ⚠️ Deliberately STRICTER than the real engine (minimatch) in one respect:
    minimatch collapses `**` to zero directories, so `helm/**/values*.yaml`
    also matches `helm/values.yaml`; this does not. The direction is the safe
    one — it can only produce a false "not covered" (a nuisance failure),
    never a false "covered" (the dangerous one). Inert today: every overlay
    lives exactly one level under `helm/`.

    Module-level so the two trigger-face guards (overlay values files, and the
    customer-delivered pin source) grade with ONE translator. Two copies would
    drift, and the copy that drifts loose is the one that stops catching things.
    """
    rx = re.escape(pattern).replace(r"\*\*", "\x00").replace(r"\*", "[^/]*")
    return re.fullmatch(rx.replace("\x00", ".*"), target) is not None


def _resolve_workflow_paths() -> list[str]:
    wf = yaml.safe_load((WORKFLOWS_DIR / "image-ref-resolve.yaml").read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the boolean True (YAML 1.1).
    triggers = wf.get("on") or wf.get(True)
    return triggers["pull_request"]["paths"]


def test_the_resolve_workflow_triggers_on_overlay_values_files() -> None:
    """⛔ Scan face and TRIGGER face must widen together.

    `image-ref-resolve.yaml` is path-filtered. Widening the checker's globs
    without widening this filter yields a gate that can see overlay files but is
    never invoked when one changes — the shape where a control is present, runs,
    and still covers nothing. A PR touching only `values-tier2.yaml` is exactly
    the PR this check exists for.
    """
    paths = _resolve_workflow_paths()

    # ⛔ Asserted BEHAVIOURALLY — the filter must actually match the overlay
    # files that exist, not merely be spelled in a way that looks right. A
    # syntactic check (`some entry starts with helm/ and contains values*`) is
    # satisfied by `helm/nonexistent/values*.yaml`, which matches nothing. That
    # is the same "present but covers nothing" shape this whole module is about.
    overlays = [p.relative_to(ROOT).as_posix()
                for p in sorted(ROOT.glob("helm/*/values-*.yaml"))]
    assert overlays, "no overlay values files found — this assertion would be vacuous"

    _matches = _gha_path_matches
    unmatched = [o for o in overlays if not any(_matches(p, o) for p in paths)]
    assert not unmatched, (
        f"image-ref-resolve.yaml does not trigger on these overlay files: {unmatched}\n"
        f"  filter paths: {paths}\n"
        "The checker globs `helm/*/values*.yaml`; a filter that misses an overlay "
        "means a PR changing only that file never runs it (#1302)."
    )


# ── #1337 follow-up: the CUSTOMER-DELIVERED scan face ───────────────────────
# Everything above is about images WE install. `da-tools init` writes a FOURTH
# class into a CUSTOMER's repo — the GitLab apply stage's runner image and the
# git-sync sidecar/init container — and those sat in no automated view of a
# registry at all. Not by decision, by three independent misses that each read
# as deliberate scoping until checked against the question:
#   * the extractor's SOURCE_GLOBS cover `helm/` and `k8s/`, never `scripts/`;
#   * all three Renovate customManagers require `@sha256:` in the match, which
#     these refs deliberately do not carry (tag pins: the customer has no
#     updater of ours to re-resolve a digest);
#   * the offline pin guard that DOES exist says in its own docstring that it
#     checks floating-vs-concrete SHAPE and never currency.
#
# ⛔ Written out in full ON PURPOSE, same reason as the Dockerfile paths further
# down: verify_diff's text_map indexes LITERAL path strings, so a PR that bumps
# scripts/tools/ops/init_project.py has to select these tests for its diff. The
# constant is cross-checked against the checker's own tuple below so a MOVE of
# the file fails here rather than silently un-selecting the guard.
_DELIVERED_PIN_SOURCE = "scripts/tools/ops/init_project.py"

# Minimum number of DISTINCT third-party refs a generated customer repo carries.
#
# ⛔ A LITERAL, and that is the whole point. Every other number in this section
# is derived from either the pin table or the scan matrix, so a floor taken from
# one of those would shrink in lockstep with the thing it is guarding — the
# exact triviality `test_the_three_selfbuilt_build_lists_agree` had to relocate
# its own floor away from. Lowering this has to be a deliberate edit.
_DELIVERED_PRODUCT_FLOOR = 5

# Shape of a concrete image ref, applied to SCALARS of the generated YAML rather
# than to key names — so it sees the ref wherever the generator happens to put
# it. Today that is two structurally different places: an `image:` value in the
# kustomize git-sync patch, and a `variables:` entry (DA_HELM_IMAGE: ...) in the
# GitLab pipeline, where the job's own `image:` line is only `$DA_HELM_IMAGE`.
# A key-name rule would have seen one of those and missed the other.
#
# ⛔ ALL THREE DIGEST FORMS, not just `:tag`. The first draft ended in a
# mandatory `:tag`, which made `repo@sha256:…`, `repo:tag@sha256:…` and
# tagless refs ALL invisible — while the assertion this feeds exists precisely
# to catch a ref someone hardcoded into a generator function. A digest ref is
# the likeliest such ref (it is what a reviewer asks for when they see a tag),
# so the one shape most likely to be added by hand was the one shape the guard
# could not see.
#
# ⛔ NON-GOAL, deliberately: a TAGLESS ref (`nginx`, `alpine/helm`) stays
# unmatched. It is not an oversight and it must not be "fixed" — a bare
# `nginx` is character-for-character indistinguishable from any ordinary YAML
# scalar (`monitoring`, `main`, a rule-pack name), so accepting it would turn
# this from a ref detector into a string detector and every anti-vacuity floor
# below it would go meaningless. The floating-tag case is covered where it can
# be judged with the key in hand: the offline pin guard in the init-project
# suite, which reads the generator's own pin table rather than a scalar walk.
#
# Measured against the real products (2026-08-06, three deploy methods):
# 1175 scalars → exactly 5 matches — the 4 third-party refs plus the
# first-party da-tools ref filtered out below. Zero false positives. The digest
# widening did NOT change that count (the generated products carry no digest
# ref today); it is drift protection for the ref someone adds next.
_GENERATED_REF_SHAPE = re.compile(
    r"^[a-z0-9][\w.\-]*(?::\d+)?(?:/[\w.\-]+)*"          # host[:port]/path…
    r"(?::[\w][\w.\-]*(?:@sha256:[0-9a-f]{64})?"         # :tag  |  :tag@digest
    r"|@sha256:[0-9a-f]{64})$"                           # @digest (no tag)
)

# ⛔ PAIRED samples, and the pairing is the point: a widening with only positive
# samples drifts toward "matches everything" and the negative half is what stops
# it. Both lists are asserted in one test so neither can be quietly dropped.
_A_DIGEST = "sha256:" + "0123456789abcdef" * 4  # 64 hex chars; SHAPE is the subject


def _delivered_pins_from_generator() -> tuple[str, ...]:
    """The four customer-delivered pins, read from the generator that owns them.

    Keeps the positive samples honest across a legitimate bump, and keeps this
    file from becoming a third place a ref is spelled out.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_extractor_pins", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extractor_pins"] = mod
    spec.loader.exec_module(mod)
    pins = tuple(mod.delivered_refs(ROOT))
    # ⛔ A FLOOR, not `== 4`. The sentence below promises the sample set follows a
    # legitimate count change automatically, and an equality check is precisely
    # what stops it doing so — the message described the behaviour the reader
    # wanted rather than the one the code had. `_DELIVERED_PRODUCT_FLOOR` already
    # holds this number and the sibling floors in this file already compare
    # against it, so the literal was another copy of it. (No count here on
    # purpose: `grep -c '>= _DELIVERED_PRODUCT_FLOOR'` is the answer, and an
    # approximation in prose is the defect this very edit is correcting.)
    assert len(pins) >= _DELIVERED_PRODUCT_FLOOR, (
        f"expected at least {_DELIVERED_PRODUCT_FLOOR} customer-delivered pins, "
        f"got {len(pins)}: {pins}. A DROP is the real signal here — the sample "
        "set follows a legitimate increase automatically, but check that the "
        "nightly bucket and its EXPECTED count moved too."
    )
    return pins


def _ref_shape_must_match() -> tuple[str, ...]:
    """Sample refs the shape MUST match — built on call, never at import.

    ⛔ This used to be a module-scope tuple whose first element was
    ``*_delivered_pins_from_generator()``, i.e. the generator ran during import.
    The helper can raise ``SystemExit`` (a missing, empty, or partially blank pin
    table is a deliberate hard exit), and at import time that does NOT surface as
    a failed test.

    ⚠️ An earlier version of this note called it "a collection error" affecting
    "~55 guards in this file". Both halves were wrong in the same direction —
    towards sounding contained. MEASURED: pytest ends with ``INTERNALERROR`` and
    ``no tests ran``, and it takes down the WHOLE invocation, not just this module
    (reproduced by running this file together with another). The guard count is
    derivable (``pytest --collect-only``) and was 53 at the time, so it does not
    belong in prose as an approximation either.
    `test_nothing_in_this_module_reads_the_pin_table_at_import_time` is the
    tripwire that keeps this true.
    """
    return (
        # ⛔ The real pins are IMPORTED, not transcribed. They used to be four
        # literals labelled "verbatim", which is a claim that expires on the first
        # legitimate bump: a coordinated `v3.5.0 -> v3.9.9` in both the generator
        # and the scan matrix left all tests green while the comment silently
        # became a lie — the samples stayed valid *shapes*, just no longer the
        # real pins. Importing them also removes a third copy: the checker's own
        # docstring said "there are exactly two places a ref exists", and
        # transcribing them here made that three.
        *_delivered_pins_from_generator(),
        # First-party — matched by SHAPE, dropped later by the ghcr.io/vencil/
        # rule. It belongs here so that filter stays the thing doing the filtering.
        "ghcr.io/vencil/da-tools:v9.9.9",
        # A registry with an explicit port.
        "localhost:5000/team/thing:1.0",
        # The three digest forms the first draft missed.
        f"alpine/k8s@{_A_DIGEST}",
        f"alpine/k8s:1.34.9@{_A_DIGEST}",
        f"quay.io/argoproj/argocd@{_A_DIGEST}",
    )


_REF_SHAPE_MUST_NOT_MATCH = (
    # Tagless — the documented non-goal above. Listed so that "we chose not to"
    # is enforced rather than remembered.
    "nginx",
    "alpine/helm",
    # Ordinary scalars the generated products are full of.
    "monitoring",
    "db-a",
    "main",
    "conf.d",
    "60",
    "0 0 * * *",
    "https://example.com/r.git",
    # A digest branch loose enough to accept these would accept prose too.
    "alpine/k8s@sha256:deadbeef",       # too short to be a sha256
    "alpine/k8s@md5:0123456789abcdef",  # not sha256
    f"alpine/k8s@SHA256:{_A_DIGEST[7:]}",  # algorithm is lowercase in OCI refs
)


def _delivered_refs_via_cli() -> set[str]:
    """The pin table, read through the checker's `--scope delivered`.

    Deliberately the CLI and not a direct import of init_project.py: this is the
    path CI actually runs, so a `--scope` that silently stopped resolving the pin
    table would red HERE instead of only in a workflow nobody reads the log of.
    """
    proc = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--root", str(ROOT), "--list",
         "--scope", "delivered"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _refs_in_a_generated_customer_repo() -> tuple[set[str], int, int]:
    """Third-party refs that actually appear in a tree `da-tools init` writes.

    ⛔ Derived from the PRODUCTS, not from the pin table — that is what makes it
    usable as an anti-vacuity floor for a guard whose other side IS the pin
    table. It also catches a class the pin-table comparison structurally cannot:
    a third-party ref hardcoded straight into a generator function, never routed
    through `_GITLAB_APPLY_IMAGES` at all.

    All three deploy methods, because the apply image is chosen per method (only
    the kustomize tree carries alpine/k8s, only the helm tree alpine/helm, and so
    on), and `config_source='git'` because the git-sync overlay is emitted on no
    other path. Returns (refs, files_walked, scalars_checked) so the caller can
    fail on a walk that collapsed rather than on an empty result that looks calm.

    ⚠️ Honest boundary: only YAML products are parsed. A ref that a future
    generator writes into the emitted README, or into a shell snippet, is
    invisible here — the pin-table equality is what covers refs added the normal
    way, and this is the second opinion on where they LAND, not a total scan.
    """
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "_delivered_init_project", ROOT / _DELIVERED_PIN_SOURCE)
    assert spec is not None and spec.loader is not None, (
        f"cannot load {_DELIVERED_PIN_SOURCE} — this guard must not degrade to "
        "'no refs found', which reads exactly like a clean generator")
    ip = importlib.util.module_from_spec(spec)
    # ⛔ Same containment as `delivered_refs`, for the same reason: executing the
    # pin source inserts four `sys.path` entries off its own `__file__` and caches
    # nine first-party modules, none of which it removes. This helper runs EARLIER
    # in the session than the guard that watches for the leak — so its entries are
    # already inside that guard's baseline snapshot and it is structurally
    # invisible there.
    #
    # ⛔ ERRATUM — a snapshot/restore was added here as a "fix the class, not the
    # instance" move, with a comment claiming both by-path loads were now
    # contained. Review measured all three halves of that claim false:
    #   * it did not contain anything — `ip.run_init(...)` runs BELOW, outside the
    #     window, and re-imports every name the rollback had just dropped, so the
    #     end state was identical to having no rollback at all;
    #   * nothing observed it — deleting the path restore, and separately the
    #     module eviction, each left the suite at 66 passed. It was the one
    #     mechanism in this file that was fully claimed and fully unwitnessed;
    #   * and the class was not closed anyway — `tests/ops/test_init_project.py`
    #     and `tests/ops/test_generated_ci_artifacts.py` do module-scope
    #     `sys.path.insert` + `import init_project` and never undo it. Those are
    #     deliberate (the module under test must be importable).
    # So it is removed rather than repaired: this is a test helper in a process
    # that exits, the leak costs nothing measurable here, and carrying unguarded
    # machinery that a comment describes as protection is worse than not having
    # it. The containment that DOES matter is in `delivered_refs`, where the
    # caller can be a customer tree, and it has two assertions on it.
    sys.modules["_delivered_init_project"] = ip
    spec.loader.exec_module(ip)

    def _scalars(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from _scalars(k)
                yield from _scalars(v)
        elif isinstance(node, list):
            for v in node:
                yield from _scalars(v)
        elif isinstance(node, str):
            yield node

    refs: set[str] = set()
    files = scalars = 0
    for deploy in ("kustomize", "helm", "argocd"):
        with tempfile.TemporaryDirectory() as tmp:
            ip.run_init({
                "ci": "both",
                "deploy": deploy,
                "rule_packs": ["mariadb"],
                "tenants": ["db-a"],
                "namespace": "monitoring",
                # ⛔ The real default, not a sentinel. A sentinel was fine while
                # first-party refs were excluded from the derivation below; now
                # that they are not, feeding a fake value would make this guard
                # compare the watched set against a ref no customer ever gets —
                # green for the wrong reason. Read from the generator so a bump
                # of the tool tag flows here without an edit.
                "da_tools_image": ip.DA_TOOLS_IMAGE,
                # git-sync only reaches a customer on this path.
                "config_source": "git",
                "git_repo": "https://example.com/r.git",
                "git_branch": "main",
                "git_path": "conf.d",
                "git_period": 60,
            }, tmp)
            for path in sorted(Path(tmp).rglob("*")):
                if not path.is_file():
                    continue
                files += 1
                if path.suffix not in (".yaml", ".yml"):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for doc in yaml.safe_load_all(text):
                    for value in _scalars(doc):
                        scalars += 1
                        candidate = value.strip()
                        # `$DA_HELM_IMAGE` / `${{ env.X }}` are references TO a
                        # ref, not a ref — the concrete value is the variables
                        # entry they point at, and it is collected on its own.
                        if "$" in candidate or "{" in candidate:
                            continue
                        if not _GENERATED_REF_SHAPE.match(candidate):
                            continue
                        # ⛔ NO first-party exclusion here any more, and the
                        # removal is the finding. It used to skip everything under
                        # `ghcr.io/vencil/` "because first-party currency is the
                        # release flow's job (#902 rejected first-party digest
                        # pinning)". #902 rejected DIGEST PINNING; it never
                        # answered "does this ref resolve" or "what CVEs does the
                        # published artifact carry". Inheriting a skip into a
                        # question it was not written for is the same move whose
                        # ⛔ note in the extractor says it is how
                        # federation-audit-sidecar shipped unscanned for months.
                        #
                        # What it hid: `ghcr.io/vencil/da-tools` is in 18/18
                        # generated projects — measured by generating the full
                        # option matrix and parsing the trees — while no project
                        # carries more than two of the four third-party pins and
                        # three carry none. The one image every customer runs was
                        # the one this guard could not see.
                        #
                        # Consequence of removing it: a future first-party ref
                        # appearing in generated output reds this test until
                        # someone decides where it is watched. That is the right
                        # failure — it is exactly the decision that was skipped
                        # by inheritance last time.
                        refs.add(candidate)
    return refs, files, scalars


def test_the_generated_ref_shape_sees_every_pin_form_and_no_prose() -> None:
    """Paired samples for `_GENERATED_REF_SHAPE` — both directions, one test.

    A shape rule exercised only against today's four tag pins can drift either
    way without anything noticing, and BOTH directions have a named failure:

      * too TIGHT — the shape ended in a mandatory `:tag`, so every digest form
        was a MISS while the assertion it feeds claims to catch a ref hardcoded
        into a generator. A digest ref is the likeliest such ref;
      * too LOOSE — once widened, the temptation is to accept a bare `nginx`
        too, at which point the rule matches ordinary YAML scalars and the
        `len(refs) >= _DELIVERED_PRODUCT_FLOOR` floor below stops meaning
        anything (it would be satisfied by four random strings).

    ⚠️ Honest boundary: this pins the SHAPE only. That a real generated tree
    contains no scalar which merely looks like a ref is a separate,
    measured claim — `test_every_ref_a_generated_customer_repo_carries_is_scanned`
    is where it is checked against the products.
    """
    must_match = _ref_shape_must_match()
    assert must_match and _REF_SHAPE_MUST_NOT_MATCH, (
        "both sample lists must be non-empty — an empty one makes its half of "
        "this test vacuous while the other half keeps it green")

    missed = [s for s in must_match if not _GENERATED_REF_SHAPE.match(s)]
    assert not missed, (
        f"_GENERATED_REF_SHAPE does not match these real ref forms: {missed}\n"
        "A form it cannot see is a form the customer-delivered coverage guard "
        "silently ignores — that was the `@sha256:` gap."
    )

    over = [s for s in _REF_SHAPE_MUST_NOT_MATCH if _GENERATED_REF_SHAPE.match(s)]
    assert not over, (
        f"_GENERATED_REF_SHAPE now matches things that are not image refs: {over}\n"
        "Widening it far enough to match ordinary scalars turns the ref walk "
        "into a string walk and makes the anti-vacuity floors below vacuous. "
        "If one of these genuinely IS a ref form we deliver, move it to "
        "_ref_shape_must_match() deliberately — do not just loosen the pattern."
    )


def test_delivered_matrix_equals_the_customer_pin_table() -> None:
    """scan-delivered matrix == the refs `da-tools init` hands a customer.

    Same contract as `test_thirdparty_matrix_equals_deployed_refs`, for the other
    scan face. Neither side is derived from the other: the matrix is a literal in
    the workflow YAML, the pin table is imported from init_project.py through the
    checker's `--scope delivered`. Bumping one without the other reds here.

    ⭐ COUNTERFACTUAL (measured, not assumed): with init_project.py restored whole
    from origin/main this test is GREEN — the four pins are unchanged on base, so
    this assertion buys DRIFT protection going forward, not a live find. The
    assertion that goes RED on base is the workflow trigger one below, and the
    detection the PR actually buys is the nightly bucket itself, which no test can
    stand in for. Do not restate this as "#1337's four pins were wrong".
    """
    matrix_refs = {e["ref"] for e in _matrix_include("scan-delivered")}
    pinned = _delivered_refs_via_cli()

    # ⛔ Floor BEFORE the equality, and taken from neither side of it: ∅ == ∅
    # passes, and both sides are things a single careless edit can empty (delete
    # the matrix entries, or empty `_GITLAB_APPLY_IMAGES`).
    assert len(pinned) >= _DELIVERED_PRODUCT_FLOOR, (
        f"the customer-delivered pin table resolved to only {len(pinned)} ref(s) "
        f"({sorted(pinned)}) — expected at least {_DELIVERED_PRODUCT_FLOOR}. Either "
        "pins were removed (then lower the floor deliberately) or `--scope "
        "delivered` stopped reading the table, which would make the equality "
        "below pass over nothing."
    )
    assert matrix_refs == pinned, (
        "the nightly scan-delivered matrix drifted from the refs `da-tools init` "
        "writes into a customer repo.\n"
        f"  only in scan matrix : {sorted(matrix_refs - pinned)}\n"
        f"  only in the pin table: {sorted(pinned - matrix_refs)}\n"
        "Sync the scan-delivered matrix in .github/workflows/nightly-image-scan.yaml "
        f"with the pin table in {_DELIVERED_PIN_SOURCE}. ⚠️ The matrix is a MIRROR "
        "of that table — do not 'improve' it by adding a digest here; the tag-only "
        "form is a decision made at the source (the customer has no updater of ours)."
    )


def test_every_ref_a_generated_customer_repo_carries_is_scanned() -> None:
    """The second opinion: run the generator and look at what it WROTE.

    `test_delivered_matrix_equals_the_customer_pin_table` compares the matrix to
    the pin table. That is only a coverage guarantee while every delivered ref
    goes THROUGH the pin table — and nothing structurally forces that. A ref
    hardcoded straight into a generator function (`_gen_gitlab_ci`,
    `_gen_git_sync_deployment`, or the next generator someone adds) ships to a
    customer while both sides of that equality stay in perfect agreement. This is
    the same failure #1302 was: two sets derived from one blind source.

    So this one runs `run_init` for all three deploy methods and reads the actual
    files, exactly as the sibling guard in the init-project suite does for
    floating tags — that suite's docstring records why a hand-listed set of
    generators was the hole, not the scan.
    """
    refs, files, scalars = _refs_in_a_generated_customer_repo()

    # Anti-vacuity on the WALK, before anything about the refs: a generator that
    # stopped emitting, or a rglob that stopped matching, produces an empty ref
    # set that reads exactly like "nothing unscanned ships".
    assert files >= 24, (
        f"only {files} files generated across three deploy methods — run_init "
        "stopped emitting, or the walk broke. The ref assertions below would "
        "pass over nothing."
    )
    assert scalars >= 500, (
        f"only {scalars} YAML scalars inspected across {files} generated files — "
        "the products stopped parsing as YAML, or the scalar walk broke."
    )
    assert len(refs) >= _DELIVERED_PRODUCT_FLOOR, (
        f"only {len(refs)} distinct third-party ref(s) found in the generated "
        f"customer tree ({sorted(refs)}), expected at least "
        f"{_DELIVERED_PRODUCT_FLOOR}. Either the generator stopped writing one "
        "(check the deploy-method branch), or the ref shape rule drifted."
    )

    matrix_refs = {e["ref"] for e in _matrix_include("scan-delivered")}
    unscanned = sorted(refs - matrix_refs)
    assert not unscanned, (
        "third-party image ref(s) written into a CUSTOMER's repo are not in the "
        f"nightly scan-delivered matrix: {unscanned}\n"
        "They are executed by the customer's pipeline (the apply stage carries "
        "`environment: name: production` plus cluster-write credentials) and "
        "nothing else looks at them — not Renovate (its customManagers all "
        "require `@sha256:` and none reach `scripts/**`), not the deploy-scope "
        "extractor (it globs `helm/` and `k8s/`).\n"
        "Route the ref through the pin table and add it to the matrix in "
        ".github/workflows/nightly-image-scan.yaml."
    )


def _strip_bash_comment(line: str) -> str:
    """Everything on ONE logical line before the bash comment starts.

    ⛔ Derived from the shell rule, not from a list of shapes someone named.
    Bash starts a comment at a `#` that BEGINS A WORD — start of line, or after
    unquoted whitespace — and never inside `'` or `"`. Every shape that beat the
    previous `line.lstrip().startswith("#")` form is the same rule read properly:

      * `cmd --scope deploy   # was --scope delivered`  (trailing; not line-initial)
      * `run: >` folded scalars, where YAML joins the lines with spaces before
        anything shell-shaped exists, so a comment on its own SOURCE line is
        mid-line by the time bash sees it (measured, not assumed)
      * `\\#` — a backslash before it means it is not a comment, and the
        "preceded by whitespace" rule already says so without a special case.

    Error direction is deliberate: if this strips something that bash would have
    kept, callers see LESS text, so an "is it invoked" assertion gets harder to
    satisfy — a false RED, never a silent pass.
    """
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


# Tokens shlex(punctuation_chars=True) emits that END one command and begin the
# next. Without the split, `echo --scope delivered && real_cmd --scope deploy`
# would present one argv holding both, and a prose `echo` would satisfy an
# assertion about the real command.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", ";;", "|", "&", "(", ")"})


def _shell_argvs(run_text: str) -> list[list[str]]:
    """Every command in a `run:` block as an argv list.

    ⛔ The subject of an "is X invoked" assertion is the COMMAND, not the text
    of the block. Substring-scanning the block reads the workflow's own English:
    `echo "reproduce locally with check_image_refs_resolve.py --scope delivered"`
    contains the string and invokes nothing. Parsing to argv makes that
    impossible — `--scope delivered` inside a quoted echo argument is ONE token,
    so it can never look like the two adjacent tokens a real invocation has.
    """
    folded = re.sub(r"\\\n\s*", " ", run_text)  # join `\`-continued lines first
    argvs: list[list[str]] = []
    for raw in folded.split("\n"):
        line = _strip_bash_comment(raw).strip()
        if not line:
            continue
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            tokens = list(lex)
        except ValueError as exc:  # unbalanced quote — refuse to grade blindly
            raise AssertionError(
                f"could not tokenise a `run:` line as a shell command ({exc}): "
                f"{line!r}\nThis guard reads real argv; teach it the shape or "
                "simplify the line rather than letting it grade a half-parse."
            ) from exc
        current: list[str] = []
        for tok in tokens:
            if tok in _SHELL_SEPARATORS:
                if current:
                    argvs.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            argvs.append(current)
    return argvs


def _names_label(text: str, label: str) -> bool:
    """True iff `text` names `label` as a whole token.

    ⛔ The boundary comes from the LABEL, never from a hardcoded family prefix.
    Both directions are load-bearing and each was wrong at some point:
      * `label in text` reports a short label as present when only a longer one
        containing it was written (`nightly-cve` inside `nightly-cve-thirdparty`);
      * extracting `nightly-cve[\\w-]*` tokens instead fixes that but bakes the
        current family in, so a fourth bucket named anything else is reported
        missing however loudly the step names it — a red whose remedy is already
        done, which is how a guard teaches people to ignore it.
    """
    return re.search(rf"(?<![\w-]){re.escape(label)}(?![\w-])", text) is not None


def _invokes_with_flag(argvs: list[list[str]], script: str, flag: str, value: str) -> bool:
    """True iff some single command runs `script` AND passes `flag value`.

    Both halves must be in the SAME argv: a workflow that runs the script with
    one scope and merely mentions the other elsewhere has not run the other.
    """
    for argv in argvs:
        if not any(tok == script or tok.endswith("/" + script) for tok in argv):
            continue
        if f"{flag}={value}" in argv:
            return True
        for i, tok in enumerate(argv[:-1]):
            if tok == flag and argv[i + 1] == value:
                return True
    return False


# WHOLE `if:` expressions that provably do not narrow which events reach a node.
# ⛔ Matched EXACTLY, never by substring: the real expression below is
# `!cancelled() && steps.X.conclusion != 'skipped'`, so a substring test for
# `!cancelled()` would wave through the second clause — which does narrow — and
# through anything a future edit appends to it. Unknown expression ⇒ treated as
# a gate ⇒ the caller reds. Fail-closed, and the allowlist has to be widened
# deliberately with the reasoning written down, as below.
_NON_NARROWING_IF = {
    "always()": "runs regardless of upstream outcome — strictly wider.",
    "!cancelled()": "only relaxes the implicit success() — strictly wider.",
    # ⛔ REMOVED, and the removal is the lesson. This table used to carry
    # `!cancelled() && steps.deploy_scope.conclusion != 'skipped'`, exempted on the
    # reasoning that an unconditional step "cannot be 'skipped'" and an un-run one
    # is absent from the context. That is inverted: a step with no `if:` carries
    # the implicit `success()`, so an earlier failure records it as
    # `conclusion: skipped` — present — and the clause narrowed exactly where the
    # entry said it did not. ⇒ A fail-open exemption justified by a false premise,
    # in the one table whose entries are graded on prose rather than on shape.
    # The workflow dropped the clause instead of re-arguing it. Rule of thumb for
    # the next entry here: if the justification needs a paragraph about context
    # semantics, delete the condition instead.
}


def _if_narrows(node: dict) -> bool:
    """True iff this job/step carries a condition this module cannot see past.

    ⛔ Reading `run:` and ignoring `if:` is how an "is it invoked" guard degrades
    into an "is it written down" guard. Measured on this very workflow: adding
    `if: ${{ github.event_name == 'workflow_dispatch' }}` to the delivered step
    left the whole suite green, and so did `if: false` on the entire job — while
    the PR-time re-resolution the assertion exists to guarantee stopped
    happening. The disabling mechanism was never read, only the text beside it.
    """
    cond = str(node.get("if", "")).strip()
    if not cond:
        return False
    if cond.startswith("${{") and cond.endswith("}}"):
        cond = cond[3:-2].strip()
    return cond not in _NON_NARROWING_IF


# `|| true` / `|| :` after a command discards its exit status. Folded first, so
# a `\`-continued invocation with the swallow on the next physical line counts.
_SWALLOW_RE = re.compile(r"\|\|\s*(?:true|:)\s*(?:$|[;&|])")


def _disarmed(node: dict) -> str | None:
    """Reason this job/step's FAILURE cannot fail the run, or None.

    ⛔ `if:` is only one of the disabling mechanisms, and `_if_narrows` above
    argues the point for it convincingly enough that it reads like the whole
    story. It is not: `continue-on-error: true` is the YAML spelling of
    `|| true`, and it leaves the step running, green, and unable to report
    anything. Measured on this workflow: adding `continue-on-error: true` to the
    delivered-scope step AND `|| true` to its command left all 42 tests passing,
    while the only output that step has — pass/fail — was gone.

    Fail-closed on expressions: a `continue-on-error: ${{ ... }}` cannot be
    evaluated here, and "I cannot tell whether failure counts" must not be
    graded as "failure counts".
    """
    raw = node.get("continue-on-error", False)
    if raw is False:
        return None
    if raw is True:
        return "continue-on-error: true"
    return f"continue-on-error: {raw!r} (not statically decidable)"


def _workflow_argvs(name: str) -> tuple[list[list[str]], list[str]]:
    """(argv of every unconditionally-reached `run:` step, labels of gated ones).

    Nodes behind a condition this module cannot evaluate — or whose failure is
    discarded — are EXCLUDED from the argv list and reported separately, so a
    caller asserting "the workflow really runs X" fails with the real reason
    ("X sits behind a gate I cannot read" / "X's failure is swallowed") rather
    than the misleading "X is not invoked".
    """
    wf = yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
    argvs: list[list[str]] = []
    gated: list[str] = []
    for job_name, job in wf["jobs"].items():
        if _if_narrows(job):
            gated.append(f"job {job_name} (if: {job['if']})")
            continue
        if (reason := _disarmed(job)):
            gated.append(f"job {job_name} ({reason})")
            continue
        for step in (job.get("steps") or []):
            label = f"{job_name}/{step.get('name', '<unnamed>')}"
            if _if_narrows(step):
                gated.append(f"step {label} (if: {step['if']})")
                continue
            if (reason := _disarmed(step)):
                gated.append(f"step {label} ({reason})")
                continue
            folded = re.sub(r"\\\n\s*", " ", step.get("run") or "")
            for line in folded.split("\n"):
                if _SWALLOW_RE.search(_strip_bash_comment(line)):
                    gated.append(
                        f"step {label} (failure swallowed: {line.strip()[:60]})"
                    )
                    continue
                argvs.extend(_shell_argvs(line))
    return argvs, gated


def test_the_resolve_workflow_covers_the_delivered_pin_source() -> None:
    """⛔ Scan face and TRIGGER face must widen together — the #1302 lesson again.

    `check_image_refs_resolve.py` grew a `--scope delivered`, and
    `image-ref-resolve.yaml` grew a step that runs it. Neither is worth anything
    unless a PR that CHANGES a pin actually starts the workflow, and the file
    those pins live in is not matched by any of the pre-existing path filters
    (`helm/**/values*.yaml`, `k8s/**`, the checker itself, the workflow itself).

    Three separate properties, because each fails on its own:
      1. the pin source is where this module thinks it is;
      2. some `paths:` entry matches it, graded BEHAVIOURALLY (a filter spelled
         `scripts/tools/ops/*.py` looks right and would also work, one spelled
         `scripts/ops/init_project.py` looks right and matches nothing);
      3. the workflow actually INVOKES `--scope delivered` — a trigger that runs
         only the deploy scope is a gate that starts and covers nothing.
    """
    # (1) Cross-check the literal against the checker's own tuple, so moving the
    # file reds here rather than silently un-selecting this test from the diff.
    import importlib.util

    spec = importlib.util.spec_from_file_location("_extractor_delivered", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extractor_delivered"] = mod
    spec.loader.exec_module(mod)
    assert "/".join(mod.DELIVERED_PIN_SOURCE) == _DELIVERED_PIN_SOURCE, (
        f"check_image_refs_resolve.DELIVERED_PIN_SOURCE points at "
        f"{'/'.join(mod.DELIVERED_PIN_SOURCE)!r}, this module expects "
        f"{_DELIVERED_PIN_SOURCE!r}. Update BOTH — including the literal here, "
        "which is what selects this test for a diff touching that file."
    )
    assert (ROOT / _DELIVERED_PIN_SOURCE).is_file(), (
        f"{_DELIVERED_PIN_SOURCE} does not exist — every assertion here would be "
        "reasoning about a file that is gone")

    # (2) Trigger face.
    paths = _resolve_workflow_paths()
    assert not any(p.lstrip().startswith("!") for p in paths), (
        f"`on.pull_request.paths` uses negation patterns, which this translator "
        f"cannot evaluate — it would report the pin source as covered while "
        f"GitHub excluded it. paths: {paths}")
    assert any(_gha_path_matches(p, _DELIVERED_PIN_SOURCE) for p in paths), (
        f"image-ref-resolve.yaml does not trigger on {_DELIVERED_PIN_SOURCE}\n"
        f"  filter paths: {paths}\n"
        "That file IS the pin table the `delivered` scope reads, so a PR that "
        "bumps one of the four customer-facing refs would never re-resolve them "
        "— a gate that can see the pins and is never invoked when one changes "
        "(#1302's shape, #1337's subject)."
    )


    # (3) Scan face: a real command has to run the checker in that scope.
    # ⛔ Graded on parsed argv, never on the text of the block. Three shapes beat
    # a text scan — measured, all three left the suite green: a trailing `#`
    # comment, a `run: >` folded scalar (YAML moves the `#` off line-start), and
    # an `echo` that merely mentions the flag. A guard whose subject is "is it
    # invoked" must not be satisfiable by prose about invoking it, and requiring
    # script and flag in the SAME argv is what makes that structural.
    argvs, gated = _workflow_argvs("image-ref-resolve.yaml")
    # ⛔ BOTH scopes. The delivered half is what this PR added, but the deploy
    # half is the original #897 gate and it lost its protection the moment one
    # invocation became two named steps: deleting the deploy step outright left
    # the suite green. A guard that only watches the new half converts every
    # future edit of the old half into a silent removal.
    for scope in ("deploy", "delivered"):
        assert _invokes_with_flag(
            argvs, "check_image_refs_resolve.py", "--scope", scope
        ), (
            f"image-ref-resolve.yaml never runs `check_image_refs_resolve.py "
            f"--scope {scope}` in a step that is unconditionally reached.\n"
            f"  steps/jobs excluded because of an `if:` this module refuses to "
            f"grade through: {gated or 'none'}\n"
            "If the invocation is one of those, that IS the finding: a gate on "
            "that step means the check stops happening on the events the "
            "trigger was widened for. If the condition provably does not "
            "narrow, add it to _NON_NARROWING_IF with the reasoning. The "
            "`delivered` scope reads the customer pin table; the default "
            "`deploy` scope does not read it at all."
        )


@pytest.mark.parametrize("ci_env,expect_zero", [(None, True), ("true", False)])
def test_a_missing_resolver_is_advisory_locally_and_fatal_in_ci(
    tmp_path, ci_env, expect_zero: bool,
) -> None:
    """No skopeo and no docker must not report success in CI.

    ⛔ This was the design's only fail-open, and the only thing standing in front
    of it was the continued existence of an `apt-get install -y skopeo` step. The
    realistic way that step stops working is not deletion — a guard covers that —
    it is a maintainer answering an apt flake with `continue-on-error: true`.
    After that the step's conclusion is `success`, both scopes print
    `::warning:: neither skopeo nor docker available`, both return 0, and both
    gates read green forever with nothing asserting otherwise.

    Locally the fail-open is the right behaviour: a laptop without skopeo must not
    fail a check about registry state. So the property is conditional, and both
    halves are pinned — a check that cannot run must not report success where
    someone is relying on it, and must not shout where nobody is.
    """
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in {"CI", "PATH", "GITHUB_ACTIONS"}}
    # A PATH with neither resolver on it. tmp_path is empty by construction, which
    # is a stronger statement than filtering a real PATH by name.
    env["PATH"] = str(tmp_path)
    env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")  # Windows needs this
    if ci_env is not None:
        env["CI"] = ci_env

    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(EXTRACTOR), "--scope", "delivered"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    blob = proc.stdout + proc.stderr
    assert "neither skopeo nor docker" in blob, (
        "the probe did not reach the no-resolver branch, so its exit code says "
        f"nothing about it.\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    if expect_zero:
        assert proc.returncode == 0, (
            "a developer box without skopeo must not fail this check; it reports "
            f"and moves on. rc={proc.returncode}")
    else:
        assert proc.returncode != 0, (
            "under CI a check that could not run reported SUCCESS. That is the "
            "shape where one `continue-on-error: true` on the install step turns "
            "both scopes permanently green.")


def test_the_delivered_scope_job_has_a_resolver_installed() -> None:
    """A resolver binary must reach the job that runs `--scope delivered`.

    ⛔ Without one, `main()` prints `::warning:: neither skopeo nor docker
    available` and returns **0** — the gate goes permanently green while every
    comment around it describes it as fail-closed. It rested on an unasserted
    workflow fact: `check_image_refs_resolve.py` says in prose "CI installs
    skopeo (image-ref-resolve.yaml), so the gate itself is safe".

    ⛔ The job is selected from PARSED ARGV, not by searching step text for
    `--scope delivered`. The first version used the substring, which (a) was a
    strict subset of the scan-face assertion above — neutralise it and that one
    still catches a removed invocation — and (b) reported "the delivered pin
    table is out of CI's view entirely" for `--scope=delivered`, the equals form
    that `_SCAN_FACE_REAL` in this same file declares MUST count. Two guards, one
    file, disagreeing about a legal spelling, with the loose one running first.

    ⚠️ HONEST BOUNDARY: this proves a resolver is *installed by the workflow*,
    not that `shutil.which()` will find one at run time — and it is satisfied by
    any step naming docker (including `uses: docker/...`). It closes "someone
    deleted the install step", which is what actually happened to gates like this
    one; it does not close "the install silently stopped working".
    """
    def _runs_delivered(job) -> bool:
        for step in (job.get("steps") or []):
            for line in (step.get("run") or "").splitlines():
                try:
                    argv = shlex.split(line, comments=True)
                except ValueError:
                    continue
                if not any(a.endswith("check_image_refs_resolve.py") for a in argv):
                    continue
                # both legal spellings, judged on tokens rather than on text
                if "--scope=delivered" in argv:
                    return True
                if "--scope" in argv:
                    i = argv.index("--scope")
                    if i + 1 < len(argv) and argv[i + 1] == "delivered":
                        return True
        return False

    wf = yaml.safe_load(
        (WORKFLOWS_DIR / "image-ref-resolve.yaml").read_text(encoding="utf-8"))
    delivered_jobs = {
        name: job for name, job in (wf.get("jobs") or {}).items()
        if isinstance(job, dict) and _runs_delivered(job)
    }
    # ⛔ No emptiness assertion here: the scan face above already fails, with a
    # better message, if no job invokes that scope. A second check on the same
    # condition only adds a place for the two to disagree.
    for name, job in delivered_jobs.items():
        blob = "\n".join(
            (s.get("run") or "") + " " + str(s.get("uses") or "")
            for s in (job.get("steps") or []))
        assert "skopeo" in blob or "docker" in blob, (
            f"job {name!r} runs `--scope delivered` but installs neither skopeo "
            "nor docker. `main()` then prints a ::warning:: and returns 0 over "
            "any number of unresolvable refs — including a blanked pin — so the "
            "check reports success while measuring nothing.")


# Shapes that satisfied the previous TEXT-scanning form of assertion (3) while
# the workflow ran `--scope deploy` only. Each is a real YAML `steps:` list; all
# three were measured green against the old scan before this pairing existed.
_SCAN_FACE_DECOYS = {
    "trailing comment": """
- run: |
    python3 scripts/ops/check_image_refs_resolve.py --scope deploy   # was --scope delivered
""",
    "folded scalar moves the # off line-start": """
- run: >
    python3 scripts/ops/check_image_refs_resolve.py --scope deploy
    # re-enable --scope delivered after #9999
""",
    "prose in an echo, no comment at all": """
- run: echo "tip - reproduce locally with check_image_refs_resolve.py --scope delivered"
""",
    "mentioned in one command, run in another": """
- run: echo --scope delivered && python3 scripts/ops/check_image_refs_resolve.py --scope deploy
""",
    "a different script gets the flag": """
- run: python3 scripts/ops/other_tool.py --scope delivered
""",
}

# …and the shapes that MUST still count, so the fix cannot be "reject everything".
_SCAN_FACE_REAL = {
    "plain": "- run: python3 scripts/ops/check_image_refs_resolve.py --scope delivered\n",
    "equals form": "- run: python3 scripts/ops/check_image_refs_resolve.py --scope=delivered\n",
    "line continuation": """
- run: |
    python3 scripts/ops/check_image_refs_resolve.py \\
      --scope delivered
""",
    "after a real comment line": """
- run: |
    # resolve the customer-facing pins
    python3 scripts/ops/check_image_refs_resolve.py --scope delivered
""",
    "second command in a chain": """
- run: set -euo pipefail && python3 scripts/ops/check_image_refs_resolve.py --scope delivered
""",
}


@pytest.mark.parametrize("label", sorted(_SCAN_FACE_DECOYS))
def test_scan_face_reads_commands_not_prose(label: str) -> None:
    """⛔ Counter-example half: text that MENTIONS the invocation must not pass.

    Assertion (3) above is only worth its message if it is unsatisfiable by
    prose. Its previous form — join the non-`#`-initial lines, substring-search
    — passed all five of these. Pairing matters as much as the fix: the
    companion test proves the tightening did not simply reject everything, which
    is the other way a guard stops measuring anything.
    """
    steps = yaml.safe_load(_SCAN_FACE_DECOYS[label])
    argvs = [argv for s in steps for argv in _shell_argvs(s.get("run") or "")]
    assert not _invokes_with_flag(
        argvs, "check_image_refs_resolve.py", "--scope", "delivered"
    ), (
        f"the scan-face check accepted {label!r} as a real invocation.\n"
        f"  parsed argv: {argvs}\n"
        "Something regressed to grading the TEXT of the run: block."
    )


@pytest.mark.parametrize("label", sorted(_SCAN_FACE_REAL))
def test_scan_face_accepts_the_real_invocation_spellings(label: str) -> None:
    """Paired positive: the shapes a maintainer would actually write must pass."""
    steps = yaml.safe_load(_SCAN_FACE_REAL[label])
    argvs = [argv for s in steps for argv in _shell_argvs(s.get("run") or "")]
    assert _invokes_with_flag(
        argvs, "check_image_refs_resolve.py", "--scope", "delivered"
    ), (
        f"the scan-face check rejected {label!r}, which is a real invocation.\n"
        f"  parsed argv: {argvs}\n"
        "A guard that only accepts one spelling reds on a harmless rewrite."
    )


def test_label_naming_is_exact_and_family_agnostic() -> None:
    """Both failure directions of `_names_label`, including the family-prefix one.

    The second block is the one worth keeping: this test's sibling claims to red
    on the day a FOURTH bucket is added, and a matcher anchored on the current
    `nightly-cve` family would instead red on a correct step forever.
    """
    long_only = "labels: 'nightly-cve-thirdparty' was deleted"
    assert not _names_label(long_only, "nightly-cve"), (
        "a longer label containing the short one satisfied it — substring drift"
    )
    assert _names_label(long_only, "nightly-cve-thirdparty")

    # A fourth bucket outside the current family must be gradeable both ways.
    other = "check the race-tracking issue"
    assert _names_label(other, "race-tracking"), (
        "a label outside the `nightly-cve` family was reported missing from a "
        "text that names it — the matcher is anchored on today's family"
    )
    assert not _names_label("check the race-tracking-v2 issue", "race-tracking")

    # Quoting/punctuation around the token must not hide it.
    for wrapped in ("'nightly-cve'", '"nightly-cve"', "(nightly-cve)", "nightly-cve."):
        assert _names_label(f"see {wrapped} for details", "nightly-cve"), wrapped


# job -> (what its Trivy step must scan, why it differs).
# Derived from what each bucket's matrix carries, not copied from the workflow:
# `scan` builds images locally and tags them, the other two scan published refs.
_SCAN_TARGETS = {
    "scan": "local-scan/${{ matrix.name }}:nightly",
    "scan-thirdparty": "${{ matrix.ref }}",
    "scan-delivered": "${{ matrix.ref }}",
}
_FRAG_PREFIXES = {"scan": "sb", "scan-thirdparty": "tp", "scan-delivered": "dl"}


def _nightly() -> dict:
    return yaml.safe_load(
        (WORKFLOWS_DIR / "nightly-image-scan.yaml").read_text(encoding="utf-8")
    )


def test_every_scan_job_actually_scans_its_own_matrix() -> None:
    """⛔ The drift guard pins the MATRIX; this pins the SCANNER to the matrix.

    Those are different contracts and only the first existed. Measured: changing
    `scan-delivered`'s `image-ref` to a literal `alpine:3.20` left all 37 tests
    green — and the runtime consequence is the worst available, because nothing
    degrades. Four jobs still run, four fragments are still written, so
    `missing = 4 - 4 = 0`, `problem=0`, and the tracking issue is AUTO-CLOSED
    with "All 4 customer-delivered images clean". No red run, no degraded
    banner, no notifying comment — the four customer pins are simply never
    looked at while the audit trail says they are fine.

    It also falsifies, silently and all at once, the workflow's own claim that
    the scan "buys does-the-ref-still-exist for free": a deleted or retagged ref
    can only show up as a degraded pull if the pull is of that ref.

    All three buckets are asserted because the hole is identical in each and
    `scan-thirdparty` has had it since #902 — fixing only the bucket this PR
    added would leave the same defect in the sibling it was copied from.
    """
    wf = _nightly()
    for job_name, expected in _SCAN_TARGETS.items():
        job = wf["jobs"][job_name]
        trivy = [
            s for s in (job.get("steps") or [])
            if "trivy-action" in str(s.get("uses", ""))
        ]
        assert len(trivy) == 1, (
            f"{job_name} has {len(trivy)} trivy-action step(s); this guard "
            "reads exactly one per bucket"
        )
        with_ = trivy[0].get("with") or {}
        got = str(with_.get("image-ref", ""))
        assert got == expected, (
            f"{job_name}'s Trivy step scans {got!r}, not {expected!r}.\n"
            "The matrix and the scanner have to name the same thing. When they "
            "diverge every job still succeeds and the tracking issue closes "
            "itself as clean, so this assertion is the only place it surfaces."
        )
        # ⛔ WHAT is scanned and HOW it is scanned are two contracts, and only
        # the first was pinned. `severity` filters AT SCAN TIME, so narrowing it
        # to `CRITICAL` means a HIGH in a customer-delivered image never enters
        # the JSON at all — `summarize_trivy_cve.sh`'s downstream "defensive"
        # HIGH-or-CRITICAL re-filter cannot recover data Trivy never emitted.
        # The job stays green, the fragment is written, `missing=0`, and the
        # tracking issue reports clean. Measured: that one-word edit left all 39
        # tests passing. `exit-code: 0` is equally load-bearing in the other
        # direction — the design is capture-don't-gate, and a non-zero code
        # would turn every finding into a red nightly run.
        contract = {
            "severity": "CRITICAL,HIGH",
            "ignore-unfixed": True,
            "exit-code": "0",
        }
        for key, want in contract.items():
            assert str(with_.get(key)) == str(want), (
                f"{job_name}'s Trivy step sets {key}={with_.get(key)!r}, "
                f"expected {want!r}. All three buckets share one scan contract; "
                "a bucket that quietly narrows it reports clean instead of "
                "reporting less."
            )

        # ⛔ Pinning those three VALUES is a denylist: it says nothing about a
        # FOURTH key. `severity` is load-bearing because it filters at scan
        # time — and `trivyignores`, `scanners`, `vuln-type` and `skip-dirs` all
        # narrow the scan through the same mechanism. Measured: adding
        # `trivyignores: components/recipe-preview/.trivyignore.yaml` to
        # scan-delivered left all 42 tests passing, with the job green, the
        # fragment written, `missing=0`, and the tracking issue closing itself
        # as "all 4 customer-delivered images clean".
        #
        # So the property is rebuilt as an allowlist: the key SET must be one we
        # have looked at. Growth then has to be argued here rather than merged
        # silently, which is the same shape as the exit-locked ledgers elsewhere
        # in this module.
        unexpected = set(with_) - _TRIVY_NEUTRAL_KEYS - set(contract) - \
            _TRIVY_EXTRA_ALLOWED[job_name]
        assert not unexpected, (
            f"{job_name}'s Trivy step passes {sorted(unexpected)}, which this "
            "guard has not vetted. Several trivy-action inputs shrink the scan "
            "surface at scan time and are indistinguishable downstream from "
            "'nothing was found'. If the new input is safe, add it to "
            "_TRIVY_EXTRA_ALLOWED with the reason."
        )

        # ⛔ Allowlisting the KEY says nothing about its VALUE, and for a waiver
        # file the value is the whole safety argument. The comment on
        # _TRIVY_EXTRA_ALLOWED claims this one is "ONLY conditionally and ONLY
        # for recipe-preview's bundled promtool" — that sentence had nothing
        # enforcing it. Measured: rewriting the expression to an unconditional
        # `trivyignores: components/recipe-preview/.trivyignore.yaml` applied
        # the waiver to all seven self-built images and left 43 tests passing.
        # The file filters by CVE id, not by image, so today that is probably a
        # no-op elsewhere — by coincidence, not by construction: one shared
        # base-image CVE landing in that list would silently eat a real finding
        # on six other images.
        #
        # Derived, not pinned: the waiver may only apply to a matrix entry that
        # actually owns a waiver file on disk. Two independent sources (the
        # workflow expression and the filesystem) have to agree.
        if "trivyignores" in with_:
            expr = str(with_["trivyignores"])
            guarded = set(re.findall(r"matrix\.name\s*==\s*'([^']+)'", expr))
            paths = set(re.findall(r"'([^']*\.trivyignore\.ya?ml)'", expr))
            assert guarded, (
                f"{job_name}'s trivyignores is unconditional ({expr!r}). A "
                "waiver file must be scoped to the image it was written for; "
                "applied matrix-wide it suppresses findings on every other "
                "image in the bucket."
            )
            owners = set()
            for path in paths:
                owner = Path(path).parent.name
                owners.add(owner)
                assert (ROOT / path).is_file(), (
                    f"{job_name} references a waiver file that does not exist: "
                    f"{path}"
                )
                assert owner in {
                    e.get("name") for e in
                    (job.get("strategy", {}).get("matrix", {}).get("include") or [])
                }, (
                    f"{job_name} gates a waiver on {owner!r}, which is not in "
                    "this job's matrix — the condition can never be true."
                )
            # ⛔ EQUALITY, not membership. An earlier version asserted
            # `owner in guarded`, which reads the wrong direction: the loop runs
            # over the waiver PATHS (one), never over the guard names (可以 N
            # 個), so an extra `|| matrix.name == 'other-image'` was invisible.
            # Measured: OR-ing `da-tools` into the condition applied
            # recipe-preview's promtool waivers to it and left 44 passing.
            # Set equality makes the guard say "these images and no others".
            assert guarded == owners, (
                f"{job_name} gates its waiver on {sorted(guarded)} but the "
                f"referenced waiver file(s) belong to {sorted(owners)}. A name "
                "in the condition that owns no waiver file silently borrows "
                "someone else's suppressions."
            )


@pytest.mark.parametrize("emptied", ["_GITLAB_APPLY_IMAGES", "GIT_SYNC_IMAGE"])
def test_delivered_refs_refuses_a_half_empty_pin_table(tmp_path, emptied: str) -> None:
    """⛔ The PER-SOURCE emptiness guard, pinned by counter-example.

    `delivered_refs()`'s docstring names a specific historical bug: checking
    `if not refs` AFTER unioning the two sources made the guard dead code,
    because a union is non-empty as long as EITHER source survives — emptying
    `_GITLAB_APPLY_IMAGES` still exited 0 and reported one clean ref. Nothing
    tested that. Measured by review: reverting the function to exactly the
    union-first form the docstring describes left the whole suite green,
    because every other assertion here compares the function's OUTPUT against
    the workflow matrix and so only sees a reduction, never a broken guard.

    That redundancy is also exactly what expires: the docstring invites reuse
    ("anything that needs these refs imports"), and a future importer gets none
    of the matrix-equality protection — only this guard. So the guard needs its
    own counter-example, one per source, which is the shape a union check can
    never satisfy.
    """
    src = (ROOT / _DELIVERED_PIN_SOURCE).read_text(encoding="utf-8")
    fake_root = tmp_path
    target = fake_root.joinpath(*_DELIVERED_PIN_SOURCE.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Neutralise ONE source while leaving the other intact — the case a union
    # check cannot see.
    target.write_text(
        src + f"\n\n{emptied} = " + ("{}" if emptied.endswith("IMAGES") else '""') + "\n",
        encoding="utf-8",
    )

    import importlib.util

    spec = importlib.util.spec_from_file_location("_extractor_halfempty", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extractor_halfempty"] = mod
    spec.loader.exec_module(mod)

    with pytest.raises(SystemExit) as excinfo:
        mod.delivered_refs(fake_root)
    assert emptied in str(excinfo.value), (
        f"delivered_refs() exited but did not name {emptied} as the empty "
        f"source: {excinfo.value}. The message is the whole diagnosis on a CI "
        "run page; a guard that fires without naming which half went missing "
        "sends the reader to the wrong file."
    )

    # Paired positive: the untouched table must still resolve, so this test
    # cannot be satisfied by a `delivered_refs` that raises unconditionally.
    # ⛔ FLOOR, not `== 4` — same reason as `_delivered_pins_from_generator`: a
    # legitimate 5th delivered pin is caught by the matrix set-equality guard,
    # not by re-spelling the count here.
    assert len(mod.delivered_refs(ROOT)) >= _DELIVERED_PRODUCT_FLOOR


def _load_extractor(name: str):
    """Fresh in-process copy of the checker, under a caller-chosen module name.

    ⚠️ Registers `name` in `sys.modules` and does NOT remove it: the loaded module
    is the test's subject and must stay reachable for the assertions. The tests
    below therefore snapshot `sys.modules` AFTER calling this, so the helper's own
    entry is inside the baseline rather than counted as a leak.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _is_under(file_attr, root: Path) -> bool:
    """Is a module's `__file__` inside `root`? One spelling, several callers.

    ⛔ There were three hand-written copies of this predicate and they were not
    equivalent — one compared against the wrong root, one omitted the resolve
    guard. A predicate duplicated per call site is a predicate that drifts per
    call site.
    """
    if not file_attr:
        return False
    try:
        return Path(file_attr).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _slug(text: str) -> str:
    """A deterministic module-name suffix.

    ⚠️ Was `abs(hash(text))`, which is PYTHONHASHSEED-randomised for str — three
    interpreters gave three different values. Nothing keys off the name, so it was
    harmless, but these names accumulate in `sys.modules`: a suite whose subject
    is `sys.modules` hygiene should not leave a non-reproducible `sys.modules`.
    """
    return "".join(ch if ch.isalnum() else "_" for ch in text)[:24] or "empty"


def _fake_pin_root(tmp_path, appended: str):
    """A tree holding a COPY of the pin source with `appended` tacked on."""
    target = tmp_path.joinpath(*_DELIVERED_PIN_SOURCE.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    src = (ROOT / _DELIVERED_PIN_SOURCE).read_text(encoding="utf-8")
    target.write_text(src + appended, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("appended,raises", [
    # The emptiness exit: exec SUCCEEDS, the guard fires afterwards.
    ("\n\n_GITLAB_APPLY_IMAGES = {}\n", SystemExit),
    # exec itself RAISES — a different window, and the one a `try/finally` exists
    # for. The first version of this test covered only the case above, so the
    # rollback could have been deleted entirely and stayed green (proved by
    # blind review: replacing the try/finally with straight-line statements left
    # 53/53 passing).
    ("\n\nraise RuntimeError('boom during exec')\n", RuntimeError),
    # The SUCCESS path: nothing raises at all. State mutated after the guard has
    # run is invisible to both cases above.
    ("\n", None),
])
def test_reading_the_pin_table_leaves_no_process_state_behind(
    tmp_path, appended: str, raises,
) -> None:
    """Loading the pin source must leave `sys.path` and `sys.modules` untouched.

    ⛔ The pin source is a CLI module, and executing it runs four
    ``sys.path.insert(0, …)`` derived from its own ``__file__`` — never undone —
    and caches nine first-party modules under generic names (`_lib_python`,
    `_registry_lib`, …). In the one-shot CI process that is invisible; in-process
    it is not. Restoring the path does NOT undo the module caching, which is why
    both are asserted here.

    ⛔ Both assertions are DERIVED (whole-list equality, whole-key-set equality),
    not a search for known names. An earlier version asserted
    ``"_da_init_project" not in sys.modules``, i.e. one hardcoded name — and a
    leak registered under ``init_project`` (the very name whose harm the comment
    described) passed it. Naming what may not leak is a list that is always one
    entry short.

    ⚠️ What this does NOT establish: that a leak would break something today. The
    concrete harm first claimed here — a later ``import init_project`` picking up
    a throwaway copy — is unreachable in this suite (the name is already cached
    from the real file by two other test modules, and nothing in this file
    imports it). That was measured in a bare interpreter and generalised without
    checking. The guard stands on the leak being real and cheap to remove.

    ⛔ AND THIS TEST IS THE WEAK ONE OF THE PAIR. It is ORDER-DEPENDENT by
    construction: `before_modules` is snapshotted inside the test, so anything an
    earlier call already leaked is in the baseline and a repeat leak of the same
    name is a no-op. Measured under a mutation that leaks `init_project`: in a
    whole-file run only one parametrization failed, and under `-k` selection a
    different one did — the identity of the working detector is decided by
    selection order. `test_the_first_load_in_a_clean_interpreter_leaks_nothing`
    is the load-bearing assertion; these three cover the three exit windows
    cheaply and are kept for that, not as the primary detector.
    """
    root = _fake_pin_root(tmp_path, appended)
    mod = _load_extractor(f"_extractor_no_leak_{_slug(appended)}")

    before_path = list(sys.path)
    before_modules = set(sys.modules)
    if raises is None:
        mod.delivered_refs(root)
    else:
        with pytest.raises(raises):
            mod.delivered_refs(root)

    assert sys.path == before_path, (
        "loading the pin source changed sys.path; the difference is "
        f"{sorted(set(sys.path) ^ set(before_path))}. Entries land at the FRONT, "
        "so whatever the copy shipped wins every later import in this process."
    )
    # ⛔ The root the call was GIVEN, not the repo root. Judging provenance against
    # ROOT while handing `delivered_refs` a tmp tree made this assertion disagree
    # with the product it guards: the copied pin source's `_lib_*` siblings resolve
    # from the REAL repo, so they are under ROOT and not under `root` — modules the
    # correct implementation deliberately does not evict. Measured on the pristine
    # tree: `-k reading_the_pin_table` reported nine of them as a leak and failed,
    # while a whole-file run passed only because an earlier test had already cached
    # those names. Green when vacuous, red when wrong.
    # ⛔ Not a delta over module NAMES — that is what made this assertion vacuous.
    # `_da_init_project` is re-registered under the same name on every call, so an
    # earlier parametrization (or any earlier test that loaded a pin source) puts
    # it in `before_modules` and a later leak of the same name is invisible.
    # Measured: with the rollback fully disabled, the delta was `[]` three times.
    # The property is "nothing is left pointing INTO this throwaway tree", which
    # is order-independent: no other test can have cached anything under a
    # `tmp_path` created for this one.
    _root = Path(root).resolve()
    from_root = sorted(
        name for name, module in list(sys.modules.items())
        if _is_under(getattr(module, "__file__", None), _root))
    assert not from_root, (
        f"loading the pin source left {len(from_root)} module(s) from under the "
        f"repo root cached: {from_root}. Restoring sys.path does not undo this — "
        "a cached module outlives the path that found it, and `root` is "
        "caller-supplied, so these are the names an arbitrary tree could shadow. "
        "⚠️ Scoped to root-provenance on purpose: stdlib and site-packages "
        "modules first imported during the load are deliberately LEFT cached, "
        "because evicting them would hand the next importer a second copy."
    )


@pytest.mark.parametrize("blank", ["''", "'   '", "None", "0", "b''"])
def test_a_single_blanked_pin_is_named_not_shrugged_off(tmp_path, blank: str) -> None:
    """One emptied entry must be reported, not diluted by its siblings.

    ``apply_refs`` is a SET of the table's values, so blanking ONE pin leaves it
    truthy and the container-level emptiness check passes.

    ⛔ Parametrized over non-`str` blanks as well, because the filter has two
    halves and only one had a counter-example: with `'   '` as the sole sample,
    replacing ``isinstance(ref, str) and ref.strip()`` with ``str(ref).strip()``
    kept the suite green — and a `None` pin then ships as the literal ref
    ``"None"``, which is exactly the unparseable-ref outcome the filter exists to
    stop.
    """
    root = _fake_pin_root(
        tmp_path,
        "\n\n_GITLAB_APPLY_IMAGES = dict(_GITLAB_APPLY_IMAGES)\n"
        "_GITLAB_APPLY_IMAGES[next(iter(_GITLAB_APPLY_IMAGES))] = "
        f"(next(iter(_GITLAB_APPLY_IMAGES.values()))[0], {blank})\n",
    )
    mod = _load_extractor(f"_extractor_blank_{_slug(blank)}")

    with pytest.raises(SystemExit) as excinfo:
        mod.delivered_refs(root)
    message = str(excinfo.value)
    # ⛔ The distinctive text, not just the table name: the container-level branch
    # puts the bare name `_GITLAB_APPLY_IMAGES` into the same message, so a
    # substring check on it cannot tell "one pin is blank" from "the table is
    # empty" — and this test's whole subject is the former.
    assert "declares" in message and "carry a non-empty ref" in message, (
        f"blanking one pin ({blank}) did not produce the partial-count "
        f"diagnosis; got: {message}"
    )


def test_the_first_load_in_a_clean_interpreter_leaks_nothing() -> None:
    """The no-leak property, measured where it is actually observable.

    ⛔ The in-process version of this check is ORDER-DEPENDENT and that is not a
    theoretical worry — it was measured. It snapshots `sys.modules` inside the
    test, but by then earlier tests have already called `delivered_refs`
    successfully, so a name leaked by the FIRST call is already in the baseline
    and a second leak of the same name is a no-op. Mutation proof: adding
    `sys.modules.setdefault('init_project', mod)` on the success path — the exact
    harm the containment exists for — left every in-process assertion green, both
    inside and outside the `try/finally` window, and was caught only here.

    A fresh interpreter has no such history: the call under test is the first one,
    so anything it adds is attributable to it. This is also the shape the CI gate
    actually runs in (`python3 scripts/ops/check_image_refs_resolve.py`), which
    makes it the honest place to assert the property.

    ⚠️ The property asserted is NOT "no module was cached" — the rollback
    deliberately leaves stdlib and site-packages alone (deleting `typing`/`ssl`/
    `jsonschema` would hand the next importer a second copy). It is "nothing whose
    file lives under `root`", which is the threat the rollback is scoped to.
    `sys.path` is compared by EQUALITY, not by additions: a rollback that
    reordered it, dropped an entry, or appended a duplicate would report an empty
    addition-delta and read green.
    """
    probe = textwrap.dedent(f"""
        import importlib.util, json, sys
        from pathlib import Path
        root = Path({str(ROOT)!r})
        spec = importlib.util.spec_from_file_location(
            "_probe_chk", root / "scripts" / "ops" / "check_image_refs_resolve.py")
        chk = importlib.util.module_from_spec(spec)
        sys.modules["_probe_chk"] = chk
        spec.loader.exec_module(chk)
        before_path, before_mods = list(sys.path), set(sys.modules)
        refs = chk.delivered_refs(root)

        def _under_root(name):
            f = getattr(sys.modules.get(name), "__file__", None)
            if not f:
                return False
            try:
                return Path(f).resolve().is_relative_to(root.resolve())
            except (OSError, ValueError):
                return False

        added = set(sys.modules) - before_mods
        print("PROBE_JSON:" + json.dumps({{
            "refs": len(refs),
            "path_equal": sys.path == before_path,
            "path_delta": sorted(set(map(str, sys.path)) ^ set(map(str, before_path))),
            "from_root": sorted(n for n in added if _under_root(n)),
            # ⛔ Only names that HAVE a file and it is NOT under root. Counting
            # every survivor would include builtins/extension modules with no
            # `__file__`, which no version of the rollback can delete — so the
            # count would be non-zero even for an evict-everything rollback, and
            # the tripwire below would be always-true.
            "kept_outside": sorted(
                n for n in added
                if getattr(sys.modules.get(n), "__file__", None) and not _under_root(n)),
        }}))
    """)
    # ⛔ `-E`, not `-I`. `-E` drops PYTHONPATH/PYTHONHOME so the child's history is
    # not whatever the environment decided — which was the point. `-I` additionally
    # implies `-s`, and on this host every third-party package lives in USER
    # site-packages: measured, the same call adds 127 modules plainly (39 from
    # site-packages) versus 48 under `-I` (zero from site-packages). Under `-I` the
    # `kept_outside` assertion below would be satisfied by stdlib alone and the
    # jsonschema/attrs/rpds scenario its message spends five lines on would never
    # occur in the process that asserts it — and the docstring's "this is the shape
    # the CI gate actually runs in" would be false, since the gate runs plainly.
    out = subprocess.run([sys.executable, "-E", "-X", "utf8", "-c", probe],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", cwd=ROOT, timeout=120)
    assert out.returncode == 0, (
        f"the clean-interpreter probe failed (rc={out.returncode}); this must not "
        f"be read as 'no leak'.\nstdout: {out.stdout}\nstderr: {out.stderr}")
    # ⛔ Tagged line + explicit failure: `splitlines()[-1]` on unexpected output
    # dies with a bare IndexError one line after the assertion that says a broken
    # probe must not read as success.
    tagged = [ln for ln in out.stdout.splitlines() if ln.startswith("PROBE_JSON:")]
    assert len(tagged) == 1, (
        f"probe did not emit exactly one result line (got {len(tagged)}); its "
        f"output was:\n{out.stdout}\nstderr:\n{out.stderr}")
    result = json.loads(tagged[0][len("PROBE_JSON:"):])

    # Anti-vacuity: a probe that resolved nothing would report an empty delta for
    # the wrong reason.
    assert result["refs"] >= _DELIVERED_PRODUCT_FLOOR, (
        f"probe resolved only {result['refs']} refs; the empty deltas below would "
        "mean nothing")
    assert result["path_equal"], (
        "the first delivered_refs() in a clean interpreter did not restore "
        f"sys.path exactly; symmetric difference: {result['path_delta']}")
    assert not result["from_root"], (
        f"the first delivered_refs() in a clean interpreter left "
        f"{len(result['from_root'])} module(s) from under root cached: "
        f"{result['from_root']}. `root` is caller-supplied, so these names came "
        "from an arbitrary tree and now shadow the real ones process-wide. "
        f"(Modules from stdlib/site-packages are deliberately kept: "
        f"{len(result['kept_outside'])} of those, see the rollback's ⛔ note.)")

    # ⛔ The OTHER direction, and it needs its own assertion because the two
    # failure modes are opposite. A rollback that evicts everything new satisfies
    # the check above perfectly while deleting `typing`, `ssl`, `jsonschema`,
    # `attrs` and the `rpds` C extension from the cache — after which the next
    # import of any of them yields a SECOND module object, `isinstance` stops
    # working across the two, and every repeat call re-imports the world. That is
    # what the first version of this rollback did (measured: 136 evicted, 9 of
    # them first-party). Nothing else in this file would notice it coming back.
    assert result["kept_outside"], (
        "the rollback evicted every module the load imported, not just the ones "
        "from under `root`: no file-backed module from outside root survived. "
        "Deleting stdlib/site-packages entries does not unload them — it "
        "guarantees a second copy on the next import, and it makes every repeat "
        "call re-import the world.")


_WATCHED = frozenset({"_delivered_pins_from_generator", "_ref_shape_must_match",
                      "delivered_refs", "_load_extractor",
                      "_refs_in_a_generated_customer_repo", "_delivered_refs_via_cli"})

# One sample per construct that evaluates at IMPORT time. The scanner must find a
# watched call in every one of them; this list is what stops the scanner passing
# because it looks in too few places.
_IMPORT_TIME_SAMPLES = (
    ("bare call", "X = _ref_shape_must_match()\n"),
    ("tuple splat", "X = (*_ref_shape_must_match(), 'a')\n"),
    ("decorator argument",
     "@pytest.mark.parametrize('p', _ref_shape_must_match())\ndef test_x(p): pass\n"),
    ("argument default", "def f(a=_ref_shape_must_match()): pass\n"),
    ("class body", "class C:\n    X = _ref_shape_must_match()\n"),
    ("module-level if", "if True:\n    X = _ref_shape_must_match()\n"),
)

# ⛔ The other direction. Positives alone let the scanner grow until it flags legal
# code: measured, an earlier version reported all three of these as import-time
# calls, and its remedy ("move the call inside the test or a fixture") was already
# satisfied in every one of them.
_NOT_IMPORT_TIME_SAMPLES = (
    ("plain top-level def body", "def f():\n    return _ref_shape_must_match()\n"),
    ("def nested under module-level if",
     "if True:\n    def f():\n        return _ref_shape_must_match()\n"),
    ("def nested in module-level try",
     "try:\n    def f():\n        return _ref_shape_must_match()\nexcept ImportError:\n    pass\n"),
    ("method body inside a top-level class",
     "class C:\n    def m(self):\n        return _ref_shape_must_match()\n"),
)


def _import_time_calls(src: str) -> list[str]:
    """Watched calls in `src` that Python evaluates while IMPORTING the module.

    ⛔ A function body does not run at import — but its decorators, its argument
    defaults, and a class body all do, and they hang off the very nodes a naive
    "skip FunctionDef/ClassDef" rule discards. So the skip is applied to the BODY
    only, never to the node.
    """
    out: list[str] = []

    def _scan_expr(node) -> None:
        """Every call inside an expression that is evaluated where it sits."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if name in _WATCHED:
                    out.append(f"line {sub.lineno}: {name}()")

    def _scan_stmts(body) -> None:
        """Statements that run at import, applying the same rule at every depth.

        ⛔ Recursive, not `ast.walk` on the whole block. The first version walked
        any non-def top-level statement wholesale, so a `def` nested inside a
        module-level ``if``/``try`` — the `except ImportError:` fallback shape —
        was reported as import-time, with a message telling the author to move a
        call that is already inside a function. Same for a method body inside a
        top-level class. Over-flagging is not the safe direction here: a guard
        that cries wolf about legal code gets its rule relaxed by the next person.
        """
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    _scan_expr(dec)
                for default in [*node.args.defaults, *(node.args.kw_defaults or [])]:
                    if default is not None:
                        _scan_expr(default)
            elif isinstance(node, ast.ClassDef):
                for dec in node.decorator_list:
                    _scan_expr(dec)
                _scan_stmts(node.body)  # a class body executes at import
            else:
                for field, value in ast.iter_fields(node):
                    items = value if isinstance(value, list) else [value]
                    for item in items:
                        if isinstance(item, ast.stmt):
                            _scan_stmts([item])
                        elif isinstance(item, ast.AST):
                            _scan_expr(item)

    _scan_stmts(ast.parse(src).body)
    return out


def test_nothing_in_this_module_reads_the_pin_table_at_import_time() -> None:
    """Import-time evaluation must stay impossible, not merely absent.

    ⛔ `_delivered_pins_from_generator()` can `SystemExit` (a missing, empty, or
    partially blank pin table is a deliberate hard exit). Called from module
    scope — which is where it lived until this was changed — that exit happens
    during import, and pytest does not report it as a failed test or even as a
    collection error: MEASURED, the run ends with `INTERNALERROR` and
    `no tests ran`, and it takes down the WHOLE invocation, not just this file
    (reproduced with a two-file run). With the call inside a function the same
    broken pin gives named failures and the other guards still execute.

    That fix is one module-scope line away from being undone and nothing else
    would notice, so it gets a tripwire rather than a comment.

    ⛔ The first version of this test skipped every top-level ``FunctionDef`` and
    ``ClassDef`` NODE with the comment "bodies run on call, not on import". True
    of the body, false of everything else hanging off those nodes: a decorator
    expression, an argument default, and a class body all evaluate at import.
    Three of the five constructs the docstring claimed to cover were the three it
    skipped — and the one that matters most is
    ``@pytest.mark.parametrize("ref", _ref_shape_must_match())``, which is both the
    dominant idiom in this file and the most natural way anyone re-introduces the
    call. Two independent reviewers reproduced it: the injected parametrize ran at
    import (its params were collected) and this test stayed green.
    """
    offenders = _import_time_calls(Path(__file__).read_text(encoding="utf-8"))
    assert not offenders, (
        "these calls run at IMPORT time, so a bad pin table aborts the whole "
        f"pytest session instead of failing a named test: {offenders}. Move the "
        "call inside the test or a fixture."
    )

    # ⛔ Anti-vacuity, run through THE REAL SCANNER. The first version parsed the
    # literal "X = _ref_shape_must_match()" and asserted the parse contained a
    # Call — true by construction of the literal, touching neither `_WATCHED` nor
    # the skip rule. It was an always-true assertion inside the test whose subject
    # is always-true assertions.
    for label, src in _IMPORT_TIME_SAMPLES:
        assert _import_time_calls(src), (
            f"the scanner cannot see an import-time call in the {label} form; "
            "it would pass this file for the wrong reason")
    for label, src in _NOT_IMPORT_TIME_SAMPLES:
        assert not _import_time_calls(src), (
            f"the scanner flags the {label} form, which does NOT run at import: "
            f"{_import_time_calls(src)}. Its failure message would tell the "
            "author to move a call that is already inside a function.")

    # And the watched names must still exist, or the scan watches ghosts.
    defined = {n.name for n in ast.parse(Path(__file__).read_text(encoding="utf-8")).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ghosts = _WATCHED - defined - {"delivered_refs"}  # delivered_refs is imported
    assert not ghosts, (
        f"_WATCHED names no longer defined in this file: {sorted(ghosts)}. A "
        "renamed helper leaves this guard watching a dead name and passing "
        "vacuously.")


@pytest.mark.parametrize("blank", ["'   '", "None", "0"])
def test_a_blank_git_sync_image_is_refused_too(tmp_path, blank: str) -> None:
    """The second source needs the same normalization, and had none of its own.

    ⛔ The existing half-empty parametrization sets `GIT_SYNC_IMAGE = ""`, which
    the pre-existing falsiness check already caught — so the normalization line
    added alongside the member-level filter was covered by nothing. Mutation
    proof: deleting it outright left the suite green. A whitespace-only value
    would then have been carried through as a ref made of spaces.

    ⚠️ Only `'   '` is load-bearing here. `None` and `0` are already falsy, so the
    container-level check flags them with or without the normalization — they are
    kept as shape coverage, not as counter-examples, and the mutation above was
    killed by the whitespace case alone. Saying otherwise would repeat the
    over-generalisation this file keeps correcting.
    """
    root = _fake_pin_root(tmp_path, f"\n\nGIT_SYNC_IMAGE = {blank}\n")
    mod = _load_extractor(f"_extractor_git_sync_{_slug(blank)}")

    with pytest.raises(SystemExit) as excinfo:
        mod.delivered_refs(root)
    assert "GIT_SYNC_IMAGE" in str(excinfo.value), (
        f"a {blank} GIT_SYNC_IMAGE exited without naming it: {excinfo.value}")


def test_the_blank_pin_guard_still_lets_the_real_table_through(tmp_path) -> None:
    """Paired positive for the two tests above, factored out of both.

    Neither can be satisfied by a `delivered_refs` that raises unconditionally.
    ⛔ Asserts the FLOOR, not `== 4`: the sibling floors in this file already
    compare against `_DELIVERED_PRODUCT_FLOOR`, and the commit that introduced
    this pair wrote a bare `== 4` in the very edit whose sibling hunk removed one
    for being a duplicate of that constant — the cited instance fixed, the class
    left alone.
    """
    mod = _load_extractor("_extractor_paired_positive")
    assert len(mod.delivered_refs(ROOT)) >= _DELIVERED_PRODUCT_FLOOR


def test_the_report_job_waits_for_every_bucket() -> None:
    """`report` must depend on all three scan jobs.

    Measured: dropping `scan-delivered` from `needs:` left 39 tests passing.
    The runtime effect is a false alarm rather than a silent miss — `report`
    can start downloading `cve-frag-dl-*` before those jobs finish, so
    `present < 4` and the run posts a "Scan degraded" banner plus a notifying
    comment for images that scanned perfectly well. That still matters: this
    bucket's whole design goal is to not train its reader to ignore it.
    """
    report = _nightly()["jobs"]["report"]
    needs = report.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    missing = sorted(set(_SCAN_TARGETS) - set(needs))
    assert not missing, (
        f"`report` does not declare needs on {missing} (needs: {needs}). Every "
        "bucket whose fragments it downloads has to be a dependency, or it "
        "aggregates a partial set and calls the difference a degraded scan."
    )


# The two conditions under which a job still runs after an upstream `needs:`
# dependency FAILED. Both are already characterised in `_NON_NARROWING_IF`
# above; this names the subset that specifically survives failure (as opposed
# to merely relaxing something), because that is the property `report` needs.
_RUNS_DESPITE_UPSTREAM_FAILURE = frozenset({"always()", "!cancelled()"})

# Inputs that say WHAT to scan and WHERE to put the answer. They cannot shrink
# what Trivy looks for, so they need no per-value pin.
_TRIVY_NEUTRAL_KEYS = frozenset({"image-ref", "format", "output"})

# Vetted exceptions, per bucket, with the reason. `scan` legitimately carries a
# waiver file, but ONLY conditionally and ONLY for recipe-preview's bundled
# promtool: the expression evaluates to '' for the other six images, so it is a
# no-op there. The other two buckets scan images we do not build and have no
# waiver story at all — an entry appearing in either is a finding, not config.
_TRIVY_EXTRA_ALLOWED: dict[str, frozenset[str]] = {
    "scan": frozenset({"trivyignores"}),
    "scan-thirdparty": frozenset(),
    "scan-delivered": frozenset(),
}


# Steps whose failure is allowed to be discarded, as {job: {step name}}. Empty
# on purpose: the nightly workflow currently has ZERO of them, and its whole
# design is fail-loud (summarize_trivy_cve.sh runs under `set -e` so an
# unpullable image aborts instead of degrading to COUNT=0). An entry here is a
# decision that has to be argued, not config.
_NIGHTLY_DISARM_ALLOWED: dict[str, frozenset[str]] = {}

# Steps allowed to carry a NARROWING `if:` (one `_if_narrows` cannot read as
# strictly-wider), as {job: {step name}}, each with its reason. Everything else
# in a scan bucket has to be unconditional, because the buckets' whole output is
# "did this image scan or not".
#   * Docker Hub login   — credential-gated; `secrets` is not allowed in `if:`
#                          so it keys off a mirrored env var, and a fork run
#                          legitimately skips it.
#   * vendor dir / stub  — per-image build prep, matrix-scoped by design.
# `always()` is NOT listed: `_if_narrows` already classes it as non-narrowing
# via `_NON_NARROWING_IF`, which is why the `Upload fragment` steps need no
# entry here.
_NIGHTLY_IF_ALLOWED: dict[str, frozenset[str]] = {
    "scan": frozenset({
        "Docker Hub login (no-op without creds)",
        "Ensure da-portal vendor dir exists",
        "Stub da-tools build inputs",
    }),
    "scan-thirdparty": frozenset({"Docker Hub login (no-op without creds)"}),
    "scan-delivered": frozenset({"Docker Hub login (no-op without creds)"}),
    # The reporter is gated at JOB level by `if: always()` (pinned separately);
    # this step only speaks when the run already failed.
    "report": frozenset({"Explain a likely failure cause"}),
}


def test_no_nightly_scan_step_is_silently_gated_off() -> None:
    """⛔ The sibling test generalised HALF of the mechanism; this is the rest.

    `_if_narrows` exists and is careful, but its only caller is
    `_workflow_argvs`, whose only call site names `image-ref-resolve.yaml`. So
    "did this step actually run" was enforced for one workflow and for none of
    the three scan buckets — the commit that generalised `_disarmed` claimed to
    "reuse the same implementation across the whole nightly" and delivered only
    the disarm half. Measured: `if: false` on scan-delivered's Trivy step left
    44 passing, and so did `if: matrix.name != 'grafana'` on scan-thirdparty's
    Summarize step.

    Same reuse discipline as the disarm test: this calls `_if_narrows` rather
    than re-deriving what counts as a narrowing condition.
    """
    wf = _nightly()
    offenders: list[str] = []
    for job_name, job in wf["jobs"].items():
        allowed = _NIGHTLY_IF_ALLOWED.get(job_name, frozenset())
        if job_name != "report" and _if_narrows(job):
            offenders.append(f"job {job_name} (if: {job['if']})")
        for step in (job.get("steps") or []):
            label = str(step.get("name", step.get("uses", "<unnamed>")))
            if label in allowed:
                continue
            if _if_narrows(step):
                offenders.append(f"{job_name}/{label} (if: {step['if']})")
    assert not offenders, (
        "these nightly steps sit behind a condition this module cannot read as "
        "strictly wider:\n  " + "\n  ".join(offenders)
        + "\nA scan step that does not run reports nothing, which downstream is "
        "indistinguishable from a scan that found nothing. If the condition is "
        "legitimate, add the step to _NIGHTLY_IF_ALLOWED with its reason."
    )


def test_the_nightly_if_allowlist_has_no_dead_entries() -> None:
    """Exit-locked: an allowlist that outlives its steps rots into permission.

    Every name listed must still exist AND still carry a narrowing `if:` — so
    removing the condition (the good outcome) also forces the entry out, rather
    than leaving a standing exemption for whatever later takes that name.
    """
    wf = _nightly()
    stale: list[str] = []
    for job_name, names in _NIGHTLY_IF_ALLOWED.items():
        job = wf["jobs"].get(job_name)
        assert job is not None, (
            f"_NIGHTLY_IF_ALLOWED names job {job_name!r}, which no longer "
            "exists — the exemption now applies to nothing and hides nothing."
        )
        present = {
            str(s.get("name", s.get("uses", "<unnamed>"))): s
            for s in (job.get("steps") or [])
        }
        for name in names:
            step = present.get(name)
            if step is None or not _if_narrows(step):
                stale.append(f"{job_name}/{name}")
    assert not stale, (
        f"exempted steps that no longer need the exemption: {stale}. Drop them "
        "from _NIGHTLY_IF_ALLOWED so the list keeps meaning what it says."
    )


def test_no_nightly_step_discards_its_own_failure() -> None:
    """⛔ The disarm check existed but was wired to ONE workflow.

    `_disarmed` / `_SWALLOW_RE` were added for the resolve workflow and are
    reachable only through `_workflow_argvs`, whose sole call site names
    `image-ref-resolve.yaml`. So the reasoning that motivated them — "a step
    whose only output is pass/fail is worthless once its failure is discarded" —
    was never applied to the three scan buckets this module actually exists to
    protect. Measured: adding `continue-on-error: true` to scan-delivered's
    Summarize step left all 43 tests passing.

    ⚠️ Honest severity: this is not the main detection chain. `file_cve_report.sh`
    computes `missing = EXPECTED - present` by counting artifacts on disk, not by
    reading job conclusions, so a single silenced step still loses its fragment
    and still trips the degraded banner. What a disarm buys is a green checkmark
    on a bucket that actually failed, plus the standing risk that someone
    silences BOTH the Trivy and the Summarize step (or the job) to stop a flaky
    bucket reddening the nightly — at which point the count itself goes quiet.

    The same helpers are reused deliberately: a second implementation of "what
    counts as disarmed" is how the two drift apart.
    """
    wf = _nightly()
    offenders: list[str] = []
    for job_name, job in wf["jobs"].items():
        allowed = _NIGHTLY_DISARM_ALLOWED.get(job_name, frozenset())
        if (reason := _disarmed(job)):
            offenders.append(f"job {job_name} ({reason})")
        for step in (job.get("steps") or []):
            label = str(step.get("name", "<unnamed>"))
            if label in allowed:
                continue
            if (reason := _disarmed(step)):
                offenders.append(f"{job_name}/{label} ({reason})")
            folded = re.sub(r"\\\n\s*", " ", step.get("run") or "")
            for line in folded.split("\n"):
                if _SWALLOW_RE.search(_strip_bash_comment(line)):
                    offenders.append(
                        f"{job_name}/{label} (failure swallowed: "
                        f"{line.strip()[:60]})"
                    )
    assert not offenders, (
        "these nightly steps discard their own failure:\n  "
        + "\n  ".join(offenders)
        + "\nA scan step that cannot report failure is a scan that reports "
        "clean. If one of these genuinely must be allowed to fail, add it to "
        "_NIGHTLY_DISARM_ALLOWED with the reason — do not delete this test."
    )


def test_the_report_job_still_runs_when_a_bucket_fails() -> None:
    """⛔ `needs:` and `if: always()` are ONE mechanism, and only half was pinned.

    The sibling test above pins the `needs:` edges. On its own that is the
    dangerous half: GitHub skips a job whose `needs` dependency failed, so
    without a condition that survives failure, `report` is skipped exactly when
    a scan job goes red — and a scan job going red is the situation this bucket
    exists for. Measured: deleting `if: always()` (keeping `needs:`) left all 42
    tests passing.

    What that costs at runtime is the whole degradation chain the workflow
    header advertises: a 404'd ref makes the Trivy step fail → no fragment is
    uploaded → `missing = EXPECTED - present > 0` → "Scan degraded" banner plus
    a notifying comment. Every link needs `report` to have RUN. Skipped, the
    tracking issue simply keeps yesterday's title and nobody is told anything —
    the broken case and the healthy case become the same silence.
    """
    report = _nightly()["jobs"]["report"]
    cond = str(report.get("if", "")).strip()
    if cond.startswith("${{") and cond.endswith("}}"):
        cond = cond[3:-2].strip()
    assert cond in _RUNS_DESPITE_UPSTREAM_FAILURE, (
        f"`report` has `if: {cond or '<none>'}`, which does not survive an "
        f"upstream failure (expected one of {sorted(_RUNS_DESPITE_UPSTREAM_FAILURE)}). "
        "With `needs:` on all three scan jobs, the default success() condition "
        "means the aggregator is skipped precisely when a bucket breaks, so the "
        "degraded-scan banner it exists to raise never fires."
    )


def test_the_three_buckets_cannot_collect_each_others_fragments() -> None:
    """⛔ The disjoint-artifact-namespace claim, asserted instead of narrated.

    The workflow comments state the `sb-`/`tp-`/`dl-` prefixes keep the report
    job's three downloads from cross-matching. Nothing checked it. Measured:
    widening the delivered download to `pattern: cve-frag-*` left 37 tests
    green, and at runtime that bucket then collects all 26 fragments —
    `missing = 4 - 26 = -22`, so no degradation is reported, and the
    customer-delivered issue is filled with the repo's entire third-party CVE
    backlog carrying remediation advice written for a different bucket ("we do
    not deploy these"), which is exactly the advice-mixing the design forbids.

    Graded as a prefix relation, not by comparing the literals: the property
    that matters is that no bucket's pattern can match another bucket's
    uploads.
    """
    wf = _nightly()
    uploads, downloads = {}, {}
    for job_name in _SCAN_TARGETS:
        for step in (wf["jobs"][job_name].get("steps") or []):
            if "upload-artifact" in str(step.get("uses", "")):
                uploads[job_name] = str((step.get("with") or {}).get("name", ""))
    for step in (wf["jobs"]["report"].get("steps") or []):
        pat = str((step.get("with") or {}).get("pattern", ""))
        if pat:
            downloads[str((step.get("with") or {}).get("path", "")) or pat] = pat

    assert len(uploads) == len(_SCAN_TARGETS), (
        f"only found uploads for {sorted(uploads)} — expected one per bucket"
    )
    assert len(downloads) == len(_SCAN_TARGETS), (
        f"expected {len(_SCAN_TARGETS)} download patterns in `report`, found "
        f"{len(downloads)}: {downloads}"
    )
    for job_name, name in uploads.items():
        prefix = f"cve-frag-{_FRAG_PREFIXES[job_name]}-"
        assert name.startswith(prefix), (
            f"{job_name} uploads {name!r}, which does not carry its own "
            f"namespace {prefix!r} — another bucket's download can now claim it"
        )
    for key, pat in downloads.items():
        assert pat.endswith("*"), f"download pattern {pat!r} is not a glob"
        stem = pat[:-1]
        matched = [j for j, n in uploads.items() if n.startswith(stem)]
        assert len(matched) == 1, (
            f"download pattern {pat!r} (for {key!r}) matches uploads from "
            f"{matched or 'no bucket'} — each download must claim exactly one "
            "bucket. A pattern that matches several silently merges their "
            "findings and drives `missing` negative, which suppresses the "
            "degraded-scan banner entirely."
        )


def test_bash_comment_stripping_follows_the_shell_rule() -> None:
    """The `#` rule itself, including the cases a line-oriented filter gets wrong.

    Kept separate from the callers because both `_shell_argvs` and
    `_report_calls` depend on it — a regression here is silent in each of them.
    """
    cases = [
        ("cmd --a  # note", "cmd --a  "),  # trailing: the whole point
        ("# note", ""),  # line-initial: the case the old form handled
        ("cmd 'a # b'", "cmd 'a # b'"),  # quoted: not a comment
        ('cmd "a # b" --c', 'cmd "a # b" --c'),  # same, double quotes
        ("cmd --tag v1#2", "cmd --tag v1#2"),  # mid-word: not a comment
        (r"cmd \#literal", r"cmd \#literal"),  # escaped: not a comment
        ("cmd", "cmd"),  # nothing to strip
    ]
    wrong = [
        (src, got, want)
        for src, want in cases
        if (got := _strip_bash_comment(src)) != want
    ]
    assert not wrong, "\n".join(
        f"{src!r} -> {got!r}, expected {want!r}" for src, got, want in wrong
    )


# ── #1337: nothing pinned the SELF-BUILT matrix to anything ─────────────────
# The third-party half of this file has had a real SSOT since #902 (the
# extractor). The self-built half had none: its five entries, the identical five
# in component-docker-build.yaml, and the five hand-written `docker buildx build`
# lines in the Makefile were kept together by a COMMENT ("Keep in sync"). Two
# images shipped inside Helm charts — helm/federation-gateway/audit-sidecar/Dockerfile
# and helm/vector/projection-gate/Dockerfile — were therefore deployed for months
# with zero CVE coverage, and no test could say so: they were absent from all
# three lists at once, and every existing assertion was about set EQUALITY
# BETWEEN those lists.
#
# ⛔ Those two paths are written out in full ON PURPOSE. `verify_diff`'s text_map
# indexes LITERAL path strings, so the `ROOT / "a" / "b"` segment form used below
# registers nothing — a guard that exists but is never selected for the diff that
# breaks it is the failure mode this module is named after.
#
# The SSOT chosen here is `DOCKERFILE_CONTEXTS` in check_iac_vibe_rules.py. It is
# already fail-closed on exactly the event that matters — an unregistered
# Dockerfile is a BLOCK finding there — so a new image cannot enter the tree
# without appearing in it. That makes it the one list a new image is guaranteed
# to touch.
#
# ⛔ Read via ast, NOT by importing the module: check_iac_vibe_rules imports
# pathspec, and a test that silently skips when an optional dependency is absent
# is the "gate exists but never runs" shape this module exists to prevent.
_IAC_RULES = ROOT / "scripts" / "tools" / "lint" / "check_iac_vibe_rules.py"

# ⛔ The registry is TRUSTED BUT VERIFIED, and both halves are load-bearing.
# Blind review sank the first cut of this guard twice on the same root cause —
# it trusted a hand-maintained dict as if it were the tree:
#   (a) the upstream discovery is `rglob("Dockerfile*")` + a name filter, so
#       `logrotate.Dockerfile` / `Containerfile` are invisible to it — an image
#       could ship in a chart while being in NO list and NO exemption; and
#   (b) the upstream BLOCK on an unregistered Dockerfile is waivable from the PR
#       body (`bypass-lint: iac-vibe-rules`), so "cannot enter the tree without
#       being registered" was never true in the strong sense.
# So this module re-derives the inventory from the filesystem with a WIDER net
# than upstream uses, and a test cannot be waived by a PR-body tag.
# ⛔ The separator set is load-bearing, and getting it wrong is how the FIRST cut
# of this guard still had the hole it was written to close. Upstream keeps
# `Dockerfile` and `Dockerfile.<x>` only, so `Dockerfile-runtime`,
# `Dockerfile_prod` and `Dockerfile2` are invisible to BOTH it and an earlier
# version of this regex — and `docker build -f Dockerfile-runtime` is an ordinary
# spelling. Accept any separator, plus `Containerfile` and `*.dockerfile`.
# Case-insensitive: on Linux `dockerfile` is a different file from `Dockerfile`.
_DOCKERFILE_NAME_RE = re.compile(
    r"(?i)^((dockerfile|containerfile)([.\-_].*|\d.*)?|.+\.(dockerfile|containerfile))$")
# ⛔ …and a subtraction, because accepting any separator over-reaches: with `_`
# allowed, `dockerfile_helpers.py` matches. Reject names whose FINAL extension is
# a source/doc type — those are code ABOUT Dockerfiles, not build recipes. Caught
# by this module's own name-rule test, which is why that test exists.
# ⛔ `.tpl` IS in this list, and an earlier round of this PR was wrong to remove
# it. The reasoning that removed it — "`Dockerfile.tpl` is a plausible templated
# build recipe" — inverts the consequence: a template is precisely what
# `docker build -f` CANNOT consume, yet once discovered it could not be exempted
# (exemptions require a `tests/` prefix) and would be demanded into
# `make docker-build-all`. Admitting it forces the one artifact class that cannot
# be built into the mandatory-build set. The buildable artifact is whatever the
# template RENDERS to, and that is what belongs in the matrices.
# ⚠️ Known boundary: if such a rendered Dockerfile is produced at build time and
# never committed, it is invisible to this discovery (which reads the git index).
# Nothing in the repo does that today; if something starts to, this is the line
# to revisit.
_NOT_A_DOCKERFILE_SUFFIX = (
    ".py", ".md", ".sh", ".txt", ".json", ".yaml", ".yml", ".js", ".ts", ".go",
    ".tpl", ".lock", ".toml", ".cfg", ".ini",
)


def _is_dockerfile_name(name: str) -> bool:
    if name.lower().endswith(_NOT_A_DOCKERFILE_SUFFIX):
        return False
    return _DOCKERFILE_NAME_RE.match(name) is not None


def _discover_dockerfiles() -> set[str]:
    """Every image-build recipe TRACKED IN GIT, by a wider name rule than upstream.

    ⛔ `git ls-files`, not a filesystem walk. Blind review killed the walk twice:
      * it descended into `.git` (full history in CI, `fetch-depth: 0`) and every
        sibling worktree under `.claude` before filtering — tens of thousands of
        stats on a FUSE mount for a 9-element answer; and
      * it saw UNTRACKED files, so a stray `Dockerfile.orig` from a failed
        `git apply` reddened this test on a developer's machine while CI stayed
        green, with a message telling them to register their scratch file.
    The index is also the honest oracle: an untracked file ships to nobody.

    Fail-CLOSED: a git failure raises rather than returning an empty set, because
    an empty set makes every comparison below trivially true.
    """
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"`git ls-files` failed in {ROOT} (rc={proc.returncode}): {proc.stderr.strip()[:300]}\n"
        "This guard derives the image inventory from the git index; it must not "
        "silently fall back to 'no Dockerfiles found'."
    )
    files = [f for f in proc.stdout.split("\0") if f]
    assert files, "`git ls-files` returned nothing — refusing to run vacuously"
    found = {f for f in files if _is_dockerfile_name(f.rsplit("/", 1)[-1])}
    # ⛔ The FILTERED set must be non-empty too, asserted HERE rather than in one
    # caller. Blind review: a floor computed as `len(discovered) - len(exempt)`
    # goes NEGATIVE if the name rule ever stops matching, and `len(matrix) >= -2`
    # is satisfied by an empty matrix — the same triviality this guard already
    # moved once, relocated from the registry to the name rule. Every caller must
    # get the floor, so the assertion lives with the producer.
    assert found, (
        "no image-build recipes matched in a tree of "
        f"{len(files)} tracked files — suspect the name rule, not the repo"
    )
    # Index-vs-worktree: `git ls-files` lists staged entries even after the file
    # is deleted from disk. Fail-closed on that rather than reporting coverage of
    # something that is gone.
    missing = sorted(f for f in found if not (ROOT / f).is_file())
    assert not missing, (
        f"tracked but absent from the worktree: {missing} — `git rm` them, or "
        "restore them; this guard would otherwise report coverage of a file "
        "nobody can build."
    )
    return found

# Dockerfiles deliberately OUTSIDE the scan matrices.
#
# ⛔ An exemption registry is itself the fail-open shape this test is closing, so
# membership is not enough — `test_scan_exemptions_stay_undeployable` re-derives
# the property each entry claims. In particular an entry must live under
# `tests/`, which is what makes it structurally impossible to quietly exempt the
# next chart-shipped image (the exact class that produced #1337).
_SCAN_EXEMPT: dict[str, str] = {
    "tests/e2e-bench/driver/Dockerfile": (
        "ephemeral benchmark fixture — built by tests/e2e-bench/docker-compose.yml "
        "for the duration of a bench run, torn down with it, never deployed and "
        "never published. EXIT: it becomes reachable from a deployable tree."
    ),
    "tests/e2e-bench/receiver/Dockerfile": (
        "same shape as the driver above (in-compose bench receiver). Same EXIT."
    ),
}

# Trees whose contents are installed on a cluster or handed to a customer. If an
# exempt Dockerfile ever becomes reachable from one of these, the exemption's
# stated premise is false and the test says so.
_DEPLOYABLE_TREES = ("helm", "k8s", "operator-manifests", "try-local")


def _dockerfile_contexts() -> dict[str, str]:
    """`DOCKERFILE_CONTEXTS` from check_iac_vibe_rules.py, read without importing.

    ⛔ HONEST SCOPE — this reader is DEFENCE IN DEPTH, not the invariant.

    Reading only the literal is fail-open by itself: an entry added after it
    (``DOCKERFILE_CONTEXTS["x"] = "y"``, ``|= {...}``, ``.update({...})``) is live
    at runtime and invisible here. The checks below close the spellings a reviewer
    is actually likely to write, but they CANNOT be complete — aliasing
    (``d = DOCKERFILE_CONTEXTS; d[k] = v``), ``globals()[...]``, ``__setitem__``
    and mutation from another module all evade any such scan, and chasing them is
    an arms race this file should not be in.

    ⭐ What actually holds the invariant is `_discover_dockerfiles()`: the
    dangerous case is a REAL Dockerfile registered out of band, and there the walk
    sees the file while this reader does not, so `unregistered` is non-empty and
    the test reds regardless of how the registration was spelled. A registration
    with no file behind it is harmless by construction. Do not restate this
    reader as "the literal is the whole story, asserted".
    """
    src = _IAC_RULES.read_text(encoding="utf-8")
    tree = ast.parse(src)
    rel = _IAC_RULES.relative_to(ROOT).as_posix()
    # ⛔ ast.walk, not tree.body: a rebinding nested in `if` / `try` / `for` is
    # still a rebinding, and the module-level-only scan missed it.
    found: list[ast.expr] = []
    rebinds: list[int] = []
    for node in ast.walk(tree):
        targets = (
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign))
            else getattr(node, "targets", [])
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "DOCKERFILE_CONTEXTS":
                if isinstance(node, ast.AugAssign):
                    # `|=` mutates in place (PEP 584) and is NOT a rebinding, so
                    # it used to slip past both halves of this reader.
                    rebinds.append(node.lineno)
                elif node in tree.body:
                    found.append(node.value)
                else:
                    rebinds.append(node.lineno)
    assert len(found) == 1 and not rebinds, (
        f"DOCKERFILE_CONTEXTS must be bound exactly once, at module level, and "
        f"never re-bound or augmented in {rel} — found {len(found)} literal "
        f"binding(s) and re-binding/augmentation at line(s) {rebinds or 'none'}. "
        "This reader takes the literal, so anything else is invisible to it."
    )

    mutators: list[str] = []
    for node in ast.walk(tree):
        tgts = list(getattr(node, "targets", []))
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            tgts.append(node.target)
        for t in tgts:
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and t.value.id == "DOCKERFILE_CONTEXTS"):
                mutators.append(f"line {t.lineno}: item assignment")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "DOCKERFILE_CONTEXTS"
                and node.func.attr in {"update", "setdefault", "pop", "popitem",
                                       "clear", "__setitem__", "__ior__"}):
            mutators.append(f"line {node.lineno}: .{node.func.attr}()")
    assert not mutators, (
        f"DOCKERFILE_CONTEXTS is mutated after its literal in {rel} "
        f"({'; '.join(mutators)}). Put the entry in the literal — a runtime-only "
        "entry is invisible to this reader while satisfying the upstream "
        "registration check."
    )
    try:
        return ast.literal_eval(found[0])
    except ValueError as exc:  # non-literal value (comprehension, dict(), f-string…)
        raise AssertionError(
            f"DOCKERFILE_CONTEXTS in {rel} is no longer a plain literal ({exc}). "
            "This guard needs to read it WITHOUT importing the module (it depends "
            "on pathspec, and a test that skips when an optional dependency is "
            "missing is the failure mode this module exists to prevent). Keep it a "
            "literal, or re-point this reader — do not delete the assertion."
        ) from exc


def _make_recipe(target: str) -> list[str]:
    """Recipe lines of a Makefile target (tab-indented lines after `<target>:`)."""
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    seen = False
    for line in lines:
        if not seen:
            if re.match(rf"^{re.escape(target)}\s*:", line):
                seen = True
            continue
        if line.startswith("\t"):
            out.append(line.lstrip("\t"))
        elif line.strip():  # a non-indented, non-blank line ends the recipe
            break
    assert out, f"Makefile target {target!r} has an empty recipe — parser or target moved"
    return out


def test_selfbuilt_matrix_covers_every_dockerfile() -> None:
    """Every Dockerfile in the tree is scanned, or carries a registered exemption.

    This is the assertion #1337 was missing.
    `test_report_expected_counts_match_matrix_sizes` below only checks the
    matrix against a literal it sits next to,
    so bumping both together passes while an image sits in neither list.

    ⭐ COUNTERFACTUAL (measured, not assumed — the three inventories restored whole
    from origin/main via `git show <base>:<file>`, these tests kept):
      * this test                          RED on base, naming both images
      * test_the_three_selfbuilt_build_lists_agree  RED on base — but ONLY via its
        anti-vacuity floor (the three lists themselves agreed, at 5). A derived
        red, not a second independent detection; do not count it as one.
      * test_scan_exemptions_stay_undeployable     GREEN before and after
      * test_pr_build_triggers_on_every_matrix_entrys_dockerfile  GREEN before and
        after (base's paths filter did cover base's matrix)
      * all 15 pre-existing tests in this module   GREEN on base
    So this ONE assertion is the detection the PR actually buys. The others are
    regression pins for mechanisms that happened to be consistent already and had
    no gate — real value, but NOT a #1337 find (the #1294 lesson).

    ⛔ Mutation counts are stated for the SUITE, not per test, deliberately: the
    harness runs every guard here against each mutation, so a per-test sub-count
    cannot be read off the results. An earlier docstring claimed "5/5" while
    naming four scenarios, and the PR-level total inherited that error — the kind
    of unreconciled number this module elsewhere refuses to write.
    Measured: **16 mutations, 0 survivors**, against a green baseline. They cover
    removing an image from the matrix; a wrong build context; registering a
    Dockerfile after the literal by item-assignment, `.update()`, `|=`, or a
    rebind nested in an `if`; emptying the registry literal; narrowing the name
    rule back to a `.` separator; dropping its over-match subtraction; a `!`
    negation in the PR filter; dropping a path from that filter; dropping an entry
    from any of the three build lists; and making one deployable tree contribute
    nothing.
    """
    contexts = _dockerfile_contexts()
    # ⛔ Anti-vacuity, and NOT a `>=` floor: the registry is compared against a
    # filesystem walk, so the guard cannot be satisfied by a parser that returns
    # less than the tree holds. The walk deliberately uses a WIDER name rule than
    # upstream's `rglob("Dockerfile*")`, because `logrotate.Dockerfile` /
    # `Containerfile` would otherwise be in no list, no registry and no exemption
    # — a hole with the same shape as the one this module exists to close.
    discovered = _discover_dockerfiles()
    assert discovered, "no Dockerfiles discovered at all — the walk is broken, not the repo"
    unregistered = sorted(discovered - set(contexts))
    ghost = sorted(set(contexts) - discovered)
    assert not unregistered, (
        "image-build recipe(s) present in the tree but absent from "
        f"DOCKERFILE_CONTEXTS: {unregistered}\n"
        "Register each one (that also forces its build context to be declared), "
        "then put it in the scan matrix or in _SCAN_EXEMPT. ⚠️ If the name is an "
        "alternate spelling, the upstream hook does NOT see it — this test is the "
        "only thing that does, and unlike that hook it cannot be waived from the "
        "PR body."
    )
    assert not ghost, (
        f"DOCKERFILE_CONTEXTS registers files that do not exist: {ghost}"
    )
    unknown_exempt = set(_SCAN_EXEMPT) - set(contexts)
    assert not unknown_exempt, (
        f"exemption for a Dockerfile that is not registered upstream: {sorted(unknown_exempt)}\n"
        "Registered means present in check_iac_vibe_rules.py DOCKERFILE_CONTEXTS."
    )

    expected = set(contexts) - set(_SCAN_EXEMPT)
    matrix = {e["dockerfile"] for e in _matrix_include("scan")}
    missing = sorted(expected - matrix)
    extra = sorted(matrix - expected)
    assert not missing and not extra, (
        "the nightly self-built scan matrix does not cover the Dockerfile inventory.\n"
        f"  built+deployed but NOT scanned: {missing}\n"
        f"  in the matrix but not a registered Dockerfile: {extra}\n"
        "Add it to the `scan` matrix in .github/workflows/nightly-image-scan.yaml "
        "(and to component-docker-build.yaml + `make docker-build-all`), or add a "
        "registered exemption to _SCAN_EXEMPT with a rationale and an EXIT "
        "condition. Silence-by-classification is what #1337 was."
    )

    # The build CONTEXT has to agree too: a matrix entry pointing at the right
    # Dockerfile with the wrong context builds nothing, or builds the wrong thing.
    for entry in _matrix_include("scan"):
        assert entry["context"] == contexts[entry["dockerfile"]], (
            f"{entry['name']}: scan matrix context {entry['context']!r} != the "
            f"registered build context {contexts[entry['dockerfile']]!r}"
        )


def test_dockerfile_name_rule_covers_the_spellings_it_claims_to() -> None:
    """Pin the name rule against known-positive and known-negative samples.

    ⛔ A discovery regex that silently matches nothing looks exactly like a clean
    repo, so it gets verified against samples rather than trusted. The hyphen and
    underscore cases are here because the first cut of this guard required a `.`
    separator and therefore missed `Dockerfile-runtime` — a spelling upstream's
    own discovery also misses, i.e. the hole would have been in BOTH.
    """
    must_match = [
        "Dockerfile", "dockerfile", "Dockerfile.dev", "Dockerfile-runtime",
        "Dockerfile_prod", "Dockerfile2", "Containerfile", "Containerfile.ci",
        "logrotate.Dockerfile", "sidecar.dockerfile",
    ]
    must_not_match = [
        ".dockerignore", "docker-compose.yml", "Dockerfilter.py", "README.md",
        "dockerfile_helpers.py", "build.sh",
        # ⛔ Pinned in the NEGATIVE direction on purpose: a template is not a
        # buildable recipe, and an earlier round of this PR removed `.tpl` from
        # the subtraction list with no sample holding it either way. The rule
        # only counts as "verified against samples" if both directions are here.
        "Dockerfile.tpl", "Containerfile.tpl",
    ]
    missed = [n for n in must_match if not _is_dockerfile_name(n)]
    over = [n for n in must_not_match if _is_dockerfile_name(n)]
    assert not missed, f"name rule misses image-build spellings: {missed}"
    assert not over, f"name rule over-matches non-Dockerfiles: {over}"


def test_scan_exemptions_stay_undeployable() -> None:
    """Re-derive each exemption's premise instead of trusting the list.

    ⚠️ GREEN-before/GREEN-after, same as the sibling above: the two bench
    fixtures were already undeployable. What it buys is that the exemption
    registry this PR introduces cannot become the next hiding place — mutation:
    adding `helm/vector/projection-gate/Dockerfile` to _SCAN_EXEMPT (with a
    plausible rationale) turns it red on the `tests/` rule.
    """
    for path, rationale in _SCAN_EXEMPT.items():
        assert (ROOT / path).is_file(), (
            f"exempt Dockerfile {path} does not exist — a stale exemption is a hole "
            "that reads like a decision"
        )
        assert len(rationale) > 40, f"exemption {path} needs a real rationale"
        # The load-bearing structural rule: only test fixtures may be exempt. A
        # chart- or manifest-shipped image cannot be waived by adding a line here.
        assert path.startswith("tests/"), (
            f"{path} is outside tests/ — an image that ships with a chart or a "
            "manifest may NOT be exempted from CVE scanning by list membership. "
            "That is precisely the shape #1337 found in production."
        )

    # ⛔ HONEST SCOPE — the load-bearing rule is the `tests/` prefix above, NOT
    # this scan. Blind review measured the limit: charts reference images by
    # REGISTRY REF (`repository: federation-audit-sidecar`), never by build
    # directory, so `helm/federation-gateway/audit-sidecar` appears in exactly
    # zero deployable YAML files today — i.e. if that (default-enabled,
    # in-production) image were listed in _SCAN_EXEMPT, this half would report
    # clean and only the prefix rule would stop it. Keep it as a cheap second net
    # for the one case it does catch — a compose/values file that builds straight
    # out of the exempt directory — and do not describe it as more than that.
    exempt_dirs = {Path(p).parent.as_posix() for p in _SCAN_EXEMPT}
    offenders: list[tuple[str, str]] = []
    scanned = 0
    missing_trees = [t for t in _DEPLOYABLE_TREES if not (ROOT / t).is_dir()]
    # Anti-vacuity: `if not base.is_dir(): continue` silently shrinks the face to
    # nothing if a tree is renamed. Make that a failure, not a quiet pass.
    assert not missing_trees, (
        f"deployable tree(s) missing: {missing_trees} — renamed? This scan would "
        "have silently covered less while staying green; update _DEPLOYABLE_TREES."
    )
    per_tree: dict[str, int] = {}
    for tree in _DEPLOYABLE_TREES:
        n = 0
        for f in (ROOT / tree).rglob("*"):
            if not f.is_file() or f.suffix not in (".yaml", ".yml", ".json", ".tpl"):
                continue
            n += 1
            scanned += 1
            text = f.read_text(encoding="utf-8", errors="replace")
            for d in exempt_dirs:
                if d in text:
                    offenders.append((f.relative_to(ROOT).as_posix(), d))
        per_tree[tree] = n
    # ⛔ PER-TREE, not a global floor. A global `scanned > 50` was satisfied by
    # `helm/` alone (105 files), so emptying the other three trees left the scan
    # three-quarters blind and green — the very shape this module already
    # documents fixing for the nightly-workflow discovery further down.
    empty = [t for t, n in per_tree.items() if n == 0]
    assert not empty, (
        f"deployable tree(s) contributed no scannable files: {empty} (counts: "
        f"{per_tree}) — the scan face shrank; fix the walk or update "
        "_DEPLOYABLE_TREES deliberately."
    )
    # ⛔ AND a magnitude floor. Replacing the global floor with the per-tree one
    # was a REGRESSION, not a strengthening (blind review): collapse three trees
    # to one file each and every tree is still non-empty, so the scan reads ~108
    # files instead of ~180 and stays green. Keep both — they fail on different
    # shapes, and an earlier cut left `scanned` computed but never asserted.
    assert scanned > 50, (
        f"only {scanned} deployable files read across {per_tree} — the scan face "
        "collapsed in aggregate even though no single tree is empty"
    )
    assert not offenders, (
        "an exempt Dockerfile's directory is referenced from a DEPLOYABLE tree, so "
        "the exemption's premise ('never deployed') is now false:\n"
        + "\n".join(f"  {p} -> {d}" for p, d in sorted(offenders))
    )


def test_the_three_selfbuilt_build_lists_agree() -> None:
    """nightly scan == component-docker-build == `make docker-build-all`.

    All three built the same five images and were held together by a comment.

    ⚠️ On origin/main the three lists genuinely DID agree (at 5), so the equality
    half catches nothing that existed — it is a regression pin for the next
    divergence. It does go red on base, but only through the anti-vacuity floor
    below; that is a derived red, not an independent find. Mutation-verified
    instead: dropping an entry from the PR workflow, from `docker-build-all`, or
    from `trivy-scan-all`'s loop each turns it red, as does shrinking all three
    at once. See the suite-level mutation note on
    test_selfbuilt_matrix_covers_every_dockerfile for the measured total — do not
    restate a per-test sub-count here, that is where the earlier arithmetic error
    came from.
    """
    nightly = {(e["name"], e["context"], e["dockerfile"]) for e in _matrix_include("scan")}
    # ⛔ Floor first. Every assertion below is a set EQUALITY, and ∅ == ∅ — with
    # both matrices emptied and the Makefile lines deleted this test passed
    # cleanly (blind review). Equality between two collections proves nothing
    # about either being non-empty.
    #
    # ⛔ Derived from the FILESYSTEM, not from DOCKERFILE_CONTEXTS. An earlier cut
    # used the registry, which is on the other side of the same guard: empty the
    # registry literal and the floor became `0 >= 0 - 2`, i.e. the empty matrix it
    # exists to reject satisfied it. A floor computed from the artifact it guards
    # proves nothing.
    floor = len(_discover_dockerfiles()) - len(_SCAN_EXEMPT)
    assert len(nightly) >= floor, (
        f"only {len(nightly)} entries in the self-built scan matrix, but the tree "
        f"holds {floor} non-exempt image-build recipes — the matrix shrank"
    )

    pr_wf = yaml.safe_load(
        (WORKFLOWS_DIR / "component-docker-build.yaml").read_text(encoding="utf-8")
    )
    pr = {
        (e["name"], e["context"], e["dockerfile"])
        for e in pr_wf["jobs"]["build"]["strategy"]["matrix"]["include"]
    }
    assert nightly == pr, (
        "nightly-image-scan.yaml `scan` and component-docker-build.yaml `build` "
        f"disagree.\n  only nightly: {sorted(nightly - pr)}\n  only PR: {sorted(pr - nightly)}"
    )

    # `docker buildx build --load -t local-test:<name> [-f <file>] <context>`
    build_re = re.compile(
        r"docker buildx build\s+--load\s+-t\s+local-test:(?P<name>[\w.-]+)\s+"
        r"(?:-f\s+(?P<file>\S+)\s+)?(?P<context>\S+)\s*$"
    )
    make_built: set[tuple[str, str, str]] = set()
    for line in _make_recipe("docker-build-all"):
        m = build_re.search(line)
        if m:
            name, file_, ctx = m.group("name"), m.group("file"), m.group("context")
            # No -f means the Dockerfile lives in the context dir.
            make_built.add((name, ctx, file_ or f"{ctx.rstrip('/')}/Dockerfile"))
    assert nightly == make_built, (
        "`make docker-build-all` and the nightly `scan` matrix disagree.\n"
        f"  only in the matrix: {sorted(nightly - make_built)}\n"
        f"  only in the Makefile: {sorted(make_built - nightly)}\n"
        "pre-tag is where a build break is supposed to surface before a tag; an "
        "image the matrix scans but pre-tag never builds is half-covered."
    )

    # trivy-scan-all iterates a hand-written name list in a shell `for`.
    scan_names: set[str] = set()
    for line in _make_recipe("trivy-scan-all"):
        m = re.search(r"for\s+img\s+in\s+(?P<names>[^;]+);", line)
        if m:
            scan_names = set(m.group("names").split())
    assert scan_names == {n for n, _, _ in nightly}, (
        "`make trivy-scan-all`'s image list != the nightly scan matrix names.\n"
        f"  only in the matrix: {sorted({n for n, _, _ in nightly} - scan_names)}\n"
        f"  only in trivy-scan-all: {sorted(scan_names - {n for n, _, _ in nightly})}"
    )


def test_pr_build_triggers_on_every_matrix_entrys_dockerfile() -> None:
    """Widening the PR-build matrix without widening its `paths:` buys nothing.

    Exactly the property `test_the_resolve_workflow_triggers_on_overlay_values_files`
    enforces for image-ref-resolve.yaml, written for the sibling workflow. Blind
    review found it missing: an 8th image could be added to the matrix, the
    nightly scan, `docker-build-all` and `trivy-scan-all` — every guard green —
    while PRs that only touch its build inputs never run the build at all. For
    the two chart-shipped images this workflow is one of only two automated
    pre-merge build paths, so a silent miss here is not cosmetic.
    """
    wf = yaml.safe_load(
        (WORKFLOWS_DIR / "component-docker-build.yaml").read_text(encoding="utf-8")
    )
    # PyYAML parses the `on:` key as the boolean True (YAML 1.1) — the same trap
    # the Helm side of this repo documents. Accept either spelling.
    on = wf.get("on", wf.get(True))
    paths = on["pull_request"]["paths"]
    assert paths, "component-docker-build.yaml has no pull_request.paths filter"

    # ⛔ `!` NEGATION IS THE UNSAFE DIRECTION and this translator cannot model it:
    # `helm/**` + `!helm/**/Dockerfile` would make every entry look covered here
    # while GitHub excludes exactly the file that matters. Refuse to grade rather
    # than grade wrongly. (`?`, `+` and `[]` are escaped to literals below, which
    # errs toward a false RED — annoying, never silent.)
    negations = [p for p in paths if p.lstrip().startswith("!")]
    assert not negations, (
        f"`on.pull_request.paths` uses negation patterns {negations}, which this "
        "check cannot evaluate — it would report every matrix entry as covered "
        "while GitHub excluded it. Express the filter without `!`, or teach this "
        "test real minimatch semantics before adding one."
    )

    def _to_re(glob: str) -> re.Pattern[str]:
        out, i = [], 0
        while i < len(glob):
            if glob.startswith("**", i):
                out.append(".*")
                i += 2
            elif glob[i] == "*":
                out.append("[^/]*")
                i += 1
            else:
                out.append(re.escape(glob[i]))
                i += 1
        return re.compile("^" + "".join(out) + "$")

    patterns = [_to_re(p) for p in paths]
    entries = wf["jobs"]["build"]["strategy"]["matrix"]["include"]
    assert entries, "component-docker-build.yaml build matrix is empty"
    uncovered = [
        e["name"] for e in entries
        if not any(rx.match(e["dockerfile"]) for rx in patterns)
    ]
    assert not uncovered, (
        "these build-matrix entries' Dockerfiles are not matched by any "
        f"`on.pull_request.paths` pattern: {uncovered}\n"
        f"  paths: {paths}\n"
        "The matrix would build them, but no PR that changes only their build "
        "inputs would ever start the workflow — scan face and TRIGGER face have "
        "to widen together (#1302's lesson, #1337's shape)."
    )


def test_report_expected_counts_match_matrix_sizes() -> None:
    """The report's hardcoded EXPECTED (7 / 15 / 4) must track the matrix sizes.

    Load-bearing beyond bookkeeping: EXPECTED is what turns "an image did not
    scan" into `missing = EXPECTED - present > 0`, and that is the ONLY thing
    separating a degraded run from a silent clean one. An EXPECTED set one too
    LOW hides exactly one unscanned image per night, permanently, with the
    tracking issue reporting the shortfall away.
    """
    n_selfbuilt = len(_matrix_include("scan"))
    n_thirdparty = len(_matrix_include("scan-thirdparty"))
    n_delivered = len(_matrix_include("scan-delivered"))
    run = _aggregate_run()

    m_sb = re.search(r'frags-sb.*?\s(\d+)\s+"self-built component"', run, re.S)
    m_tp = re.search(r'frags-tp.*?\s(\d+)\s+"third-party upstream image"', run, re.S)
    m_dl = re.search(r'frags-dl.*?\s(\d+)\s+"customer-delivered image"', run, re.S)

    assert m_sb is not None, "could not find the self-built file_cve_report.sh EXPECTED arg"
    assert m_tp is not None, "could not find the third-party file_cve_report.sh EXPECTED arg"
    assert m_dl is not None, (
        "could not find the customer-delivered file_cve_report.sh EXPECTED arg — "
        "the frags-dl call site or its KIND string moved, and this guard would "
        "otherwise stop checking that bucket entirely")
    assert int(m_sb.group(1)) == n_selfbuilt, (
        f"report self-built EXPECTED={m_sb.group(1)} != {n_selfbuilt} scan matrix entries"
    )
    assert int(m_tp.group(1)) == n_thirdparty, (
        f"report third-party EXPECTED={m_tp.group(1)} != {n_thirdparty} scan-thirdparty entries"
    )
    assert int(m_dl.group(1)) == n_delivered, (
        f"report customer-delivered EXPECTED={m_dl.group(1)} != {n_delivered} "
        f"scan-delivered entries"
    )


# ── GitHub API contract guard (the 33-night alerting outage) ─────────────────


REPORT_SH_NAME = "file_cve_report.sh"

# A call site is the script in COMMAND position: at the start of a (folded) line
# or right after a command separator, optionally through an interpreter, with any
# path prefix. Anchoring on the literal `bash scripts/ops/file_cve_report.sh`
# would miss `./scripts/…`, `sh scripts/…`, a doubled space, and a
# `"$GITHUB_WORKSPACE/…"` prefix.
#
# Widening it buys quieter CI, NOT safety. Safety comes from the conservation
# check below, which bounds matches ≤ mentions: an unrecognised spelling makes
# the two counts disagree and fails LOUDLY, so no spelling can slip past in
# silence regardless of how wide this pattern is.
#
# `=` is excluded from the path prefix on purpose: `REPORT=scripts/ops/<name>` is
# an ASSIGNMENT, not a call, and letting it match produced an EMPTY chunk that
# still counted toward the conservation check below — satisfying the count while
# the real `bash "$REPORT" …` invocation went unscanned, which is precisely the
# silent direction that check exists to invert. Excluded, an assignment matches
# nothing and the mention/chunk counts disagree loudly.
REPORT_SH_CALL_RE = re.compile(
    r"(?:^|[;&|]\s*)\s*(?:\w*sh\s+)?[^\s;&|=]*" + re.escape(REPORT_SH_NAME) + r"[\"']?",
    re.M,
)

# Steps where the script name legitimately appears WITHOUT being an invocation
# (prose inside an argument). Keyed by the `where` string below, value = how many
# such mentions. Empty today. Add an entry only after confirming the occurrence
# really is prose — it is the escape hatch for the conservation check, so a
# careless entry here is how that check goes quiet.
SCRIPT_MENTION_ALLOWLIST: dict[str, int] = {}


def _strip_shell_comments(run: str) -> str:
    """Drop comment lines from a `run:` block, keeping continuation lines.

    Comments must go before continuations are folded, or their prose (which does
    contain backticks) lands in the token stream. But a leading `#` is only a
    comment when the shell is not already mid-word: after a trailing `\\` the next
    line CONTINUES the quoted argument, so its `#` is ordinary text and dropping
    it deletes real argument content — before the detector is ever shown it.

    Measured under bash: ``"prose one \\`` / ``# … live `id -u` \\`` / ``more"``
    arrives at the script as `prose one # … live 197609 more`, while the naive
    line-level filter yielded a chunk containing no backtick at all AND left the
    conservation count balanced. The blind spot was in what gets SUBMITTED for
    checking, not in the check — which is why this is a named function with its
    own test rather than three lines inline.

    Continuation-awareness alone was not enough: a `#` line inside a genuine
    multi-line double-quoted argument (no trailing `\\`) was still dropped. So
    track the double quote as well — a `run:` block is a whole script read from
    byte 0, so that state is determined, exactly as in the sibling script scanner.
    """
    kept: list[str] = []
    continued = False
    in_dq = False
    for ln in run.splitlines():
        if not continued and not in_dq and ln.lstrip().startswith("#"):
            continue  # a real comment — and a comment cannot itself continue
        kept.append(ln)
        continued = ln.rstrip().endswith("\\")
        # Unescaped double quotes on this line flip the state for the next one.
        j, quotes = 0, 0
        while j < len(ln):
            if ln[j] == "\\":
                j += 2
                continue
            if ln[j] == '"':
                quotes += 1
            j += 1
        if quotes % 2:
            in_dq = not in_dq
    return "\n".join(kept)


def _report_call_chunks() -> list[str]:
    """Every `file_cve_report.sh` invocation in ANY workflow, as RAW shell text.

    Reads the REAL call sites rather than a fixture: the outage was caused by
    the arguments the workflow actually passes, and a synthetic test would have
    stayed green through all 33 failures.

    DISCOVERED across workflows, not hardcoded to nightly-image-scan's report job,
    for the reason stated at the top of the file — a hardcoded lookup reads like a
    repo-wide rule while covering exactly one step, which is how #1275 landed
    outside the sibling guards.

    ⚠️ Scope, since "discovery" invites a bigger reading than this earns: it is
    every workflow, but only invocations of THIS script. The sibling issue-filer
    `file_race_report.py` (nightly-race.yaml) is NOT covered, and cannot simply be
    folded in — it legitimately passes `--run-url "${GITHUB_SERVER_URL}/…"`, so a
    shared rule would need a per-argument policy on which arguments may expand,
    not one blanket "no `$` anywhere". A new issue-filing step calling a THIRD
    script is likewise uncovered until someone widens this.

    Kept separate from `_report_calls()` because quoting survives here. Anything
    reasoning about how bash will EXPAND an argument has to read this — see
    `_unescaped_expansions` for why the parsed argv cannot answer that.
    """
    return [c for rec in _report_call_scan() for c in rec["chunks"]]


def _report_call_scan() -> list[dict]:
    """Per-step scan: how often the script is NAMED vs how many calls parsed.

    Both numbers are kept because their disagreement is the interesting event —
    see `test_every_script_mention_is_an_accounted_call_site`.
    """
    scan: list[dict] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        wf = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                run = step.get("run")
                if not isinstance(run, str) or REPORT_SH_NAME not in run:
                    continue
                flat = re.sub(r"\\\n\s*", " ", _strip_shell_comments(run))
                scan.append({
                    "where": f"{path.name}::{job_name}::{step.get('name') or '?'}",
                    "mentions": flat.count(REPORT_SH_NAME),
                    "chunks": [
                        chunk.split("|| rc=")[0].splitlines()[0]
                        for chunk in REPORT_SH_CALL_RE.split(flat)[1:]
                    ],
                })
    return scan


def _report_calls() -> list[list[str]]:
    """The same invocations, parsed into argv lists.

    ⚠️ These strings are NOT what bash hands the script. `shlex` keeps a
    backslash that bash consumes, so each escaped metacharacter reads ONE
    character longer here than in the issue body (`\\``  vs `` ` ``). Fine for
    "which component does this name" questions; wrong for any byte-level or
    length assertion — use `_report_call_chunks()` and reason about the raw text
    for those.
    """
    return [shlex.split(chunk) for chunk in _report_call_chunks()]


def _report_sh() -> str:
    return REPORT_SH.read_text(encoding="utf-8")


def _shell_const(name: str) -> int:
    m = re.search(rf"^{name}=(\d+)", _report_sh(), re.M)
    assert m is not None, f"{name} not found in file_cve_report.sh"
    return int(m.group(1))


def test_report_call_sites_are_parseable() -> None:
    """Guard the guard: if this stops matching, the checks below go vacuous."""
    calls = _report_calls()
    assert len(calls) == 3, f"expected 3 file_cve_report.sh calls, parsed {len(calls)}"
    for args in calls:
        assert len(args) >= 5, f"call site missing positional args: {args}"
    # One dir per bucket. Two calls pointed at the same fragment dir would double-
    # count one bucket and report the other as entirely unscanned — and both
    # tracking issues would still be filed, so nothing else here would notice.
    dirs = [a[0] for a in calls]
    assert len(set(dirs)) == len(dirs), f"two report calls share a fragments dir: {dirs}"
    labels = [a[1] for a in calls]
    assert len(set(labels)) == len(labels), (
        f"two report calls share a label: {labels} — the label is the issue dedup "
        "key, so two buckets sharing one would overwrite each other's issue nightly")


def _failure_explainer_run() -> str:
    """The `run:` of the report job's failure-explainer step."""
    steps = _workflow()["jobs"]["report"]["steps"]
    step = next(
        (s for s in steps if "Explain" in (s.get("name") or "")), None)
    assert step is not None, (
        "the report job has no 'Explain a likely failure cause' step — it was "
        "renamed or removed, and the guard below would pass over nothing")
    return step["run"]


def test_the_failure_explainer_names_every_report_label() -> None:
    """The only diagnostic this job prints must name every label it can fail on.

    `file_cve_report.sh` delivers the alert unlabelled and then exits 1 when its
    label is unusable, so ANY ONE of the buckets can red this job on its own. The
    explainer is the whole diagnosis a maintainer gets on the run page (the real
    cause of the 33-night outage was four scrolls into a step log), and it listed
    two labels while three call sites existed — pointing the reader at two labels
    that are fine.

    ⛔ DERIVED from the call sites, not a copy of the list. A fourth bucket makes
    this red on the day it is added, which is the point: enumerating the labels
    here would reproduce exactly the staleness it is guarding against.
    """
    labels = [args[1] for args in _report_calls()]
    assert len(labels) >= 2, (
        f"only {len(labels)} report call site(s) parsed ({labels}) — "
        "_report_calls() stopped matching and this check would be vacuous")

    # ⛔ Whole-token match, not `lbl in text`. Substring matching reports a label
    # as present when only a LONGER label containing it was written: with the
    # real names, `nightly-cve` is a prefix of `nightly-cve-thirdparty`, so a
    # text naming just the long one would satisfy the short one for free.
    # (Same shape as feedback_substring_match_hides_drift; found by review.)
    #
    # ⛔ And the boundary is derived FROM EACH LABEL, not from a `nightly-cve`
    # prefix pattern. This test's whole claim is that it survives a fourth
    # bucket; a prefix-anchored extractor would see a differently-named fourth
    # label as absent no matter what the step says, producing a red whose advice
    # ("add the label to the step") is already done — the failure mode that
    # trains readers to ignore it. Lookarounds on `[\w-]` are what make
    # `nightly-cve` distinct from `nightly-cve-thirdparty` without any literal.
    text = _failure_explainer_run()
    missing = sorted(lbl for lbl in set(labels) if not _names_label(text, lbl))
    assert not missing, (
        f"the failure-explainer step does not mention these tracking label(s): "
        f"{missing}\n"
        f"  it names: {text.strip()[:200]}…\n"
        "Any one of them going missing reds this job, and this message is the "
        "only diagnosis on the run page. Add the label(s) to the "
        "'Explain a likely failure cause' step in "
        ".github/workflows/nightly-image-scan.yaml."
    )


def test_comment_stripper_keeps_continuation_lines() -> None:
    """Pin `_strip_shell_comments` on synthetic input, not on today's workflow.

    The real `run:` block happens to contain no `#`-leading continuation, so
    reverting this helper to a plain `startswith("#")` filter changes nothing
    about the current file — measured: that mutation leaves the whole suite
    GREEN. A guard whose logic is only exercised by data that does not exercise
    it is not guarded at all, so the discriminating cases live here.
    """
    # A `#` line that follows a continuation is argument text, not a comment.
    continued = 'bash x.sh "prose one \\\n# live -> `id -u` \\\nprose two"\n'
    assert "`id -u`" in _strip_shell_comments(continued), (
        "a continuation line was dropped as if it were a comment — its content, "
        "including any live substitution, becomes invisible to every guard"
    )

    # A genuine comment still goes, otherwise its prose (which carries backticks
    # in this very workflow) would be scanned as if it were an argument.
    plain = '# a comment mentioning `tests/foo.py`\nbash x.sh "arg"\n'
    assert "`tests/foo.py`" not in _strip_shell_comments(plain)

    # A comment cannot itself continue: the line after one is a fresh command,
    # so a `#` there is a comment again.
    after = '# comment ending in a backslash \\\n# second comment `id`\nbash x.sh "a"\n'
    assert "`id`" not in _strip_shell_comments(after)

    # ...and a `#` inside a genuine MULTI-LINE quoted argument — no trailing
    # backslash anywhere — is argument text too. Continuation-tracking alone
    # missed this one; it needs the quote state.
    multiline = 'bash x.sh "line one\n# live -> `id -u`\nline three"\n'
    assert "`id -u`" in _strip_shell_comments(multiline), (
        "a `#` line inside an open double-quoted argument was dropped as a comment"
    )


def test_call_site_regex_ignores_a_plain_assignment() -> None:
    """`REPORT=scripts/ops/<name>` is not an invocation and must not count as one.

    It used to match, yielding an EMPTY chunk that still counted toward the
    conservation check — so the mention/chunk totals agreed while the real
    `bash "$REPORT" … "prose"` invocation was never scanned. That is exactly the
    silent direction the conservation check exists to invert, reintroduced by the
    matcher itself.
    """
    assign = "          REPORT=scripts/ops/file_cve_report.sh"
    assert len(REPORT_SH_CALL_RE.split(assign)) == 1, (
        "an assignment matched as a call site; it contributes an empty chunk that "
        "silently balances the conservation count"
    )
    # The real thing still matches, so this is not satisfied by matching nothing.
    call = '          bash scripts/ops/file_cve_report.sh a b "c" 1 "d" "e"'
    assert len(REPORT_SH_CALL_RE.split(call)) == 2


def test_every_script_mention_is_an_accounted_call_site() -> None:
    """Every occurrence of the script name must be recognised, or fail loudly.

    Recognising shell command position is UNBOUNDED. `if …; then bash …`,
    `for …; do bash …`, `( bash … )`, `{ bash …; }`, `env FOO=1 bash …`,
    `timeout 60 bash …`, `bash -x …` are all ordinary and all defeat a
    position-anchored pattern — measured, all seven slip past `REPORT_SH_CALL_RE`.
    Enumerating spellings the pattern already matches proves nothing: it is the
    pattern written a second time, and it passes precisely because the cases were
    chosen to fit it.

    So stop trying to recognise every position and require CONSERVATION instead:
    every non-comment mention of the script in a scanned `run:` must have produced
    a call-site chunk. That inverts the failure direction, which is the whole
    point — an unrecognised spelling now fails HERE instead of silently
    contributing nothing while `test_report_call_sites_are_parseable`'s count
    stays satisfied by the call sites that did match.
    """
    scan = _report_call_scan()
    assert scan, (
        f"no workflow step mentions {REPORT_SH_NAME} — discovery is dead and every "
        "guard built on it is vacuous"
    )
    for rec in scan:
        expected = len(rec["chunks"]) + SCRIPT_MENTION_ALLOWLIST.get(rec["where"], 0)
        assert rec["mentions"] == expected, (
            f"{rec['where']}: the script is named {rec['mentions']}x but only "
            f"{len(rec['chunks'])} call site(s) were recognised.\n"
            "An invocation this module cannot parse is INVISIBLE to every guard "
            "below — its arguments are never checked for shell expansion. Either "
            "put the invocation in a position REPORT_SH_CALL_RE matches (simplest: "
            "on its own line), widen the pattern, or — if this really is prose that "
            "merely names the script — add it to SCRIPT_MENTION_ALLOWLIST."
        )


def _unescaped_expansions(text: str) -> list[tuple[str, int]]:
    """Offsets in `text` where bash would REWRITE the word.

    Scope, stated precisely because the obvious phrasing overclaims: inside a
    DOUBLE-QUOTED word bash treats exactly three characters specially — `` ` ``,
    `$`, and `\\` — so for such words the complete set of rewrite triggers is the
    first two, with the third as their escape. This function is that rule, not a
    list of spellings that have burned us.

    It does NOT cover an UNQUOTED word, where globbing, brace and tilde expansion
    also rewrite (`*.yaml`, `{a,b}`, `frags-[01]`, `~/x` all pass through here
    unflagged — measured). Today every argument carrying prose is double-quoted
    and the unquoted ones are literals (`frags-sb`, `nightly-cve`, `7`), so the
    gap is not live; it is recorded because the guard reads as universal and is
    not. Quoting-state tracking is what would close it, and that is exactly the
    thing the paragraph below explains this function must not attempt.

    Flagging `` ` `` and `$(` alone would be a partial enumeration even within the
    double-quoted scope, and it left both `$VAR` (silently substitutes — and the
    filing step has `GH_TOKEN` in its env, on a PUBLIC repo) and `"$PROSE"`
    (hoisting the argument into a shell variable, the obvious readability
    refactor of a 1.9 KB line) invisible to this guard.

    Parity-counting the escape is what the shell itself does, and it is why a
    lookbehind (`(?<!\\\\)`) is wrong: in `C:\\\\$(cmd)` the backslash is itself
    escaped, so the substitution IS live and a lookbehind reads it as safe.

    Deliberately NOT quote-state aware, and the asymmetry matters. Tracking
    single quotes would let it accept a genuinely-literal `` ` `` inside '...',
    but this prose is full of apostrophes ("Renovate's last weekly run",
    "mariadb's 15 findings"). A naive tracker reads the first one as an opening
    quote and then treats the rest of the argument as single-quoted — i.e. it
    stops reporting, silently, which is the exact failure mode this guard
    exists to end. Over-reporting a single-quoted backtick fails LOUD and costs
    one escape; under-reporting ships another broken alert.
    """
    hits: list[tuple[str, int]] = []
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2  # escaped — whatever follows is literal to bash
            continue
        if text[i] in "`$":
            hits.append((text[i], i))
        i += 1
    return hits


def test_report_args_contain_no_shell_expansion() -> None:
    """No argument to `file_cve_report.sh` may be silently rewritten by bash.

    The self-built remediation prose quotes the exact command a reader is being
    told about (``go get google.golang.org/grpc@...``). Written with a bare
    backtick pair inside the double-quoted argument, bash does not render it —
    it RUNS it, on the runner, and substitutes the (empty) stdout. The reader
    then gets "forcing a local  DOES compile": the one command the sentence
    exists to name, deleted.

    Nothing reports it. A command substitution that fails inside a word does not
    set the enclosing simple command's status, so `set -e` does not abort and
    `|| rc=1` never fires — the job stays GREEN while the alert ships with a
    hole. That is the same shape as the 33-night outage above: the alerting path
    degraded silently, and only the content was wrong instead of missing.

    Scanned on the RAW call text, not `_report_calls()`: shlex.split resolves
    quoting away, so on the parsed argv an escaped and an unescaped backtick are
    the same string and this guard would be structurally blind to its own bug.

    Fix by escaping (``\\`` / ``\\$``) — that keeps the rendered issue body
    byte-identical. If a call site ever genuinely WANTS an expansion, teach this
    guard which argument may expand; that is a decision to make deliberately
    here, not a default to inherit.
    """
    chunks = _report_call_chunks()
    assert len(chunks) >= 2, (
        f"expected at least the 2 known file_cve_report.sh call sites, found "
        f"{len(chunks)} — the extraction drifted and this guard would pass vacuously."
    )
    for chunk, args in zip(chunks, _report_calls()):
        assert len(args) >= 6, (
            f"call site parsed to {len(args)} args, so the remediation argument "
            f"was not captured and nothing below is actually being checked: {args}"
        )
        hits = _unescaped_expansions(chunk)
        assert not hits, (
            "unescaped shell expansion in a file_cve_report.sh argument:\n"
            + "\n".join(
                f"  {kind!r} at offset {off}: ...{chunk[max(0, off - 60):off + 60]}..."
                for kind, off in hits
            )
            + "\nbash rewrites this before the script ever sees it, and the result "
            "goes verbatim into a PUBLIC tracking-issue body: `` ` ``/`$(` EXECUTE "
            "on the runner and substitute their stdout (deleting the text they "
            "replace), `$VAR` substitutes the value (`GH_TOKEN` is in this step's "
            "env). The run stays green either way — a failed expansion inside a "
            "word does not trip `set -e`, so `|| rc=1` never fires.\n"
            "In a DOUBLE-quoted argument, escape it as \\` or \\$ — the rendered "
            "text stays byte-identical. In a SINGLE-quoted one the text is already "
            "literal and this is a known over-report (see `_unescaped_expansions`); "
            "do NOT add backslashes there, they would land in the issue body — "
            "switch to double quotes and escape, or teach the guard. And if you "
            "moved the prose into a shell variable, the guard can no longer read "
            "the literal it must check — keep the argument inline."
        )


def test_expansion_detector_is_not_vacuous() -> None:
    """The guard above must still be able to see the bugs it was written for.

    Asserting only "the real call sites are clean" passes just as happily when
    the detector has been broken as when the workflow is correct. So splice each
    rewriting spelling back into the REAL chunk and require it to be caught, and
    require the escaped spellings to be left alone — a detector that flagged both
    would push authors toward deleting the prose instead of escaping it.

    Injected as a trailing argument rather than by substituting known content, so
    the test keeps biting after the prose is reworded.
    """
    chunk = _report_call_chunks()[0]
    assert not _unescaped_expansions(chunk), "precondition: real chunk is clean"

    must_catch = [
        "`go get google.golang.org/grpc@v1.82.1`",  # the original bug
        "$(whoami)",                                 # the modern spelling of it
        "$RUN_URL",                                  # bare parameter expansion
        "${GH_TOKEN}",                               # braced — leaks into a public issue
        "$PROSE",                                    # the argument hoisted into a variable
        # Escaped BACKSLASH before a live metachar: bash consumes `\\` as one
        # literal backslash and the substitution still runs. A lookbehind-based
        # detector reads this as safe; parity-counting does not.
        r"C:\\$(echo RAN)",
    ]
    for injected in must_catch:
        assert _unescaped_expansions(f'{chunk} "{injected}"'), (
            f"detector missed {injected!r} appended to the real call site — bash "
            "would rewrite that argument before the script saw it"
        )

    # Only ESCAPED metacharacters discriminate here. `*.yaml` / `~/x` / `100%`
    # inside double quotes contain neither `` ` `` nor `$`, so no implementation
    # of this shape could flag them — asserting on those would be decoration that
    # passes by construction rather than coverage.
    for benign in (r"\`go get x\`", r"\$(y)", r"\$VAR", r"\${GH_TOKEN}"):
        assert not _unescaped_expansions(f'{chunk} "{benign}"'), (
            f"detector wrongly flagged {benign!r}, which bash passes through "
            "literally — false positives push authors to delete prose"
        )


def test_remediation_text_names_only_real_components() -> None:
    """Remediation prose must not name a component the scan matrix no longer has.

    The self-built advice singles out `recipe-preview` as the one image whose
    findings a first-party bump CANNOT clear (its CVEs live in a bundled upstream
    promtool binary — #1058). That is a semantic claim about a specific component,
    and a rename/removal would silently turn it into a ghost reference telling the
    reader to reason about something that no longer exists.

    Scoped deliberately: only hyphenated tokens that correspond to a real
    `components/<name>/` directory are checked, so ordinary prose ("first-party",
    "pinned-dependency") cannot trip it.
    """
    matrix_names = {e["name"] for e in _matrix_include("scan")}
    for args in _report_calls():
        remediation = args[-1]
        for token in set(re.findall(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b", remediation)):
            if not (ROOT / "components" / token).is_dir():
                continue  # not a component name at all — ordinary prose
            assert token in matrix_names, (
                f"remediation text names component {token!r}, but the self-built scan "
                f"matrix holds {sorted(matrix_names)}. Either the component was "
                f"renamed/removed from the matrix or the advice is stale — a reader "
                f"following it would reason about an image that is no longer scanned."
            )


def test_label_descriptions_fit_the_github_api_cap() -> None:
    """The composed label description must fit GitHub's 100-char limit.

    This is the exact regression: `... and/or {KIND} images failing to
    build/scan on main` rendered 111 and 117 chars, the label API 422'd, and
    `gh issue create --label` then failed with "not found" every night.
    """
    cap = _shell_const("LABEL_DESC_MAX")
    assert cap == 100, (
        "LABEL_DESC_MAX must stay at GitHub's documented label-description limit "
        "of 100 — raising it does not raise the API's limit, it only hides the 422."
    )
    m = re.search(r'^label_desc="([^"]*)"', _report_sh(), re.M)
    assert m is not None, "could not find the label_desc template in file_cve_report.sh"
    template = m.group(1)

    for args in _report_calls():
        kind = args[4]
        desc = template.replace("${KIND}", kind)
        assert len(desc) <= cap, (
            f"label description for KIND={kind!r} is {len(desc)} chars (cap {cap}):\n"
            f"  {desc}\n"
            "GitHub rejects this with HTTP 422 and the tracking issue never gets filed. "
            "Shorten the template in file_cve_report.sh or the KIND argument."
        )


def test_issue_titles_fit_the_github_api_cap() -> None:
    """Base title + the runtime count suffix must fit the 256-char issue cap."""
    cap = _shell_const("ISSUE_TITLE_MAX")
    for args in _report_calls():
        title = args[2]
        budgeted = len(title) + TITLE_SUFFIX_BUDGET
        assert budgeted <= cap, (
            f"issue title {title!r} is {len(title)} chars; with the runtime "
            f"' — N fixable, M unscanned' suffix budget ({TITLE_SUFFIX_BUDGET}) it "
            f"reaches {budgeted} > {cap}."
        )


def _swallowed_label_create(source: str) -> list[str]:
    """Lines where a `gh label create` has its failure discarded.

    Continuations are folded FIRST. The real call site already spans two lines
    via `\\`, so a line-scoped scan would let `... --force \\` / `|| true` through
    — passing vacuously on precisely the shape it exists to catch.
    """
    # Comment lines are excluded on purpose: file_cve_report.sh's header narrates
    # the original `|| true` bug verbatim, and that prose must not trip its own guard.
    body = "\n".join(ln for ln in source.splitlines() if not ln.lstrip().startswith("#"))
    folded = re.sub(r"\\\n\s*", " ", body)
    return [
        ln.strip()
        for ln in folded.splitlines()
        # `|| :` is the same fail-open idiom spelled differently.
        if "label create" in ln and ("|| true" in ln or "|| :" in ln)
    ]


def _nightly_sources() -> dict[str, str]:
    """Every nightly workflow plus the scripts it references, as {label: text}.

    Discovered, not enumerated: a hardcoded list is how the previous version of
    these guards ended up covering exactly one workflow while reading like a
    repo-wide rule.
    """
    sources: dict[str, str] = {}
    for wf in NIGHTLY_WORKFLOWS:
        text = wf.read_text(encoding="utf-8")
        sources[wf.name] = text
        for rel in sorted(set(_SCRIPT_REF_RE.findall(text))):
            script = ROOT / rel
            if script.is_file():
                sources[f"{wf.name} -> {rel}"] = script.read_text(encoding="utf-8")
    return sources


def test_nightly_discovery_is_not_vacuous() -> None:
    """Guard the guard: an empty glob would make every check below pass silently.

    Pins the workflows known to exist so a RENAME surfaces here (with an obvious
    fix) instead of as silent loss of coverage. Adding a new nightly-*.yaml does
    NOT trip this — the found set only has to stay a superset.
    """
    for name in _EXTRA_SCANNED:
        assert (WORKFLOWS_DIR / name).is_file(), (
            f"{name} is listed in _EXTRA_SCANNED but does not exist. A named entry "
            "that silently resolves to nothing is the same vacuity a bad glob "
            "causes — delete it deliberately or fix the name."
        )
    # Every name is a LITERAL, including the ones that reach the scan via
    # _EXTRA_SCANNED. Writing `*_EXTRA_SCANNED` here instead was a real hole:
    # emptying that tuple emptied the expectation too, so a workflow could be
    # silently dropped from coverage and this test still passed. An assertion
    # derived from the thing it guards does not guard it.
    found = {p.name for p in NIGHTLY_WORKFLOWS}
    expected = {
        "nightly-image-scan.yaml",
        "nightly-mutation-pilot.yaml",
        "nightly-race.yaml",
        "nightly-vm-replay.yaml",
        "try-local-smoke.yaml",
    }
    missing = expected - found
    assert not missing, (
        f"nightly workflow(s) {sorted(missing)} no longer match `nightly-*.y*ml`. "
        "If they were renamed, update the expected set here — otherwise the API "
        "contract guards below stop covering them and pass vacuously."
    )
    # PER-WORKFLOW, not a global floor: `len(sources) > len(found)` was satisfied
    # by three workflows resolving helpers while a fourth resolved none, so a
    # whole workflow could sit outside every guard below and still read as covered.
    # Applied only to the GLOBBED nightly workflows. Named extras are scanned for
    # their own text and may legitimately inline everything — try-local-smoke does,
    # so demanding a helper of it would fail for a reason that says nothing about
    # coverage. Their existence is asserted above instead.
    sources = _nightly_sources()
    without_helpers = sorted(
        wf.name for wf in NIGHTLY_WORKFLOWS
        if wf.name not in _EXTRA_SCANNED
        and not any(k.startswith(f"{wf.name} -> ") for k in sources)
    )
    assert not without_helpers, (
        f"nightly workflow(s) {without_helpers} resolved to ZERO helper files. Either "
        "they genuinely run everything inline (then drop them from this assertion "
        "deliberately) or _SCRIPT_REF_RE has drifted and the guards below silently "
        f"stopped covering them. Sources seen: {sorted(sources)}"
    )


def test_label_creation_failure_is_not_swallowed() -> None:
    """`gh label create ... || true` is what turned a 422 into a month of silence.

    The label is the dedup key for the tracking issue, so its absence has to be
    handled explicitly (probe + title fallback + a red run), never discarded.

    Scans EVERY nightly workflow and its helper scripts, not just
    file_cve_report.sh: the failure mode is not specific to that script, and the
    next scheduled workflow to grow a label-keyed tracking issue would otherwise
    reintroduce it unguarded.
    """
    offenders = {
        label: lines
        for label, text in _nightly_sources().items()
        if (lines := _swallowed_label_create(text))
    }
    assert not offenders, (
        "`gh label create` must not be `|| true`-swallowed — the label is the issue "
        "dedup key and its absence breaks filing entirely. Probe with "
        "`gh api repos/O/R/labels/NAME` to tell 'create failed' from 'label absent', "
        f"degrade loudly, and red the run. Offenders: {offenders}"
    )


# Matching the ASSIGNMENT rather than the `--description` flag is deliberate:
# neither nightly script passes the text inline (the shell one templates it, the
# Python one hands a constant to an argv list), so a flag-scoped regex finds ZERO
# call sites and passes vacuously. Non-Python sources have no parser available
# here, so they keep a regex — the shell template is additionally resolved by
# test_label_descriptions_fit_the_github_api_cap above.
# `\w*` not `[A-Za-z_]*`: the latter rejected any prefix containing a digit, so a
# perfectly ordinary `V2_LABEL_DESC=` was invisible to the shell/YAML path.
_LABEL_DESC_ASSIGN = re.compile(
    r"""^\s*\w*label_desc\s*=\s*(['"])(.*?)\1""", re.M | re.I
)
_LABEL_DESC_NAME = re.compile(r"label_desc", re.I)


def _label_desc_literals(text: str, is_python: bool) -> list[tuple[str, bool]]:
    """Every label-description constant as (value, is_templated).

    Python goes through `ast`, not a regex, because the idiomatic way to write a
    description longer than one line is parenthesised implicit concatenation:

        LABEL_DESC = (
            "first half "
            "second half"
        )

    A line-anchored regex sees nothing there — so the very shape most likely to
    BREACH a 100-char cap was the one shape the guard could not see. (Repo rule:
    count structured sources by parsing them, not by grepping.)

    Two extraction shapes, because named bindings are not the only way to spell it:
      * an assignment whose NAME contains `label_desc` (today's two scripts), and
      * a string literal sitting immediately after a `--description` element in an
        argv list or call — the shape a NEW helper is most likely to use, since
        that is how you hand the value straight to `gh`. Missing it would repeat
        the mistake above: the guard blind to precisely the idiom people reach for.
    """
    out: list[tuple[str, bool]] = []
    if is_python:
        try:
            tree = ast.parse(text)
        except SyntaxError:  # not our file to police
            return out
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if not any(_LABEL_DESC_NAME.search(n) for n in names) or node.value is None:
                    continue
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    out.append(("<runtime-composed>", True))  # f-string / .format()
                    continue
                out.append((value, False) if isinstance(value, str) else ("<non-str>", True))
            elif isinstance(node, (ast.List, ast.Tuple, ast.Call)):
                elts = node.elts if isinstance(node, (ast.List, ast.Tuple)) else node.args
                for prev, nxt in zip(elts, elts[1:]):
                    if not (isinstance(prev, ast.Constant) and prev.value == "--description"):
                        continue
                    try:
                        value = ast.literal_eval(nxt)
                    except (ValueError, SyntaxError):
                        out.append(("<runtime-composed>", True))
                        continue
                    if isinstance(value, str):
                        out.append((value, False))
        return out
    for _quote, desc in _LABEL_DESC_ASSIGN.findall(text):
        out.append((desc, "$" in desc or "{" in desc))
    return out


def test_label_descriptions_fit_the_api_cap_repo_wide() -> None:
    """Every nightly label description must fit GitHub's 100-char cap.

    Complements the file_cve_report.sh template check above, which can only reason
    about that one script's `${KIND}` interpolation. 111/117 characters is exactly
    what 422'd for a month — and because the label is the dedup key, a description
    that is merely too LONG silently disables issue filing entirely.

    Templated values cannot be length-checked statically and are skipped. They are
    therefore NOT counted toward the anti-vacuity assertion below: an earlier draft
    counted them, so the assertion stayed satisfied by two skipped template strings
    while the single genuinely-checked literal could vanish unnoticed — an
    anti-vacuity guard that was itself vacuous.
    """
    cap = 100
    checked: list[str] = []
    offenders: list[str] = []
    for label, text in _nightly_sources().items():
        for desc, templated in _label_desc_literals(text, label.endswith(".py")):
            if templated:
                continue  # runtime-composed — see the per-script test above
            checked.append(f"{label} ({len(desc)})")
            if len(desc) > cap:
                offenders.append(f"{label}: {len(desc)} chars — {desc!r}")
    assert not offenders, (
        f"label description(s) exceed GitHub's {cap}-char cap; the API 422s, "
        "`gh issue create --label` then fails, and the tracking issue is never "
        "filed:\n  " + "\n  ".join(offenders)
    )
    assert checked, (
        "no label description was actually LENGTH-CHECKED in any nightly source "
        "(templated ones do not count). Either every description became runtime-"
        "composed — then this guard is inert and needs a different approach — or "
        "the extraction drifted. Do not leave it silently checking nothing, which "
        "is the shape that let the original outage survive 33 nights."
    )


def _label_create_steps_with_continue_on_error() -> list[str]:
    """Steps that create a label AND are marked `continue-on-error: true`.

    The YAML-native spelling of the same fail-open `|| true` cost. Only reachable
    now that these guards read the workflow files themselves.

    Checks BOTH levels and treats any enabled value as fail-open, not just a
    literal step-level `True`:
      * `continue-on-error` is valid on a JOB too (backtest.yaml and
        self-review-pass2.yaml both use it that way), and a job-level one
        swallows every step under it.
      * a templated `${{ ... }}` value yaml-loads as a STRING, so an `is True`
        comparison silently passes the very spelling used to make it conditional.
    """
    offenders: list[str] = []
    for wf in NIGHTLY_WORKFLOWS:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            job = job or {}
            job_coe = job.get("continue-on-error")
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                coe = step.get("continue-on-error", job_coe)
                if "label create" in run and coe is not None and coe is not False:
                    where = "step" if "continue-on-error" in step else "job"
                    offenders.append(
                        f"{wf.name}:{job_name}:{step.get('name') or '<unnamed>'} "
                        f"({where}-level continue-on-error={coe!r})"
                    )
    return offenders


def test_label_creation_is_not_continue_on_error() -> None:
    """`continue-on-error: true` on a label-create step is `|| true` in YAML."""
    offenders = _label_create_steps_with_continue_on_error()
    assert not offenders, (
        "label creation must not be marked continue-on-error — the label is the "
        "issue dedup key, so discarding its failure breaks filing exactly as "
        f"`|| true` did. Offending step(s): {offenders}"
    )


def test_swallow_guard_catches_the_multiline_form() -> None:
    """Discriminability: the guard must catch the shape the real call site uses.

    Without folding, the two-line spelling below reads as one line with
    `label create` and another with `|| true` — the guard would report clean.
    """
    multiline = (
        'if ! gh label create "$LABEL" --repo "$REPO" \\\n'
        '       --description "$d" --force || true; then\n'
    )
    assert _swallowed_label_create(multiline), "guard missed the backslash-continued form"
    assert _swallowed_label_create('gh label create "$L" --force || :\n'), "guard missed `|| :`"
    # And it must not fire on the current, correct call site.
    assert not _swallowed_label_create(
        'if ! gh label create "$LABEL" --repo "$REPO" \\\n       --force; then\n'
    )
