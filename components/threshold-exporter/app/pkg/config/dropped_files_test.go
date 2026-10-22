package config

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// TestDroppedFiles_IsTheBuilds: TreeScan.DroppedFiles over a ScanDirTreeFiles
// walk (no decode, no build) is exactly FlatBuild.ParseFailed of the
// exporter's own load of the same tree (LoadDirReport) — every shape that
// reaches a different decode branch, kept or dropped (#3206).
func TestDroppedFiles_IsTheBuilds(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	for rel, text := range map[string]string{
		"_defaults.yaml":           "defaults:\n  mysql_connections: 80\n",
		"_defaults.yml":            "defaults: [\n", // unselected root carrier: not parsed
		"_profiles.yaml":           "profiles: [\n", // root `_` file: dropped
		"_platform.yaml":           "state_filters: {}\n",
		"ok.yaml":                  "tenants:\n  ok: {mysql_connections: 5}\n",
		"syntax.yaml":              "tenants: {t1: [\n",
		"list.yaml":                "tenants:\n  - x\n", // decode alone refuses it
		"dup.yaml":                 "tenants:\n  d: {a: 1}\n  d: {a: 2}\n",
		"flat.yaml":                "mysql_connections: 5\n",
		"team/_defaults.yaml":      "defaults:\n  mysql_connections: {default: \"90\"}\n",
		"team/_defaults.yml":       "defaults: [\n", // unselected nested carrier: probed, dropped
		"team/_domain_policy.yaml": "x: [\n",
		"team/t.yaml":              "tenants:\n  t: {}\n",
		"deep/a/_profiles.yaml":    "profiles: {}\n",
	} {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_, rep, err := LoadDirReport(root, nil)
	if err != nil {
		t.Fatal(err)
	}
	scan, err := ScanDirTreeFiles(root, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	got := scan.DroppedFiles()
	if !reflect.DeepEqual(got, rep.ParseFailed) {
		t.Errorf("DroppedFiles %v, the build's ParseFailed %v", got, rep.ParseFailed)
	}
	if len(got) < 6 {
		t.Errorf("fixture: only %v dropped; the tree is meant to reach every branch", got)
	}
}
