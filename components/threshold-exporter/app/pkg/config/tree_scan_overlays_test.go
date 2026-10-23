package config

// tree_scan_overlays_test.go — ScanDirTreeFilesWith (#3206 step 10c): the
// list-only walk with many files' bytes read at their paths, a create's
// missing directories taken as made. The oracle is the same tree with the
// bytes written there (and the directories made).

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// filesShape is what parse-tree reads off a ScanDirTreeFiles walk.
func filesShape(t *testing.T, s *TreeScan) map[string]any {
	t.Helper()
	data := map[string]string{}
	for k, f := range s.Files {
		data[k] = string(f.Data)
	}
	defaults := map[string]bool{}
	for p := range s.Defaults {
		rel, err := filepath.Rel(s.AbsRoot, p)
		if err != nil {
			t.Fatal(err)
		}
		defaults[filepath.ToSlash(rel)] = true
	}
	carriers := map[string]string{}
	for d, c := range s.DefaultsCarriers().ByDir {
		rd, _ := filepath.Rel(s.AbsRoot, d)
		rc, _ := filepath.Rel(s.AbsRoot, c)
		carriers[filepath.ToSlash(rd)] = filepath.ToSlash(rc)
	}
	return map[string]any{"keys": s.Keys, "data": data, "defaults": defaults, "carriers": carriers,
		"dirs": s.Dirs, "not_files": s.NotFiles, "unreadable": s.Unreadable}
}

func writeFiles(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestScanDirTreeFilesWith_IsTheTreeWithTheFilesWritten(t *testing.T) {
	t.Parallel()
	disk := map[string]string{
		"_defaults.yaml":     "defaults:\n  a: 1\n",
		"a-b.yaml":           "tenants:\n  t1: {}\n",
		"team/t2.yaml":       "tenants:\n  t2: {}\n",
		"team/_defaults.yml": "defaults: {}\n",
	}
	overlays := []FileOverlay{
		{Rel: "a-b.yaml", Data: []byte("tenants:\n  t1:\n    x: 1\n")},    // substitute
		{Rel: "team/_defaults.yaml", Data: []byte("defaults:\n  b: 2\n")}, // a carrier the directory now selects
		{Rel: "new/deeper/t3.yaml", Data: []byte("tenants:\n  t3: {}\n")}, // two missing directories
		{Rel: "new/t4.yaml", Data: []byte("tenants:\n  t4: {}\n")},
		{Rel: "odd.yaml/t5.yaml", Data: []byte("tenants:\n  t5: {}\n")}, // a config-named directory made
		{Rel: "0.yaml", Data: []byte("tenants:\n  t0: {}\n")},           // first in walk order
	}
	dir := t.TempDir()
	writeFiles(t, dir, disk)
	got, err := ScanDirTreeFilesWith(dir, discardLogger, overlays, true)
	if err != nil {
		t.Fatal(err)
	}

	oracle := t.TempDir()
	writeFiles(t, oracle, disk)
	written := map[string]string{}
	for _, o := range overlays {
		written[o.Rel] = string(o.Data)
	}
	writeFiles(t, oracle, written)
	want, err := ScanDirTreeFiles(oracle, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	if g, w := filesShape(t, got), filesShape(t, want); !reflect.DeepEqual(g, w) {
		t.Fatalf("overlay walk\n%v\nwritten tree\n%v", g, w)
	}
	// Nothing was written.
	if _, err := os.Lstat(filepath.Join(dir, "new")); !os.IsNotExist(err) {
		t.Fatalf("the walk made a directory: %v", err)
	}
}

func TestScanDirTreeFilesWith_Refusals(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFiles(t, dir, map[string]string{"a.yaml": "tenants: {}\n"})
	for name, tc := range map[string]struct {
		overlays []FileOverlay
		mkdirs   bool
		rel      string
	}{
		"a path twice":                   {[]FileOverlay{{Rel: "b.yaml"}, {Rel: "b.yaml"}}, true, "b.yaml"},
		"a missing directory, no mkdirs": {[]FileOverlay{{Rel: "new/b.yaml"}}, false, "new/b.yaml"},
		"a file and a directory":         {[]FileOverlay{{Rel: "x.yaml"}, {Rel: "x.yaml/b.yaml"}}, true, "x.yaml"},
		"under a file":                   {[]FileOverlay{{Rel: "a.yaml/b.yaml"}}, true, "a.yaml/b.yaml"},
		"a hidden name":                  {[]FileOverlay{{Rel: ".b.yaml"}}, true, ".b.yaml"},
	} {
		_, err := ScanDirTreeFilesWith(dir, discardLogger, tc.overlays, tc.mkdirs)
		var oe *OverlayError
		if !errors.As(err, &oe) || !errors.Is(err, ErrOverlay) || oe.Rel != tc.rel {
			t.Errorf("%s: %v, want an OverlayError at %q", name, err, tc.rel)
		}
	}
}
