//go:build unix

package main

import (
	"bytes"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"
)

// TestParseTree_FIFOIsNotRead: a FIFO named like a config file (or a link to
// one) is not_regular_file, and the run ends — the exporter's own walk reads
// it and blocks; parse-tree's never reads a non-regular entry.
func TestParseTree_FIFOIsNotRead(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"ok.yaml": "a: 1\n"})
	if err := syscall.Mkfifo(filepath.Join(root, "fifo.yaml"), 0o600); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	done := make(chan struct{})
	var code int
	var out, errOut bytes.Buffer
	go func() {
		defer close(done)
		code = runParseTree([]string{"--config-dir", root}, strings.NewReader(""), &out, &errOut)
	}()
	select {
	case <-done:
	case <-time.After(30 * time.Second):
		t.Fatal("parse-tree blocked on a FIFO")
	}
	if code != exitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	_, doc, _ := runPT(t, "", "--config-dir", root)
	if got := unreadableKinds(doc); !reflect.DeepEqual(got, map[string]string{"fifo.yaml": "not_regular_file"}) {
		t.Fatalf("unreadable %v", got)
	}
	if r := readersOf(doc)["fifo.yaml"]; !reflect.DeepEqual(r, []string{"exporter", "generator"}) {
		t.Fatalf("readers %v", r)
	}
}
