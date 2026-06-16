#!/usr/bin/env python3
"""check_pint.py — Prometheus rule linting (pint engine + thin Vibe wrapper).

Hybrid lint policy (adopt the OSS engine, don't DIY): pint is the engine; this
wrapper only drives it with the repo's ``.pint.hcl``. It prefers a ``pint`` binary
on PATH and falls back to the pinned docker image — mirroring the kube-linter
L2/L4 wrappers (``check_iac_helm.py`` / ``check_k8s_manifests.py``).

Severity + exemptions live NATIVELY in ``.pint.hcl`` (the central, audited
registry), so this wrapper does NOT re-parse pint output — it just forwards
pint's exit code. That keeps the wrapper thin and avoids re-implementing what the
engine already does.

The high-ROI check is ``alerts/template``: it mechanically catches the
"aggregation strips a label the alert template uses → silent-forever alert" class
that has burned this repo repeatedly (today guarded only by hand-written comments).
The idiom-noisy default checks are disabled in ``.pint.hcl``; see that file and
docs/internal/pint-lint-baseline.md for the policy + the sentinel exemptions.

Scope: ``rule-packs/rule-pack-*.yaml`` (component rule-pack sources) + the platform
self-monitoring EXTRACTS ``tests/rulepacks/*.rules.yaml`` — which put the ADR-025
guardian (``Watchdog`` + ``AlertmanagerWebhookNotificationsFailing``) under the gate
(the deployed ``configmap-rules-platform.yaml`` is ConfigMap-wrapped → unparseable;
the extracts are its only pint-reachable form). Copies are sync-guarded by
``check_rulepack_sync.py``. Runs ``--offline`` (no Prometheus). Baseline = 0 blocking.

Usage:
    python3 scripts/tools/lint/check_pint.py [--ci]

Exit codes:
    0  no blocking findings (or pint unavailable in non-CI dev mode)
    1  blocking findings, or pint engine unavailable in --ci mode
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
# Keep in sync with the pint install step in .github/workflows/ci.yml.
# NB: the ghcr docker tag has NO `v` prefix (0.86.0); the GitHub *release* tag
# used by the binary curl in CI DOES (v0.86.0).
PINT_VERSION = "0.86.0"
# Supply-chain: pin the Docker fallback by digest (multi-arch index of :0.86.0) so a
# re-pushed/tampered tag can't swap the image. Re-resolve alongside PINT_VERSION via
# the registry manifest API (see scripts/ops/_verify_download.sh sibling pattern). #849 follow-up.
PINT_DIGEST = "sha256:93d01d7522b8d477c183d938b4263302c6ed980e187aa3af00b4bf810a9697cc"
PINT_IMAGE = f"ghcr.io/cloudflare/pint:{PINT_VERSION}@{PINT_DIGEST}"
# Scan the component rule-pack sources AND the platform extracts (tests/rulepacks/);
# the parser.include in .pint.hcl narrows each dir to actual rule documents.
_PINT_ARGS = ["--offline", "-c", ".pint.hcl", "lint", "rule-packs/", "tests/rulepacks/"]


def _build_cmd() -> list[str] | None:
    """Prefer a pint binary on PATH; else the pinned docker image; else None."""
    if shutil.which("pint"):
        return ["pint", *_PINT_ARGS]
    if shutil.which("docker"):
        return ["docker", "run", "--rm",
                "-v", f"{REPO_ROOT.as_posix()}:/work", "-w", "/work",
                "--entrypoint", "pint", PINT_IMAGE, *_PINT_ARGS]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pint Prometheus rule linter (Vibe wrapper)")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: non-zero exit on findings / missing engine")
    args = parser.parse_args()

    cmd = _build_cmd()
    if cmd is None:
        msg = "pint engine unavailable (no `pint` binary on PATH and no docker)"
        if args.ci:
            print(f"ERROR: {msg}", file=sys.stderr)
            return EXIT_VIOLATION
        print(f"WARN: {msg}; skipping (install pint or docker for local linting)",
              file=sys.stderr)
        return EXIT_OK

    print(f"pint via: {cmd[0]} (-c .pint.hcl --offline lint rule-packs/)", file=sys.stderr)
    try:
        # generous: pint lint is seconds, but a first-run docker image pull is slow.
        result = subprocess.run(cmd, cwd=REPO_ROOT, timeout=300,
                                capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print("ERROR: pint timed out after 300s", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    # Stream pint's own output through (captured only so we can inspect entries=).
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)

    # Robustness guard: pint logs `entries=N` for how many rules it checked. If the
    # parser.include matched nothing (a file rename / regex typo / path drift), pint
    # exits 0 with entries=0 — which would silently DISABLE the whole gate behind a
    # green check. Fail loud in --ci so drift can never hollow out the gate.
    m = re.search(r"entries=(\d+)", (result.stderr or "") + (result.stdout or ""))
    if args.ci and m is not None and int(m.group(1)) == 0:
        print("FAIL: pint checked 0 rule entries — .pint.hcl parser.include matched "
              "no files (gate silently disabled?). Failing loud instead of green.",
              file=sys.stderr)
        return EXIT_VIOLATION

    if result.returncode != 0 and args.ci:
        print("FAIL: pint reported blocking rule findings — see "
              "docs/internal/pint-lint-baseline.md for the policy + exemptions",
              file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
