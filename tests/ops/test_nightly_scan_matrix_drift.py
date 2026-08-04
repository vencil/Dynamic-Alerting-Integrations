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

    Read independently of the extractor on purpose: this is the invariant the
    extractor is supposed to satisfy, so deriving it from the extractor would be
    the check asking itself.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_extractor", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extractor"] = mod          # frozen dataclasses need this
    spec.loader.exec_module(mod)

    out: dict[str, set[str]] = {}
    for path in sorted(ROOT.glob("helm/*/values*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
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
    concrete = re.compile(
        r"image:\s*[\"']?([a-z0-9][\w.\-]*(?:/[\w.\-]+)*:[\w.\-]+"
        r"(?:@sha256:[0-9a-f]{64})?)[\"']?\s*$")
    matrix_refs = {e["ref"] for e in _matrix_include("scan-thirdparty")}
    selfbuilt_refs = {e.get("ref") for e in _matrix_include("scan")}

    offenders: dict[str, str] = {}
    templates = sorted(
        p for p in ROOT.glob("helm/*/templates/**/*")
        if p.suffix in (".yaml", ".yml"))
    assert templates, "no helm templates found — this scan would be a no-op"
    for path in templates:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = concrete.search(line.strip())
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith("ghcr.io/vencil/") or ref in selfbuilt_refs:
                continue  # first-party: the release pipeline owns its currency
            if ref not in matrix_refs:
                offenders[path.relative_to(ROOT).as_posix()] = ref
    assert not offenders, (
        "a third-party image ref is hardcoded in a Helm TEMPLATE and is not in "
        "the nightly scan matrix — the extractor cannot see templates (they are "
        "not parseable YAML), so nothing else will catch it:\n"
        + "\n".join(f"  {p}: {r}" for p, r in sorted(offenders.items()))
        + "\nEither add it to the matrix, or move the pin into values.yaml where "
          "the extractor samples it (#1302)."
    )


def test_the_extractor_samples_overlay_values_files() -> None:
    """The structural half of the guard above, so a narrowed glob fails with a
    message about the glob rather than as a mysterious missing ref."""
    src = EXTRACTOR.read_text(encoding="utf-8")
    assert '"helm/*/values*.yaml"' in src, (
        "check_image_refs_resolve.py stopped globbing `values*.yaml`. Overlay "
        "profiles (`-f values-tier2.yaml`) pin real deployed images; a "
        "`values.yaml`-only glob makes them invisible to the nightly scan and "
        "to this whole test module (#1302)."
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
        """GitHub Actions path-filter semantics: `**` spans separators, `*` does not."""
        rx = re.escape(pattern).replace(r"\*\*", "\x00").replace(r"\*", "[^/]*")
        return re.fullmatch(rx.replace("\x00", ".*"), target) is not None

    unmatched = [o for o in overlays if not any(_matches(p, o) for p in paths)]
    assert not unmatched, (
        f"image-ref-resolve.yaml does not trigger on these overlay files: {unmatched}\n"
        f"  filter paths: {paths}\n"
        "The checker globs `helm/*/values*.yaml`; a filter that misses an overlay "
        "means a PR changing only that file never runs it (#1302)."
    )


def test_report_expected_counts_match_matrix_sizes() -> None:
    """The report's hardcoded EXPECTED (5 / 15) must track the matrix sizes."""
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


def _report_calls() -> list[list[str]]:
    """Every `file_cve_report.sh` invocation in the report job, as argv lists.

    Reads the REAL call sites rather than a fixture: the outage was caused by
    the arguments the workflow actually passes, and a synthetic test would have
    stayed green through all 33 failures.
    """
    run = _aggregate_run()
    # Comment lines first — otherwise the prose ends up in the token stream once
    # backslash-continuations are folded together.
    body = "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))
    flat = re.sub(r"\\\n\s*", " ", body)
    calls = []
    for chunk in flat.split("bash scripts/ops/file_cve_report.sh")[1:]:
        chunk = chunk.split("|| rc=")[0].splitlines()[0]
        calls.append(shlex.split(chunk))
    return calls


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
