package config

import (
	"os"
	"path/filepath"
	"testing"
)

// IsScannedPath is the walker's read predicate restated for a relative path
// (#1988: tenant-api asks it about untracked files). Pinned against the walk
// itself, so the two cannot drift.
func TestIsScannedPath_MatchesTheWalk(t *testing.T) {
	t.Parallel()
	cases := map[string]bool{
		"t-a.yaml":             true,
		"T-B.YML":              true,
		"_defaults.yaml":       true,
		"team/t-c.yaml":        true,
		".hidden.yaml":         false,
		".git/x.yaml":          false,
		"team/.cache/t-d.yaml": false,
		"t-e.yaml.tmp":         false,
		".t-f.yaml.tmp-1":      false,
		"notes.txt":            false,
	}
	dir := t.TempDir()
	for rel := range cases {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("tenants: {}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	for rel, want := range cases {
		_, walked := scan.Files[rel]
		if walked != want {
			t.Errorf("fixture: the walk read %q = %v, want %v", rel, walked, want)
		}
		if got := IsScannedPath(rel); got != walked {
			t.Errorf("IsScannedPath(%q) = %v, the walk says %v", rel, got, walked)
		}
	}
}
