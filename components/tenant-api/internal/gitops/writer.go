// Package gitops implements commit-on-write operations for tenant config files.
//
// Design (ADR-009, ADR-011):
//   - All write operations hold a sync.Mutex to prevent concurrent git conflicts.
//   - Each write records the HEAD commit before and after to detect conflicts.
//   - Commits use the operator's email as git author for audit trail.
//   - Schema validation is run before any disk write.
//   - v2.6.0: PR-based write-back mode (ADR-011) creates feature branches
//     and pushes for external PR creation instead of committing to the main branch.
package gitops

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
	"sync"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// ErrConflict is returned when the git HEAD moved during a write operation.
var ErrConflict = errors.New("conflict: repository was updated concurrently, please refresh and retry")

// ErrPendingPR is returned when a tenant already has a pending PR (PR mode only).
var ErrPendingPR = errors.New("pending PR exists for this tenant")

// OnWriteFunc is called after a successful config write.
// tenantID is the tenant or entity that was written (tenant ID, "groups", "views", etc.)
type OnWriteFunc func(tenantID string)

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

// Writer handles GitOps write-back operations.
type Writer struct {
	mu             sync.Mutex
	configDir      string        // path to conf.d/ directory (YAML files live here)
	gitDir         string        // git repository root (may differ from configDir)
	committerName  string        // cached from GIT_COMMITTER_NAME env var
	committerEmail string        // cached from GIT_COMMITTER_EMAIL env var
	onWrite        OnWriteFunc   // v2.6.0: callback for post-write notifications (e.g. SSE hub)
	gitTimeout     time.Duration // per-git-command wall-clock deadline (#630); 0 → defaultGitTimeout
	gitWaitDelay   time.Duration // cmd.WaitDelay grace after a deadline kill (#630); 0 → defaultGitKillGrace
	gitBinary      string        // git executable; "git" in prod, overridden in tests (timeout seam)
	baseBranch     string        // PR-mode base to branch from / return to (#638); "" → defaultBaseBranch
}

// defaultBaseBranch is the PR-mode base branch when none is configured (#638).
const defaultBaseBranch = "main"

// NewWriter creates a Writer for the given directories.
// configDir is where tenant YAML files live; gitDir is the git repo root.
// If gitDir is empty, configDir is used as the git root.
func NewWriter(configDir, gitDir string) *Writer {
	if gitDir == "" {
		gitDir = configDir
	}
	return &Writer{
		configDir:      configDir,
		gitDir:         gitDir,
		committerName:  os.Getenv("GIT_COMMITTER_NAME"),
		committerEmail: os.Getenv("GIT_COMMITTER_EMAIL"),
		gitTimeout:     gitTimeoutFromEnv(),
		gitBinary:      "git",
	}
}

// gitTimeoutFromEnv reads TENANT_API_GIT_TIMEOUT as a Go duration, falling back
// to defaultGitTimeout when unset, unparseable, or non-positive (a clamp keeps a
// fat-fingered "0"/"-5s" from disabling the lock-release safety net).
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

// SetOnWrite registers a callback to be invoked after a successful config write.
// This is used by v2.6.0 WebSocket/SSE hub to broadcast config change events.
func (w *Writer) SetOnWrite(fn OnWriteFunc) {
	w.onWrite = fn
}

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

// Write validates, persists, and commits a tenant's config YAML.
//
// Flow (steps 2–6 are shared with writeSpecialFile via commitFileChange):
//  1. Validate YAML schema (ParseConfig + ValidateTenantKeys)
//  2. Lock mutex
//  3. Record HEAD before write
//  4. Write file to configDir/{tenantID}.yaml
//  5. git add + git commit --author="<authorEmail>"
//  6. Check HEAD again (conflict detection)
//  7. onWrite callback (e.g. SSE broadcast)
func (w *Writer) Write(tenantID, authorEmail, yamlContent string) error {
	// Step 1: validate schema before touching disk.
	if errs := validate(tenantID, yamlContent); len(errs) > 0 {
		return fmt.Errorf("validation failed: %s", strings.Join(errs, "; "))
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	return w.commitFileChange(
		filepath.Join(w.configDir, tenantID+".yaml"),
		tenantID,
		authorEmail,
		[]byte(yamlContent),
	)
}

// Diff returns the unified diff between the current file and proposed content.
// Returns empty string if files are identical or no current file exists.
func (w *Writer) Diff(tenantID, proposedContent string) (string, error) {
	filePath := filepath.Join(w.configDir, tenantID+".yaml")

	existing, err := os.ReadFile(filePath)
	if os.IsNotExist(err) {
		// New file — show the entire proposed content as an addition
		var lines []string
		for _, line := range strings.Split(proposedContent, "\n") {
			lines = append(lines, "+"+line)
		}
		return strings.Join(lines, "\n"), nil
	}
	if err != nil {
		return "", fmt.Errorf("read existing: %w", err)
	}

	if string(existing) == proposedContent {
		return "", nil
	}

	// Use git diff --no-index for a proper unified diff
	tmpFile, err := os.CreateTemp("", "tenant-api-diff-*.yaml")
	if err != nil {
		return "", fmt.Errorf("create temp file: %w", err)
	}
	defer func() { _ = os.Remove(tmpFile.Name()) }()

	if _, err := tmpFile.WriteString(proposedContent); err != nil {
		return "", fmt.Errorf("write temp file: %w", err)
	}
	_ = tmpFile.Close()

	cmd, _, cancel := w.gitCmd("diff", "--no-index", "--", filePath, tmpFile.Name())
	defer cancel()
	// git diff exits 1 when there are differences — that's expected, so the error
	// is intentionally discarded. A deadline-killed diff likewise returns empty
	// output here (this is a read-only advisory diff, not a write path).
	out, _ := cmd.Output()
	return string(out), nil
}

// WriteGroupsFile validates, persists, and commits the _groups.yaml file.
// Reuses the same sync.Mutex and HEAD conflict detection as tenant writes.
func (w *Writer) WriteGroupsFile(authorEmail, yamlContent string) error {
	return w.writeSpecialFile("_groups.yaml", "groups", authorEmail, yamlContent)
}

// WriteViewsFile validates, persists, and commits the _views.yaml file.
// v2.5.0 Phase C: Saved Views support.
func (w *Writer) WriteViewsFile(authorEmail, yamlContent string) error {
	return w.writeSpecialFile("_views.yaml", "views", authorEmail, yamlContent)
}

// WriteFederationPolicyFile validates, persists, and commits the
// platform federation whitelist (_federation_policy.yaml). ADR-020 IV-2e.
//
// An optional trailer is appended to the commit message body — used to
// record an admission-validator `--force` bypass (operator + reason)
// directly in git history, the only durable audit trail in a GitOps
// system (ADR-020 IV-2e; stdout logs rotate away).
func (w *Writer) WriteFederationPolicyFile(authorEmail, yamlContent string, trailer ...string) error {
	return w.writeSpecialFile("_federation_policy.yaml", "federation-policy", authorEmail, yamlContent, trailer...)
}

// WriteFederationSubsetFile validates, persists, and commits one
// tenant's federation metric subset to _federation/<tenantID>.yaml
// (ADR-020 IV-2e). One file per tenant on purpose: a tenant's
// self-service subset edits never contend on a shared git object, so
// concurrent edits across tenants cannot conflict. The _federation/
// directory is created on first write.
func (w *Writer) WriteFederationSubsetFile(tenantID, authorEmail, yamlContent string) error {
	// Basic YAML validity check (the schema check is the caller's job).
	var raw map[string]interface{}
	if err := yaml.Unmarshal([]byte(yamlContent), &raw); err != nil {
		return fmt.Errorf("invalid YAML: %w", err)
	}

	// MkdirAll is idempotent and git-independent — done before taking
	// the write lock so a filesystem syscall never serialises behind
	// the (git-bound) write path.
	dir := filepath.Join(w.configDir, "_federation")
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("create _federation dir: %w", err)
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	return w.commitFileChange(
		filepath.Join(dir, tenantID+".yaml"),
		"federation/"+tenantID,
		authorEmail,
		[]byte(yamlContent),
	)
}

// writeSpecialFile is a shared implementation for writing _groups.yaml, _views.yaml, etc.
// These files use the same mutex and conflict detection as tenant writes — only
// the validation step differs (basic YAML well-formedness, not full schema).
func (w *Writer) writeSpecialFile(filename, entityType, authorEmail, yamlContent string, trailer ...string) error {
	// Basic YAML validity check (special files don't have a schema).
	var raw map[string]interface{}
	if err := yaml.Unmarshal([]byte(yamlContent), &raw); err != nil {
		return fmt.Errorf("invalid YAML: %w", err)
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	return w.commitFileChange(
		filepath.Join(w.configDir, filename),
		entityType,
		authorEmail,
		[]byte(yamlContent),
		trailer...,
	)
}

// commitFileChange is the shared write+commit+conflict-detect+notify
// flow used by both Write (tenant YAML) and writeSpecialFile
// (_groups.yaml / _views.yaml). Caller MUST hold w.mu before calling.
//
// `commitTag` identifies what's being committed in log lines, the
// commit message subject (via gitCommit), and the onWrite callback
// argument. For tenant writes it's the tenant ID; for special files
// it's the entity type ("groups" / "views").
//
// Returns ErrConflict if the recorded HEAD before the write differs
// from our commit's parent (someone else pushed between our read and
// our write). Non-git environments skip conflict detection but still
// return commit errors verbatim.
func (w *Writer) commitFileChange(filePath, commitTag, authorEmail string, content []byte, trailer ...string) error {
	headBefore, err := w.currentHEAD()
	if err != nil {
		// Proceed without conflict detection in non-git environments.
		slog.Warn("gitops: could not read HEAD before write",
			"commit_tag", commitTag, "error", err)
	}

	if err := os.WriteFile(filePath, content, 0644); err != nil {
		return fmt.Errorf("write file: %w", err)
	}

	if err := w.gitCommit(filePath, commitTag, authorEmail, trailer...); err != nil {
		slog.Warn("gitops: commit failed", "commit_tag", commitTag, "error", err)
		return fmt.Errorf("git commit: %w", err)
	}

	if headBefore != "" {
		parent, err := w.commitParent()
		if err == nil && parent != headBefore {
			slog.Warn("gitops: external commit detected",
				"commit_tag", commitTag,
				"expected_parent", headBefore[:8],
				"actual_parent", parent[:8])
			return ErrConflict
		}
	}

	slog.Info("gitops: committed", "commit_tag", commitTag, "author", authorEmail)

	// v2.6.0: Notify via callback (e.g. SSE hub broadcast).
	if w.onWrite != nil {
		w.onWrite(commitTag)
	}

	return nil
}

// currentHEAD returns the current HEAD commit hash of the git repository.
func (w *Writer) currentHEAD() (string, error) {
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", "HEAD")
	defer cancel()
	out, err := cmd.Output()
	if err != nil {
		return "", w.gitErr(ctx, "rev-parse HEAD", err, out)
	}
	return strings.TrimSpace(string(out)), nil
}

// commitParent returns the parent commit hash of HEAD (i.e. HEAD~1).
func (w *Writer) commitParent() (string, error) {
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", "HEAD~1")
	defer cancel()
	out, err := cmd.Output()
	if err != nil {
		return "", w.gitErr(ctx, "rev-parse HEAD~1", err, out)
	}
	return strings.TrimSpace(string(out)), nil
}

// gitCommit stages filePath and creates a commit with the operator's email as author.
//
// Committer identity is sourced from the GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL
// environment variables (set in the K8s Deployment). This keeps the audit trail clean:
//   - author  = the human operator (from X-Forwarded-Email via oauth2-proxy)
//   - committer = the service account (da-portal@dynamic-alerting.local)
func (w *Writer) gitCommit(filePath, tenantID, authorEmail string, trailer ...string) error {
	// Stage the file
	addCmd, addCtx, addCancel := w.gitCmd("-C", w.gitDir, "add", filePath)
	defer addCancel()
	if out, err := addCmd.CombinedOutput(); err != nil {
		return w.gitErr(addCtx, "add", err, out)
	}

	// Check if there's actually something to commit
	statusCmd, _, statusCancel := w.gitCmd("-C", w.gitDir, "diff", "--cached", "--quiet")
	defer statusCancel()
	if err := statusCmd.Run(); err == nil {
		// Exit 0 means no changes staged — nothing to commit
		return nil
	}

	msg := fmt.Sprintf("tenant/%s: update via portal\n\nTimestamp: %s\nSource: da-portal/tenant-manager",
		tenantID, time.Now().UTC().Format(time.RFC3339))
	// Optional trailer — appended to the message body so an audit
	// annotation (e.g. an admission-validator --force bypass) is
	// permanently bound to the commit, not just an ephemeral log line.
	if len(trailer) > 0 && trailer[0] != "" {
		msg += "\n\n" + trailer[0]
	}

	// author name defaults to email prefix when no display name is available
	authorName := authorEmail
	if at := strings.Index(authorEmail, "@"); at > 0 {
		authorName = authorEmail[:at]
	}
	author := fmt.Sprintf("%s <%s>", authorName, authorEmail)

	// Committer identity: cached from env vars injected by K8s Deployment.
	// Fall back to author identity if not set (dev/local mode).
	committerName := w.committerName
	committerEmail := w.committerEmail
	if committerName == "" {
		committerName = authorName
	}
	if committerEmail == "" {
		committerEmail = authorEmail
	}

	commitCmd, commitCtx, commitCancel := w.gitCmd("-C", w.gitDir,
		"-c", "user.name="+committerName,
		"-c", "user.email="+committerEmail,
		"commit",
		"--author="+author,
		"-m", msg,
	)
	defer commitCancel()
	if out, err := commitCmd.CombinedOutput(); err != nil {
		return w.gitErr(commitCtx, "commit", err, out)
	}
	return nil
}

// PRWriteResult contains the result of a PR-mode write operation.
type PRWriteResult struct {
	BranchName string // the feature branch name (e.g. "tenant-api/db-a-prod/20260406-143022")
	FilePath   string // the path of the written file
}

// WritePR validates and writes a tenant config to a feature branch for PR creation.
//
// Unlike Write(), this method:
//  1. Checks out the base branch, then creates a feature branch from it
//  2. Writes the file and commits on the feature branch
//  3. Pushes the branch to origin
//  4. Returns to the base branch + returns the branch name (caller creates the PR)
//
// The caller (handler) is responsible for creating the GitHub PR using the returned branch name.
func (w *Writer) WritePR(tenantID, authorEmail, yamlContent string) (*PRWriteResult, error) {
	// Step 1: validate schema before anything
	if errs := validate(tenantID, yamlContent); len(errs) > 0 {
		return nil, fmt.Errorf("validation failed: %s", strings.Join(errs, "; "))
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	// Step 2: generate branch name
	ts := time.Now().UTC().Format("20060102-150405")
	branchName := fmt.Sprintf("tenant-api/%s/%s", tenantID, ts)

	// Step 3: anchor on a clean base, THEN branch from it. Always checking out the
	// base first (rather than branching from "current HEAD" and returning via the
	// relative `checkout -`) makes cross-tenant branch pollution impossible: even if
	// a prior write left the tree on some feature branch, this re-establishes the
	// base every time (#638). Abort if the base is unreachable — branching from an
	// unknown ref is exactly the bug we're preventing.
	base := w.base()
	if err := w.checkoutBaseClean(base); err != nil {
		return nil, err
	}
	if err := w.gitExec("checkout", "-b", branchName); err != nil {
		return nil, fmt.Errorf("create branch: %w", err)
	}

	// Step 4: write file
	filePath := filepath.Join(w.configDir, tenantID+".yaml")
	if err := os.WriteFile(filePath, []byte(yamlContent), 0644); err != nil {
		// Rollback: force back to a clean base (the file we just wrote is now a
		// dirty tracked change) + drop the branch.
		_ = w.checkoutBaseClean(base)
		_ = w.gitExec("branch", "-D", branchName)
		return nil, fmt.Errorf("write file: %w", err)
	}

	// Step 5: commit on feature branch
	if err := w.gitCommit(filePath, tenantID, authorEmail); err != nil {
		_ = w.checkoutBaseClean(base)
		_ = w.gitExec("branch", "-D", branchName)
		return nil, fmt.Errorf("git commit on branch: %w", err)
	}

	// Step 6: push branch to origin
	pushed := true
	if err := w.gitExec("push", "origin", branchName); err != nil {
		pushed = false
		slog.Warn("gitops: push branch failed",
			"branch", branchName, "error", err, "note", "PR creation will fail")
		// Don't delete the branch — the commit is valuable even if push fails
	}

	// Step 7: return to a clean base branch. On failure we only warn: the next
	// WritePR re-anchors on the base at Step 3 regardless, so the tree can never
	// stay stranded on a feature branch and pollute the next tenant's PR.
	if err := w.checkoutBaseClean(base); err != nil {
		slog.Warn("gitops: failed to switch back to base branch",
			"base", base, "branch", branchName, "error", err)
	}

	// Step 8: drop the local feature branch after a CONFIRMED push (#641). The
	// commit is now safely on origin and the PR is created from origin/<branch> —
	// the local ref is no longer needed. Without this, every WritePR leaks a local
	// `tenant-api/<tenant>/<ts>` ref forever (the deployment runs one long-lived
	// replica, so this is the only thing bounding the loose-ref accumulation).
	// On push failure we KEEP the branch (the only copy of the commit) — same as
	// before. Must run AFTER step 7 (can't -D the currently-checked-out branch).
	// Edge: if step 7 itself only warned (still on the feature branch), this -D
	// fails ("checked out branch") and that one branch leaks — bounded by the
	// next WritePR's #638 ironclad re-anchor at step 3.
	if pushed {
		if err := w.gitExec("branch", "-D", branchName); err != nil {
			slog.Warn("gitops: failed to delete local feature branch after push",
				"branch", branchName, "error", err)
		}
	}

	slog.Info("gitops: PR branch created",
		"branch", branchName, "tenant", tenantID, "author", authorEmail)

	return &PRWriteResult{
		BranchName: branchName,
		FilePath:   filePath,
	}, nil
}

// WritePRBatch validates and writes multiple tenant configs to a single feature branch.
// This supports batch PR mode where all changes are consolidated into one PR.
func (w *Writer) WritePRBatch(ops []PRBatchOp, authorEmail string) (*PRWriteResult, error) {
	if len(ops) == 0 {
		return nil, fmt.Errorf("empty batch operations")
	}

	// Validate all operations first
	for _, op := range ops {
		if errs := validate(op.TenantID, op.YAMLContent); len(errs) > 0 {
			return nil, fmt.Errorf("validation failed for %s: %s", op.TenantID, strings.Join(errs, "; "))
		}
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	ts := time.Now().UTC().Format("20060102-150405")
	branchName := fmt.Sprintf("tenant-api/batch/%s", ts)

	// Anchor on a clean base then branch from it (#638 — see WritePR Step 3).
	base := w.base()
	if err := w.checkoutBaseClean(base); err != nil {
		return nil, err
	}
	if err := w.gitExec("checkout", "-b", branchName); err != nil {
		return nil, fmt.Errorf("create branch: %w", err)
	}

	// Write all files and commit each
	for _, op := range ops {
		filePath := filepath.Join(w.configDir, op.TenantID+".yaml")
		if err := os.WriteFile(filePath, []byte(op.YAMLContent), 0644); err != nil {
			_ = w.checkoutBaseClean(base)
			_ = w.gitExec("branch", "-D", branchName)
			return nil, fmt.Errorf("write file for %s: %w", op.TenantID, err)
		}
		if err := w.gitCommit(filePath, op.TenantID, authorEmail); err != nil {
			_ = w.checkoutBaseClean(base)
			_ = w.gitExec("branch", "-D", branchName)
			return nil, fmt.Errorf("commit for %s: %w", op.TenantID, err)
		}
	}

	pushed := true
	if err := w.gitExec("push", "origin", branchName); err != nil {
		pushed = false
		slog.Warn("gitops: push batch branch failed",
			"branch", branchName, "error", err)
	}

	if err := w.checkoutBaseClean(base); err != nil {
		slog.Warn("gitops: failed to switch back to base branch",
			"base", base, "branch", branchName, "error", err)
	}

	// Drop the local batch branch after a confirmed push (#641, same rationale as
	// WritePR Step 8). On push failure we keep it — the commit is only local.
	if pushed {
		if err := w.gitExec("branch", "-D", branchName); err != nil {
			slog.Warn("gitops: failed to delete local batch branch after push",
				"branch", branchName, "error", err)
		}
	}

	slog.Info("gitops: PR batch branch created",
		"branch", branchName, "ops", len(ops), "author", authorEmail)

	return &PRWriteResult{
		BranchName: branchName,
	}, nil
}

// PRBatchOp represents a single operation in a PR-mode batch write.
type PRBatchOp struct {
	TenantID    string
	YAMLContent string
}

// gitCmd builds a git *exec.Cmd bounded by the writer's per-command timeout
// (#630). The caller MUST defer the returned cancel — it stops the deadline
// timer and kills the child if it's still running. The context is returned so
// callers can tell a deadline kill apart from an ordinary non-zero git exit.
func (w *Writer) gitCmd(args ...string) (*exec.Cmd, context.Context, context.CancelFunc) {
	timeout := w.gitTimeout
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
	return fmt.Errorf("git %s: %w — %s", op, err, string(out))
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

// validate parses the YAML as a ThresholdConfig and runs ValidateTenantKeys.
//
// yamlContent must be a complete ThresholdConfig document:
//
//	tenants:
//	  <tenantID>:
//	    key: value
func validate(tenantID, yamlContent string) []string {
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal([]byte(yamlContent), &tcfg); err != nil {
		return []string{"invalid YAML: " + err.Error()}
	}
	if _, ok := tcfg.Tenants[tenantID]; !ok {
		return []string{fmt.Sprintf("YAML must contain tenants.%s section", tenantID)}
	}
	return tcfg.ValidateTenantKeys()
}
