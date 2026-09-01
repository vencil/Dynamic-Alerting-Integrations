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
	if diff == "" {
		t.Fatal("Diff was empty — it read a nonexistent <id>.yaml instead of the tenant's .yml file")
	}
}
