package config

// tree_identity_test.go — #4042: TreeScan.Composite (treeIdentity) moves
// with every change to what a walk of the tree saw, paths included.
//
// Until #4042 the composite hashed the per-file hashes alone: a file renamed
// into another class (`db.yaml` → `_db.yaml`: its tenant no longer served),
// the root `_defaults.yaml` moved into a subdirectory, or an entry that
// became unreadable changed what the tree serves and left the hash as it was.
// Each case below was measured that way on the old composite.
//
// Seams: none — t.TempDir() trees; the scan logger discards.

import (
	"crypto/sha256"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

func identityTreeFiles(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"team-a/db.yaml": "tenants:\n  ta:\n    mysql_connections: \"80\"\n",
		"team-b/db.yaml": "tenants:\n  tb:\n    mysql_connections: \"90\"\n",
	} {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func identityOf(t *testing.T, dir string) string {
	t.Helper()
	scan, err := ScanDirTree(dir, nil, nil, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("ScanDirTree: %v", err)
	}
	return scan.Composite
}

func mustRename(t *testing.T, dir, from, to string) {
	t.Helper()
	dst := filepath.Join(dir, filepath.FromSlash(to))
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(dir, filepath.FromSlash(from)), dst); err != nil {
		t.Fatal(err)
	}
}

func TestTreeIdentity_MovesWithWhatTheWalkSaw(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		change func(t *testing.T, dir string)
	}{
		// The three the old composite missed while the answer changed.
		{"renamed into another class", func(t *testing.T, d string) { mustRename(t, d, "team-b/db.yaml", "team-b/_db.yaml") }},
		{"root defaults moved into a subdirectory", func(t *testing.T, d string) { mustRename(t, d, "_defaults.yaml", "team-a/_defaults.yaml") }},
		{"a dangling link appears", func(t *testing.T, d string) {
			symlinkOrSkip(t, filepath.Join(d, "nowhere.yaml"), filepath.Join(d, "team-c.yaml"))
		}},
		// Same bytes under another key: a tenant's provenance moved.
		{"renamed within its class", func(t *testing.T, d string) { mustRename(t, d, "team-a/db.yaml", "team-a/zz.yaml") }},
		{"moved to another directory", func(t *testing.T, d string) { mustRename(t, d, "team-b/db.yaml", "team-a/db2.yaml") }},
		{"a directory with a config file's name appears", func(t *testing.T, d string) {
			if err := os.Mkdir(filepath.Join(d, "x.yaml"), 0o755); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := identityTreeFiles(t)
			before := identityOf(t, dir)
			tc.change(t, dir)
			if after := identityOf(t, dir); after == before {
				t.Errorf("the walk's identity did not move: %s", after)
			}
		})
	}
}

// Every walk that reads the files gives the same identity for the same tree:
// the exporter's walk (ScanDirTree, cold and over its prior, where an
// unchanged file is carried rather than read) and parse-tree's
// (ScanDirTreeFiles). patch-config and #3965 compare across them.
func TestTreeIdentity_SameTreeSameIdentityAcrossWalks(t *testing.T) {
	t.Parallel()
	dir := identityTreeFiles(t)
	symlinkOrSkip(t, filepath.Join(dir, "nowhere.yaml"), filepath.Join(dir, "team-c.yaml"))
	if err := os.Mkdir(filepath.Join(dir, "x.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	quiet := log.New(io.Discard, "", 0)
	cold, err := ScanDirTree(dir, nil, nil, quiet)
	if err != nil {
		t.Fatal(err)
	}
	if len(cold.Unreadable) == 0 || len(cold.NotFiles) == 0 {
		t.Fatalf("the fixture lost its u/n records: unreadable=%v notfiles=%v", cold.Unreadable, cold.NotFiles)
	}
	warm, err := ScanDirTree(dir, cold, nil, quiet)
	if err != nil {
		t.Fatal(err)
	}
	files, err := ScanDirTreeFiles(dir, quiet)
	if err != nil {
		t.Fatal(err)
	}
	if warm.Composite != cold.Composite || files.Composite != cold.Composite {
		t.Errorf("one tree, three identities: cold %s, over its prior %s, files %s",
			cold.Composite, warm.Composite, files.Composite)
	}
}

// The definition, computed here without treeIdentity: one record per kept
// file, Unreadable entry and NotFile, as its doc comment states.
func TestTreeIdentity_IsTheDocumentedRecords(t *testing.T) {
	t.Parallel()
	dir := identityTreeFiles(t)
	symlinkOrSkip(t, filepath.Join(dir, "nowhere.yaml"), filepath.Join(dir, "team-c.yaml"))
	if err := os.Mkdir(filepath.Join(dir, "x.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	scan, err := ScanDirTree(dir, nil, nil, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	keys := []string{"_defaults.yaml", "team-a/db.yaml", "team-b/db.yaml"}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		body, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(k))) // #nosec G304 -- test tree
		if err != nil {
			t.Fatal(err)
		}
		fmt.Fprintf(h, "f\x00%s\x00%x\n", k, sha256.Sum256(body))
	}
	fmt.Fprintf(h, "u\x00%s\n", "team-c.yaml")
	fmt.Fprintf(h, "n\x00%s\x00%s\n", "x.yaml", NotFileDirectory)
	if want := fmt.Sprintf("%x", h.Sum(nil)); scan.Composite != want {
		t.Errorf("Composite = %s, want %s (keys %q, unreadable %+v, notfiles %+v)",
			scan.Composite, want, scan.Keys, scan.Unreadable, scan.NotFiles)
	}
}
