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
4. A declaration naming a file that no longer exists is also a violation, so
   the table cannot rot into a list of ghosts.

Scope boundary (deliberate — see #1755)
---------------------------------------
This asserts the chart **source tree** plus its `.helmignore`. It does **not**
read the bytes of the produced `.tgz`: that needs `helm`, which is absent here
(`get.helm.sh` is blocked by egress policy and helm publishes no GitHub release
asset), and a gate whose main path its author cannot run is the exact shape
PR #1747 was closed for. `templates/`, `crds/` and `charts/` are out of scope
too: their contents are deployment material by construction, while the incident
class lives at the chart root, next to `values.yaml`.

Usage
    python3 scripts/tools/lint/check_chart_ship_surface.py

Exit codes
    0  every shipping chart's root is declared and consistent
    1  violation
    2  caller error — could not RUN the check (missing chart dir, unreadable
       workflow). Never a silent pass.
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
        "README.md": (SHIP, "chart usage doc, written for whoever pulls it"),
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
        text = makefile.read_text(encoding="utf-8", errors="replace")
        make_vars = _resolve_make_vars(text)
        for n, line in enumerate(text.splitlines(), 1):
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
        for wf in sorted(wf_dir.iterdir()):
            if wf.suffix not in {".yaml", ".yml"}:
                continue
            try:
                text = wf.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover — unreadable workflow
                raise CallerError(f"cannot read {wf}: {exc}") from exc
            for n, line in enumerate(text.splitlines(), 1):
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
    # helm matches a bare basename at any depth; `**` is unsupported upstream
    # (documented in helm/tenant-api/.helmignore). fnmatch covers both the
    # literal and the glob spellings without re-implementing helm's walker.
    return any(name == p or fnmatch.fnmatch(name, p) for p in patterns)


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
            if disposition == EXCLUDE and not _is_ignored(name, patterns):
                violations.append(
                    f"{chart}/{name}: declared {EXCLUDE} but no .helmignore line "
                    f"matches it, so `helm package` still ships it. Add the "
                    f"pattern, or change the declaration to {SHIP}."
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
        violations = check()
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
