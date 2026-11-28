package gitops

// #4253: a threshold the tenant's section writes under both #1231 spellings
// is refused where this write puts the pair there — the exporter serves one
// line and folds the other away — and is the rename notice it always was
// where the file already holds the pair.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// retiredThreads is the #1231 retired spelling of mysql_threads_running.
const retiredThreads = "mysql_" + "cpu"

func addRetiredThreads(existing []byte) (string, error) {
	return string(existing) + "    " + retiredThreads + ": \"41\"\n", nil
}

func TestTwinSpelling_AWriteThatPutsBothSpellingsIsRefused(t *testing.T) {
	dir := seedTenantRepo(t)
	w := newTestWriter(dir, dir)
	head := gitOut(t, dir, "rev-parse", "HEAD")
	_, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", addRetiredThreads)
	if !errors.Is(err, ErrValidation) || !strings.Contains(err.Error(), "are one threshold (#1231 rename)") {
		t.Fatalf("WriteMerged: err = %v, want the ErrValidation naming both spellings", err)
	}
	if got := gitOut(t, dir, "rev-parse", "HEAD"); got != head {
		t.Errorf("WriteMerged committed %s", got)
	}
}

func TestTwinSpelling_APairTheFileHoldsDoesNotBlockAnotherEdit(t *testing.T) {
	dir := seedTenantRepo(t)
	path := filepath.Join(dir, "db-a.yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	held, _ := addRetiredThreads(raw)
	if err := os.WriteFile(path, []byte(held), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "commit", "-am", "the file already holds both spellings")
	w := newTestWriter(dir, dir)
	if _, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", addSilentMode); err != nil {
		t.Fatalf("WriteMerged refused an edit of a tenant whose file already holds both spellings: %v", err)
	}
}
