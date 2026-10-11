package gitops

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

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
		if err := os.WriteFile(filepath.Join(dir, filename), []byte("next_account_id: "+n+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		for _, args := range [][]string{{"add", filename}, {"commit", "-m", "registry " + n}} {
			if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
				t.Fatalf("git %v: %v\n%s", args, err, out)
			}
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
