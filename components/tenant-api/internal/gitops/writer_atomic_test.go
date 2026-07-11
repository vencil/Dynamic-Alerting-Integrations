package gitops

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// TestWriteFileAtomic_OverwritesWithPerm verifies the happy path:
// content replaces an existing file and the result carries the
// requested permission bits (not os.CreateTemp's default 0600).
func TestWriteFileAtomic_OverwritesWithPerm(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "_groups.yaml")
	if err := os.WriteFile(path, []byte("old\n"), 0644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := writeFileAtomic(path, []byte("new\n"), 0644); err != nil {
		t.Fatalf("writeFileAtomic: %v", err)
	}

	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != "new\n" {
		t.Errorf("content = %q, want %q", got, "new\n")
	}

	// Permission bits are not enforced by the FS on Windows, so only
	// assert them where they are meaningful.
	if runtime.GOOS != "windows" {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if perm := info.Mode().Perm(); perm != 0644 {
			t.Errorf("perm = %o, want 0644 (must not inherit CreateTemp's 0600)", perm)
		}
	}
}

// TestWriteFileAtomic_LeavesNoTemp asserts that a completed write leaves
// no sibling temp file. A lingering temp ending in .yaml would be globbed
// as a bogus tenant by the conf.d scanner; the helper deliberately uses a
// .tmp suffix and removes it on the success path via os.Rename.
func TestWriteFileAtomic_LeavesNoTemp(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "db-a.yaml")

	if err := writeFileAtomic(path, []byte("tenant: db-a\n"), 0644); err != nil {
		t.Fatalf("writeFileAtomic: %v", err)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if len(names) != 1 || names[0] != "db-a.yaml" {
		t.Errorf("dir entries = %v, want exactly [db-a.yaml] (no temp litter)", names)
	}
	// Belt and braces: nothing matching the conf.d tenant glob other than
	// the target itself.
	for _, n := range names {
		if n != "db-a.yaml" && strings.HasSuffix(n, ".yaml") {
			t.Errorf("stray .yaml file %q would register as a bogus tenant", n)
		}
	}
}

// TestWriteFileAtomic_NoTornRead is the core guarantee: a concurrent
// reader either sees the whole old content or the whole new content,
// never a truncated/half-written document. With os.WriteFile (truncate
// in place) a reader can observe a zero-or-partial file; os.Rename makes
// the swap atomic. The reader asserts every observation parses as one of
// the two complete generations.
func TestWriteFileAtomic_NoTornRead(t *testing.T) {
	// The atomic-rename-over-open-file guarantee is POSIX. On Windows,
	// os.Rename onto a path a reader currently has open fails with a
	// sharing violation ("Access is denied") rather than swapping, so
	// this concurrency test cannot express the invariant there. The
	// production runtime is a Linux container, where rename(2) over an
	// open file always succeeds and the reader keeps its old inode — so
	// skip on Windows rather than weaken the assertion.
	if runtime.GOOS == "windows" {
		t.Skip("atomic rename-over-open-file is POSIX; production target is Linux")
	}
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "_groups.yaml")

	// Two distinct, sizeable contents whose partial writes would be
	// distinguishable from either whole generation.
	oldContent := []byte(strings.Repeat("a: 1\n", 500))
	newContent := []byte(strings.Repeat("b: 2\n", 500))
	if err := os.WriteFile(path, oldContent, 0644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	var wg sync.WaitGroup

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 300; i++ {
			content := oldContent
			if i%2 == 1 {
				content = newContent
			}
			if err := writeFileAtomic(path, content, 0644); err != nil {
				t.Errorf("writeFileAtomic: %v", err)
				return
			}
		}
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 3000; i++ {
			data, err := os.ReadFile(path)
			if err != nil {
				// A rename swap must never make the path briefly absent.
				t.Errorf("read observed error (torn swap?): %v", err)
				return
			}
			if !isWholeGeneration(data, oldContent, newContent) {
				t.Errorf("read observed a torn file: len=%d", len(data))
				return
			}
		}
	}()

	wg.Wait()
}

func isWholeGeneration(got, a, b []byte) bool {
	return string(got) == string(a) || string(got) == string(b)
}
