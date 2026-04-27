package batchpr

// PR-2 — interfaces between Apply() orchestration and the actual
// git / GitHub side-effects. All side effects live behind these
// interfaces so Apply() is fully unit-testable with in-memory
// stubs (see apply_test.go) and production wires shell-out
// implementations (git_shell.go, pr_gh.go).
//
// Why interfaces vs direct shell-out:
//
//   1. Tests would otherwise need a real git repo + GitHub token,
//      which makes them slow + brittle. Stubs run in-process.
//
//   2. Customers may run apply against a non-GitHub remote (GitLab
//      MR, Gerrit) — letting the interface define the behaviour
//      keeps the orchestration logic re-usable; only the impl swaps.
//
//   3. Rate-limit handling lives in the impl layer, not the
//      orchestration. Apply() can stay simple-minded; impls retry
//      with backoff per their underlying client's conventions.

import "context"

// GitClient performs git-level operations against the local working
// tree + remote.
//
// All methods should be idempotent where possible — e.g.
// CreateBranch with an already-existing branch name should NOT
// error (the apply layer's idempotency check is one level up,
// at PRClient.FindPRByBranch).
type GitClient interface {
	// CreateBranch creates `name` from `baseBranch`. If `name`
	// already exists locally, the impl should fast-forward it to
	// `baseBranch` (or no-op when already at HEAD); a return error
	// here is treated as fatal for the PlanItem.
	CreateBranch(ctx context.Context, name, baseBranch string) error

	// WriteFiles writes each `path → bytes` entry to the repo
	// working tree, replacing existing files. The branch arg
	// confirms which branch the impl is operating on (for shell
	// impls that use `git checkout` per call).
	WriteFiles(ctx context.Context, branch string, files map[string][]byte) error

	// Commit creates a single commit on `branch` with the given
	// message + author. Author follows the conventional
	// `Name <email>` shape; empty falls back to the impl's
	// configured user. The impl is responsible for `git add` of
	// every WriteFiles path.
	Commit(ctx context.Context, branch, message, author string) error

	// Push pushes `branch` to `origin`. Should be a force-push only
	// when the impl detects that the local branch was rewound
	// (deliberate rewrite) — never blindly. The orchestration
	// layer doesn't currently rewrite branches, so non-force is
	// the default expected behaviour.
	Push(ctx context.Context, branch string) error

	// BranchExistsRemote checks whether `branch` already exists on
	// origin. Used by Apply() to skip work for PRs that have
	// already been opened (idempotency).
	BranchExistsRemote(ctx context.Context, branch string) (bool, error)
}

// PRClient performs GitHub PR API operations.
type PRClient interface {
	// OpenPR creates a new pull request and returns the resulting
	// PR number + URL. Errors include rate-limit + auth failures;
	// the apply layer surfaces them per-item.
	OpenPR(ctx context.Context, in OpenPRInput) (*PROpened, error)

	// FindPRByBranch looks up the open PR whose head branch is
	// `branch`. Returns (nil, nil) when no such PR exists — this
	// is NOT an error. Used by Apply() for idempotency: an
	// existing PR for our deterministic branch name means a
	// previous apply already opened it.
	FindPRByBranch(ctx context.Context, branch string) (*PROpened, error)

	// UpdatePRDescription replaces the body of an existing PR with
	// `body`. Used by Apply() to fill the `<base>` placeholder in
	// tenant PR descriptions after the base PR opens.
	UpdatePRDescription(ctx context.Context, num int, body string) error
}

// OpenPRInput is the contract for a new PR.
type OpenPRInput struct {
	// Title is the PR title (PlanItem.Title).
	Title string

	// Body is the PR description Markdown (PlanItem.Description,
	// possibly with the `<base>` placeholder still unresolved —
	// the orchestration substitutes after the base PR opens).
	Body string

	// Head is the source branch (e.g. `da-tools/c10/base-<sha>`).
	Head string

	// Base is the target branch (typically `main`). Mirrors
	// Repo.BaseBranch.
	Base string
}

// PROpened reports the outcome of OpenPR or FindPRByBranch.
type PROpened struct {
	// Number is the PR number assigned by GitHub.
	Number int

	// URL is the GitHub HTML URL (e.g.
	// `https://github.com/owner/repo/pull/123`).
	URL string
}
