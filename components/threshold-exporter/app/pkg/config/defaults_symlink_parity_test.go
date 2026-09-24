package config

// defaults_symlink_parity_test.go — Go's half of
// tests/shared/defaults_symlink_parity_matrix.json (#1674 round 2): defaults
// carriers that are symlinks. The Python half is
// tests/shared/test_defaults_symlink_parity.py (describe_tenant). Neither side
// reads the other's source; both assert the table.
//
// Seams: none — t.TempDir() trees.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
)

type symlinkParityMatrix struct {
	Trees []struct {
		Name     string            `json:"name"`
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
	if err := json.Unmarshal(raw, &m); err != nil {
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
			for tenant, want := range tree.Expect {
				ec, err := ResolveEffective(root, tenant)
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
