package gitops

// Low-level git command runner for the GitOps writer. Split out of writer.go
// (Cycle 4 refactor) so the per-command timeout / kill-grace / stale-lock-sweep
// machinery (#630 / #638) lives apart from the higher-level write orchestration
// — no behavior change, pure intra-package move.
//
// Every git invocation here is bounded by a per-command deadline because the
// writer holds a single global mutex for the whole operation: a hung git child
// (most dangerously `git push` against a degraded forge) would otherwise pin the
// mutex and freeze EVERY tenant's writes. The deadline + WaitDelay + stale-lock
// sweep together guarantee a stuck git fails loudly and releases the lock.

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// defaultGitTimeout bounds a single git CLI invocation. Every write holds the
// writer mutex (w.mu) for the whole operation, so a hung git child (most
// dangerously `git push` against a degraded on-prem forge / network microcut)
// would otherwise hold the mutex indefinitely and freeze EVERY tenant's writes,
// not just one. A per-command deadline makes a stuck git fail loudly and release
// the lock. Override via TENANT_API_GIT_TIMEOUT (a Go duration, e.g. "90s").
const defaultGitTimeout = 60 * time.Second

// defaultGitKillGrace is cmd.WaitDelay: the grace the runtime allows for I/O
// cleanup AFTER the deadline kills the git child, before it force-closes the
// pipes and lets Wait return. This is NOT cosmetic — `git push` over HTTPS/SSH
// forks a `git-remote-https`/`ssh` helper that INHERITS git's stdout/stderr.
// CommandContext only SIGKILLs the direct git child on deadline; the helper can
// survive (blocked on a network read) holding the pipe's write-end open, which
// would otherwise make CombinedOutput's reader — and thus Wait — block long past
// the deadline and keep the writer mutex held (i.e. #630 would NOT be fixed).
// WaitDelay guarantees Wait returns shortly after the deadline regardless.
const defaultGitKillGrace = 5 * time.Second

// defaultGitFetchTimeout bounds the in-lock base fetch (TRK-318) that anchors
// each PR write on the freshest origin/<base> before branching. It is SEPARATE
// from (and far more aggressive than) defaultGitTimeout: the fetch runs inside
// the global writer mutex, so a degraded forge must surface as a fast 503 and
// release the lock in seconds — not hold every tenant's writes for the full 60s
// regular git timeout. Override via TA_GIT_FETCH_TIMEOUT (a Go duration).
const defaultGitFetchTimeout = 5 * time.Second

// gitTimeoutFromEnv reads TENANT_API_GIT_TIMEOUT as a Go duration, falling back
// to defaultGitTimeout when unset, unparseable, or non-positive.
func gitTimeoutFromEnv() time.Duration {
	v := os.Getenv("TENANT_API_GIT_TIMEOUT")
	if v == "" {
		return defaultGitTimeout
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		slog.Warn("gitops: invalid TENANT_API_GIT_TIMEOUT — using default",
			"value", v, "default", defaultGitTimeout, "error", err)
		return defaultGitTimeout
	}
	return d
}

// fetchTimeoutFromEnv reads TA_GIT_FETCH_TIMEOUT as a Go duration, falling back
// to defaultGitFetchTimeout when unset, unparseable, or non-positive. Kept
// DELIBERATELY independent of TENANT_API_GIT_TIMEOUT (TRK-318 / ADR-023 §B): the
// in-lock fetch needs an aggressive, separately-tuned deadline so a degraded
// forge fast-fails to 503 instead of pinning the global write lock for the much
// longer regular git timeout. The clamp keeps a fat-fingered "0"/"-5s" from
// disabling the fail-loud safety net (which would re-admit the stale-base hazard).
func fetchTimeoutFromEnv() time.Duration {
	v := os.Getenv("TA_GIT_FETCH_TIMEOUT")
	if v == "" {
		return defaultGitFetchTimeout
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		slog.Warn("gitops: invalid TA_GIT_FETCH_TIMEOUT — using default",
			"value", v, "default", defaultGitFetchTimeout, "error", err)
		return defaultGitFetchTimeout
	}
	return d
}

// gitCmd builds a git *exec.Cmd bounded by the writer's per-command timeout
// (#630). The caller MUST defer the returned cancel — it stops the deadline
// timer and kills the child if it's still running. The context is returned so
// callers can tell a deadline kill apart from an ordinary non-zero git exit.
func (w *Writer) gitCmd(args ...string) (*exec.Cmd, context.Context, context.CancelFunc) {
	return w.gitCmdWithTimeout(w.gitTimeout, args...)
}

// gitCmdWithTimeout is gitCmd with an explicit per-command deadline, so the
// TRK-318 in-lock fetch can use its own aggressive TA_GIT_FETCH_TIMEOUT instead
// of the regular git timeout. A non-positive timeout falls back to the regular
// default (the same clamp gitCmd relied on).
func (w *Writer) gitCmdWithTimeout(timeout time.Duration, args ...string) (*exec.Cmd, context.Context, context.CancelFunc) {
	if timeout <= 0 {
		timeout = defaultGitTimeout
	}
	bin := w.gitBinary
	if bin == "" {
		bin = "git"
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	cmd := exec.CommandContext(ctx, bin, args...)
	// Bound how long Wait blocks for pipe I/O after the deadline kill, so a
	// surviving remote-helper grandchild holding stdout can't keep the writer
	// mutex pinned past the deadline (see defaultGitKillGrace).
	cmd.WaitDelay = w.gitWaitDelay
	if cmd.WaitDelay <= 0 {
		cmd.WaitDelay = defaultGitKillGrace
	}
	return cmd, ctx, cancel
}

// gitErr renders a git failure. A deadline kill is reported LOUDLY and flags that
// the write lock is released as the caller returns (the whole point of #630), so
// a stuck push surfaces as a clear timeout instead of a silent global write freeze.
func (w *Writer) gitErr(ctx context.Context, op string, err error, out []byte) error {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		// #638: a SIGKILL'd git leaves its write-locks behind (no signal cleanup).
		// Sweep them now so the released mutex hands the next write a clean repo
		// instead of a permanent "index.lock: File exists" brick.
		w.clearStaleGitLocks()
		timeout := w.gitTimeout
		if timeout <= 0 {
			timeout = defaultGitTimeout
		}
		return fmt.Errorf("git %s timed out after %s — write lock released: %w — %s",
			op, timeout, context.DeadlineExceeded, string(out))
	}
	// Lock CONTENTION (a live competitor holds index.lock right now), NOT the
	// stale lock the timeout branch above sweeps: map to ErrWriteOverloaded so
	// the handler returns a retryable 503 (+ Retry-After) instead of a 500 that
	// would page as a server fault. The write plane is replicaCount=1 + an
	// in-process mutex, so this is rare; it surfaces only if an external git
	// process (an ops shell, a CronJob) races the writer on the same repo. A
	// transient "wait and retry" is the correct client contract — not an alarm.
	if isGitLockContention(out) {
		return fmt.Errorf("git %s: %w — %s", op, ErrWriteOverloaded, string(out))
	}
	return fmt.Errorf("git %s: %w — %s", op, err, string(out))
}

// isGitLockContention reports whether git's output is the "another process holds
// the index lock" failure (`fatal: Unable to create '…/index.lock': File
// exists.`). Matched on the stable substrings git emits rather than an errno so
// it is portable across git versions / locales-that-keep-the-English-template.
// Deliberately NARROW — only the index.lock-already-exists shape — so an
// unrelated git failure still maps to a 500 (a genuine server fault), not a
// misleading "retry later".
func isGitLockContention(out []byte) bool {
	s := strings.ToLower(string(out))
	if !strings.Contains(s, "index.lock") {
		return false
	}
	return strings.Contains(s, "file exists") || strings.Contains(s, "unable to create")
}

// clearStaleGitLocks best-effort removes git write-locks a deadline-killed git
// child leaves behind (#638): index.lock, packed-refs.lock, and any refs/**/*.lock
// (a killed commit can leave the branch ref lock, which nests arbitrarily deep).
//
// Safe to remove unconditionally ONLY because w.mu serializes all git access AND
// conf.d/gitDir is owned by this single tenant-api replica (no sidecar/cronjob
// touches it) — so when this runs, the just-killed op was the lone git process
// under the lock and no legitimate concurrent git holds these. Best-effort:
// failures are swallowed (the loud timeout error is returned regardless). No-op on
// bare repos / worktrees / a non-git gitDir (where .git is a file or absent).
func (w *Writer) clearStaleGitLocks() {
	gitMeta := filepath.Join(w.gitDir, ".git")
	if fi, err := os.Stat(gitMeta); err != nil || !fi.IsDir() {
		return
	}
	// HEAD.lock (a killed checkout/commit updating HEAD) would re-brick the very
	// next checkout; config.lock likewise blocks the -c-flagged commit.
	for _, name := range []string{"index.lock", "HEAD.lock", "packed-refs.lock", "config.lock"} {
		p := filepath.Join(gitMeta, name)
		if _, err := os.Stat(p); err == nil {
			if rmErr := os.Remove(p); rmErr == nil {
				slog.Warn("gitops: removed stale git lock after timeout (#638)", "path", p)
			}
		}
	}
	_ = filepath.WalkDir(filepath.Join(gitMeta, "refs"), func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // skip unreadable entries, keep walking
		}
		if !d.IsDir() && strings.HasSuffix(p, ".lock") {
			if rmErr := os.Remove(p); rmErr == nil {
				slog.Warn("gitops: removed stale ref lock after timeout (#638)", "path", p)
			}
		}
		return nil
	})
}

// gitExec runs a git command in the git directory, bounded by the write timeout.
func (w *Writer) gitExec(args ...string) error {
	fullArgs := append([]string{"-C", w.gitDir}, args...)
	cmd, ctx, cancel := w.gitCmd(fullArgs...)
	defer cancel()
	if out, err := cmd.CombinedOutput(); err != nil {
		return w.gitErr(ctx, args[0], err, out)
	}
	return nil
}
