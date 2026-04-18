package main

// Golden parity test — the TRUMP CARD for ADR-018 conformance.
//
// This test reads tests/golden/golden.json (captured from Python
// describe_tenant.py) and runs the Go computeMergedHash + computeSourceHash
// against the same 8 fixtures. Byte-for-byte hash equality is required;
// any divergence is a §8.11.2 semantic trap and a ship blocker.
//
// Fixtures cover every deep_merge rule from ADR-018:
//   flat              — no defaults chain
//   l0-only           — root _defaults + tenant override (scalar)
//   full-l0-l3        — 4-level inheritance, array replace, tenant override
//   mixed-mode        — flat + hierarchical tenants in same conf.d
//   array-replace     — arrays replaced (not concat)
//   opt-out-null      — explicit null deletes inherited key
//   metadata-skipped  — _metadata never propagates
//
// If this test is red and the Python side is green, the Go port has drifted.
// Run `python3 tests/golden/build_and_capture.py` only when Python semantics
// *intentionally* change — regenerating golden.json to mask a Go bug is the
// wrong move.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

type goldenEntry struct {
	Scenario        string         `json:"scenario"`
	TenantID        string         `json:"tenant_id"`
	FixtureDir      string         `json:"fixture_dir"`
	SourceFile      string         `json:"source_file"`
	SourceHash      string         `json:"source_hash"`
	MergedHash      string         `json:"merged_hash"`
	DefaultsChain   []string       `json:"defaults_chain"`
	EffectiveConfig map[string]any `json:"effective_config"`
}

// goldenRepoRoot discovers the repo root by walking up from this source
// file. In the Dev Container this is /workspaces/vibe-k8s-lab; in Cowork
// VM it's /sessions/.../mnt/vibe-k8s-lab. The test is expected to run
// in the Dev Container (Cowork VM doesn't have Go), but discovery works
// either way.
func goldenRepoRoot(t *testing.T) string {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	// thisFile = .../components/threshold-exporter/app/config_golden_parity_test.go
	// Walk up 3 levels: app → threshold-exporter → components → repo root
	return filepath.Clean(filepath.Join(filepath.Dir(thisFile), "..", "..", ".."))
}

func loadGolden(t *testing.T) []goldenEntry {
	t.Helper()
	root := goldenRepoRoot(t)
	path := filepath.Join(root, "tests", "golden", "golden.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden.json: %v (did you run tests/golden/build_and_capture.py?)", err)
	}
	var entries []goldenEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		t.Fatalf("parse golden.json: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("golden.json is empty")
	}
	return entries
}

// TestGoldenParity_SourceHash verifies per-file SHA-256[:16] matches Python.
// Drift here = byte-level diff in the tenant file (line endings? BOM? extra
// newline?). Fixture files are built by Python — parity is about reading the
// exact same bytes.
func TestGoldenParity_SourceHash(t *testing.T) {
	entries := loadGolden(t)
	root := goldenRepoRoot(t)

	for _, g := range entries {
		g := g
		t.Run(fmt.Sprintf("%s_%s", g.Scenario, g.TenantID), func(t *testing.T) {
			tenantPath := filepath.Join(root, "tests", "golden", "fixtures", g.FixtureDir, "conf.d", g.SourceFile)
			data, err := os.ReadFile(tenantPath)
			if err != nil {
				t.Fatalf("read %s: %v", tenantPath, err)
			}
			got := computeSourceHash(data)
			if got != g.SourceHash {
				t.Errorf("source_hash drift: got %q want %q (file=%s)",
					got, g.SourceHash, tenantPath)
			}
		})
	}
}

// TestGoldenParity_MergedHash is THE test — every drift here maps to one of
// the 8 semantic traps in §8.11.2. Run this first when debugging parity.
func TestGoldenParity_MergedHash(t *testing.T) {
	entries := loadGolden(t)
	root := goldenRepoRoot(t)

	for _, g := range entries {
		g := g
		t.Run(fmt.Sprintf("%s_%s", g.Scenario, g.TenantID), func(t *testing.T) {
			confD := filepath.Join(root, "tests", "golden", "fixtures", g.FixtureDir, "conf.d")
			tenantPath := filepath.Join(confD, g.SourceFile)
			tenantBytes, err := os.ReadFile(tenantPath)
			if err != nil {
				t.Fatalf("read tenant file %s: %v", tenantPath, err)
			}

			// Build defaults chain bytes in golden's declared order.
			var defaultsBytes [][]byte
			for _, rel := range g.DefaultsChain {
				defPath := filepath.Join(confD, rel)
				b, err := os.ReadFile(defPath)
				if err != nil {
					t.Fatalf("read defaults %s: %v", defPath, err)
				}
				defaultsBytes = append(defaultsBytes, b)
			}

			got, err := computeMergedHash(tenantBytes, g.TenantID, defaultsBytes)
			if err != nil {
				t.Fatalf("computeMergedHash: %v", err)
			}
			if got != g.MergedHash {
				t.Errorf("merged_hash drift for %s/%s:\n"+
					"  got:  %s\n"+
					"  want: %s\n"+
					"  fixture: %s\n"+
					"  defaults: %v\n"+
					"See §8.11.2 traps 1-8 for debugging.",
					g.Scenario, g.TenantID, got, g.MergedHash, confD, g.DefaultsChain)
			}
		})
	}
}

// TestGoldenParity_EffectiveConfig compares the merged dict structure against
// golden. This is stricter than the hash — it catches hash collisions and
// type coercion drift (e.g. int vs float64 producing the same 16-char hash
// by coincidence but different structural output). If merged_hash passes
// but this fails, something semantically equivalent has shifted.
func TestGoldenParity_EffectiveConfig(t *testing.T) {
	entries := loadGolden(t)
	root := goldenRepoRoot(t)

	for _, g := range entries {
		g := g
		t.Run(fmt.Sprintf("%s_%s", g.Scenario, g.TenantID), func(t *testing.T) {
			confD := filepath.Join(root, "tests", "golden", "fixtures", g.FixtureDir, "conf.d")
			tenantPath := filepath.Join(confD, g.SourceFile)
			tenantBytes, err := os.ReadFile(tenantPath)
			if err != nil {
				t.Fatalf("read %s: %v", tenantPath, err)
			}
			var defaultsBytes [][]byte
			for _, rel := range g.DefaultsChain {
				b, err := os.ReadFile(filepath.Join(confD, rel))
				if err != nil {
					t.Fatalf("read defaults: %v", err)
				}
				defaultsBytes = append(defaultsBytes, b)
			}

			got, err := computeEffectiveConfig(tenantBytes, g.TenantID, defaultsBytes)
			if err != nil {
				t.Fatalf("computeEffectiveConfig: %v", err)
			}

			// Compare via canonical JSON round-trip so int vs float coercion
			// can't false-positive a "different" result — if both sides
			// serialize identically, we consider them equal.
			gotJSON, err := canonicalJSON(got)
			if err != nil {
				t.Fatalf("canonicalJSON(got): %v", err)
			}
			wantJSON, err := canonicalJSON(g.EffectiveConfig)
			if err != nil {
				t.Fatalf("canonicalJSON(want): %v", err)
			}
			if string(gotJSON) != string(wantJSON) {
				t.Errorf("effective_config drift for %s/%s:\n"+
					"  got:  %s\n"+
					"  want: %s",
					g.Scenario, g.TenantID, gotJSON, wantJSON)
			}
		})
	}
}

// TestGoldenParity_ScannerChainOrder verifies that scanDirHierarchical picks
// up the same defaults_chain that Python captured. Catches walker / chain
// bugs independently of hash computation.
func TestGoldenParity_ScannerChainOrder(t *testing.T) {
	entries := loadGolden(t)
	root := goldenRepoRoot(t)

	// Group fixtures by directory so we scan each conf.d once.
	seen := make(map[string]bool)
	type byDir struct {
		dir     string
		tenants []goldenEntry
	}
	var groups []byDir
	for _, g := range entries {
		if seen[g.FixtureDir] {
			continue
		}
		seen[g.FixtureDir] = true
		var ts []goldenEntry
		for _, gg := range entries {
			if gg.FixtureDir == g.FixtureDir {
				ts = append(ts, gg)
			}
		}
		groups = append(groups, byDir{dir: g.FixtureDir, tenants: ts})
	}

	for _, grp := range groups {
		grp := grp
		t.Run(grp.dir, func(t *testing.T) {
			confD := filepath.Join(root, "tests", "golden", "fixtures", grp.dir, "conf.d")
			_, _, _, _, graph, err := scanDirHierarchical(confD, nil)
			if err != nil {
				t.Fatalf("scanDirHierarchical: %v", err)
			}
			absConfD, _ := filepath.Abs(confD)
			absConfD = filepath.Clean(absConfD)

			for _, g := range grp.tenants {
				gotChain, exists := graph.TenantDefaults[g.TenantID]
				if !exists {
					t.Errorf("tenant %q not found by scanner in %s", g.TenantID, confD)
					continue
				}
				if len(gotChain) != len(g.DefaultsChain) {
					t.Errorf("tenant=%s chain length mismatch: got %d want %d (%v vs %v)",
						g.TenantID, len(gotChain), len(g.DefaultsChain), gotChain, g.DefaultsChain)
					continue
				}
				// Compare as paths relative to conf.d/ for cross-platform
				// stability. golden.json stores POSIX-style relative paths.
				for i, absPath := range gotChain {
					rel, err := filepath.Rel(absConfD, absPath)
					if err != nil {
						t.Errorf("filepath.Rel(%s, %s): %v", absConfD, absPath, err)
						continue
					}
					// Normalize to forward slashes (golden uses POSIX paths).
					relPosix := filepath.ToSlash(rel)
					if relPosix != g.DefaultsChain[i] {
						t.Errorf("tenant=%s chain[%d] = %q, want %q",
							g.TenantID, i, relPosix, g.DefaultsChain[i])
					}
				}
			}
		})
	}
}
