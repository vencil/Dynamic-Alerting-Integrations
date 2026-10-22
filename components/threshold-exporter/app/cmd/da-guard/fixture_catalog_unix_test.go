//go:build unix

package main

import (
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"syscall"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// buildCatalogRow applies the catalogue's base (when the row asks) and the
// row's ops under dir — the recipe language of
// tests/shared/confd_fixture_catalog.json, which the Python matrix applies
// the same way — and returns the tree.
func buildCatalogRow(t *testing.T, c fixtureCatalog, row catalogRow, dir string) string {
	t.Helper()
	ops := row.Ops
	if row.Base {
		ops = append(append([]catalogOp(nil), c.Base...), row.Ops...)
	}
	var chmodded []string
	t.Cleanup(func() {
		for _, p := range chmodded {
			_ = os.Chmod(p, 0o755) // so t.TempDir can remove the tree
		}
	})
	for i, op := range ops {
		p := filepath.Join(dir, filepath.FromSlash(op.Path))
		if op.PathHex != "" {
			raw, err := hex.DecodeString(op.PathHex)
			if err != nil {
				t.Fatalf("%s op %d: path_hex: %v", row.ID, i, err)
			}
			p = filepath.Join(dir, string(raw))
		}
		var err error
		switch op.Op {
		case "mkdir":
			err = os.MkdirAll(p, 0o755)
		case "write":
			data := []byte(op.Text)
			if op.B64 != "" {
				data, err = base64.StdEncoding.DecodeString(op.B64)
			}
			if err == nil {
				err = os.WriteFile(p, data, 0o644) // #nosec G306 -- a fixture other uids read
			}
		case "symlink":
			err = os.Symlink(op.Target, p)
		case "symlink_chain":
			via := filepath.Join(dir, filepath.FromSlash(op.Via))
			prev := op.Target
			for j := 0; j < op.Length && err == nil; j++ {
				err = os.Symlink(prev, filepath.Join(via, fmt.Sprintf("l%d", j)))
				prev = fmt.Sprintf("l%d", j)
			}
			if err == nil {
				var rel string
				if rel, err = filepath.Rel(filepath.Dir(p), via); err == nil {
					err = os.Symlink(filepath.Join(rel, prev), p)
				}
			}
		case "mkfifo":
			err = syscall.Mkfifo(p, 0o644)
		case "chmod":
			var m uint64
			if m, err = strconv.ParseUint(op.Mode, 8, 32); err == nil {
				chmodded = append(chmodded, p)
				err = os.Chmod(p, os.FileMode(m))
			}
		default:
			err = fmt.Errorf("unknown op %q", op.Op)
		}
		if err != nil {
			t.Fatalf("%s op %d (%s %s): %v", row.ID, i, op.Op, op.Path, err)
		}
	}
	return filepath.Join(dir, c.Root)
}

// TestFixtureCatalog_RecipesGiveTheirEntries: each catalogue recipe, built
// and walked as uid 65534 (re-executed under setpriv when run as root, so a
// mode bit really denies), gives exactly the entries the row expects — path,
// role and problem kind — or, for a refused row, exit 2. The Python matrix
// asks da-guard itself on every cell; this is what keeps the catalogue's
// labels true.
func TestFixtureCatalog_RecipesGiveTheirEntries(t *testing.T) {
	if os.Geteuid() == 0 && os.Getenv(testutil.UnprivChildEnv) == "" {
		// The run as uid 65534 cannot read a checkout in a 0700 home: hand
		// it a copy of the catalogue in a directory every uid can read.
		raw, err := os.ReadFile(fixtureCatalogPath())
		if err != nil {
			t.Fatalf("read the fixture catalogue: %v", err)
		}
		dir, err := os.MkdirTemp("/var/tmp", "vibe-catalog-")
		if err != nil {
			testutil.SkipOrFail(t, fmt.Sprintf("cannot make a directory under /var/tmp: %v", err))
			return
		}
		t.Cleanup(func() { _ = os.RemoveAll(dir) })
		copyPath := filepath.Join(dir, "confd_fixture_catalog.json")
		if err := os.WriteFile(copyPath, raw, 0o644); err != nil { // #nosec G306 -- nobody reads it
			t.Fatal(err)
		}
		if err := os.Chmod(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv(fixtureCatalogEnv, copyPath)
	}
	if !testutil.RunUnprivileged(t, "TestFixtureCatalog_RecipesGiveTheirEntries") {
		return
	}
	c := loadFixtureCatalog(t)
	for _, row := range c.Rows {
		t.Run(row.ID, func(t *testing.T) {
			conf := buildCatalogRow(t, c, row, t.TempDir())
			code, doc, msg := runPT(t, "", "--config-dir", conf)
			if row.Expect.Refused {
				if code != exitCallerErr {
					t.Fatalf("exit %d, want %d (a tree parse-tree refuses): %s", code, exitCallerErr, msg)
				}
				return
			}
			if code != exitOK {
				t.Fatalf("exit %d: %s", code, msg)
			}
			want := map[string]string{}
			expected := row.Expect.Entries
			if row.Base {
				expected = append(append([]catalogEntry(nil), c.BaseEntries...), expected...)
			}
			for _, e := range expected {
				k := "-"
				if e.Kind != nil {
					k = *e.Kind
				}
				want[e.Path] = e.Role + "/" + k
			}
			got := map[string]string{}
			for p, e := range entriesByPath(t, doc) {
				k := "-"
				if pr, ok := e["problem"].(map[string]any); ok {
					k, _ = pr["kind"].(string)
				}
				role, _ := e["role"].(string)
				got[p] = role + "/" + k
			}
			var diff []string
			for p, w := range want {
				if got[p] != w {
					diff = append(diff, fmt.Sprintf("%s: got %q, want %q", p, got[p], w))
				}
			}
			for p, g := range got {
				if _, ok := want[p]; !ok {
					diff = append(diff, fmt.Sprintf("%s: got %q, not in the row", p, g))
				}
			}
			sort.Strings(diff)
			for _, d := range diff {
				t.Error(d)
			}
		})
	}
}
