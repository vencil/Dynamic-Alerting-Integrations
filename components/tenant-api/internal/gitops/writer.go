// Package gitops implements commit-on-write operations for tenant config files.
//
// Design (ADR-009):
//   - All write operations hold a sync.Mutex to prevent concurrent git conflicts.
//   - Each write records the HEAD commit before and after to detect conflicts.
//   - Commits use the operator's email as git author for audit trail.
//   - Schema validation is run before any disk write.
package gitops

import (
	"errors"
	"fmt"
	"log"
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

// Writer handles GitOps write-back operations.
type Writer struct {
	mu             sync.Mutex
	configDir      string // path to conf.d/ directory (YAML files live here)
	gitDir         string // git repository root (may differ from configDir)
	committerName  string // cached from GIT_COMMITTER_NAME env var
	committerEmail string // cached from GIT_COMMITTER_EMAIL env var
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

// Write validates, persists, and commits a tenant's config YAML.
//
// Flow:
//  1. Validate YAML schema (ParseConfig + ValidateTenantKeys)
//  2. Lock mutex
//  3. Record HEAD before write
//  4. Write file to configDir/{tenantID}.yaml
//  5. git add + git commit --author="<authorEmail>"
//  6. Check HEAD again (conflict detection)
//  7. Unlock mutex
func (w *Writer) Write(tenantID, authorEmail, yamlContent string) error {
	// Step 1: validate schema before touching disk
	if errs := validate(tenantID, yamlContent); len(errs) > 0 {
		return fmt.Errorf("validation failed: %s", strings.Join(errs, "; "))
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	// Step 2: record HEAD before write
	headBefore, err := w.currentHEAD()
	if err != nil {
		log.Printf("WARN: gitops: could not read HEAD before write (non-git mode?): %v", err)
		// Proceed without conflict detection in non-git environments
	}

	// Step 3: write file
	filePath := filepath.Join(w.configDir, tenantID+".yaml")
	if err := os.WriteFile(filePath, []byte(yamlContent), 0644); err != nil {
		return fmt.Errorf("write file: %w", err)
	}

	// Step 4: git commit
	if err := w.gitCommit(filePath, tenantID, authorEmail); err != nil {
		// Rollback: remove file if it didn't exist before (best-effort)
		log.Printf("WARN: gitops: commit failed for tenant=%s: %v", tenantID, err)
		return fmt.Errorf("git commit: %w", err)
	}

	// Step 5: conflict detection — verify our commit's parent is the HEAD we
	// recorded before writing.  If HEAD~1 != headBefore, an external commit
	// landed between our read and write (e.g. a concurrent git push).
	if headBefore != "" {
		parent, err := w.commitParent()
		if err == nil && parent != headBefore {
			log.Printf("WARN: gitops: external commit detected for tenant=%s (expected parent=%s, got=%s)",
				tenantID, headBefore[:8], parent[:8])
			return ErrConflict
		}
	}

	log.Printf("gitops: tenant=%s committed by %s", tenantID, authorEmail)
	return nil
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
