//go:build unix

package confd

import (
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// A FIFO named like a tenant file must be classified, not read: neither the
// open nor the read may block (a FIFO with no writer blocks a plain O_RDONLY
// open forever, and /search holds its cache mutex around this load).
func TestReadTenantFile_FIFODoesNotBlock(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := syscall.Mkfifo(filepath.Join(dir, "acme.yaml"), 0o644); err != nil {
		t.Fatalf("mkfifo: %v", err)
	}
	files, err := ListTenantFiles(dir)
	if err != nil || len(files) != 1 || files[0].ID != "acme" {
		t.Fatalf("ListTenantFiles = %+v, %v; want the FIFO listed as acme", files, err)
	}
	done := make(chan FileProblem, 1)
	go func() {
		_, problem := ReadTenantFile(dir, "acme.yaml")
		done <- problem
	}()
	select {
	case problem := <-done:
		if problem != ProblemNotRegularFile {
			t.Errorf("problem = %q, want %q", problem, ProblemNotRegularFile)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ReadTenantFile blocked on a FIFO")
	}
}
