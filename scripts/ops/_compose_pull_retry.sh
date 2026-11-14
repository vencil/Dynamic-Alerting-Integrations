#!/usr/bin/env bash
# _compose_pull_retry.sh — pull a compose stack's images, retrying.
#
# SOURCED, not executed (it defines one function). Used by
# federation_e2e_run.sh before each `docker compose … up`.
#
# Why: `Federation E2E (ADR-020 IV-2j)` is a required check, and with
# enforce_admins on, a red required check can only be re-run. Its one measured
# failure in the 300 runs before it became required was Docker Hub answering
# an image pull with `unauthorized: authentication required` — no scenario
# had run. `up` pulls implicitly and fails the same way, with nothing to tell
# a registry hiccup from a failing test. Pulling first, with retries, makes
# the two distinguishable and absorbs the transient case.
#
# Usage:
#   compose_pull_with_retry <stack label> [docker compose global args…]
#
# Behaviour:
#   - a pull succeeds        → returns 0; `up` then finds every image local.
#   - every attempt fails    → prints a message naming the IMAGE PULL (not a
#                              test) and returns 2: no verdict was reached
#                              (0 OK / 1 finding / 2 inconclusive).
#
# `--ignore-buildable` skips services that have a `build:`: they have no
# image to pull until `up --build` builds it, and without the flag a pull of
# their not-yet-existing tag is a failure that no retry fixes.
#
# Three attempts, waiting 5s then 15s. No knobs: tests/ops/
# test_compose_pull_retry.py runs this loop with `docker` and `sleep` stubbed
# on PATH.

compose_pull_with_retry() {
    local label="$1"
    shift
    local attempts=3
    local delay=5
    local attempt
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if docker compose "$@" pull --ignore-buildable; then
            return 0
        fi
        if ((attempt < attempts)); then
            echo "[compose-pull] ${label} stack: image pull failed (attempt ${attempt}/${attempts}); retrying in ${delay}s" >&2
            sleep "$delay"
            delay=$((delay * 3))
        fi
    done
    echo "[compose-pull] ${label} stack: IMAGE PULL FAILED after ${attempts} attempts." >&2
    echo "[compose-pull] This is a registry/network failure, NOT a test failure: no scenario ran. Re-run the job." >&2
    return 2
}
