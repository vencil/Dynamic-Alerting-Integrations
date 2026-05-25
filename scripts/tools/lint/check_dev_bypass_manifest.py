#!/usr/bin/env python3
"""check_dev_bypass_manifest.py — ADR-022 Layer 4 (deploy-time guard).

Forbids the tenant-api dev-auth-bypass switch (``TA_DEV_BYPASS_AUTH`` /
``--dev-bypass-auth``) from EVER appearing in a deploy manifest. The switch is
a LOCAL-DEV-ONLY identity bypass (it injects a dev identity when no oauth2-proxy
header is present); if it reaches a deployment it disables the identity
requirement.

This is the deploy-time complement to the runtime poison pill (the binary
panics if ``--dev-bypass-auth`` is set inside a Kubernetes cluster). SAST
catches it earlier — in the PR that would introduce it — and also covers
NON-k8s manifests the runtime k8s-detection cannot see.

HARD block: there is no bypass tag. The switch must never be committed to a
manifest. (Doc/comment lines that merely *mention* it are allowed.)

Scope:   helm/**/*.ya?ml + k8s/**/*.ya?ml + operator-manifests/**/*.ya?ml
Forbid:  TA_DEV_BYPASS_AUTH | --dev-bypass-auth | dev-bypass-auth: (case-insensitive)

Usage:   python3 scripts/tools/lint/check_dev_bypass_manifest.py [--ci]
Exit:    0 clean | 1 findings (with --ci)

Lint class: (b) negative pattern (docs/internal/lint-policy.md). #448 / ADR-022.
A Vibe wrapper — no kube-linter/trivy rule exists for a project-specific
forbidden env var name.
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_DIRS = ("helm", "k8s", "operator-manifests")
FORBIDDEN = re.compile(r"(?i)(TA_DEV_BYPASS_AUTH|dev-bypass-auth)")


def find_violations(root: Path | None = None) -> list[tuple[str, int, str]]:
    """Return (relpath, line_no, line) for each forbidden occurrence.

    ``root`` defaults to the repo root; tests pass a temp dir.
    """
    base = root if root is not None else REPO_ROOT
    out: list[tuple[str, int, str]] = []
    for d in SCAN_DIRS:
        scan_root = base / d
        if not scan_root.is_dir():
            continue
        for p in sorted(scan_root.rglob("*.y*ml")):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                # A comment that merely mentions the switch (e.g. a "# never
                # set this here" warning) is allowed — only an actual setting
                # is a violation.
                if line.lstrip().startswith("#"):
                    continue
                if FORBIDDEN.search(line):
                    out.append((p.relative_to(base).as_posix(), i, line.strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--ci", action="store_true", help="exit 1 on findings")
    args = ap.parse_args()

    violations = find_violations()
    if not violations:
        print("OK no dev-auth-bypass switch in deploy manifests.")
        return 0

    print(
        "❌ ADR-022 Layer 4: tenant-api dev-auth-bypass switch found in deploy "
        "manifest(s)."
    )
    print(
        "   TA_DEV_BYPASS_AUTH / --dev-bypass-auth is LOCAL-DEV-ONLY and must NEVER "
        "be deployed (it disables the oauth2-proxy identity requirement)."
    )
    for rel, ln, content in violations:
        print(f"  {rel}:{ln}: {content}")
    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
