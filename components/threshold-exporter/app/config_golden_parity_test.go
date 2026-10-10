package main

// Golden parity test — the TRUMP CARD for ADR-017 conformance.
//
// This test reads tests/golden/golden.json (captured with Python
// describe_tenant.py, whose merged_hash is read from `da-guard effective`
// since #1549; tests/golden/test_merge_parity.py checks describe_tenant's own
// canonical hash against the same value) and runs the Go computeMergedHash +
// config.ComputeSourceHash against the same fixtures. Byte-for-byte hash equality is required;
// any divergence is a §8.11.2 semantic trap and a ship blocker.
//
// What the fixtures cover (NOT every ADR-017 clause; see "Known gaps"):
//   flat              — no defaults chain
//   l0-only           — root _defaults + tenant override (scalar)
//   full-l0-l3        — 4-level inheritance, array replace, tenant override
//   mixed-mode        — flat + hierarchical tenants in same conf.d
//   array-replace     — arrays replaced (not concat)
//   opt-out-null      — null on a NON-reserved key (a scalar and a nested
//                       threshold key) does not delete it; the inherited
//                       default survives. The "null deletes a reserved `_`
//                       key" branch is reserved-null-delete's
//   opt-out-null-threshold — real flat-metric-key shape: null keeps the
//                       inherited default, "disable" is the opt-out (#1339)
//   null-body         — a tenant declared with a null body inherits every
//                       default (#1677 F2); own file in the
//                       opt-out-null-threshold tree
//   metadata-skipped  — _metadata never propagates
//   wrapper-siblings  — `defaults:` wrapper WITH sibling top-level keys, the
//                       shape the shipped platform file has; see
//                       tests/golden/build_and_capture.py for its limits
//   carrier-selection — ONE defaults carrier per directory (#1674): a
//                       `.yaml`+`.yml` pair reads the `.yaml`, a subtree
//                       `_DEFAULTS.YML` enters the chain (two tenants, in
//                       the mixed-mode tree's `carrier/` subtree)
//   canonical-json-escaping — a tenant string with < > & and CJK: the
//                       no-HTML-escape and non-ASCII clauses of the canonical
//                       JSON move merged_hash (#1550)
//   reserved-nested-null — null on non-reserved sub-keys of an inherited
//                       reserved key (`_x.*`): an inherited value is retained
//                       and an uninherited null dropped (#1550; was
//                       `_routing` until #2417)
//   reserved-null-delete — a `_` key inherited from L0 and nulled in an L1
//                       _defaults.yaml is deleted; a sibling `_` key survives
//                       (#1550)
//   served-chain      — numeric L0 -> L1 -> L2 chain in the shipped shape, plus
//                       a root-level sibling tenant (served-root) (#2387)
//   served-disable    — "disable" on an L0 key while L1 overrides another
//                       (#2387)
//   yaml-date         — an unquoted date inherited from _defaults.yaml and a
//                       tenant datetime with fraction + offset: yaml.v3's
//                       time.Time as encoding/json writes it (#2371)
//   yaml-binary       — `!!binary` values as their UTF-8 text (#2371); an
//                       invalid byte cannot be a row (golden.json is JSON
//                       text), see tests/dx/test_describe_tenant.py
//   yaml-keys         — non-string mapping keys spelled with `%v` (a date key
//                       is time.Time.String()); a date key in both files is
//                       one key, so the bodies merge (#2371)
//   yaml-numbers      — int / float values typed by yaml.v3's rules, not
//                       YAML 1.1's (`1e3` a number, `12:30:45` a string), and
//                       floats as encoding/json writes them (`1.0` is `1`);
//                       per-shape table: pkg/config/
//                       yaml_number_value_parity_test.go (#2415)
//
// ⚠️ Five trees (l0-only, full-l0-l3, array-replace, opt-out-null,
// metadata-skipped) have a ROOT _defaults.yaml the exporter drops whole, so
// /metrics serves none of it: parity there proves the readers agree with each
// other, not that they describe served values. They are kept as merge-core
// corpus and listed in tests/golden/not_served.json;
// cmd/da-guard/golden_served_test.go holds that list to the trees (#2387).
//
// ⛔ That list names only trees the exporter drops a file of WHOLE. A tree off
// it exits da-guard rc 0, which does NOT make every golden row here a value
// /metrics serves. Rows in rc-0 trees that /metrics does not carry (measured
// with `da-guard served-values`, #2387):
//   flat, mixed-mode-flat, mixed-mode-hier — the tenant file's nested
//       `threshold:` map (and flat's `alert_group`): /metrics serves no row for
//       a nested map, it reports it as unserved; mixed-mode-hier's inherited
//       `threshold.memory: 60` does not reach it at all.
//   carrier-selection-pair / -sub — `cpu_pct` is declared only in a subtree
//       _defaults.yaml, and /metrics emits no row for a subtree-only key
//       (#1976).
// On those rows parity still proves only reader-vs-reader agreement.
//
// Chain discovery: MergedHash / EffectiveConfig read the defaults chain out of
// golden.json on purpose (they isolate the merge core). The chain itself is
// derived from the tree, and compared with Python's, by ScannerChainOrder (the
// exporter's /metrics chain, TreeScan.InheritanceGraph) and ResolveEffective
// (pkg/config ResolveEffective, behind tenant-api /effective and da-guard).
//
// Known gaps (a mutation there leaves this oracle green):
//   - ADR-017's `_routing` null opt-out is enforced by the Python route
//     generator (_grar_merge.py), which neither merge implementation runs;
//     no golden row exercises `_routing` (#2417).
//   - EffectiveConfig compares Go canonicalJSON with Go canonicalJSON, so it
//     is blind to a Go-side escaping change; MergedHash / ResolveEffective
//     catch that.
//   - `_custom_alerts` is in no fixture: describe_tenant's effective config
//     carries the compiler's ADR-024 UNION for it, this merge replaces the
//     list, so the effective_config rows cannot hold it (#1549).
// Orphan files in the fixture trees are guarded on the Python side
// (tests/golden/test_merge_parity.py::test_fixture_trees_have_no_orphans,
// #1551).
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
	"reflect"
	"runtime"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
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
	t.Parallel()
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
			got := config.ComputeSourceHash(data)
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
	t.Parallel()
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
	t.Parallel()
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

			got, err := config.ComputeEffectiveConfig(tenantBytes, g.TenantID, defaultsBytes)
			if err != nil {
				t.Fatalf("config.ComputeEffectiveConfig: %v", err)
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
	t.Parallel()
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

// TestGoldenParity_ResolveEffective derives everything from the fixture tree
// through pkg/config.ResolveEffective — the resolver behind tenant-api
// `/effective`, and (through the same effectiveResolver) da-guard's
// ScopeEffective — and compares it with what Python describe_tenant.py
// captured (#1550).
//
// Unlike the MergedHash / EffectiveConfig legs above, nothing is taken from
// golden.json but the tenant id and the expectations: the tenant file is
// located, the defaults chain discovered and ordered, and the merge run by
// the Go code under test. So a chain-discovery divergence on this path (order,
// a missing or extra level, the carrier picked in a directory) is red here;
// before, it was red in no cross-language test. The exporter's own /metrics
// chain (TreeScan.InheritanceGraph) is the one ScannerChainOrder above walks;
// the two share chainFromCarriers but not the call site, so each keeps a leg.
func TestGoldenParity_ResolveEffective(t *testing.T) {
	t.Parallel()
	entries := loadGolden(t)
	root := goldenRepoRoot(t)

	for _, g := range entries {
		g := g
		t.Run(fmt.Sprintf("%s_%s", g.Scenario, g.TenantID), func(t *testing.T) {
			confD := filepath.Join(root, "tests", "golden", "fixtures", g.FixtureDir, "conf.d")
			got, err := config.ResolveEffective(confD, g.TenantID)
			if err != nil {
				t.Fatalf("ResolveEffective(%s, %s): %v", confD, g.TenantID, err)
			}

			// nil and [] are the same chain; golden writes [] for "none".
			gotChain, wantChain := got.DefaultsChain, g.DefaultsChain
			if gotChain == nil {
				gotChain = []string{}
			}
			if wantChain == nil {
				wantChain = []string{}
			}
			if !reflect.DeepEqual(gotChain, wantChain) {
				t.Errorf("defaults_chain drift for %s/%s:\n  got:  %q\n  want: %q",
					g.Scenario, g.TenantID, gotChain, wantChain)
			}
			if got.SourceFile != g.SourceFile {
				t.Errorf("source_file drift: got %q want %q", got.SourceFile, g.SourceFile)
			}
			if got.SourceHash != g.SourceHash {
				t.Errorf("source_hash drift: got %q want %q", got.SourceHash, g.SourceHash)
			}
			if got.MergedHash != g.MergedHash {
				t.Errorf("merged_hash drift for %s/%s: got %q want %q (chain %q)",
					g.Scenario, g.TenantID, got.MergedHash, g.MergedHash, gotChain)
			}
			gotJSON, err := canonicalJSON(got.EffectiveConfig)
			if err != nil {
				t.Fatalf("canonicalJSON(got): %v", err)
			}
			wantJSON, err := canonicalJSON(g.EffectiveConfig)
			if err != nil {
				t.Fatalf("canonicalJSON(want): %v", err)
			}
			if string(gotJSON) != string(wantJSON) {
				t.Errorf("effective_config drift for %s/%s:\n  got:  %s\n  want: %s",
					g.Scenario, g.TenantID, gotJSON, wantJSON)
			}
		})
	}
}
