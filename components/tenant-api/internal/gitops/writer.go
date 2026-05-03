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
	"errors"
	"fmt"
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

// Writer handles GitOps write-back operations.
type Writer struct {
	mu             sync.Mutex
	configDir      string // path to conf.d/ directory (YAML files live here)
	gitDir         string // git repository root (may differ from configDir)
	committerName  string // cached from GIT_COMMITTER_NAME env var
	committerEmail string // cached from GIT_COMMITTER_EMAIL env var
	onWrite        OnWriteFunc // v2.6.0: callback for post-write notifications (e.g. SSE hub)
}

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
	}
}

// SetOnWrite registers a callback to be invoked after a successful config write.
// This is used by v2.6.0 WebSocket/SSE hub to broadcast config change events.
func (w *Writer) SetOnWrite(fn OnWriteFunc) {
	w.onWrite = fn
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

	cmd := exec.Command("git", "diff", "--no-index", "--", filePath, tmpFile.Name())
	out, _ := cmd.Output() // git diff exits 1 when there are differences — that's expected
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

// writeSpecialFile is a shared implementation for writing _groups.yaml, _views.yaml, etc.
// These files use the same mutex and conflict detection as tenant writes — only
// the validation step differs (basic YAML well-formedness, not full schema).
func (w *Writer) writeSpecialFile(filename, entityType, authorEmail, yamlContent string) error {
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
func (w *Writer) commitFileChange(filePath, commitTag, authorEmail string, content []byte) error {
	headBefore, err := w.currentHEAD()
	if err != nil {
		// Proceed without conflict detection in non-git environments.
		slog.Warn("gitops: could not read HEAD before write",
			"commit_tag", commitTag, "error", err)
	}

	if err := os.WriteFile(filePath, content, 0644); err != nil {
		return fmt.Errorf("write file: %w", err)
	}

	if err := w.gitCommit(filePath, commitTag, authorEmail); err != nil {
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
	cmd := exec.Command("git", "-C", w.gitDir, "rev-parse", "HEAD")
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

// commitParent returns the parent commit hash of HEAD (i.e. HEAD~1).
func (w *Writer) commitParent() (string, error) {
	cmd := exec.Command("git", "-C", w.gitDir, "rev-parse", "HEAD~1")
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

// gitCommit stages filePath and creates a commit with the operator's email as author.
//
// Committer identity is sourced from the GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL
// environment variables (set in the K8s Deployment). This keeps the audit trail clean:
//   - author  = the human operator (from X-Forwarded-Email via oauth2-proxy)
//   - committer = the service account (da-portal@dynamic-alerting.local)
func (w *Writer) gitCommit(filePath, tenantID, authorEmail string) error {
	// Stage the file
	addCmd := exec.Command("git", "-C", w.gitDir, "add", filePath)
	if out, err := addCmd.CombinedOutput(); err != nil {
		return fmt.Errorf("git add: %w — %s", err, string(out))
	}

	// Check if there's actually something to commit
	statusCmd := exec.Command("git", "-C", w.gitDir, "diff", "--cached", "--quiet")
	if err := statusCmd.Run(); err == nil {
		// Exit 0 means no changes staged — nothing to commit
		return nil
	}

	msg := fmt.Sprintf("tenant/%s: update via portal\n\nTimestamp: %s\nSource: da-portal/tenant-manager",
		tenantID, time.Now().UTC().Format(time.RFC3339))

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

	commitCmd := exec.Command("git", "-C", w.gitDir,
		"-c", "user.name="+committerName,
		"-c", "user.email="+committerEmail,
		"commit",
		"--author="+author,
		"-m", msg,
	)
	if out, err := commitCmd.CombinedOutput(); err != nil {
		return fmt.Errorf("git commit: %w — %s", err, string(out))
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
//  1. Creates a new branch from the current HEAD
//  2. Writes the file and commits on the feature branch
//  3. Pushes the branch to origin
//  4. Returns the branch name (caller creates the PR via GitHub API)
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

	// Step 3: create and checkout feature branch
	if err := w.gitExec("checkout", "-b", branchName); err != nil {
		return nil, fmt.Errorf("create branch: %w", err)
	}

	// Step 4: write file
	filePath := filepath.Join(w.configDir, tenantID+".yaml")
	if err := os.WriteFile(filePath, []byte(yamlContent), 0644); err != nil {
		// Rollback: switch back to original branch
		_ = w.gitExec("checkout", "-")
		_ = w.gitExec("branch", "-D", branchName)
		return nil, fmt.Errorf("write file: %w", err)
	}

	// Step 5: commit on feature branch
	if err := w.gitCommit(filePath, tenantID, authorEmail); err != nil {
		_ = w.gitExec("checkout", "-")
		_ = w.gitExec("branch", "-D", branchName)
		return nil, fmt.Errorf("git commit on branch: %w", err)
	}

	// Step 6: push branch to origin
	if err := w.gitExec("push", "origin", branchName); err != nil {
		slog.Warn("gitops: push branch failed",
			"branch", branchName, "error", err, "note", "PR creation will fail")
		// Don't delete the branch — the commit is valuable even if push fails
	}

	// Step 7: switch back to the original branch (main/HEAD)
	if err := w.gitExec("checkout", "-"); err != nil {
		slog.Warn("gitops: failed to switch back from branch",
			"branch", branchName, "error", err)
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

	if err := w.gitExec("checkout", "-b", branchName); err != nil {
		return nil, fmt.Errorf("create branch: %w", err)
	}

	// Write all files and commit each
	for _, op := range ops {
		filePath := filepath.Join(w.configDir, op.TenantID+".yaml")
		if err := os.WriteFile(filePath, []byte(op.YAMLContent), 0644); err != nil {
			_ = w.gitExec("checkout", "-")
			_ = w.gitExec("branch", "-D", branchName)
			return nil, fmt.Errorf("write file for %s: %w", op.TenantID, err)
		}
		if err := w.gitCommit(filePath, op.TenantID, authorEmail); err != nil {
			_ = w.gitExec("checkout", "-")
			_ = w.gitExec("branch", "-D", branchName)
			return nil, fmt.Errorf("commit for %s: %w", op.TenantID, err)
		}
	}

	if err := w.gitExec("push", "origin", branchName); err != nil {
		slog.Warn("gitops: push batch branch failed",
			"branch", branchName, "error", err)
	}

	if err := w.gitExec("checkout", "-"); err != nil {
		slog.Warn("gitops: failed to switch back from batch branch",
			"branch", branchName, "error", err)
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

// gitExec runs a git command in the git directory.
func (w *Writer) gitExec(args ...string) error {
	fullArgs := append([]string{"-C", w.gitDir}, args...)
	cmd := exec.Command("git", fullArgs...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("git %s: %w — %s", args[0], err, string(out))
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
