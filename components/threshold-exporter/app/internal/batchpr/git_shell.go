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
