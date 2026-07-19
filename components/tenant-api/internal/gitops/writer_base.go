// PR-mode base-branch anchoring and in-lock origin fetch plumbing.
//
// These helpers were split out of writer.go (PR-2 Wave A) to keep writer.go
// focused on the commit-on-write core. Everything here is reached only from the
// PR-mode path (writer_pr.go WritePR / WritePRBatch): resolving/refreshing the
// LOCAL base branch and the in-lock `git fetch origin <base>` (TRK-318) that
// guards against branching a new feature branch from a STALE base. Behavior is
// unchanged from the original writer.go definitions; ErrForgeDegraded stays
// declared in writer.go and is visible here as same-package (gitops) state.
package gitops

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
)

// SetBaseBranch sets the PR-mode base branch the Writer branches from and returns
// to (#638). Wired from the TA_GIT_BASE_BRANCH flag in cmd/server. An empty value
// is ignored so the defaultBaseBranch fallback stands. Forge-neutral: this is the
// LOCAL git base, independent of the forge's PR target branch.
func (w *Writer) SetBaseBranch(b string) {
	if b != "" {
		w.baseBranch = b
	}
}

// base returns the configured base branch, or defaultBaseBranch when unset — so a
// zero-value Writer (e.g. constructed in tests via NewWriter) still has a base.
func (w *Writer) base() string {
	if w.baseBranch == "" {
		return defaultBaseBranch
	}
	return w.baseBranch
}

// checkoutBaseClean force-resets to a pristine base branch (#638). A plain
// `git checkout <base>` REFUSES when the working tree is dirty — which a write
// killed AFTER os.WriteFile but BEFORE its commit completes leaves behind (a
// modified-uncommitted tracked file). That would wedge EVERY subsequent PR write
// at the anchoring checkout — a global brick, and on a PVC-backed conf.d it
// survives pod restarts (a death-loop). So we first `reset --hard` (drop index +
// worktree changes) then `checkout -f` (switch even past a dirty tree). Safe to
// discard: in PR mode no uncommitted change here is ever legitimate — each write
// writes-then-commits on its own feature branch, returning to base when done.
func (w *Writer) checkoutBaseClean(base string) error {
	if err := w.gitExec("reset", "--hard", "HEAD"); err != nil {
		return fmt.Errorf("reset to clean state: %w", err)
	}
	if err := w.gitExec("checkout", "-f", base); err != nil {
		return fmt.Errorf("checkout base %q: %w", base, err)
	}
	return nil
}

// resolveFreshBaseRef fetches origin/<base> (in-lock, TRK-318) and returns the
// ref the new PR branch should be CUT FROM: "origin/<base>" once the fetch has
// populated it, else the local <base> as a best-effort fallback. The caller MUST
// already be on a clean base (via checkoutBaseClean) and MUST hold w.mu.
//
// WHY this exists: the local base ref is only synced at pod startup by the
// git-clone initContainer. A long-lived pod's base therefore STALLS after a
// remote merge — so a later PR branched from it silently rolls back a shared
// file (_groups.yaml / _views.yaml / _federation_policy.yaml) that another
// tenant already merged. Fetching here, inside the lock and immediately before
// branching, closes that window atomically (B1, not the TOCTOU-prone lock-outside
// B2 — ADR-023 §B).
//
// WHY return a ref instead of `reset --hard origin/<base>`: a hard reset would
// move the LOCAL base ref and revert the working tree — silently discarding any
// commit made directly on the local base. The special-file writes
// (WriteGroupsFile / WriteViewsFile / WriteFederationPolicyFile) commit straight
// to the current branch via commitFileChange even in PR mode, so they CAN leave
// such local-base commits. Branching the new feature branch from origin/<base>
// gets the same fresh anchor without touching the local base or its working tree.
//
// Failure semantics:
//   - No origin remote (dev/local, unit tests): nothing to be stale against, the
//     local base IS the source of truth → return the local base, no fetch.
//   - Fetch TIMEOUT: forge degradation → return ErrForgeDegraded so the caller
//     releases the lock and the handler returns 503. NEVER proceed on a stale base.
//   - Fetch non-timeout error (origin/<base> not pushed yet, transient remote
//     blip): the hazard TRK-318 guards is the never-fetches case; a one-off error
//     self-corrects on the next write, so warn and fall back to the local base
//     rather than bricking every write on a flaky remote.
func (w *Writer) resolveFreshBaseRef(base string) (string, error) {
	if !w.hasOriginRemote() {
		return base, nil
	}
	if err := w.fetchOriginBase(base); err != nil {
		return "", err // ErrForgeDegraded on timeout (only hard-fail path)
	}
	// Branch from the freshest remote-tracking ref when it exists; otherwise
	// (freshly-init'd bare remote with nothing pushed) fall back to local base.
	if w.originRefExists(base) {
		return "origin/" + base, nil
	}
	return base, nil
}

// fetchOriginBase runs `git fetch --prune origin <base>` bounded by the SEPARATE
// fetchTimeout (TRK-318). A deadline kill is the forge-degradation signal: sweep
// any locks the SIGKILL'd fetch left and return ErrForgeDegraded (fail loud). A
// non-deadline error is logged and swallowed (proceed on local base).
func (w *Writer) fetchOriginBase(base string) error {
	timeout := w.fetchTimeout
	if timeout <= 0 {
		timeout = defaultGitFetchTimeout
	}
	cmd, ctx, cancel := w.gitCmdWithTimeout(timeout, "-C", w.gitDir, "fetch", "--prune", "origin", base)
	defer cancel()
	out, err := cmd.CombinedOutput()
	if err == nil {
		return nil
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		// SIGKILL'd fetch leaves no signal cleanup; clear stale locks so the
		// released mutex hands the next write a clean repo (mirrors gitErr's #638
		// self-heal). Then fail loud — the caller turns this into a 503.
		w.clearStaleGitLocks()
		slog.Warn("gitops: base fetch timed out — write lock released (TRK-318)",
			"base", base, "timeout", timeout)
		return fmt.Errorf("git fetch origin %s timed out after %s: %w", base, timeout, ErrForgeDegraded)
	}
	slog.Warn("gitops: base fetch failed — proceeding on local base (TRK-318)",
		"base", base, "error", err, "output", strings.TrimSpace(string(out)))
	return nil
}

// hasOriginRemote reports whether an "origin" remote is configured. Used to skip
// the base fetch in dev/local/unit-test setups that have no forge.
func (w *Writer) hasOriginRemote() bool {
	cmd, _, cancel := w.gitCmd("-C", w.gitDir, "remote", "get-url", "origin")
	defer cancel()
	return cmd.Run() == nil
}

// originRefExists reports whether the remote-tracking ref refs/remotes/origin/<base>
// exists locally (i.e. a successful fetch/clone has populated it at least once).
func (w *Writer) originRefExists(base string) bool {
	cmd, _, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/"+base)
	defer cancel()
	return cmd.Run() == nil
}
