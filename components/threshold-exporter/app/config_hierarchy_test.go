package main

// Unit tests for scanDirHierarchical + collectDefaultsChain + InheritanceGraph.
// Fixtures are built inline under t.TempDir() so tests are self-contained
// and don't depend on tests/golden/fixtures (which are used by the Python↔Go
// parity test — see config_golden_parity_test.go).

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// writeFile is a test helper that creates the parent dir and writes content.
// Using a helper keeps the fixture-building code in tests readable.
func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// TestScanDirHierarchical_FlatMode covers the degenerate case of a conf.d/
// with no _defaults.yaml and all tenant files at the root. This should
// behave identically to flat-mode scanDirFileHashes for tenant discovery —
// every tenant gets an empty defaults chain.
func TestScanDirHierarchical_FlatMode(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "tenants.yaml"), `tenants:
  tenant-a:
    threshold:
      cpu: 80
  tenant-b:
    threshold:
      cpu: 70
`)

	tenants, defaults, hashes, _, graph, err := scanDirHierarchical(root, nil)
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	if len(tenants) != 2 {
		t.Errorf("expected 2 tenants, got %d: %v", len(tenants), tenants)
	}
	if _, ok := tenants["tenant-a"]; !ok {
		t.Errorf("missing tenant-a")
	}
	if _, ok := tenants["tenant-b"]; !ok {
		t.Errorf("missing tenant-b")
	}
	if len(defaults) != 0 {
		t.Errorf("expected 0 defaults, got %d: %v", len(defaults), defaults)
	}
	if len(hashes) != 1 {
		t.Errorf("expected 1 hashed file (tenants.yaml), got %d", len(hashes))
	}
	for tid, chain := range graph.TenantDefaults {
		if len(chain) != 0 {
			t.Errorf("tenant=%s expected empty chain, got %v", tid, chain)
		}
	}
}

// TestScanDirHierarchical_FullL0toL3 covers the flagship nested scenario:
// _defaults.yaml at every level of a 4-deep tree. The returned chain must
// be L0-first, leaf-last.
func TestScanDirHierarchical_FullL0toL3(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "_defaults.yaml"), "defaults:\n  level: L0\n")
	writeFile(t, filepath.Join(root, "db", "_defaults.yaml"), "defaults:\n  level: L1\n")
	writeFile(t, filepath.Join(root, "db", "mariadb", "_defaults.yaml"), "defaults:\n  level: L2\n")
	writeFile(t, filepath.Join(root, "db", "mariadb", "prod", "_defaults.yaml"), "defaults:\n  level: L3\n")
	writeFile(t, filepath.Join(root, "db", "mariadb", "prod", "tenant-x.yaml"), `tenants:
  tenant-x:
    threshold:
      cpu: 95
`)

	tenants, defaults, _, _, graph, err := scanDirHierarchical(root, nil)
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	if len(tenants) != 1 {
		t.Fatalf("expected 1 tenant, got %d", len(tenants))
	}
	if len(defaults) != 4 {
		t.Errorf("expected 4 _defaults files, got %d: %v", len(defaults), defaults)
	}

	chain := graph.TenantDefaults["tenant-x"]
	if len(chain) != 4 {
		t.Fatalf("expected chain of 4, got %d: %v", len(chain), chain)
	}
	// Chain must be L0→L3. We verify by checking each entry is nested deeper
	// than the previous (shorter paths come first when under the same root).
	for i := 1; i < len(chain); i++ {
		if len(chain[i]) <= len(chain[i-1]) {
			t.Errorf("chain order wrong at %d: %q should be deeper than %q",
				i, chain[i], chain[i-1])
		}
	}
	// L0 = root's _defaults.yaml
	wantL0 := filepath.Clean(filepath.Join(root, "_defaults.yaml"))
	if chain[0] != wantL0 {
		t.Errorf("chain[0] = %q, want %q", chain[0], wantL0)
	}
	// L3 = nearest to tenant file
	wantL3 := filepath.Clean(filepath.Join(root, "db", "mariadb", "prod", "_defaults.yaml"))
	if chain[3] != wantL3 {
		t.Errorf("chain[3] = %q, want %q", chain[3], wantL3)
	}

	// Reverse lookup: each defaults file must list tenant-x as affected.
	for _, dp := range chain {
		affected := graph.TenantsAffectedBy(dp)
		found := false
		for _, a := range affected {
			if a == "tenant-x" {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("defaults %q should affect tenant-x, got %v", dp, affected)
		}
	}
}

// TestScanDirHierarchical_MixedMode verifies that flat tenants and
// hierarchical tenants can coexist. A tenant at root has an empty (or
// root-only) chain; a tenant in a subdir inherits only its subdir's defaults.
func TestScanDirHierarchical_MixedMode(t *testing.T) {
	root := t.TempDir()
	// Flat: tenant at root, no root _defaults
	writeFile(t, filepath.Join(root, "flat-tenant.yaml"), `tenants:
  tenant-flat:
    threshold:
      cpu: 55
`)
	// Hier: tenant in db/ with db/_defaults.yaml
	writeFile(t, filepath.Join(root, "db", "_defaults.yaml"), "defaults:\n  threshold:\n    memory: 60\n")
	writeFile(t, filepath.Join(root, "db", "hier-tenant.yaml"), `tenants:
  tenant-hier:
    threshold:
      cpu: 88
`)

	tenants, _, _, _, graph, err := scanDirHierarchical(root, nil)
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	if len(tenants) != 2 {
		t.Fatalf("expected 2 tenants, got %d: %v", len(tenants), tenants)
	}

	flatChain := graph.TenantDefaults["tenant-flat"]
	if len(flatChain) != 0 {
		t.Errorf("tenant-flat expected empty chain (no root _defaults), got %v", flatChain)
	}
	hierChain := graph.TenantDefaults["tenant-hier"]
	if len(hierChain) != 1 {
		t.Errorf("tenant-hier expected 1-length chain, got %v", hierChain)
	} else {
		wantDB := filepath.Clean(filepath.Join(root, "db", "_defaults.yaml"))
		if hierChain[0] != wantDB {
			t.Errorf("tenant-hier chain[0] = %q, want %q", hierChain[0], wantDB)
		}
	}
}

// TestScanDirHierarchical_DuplicateTenant ensures the guardrail fires when
// the same tenant ID appears in two distinct files. Silently preferring one
// would hide a config-management bug.
func TestScanDirHierarchical_DuplicateTenant(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.yaml"), `tenants:
  shared-tenant:
    threshold: {cpu: 80}
`)
	writeFile(t, filepath.Join(root, "sub", "b.yaml"), `tenants:
  shared-tenant:
    threshold: {cpu: 99}
`)

	_, _, _, _, _, err := scanDirHierarchical(root, nil)
	if err == nil {
		t.Fatal("expected duplicate-tenant error, got nil")
	}
	if !strings.Contains(err.Error(), "duplicate tenant ID") {
		t.Errorf("error message mismatch: %v", err)
	}
	if !strings.Contains(err.Error(), "shared-tenant") {
		t.Errorf("error should name the duplicate tenant: %v", err)
	}
}

// TestScanDirHierarchical_HiddenDirsPruned ensures .git and similar hidden
// directories don't pollute the scan (regression guard for accidental
// `.git/ORIG_HEAD` shaped YAML-like files).
func TestScanDirHierarchical_HiddenDirsPruned(t *testing.T) {
	root := t.TempDir()
	// Decoy file inside a .git-looking dir — must not be hashed.
	writeFile(t, filepath.Join(root, ".git", "config.yaml"), "tenants:\n  decoy: {}\n")
	writeFile(t, filepath.Join(root, "real.yaml"), `tenants:
  real-tenant:
    threshold: {cpu: 50}
`)

	tenants, _, hashes, _, _, err := scanDirHierarchical(root, nil)
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	if _, bad := tenants["decoy"]; bad {
		t.Errorf("decoy tenant under .git/ should have been pruned")
	}
	if _, ok := tenants["real-tenant"]; !ok {
		t.Errorf("real-tenant should be present")
	}
	for path := range hashes {
		if strings.Contains(path, string(filepath.Separator)+".git"+string(filepath.Separator)) {
			t.Errorf("hashed file under .git/: %s", path)
		}
	}
}

// TestInheritanceGraph_AddTenantDefensiveCopy ensures that mutating the
// chain slice after AddTenant doesn't corrupt the graph's internal state.
func TestInheritanceGraph_AddTenantDefensiveCopy(t *testing.T) {
	g := NewInheritanceGraph()
	chain := []string{"/a", "/a/b"}
	g.AddTenant("t1", chain)

	chain[0] = "MUTATED" // simulate accidental reuse

	got := g.TenantDefaults["t1"]
	if got[0] != "/a" {
		t.Errorf("graph state leaked: got[0]=%q after external mutation", got[0])
	}
}

// TestInheritanceGraph_DefaultsToTenantsOrder verifies that the reverse map
// accumulates tenants in deterministic order (sorted by tenant ID via the
// scan path). This matters for test stability and for debounced reload
// batching. Direct AddTenant calls preserve insertion order; it's the
// scanner's responsibility (via sortStrings) to make insertions sorted.
func TestInheritanceGraph_DefaultsToTenantsOrder(t *testing.T) {
	g := NewInheritanceGraph()
	g.AddTenant("tenant-b", []string{"/root/_defaults.yaml"})
	g.AddTenant("tenant-a", []string{"/root/_defaults.yaml"})
	g.AddTenant("tenant-c", []string{"/root/_defaults.yaml"})

	got := g.TenantsAffectedBy("/root/_defaults.yaml")
	// Caller's insertion order: b, a, c — graph preserves that without sorting.
	want := []string{"tenant-b", "tenant-a", "tenant-c"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v (insertion order)", got, want)
	}

	// Scanner feeds sorted IDs → result is sorted. Verify the sort helper
	// produces the shape we expect.
	ids := []string{"tenant-b", "tenant-a", "tenant-c"}
	sortStrings(ids)
	sortExpect := []string{"tenant-a", "tenant-b", "tenant-c"}
	if !reflect.DeepEqual(ids, sortExpect) {
		t.Errorf("sortStrings produced %v, want %v", ids, sortExpect)
	}
	// Also make sure we agree with stdlib sort for our small-N use case.
	cross := []string{"c", "a", "b"}
	sort.Strings(cross)
	ours := []string{"c", "a", "b"}
	sortStrings(ours)
	if !reflect.DeepEqual(cross, ours) {
		t.Errorf("local sort diverges from stdlib: stdlib=%v ours=%v", cross, ours)
	}
}
