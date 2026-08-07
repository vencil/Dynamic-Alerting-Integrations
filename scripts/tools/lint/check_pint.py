#!/usr/bin/env python3
"""check_pint.py — Prometheus rule linting (pint engine + thin Vibe wrapper).

Hybrid lint policy (adopt the OSS engine, don't DIY): pint is the engine; this
wrapper only drives it with the repo's ``.pint.hcl``. It prefers a ``pint`` binary
on PATH and falls back to the pinned docker image — mirroring the kube-linter
L2/L4 wrappers (``check_iac_helm.py`` / ``check_k8s_manifests.py``).

Severity + exemptions live NATIVELY in ``.pint.hcl`` (the central, audited
registry), so this wrapper does NOT re-derive findings from pint output — it just
forwards pint's exit code. That keeps the wrapper thin and avoids re-implementing
what the engine already does. The ONE thing it does read back is the ``entries=N``
count, because that is the one failure mode pint's exit code cannot express: a gate
that checked far fewer rules than it should still exits 0 (see ``_MIN_PINT_ENTRIES``).

The high-ROI check is ``alerts/template``: it mechanically catches the
"aggregation strips a label the alert template uses → silent-forever alert" class
that has burned this repo repeatedly (today guarded only by hand-written comments).
The idiom-noisy default checks are disabled in ``.pint.hcl``; see that file and
docs/internal/pint-lint-baseline.md for the policy + the sentinel exemptions.

Scope (three roots, kept in lockstep between ``_PINT_ARGS`` below and
``parser.include`` in ``.pint.hcl`` — pint only walks directories it is HANDED, so
widening the include alone is a no-op):

* ``rule-packs/rule-pack-*.yaml`` — component rule-pack sources.
* ``tests/rulepacks/*.rules.yaml`` — the bare-rules EXTRACTS that put the ADR-025
  guardian (``Watchdog`` + ``AlertmanagerWebhookNotificationsFailing``) under the gate.
* ``k8s/03-monitoring/configmap-rules-platform*.y(a)ml`` — the DEPLOYED platform
  self-monitoring pack. It is ConfigMap-wrapped, which pint reads via
  ``parser.relaxed`` (supported since pint v0.19.0), so the old "ConfigMap-wrapped →
  unparseable, the extracts are its only pint-reachable form" note no longer holds.
  16 of its 42 alerts had no extract and were invisible to pint; 6 of those carry
  real ``alerts/template`` label-flow risk. Only the *platform* prefix is added —
  the other 16 ``configmap-rules-*.yaml`` are ``DO NOT EDIT`` copies generated from
  the rule-packs already linted here; see ``.pint.hcl`` for the measurements.

Copies are sync-guarded by ``check_rulepack_sync.py``. Runs ``--offline`` (no
Prometheus). Baseline = 0 blocking.

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
# re-pushed/tampered tag can't swap the image. On version bump, re-resolve with:
#   docker buildx imagetools inspect ghcr.io/cloudflare/pint:<ver>   (take the top-level Digest)
# Do NOT use `docker inspect` — on an arm64 host it can yield an arch-specific digest that
# breaks the amd64 CI fallback (manifest unknown / exec format error). #849 follow-up.
PINT_DIGEST = "sha256:93d01d7522b8d477c183d938b4263302c6ed980e187aa3af00b4bf810a9697cc"
PINT_IMAGE = f"ghcr.io/cloudflare/pint:{PINT_VERSION}@{PINT_DIGEST}"
# Scan the component rule-pack sources, the platform extracts (tests/rulepacks/) AND
# the deployed platform ConfigMap (k8s/03-monitoring/); the parser.include in
# .pint.hcl narrows each dir to actual rule documents — of the 41 files in
# k8s/03-monitoring/, pint's include list admits exactly 1 and rejects 40.
#
# ⚠️ This list and .pint.hcl's parser.include are TWO halves of one decision. pint
# only walks the paths it is handed on the CLI, so a path added to parser.include but
# not here is dead config: measured, adding only the ConfigMap include left entries at
# 351 with exit 0 (i.e. a silently unchanged gate), and adding k8s/03-monitoring/ here
# took it to 393. _MIN_PINT_ENTRIES below is what makes that half-edit fail loudly.
_PINT_ARGS = ["--offline", "-c", ".pint.hcl", "lint",
              "rule-packs/", "tests/rulepacks/", "k8s/03-monitoring/"]

# Non-vacuity FLOOR on the rule entries pint actually parsed. `entries == 0` alone is
# too weak — every silent-green observed here reported a healthy-looking non-zero
# count with exit 0:
#   (a) parser.include gained the platform ConfigMap but _PINT_ARGS did not gain
#       k8s/03-monitoring/  → entries=351 (should be 393), exit 0;
#   (b) configmap-rules-platform.yaml renamed / moved out from under the include
#       → entries=351 again, exit 0.
# Both hollow out the platform half of the gate behind a green check. So this mirrors
# `_MIN_PLATFORM_ALERTS` in tests/ops/test_generate_routes_orchestration.py: ONE
# hand-maintained floor, deliberately not duplicated, that only ever needs RAISING.
#
# Sizing (pint 0.86.0, measured 393 = 351 rule-packs + tests/rulepacks, + 42 from
# configmap-rules-platform.yaml). The floor must sit:
#   * ABOVE 351 — losing the whole platform pack is exactly the −42 drop this exists
#     to catch, so anything ≤351 would be blind to it; and
#   * BELOW 393 — entries only ever GROW when rules are added, so a floor under
#     today's count means routine rule additions never touch this number.
# 385 splits that window with ~34 entries of headroom over the 351 failure value and
# 8 entries of slack for small legitimate deletions. A deletion large enough to trip
# it (≥9 rules, e.g. retiring a whole rule-pack) SHOULD require a conscious edit here.
#
# This floor is also the only MECHANICAL detector for a pint version bump changing
# parse behaviour. `parser.relaxed` (ConfigMap-embedded rules) is the fragile part: if
# a future pint stops applying it, the platform pack quietly contributes 0 entries and
# nothing else in CI notices — every remaining rule still lints clean, so the build
# stays green while 42 alerts fall out of the gate. The count dropping to 351 is the
# only signal, and this is what turns it red. Re-measure on every PINT_VERSION bump.
_MIN_PINT_ENTRIES = 385

# What configmap-rules-platform.yaml contributes (pint 0.86.0: 393 total, 351
# without it) and the total measured at the same time. Both are needed: the pair
# is what keeps the floor's DETECTION WINDOW open as the repo grows, and the
# test asserts the floor sits inside (total - pack, total].
#
# ⛔ 42 is not a free-standing number — it is the platform alert count, already
# pinned as `_MIN_PLATFORM_ALERTS` in tests/ops/test_generate_routes_orchestration.py
# (the ConfigMap holds 42 `- alert:` and 0 `- record:`, so pint entries and alert
# count are the same quantity). Restating it here is a third hand-copy; the
# sibling test file's own convention is to read such values back out rather than
# repeat them, and the lockstep test does exactly that for this constant.
_PLATFORM_PACK_ENTRIES = 42
_PINT_ENTRIES_AT_MEASURE = 393

# pint colours its logs unconditionally — it does NOT check for a TTY — so under
# `capture_output=True` the entries line arrives as
# `\x1b[2mentries=\x1b[0m\x1b[94m393\x1b[0m`. A plain `entries=(\d+)` never matches
# that, which is how the previous `entries == 0` guard came to be dead code. Strip
# SGR sequences before matching; the raw (coloured) text is still what gets streamed
# to the caller.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_ENTRIES_RE = re.compile(r"entries=(\d+)")


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

    print(f"pint via: {cmd[0]} ({' '.join(_PINT_ARGS)})", file=sys.stderr)
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

    # Robustness guard: pint logs `entries=N` for how many rule entries it checked. A
    # rename / regex typo / path drift / pint parse-behaviour change silently shrinks
    # that number while pint still exits 0 — a green check over a hollowed-out gate.
    # Assert a FLOOR, not just non-emptiness (see _MIN_PINT_ENTRIES for the sizing).
    m = _ENTRIES_RE.search(
        _ANSI_SGR_RE.sub("", (result.stderr or "") + (result.stdout or "")))
    if args.ci:
        if m is None:
            # Not "can't check, carry on" — an unreadable count means the floor below
            # is unenforceable, which is the same silent-green it exists to prevent.
            # ⛔ Do NOT name a single cause here. This branch is reached by any
            # failure that stops pint printing a summary, and the two commonest
            # are NOT a version bump: the docker fallback failing to pull (the
            # binary install in ci.yml is continue-on-error), and .pint.hcl
            # itself failing to parse. Asserting "log format changed" sends the
            # reader to re-measure a floor when the real fix is a pull or a
            # syntax error a few lines up.
            print("FAIL: could not read `entries=N` from pint's output, so the "
                  "non-vacuity floor cannot be evaluated. Failing loud instead "
                  "of skipping it. Check, in this order: pint's own output above "
                  "(a .pint.hcl parse error or a failed docker pull prints there "
                  "and stops the run before any summary); then whether a "
                  "PINT_VERSION bump renamed the `entries=` field.",
                  file=sys.stderr)
            return EXIT_VIOLATION
        entries = int(m.group(1))
        if entries < _MIN_PINT_ENTRIES:
            print(f"FAIL: pint checked {entries} rule entries, below the "
                  f"{_MIN_PINT_ENTRIES} floor — the gate has been (partly) hollowed "
                  f"out behind a green check. Usual causes: a rule file renamed or "
                  f"moved out from under .pint.hcl's parser.include; a path present "
                  f"in parser.include but missing from _PINT_ARGS (pint only walks "
                  f"the dirs it is handed); or a pint upgrade that changed how "
                  f"parser.relaxed parses the ConfigMap-embedded platform rules. "
                  f"If rules were legitimately REMOVED, lower _MIN_PINT_ENTRIES in "
                  f"{Path(__file__).name} deliberately.", file=sys.stderr)
            return EXIT_VIOLATION

        # ⛔ Keep the DETECTION WINDOW open. A fixed floor decays: the drop it
        # exists to catch is −_PLATFORM_PACK_ENTRIES, so once the repo grows
        # past floor + that, the pack can fall out entirely and the total still
        # clears the floor. The guard would read green forever after, with no
        # edit ever having been made to it — failure by growth, not by change.
        # Requiring the slack to stay under one pack's worth makes the floor
        # self-maintaining: routine additions are free until they would blind
        # it, and then CI names the number to move it to.
        slack = entries - _MIN_PINT_ENTRIES
        # `>=`, not `>`: the floor can only see the pack vanish while
        # `entries - pack < floor`, i.e. while slack is STRICTLY under one
        # pack's worth. At slack == pack the drop lands exactly ON the floor and
        # `<` is false, so it passes — the one value the guard was written for.
        if slack >= _PLATFORM_PACK_ENTRIES:
            print(f"FAIL: pint checked {entries} entries against a "
                  f"{_MIN_PINT_ENTRIES} floor — {slack} of slack, more than the "
                  f"{_PLATFORM_PACK_ENTRIES} the platform ConfigMap contributes. "
                  f"The floor can no longer detect that pack dropping out of "
                  f"scope, which is the main thing it is for. Ratchet "
                  f"_MIN_PINT_ENTRIES in {Path(__file__).name} up to "
                  f"{entries - 8} AND _PINT_ENTRIES_AT_MEASURE to {entries} "
                  f"(keeping the usual 8 for small legitimate deletions). Both, "
                  f"or the lockstep test in tests/lint/test_check_pint.py fails: "
                  f"it derives the useful window from that pair.",
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
