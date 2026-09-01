package gitops

// #1673 write-plane coverage: a tenant whose config file is spelled `<id>.yml`
// must be UPDATED IN PLACE, not shadowed by a freshly created `<id>.yaml`.
//
// Before the shared resolver, every writer site joined a hardcoded
// `<id>.yaml`: readMergeValidate read a file that did not exist, concluded
// "new tenant", and commitFileChange wrote a second file next to the real one.
// The listing plane then reported the tenant twice — with two different sets
// of metadata — and the next whole-file PUT silently dropped whichever file
// lost.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/confd"
)

func seedCommitted(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "add", name)
	gitRun(t, dir, "commit", "-m", "seed "+name)
}

func TestWrite_YmlTenantIsUpdatedInPlace(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	const tenant = "db-a"
	seedCommitted(t, dir, tenant+".yml", "tenants:\n  "+tenant+":\n    mysql_threads_running: \"70\"\n")

	w := NewWriter(dir, dir)
	const updated = "tenants:\n  " + tenant + ":\n    mysql_threads_running: \"90\"\n"
	if _, err := w.Write(context.Background(), tenant, "op@example.com", updated); err != nil {
		t.Fatalf("Write: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(dir, tenant+".yml"))
	if err != nil {
		t.Fatalf("the .yml file should still be the tenant's file: %v", err)
	}
	if !strings.Contains(string(got), `"90"`) {
		t.Errorf(".yml was not updated in place; content = %q", got)
	}
	if _, err := os.Stat(filepath.Join(dir, tenant+".yaml")); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("a second file %s.yaml was created beside the tenant's real file (err=%v)", tenant, err)
	}
}

func TestWrite_AmbiguousSpellingIsRefused(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	const tenant = "db-a"
	body := "tenants:\n  " + tenant + ":\n    mysql_threads_running: \"70\"\n"
	seedCommitted(t, dir, tenant+".yml", body)
	seedCommitted(t, dir, tenant+".yaml", body)

	w := NewWriter(dir, dir)
	_, err := w.Write(context.Background(), tenant, "op@example.com", body)
	if !errors.Is(err, confd.ErrAmbiguousTenantFile) {
		t.Fatalf("Write err = %v, want confd.ErrAmbiguousTenantFile", err)
	}
}

func TestDiff_ReadsTheTenantsActualFile(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	const tenant = "db-a"
	seedCommitted(t, dir, tenant+".yml", "tenants:\n  "+tenant+":\n    mysql_threads_running: \"70\"\n")

	w := NewWriter(dir, dir)
	diff, err := w.Diff(tenant, "tenants:\n  "+tenant+":\n    mysql_threads_running: \"90\"\n")
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	// A non-empty diff is NOT enough: when Diff reads a nonexistent <id>.yaml it
	// reports the whole proposal as a new file (all `+` lines), which is also
	// non-empty. The discriminating evidence is the OLD value — it can only
	// appear if the tenant's real .yml file was read.
	if !strings.Contains(diff, "70") {
		t.Fatalf("Diff did not read the tenant's .yml file — the old value is absent, so this is a new-file diff, not a modification. diff=%q", diff)
	}
}

// CodeRabbit finding on PR #1682: WritePR resolved the tenant's file BEFORE
// checkoutBaseClean + resolveFreshBaseRef, then wrote that pre-checkout path
// after the branch was cut. When the fresh base carries a rename, the write
// recreates the OLD spelling beside the new one — the exact duplicate #1673
// exists to prevent, reintroduced by the very change that fixes it.
//
// Scenario: the tenant ships as `db-a.yml`; upstream renames it to
// `db-a.yaml` and merges; the writer's long-lived clone is still on the stale
// base when the PR is opened.
func TestWritePR_ReResolvesAfterFreshBaseRename(t *testing.T) {
	remoteDir := initBareRemoteOnMain(t)

	authorDir := t.TempDir()
	gitClone(t, remoteDir, authorDir)
	gitRun(t, authorDir, "config", "user.email", "a@a.com")
	gitRun(t, authorDir, "config", "user.name", "A")
	writeFileInDir(t, authorDir, "db-a.yml", validTenantYAML)
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", "seed db-a as .yml")
	gitRun(t, authorDir, "push", "origin", "main")

	// The writer's clone: fetched once, then goes stale.
	dir := t.TempDir()
	gitClone(t, remoteDir, dir)
	gitRun(t, dir, "config", "user.email", "t@t.com")
	gitRun(t, dir, "config", "user.name", "T")

	// Upstream renames the tenant's file and merges. The writer's local base
	// still has the OLD spelling.
	gitRun(t, authorDir, "mv", "db-a.yml", "db-a.yaml")
	gitRun(t, authorDir, "commit", "-m", "rename db-a.yml -> db-a.yaml")
	gitRun(t, authorDir, "push", "origin", "main")

	w := NewWriter(dir, dir)
	res, err := w.WritePR(context.Background(), "db-a", "bob@example.com",
		"tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	if err != nil {
		t.Fatalf("WritePR: %v", err)
	}

	// The branch must carry ONE file for this tenant — the fresh base's
	// spelling — and must not have resurrected the pre-rename name.
	files := gitOut(t, dir, "ls-tree", "--name-only", "refs/remotes/origin/"+res.BranchName)
	if !strings.Contains(files, "db-a.yaml") {
		t.Errorf("branch does not contain db-a.yaml; tree = %q", files)
	}
	if strings.Contains(files, "db-a.yml\n") || strings.HasSuffix(files, "db-a.yml") {
		t.Errorf("branch resurrected the pre-rename db-a.yml — WritePR wrote a stale path; tree = %q", files)
	}
	// And the only delta vs the fresh base is that one file.
	diff := gitOut(t, dir, "diff", "--name-only", "origin/main", "refs/remotes/origin/"+res.BranchName)
	if diff != "db-a.yaml" {
		t.Errorf("branch vs origin/main changed files = %q, want only db-a.yaml", diff)
	}
}
