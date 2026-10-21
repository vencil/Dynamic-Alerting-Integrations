package config

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/confdname"
)

// confdname.IsScannedPath answers, for a relative path, what the walk reads
// (#1988: tenant-api asks it about untracked files). The walk classifies by
// the same package, but it also prunes directories as it descends; pinned
// against the walk itself, so a path predicate and a pruning walk cannot
// drift.
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
		if got := confdname.IsScannedPath(rel); got != walked {
			t.Errorf("confdname.IsScannedPath(%q) = %v, the walk says %v", rel, got, walked)
		}
	}
}
