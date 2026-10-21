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

// TestParseTreeList_SpecialFileIsNotOpened: a FIFO in the tree is listed as
// not_regular_file and the command returns (an open of a FIFO with no writer
// would block). That no file is read at all is config.ScanDirTreeNames'
// promise, pinned there (TestScanDirTreeNames_ReadsNothing).
func TestParseTreeList_SpecialFileIsNotOpened(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"t1.yaml": "tenants: {t1: {}}\n"})
	if err := syscall.Mkfifo(filepath.Join(root, "pipe.yaml"), 0o600); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	_, list, _ := runPTList(t, "", "--config-dir", root)
	got := map[string]*parseTreeListProblem{}
	for _, e := range list.Entries {
		got[e.Path] = e.Unreadable
	}
	if p, ok := got["t1.yaml"]; !ok || p != nil {
		t.Errorf("t1.yaml: listed %v, unreadable %v; want listed and readable", ok, p)
	}
	if p := got["pipe.yaml"]; p == nil || p.Kind != "not_regular_file" {
		t.Errorf("pipe.yaml: unreadable %v, want not_regular_file", p)
	}
}
