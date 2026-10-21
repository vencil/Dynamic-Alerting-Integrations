package config

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// TestScanDirTreeNames_ReadsNothing pins ScanDirTreeNames (#3206,
// `da-guard parse-tree --list-only`) as the walk of ScanDirTreeFiles with no
// byte read: the same Keys, Unreadable, NotFiles, Defaults and carrier
// selection, and no file hashed or cached — a hash needs the bytes, so an
// empty Hash on every file is the scan saying it read none.
func TestScanDirTreeNames_ReadsNothing(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	testutil.WriteTree(t, root, map[string]string{
		"_defaults.yaml":        "defaults: {}\n",
		"_DEFAULTS.YML":         "defaults: {}\n",
		"t1.yaml":               "tenants: {t1: {}}\n",
		"Upper.YAML":            "tenants: {t2: {}}\n",
		"team/_defaults.yml":    "defaults: {}\n",
		"team/t3.yml":           ": not yaml [\n",
		".git/x.yaml":           "x\n",
		"notes.yaml/inner.yaml": "tenants: {t4: {}}\n",
		"readme.md":             "x\n",
	})
	for link, target := range map[string]string{"dangling.yaml": "nowhere.yaml", "away": t.TempDir()} {
		if err := os.Symlink(target, filepath.Join(root, link)); err != nil {
			t.Skipf("symlink: %v", err)
		}
	}
	files, err := ScanDirTreeFiles(root, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	names, err := ScanDirTreeNames(root, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	for what, pair := range map[string][2]any{
		"Keys":       {names.Keys, files.Keys},
		"Unreadable": {names.Unreadable, files.Unreadable},
		"NotFiles":   {names.NotFiles, files.NotFiles},
		"Defaults":   {names.Defaults, files.Defaults},
		"carriers":   {names.DefaultsCarriers().ByDir, files.DefaultsCarriers().ByDir},
	} {
		if !reflect.DeepEqual(pair[0], pair[1]) {
			t.Errorf("%s: names %v, files %v", what, pair[0], pair[1])
		}
	}
	if len(names.Keys) == 0 || len(names.Unreadable) == 0 || len(names.NotFiles) == 0 {
		t.Fatalf("fixture: keys %v unreadable %v notfiles %v — each must be non-empty",
			names.Keys, names.Unreadable, names.NotFiles)
	}
	for _, k := range names.Keys {
		f := names.Files[k]
		if f.Hash != "" || f.Data != nil || f.TenantIDs != nil || f.Parsed {
			t.Errorf("%s: hash %q, %d bytes, tenants %v, parsed %v — a names-only scan read it",
				k, f.Hash, len(f.Data), f.TenantIDs, f.Parsed)
		}
	}
	if names.Partials != nil || len(names.Tenants) != 0 {
		t.Errorf("partials %v tenants %v, want none", names.Partials, names.Tenants)
	}
}
