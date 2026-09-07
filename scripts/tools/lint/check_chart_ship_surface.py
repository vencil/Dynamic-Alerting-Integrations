#!/usr/bin/env python3
"""check_chart_ship_surface.py — a shipping chart's root files must be declared SHIP or .helmignore'd (#1755).

Every file a packaged Helm chart hands to `helm package` must be declared: it
either reaches customers on purpose, or it is excluded by that chart's
`.helmignore`.

Why this exists
---------------
`helm package helm/tenant-api` runs on every tenant-api release and the `.tgz`
is pushed to `oci://<registry>/charts`, so whatever sits in the chart directory
reaches whoever pulls the chart. #1597 found — **by hand** — that the Container
SAST L2 lint variant `values-scope-enforce.yaml` was about to ship next to
`values.yaml`: a values file with all three RBAC scope axes pre-enforced,
sitting in the chart customers install. The fix was one `.helmignore` line.
Nothing mechanical would have caught it, and nothing would catch the next one.

The repo already asserts artifact CONTENTS — but only on the container-image
carrier (`check_image_pin_capability.py`: a pinned image must CONTAIN the
program the workload runs; `check_build_completeness.py`). The chart carrier
had zero such gate (#1755). This is that assertion moved to the chart carrier.

What it checks
--------------
1. **Which charts ship is derived, not enumerated.** Every `helm package <dir>`
   in `.github/workflows/*.yaml|yml` and in the `Makefile` (whose `VAR := value`
   assignments are resolved) marks `<dir>` as shipping. A fourth chart starting
   to ship is covered the day its release step lands, without editing this file
   — and the gate then demands declarations for it.
2. Every file at a shipping chart's **root** must appear in `DECLARED`, marked
   `SHIP` or `EXCLUDE`.
3. An `EXCLUDE` file must actually be matched by a line in that chart's
   `.helmignore`. A declaration the packer does not honour is worse than none:
   it reads like protection while shipping the file.
4. Symmetrically, a `SHIP` file must **not** be matched by any `.helmignore`
   line. Without this the table can claim a file reaches customers while
   `helm package` drops it — which is what `helm/threshold-exporter/README.md`
   did: declared SHIP as "chart usage doc, written for whoever pulls it", while
   line 12 of that chart's `.helmignore` is literally `README.md`, so nobody
   pulling the chart has ever received it. Verified against a real `.tgz`.
5. A declaration naming a file that no longer exists is also a violation, so
   the table cannot rot into a list of ghosts.
6. **Every `helm package` call site must be followed by the archive check**
   (`check_chart_package_contents.py`) before any `helm push` in the same file.
   That check reads the produced `.tgz` and needs helm, so it lives in release
   workflows rather than here — and a step that only lives in a workflow can be
   deleted, renamed or `|| true`'d without anything noticing. This assertion is
   what stops that wiring from rotting silently, and it needs no helm to run.
   ⚠️ Asserted by `check_package_verification_wiring()`, which `main()` runs
   alongside `check()`. Deliberately NOT folded into `check()`: that one is
   about a chart's DECLARED table, this one about `helm package` call sites, and
   a fixture for one is not a fixture for the other.

Scope boundary (deliberate — see #1755)
---------------------------------------
This asserts the chart **source tree** plus its `.helmignore`. It does **not**
read the bytes of the produced `.tgz`.

⚠️ The reason is NOT that helm is unobtainable. An earlier revision of this
docstring said so and was wrong: `get.helm.sh` is indeed blocked by egress
policy, but `go install helm.sh/helm/v3/cmd/helm@vX` builds it from source in
about two minutes, and CI already installs it (`azure/setup-helm@v4`, three
jobs in ci.yml and three in release.yaml). The real reason is placement: this
gate runs in pre-commit's *automatic* stage, where a 107 MB toolchain fetch
does not belong. A tarball-reading assertion is a separate check for the
manual stage or a CI job, and is tracked as such rather than smuggled in here.

What that costs is bounded and measured, not guessed. The model below was run
against real `helm package` output for all six `.helmignore` spellings that can
apply to a root file — bare basename, glob, `/glob`, `/literal`, trailing-slash
`mustDir`, non-matching — and agreed with helm 6/6. The two constructs it does
not model make helm fail loudly rather than diverge silently: a `!` negation
line ignores everything it does not match (so `helm package` aborts with
"Chart.yaml file is missing"), and `**` is rejected outright by helm's own
parser. So the residual risk lives in the SUBDIRECTORIES this gate declares out
of scope — `templates/`, `crds/`, `charts/` — not at the chart root.

`templates/`, `crds/` and `charts/` are out of scope: their contents are
deployment material by construction, while the incident class lives at the
chart root, next to `values.yaml`.

Usage
    python3 scripts/tools/lint/check_chart_ship_surface.py

Exit codes
    0  every shipping chart's root is declared and consistent
    1  violation
    2  caller error — could not RUN the check (missing chart dir, unreadable
       workflow or Makefile). Never a silent pass.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK,
    EXIT_VIOLATION,
    EXIT_CALLER_ERROR,
)
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

SHIP = "SHIP"
EXCLUDE = "EXCLUDE"

# Chart-root files and why each one is where it is. Keyed by the chart
# directory exactly as the `helm package` invocation spells it.
#
# SHIP    — reaching customers inside the .tgz is intended.
# EXCLUDE — must be named in that chart's .helmignore (checked, not trusted).
DECLARED: Dict[str, Dict[str, Tuple[str, str]]] = {
    "helm/threshold-exporter": {
        "Chart.yaml": (SHIP, "chart metadata — helm requires it"),
        "values.yaml": (SHIP, "the chart's default values"),
        "README.md": (
            EXCLUDE,
            "⚠️ NOT shipped, despite reading like chart documentation: line 12 "
            "of this chart's .helmignore is `README.md`, and a real "
            "`helm package` confirms the .tgz has never contained it. The "
            "declaration used to say SHIP — the first thing this gate's "
            "SHIP-side assertion caught. Left excluded on purpose: dropping "
            "that .helmignore line would change the contents of an already "
            "published artifact, which is a release decision, not a lint fix.",
        ),
        ".helmignore": (SHIP, "helm's own packing control file"),
    },
    "helm/recipe-preview": {
        "Chart.yaml": (SHIP, "chart metadata — helm requires it"),
        "values.yaml": (SHIP, "the chart's default values"),
    },
    "helm/tenant-api": {
        "Chart.yaml": (SHIP, "chart metadata — helm requires it"),
        "values.yaml": (SHIP, "the chart's default values"),
        ".helmignore": (SHIP, "helm's own packing control file"),
        "values-scope-enforce.yaml": (
            EXCLUDE,
            "#1597: Container SAST L2 lint variant with every RBAC scope axis "
            "pre-enforced — not a deployment profile. check_iac_helm.py reads "
            "it from the repo by path, so excluding it costs the lint nothing.",
        ),
    },
}

_HELM_PACKAGE_RE = re.compile(r"helm\s+package\s+(?P<target>[^\s]+)")
_HELM_PUSH_RE = re.compile(r"helm\s+push\b")
# A comment that merely mentions `helm package` is not a call site. Found by
# a fixture whose own comment read "# no helm package in this workflow" and
# was counted as one — the shape a prose-matching guard fails in.
_COMMENT_RE = re.compile(r"^[\s@\-]*#")

# The archive-contents gate (#1755). Named here as a string on purpose: this
# module must not import it — that one imports DECLARED from here.
ARCHIVE_CHECK = "check_chart_package_contents.py"

# Spellings that discard the verification step's exit code. Compared
# whitespace-stripped, so `|| true` and `||true` both match. Which of these
# actually applies to a given invocation is decided by `_neutering_hits`, not by
# their mere presence in the window.
_NEUTERING_INLINE = ("||true", "||:")
_SET_MINUS_E = "set+e"
_CONTINUE_ON_ERROR = "continue-on-error:true"
# Start of a YAML step, so `continue-on-error:` can be attributed to the step it
# actually belongs to rather than to anything that happens to sit in the window.
_STEP_START_RE = re.compile(r"^\s*-\s+(name|uses|run|id|with|env|if|shell):")
_MAKE_ASSIGN_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)\s*:?=\s*(?P<value>\S+)")


class CallerError(Exception):
    """The check could not be RUN — never reported as a pass."""


def _resolve_make_vars(makefile_text: str) -> Dict[str, str]:
    """Literal `VAR := value` assignments, enough to resolve `$(CHART_DIR)`.

    Deliberately does not evaluate `$(shell …)` or recursive expansion: an
    unresolvable target is reported as such rather than guessed at.
    """
    out: Dict[str, str] = {}
    for line in makefile_text.splitlines():
        m = _MAKE_ASSIGN_RE.match(line)
        if m and "$(" not in m.group("value"):
            out[m.group("name")] = m.group("value")
    return out


def _expand(target: str, make_vars: Dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        return make_vars.get(m.group(1), m.group(0))

    return re.sub(r"\$\((\w+)\)", repl, target)


def discover_shipping_charts(repo_root: Path = REPO_ROOT) -> Dict[str, List[str]]:
    """Chart dir -> the call sites that package it. Derived, not enumerated."""
    sites: Dict[str, List[str]] = {}

    makefile = repo_root / "Makefile"
    make_vars: Dict[str, str] = {}
    if makefile.is_file():
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CallerError(f"cannot read {makefile}: {exc}") from exc
        make_vars = _resolve_make_vars(text)
        for n, line in enumerate(text.splitlines(), 1):
            if _COMMENT_RE.match(line):
                continue
            m = _HELM_PACKAGE_RE.search(line)
            if m:
                target = _expand(m.group("target"), make_vars)
                if "$(" in target:
                    raise CallerError(
                        f"Makefile:{n} packages an unresolvable target "
                        f"{m.group('target')!r}; teach _resolve_make_vars about it "
                        f"rather than letting a shipping chart go unchecked"
                    )
                sites.setdefault(target.rstrip("/"), []).append(f"Makefile:{n}")

    wf_dir = repo_root / ".github" / "workflows"
    if wf_dir.is_dir():
        try:
            entries = sorted(wf_dir.iterdir())
        except OSError as exc:
            raise CallerError(f"cannot list {wf_dir}: {exc}") from exc
        for wf in entries:
            if wf.suffix not in {".yaml", ".yml"}:
                continue
            try:
                text = wf.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise CallerError(f"cannot read {wf}: {exc}") from exc
            for n, line in enumerate(text.splitlines(), 1):
                if _COMMENT_RE.match(line):
                    continue
                m = _HELM_PACKAGE_RE.search(line)
                if m:
                    target = m.group("target")
                    if "$" in target:
                        raise CallerError(
                            f"{wf.name}:{n} packages a templated target "
                            f"{target!r}; this gate cannot tell which chart ships"
                        )
                    sites.setdefault(target.rstrip("/"), []).append(
                        f".github/workflows/{wf.name}:{n}"
                    )

    return sites


def helmignore_patterns(chart_dir: Path) -> List[str]:
    """Non-comment, non-blank lines of a chart's .helmignore (may be empty)."""
    path = chart_dir / ".helmignore"
    if not path.is_file():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def _is_ignored(name: str, patterns: List[str]) -> bool:
    """True if a file named `name`, sitting at the chart ROOT, is excluded.

    Mirrors the part of helm's `parseRule` (helm/pkg/ignore/rules.go) that can
    apply to a root file — read from helm's source, not from memory:

    - trailing `/` sets `mustDir`, so the rule only ever matches directories
      and can never exclude a file;
    - leading `/` is root-relative: helm strips it and matches the remainder
      against the path relative to the chart root, which for a root file is
      just its name. Without this, `/values-*.yaml` — the spelling helm's own
      docs give for "root only" — would read as unmatched and the gate would
      report a violation for a file helm does exclude;
    - a pattern with no slash matches the basename at any depth;
    - `**` is rejected by helm itself (documented in helm/tenant-api/.helmignore).

    Negation (`!`) is deliberately not modelled. Per helm's `Ignore`, a
    negative rule never un-ignores a path an earlier rule already matched, and
    leaving one to be compared literally can only under-report an exclusion —
    the direction that makes this gate complain, never the direction that lets
    an undeclared file ship unnoticed.
    """
    return matching_pattern(name, patterns) is not None


def matching_pattern(name: str, patterns: List[str]) -> str | None:
    """The `.helmignore` line that excludes `name`, verbatim, or None.

    Same semantics as `_is_ignored`; it returns the line rather than a bool so
    a violation can name the rule the author has to look at, instead of making
    them scan the file for whichever pattern matched.
    """
    for raw in patterns:
        if raw.endswith("/"):
            continue  # directory-only rule (helm's mustDir); never a file
        rule = raw[1:] if raw.startswith("/") else raw
        if name == rule or fnmatch.fnmatch(name, rule):
            return raw
    return None


def _runner_files(repo_root: Path) -> List[Path]:
    """Files that may invoke `helm package`: the Makefile and every workflow."""
    files: List[Path] = []
    makefile = repo_root / "Makefile"
    if makefile.is_file():
        files.append(makefile)
    wf_dir = repo_root / ".github" / "workflows"
    if wf_dir.is_dir():
        try:
            files.extend(
                sorted(p for p in wf_dir.iterdir() if p.suffix in {".yaml", ".yml"})
            )
        except OSError as exc:
            raise CallerError(f"cannot list {wf_dir}: {exc}") from exc
    return files


def _neutering_hits(window: List[str], idx: int) -> List[Tuple[int, str]]:
    """Error handling that actually applies to the verifier at `window[idx]`.

    Scoped on purpose. An earlier revision flagged any `|| true` or `set +e`
    anywhere in the window, which made `rm -rf .build/tmp || true` next to a
    perfectly wired check read as sabotage — a guard that reports work nobody
    can do gets switched off, so a false positive here is not harmless.

    What still counts:
      - `|| true` / `||:` on the verifier's OWN line — that is how you discard
        a command's exit code;
      - `set +e` BEFORE it in the same block — after it the command has already
        run and been evaluated;
      - `continue-on-error: true` inside the verifier's own YAML step.
    """
    hits: List[Tuple[int, str]] = []
    squashed = window[idx].replace(" ", "")
    hits.extend((idx, m) for m in _NEUTERING_INLINE if m in squashed)
    hits.extend(
        (i, "set +e")
        for i in range(idx)
        if _SET_MINUS_E in window[i].replace(" ", "")
        and not _COMMENT_RE.match(window[i])
    )

    start = 0
    for i in range(idx, -1, -1):
        if _STEP_START_RE.match(window[i]):
            start = i
            break
    end = len(window)
    for i in range(idx + 1, len(window)):
        if _STEP_START_RE.match(window[i]):
            end = i
            break
    hits.extend(
        (i, "continue-on-error: true")
        for i in range(start, end)
        if _CONTINUE_ON_ERROR in window[i].replace(" ", "")
        and not _COMMENT_RE.match(window[i])
    )
    return sorted(hits)


def check_package_verification_wiring(repo_root: Path = REPO_ROOT) -> List[str]:
    """Every `helm package` must be verified before the archive is pushed.

    The window is "after this `helm package`, before the next `helm push` in the
    same file" — ordering matters: a check that runs after publication is not a
    gate, it is a post-mortem.
    """
    violations: List[str] = []
    for path in _runner_files(repo_root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise CallerError(f"cannot read {path}: {exc}") from exc
        rel = path.relative_to(repo_root)
        for n, line in enumerate(lines, 1):
            if _COMMENT_RE.match(line) or not _HELM_PACKAGE_RE.search(line):
                continue
            below = lines[n:]
            stop = next(
                (
                    i
                    for i, l in enumerate(below)
                    if _HELM_PUSH_RE.search(l) and not _COMMENT_RE.match(l)
                ),
                len(below),
            )
            window = below[:stop]
            if not any(
                ARCHIVE_CHECK in l and not _COMMENT_RE.match(l) for l in window
            ):
                where = (
                    f"before the `helm push` on line {n + stop + 1}"
                    if stop < len(below)
                    else "after it"
                )
                violations.append(
                    f"{rel}:{n} runs `helm package` but nothing invokes "
                    f"{ARCHIVE_CHECK} {where}. The archive would be published "
                    f"without anyone asserting what is inside it — the #1755 gap "
                    f"this pair of checks exists to close."
                )
                continue
            # Present but neutered reads exactly like protection while providing
            # none — the failure mode this whole assertion exists to prevent, so
            # it is not enough to see the invocation.
            idx = next(
                i
                for i, l in enumerate(window)
                if ARCHIVE_CHECK in l and not _COMMENT_RE.match(l)
            )
            for offset, neutered in _neutering_hits(window, idx):
                violations.append(
                    f"{rel}:{n + offset + 1} neutralises the {ARCHIVE_CHECK} "
                    f"step for the `helm package` on line {n} "
                    f"({neutered!r}): its exit code is discarded, so the step "
                    f"passes whatever the archive contains."
                )
    return violations


def check(repo_root: Path = REPO_ROOT) -> List[str]:
    """Return the list of violations. Empty list == pass."""
    violations: List[str] = []
    sites = discover_shipping_charts(repo_root)

    for chart, call_sites in sorted(sites.items()):
        chart_dir = repo_root / chart
        if not chart_dir.is_dir():
            raise CallerError(
                f"{call_sites[0]} packages {chart!r}, which is not a directory"
            )

        declared = DECLARED.get(chart)
        if declared is None:
            violations.append(
                f"{chart}: ships (packaged at {', '.join(call_sites)}) but has no "
                f"entry in DECLARED. Every file at its root must be declared "
                f"{SHIP} or {EXCLUDE} before the chart can reach customers."
            )
            continue

        present = {p.name for p in chart_dir.iterdir() if p.is_file()}
        patterns = helmignore_patterns(chart_dir)

        for name in sorted(present - set(declared)):
            violations.append(
                f"{chart}/{name}: undeclared file at the root of a chart that "
                f"ships ({', '.join(call_sites)}). Declare it {SHIP} with a "
                f"reason, or add it to .helmignore and declare it {EXCLUDE}."
            )

        for name, (disposition, _reason) in sorted(declared.items()):
            if name not in present:
                violations.append(
                    f"{chart}/{name}: declared {disposition} but no such file — "
                    f"drop the stale declaration."
                )
                continue
            hit = matching_pattern(name, patterns)
            if disposition == EXCLUDE and hit is None:
                violations.append(
                    f"{chart}/{name}: declared {EXCLUDE} but no .helmignore line "
                    f"matches it, so `helm package` still ships it. Add the "
                    f"pattern, or change the declaration to {SHIP}."
                )
            elif disposition == SHIP and hit is not None:
                violations.append(
                    f"{chart}/{name}: declared {SHIP}, but the .helmignore line "
                    f"{hit!r} matches it, so `helm package` drops it and nobody "
                    f"pulling the chart receives it. Either remove that line — "
                    f"which CHANGES what the published .tgz contains — or change "
                    f"the declaration to {EXCLUDE} with the real reason."
                )

    return violations


def main(argv: List[str] | None = None) -> int:
    # argparse with no flags — enforces the repo-wide CLI contract
    # (`--help` exits 0; unknown flags exit 2, argparse's default). The
    # description goes through i18n_text so `--help` responds to DA_LANG,
    # the bilingual contract of dev-rules §9 L3; `try_utf8_stdout()` first
    # because CJK in --help would raise UnicodeEncodeError on a legacy
    # Windows console (cp950/cp936) before argparse ever prints.
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "斷言每個會出貨的 Helm chart，其根目錄的每個檔案都已宣告："
            "要嘛刻意隨 .tgz 到客戶手上，要嘛被該 chart 的 .helmignore 排除。",
            "Assert that every file at the root of a shipping Helm chart is "
            "declared: it either reaches customers inside the .tgz on "
            "purpose, or that chart's .helmignore excludes it.",
        ),
    )
    parser.parse_args(argv)

    try:
        violations = check() + check_package_verification_wiring()
    except CallerError as exc:
        print(f"❌ chart ship-surface check could not run: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if violations:
        print("❌ chart ship surface (#1755):", file=sys.stderr)
        for v in violations:
            print(f"   - {v}", file=sys.stderr)
        print(
            "\n   A chart directory is the ship manifest: `helm package` archives "
            "what it finds\n   there, minus .helmignore. #1597 was caught by hand; "
            "this gate is why it\n   should not need to be caught by hand twice.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    charts = ", ".join(sorted(discover_shipping_charts()))
    print(f"✓ chart ship surface declared for: {charts}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
