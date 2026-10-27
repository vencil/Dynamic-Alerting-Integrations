//go:build unix

package main

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// TestWrite_ACopyNeverReadsAFIFO: a FIFO source with no writer (which a
// plain read blocks on for ever) is a caller error at once, named, and never
// read.
func TestWrite_ACopyNeverReadsAFIFO(t *testing.T) {
	root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
	before := snapshot(t, root)
	src := filepath.Join(t.TempDir(), "p.yaml")
	if err := syscall.Mkfifo(src, 0o600); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	done := make(chan struct{})
	var code int
	var res writeResult
	go func() {
		defer close(done)
		code, res = runBatch(t, writeCmd, batchOf(copyEntry("p.yaml", src, false)), "--config-dir", root)
	}()
	select {
	case <-done:
	case <-time.After(30 * time.Second):
		// Unblock the reader so the goroutine can end, then fail.
		if f, err := os.OpenFile(src, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
		t.Fatal("the copy blocked on the FIFO")
	}
	if code != exitCallerErr || reasonOf(res.Files[0]) != writeReasonBadSource ||
		!strings.Contains(*res.Files[0].Message, src) {
		t.Fatalf("exit %d %v %v", code, statuses(res), res.Files[0].Message)
	}
	sameTree(t, before, snapshot(t, root))
}
