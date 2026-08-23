package gitops

// Optimistic-concurrency coverage for WriteIfUnchanged: the base-hash check
// runs under the writer lock, immediately before the commit, and a failed
// check must leave the file exactly as it was.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const (
	preconditionSeed   = "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"
	preconditionUpdate = "tenants:\n  db-a:\n    mysql_threads_running: \"75\"\n"
)

// seedTenant writes an initial tenant file through the writer and returns its
// path plus the source hash a client would have read back from GET.
func seedTenant(t *testing.T, w *Writer, dir, tenantID string) (path, hash string) {
	t.Helper()
	if _, err := w.Write(context.Background(), tenantID, "op@example.com", preconditionSeed); err != nil {
		t.Fatalf("seed write: %v", err)
	}
	path = filepath.Join(dir, tenantID+".yaml")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read seeded file: %v", err)
	}
	return path, cfg.ComputeSourceHash(raw)
}

func TestWriteIfUnchanged_CurrentBaseWrites(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)
	path, hash := seedTenant(t, w, dir, "db-a")

	if _, err := w.WriteIfUnchanged(context.Background(), "db-a", "op@example.com",
		preconditionUpdate, hash); err != nil {
		t.Fatalf("write with the current base hash: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != preconditionUpdate {
		t.Errorf("file = %q, want the updated body", got)
	}
}

func TestWriteIfUnchanged_StaleBaseRejectsAndLeavesFileAlone(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)
	path, stale := seedTenant(t, w, dir, "db-a")

	// Somebody else's write lands first; `stale` now describes nothing.
	if _, err := w.Write(context.Background(), "db-a", "other@example.com",
		"tenants:\n  db-a:\n    mysql_threads_running: \"90\"\n"); err != nil {
		t.Fatalf("competing write: %v", err)
	}
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read after competing write: %v", err)
	}

	_, err = w.WriteIfUnchanged(context.Background(), "db-a", "op@example.com",
		preconditionUpdate, stale)
	if !errors.Is(err, ErrPrecondition) {
		t.Fatalf("stale base: err = %v, want ErrPrecondition", err)
	}
	var pre *PreconditionError
	if !errors.As(err, &pre) {
		t.Fatalf("err = %T, want *PreconditionError", err)
	}
	if pre.Expected != stale {
		t.Errorf("Expected = %q, want the stale hash %q", pre.Expected, stale)
	}
	if want := cfg.ComputeSourceHash(before); pre.Current != want {
		t.Errorf("Current = %q, want the on-disk hash %q", pre.Current, want)
	}

	// The rejected write must not have touched the file — a precondition that
	// reports failure after writing would be worse than none at all.
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read after rejection: %v", err)
	}
	if string(after) != string(before) {
		t.Errorf("file changed despite the rejection:\n got %q\nwant %q", after, before)
	}
}

// An empty base hash is a caller bug, not "no precondition": WriteIfUnchanged
// exists to enforce one, so it must never fall back to an unconditional write.
func TestWriteIfUnchanged_EmptyBaseIsRejected(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)
	path, _ := seedTenant(t, w, dir, "db-a")

	_, err := w.WriteIfUnchanged(context.Background(), "db-a", "op@example.com", preconditionUpdate, "")
	if !errors.Is(err, ErrPrecondition) {
		t.Fatalf("empty base hash: err = %v, want ErrPrecondition", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != preconditionSeed {
		t.Errorf("file = %q, want the untouched seed", got)
	}
}

// A base hash quoted for a tenant file that is not there is just as stale as an
// outdated one; Current is empty because there is nothing to hash.
func TestWriteIfUnchanged_MissingFileReportsEmptyCurrent(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)

	_, err := w.WriteIfUnchanged(context.Background(), "db-a", "op@example.com",
		preconditionUpdate, "0123456789abcdef")
	var pre *PreconditionError
	if !errors.As(err, &pre) {
		t.Fatalf("err = %v (%T), want *PreconditionError", err, err)
	}
	if pre.Current != "" {
		t.Errorf("Current = %q, want empty for a missing tenant file", pre.Current)
	}
	if _, statErr := os.Stat(filepath.Join(dir, "db-a.yaml")); !os.IsNotExist(statErr) {
		t.Errorf("rejected write created the file: stat err = %v", statErr)
	}
}
