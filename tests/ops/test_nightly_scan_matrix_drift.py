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
import re
import shlex
import subprocess
import sys
from pathlib import Path

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


def test_the_resolve_workflow_triggers_on_overlay_values_files() -> None:
    """⛔ Scan face and TRIGGER face must widen together.

    `image-ref-resolve.yaml` is path-filtered. Widening the checker's globs
    without widening this filter yields a gate that can see overlay files but is
    never invoked when one changes — the shape where a control is present, runs,
    and still covers nothing. A PR touching only `values-tier2.yaml` is exactly
    the PR this check exists for.
    """
    wf = yaml.safe_load((WORKFLOWS_DIR / "image-ref-resolve.yaml").read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the boolean True (YAML 1.1).
    triggers = wf.get("on") or wf.get(True)
    paths = triggers["pull_request"]["paths"]

    # ⛔ Asserted BEHAVIOURALLY — the filter must actually match the overlay
    # files that exist, not merely be spelled in a way that looks right. A
    # syntactic check (`some entry starts with helm/ and contains values*`) is
    # satisfied by `helm/nonexistent/values*.yaml`, which matches nothing. That
    # is the same "present but covers nothing" shape this whole module is about.
    overlays = [p.relative_to(ROOT).as_posix()
                for p in sorted(ROOT.glob("helm/*/values-*.yaml"))]
    assert overlays, "no overlay values files found — this assertion would be vacuous"

    def _matches(pattern: str, target: str) -> bool:
        """GitHub Actions path-filter semantics: `**` spans separators, `*` does not.

        ⚠️ Deliberately STRICTER than the real engine (minimatch) in one respect:
        minimatch collapses `**` to zero directories, so `helm/**/values*.yaml`
        also matches `helm/values.yaml`; this does not. The direction is the safe
        one — it can only produce a false "not covered" (a nuisance failure),
        never a false "covered" (the dangerous one). Inert today: every overlay
        lives exactly one level under `helm/`.
        """
        rx = re.escape(pattern).replace(r"\*\*", "\x00").replace(r"\*", "[^/]*")
        return re.fullmatch(rx.replace("\x00", ".*"), target) is not None

    unmatched = [o for o in overlays if not any(_matches(p, o) for p in paths)]
    assert not unmatched, (
        f"image-ref-resolve.yaml does not trigger on these overlay files: {unmatched}\n"
        f"  filter paths: {paths}\n"
        "The checker globs `helm/*/values*.yaml`; a filter that misses an overlay "
        "means a PR changing only that file never runs it (#1302)."
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

    This is the assertion #1337 was missing. `test_report_expected_counts_match_
    matrix_sizes` below only checks the matrix against a literal it sits next to,
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
    """The report's hardcoded EXPECTED (7 / 15) must track the matrix sizes."""
    n_selfbuilt = len(_matrix_include("scan"))
    n_thirdparty = len(_matrix_include("scan-thirdparty"))
    run = _aggregate_run()

    m_sb = re.search(r'frags-sb.*?\s(\d+)\s+"self-built component"', run, re.S)
    m_tp = re.search(r'frags-tp.*?\s(\d+)\s+"third-party upstream image"', run, re.S)

    assert m_sb is not None, "could not find the self-built file_cve_report.sh EXPECTED arg"
    assert m_tp is not None, "could not find the third-party file_cve_report.sh EXPECTED arg"
    assert int(m_sb.group(1)) == n_selfbuilt, (
        f"report self-built EXPECTED={m_sb.group(1)} != {n_selfbuilt} scan matrix entries"
    )
    assert int(m_tp.group(1)) == n_thirdparty, (
        f"report third-party EXPECTED={m_tp.group(1)} != {n_thirdparty} scan-thirdparty entries"
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
    assert len(calls) == 2, f"expected 2 file_cve_report.sh calls, parsed {len(calls)}"
    for args in calls:
        assert len(args) >= 5, f"call site missing positional args: {args}"


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
