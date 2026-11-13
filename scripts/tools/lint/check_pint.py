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

Scope (two roots, kept in lockstep between ``_PINT_ARGS`` below and
``parser.include`` in ``.pint.hcl`` — pint only walks directories it is HANDED, so
widening the include alone is a no-op):

* ``rule-packs/rule-pack-*.yaml`` — every rule-pack source, the platform
  self-monitoring pack (``rule-pack-platform.yaml``, #1595 D1a) included: the
  ADR-025 guardian (``Watchdog`` + ``AlertmanagerWebhookNotificationsFailing``)
  is linted in its source form.
* ``tests/rulepacks/*.rules.yaml`` — the bare-rules promtool targets that are
  not rule packs.

No ``k8s/03-monitoring/configmap-rules-*.yaml``: every one is a ``DO NOT EDIT``
copy generated from the rule packs already linted here — the platform one too
since #1595 (it used to be hand-maintained and read through ``parser.relaxed``);
see ``.pint.hcl``.

Copies are sync-guarded by ``check_rulepack_sync.py``. Runs ``--offline`` (no
Prometheus). Baseline = 0 blocking.

The engine is REQUIRED with or without ``--ci`` (#3611, owner 2026-11-06):
pint missing, timed out, or exiting without its ``entries=N`` summary is no
verdict — exit 2, never a green run. ``--ci`` only decides whether findings
(pint's own, or the entries floor below) exit 1.

Usage:
    python3 scripts/tools/lint/check_pint.py [--ci]

Exit codes:
    0  no blocking findings (findings without --ci are printed, exit 0)
    1  blocking findings, or the entries floor tripped (only when --ci)
    2  no verdict: pint unavailable (no binary, no docker), timed out, or
       did not run (no ``entries=N`` in its output)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
import _lib_engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
# Keep in sync with the pint install step in .github/workflows/ci.yml.
# NB: the ghcr docker tag has NO `v` prefix (0.86.0); the GitHub *release* tag
# used by the binary curl in CI DOES (v0.86.0).
PINT_VERSION = "0.86.0"
# SHA-256 of the release asset `pint-<PINT_VERSION>-linux-amd64.tar.gz`
# (upstream checksums.txt). This line is the SSOT: ci.yml's `PINT_SHA256` must
# equal it (tests/shared/test_toolchain_pin_parity.py), and
# .claude/hooks/session-start.sh reads it at run time to install pint (#3611).
PINT_SHA256 = "6166233a8d6d726d56345cb863f749a20f398cc9ffc01cd5c36410a76afa73bc"
# Supply-chain: pin the Docker fallback by digest (multi-arch index of :0.86.0) so a
# re-pushed/tampered tag can't swap the image. On version bump, re-resolve with:
#   docker buildx imagetools inspect ghcr.io/cloudflare/pint:<ver>   (take the top-level Digest)
# Do NOT use `docker inspect` — on an arm64 host it can yield an arch-specific digest that
# breaks the amd64 CI fallback (manifest unknown / exec format error). #849 follow-up.
PINT_DIGEST = "sha256:93d01d7522b8d477c183d938b4263302c6ed980e187aa3af00b4bf810a9697cc"
PINT_IMAGE = f"ghcr.io/cloudflare/pint:{PINT_VERSION}@{PINT_DIGEST}"
# Scan the rule-pack sources (the platform pack included, #1595) and the bare-rules
# promtool targets under tests/rulepacks/; the parser.include in .pint.hcl narrows
# each dir to actual rule documents.
#
# ⚠️ This list and .pint.hcl's parser.include are TWO halves of one decision. pint
# only walks the paths it is handed on the CLI, so a path added to parser.include but
# not here is dead config: measured (when the platform rules were a ConfigMap read
# through parser.relaxed), adding only the ConfigMap include left entries unchanged
# with exit 0, and adding its directory here moved them. _MIN_PINT_ENTRIES below is
# what makes that half-edit fail loudly.
_PINT_ARGS = ["--offline", "-c", ".pint.hcl", "lint",
              "rule-packs/", "tests/rulepacks/"]

# Non-vacuity FLOOR on the rule entries pint actually parsed. `entries == 0` alone is
# too weak — every silent-green observed here reported a healthy-looking non-zero
# count with exit 0 (an include path missing from _PINT_ARGS; the platform rules
# renamed out from under the include). Both hollow out the platform half of the gate
# behind a green check. So this mirrors `_MIN_PLATFORM_ALERTS` in
# tests/ops/test_generate_routes_orchestration.py: ONE hand-maintained floor,
# deliberately not duplicated, that only ever needs RAISING.
#
# Sizing (#1595 D1a): 376 = the rules in rule-packs/rule-pack-*.yaml (347, the
# platform pack's 51 among them) + tests/rulepacks/**/*.rules.yaml (29). Counted the
# way pint counts — that same count over origin/main gave 412, exactly the
# `entries=412` CI's pint step logged on main before this change (296 in the tenant
# packs + 65 in tests/rulepacks, 36 of them in the 23 deleted platform extracts, +
# the platform ConfigMap's 51). The
# floor must sit:
#   * ABOVE 376 - 42 — losing the platform pack must trip it; and
#   * BELOW 376 — entries only ever GROW when rules are added, so a floor under
#     today's count means routine rule additions never touch this number.
# 368 leaves 8 entries of slack for small legitimate deletions. A deletion large
# enough to trip it (≥9 rules, e.g. retiring a whole rule-pack) SHOULD require a
# conscious edit here — #1204 D-del was one: 11 orphan recording rules deleted,
# CI's pint step then logged entries=367, so the pair below moved to 367 / 359. #3658
# added critical tiers (a record + an alert each); growth, not deletion, but once the
# slack exceeds the platform pack's share the floor no longer sees that pack drop out,
# so the pair below was re-measured from CI's pint step. #3783 removed two Kafka alerts
# (+ critical tier), their recording rules and thresholds: CI's pint step then logged
# entries=391, so the pair moved to 391 / 383. Re-measure on every PINT_VERSION bump: a pint that stops
# parsing a file is only visible as this count dropping.
_MIN_PINT_ENTRIES = 383

# The platform pack's share of the count, and the total measured at the same time.
# Both are needed: the pair is what keeps the floor's DETECTION WINDOW open as the
# repo grows, and the test asserts the floor sits inside (total - pack, total].
#
# ⛔ 42 is not a free-standing number — it is the platform alert FLOOR, already
# pinned as `_MIN_PLATFORM_ALERTS` in tests/ops/test_generate_routes_orchestration.py
# (the pack holds only `- alert:`, no `- record:`, so pint entries and alert count
# are the same quantity). Restating it here is a third hand-copy; the sibling test
# file's own convention is to read such values back out rather than repeat them, and
# the lockstep test does exactly that for this constant.
_PLATFORM_PACK_ENTRIES = 42
_PINT_ENTRIES_AT_MEASURE = 391


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pint Prometheus rule linter (Vibe wrapper)")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 on findings / a tripped entries floor")
    args = parser.parse_args()

    located = _lib_engine.locate("pint", docker_image=PINT_IMAGE)
    if isinstance(located, _lib_engine.NoVerdict):
        print(f"ERROR: {located.reason}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    print(f"pint via: {located.describe()} ({' '.join(_PINT_ARGS)})", file=sys.stderr)
    # generous: pint lint is seconds, but a first-run docker image pull is slow.
    # pint logs `entries=N` (how many rule entries it checked; coloured
    # unconditionally, so _lib_engine strips SGR before matching) whenever it
    # parsed its rule files; its absence is the run not having happened.
    result = _lib_engine.run(
        located, "lint", _PINT_ARGS, timeout=300,
        cwd=str(REPO_ROOT) if located.mode == "binary" else None,
        docker_run=["-v", f"{REPO_ROOT.as_posix()}:/work", "-w", "/work"],
        entrypoint="pint" if located.mode == "docker" else None)
    if isinstance(result, _lib_engine.NoVerdict):
        # ⛔ Do NOT name a single cause. The commonest are NOT a version
        # bump: the docker fallback failing to pull (the binary install in
        # ci.yml is continue-on-error), and .pint.hcl failing to parse.
        print(f"ERROR: {result.message()}\nCheck, in this order: pint's own "
              "output (its stderr tail above (a .pint.hcl parse error or a failed docker pull "
              "stops the run before any summary)); then whether a PINT_VERSION "
              "bump renamed the `entries=` field.", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Stream pint's own output through (captured only so we can inspect entries=).
    if result.proc.stdout:
        sys.stdout.write(result.proc.stdout)
    if result.proc.stderr:
        sys.stderr.write(result.proc.stderr)

    # Robustness guard: a rename / regex typo / path drift / pint parse-behaviour
    # change silently shrinks `entries` while pint still exits 0 — a green check
    # over a hollowed-out gate. Assert a FLOOR, not just non-emptiness (see
    # _MIN_PINT_ENTRIES for the sizing).
    entries, returncode = result.data
    if args.ci:
        if entries < _MIN_PINT_ENTRIES:
            print(f"FAIL: pint checked {entries} rule entries, below the "
                  f"{_MIN_PINT_ENTRIES} floor — the gate has been (partly) hollowed "
                  f"out behind a green check. Usual causes: a rule file renamed or "
                  f"moved out from under .pint.hcl's parser.include; a path present "
                  f"in parser.include but missing from _PINT_ARGS (pint only walks "
                  f"the dirs it is handed); or a pint upgrade that stopped "
                  f"parsing a rule file. "
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
                  f"{_PLATFORM_PACK_ENTRIES} the platform pack contributes. "
                  f"The floor can no longer detect that pack dropping out of "
                  f"scope, which is the main thing it is for. Ratchet "
                  f"_MIN_PINT_ENTRIES in {Path(__file__).name} up to "
                  f"{entries - 8} AND _PINT_ENTRIES_AT_MEASURE to {entries} "
                  f"(keeping the usual 8 for small legitimate deletions). Both, "
                  f"or the lockstep test in tests/lint/test_check_pint.py fails: "
                  f"it derives the useful window from that pair.",
                  file=sys.stderr)
            return EXIT_VIOLATION

    if returncode != 0 and args.ci:
        print("FAIL: pint reported blocking rule findings — see "
              "docs/internal/pint-lint-baseline.md for the policy + exemptions",
              file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
