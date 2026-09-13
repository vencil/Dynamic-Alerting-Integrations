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
# CALLER CHANNEL — the dispatcher has to say so, because nothing else can (#1846)
#   With zero rows on stdin and no PRE_COMMIT_REMOTE_BRANCH, two answers are
#   indistinguishable from inside this file: "git had nothing to feed" and
#   "pre-commit ate the refspec". An inherited PRE_COMMIT=1 was read as the
#   second, so the guards refused a push that had nothing to judge — while
#   every cause the refusal message lists was inapplicable, so following it
#   could not get that push out.
#
#   prepush_dispatch.sh therefore exports VIBE_PREPUSH_FROM_DISPATCH=1. Under
#   it, zero rows IS "nothing to push": the dispatcher is the only thing in
#   this repo that reads git's pre-push stdin, and when pre-commit owns
#   .git/hooks/pre-push it hands the legacy hook — the dispatcher — the WHOLE
#   stdin. Read on pre-commit 4.6.0 (the ci-constraints pin): hook_impl.py
#   `_run_legacy` does `sys.stdin.buffer.read()` for pre-push and passes it as
#   `input=`. So a dispatcher that read nothing was fed nothing.
#
#   ⛔ PRECEDENCE: this channel is consulted BEFORE the env one, and that order
#   is as load-bearing as the stdin-before-env order above. pre-commit does not
#   set PRE_COMMIT_REMOTE_BRANCH for the legacy path (it is exported in
#   commands/run.py, which hook_impl reaches only AFTER _run_legacy), so any
#   value visible under the dispatcher came from some outer process and names a
#   ref this push is not touching. Measured with the order reversed: an
#   up-to-date push carrying a stale refs/heads/main was blocked as a direct
#   push to main, while this same file's dispatcher had already concluded the
#   push carried no commits at all.
#
#   ⛔ This adds a manually settable variable, i.e. a new bypass surface. But
#   it is NOT the equal of GIT_PREFLIGHT_BYPASS, and saying so would invite
#   hardening against an attack surface that does not exist. Measured, real
#   installs and real pushes, with the variable exported by hand in the shell:
#       real commit -> main                     blocked (banner), both ways
#       feature branch with no marker           blocked, both ways
#       the same push with GIT_PREFLIGHT_BYPASS=1   ALLOWED
#   The sibling flag releases a push that would otherwise be stopped; this one
#   cannot, because a real push carries rows on stdin and rows are answered one
#   branch above.
#   ⛔ But its reach is "no rows ON STDIN", which is NOT the same as "nothing to
#   guard". It also covers invocations where the env channel would have supplied
#   a real refspec, and this branch discards that. Measured: with the variable
#   set, zero stdin rows and PRE_COMMIT_REMOTE_BRANCH=refs/heads/main, the guard
#   allows; unset, it blocks with the direct-push banner. Exported into the
#   ambient environment, nine cells of tests/ops/test_prepush_hook_wiring.py go
#   red (clean environment: all green), six of them because an env-channel push
#   at a protected branch was allowed.
#   That is safe TODAY for one mechanical reason only: the dispatcher is a CHILD
#   process of pre-commit's hook_impl, and a child's export cannot reach the
#   siblings its parent spawns afterwards (measured, with a control). ⛔ So never
#   export this variable from anywhere but the dispatcher — not a wrapper, not
#   the installer, not CI. The sentence that used to sit here said "there is
#   nothing there to guard", which would have licensed exactly that.
#
#   For the same reason this channel is SILENT, unlike the two sibling flags
#   which announce themselves: it is taken on every up-to-date push, which is
#   an ordinary event, not an override.
#
#   The two routes that could have made it matter are both closed:
#     * a guard run by pre-commit as a `stages: [pre-push]` hook during a real
#       push always has PRE_COMMIT_REMOTE_BRANCH, so it is answered above and
#       never reaches here. Not an assumption, but the citation is a
#       CONJUNCTION, not just _pre_push_ns: all three of its non-None returns
#       set remote_branch, local_branch, remote_name and remote_url, and
#       commands/run.py exports the variable only when all four are present.
#       A future pre-commit that changed either half would break this
#       paragraph with nothing in this repo watching, so re-read both.
#     * the dispatcher must not be that stanza either, and is not: half 2 of
#       test_the_shipped_wiring_runs_exactly_the_three_guards asserts
#       .pre-commit-config.yaml declares NO pre-push hook in ANY spelling.
#       That assertion is load-bearing for this paragraph, not housekeeping.
#   ⚠️ What is left is a person exporting the variable and then invoking a
#   guard by hand, e.g. `bash scripts/ops/protect_main_push.sh`. ⛔ Do not write
#   that `pre-commit run --hook-stage pre-push` is that route — not here and not
#   in the refusal message below: measured on this repo's own config, that
#   command runs ZERO hooks and exits 0, because there are no pre-push stanzas
#   for it to run. It reaches a guard only in a config that declares one, which
#   is what this file's own tests synthesise.
#   ⛔ Do NOT generalise this to "zero rows always passes" — that is the #1664
#   defect. "Nothing to push" and "cannot see what is being pushed" have to
#   stay two different answers; this only says who is allowed to give the first.
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
#   3 — running under pre-commit with neither channel carrying a refspec, AND
#       not invoked by the dispatcher (see CALLER CHANNEL — under it zero rows
#       is answered as 0, because there it means nothing is being pushed).
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

    # Zero rows with the dispatcher as the caller — see CALLER CHANNEL in the
    # header. There, and only there, zero rows means "nothing to push".
    # ⛔ ABOVE the env channel, not below it. The dispatcher reads git's stdin
    # in full, so a PRE_COMMIT_REMOTE_BRANCH still set under it was inherited
    # from an outer process and names a ref THIS push is not touching. Measured
    # with it below: zero rows plus a stale refs/heads/main produced a phantom
    # row and the guard blocked a push that was already up to date — the same
    # dead end as the one above, one door along.
    # ⛔ Exact value, not a presence test. Only the dispatcher writes this, and
    # it writes `1`; matching exactly sends every other inherited value — `0`,
    # `true`, someone's leftover export — back to the conservative branch below.
    # ⛔ Do not read this as "same family as the bypass flags": for those, unset
    # is the safe side, and here unset is the #1846 mis-refusal.
    if [ "${VIBE_PREPUSH_FROM_DISPATCH:-0}" = "1" ]; then
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

Common ways to reach this message:

  * you invoked a guard by hand (`bash scripts/ops/protect_main_push.sh`, say)
    while PRE_COMMIT was set in your environment, in which case there is
    genuinely nothing to judge;
  * something exported PRE_COMMIT before `git push`, and the guard was reached
    WITHOUT scripts/ops/prepush_dispatch.sh (a hand-wired .git/hooks/pre-push,
    for instance). See CALLER CHANNEL in scripts/ops/_prepush_refs.sh.

⛔ One cause is NOT on that list, deliberately: a `stages: [pre-push]` entry
re-added to .pre-commit-config.yaml. On a real push that copy is handed ONE
refspec, so it prints a verdict about that instead of this message — it looks
green while shadowing the dispatcher, and this text will never tell you.
Measured: it reaches this message only when pre-commit is run by hand.

To exercise the guards for real, push something:

    git push --dry-run origin HEAD:refs/heads/<branch>

⛔ Measured: --dry-run runs the hooks and leaves the remote untouched, and an
up-to-date push runs them with ZERO rows — "nothing to push" and "cannot see
what is being pushed" are different answers and must stay that way.

⛔ Do not reach for --no-verify, and do not "fix" this by allowing the empty
case: that is exactly the defect #1664 removed. To ask whether the guards are
on the push path at all:

    make pr-preflight          (its `Local hooks` row answers exactly that)
    make pr-preflight-quick    (same answer, without the --all-files run)

PREPUSH_MSG
}
