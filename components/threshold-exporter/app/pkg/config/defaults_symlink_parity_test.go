package config

// defaults_symlink_parity_test.go — Go's half of
// tests/shared/defaults_symlink_parity_matrix.json (#1674 round 2): defaults
// carriers that are symlinks. The Python half is
// tests/shared/test_defaults_symlink_parity.py (describe_tenant). Neither side
// reads the other's source; both assert the table.
//
// Seams: none — t.TempDir() trees.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

// ⛔ Decoded with DisallowUnknownFields (#1967 blind review): a misspelt
// optional key (`confd` for `conf_d`) was silently dropped, the tree fell
// back to the tree root as conf.d, and the row stopped testing what its name
// says while staying green. `_comment` is therefore declared, not ignored.
type symlinkParityMatrix struct {
	Comment []string `json:"_comment"`
	Trees   []struct {
		Name string `json:"name"`
		// ConfD (optional, #1967): the tree-root-relative directory handed
		// to ResolveEffective, so a tree can hold link targets outside
		// conf.d. Empty = the tree root.
		ConfD    string            `json:"conf_d"`
		Files    map[string]string `json:"files"`
		Symlinks map[string]string `json:"symlinks"`
		Expect   map[string]struct {
			ChainLen        int            `json:"chain_len"`
			EffectiveConfig map[string]any `json:"effective_config"`
			MergedHash      string         `json:"merged_hash"`
		} `json:"expect"`
	} `json:"trees"`
}

func TestDefaultsSymlinkParityMatrix(t *testing.T) {
	t.Parallel()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "defaults_symlink_parity_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m symlinkParityMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	for _, tree := range m.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			for rel, content := range tree.Files {
				rootWrite(t, filepath.Join(root, filepath.FromSlash(rel)), content)
			}
			for link, target := range tree.Symlinks {
				if err := os.Symlink(filepath.FromSlash(target), filepath.Join(root, filepath.FromSlash(link))); err != nil {
					t.Skipf("symlinks unavailable here (%v) — measured on Linux/macOS CI", err)
				}
			}
			confD := filepath.Join(root, filepath.FromSlash(tree.ConfD))
			if tree.ConfD != "" {
				assertSomeLinkEscapesConfD(t, confD, root, tree.Symlinks)
			}
			for tenant, want := range tree.Expect {
				ec, err := ResolveEffective(confD, tenant)
				if err != nil {
					t.Fatalf("%s: %v", tenant, err)
				}
				if len(ec.DefaultsChain) != want.ChainLen {
					t.Errorf("%s: chain %v, want %d levels", tenant, ec.DefaultsChain, want.ChainLen)
				}
				gotJSON, _ := json.Marshal(ec.EffectiveConfig)
				var got map[string]any
				_ = json.Unmarshal(gotJSON, &got)
				if !reflect.DeepEqual(got, want.EffectiveConfig) {
					t.Errorf("%s: effective %v, want %v", tenant, got, want.EffectiveConfig)
				}
				if ec.MergedHash != want.MergedHash {
					t.Errorf("%s: merged_hash %s, pinned %s", tenant, ec.MergedHash, want.MergedHash)
				}
			}
		})
	}
}

// assertSomeLinkEscapesConfD fails the row unless at least one of its links
// resolves OUTSIDE confD. `conf_d` exists only so a row can hold such a
// target (#1967 rows h / k); a row that declares it with every target inside
// conf.d no longer tests what it was added for, and would still pass.
func assertSomeLinkEscapesConfD(t *testing.T, confD, root string, links map[string]string) {
	t.Helper()
	realConfD, err := filepath.EvalSymlinks(confD)
	if err != nil {
		t.Fatalf("conf_d %s: %v", confD, err)
	}
	for link := range links {
		target, err := filepath.EvalSymlinks(filepath.Join(root, filepath.FromSlash(link)))
		if err != nil {
			t.Fatalf("resolve %s: %v", link, err)
		}
		rel, err := filepath.Rel(realConfD, target)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return
		}
	}
	t.Fatalf("row declares conf_d %q but no symlink target resolves outside it — "+
		"the row no longer tests a target outside conf.d", confD)
}
