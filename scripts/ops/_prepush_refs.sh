#!/usr/bin/env bash
# _prepush_refs.sh — one implementation of "what is this push actually updating?"
#
# ⛔ Sourced, not executed. See EXTERNAL COMMANDS at the bottom for how.
#
# WHAT IT READS — git's pre-push stdin, one line per ref it is going to update:
#       <local_ref> <local_sha> <remote_ref> <remote_sha>
#   prepush_dispatch.sh reads that once and hands each guard its own copy.
#   Zero rows means nothing is being pushed: git feeds no line for a ref that
#   is already up to date.
#
#   ⛔ stdin is the ONLY channel. A guard that pre-commit runs as a pre-push
#   stage hook gets an empty stdin (pre-commit reads it first, #1664), so it
#   judges nothing and passes. That is why the guards are not pre-commit hooks
#   (#1689): .pre-commit-config.yaml declares none, pinned by
#   test_the_shipped_wiring_runs_exactly_the_three_guards.
#
# OUTPUT
#   prepush_refs   <remote_ref> <local_sha>
#
#   ⛔ Do not add a third column. Consumers parse with
#   `read -r remote_ref local_sha`, which folds any extra field into
#   `local_sha`, and the ones that skip deletions compare that against the
#   40-zero sha — widening it brings #1691 back.
#
# ⛔ EXTERNAL COMMANDS — this file uses only bash builtins, and its callers
#   must source it with parameter expansion, NOT `$(dirname …)`. That is a
#   requirement, not a style choice: `test_gh_missing_*` in
#   tests/dx/test_preflight_pass_gate.py runs require_preflight_pass.sh with
#   PATH stripped to bash/git/basename/sh/cat, because "this gate still works
#   when `gh` is absent" is one of its contracts. A `dirname` in the
#   sourcing line fails there with `command not found` and takes the whole gate
#   down with it — and only where those tests run, which is not Windows.

prepush_refs() {
    local _local_ref local_sha remote_ref _remote_sha

    while read -r _local_ref local_sha remote_ref _remote_sha; do
        [ -n "${remote_ref:-}" ] || continue
        printf '%s %s\n' "$remote_ref" "$local_sha"
    done
}
