package config

// tree_scan_overlay_test.go — ScanOptions.Overlay (#2906): the walk with one
// file's bytes read at one path. The oracle is the same tree with the bytes
// written there; the zero ScanOptions is ScanDirTree itself.

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

var overlayTree = map[string]string{
	"_defaults.yaml":     "defaults:\n  a: 1\n",
	"a-b.yaml":           "tenants:\n  t1: {}\n",
	"a/x.yaml":           "tenants:\n  t2: {}\n",
	"team/tenant-3.yaml": "tenants:\n  t3: {}\n",
}

// scanShape is what a consumer reads off a scan: files in walk order
// (Keys is sorted; the walk order is the duplicate's), their hashes and
// declarations, the defaults set relative to the root.
func scanShape(t *testing.T, s *TreeScan) map[string]any {
	t.Helper()
	files := map[string][2]any{}
	for k, f := range s.Files {
		files[k] = [2]any{f.Hash, f.TenantIDs}
	}
	defaults := map[string]bool{}
	for p := range s.Defaults {
		rel, err := filepath.Rel(s.AbsRoot, p)
		if err != nil {
			t.Fatal(err)
		}
		defaults[filepath.ToSlash(rel)] = true
	}
	return map[string]any{"keys": s.Keys, "files": files, "defaults": defaults,
		"composite": s.Composite, "conflict": s.Conflict != nil}
}

func scanOf(t *testing.T, files map[string]string, opts ScanOptions) *TreeScan {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files { // testutil.WriteTree makes "" a directory
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	s, err := ScanDirTreeWith(dir, nil, nil, discardLogger, opts)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestScanDirTreeWith_ZeroOptionsIsScanDirTree(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, overlayTree)
	plain, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	with, err := ScanDirTreeWith(dir, nil, nil, discardLogger, ScanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(scanShape(t, plain), scanShape(t, with)) {
		t.Errorf("zero ScanOptions differs from ScanDirTree")
	}
}

func TestScanDirTreeWith_OverlayEqualsWrittenTree(t *testing.T) {
	t.Parallel()
	for name, ov := range map[string]FileOverlay{
		"substitute tenant file":  {Rel: "a/x.yaml", Data: []byte("tenants:\n  t2: {x: 2}\n  t9: {}\n")},
		"substitute carrier":      {Rel: "_defaults.yaml", Data: []byte("defaults:\n  a: 5\n")},
		"create carrier":          {Rel: "team/_defaults.yaml", Data: []byte("defaults:\n  a: 7\n")},
		"create first in its dir": {Rel: "a/0.yaml", Data: []byte("tenants:\n  t2: {}\n")},
		"create at the root":      {Rel: "a.yaml", Data: []byte("tenants:\n  t1: {}\n")},
		"create empty":            {Rel: "team/empty.yaml", Data: []byte{}},
	} {
		got := scanOf(t, overlayTree, ScanOptions{Overlay: &FileOverlay{Rel: ov.Rel, Data: ov.Data}})
		written := map[string]string{}
		for k, v := range overlayTree {
			written[k] = v
		}
		written[ov.Rel] = string(ov.Data)
		want := scanOf(t, written, ScanOptions{})
		if !reflect.DeepEqual(scanShape(t, got), scanShape(t, want)) {
			t.Errorf("%s: overlay scan differs from the written tree:\n got %v\nwant %v", name, scanShape(t, got), scanShape(t, want))
		}
		if (got.Conflict == nil) != (want.Conflict == nil) ||
			(got.Conflict != nil && (got.Conflict.TenantID != want.Conflict.TenantID ||
				filepath.Base(got.Conflict.PathA) != filepath.Base(want.Conflict.PathA) ||
				filepath.Base(got.Conflict.PathB) != filepath.Base(want.Conflict.PathB))) {
			t.Errorf("%s: conflict %v, want %v (walk order)", name, got.Conflict, want.Conflict)
		}
		if f := got.Files[ov.Rel]; f == nil || f.Data == nil {
			t.Errorf("%s: the overlaid file holds no cached bytes", name)
		}
	}
}

func TestScanDirTreeWith_OverlayRefusalsWrapErrOverlay(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteTree(t, dir, overlayTree)
	for _, rel := range []string{"", "/abs.yaml", "../x.yaml", "./a.yaml", "a//x.yaml", "x.txt",
		".h.yaml", ".d/x.yaml", "a", "nope/x.yaml", "a-b.yaml/x.yaml"} {
		_, err := ScanDirTreeWith(dir, nil, nil, discardLogger, ScanOptions{Overlay: &FileOverlay{Rel: rel, Data: []byte("{}")}})
		if !errors.Is(err, ErrOverlay) {
			t.Errorf("%q: err = %v, want ErrOverlay", rel, err)
		}
	}
}

func TestWalkOrderLess(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"a/x.yaml", "a-b.yaml", true}, // directory `a` is listed before `a-b.yaml`
		{"a-b.yaml", "a/x.yaml", false},
		{"_defaults.yaml", "a.yaml", true},
		{"b/c.yaml", "b/c/d.yaml", false}, // directory `c` before `c.yaml`
	} {
		if got := walkOrderLess(c.a, c.b); got != c.want {
			t.Errorf("walkOrderLess(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}
