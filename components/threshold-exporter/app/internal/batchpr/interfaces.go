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

	// RebaseOnto runs `git rebase --onto <newBase> <oldBase>
	// <branch>`, leaving the working tree on `branch`. Used by
	// PR-3 Refresh() to re-anchor tenant branches onto the merged
	// Base PR's main HEAD.
	//
	// Returns RebaseOutcome describing what happened. Conflicts
	// are NOT errors at the interface level — they're an expected
	// outcome the orchestration handles. The impl SHOULD `git
	// rebase --abort` on conflicts so the working tree returns to
	// a clean state regardless of conflict outcome (apply.go's
	// other operations assume a clean tree).
	//
	// A non-nil error reflects a "couldn't even start" condition
	// (git not on PATH, repo not a git repo, etc.) — distinct from
	// "started but conflicts emerged".
	RebaseOnto(ctx context.Context, branch, oldBase, newBase string) (*RebaseOutcome, error)

	// ForcePushWithLease pushes `branch` to origin with `--force-
	// with-lease`. Used after RebaseOnto rewrites history; the
	// `--with-lease` variant refuses to overwrite remote work that
	// landed since our last fetch — a safer alternative to plain
	// `--force` for collaborative branches.
	ForcePushWithLease(ctx context.Context, branch string) error
}

// RebaseOutcome describes the outcome of a single RebaseOnto call.
// The orchestration translates this into the higher-level
// RebaseStatus on RefreshItemResult.
type RebaseOutcome struct {
	// Conflicted is true iff git reported conflicts that aborted
	// the rebase. ConflictedFiles is populated in this case.
	Conflicted bool

	// ConflictedFiles is the list of conflicted paths git named
	// (parsed out of `git status --porcelain` after the rebase
	// failed, or `git rerere` style output — impl's choice). Sorted
	// for stable report output. Empty when Conflicted=false.
	ConflictedFiles []string

	// AlreadyUpToDate is true when `git rebase --onto` reported
	// "Current branch is up to date" — the rebase was a no-op
	// because the branch was already anchored on newBase. Treated
	// as Clean by the orchestration (the desired state was
	// already achieved). The impl sets this OR returns a clean
	// rebase; either way the orchestration sees "no work needed".
	AlreadyUpToDate bool
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

	// GetPR fetches metadata about an existing PR. Used by
	// Refresh() to decide whether a tenant PR is still open
	// (rebase needed) or closed/merged (skip).
	//
	// Errors when the PR doesn't exist or the API call fails;
	// neither case is meaningful for refresh, so the orchestration
	// records both as `step="get_pr"` failures.
	GetPR(ctx context.Context, num int) (*PRDetails, error)

	// CommentPR posts a Markdown comment on an existing PR. Used
	// by Refresh() to leave a "rebased onto merged main" note so
	// reviewers see the refresh happened.
	CommentPR(ctx context.Context, num int, body string) error
}

// PRDetails reports the subset of PR metadata Refresh() cares about.
type PRDetails struct {
	// Number echoes the queried PR number.
	Number int

	// State is "open", "closed", or "merged". Mirrors GitHub's PR
	// state machine. A merged PR also has State=="merged" (not
	// "closed") so callers can distinguish merged-then-closed
	// (success) from explicitly-closed (abandoned).
	State PRState

	// HeadBranch is the head branch the PR is built on. Refresh
	// cross-checks this against RefreshTarget.BranchName; a
	// mismatch surfaces as a warning so operators notice when
	// someone manually rebased + force-pushed (or otherwise rewrote
	// the branch) before our refresh ran.
	HeadBranch string

	// URL is the GitHub HTML URL — populated for completeness so
	// the refresh-report.md can link back to each PR.
	URL string
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
