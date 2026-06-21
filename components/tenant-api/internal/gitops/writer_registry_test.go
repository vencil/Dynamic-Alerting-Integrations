package gitops

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// TestMutateConfigFile_CommitsTransformedBytes: a transform that returns
// new bytes is written to the named conf.d file and committed.
func TestMutateConfigFile_CommitsTransformedBytes(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)

	const filename = "_account_registry.yaml"
	err := w.MutateConfigFile(context.Background(), filename, "account-registry", "ops@example.com",
		func(current []byte) ([]byte, error) {
			if current != nil {
				t.Errorf("first call current = %q, want nil (file absent)", current)
			}
			return []byte("schema_version: v1\nnext_account_id: 1001\n"), nil
		})
	if err != nil {
		t.Fatalf("MutateConfigFile: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(dir, filename))
	if err != nil {
		t.Fatalf("read written file: %v", err)
	}
	if string(got) != "schema_version: v1\nnext_account_id: 1001\n" {
		t.Errorf("file content = %q", got)
	}
}

// TestMutateConfigFile_ReadsCurrentBytes: a second mutation sees the bytes
// the first committed — proving the in-lock read-modify-write the allocator
// relies on for its monotonic counter.
func TestMutateConfigFile_ReadsCurrentBytes(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)
	const filename = "_account_registry.yaml"

	if err := w.MutateConfigFile(context.Background(), filename, "account-registry", "ops@example.com",
		func([]byte) ([]byte, error) { return []byte("v1-first\n"), nil }); err != nil {
		t.Fatalf("first mutate: %v", err)
	}

	var sawCurrent string
	if err := w.MutateConfigFile(context.Background(), filename, "account-registry", "ops@example.com",
		func(current []byte) ([]byte, error) {
			sawCurrent = string(current)
			return []byte("v1-second\n"), nil
		}); err != nil {
		t.Fatalf("second mutate: %v", err)
	}
	if sawCurrent != "v1-first\n" {
		t.Errorf("second transform saw current = %q, want the first commit's bytes", sawCurrent)
	}
}

// TestMutateConfigFile_NoOpDoesNotCommit: a transform returning (nil, nil)
// writes nothing and produces no commit (the idempotent path).
func TestMutateConfigFile_NoOpDoesNotCommit(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)
	const filename = "_account_registry.yaml"

	headBefore, err := w.currentHEAD()
	if err != nil {
		t.Fatalf("HEAD before: %v", err)
	}
	if err := w.MutateConfigFile(context.Background(), filename, "account-registry", "ops@example.com",
		func([]byte) ([]byte, error) { return nil, nil }); err != nil {
		t.Fatalf("no-op mutate: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, filename)); !os.IsNotExist(err) {
		t.Errorf("no-op mutate created the file (stat err = %v)", err)
	}
	headAfter, err := w.currentHEAD()
	if err != nil {
		t.Fatalf("HEAD after: %v", err)
	}
	if headBefore != headAfter {
		t.Errorf("no-op mutate moved HEAD %s → %s", headBefore, headAfter)
	}
}

// TestMutateConfigFile_ClampsToBasename: a path-bearing filename is clamped
// to its basename, so a write can never escape configDir even if a future
// caller passes a path — turns the "bare name" convention into an enforced
// invariant (defence-in-depth; today's callers pass a constant).
func TestMutateConfigFile_ClampsToBasename(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)

	if err := w.MutateConfigFile(context.Background(), "../escape.yaml", "account-registry", "ops@example.com",
		func([]byte) ([]byte, error) { return []byte("x\n"), nil }); err != nil {
		t.Fatalf("MutateConfigFile: %v", err)
	}
	// The write lands inside configDir under the basename...
	if _, err := os.Stat(filepath.Join(dir, "escape.yaml")); err != nil {
		t.Errorf("clamped write should land in configDir/escape.yaml: %v", err)
	}
	// ...and never in the parent directory it tried to traverse to.
	if _, err := os.Stat(filepath.Join(filepath.Dir(dir), "escape.yaml")); !os.IsNotExist(err) {
		t.Errorf("write escaped configDir into the parent (stat err = %v)", err)
	}
}

// TestMutateConfigFile_TransformErrorAborts: a transform error propagates
// and nothing is written.
func TestMutateConfigFile_TransformErrorAborts(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)
	const filename = "_account_registry.yaml"

	sentinel := errors.New("boom")
	err := w.MutateConfigFile(context.Background(), filename, "account-registry", "ops@example.com",
		func([]byte) ([]byte, error) { return nil, sentinel })
	if !errors.Is(err, sentinel) {
		t.Fatalf("MutateConfigFile err = %v, want sentinel", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, filename)); !os.IsNotExist(statErr) {
		t.Errorf("aborted mutate created the file (stat err = %v)", statErr)
	}
}

// TestHistoricalMaxUint: the revert-proof high-water mark is the LARGEST value
// the counter ever committed — not the current (possibly reverted-lower) value.
func TestHistoricalMaxUint(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)
	const filename = "_account_registry.yaml"
	ctx := context.Background()

	// No history yet → 0 (a never-committed path is not an error).
	if m, err := w.HistoricalMaxUint(ctx, filename, "next_account_id"); err != nil || m != 0 {
		t.Fatalf("empty history = (%d, %v), want (0, nil)", m, err)
	}

	commit := func(n string) {
		t.Helper()
		if err := w.MutateConfigFile(ctx, filename, "account-registry", "ops@example.com",
			func([]byte) ([]byte, error) { return []byte("next_account_id: " + n + "\n"), nil }); err != nil {
			t.Fatalf("commit %s: %v", n, err)
		}
	}
	commit("1000") // counter rises…
	commit("1005") // …to 1005…
	commit("1002") // …then a "revert" lowers it back to 1002.

	m, err := w.HistoricalMaxUint(ctx, filename, "next_account_id")
	if err != nil {
		t.Fatalf("HistoricalMaxUint: %v", err)
	}
	if m != 1005 {
		t.Errorf("historical max = %d, want 1005 (the pre-revert high, not the current 1002)", m)
	}
}
