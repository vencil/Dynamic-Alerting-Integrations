#!/usr/bin/env bash
# _prepush_refs.sh — one implementation of "what is this push actually updating?"
#
# ⛔ Sourced, not executed. See EXTERNAL COMMANDS at the bottom for how.
#
# WHY THIS EXISTS (#1664)
#   A pre-push hook learns what is being pushed from git's stdin protocol —
#   one line per ref:
#       <local_ref> <local_sha> <remote_ref> <remote_sha>
#   That works when the hook IS .git/hooks/pre-push. It does NOT work when the
#   hook runs under pre-commit: pre-commit's own hook-impl reads the whole
#   stdin itself (`stdin = sys.stdin.buffer.read()` in
#   pre_commit/commands/hook_impl.py::_run_legacy) and then spawns every hook
#   with stdin=PIPE that it never writes to, so the hook sees EOF immediately.
#
#   Measured on pre-commit 4.6.0 — same repo, same commit, same push:
#       native .git/hooks/pre-push  -> hook stdin carries the refspec -> exits 1
#       installed via pre-commit    -> hook stdin EMPTY -> exits 0, and
#                                      pre-commit prints "Passed"
#   Both guards that read stdin were therefore inert while reporting success.
#
#   pre-commit does hand the same information over — as environment variables.
#   This helper reads whichever channel is actually carrying it, so the
#   decision lives in one place instead of once per guard.
#
# CHANNEL ORDER — stdin first, env second. That order is load-bearing:
#   * Under pre-commit, stdin is an already-closed pipe, so reading it costs
#     nothing and yields nothing; the env channel then answers.
#   * Invoked directly with a piped refspec (how the gate's own unit tests and
#     a native hook drive it), stdin answers and the env channel is never
#     consulted — so a stray PRE_COMMIT=1 inherited from an unrelated parent
#     process cannot change the verdict.
#   Checking the environment first would have made every stdin-fed caller
#   depend on PRE_COMMIT being absent, which is not a property anyone controls.
#
# OUTPUT — TWO shapes, ONE channel decision
#   prepush_refs        <remote_ref> <local_sha>
#   prepush_refs_full   <remote_ref> <local_sha> <remote_sha>
#
#   One walk produces both, so the channel decision has one implementation.
#
#   ⛔ Do not add a third column to `prepush_refs`. Its consumers parse with
#   `read -r remote_ref local_sha`, which folds any extra field into
#   `local_sha`, and one of them compares that against the 40-zero sha to skip
#   deletions — widening it brings #1691 back.
#
#   ⛔ In the three-column shape, unknown is the literal `-`, never blank. A
#   blank middle field is collapsed by default-IFS `read` and shifts every
#   later column left (the FIELD ORDER defect below, one position right).
#
#   ⛔ `-` for remote_sha does not mean "use PRE_COMMIT_FROM_REF instead": that
#   variable is `<first-ancestor>^` when the remote lacks the branch, which is
#   a different quantity in a documented slot. Consumers must read `-` as "base
#   unknown" and do the work rather than skip it.
#
#   ⛔ FIELD ORDER: remote_ref FIRST. That is not cosmetic. `local_sha` can
#   legitimately be empty — hook_impl._pre_push_ns has an `all_files=True`
#   path (pushing a branch whose first ancestor missing from the remote is the
#   ROOT commit, i.e. the first push to an empty remote) that returns a
#   namespace with `to_ref=None`, so pre-commit exports REMOTE_BRANCH without
#   TO_REF. With the sha first the row began with a blank field, default-IFS
#   `read` collapsed it, and `remote_ref` came out EMPTY — so both guards
#   dropped the row and allowed the push. Measured before the fix, single
#   refspec, real push: `Guard: block direct push to main ... Passed` and
#   `refs/heads/main:refs/heads/main [new branch]`. The verdict-bearing field
#   has to be the one that cannot be eaten.
#
# EXIT STATUS
#   0 — rows written to stdout. Zero rows is a legitimate answer: nothing is
#       being pushed (git only feeds lines for refs it is going to update).
#   3 — running under pre-commit with neither channel carrying a refspec.
#       Callers MUST treat this as "I cannot see what I am guarding" and exit
#       non-zero. Warning-and-allowing is not an option here: a PASSING hook's
#       stdout and stderr are both swallowed by pre-commit (measured — a hook
#       that wrote a marker to each and exited 0 produced zero visible bytes),
#       so a warning would be byte-for-byte the same picture as the bug this
#       file exists to remove.
#
# ⚠️ KNOWN RESIDUAL — the env channel carries ONE ref; a push can carry N.
#   hook_impl._pre_push_ns returns on the first pushable line it finds, so
#   under pre-commit a guard is shown one of N refs. Measured, with both
#   branches already present on the remote:
#       git push origin main aaa-first
#         native stdin   -> 2 rows (aaa-first, main)
#         pre-commit env -> PRE_COMMIT_REMOTE_BRANCH=refs/heads/aaa-first
#         result         -> main was updated by that same command
#   ⛔ Which ref survives is LEXICOGRAPHIC, not the order you typed — writing
#   `main` first does not protect it.
#   A guard built on this helper does not see that main. The other rows cannot
#   be recovered from inside the hook; only the stdin channel has full
#   fidelity. This is disclosure, not coverage — tests/ops/test_prepush_hook_wiring.py
#   pins the measurement so the gap cannot quietly change shape.
#
# ⛔ EXTERNAL COMMANDS — this file uses only bash builtins plus `cat`, and its
#   callers must source it with parameter expansion, NOT `$(dirname …)`. That
#   is a requirement, not a style choice: `test_gh_missing_*` in
#   tests/dx/test_preflight_pass_gate.py runs require_preflight_pass.sh with
#   PATH stripped to bash/git/basename/sh/cat, because "this gate still works
#   when `gh` is absent" is one of its contracts. Measured: a `dirname` in the
#   sourcing line fails there with `command not found` and takes the whole gate
#   down with it — on Linux only, so a Windows run reports the sourcing as fine.

_prepush_rows() {
    local _local_ref local_sha remote_ref remote_sha
    local _rows=()

    while read -r _local_ref local_sha remote_ref remote_sha; do
        [ -n "${remote_ref:-}" ] || continue
        _rows+=("$remote_ref ${local_sha:--} ${remote_sha:--}")
    done

    if [ "${#_rows[@]}" -gt 0 ]; then
        printf '%s\n' "${_rows[@]}"
        return 0
    fi

    if [ -n "${PRE_COMMIT_REMOTE_BRANCH:-}" ]; then
        printf '%s %s -\n' "${PRE_COMMIT_REMOTE_BRANCH}" "${PRE_COMMIT_TO_REF:--}"
        return 0
    fi

    if [ -n "${PRE_COMMIT:-}" ]; then
        return 3
    fi

    return 0
}

prepush_refs_full() {
    _prepush_rows
}

# ⛔ Second column is EMPTY (not `-`) when unknown — that is the shape its two
# consumers have always parsed. Emitting `-` here is a silent behaviour change.
prepush_refs() {
    local _rc=0 _out
    _out="$(_prepush_rows)" || _rc=$?
    [ "$_rc" -ne 0 ] && return "$_rc"
    [ -z "$_out" ] && return 0
    local remote_ref local_sha _remote_sha
    while read -r remote_ref local_sha _remote_sha; do
        [ -n "${remote_ref:-}" ] || continue
        [ "$local_sha" = "-" ] && local_sha=""
        printf '%s %s\n' "$remote_ref" "$local_sha"
    done <<< "$_out"
    return 0
}

prepush_refs_unavailable_message() {
    cat <<'PREPUSH_MSG'

[prepush] ⛔ This guard cannot see what is being pushed, so it is refusing.

It is running under pre-commit, but neither channel carried a refspec: git's
stdin was empty (pre-commit consumes it before the hook runs, #1664) and
PRE_COMMIT_REMOTE_BRANCH is unset.

⛔ SINCE #1689 THIS GUARD IS NOT SUPPOSED TO RUN UNDER pre-commit AT ALL.
The pre-push guards were moved out of .pre-commit-config.yaml because a hook
run by pre-commit is shown exactly ONE refspec, so a push carrying `feat/x`
and `main` together hid `main` from the guard whose entire job is to block it.
They are run by scripts/ops/prepush_dispatch.sh now, which reads git's stdin
itself and hands every guard the whole thing.

So reaching this message means one of:

  * a `stages: [pre-push]` entry for this guard was added back to
    .pre-commit-config.yaml — remove it; the copy pre-commit runs is the blind
    one, and it will not stop shadowing the dispatcher by being green;
  * you invoked it by hand under a pre-commit environment, in which case there
    is genuinely nothing to judge.

To exercise the guards for real, push something:

    git push --dry-run origin HEAD:refs/heads/<branch>

⛔ Measured: --dry-run runs the hooks and leaves the remote untouched, and an
up-to-date push runs them with ZERO rows — "nothing to push" and "cannot see
what is being pushed" are different answers and must stay that way.

⛔ Do not reach for --no-verify, and do not "fix" this by allowing the empty
case: that is exactly the defect #1664 removed. To ask whether the guards are
on the push path at all:

    make pr-preflight          (its `Local hooks` row answers exactly that)
    make pr-preflight-quick    (same answer, without the --all-files run: #1811
                                made --skip-hooks skip only that half)

PREPUSH_MSG
}
