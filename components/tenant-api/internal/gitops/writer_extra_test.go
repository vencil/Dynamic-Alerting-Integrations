package gitops

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// --- Validate extended tests ---

func TestValidate_ValidConfig(t *testing.T) {
	yaml := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	errs := validate("db-a", yaml)
	if len(errs) != 0 {
		t.Errorf("expected no errors, got: %v", errs)
	}
}

func TestValidate_MultipleTenants(t *testing.T) {
	yaml := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n  db-b:\n    _silent_mode: \"critical\"\n"
	errs := validate("db-a", yaml)
	if len(errs) != 0 {
		t.Errorf("expected no errors for db-a, got: %v", errs)
	}
	errs = validate("db-b", yaml)
	if len(errs) != 0 {
		t.Errorf("expected no errors for db-b, got: %v", errs)
	}
}

func TestValidate_InvalidYAML(t *testing.T) {
	errs := validate("db-a", "{{not yaml")
	if len(errs) == 0 {
		t.Error("expected errors for invalid YAML")
	}
	if !strings.Contains(errs[0], "invalid YAML") {
		t.Errorf("expected 'invalid YAML' in error, got: %s", errs[0])
	}
}

func TestValidate_MissingTenantSection(t *testing.T) {
	yaml := "tenants:\n  db-b:\n    cpu: \"80\"\n"
	errs := validate("db-a", yaml)
	if len(errs) == 0 {
		t.Error("expected error for missing tenant section")
	}
	if !strings.Contains(errs[0], "tenants.db-a") {
		t.Errorf("expected error about tenants.db-a, got: %s", errs[0])
	}
}

func TestValidate_EmptyContent(t *testing.T) {
	errs := validate("db-a", "")
	if len(errs) == 0 {
		t.Error("expected error for empty content")
	}
}

func TestValidate_NoTenantsKey(t *testing.T) {
	yaml := "defaults:\n  cpu: 80\n"
	errs := validate("db-a", yaml)
	if len(errs) == 0 {
		t.Error("expected error when tenants key is missing")
	}
}

// --- NewWriter extended tests ---

func TestNewWriter_BothDirs(t *testing.T) {
	w := NewWriter("/config", "/git")
	if w.configDir != "/config" {
		t.Errorf("configDir = %q, want /config", w.configDir)
	}
	if w.gitDir != "/git" {
		t.Errorf("gitDir = %q, want /git", w.gitDir)
	}
}

func TestNewWriter_EmptyGitDir(t *testing.T) {
	w := NewWriter("/config", "")
	if w.gitDir != "/config" {
		t.Errorf("gitDir should default to configDir, got %q", w.gitDir)
	}
}

func TestNewWriter_ReadsEnvVars(t *testing.T) {
	t.Setenv("GIT_COMMITTER_NAME", "Test Bot")
	t.Setenv("GIT_COMMITTER_EMAIL", "bot@test.com")

	w := NewWriter("/config", "")
	if w.committerName != "Test Bot" {
		t.Errorf("committerName = %q, want 'Test Bot'", w.committerName)
	}
	if w.committerEmail != "bot@test.com" {
		t.Errorf("committerEmail = %q, want 'bot@test.com'", w.committerEmail)
	}
}

// --- Write tests ---

func TestWrite_ValidationFailure(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir, dir)

	// Invalid YAML should fail validation before touching disk
	err := w.Write("db-a", "test@example.com", "{{invalid yaml")
	if err == nil {
		t.Error("expected error for invalid YAML")
	}
	if !strings.Contains(err.Error(), "validation failed") {
		t.Errorf("expected 'validation failed' in error, got: %v", err)
	}

	// File should not have been created
	_, statErr := os.Stat(filepath.Join(dir, "db-a.yaml"))
	if statErr == nil {
		t.Error("file should not exist after validation failure")
	}
}

func TestWrite_MissingTenantSection(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir, dir)

	err := w.Write("db-a", "test@example.com", "tenants:\n  db-b:\n    cpu: \"80\"\n")
	if err == nil {
		t.Error("expected error for missing tenant section")
	}
	if !strings.Contains(err.Error(), "validation failed") {
		t.Errorf("expected 'validation failed' in error, got: %v", err)
	}
}

// --- Diff extended tests ---

func TestDiff_NewFile(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir, "")

	diff, err := w.Diff("new-tenant", "line1\nline2\n")
	if err != nil {
		t.Fatalf("Diff returned error: %v", err)
	}
	if !strings.Contains(diff, "+line1") {
		t.Errorf("expected '+line1' in diff, got: %s", diff)
	}
	if !strings.Contains(diff, "+line2") {
		t.Errorf("expected '+line2' in diff, got: %s", diff)
	}
}

func TestDiff_IdenticalContent(t *testing.T) {
	dir := t.TempDir()
	content := "tenants:\n  db-a:\n    cpu: \"80\"\n"
	if err := os.WriteFile(filepath.Join(dir, "db-a.yaml"), []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	w := NewWriter(dir, "")
	diff, err := w.Diff("db-a", content)
	if err != nil {
		t.Fatalf("Diff returned error: %v", err)
	}
	if diff != "" {
		t.Errorf("expected empty diff for identical content, got: %s", diff)
	}
}

func TestDiff_ModifiedContent(t *testing.T) {
	dir := t.TempDir()
	original := "tenants:\n  db-a:\n    cpu: \"80\"\n"
	if err := os.WriteFile(filepath.Join(dir, "db-a.yaml"), []byte(original), 0644); err != nil {
		t.Fatal(err)
	}

	w := NewWriter(dir, "")
	proposed := "tenants:\n  db-a:\n    cpu: \"90\"\n"
	diff, err := w.Diff("db-a", proposed)
	if err != nil {
		t.Fatalf("Diff returned error: %v", err)
	}
	if diff == "" {
		t.Error("expected non-empty diff for modified content")
	}
}

func TestDiff_EmptyProposed(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir, "")

	// New file with empty content
	diff, err := w.Diff("empty-tenant", "")
	if err != nil {
		t.Fatalf("Diff returned error: %v", err)
	}
	// Should show a single "+" for the empty string split
	if !strings.Contains(diff, "+") {
		t.Errorf("expected additions in diff, got: %q", diff)
	}
}

// --- Write with real git repo ---

func initGitRepo(t *testing.T, dir string) {
	t.Helper()
	cmds := [][]string{
		{"git", "-C", dir, "init"},
		{"git", "-C", dir, "config", "user.email", "test@test.com"},
		{"git", "-C", dir, "config", "user.name", "Test"},
		{"git", "-C", dir, "commit", "--allow-empty", "-m", "initial"},
	}
	for _, args := range cmds {
		cmd := exec.Command(args[0], args[1:]...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Skipf("git command %v failed: %v\n%s", args, err, string(out))
		}
	}
}

func TestWrite_InGitRepo(t *testing.T) {
	dir := t.TempDir()
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)

	yamlContent := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	err := w.Write("db-a", "test@example.com", yamlContent)
	if err != nil {
		t.Fatalf("Write returned error: %v", err)
	}

	// Verify file was written
	data, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	if string(data) != yamlContent {
		t.Errorf("file content = %q, want %q", string(data), yamlContent)
	}
}

func TestWrite_UpdateExistingInGitRepo(t *testing.T) {
	dir := t.TempDir()
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)

	// First write
	yaml1 := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	if err := w.Write("db-a", "test@example.com", yaml1); err != nil {
		t.Fatalf("first Write: %v", err)
	}

	// Second write (update)
	yaml2 := "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"
	if err := w.Write("db-a", "test@example.com", yaml2); err != nil {
		t.Fatalf("second Write: %v", err)
	}

	data, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(data) != yaml2 {
		t.Errorf("file content = %q, want %q", string(data), yaml2)
	}
}

func TestWrite_DifferentTenants(t *testing.T) {
	dir := t.TempDir()
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)

	yaml1 := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	if err := w.Write("db-a", "alice@example.com", yaml1); err != nil {
		t.Fatalf("Write db-a: %v", err)
	}

	yaml2 := "tenants:\n  db-b:\n    _silent_mode: \"critical\"\n"
	if err := w.Write("db-b", "bob@example.com", yaml2); err != nil {
		t.Fatalf("Write db-b: %v", err)
	}

	for _, name := range []string{"db-a.yaml", "db-b.yaml"} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			t.Errorf("file %s should exist: %v", name, err)
		}
	}
}

func TestWrite_AuthorEmailParsing(t *testing.T) {
	dir := t.TempDir()
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)

	// Email with @ should extract name from prefix
	yaml := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	err := w.Write("db-a", "alice.smith@example.com", yaml)
	if err != nil {
		t.Fatalf("Write: %v", err)
	}

	// Check git log for author
	cmd := exec.Command("git", "-C", dir, "log", "--format=%an <%ae>", "-1")
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("git log: %v", err)
	}
	author := strings.TrimSpace(string(out))
	if !strings.Contains(author, "alice.smith") {
		t.Errorf("expected author to contain 'alice.smith', got: %s", author)
	}
}

func TestWrite_CommitterFromEnv(t *testing.T) {
	// When GIT_COMMITTER_NAME/EMAIL are set, they're used as committer identity
	dir := t.TempDir()
	initGitRepo(t, dir)

	t.Setenv("GIT_COMMITTER_NAME", "DA Portal Bot")
	t.Setenv("GIT_COMMITTER_EMAIL", "bot@da.local")

	w := NewWriter(dir, dir)

	yaml := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	if err := w.Write("db-a", "operator@example.com", yaml); err != nil {
		t.Fatalf("Write: %v", err)
	}

	// Verify committer identity from git log
	cmd := exec.Command("git", "-C", dir, "log", "--format=%cn <%ce>", "-1")
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("git log: %v", err)
	}
	committer := strings.TrimSpace(string(out))
	if !strings.Contains(committer, "DA Portal Bot") {
		t.Errorf("expected committer 'DA Portal Bot', got: %s", committer)
	}
	if !strings.Contains(committer, "bot@da.local") {
		t.Errorf("expected committer email 'bot@da.local', got: %s", committer)
	}
}

// --- currentHEAD / commitParent tests ---

func TestCurrentHEAD_InGitRepo(t *testing.T) {
	dir := t.TempDir()
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)
	head, err := w.currentHEAD()
	if err != nil {
		t.Fatalf("currentHEAD: %v", err)
	}
	if len(head) < 7 {
		t.Errorf("expected commit hash, got: %q", head)
	}
}

func TestCurrentHEAD_NotGitRepo(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir, dir)
	_, err := w.currentHEAD()
	if err == nil {
		t.Error("expected error for non-git directory")
	}
}

func TestCommitParent_InGitRepo(t *testing.T) {
	dir := t.TempDir()
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)

	// Write to create a second commit
	yaml := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	if err := w.Write("db-a", "test@example.com", yaml); err != nil {
		t.Fatalf("Write: %v", err)
	}

	parent, err := w.commitParent()
	if err != nil {
		t.Fatalf("commitParent: %v", err)
	}
	if len(parent) < 7 {
		t.Errorf("expected parent hash, got: %q", parent)
	}
}

// --- ErrConflict tests ---

func TestErrConflict_IsError(t *testing.T) {
	if ErrConflict.Error() == "" {
		t.Error("ErrConflict should have a non-empty message")
	}
	if !strings.Contains(ErrConflict.Error(), "conflict") {
		t.Errorf("ErrConflict message should contain 'conflict', got: %s", ErrConflict.Error())
	}
}
