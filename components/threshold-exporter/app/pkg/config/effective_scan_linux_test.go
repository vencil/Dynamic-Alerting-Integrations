package config

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// A warm resolve reads only the files the tenant's answer depends on (#1977):
// with every other file of the tree replaced by a FIFO AFTER the walk — a
// read of it blocks until a writer opens it — it still answers, and answers
// the same. The whole-tree build reads every tenant file again on a warm scan
// (they carry neither bytes nor a decoded partial), so it blocks here.
func TestResolveEffectiveFromScan_ReadsOnlyTheTenantsFiles(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	files := effectiveScanCorpus()["chain and siblings"]
	testutil.WriteTree(t, dir, files)
	ageTree(t, dir)
	want := effectiveAnswer(ResolveEffective(dir, "t1"))
	warm := warmScanOf(t, dir)
	needed := map[string]bool{"_defaults.yaml": true, "a/_defaults.yaml": true, "a/b/_defaults.yaml": true,
		"a/b/_defaults.yml": true, "a/b/t1.yaml": true}
	for rel := range files {
		if needed[rel] {
			continue
		}
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.Remove(p); err != nil {
			t.Fatal(err)
		}
		if err := syscall.Mkfifo(p, 0o644); err != nil {
			t.Fatal(err)
		}
		// Unblock a reader left on it, so a failing run does not leak one.
		t.Cleanup(func() {
			if f, err := os.OpenFile(p, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
				_ = f.Close()
			}
		})
	}
	done := make(chan string, 1)
	go func() { done <- effectiveAnswer(ResolveEffectiveFromScan(warm, "t1")) }()
	select {
	case got := <-done:
		if got != want {
			t.Fatalf("warm resolve changed its answer:\n got  %s\n want %s", got, want)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("warm resolve blocked reading a file outside the tenant's chain")
	}
}
