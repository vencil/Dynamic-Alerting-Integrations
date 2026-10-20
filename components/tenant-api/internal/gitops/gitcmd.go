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

// SetGitTimeout overrides, for this Writer only, the per-command git
// deadline NewWriter read from TENANT_API_GIT_TIMEOUT; a non-positive d is
// ignored. Set it before the Writer serves. Tests outside this package use
// it to make a git command time out (#3173) without changing the
// process-wide environment.
func (w *Writer) SetGitTimeout(d time.Duration) {
	if d > 0 {
		w.gitTimeout = d
	}
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
	cmd.Env = gitEnv(literalPathspecs(args))
	// Bound how long Wait blocks for pipe I/O after the deadline kill, so a
	// surviving remote-helper grandchild holding stdout can't keep the writer
	// mutex pinned past the deadline (see defaultGitKillGrace).
	cmd.WaitDelay = w.gitWaitDelay
	if cmd.WaitDelay <= 0 {
		cmd.WaitDelay = defaultGitKillGrace
	}
	return cmd, ctx, cancel
}

// pathspecEnv are git's global pathspec settings. gitEnv drops every one
// of them that tenant-api inherited before it decides what git gets.
var pathspecEnv = []string{
	"GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_ICASE_PATHSPECS", "GIT_NOGLOB_PATHSPECS",
}

// gitEnv is the environment a git child runs with: the process's own
// without any inherited pathspecEnv, plus GIT_LITERAL_PATHSPECS=1 when
// literal is set (#3114). Every path tenant-api hands git is a name —
// conf.d's path, as the operator configured it, and file names in it —
// never a pattern; a `[`, `*` or `?` in one would otherwise glob onto other
// tracked files, and `add` / `reset` / `log` would touch or read those too.
//
// literal is decided per command by literalPathspecs, NOT set on every
// command: git hands its own environment to the hooks it runs, so a hook in
// the conf repo — a pre-commit that runs `git diff --cached -- '*.yaml'` to
// lint what is staged — would read its own patterns literally, match
// nothing and pass. A command that runs a hook able to affect its result
// therefore gets no pathspec setting at all (its hooks see what they see
// when an operator runs git by hand), and the paths it takes are made
// literal one by one with literalPathspec. The one hook that does see the
// literal setting is post-index-change (after add / reset -- paths), which
// cannot change the result; see literalPathspecs.
//
// The inherited settings are dropped on every command: GIT_GLOB_PATHSPECS
// and GIT_ICASE_PATHSPECS make git refuse to run beside the literal setting
// ("incompatible with all other global pathspec settings"), and on a
// command without it any of the four would decide for tenant-api's paths
// and its hooks' patterns what the operator never configured.
//
// Callers that add variables append to cmd.Env, not os.Environ().
func gitEnv(literal bool) []string {
	env := make([]string, 0, len(os.Environ())+1)
	for _, kv := range os.Environ() {
		if isPathspecEnv(kv) {
			continue
		}
		env = append(env, kv)
	}
	if literal {
		env = append(env, "GIT_LITERAL_PATHSPECS=1")
	}
	return env
}

func isPathspecEnv(kv string) bool {
	for _, k := range pathspecEnv {
		if strings.HasPrefix(kv, k+"=") {
			return true
		}
	}
	return false
}

// literalPathspecs reports whether a git command (args as gitCmd gets
// them, global options such as `-C dir` first) gets GIT_LITERAL_PATHSPECS:
// only a command that takes pathspecs AND runs no hook that can change its
// outcome. The list is closed on purpose — a subcommand added later gets no
// setting until someone has checked which hooks it runs; until then, a path
// it takes goes through literalPathspec.
//
//   - diff, log, ls-files, ls-tree: run no hook at all.
//   - add, and reset with `--`: run one hook, post-index-change, and that
//     hook DOES see GIT_LITERAL_PATHSPECS=1 — the one known exception,
//     accepted because it runs after the index is written and its exit
//     status is ignored, so nothing it does can change the command's result
//     (pinned by TestGitHooks_PostIndexChangeSeesLiteral). The path form of
//     reset only rewrites index entries; it moves no ref, so no
//     reference-transaction hook. A reset without `--` (`reset -q`,
//     `reset --hard HEAD`) updates HEAD / ORIG_HEAD and runs that hook, so
//     it is left out.
//
// Not here, because they run hooks: commit (pre-commit, commit-msg,
// post-commit, reference-transaction), checkout (post-checkout — a file
// checkout too), push (pre-push), fetch, update-ref, branch, reset without
// paths (reference-transaction), worktree add (post-checkout). Commands that
// take no pathspec (rev-parse, cat-file, show, commit-tree, write-tree,
// merge-base, status, …) gain nothing from the setting and are left out too.
//
// What the setting still reaches inside an allowed command is a program the
// conf repo configures for it — a clean filter on `add`, a textconv driver on
// `log -p`, a core.fsmonitor hook — none of which tenant-api's repos use.
func literalPathspecs(args []string) bool {
	sub, rest := gitSubcommand(args)
	switch sub {
	case "add", "diff", "log", "ls-files", "ls-tree":
		return true
	case "reset":
		for _, a := range rest {
			switch a {
			case "--soft", "--mixed", "--hard", "--merge", "--keep":
				return false
			}
		}
		for _, a := range rest {
			if a == "--" {
				return true
			}
		}
	}
	return false
}

// gitGlobalOptsWithValue are git's global options that take their value as
// the next argument (`--git-dir X`); the `--git-dir=X` form is one argument.
var gitGlobalOptsWithValue = map[string]bool{
	"-C": true, "-c": true,
	"--git-dir": true, "--work-tree": true, "--namespace": true,
	"--exec-path": true, "--config-env": true, "--super-prefix": true,
}

// gitGlobalFlags are git's global options that take no value.
var gitGlobalFlags = map[string]bool{
	"-p": true, "-P": true, "--paginate": true, "--no-pager": true,
	"--bare": true, "--no-replace-objects": true, "--no-optional-locks": true,
	"--no-lazy-fetch": true, "--no-advice": true,
	"--literal-pathspecs": true, "--glob-pathspecs": true,
	"--noglob-pathspecs": true, "--icase-pathspecs": true,
}

// gitSubcommand splits args at git's subcommand: global options come
// before it, and those in gitGlobalOptsWithValue take the next argument as
// their value. An option it does not know — a flag or one that takes a
// value, it cannot tell which — makes it return "": where the subcommand is
// is then unknown, and "" gets no literal setting (the safe direction: the
// command's hooks see what an operator's would).
func gitSubcommand(args []string) (sub string, rest []string) {
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case gitGlobalOptsWithValue[a]:
			i++
		case gitGlobalFlags[a]:
		case strings.HasPrefix(a, "--") && strings.Contains(a, "="):
			name, _, _ := strings.Cut(a, "=")
			if !gitGlobalOptsWithValue[name] {
				return "", nil
			}
		case strings.HasPrefix(a, "-"):
			return "", nil
		default:
			return a, args[i+1:]
		}
	}
	return "", nil
}

// literalPathspec makes p a literal pathspec on its own (`:(literal)p`), for
// a command that runs hooks and so gets no GIT_LITERAL_PATHSPECS (see
// literalPathspecs). p keeps its meaning otherwise: a relative p is relative
// to the directory git runs in (-C), an absolute one must be inside the
// worktree.
func literalPathspec(p string) string {
	return ":(literal)" + p
}

// gitErr renders a git failure. A deadline kill is reported LOUDLY and flags that
// the write lock is released as the caller returns (the whole point of #630), so
// a stuck push surfaces as a clear timeout instead of a silent global write freeze.
// It wraps ErrGitTimeout, not context.DeadlineExceeded (#3173): the deadline is
// the command's own, and whether the write it was part of reached the branch is
// the writer's to decide (write_outcome.go), not the caller's to guess.
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
			op, timeout, ErrGitTimeout, string(out))
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

// gitReadErr renders the failure of a read-only git command (gitOutput). It
// is gitErr without the #638 sweep: a reader running with
// --no-optional-locks takes no lock, so a lock present when it times out was
// taken by someone else — a concurrent write, an operator's shell — and is
// not this reader's to delete. Nor does a reader hold the write lock, so the
// message does not claim to release one. A timeout wraps ErrGitTimeout, as
// gitErr's does.
func (w *Writer) gitReadErr(ctx context.Context, op string, err error, out []byte) error {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		timeout := w.gitTimeout
		if timeout <= 0 {
			timeout = defaultGitTimeout
		}
		return fmt.Errorf("git %s (read-only) timed out after %s: %w — %s",
			op, timeout, ErrGitTimeout, string(out))
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

// gitCheck runs a git command and reports only whether it succeeded.
// Used for existence-style probes where output and error detail are irrelevant.
func (w *Writer) gitCheck(args ...string) bool {
	fullArgs := append([]string{"-C", w.gitDir}, args...)
	cmd, _, cancel := w.gitCmd(fullArgs...)
	defer cancel()
	return cmd.Run() == nil
}
