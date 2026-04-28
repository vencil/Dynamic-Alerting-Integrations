package batchpr

// PR-2 — production GitClient implementation: shell out to `git`.
//
// Why shell-out (vs go-git or libgit2):
//
//   - `git` is universally available in customer migration
//     environments. go-git is a substantial dep that brings its
//     own quirks (e.g. handling of `.gitignore`, `.gitattributes`).
//
//   - This impl mirrors the existing `tenant-api/internal/gitops/
//     writer.go` pattern (which also shells out to `git`) so the
//     two PR-flow toolkits stay consistent in behaviour.
//
//   - The interface boundary (GitClient) decouples this choice
//     from Apply() orchestration; a customer who wants go-git can
//     supply their own impl without touching the apply layer.
//
// Limitations callers should know:
//
//   - The impl assumes the CWD is inside the target repo. Apply()
//     callers (CLI / UI) must `cd` to the repo before invoking.
//   - `git` must already be authenticated to push (SSH agent /
//     credential helper / pre-set GH_TOKEN env). The impl does NOT
//     manage auth — that's the caller's responsibility.
//   - Force-push is intentionally NOT supported. Re-pushing to a
//     branch that already has commits is an Apply() idempotency
//     violation (see apply.go skip-existing path); if you need to
//     rewrite, delete the remote branch first.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

// ShellGitClient is the production GitClient for batchpr.Apply.
// Construct with NewShellGitClient(workdir).
type ShellGitClient struct {
	// Workdir is the repo root the impl operates against. Must be
	// a path that already contains a `.git` directory.
	Workdir string

	// run is injected for testability (defaults to exec.CommandContext).
	// Tests substitute a stub that records args without actually
	// shelling out. nil = use default.
	run cmdRunner
}

// NewShellGitClient returns a ShellGitClient anchored at `workdir`.
func NewShellGitClient(workdir string) *ShellGitClient {
	return &ShellGitClient{Workdir: workdir}
}

// CreateBranch implements GitClient.
//
// Strategy: `git checkout -B name baseBranch`. The `-B` form
// creates-or-resets the local branch — safe even when re-running
// Apply() after a partial failure (the orchestration's idempotency
// check already filtered out branches that exist on the remote).
func (g *ShellGitClient) CreateBranch(ctx context.Context, name, baseBranch string) error {
	if name == "" {
		return fmt.Errorf("create branch: empty branch name")
	}
	if _, err := g.runGit(ctx, "checkout", "-B", name, baseBranch); err != nil {
		return fmt.Errorf("git checkout -B %s %s: %w", name, baseBranch, err)
	}
	return nil
}

// WriteFiles implements GitClient. The branch parameter is used as
// a sanity check that we're operating on the expected branch — a
// future caller that interleaves WriteFiles across branches would
// otherwise silently write to the wrong worktree.
func (g *ShellGitClient) WriteFiles(ctx context.Context, branch string, files map[string][]byte) error {
	current, err := g.currentBranch(ctx)
	if err != nil {
		return fmt.Errorf("read current branch: %w", err)
	}
	if current != branch {
		return fmt.Errorf("write files: expected branch %q, currently on %q", branch, current)
	}
	for relPath, body := range files {
		full := filepath.Join(g.Workdir, filepath.FromSlash(relPath))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			return fmt.Errorf("mkdir for %q: %w", relPath, err)
		}
		if err := os.WriteFile(full, body, 0o644); err != nil {
			return fmt.Errorf("write %q: %w", relPath, err)
		}
		if _, err := g.runGit(ctx, "add", "--", relPath); err != nil {
			return fmt.Errorf("git add %q: %w", relPath, err)
		}
	}
	return nil
}

// Commit implements GitClient.
//
// Empty `author` falls back to `git config user.{name,email}`
// (i.e. git's normal env-var resolution); we don't re-implement
// that ladder here.
func (g *ShellGitClient) Commit(ctx context.Context, branch, message, author string) error {
	current, err := g.currentBranch(ctx)
	if err != nil {
		return fmt.Errorf("read current branch: %w", err)
	}
	if current != branch {
		return fmt.Errorf("commit: expected branch %q, currently on %q", branch, current)
	}
	args := []string{"commit", "-m", message}
	if author != "" {
		args = append(args, fmt.Sprintf("--author=%s", author))
	}
	if _, err := g.runGit(ctx, args...); err != nil {
		return fmt.Errorf("git commit: %w", err)
	}
	return nil
}

// Push implements GitClient. Uses `--set-upstream` so the local
// branch tracks the remote — convenient when a customer runs
// follow-up pushes manually after Apply().
func (g *ShellGitClient) Push(ctx context.Context, branch string) error {
	if _, err := g.runGit(ctx, "push", "--set-upstream", "origin", branch); err != nil {
		return fmt.Errorf("git push origin %s: %w", branch, err)
	}
	return nil
}

// BranchExistsRemote implements GitClient.
//
// Uses `git ls-remote --exit-code origin refs/heads/<branch>`,
// which exits 0 (found) / 2 (not found) / 1 (other error). Apply()
// only branches on found-or-not, so we map exit 2 → false and
// non-zero non-2 → error.
//
// Note: `defaultRunner.run` wraps the underlying *exec.ExitError
// via fmt.Errorf("…%w…"); the unwrap MUST use errors.As, not a
// direct type assertion. (PR-2 self-review caught this — the
// direct assertion always failed and ExitCode==2 was treated as
// an error rather than "branch absent".)
func (g *ShellGitClient) BranchExistsRemote(ctx context.Context, branch string) (bool, error) {
	_, err := g.runGit(ctx, "ls-remote", "--exit-code", "origin", "refs/heads/"+branch)
	if err == nil {
		return true, nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) && exitErr.ExitCode() == 2 {
		return false, nil
	}
	return false, fmt.Errorf("git ls-remote: %w", err)
}

// RebaseOnto implements GitClient.
//
// Strategy:
//
//  1. Fetch origin so newBase is locally available (typical
//     newBase is a SHA from the merged base PR; the local repo
//     may not have it yet).
//  2. Checkout the target branch.
//  3. `git rebase --onto <newBase> <oldBase> <branch>`. On
//     success → return clean RebaseOutcome. On conflict → parse
//     `git status --porcelain` for the conflicted-file list,
//     `git rebase --abort` to clean the working tree, return
//     RebaseOutcome with Conflicted=true.
//  4. "Already up to date" (rebase exit 0 + a specific stderr
//     marker) is treated as AlreadyUpToDate=true so the
//     orchestration knows no force-push is needed (caller still
//     pushes — the operation is a no-op which is fine).
//
// Errors at the interface level are limited to "git couldn't even
// run" conditions (PATH issue, repo missing, ref missing). Conflicts
// are NOT errors — they're an outcome the caller handles.
func (g *ShellGitClient) RebaseOnto(ctx context.Context, branch, oldBase, newBase string) (*RebaseOutcome, error) {
	if branch == "" {
		return nil, fmt.Errorf("rebase: empty branch name")
	}
	if oldBase == "" {
		return nil, fmt.Errorf("rebase: empty oldBase")
	}
	if newBase == "" {
		return nil, fmt.Errorf("rebase: empty newBase")
	}

	if _, err := g.runGit(ctx, "fetch", "origin"); err != nil {
		return nil, fmt.Errorf("git fetch origin: %w", err)
	}
	if _, err := g.runGit(ctx, "checkout", branch); err != nil {
		return nil, fmt.Errorf("git checkout %s: %w", branch, err)
	}

	out, err := g.runGit(ctx, "rebase", "--onto", newBase, oldBase, branch)
	if err == nil {
		// Clean rebase. Distinguish "real rebase happened" from
		// "already up to date" via git's stdout message — purely
		// informational, both are AlreadyUpToDate semantically
		// equivalent for the orchestration (no conflicts, branch
		// at the desired state).
		alreadyUTD := strings.Contains(out, "is up to date") ||
			strings.Contains(out, "Current branch") && strings.Contains(out, "up to date")
		return &RebaseOutcome{AlreadyUpToDate: alreadyUTD}, nil
	}

	// Rebase produced conflicts (or a different non-zero exit).
	// Parse git status to identify conflicted files, then abort
	// the rebase so the working tree is clean for the next op.
	conflicts, statusErr := g.collectRebaseConflicts(ctx)
	if abortErr := g.abortRebase(ctx); abortErr != nil {
		// Best-effort abort failure is logged into the returned
		// error but doesn't override the conflict signal — the
		// orchestration's report still surfaces the conflict.
		return nil, fmt.Errorf("git rebase --onto %s %s %s failed AND abort failed: rebase=%w; abort=%v",
			newBase, oldBase, branch, err, abortErr)
	}
	if statusErr != nil {
		// Couldn't even read the conflicted-file list. Surface as
		// a hard error since the caller has no useful payload.
		return nil, fmt.Errorf("git rebase --onto failed and conflict-list query also failed: rebase=%w; status=%v",
			err, statusErr)
	}

	return &RebaseOutcome{
		Conflicted:      true,
		ConflictedFiles: conflicts,
	}, nil
}

// collectRebaseConflicts parses `git status --porcelain` for files
// in conflicting states (XX where X is U or both files modified).
// Returns sorted file paths.
//
// Porcelain format: each line is `XY <path>` where X / Y are status
// codes. Conflicts have at least one of {U, A, D} on both sides:
// "UU", "AA", "DD", "UD", "DU", "AU", "UA". We accept any line with
// at least one 'U' to keep the matcher broad.
func (g *ShellGitClient) collectRebaseConflicts(ctx context.Context) ([]string, error) {
	out, err := g.runGit(ctx, "status", "--porcelain")
	if err != nil {
		return nil, err
	}
	var files []string
	for _, line := range strings.Split(out, "\n") {
		if len(line) < 4 {
			continue
		}
		status := line[:2]
		// Conflicts always include 'U' on at least one side OR
		// both 'A' / both 'D'. Match any of those.
		conflicted := strings.ContainsAny(status, "U") ||
			status == "AA" || status == "DD"
		if !conflicted {
			continue
		}
		path := strings.TrimSpace(line[3:])
		if path == "" {
			continue
		}
		files = append(files, path)
	}
	// Stable order for the report.
	sort.Strings(files)
	return files, nil
}

// abortRebase issues `git rebase --abort`. Best-effort; the caller
// converts a non-nil error into a wrapped error message rather than
// retrying.
func (g *ShellGitClient) abortRebase(ctx context.Context) error {
	if _, err := g.runGit(ctx, "rebase", "--abort"); err != nil {
		return err
	}
	return nil
}

// ForcePushWithLease implements GitClient.
//
// Uses `git push --force-with-lease origin <branch>`. The
// `--with-lease` variant refuses to overwrite remote work that
// landed since our last fetch — a safer alternative to plain
// `--force`. Refresh() runs `git fetch` (via RebaseOnto's fetch
// step) before this so the lease is freshly anchored.
func (g *ShellGitClient) ForcePushWithLease(ctx context.Context, branch string) error {
	if branch == "" {
		return fmt.Errorf("force-push: empty branch name")
	}
	if _, err := g.runGit(ctx, "push", "--force-with-lease", "origin", branch); err != nil {
		return fmt.Errorf("git push --force-with-lease origin %s: %w", branch, err)
	}
	return nil
}

// currentBranch returns the branch HEAD points at via `git
// rev-parse --abbrev-ref HEAD`. Helper for the WriteFiles / Commit
// "are we on the right branch" sanity guard.
func (g *ShellGitClient) currentBranch(ctx context.Context) (string, error) {
	out, err := g.runGit(ctx, "rev-parse", "--abbrev-ref", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(out), nil
}

// runGit invokes `git` with the supplied args in g.Workdir. Returns
// stdout bytes (trimmed of trailing newline by callers when
// relevant).
func (g *ShellGitClient) runGit(ctx context.Context, args ...string) (string, error) {
	r := g.run
	if r == nil {
		r = defaultRunner{}
	}
	return r.run(ctx, g.Workdir, "git", args...)
}

// cmdRunner abstracts exec.CommandContext so tests can capture
// invocations without actually shelling out. Production wires
// defaultRunner.
type cmdRunner interface {
	run(ctx context.Context, dir, name string, args ...string) (string, error)
}

// defaultRunner is the production cmdRunner — uses exec.CommandContext
// in the supplied dir. Stdout captured; stderr discarded into the
// error message via exec.ExitError.Stderr.
type defaultRunner struct{}

func (defaultRunner) run(ctx context.Context, dir, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	out, err := cmd.Output()
	if err != nil {
		// exec.ExitError carries Stderr; surface it inline so
		// callers see the actual git diagnostic, not just "exit 1".
		if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
			return "", fmt.Errorf("%s %s: %w (stderr: %s)",
				name, strings.Join(args, " "), err, strings.TrimSpace(string(ee.Stderr)))
		}
		return "", fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return string(out), nil
}

// Compile-time interface assertion: *ShellGitClient implements GitClient.
var _ GitClient = (*ShellGitClient)(nil)
