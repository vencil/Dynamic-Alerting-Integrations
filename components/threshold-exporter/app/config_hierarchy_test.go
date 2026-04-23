package main

// Unit tests for scanDirHierarchical + collectDefaultsChain + InheritanceGraph.
// Fixtures are built inline under t.TempDir() so tests are self-contained
// and don't depend on tests/golden/fixtures (which are used by the Python↔Go
// parity test — see config_golden_parity_test.go).

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
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

// TestScanDirHierarchical_K8sSymlinkLayout (A-8b, planning §12.2) locks
// the invariants around Kubernetes ConfigMap mount layouts:
//
//   conf.d/                        ← exporter mount root
//     _defaults.yaml  → real/_defaults.yaml        (file-symlink)
//     team-a/                                      (real directory)
//       tenant-a.yaml → ../real/tenant-a.yaml      (file-symlink)
//     sl-dir/         → real/                      (dir-symlink)
//     real/                                        (actual content)
//       _defaults.yaml
//       tenant-a.yaml
//       nested-only.yaml                           (only reachable via sl-dir)
//
// Go `filepath.WalkDir` under the hood uses `fs.DirEntry` + `Lstat`:
//   - **file-level symlinks** ARE followed when we call `os.ReadFile(path)`
//     (ReadFile uses Stat, which resolves symlinks) → content IS read
//   - **dir-level symlinks** are NOT recursed into (WalkDir sees them as
//     non-dir leaves via Lstat; we then call os.ReadFile which fails
//     with "is a directory" and we log+skip)
//
// K8s ConfigMap mount pattern flattens to file-level symlinks only
// (nested keys are legal via `/` in the key name becoming subdirs, but
// each leaf is a file-symlink). So file-symlinks must work; dir-symlinks
// must NOT cause double-walk or infinite loops.
//
// The production scanner relies on this behavior but never asserts it.
// Per Gemini R3 #1: invariant under-test → future Go stdlib change could
// silently regress. This test nails it down.
func TestScanDirHierarchical_K8sSymlinkLayout(t *testing.T) {
	// Skip on platforms that can't create symlinks without privilege.
	// Linux CI (ubuntu-latest) always works; Windows developer-mode
	// usually has symlinks enabled but CI runners may not.
	if runtime.GOOS == "windows" {
		t.Skip("symlink creation requires Windows Developer Mode; covered on Linux CI")
	}

	// Real content lives OUTSIDE the scan root. K8s ConfigMap mount does
	// the equivalent via `..data/` hidden by the dotfile prefix; here we
	// just keep the backing dir as a separate tmp to avoid the scanner
	// walking it directly (which would produce duplicate tenant IDs from
	// both the real path AND the symlink resolution).
	tmp := t.TempDir()
	root := filepath.Join(tmp, "root")
	backing := filepath.Join(tmp, "backing")

	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatalf("mkdir root: %v", err)
	}
	writeFile(t, filepath.Join(backing, "_defaults.yaml"), `defaults:
  level: L0
`)
	writeFile(t, filepath.Join(backing, "tenant-a.yaml"), `tenants:
  tenant-a:
    threshold:
      cpu: 90
`)
	writeFile(t, filepath.Join(backing, "nested-only.yaml"), `tenants:
  nested-only:
    threshold:
      cpu: 95
`)

	// File-symlink at root: should be followed (K8s-style key mount).
	if err := os.Symlink(
		filepath.Join(backing, "_defaults.yaml"),
		filepath.Join(root, "_defaults.yaml"),
	); err != nil {
		t.Fatalf("symlink root file: %v", err)
	}

	// File-symlink in subdirectory: K8s "team-a/tenant-a.yaml" key format.
	if err := os.MkdirAll(filepath.Join(root, "team-a"), 0o755); err != nil {
		t.Fatalf("mkdir team-a: %v", err)
	}
	if err := os.Symlink(
		filepath.Join(backing, "tenant-a.yaml"),
		filepath.Join(root, "team-a", "tenant-a.yaml"),
	); err != nil {
		t.Fatalf("symlink nested file: %v", err)
	}

	// Dir-symlink: should NOT be recursed into. `nested-only.yaml`
	// is ONLY reachable via this symlink path — so if we see it in
	// `tenants`, the scanner mistakenly followed the dir-symlink.
	if err := os.Symlink(
		backing,
		filepath.Join(root, "sl-dir"),
	); err != nil {
		t.Fatalf("symlink dir: %v", err)
	}

	tenants, defaults, hashes, _, graph, err := scanDirHierarchical(root, nil)
	if err != nil {
		t.Fatalf("scan: %v", err)
	}

	// Invariant #1: root-level file-symlink followed → _defaults.yaml
	// is registered as a defaults file.
	if len(defaults) == 0 {
		t.Errorf("defaults map empty; file-symlink at root not followed")
	}

	// Invariant #2: subdirectory file-symlink followed → tenant-a
	// discovered via team-a/tenant-a.yaml path.
	if _, ok := tenants["tenant-a"]; !ok {
		t.Errorf("tenant-a missing; nested file-symlink not followed (WalkDir behavior regressed)")
	}

	// Invariant #3: tenant-a has a non-empty defaults chain (L0 picked up
	// from the root symlink'd _defaults.yaml).
	if chain := graph.TenantDefaults["tenant-a"]; len(chain) == 0 {
		t.Errorf("tenant-a defaults chain empty; _defaults.yaml not in hierarchy")
	}

	// Invariant #4: dir-symlink NOT recursed — nested-only tenant MUST
	// NOT appear. If it does, scanner is double-walking (also potential
	// infinite-loop risk if the symlink pointed at an ancestor).
	if _, ok := tenants["nested-only"]; ok {
		t.Errorf("nested-only tenant discovered; scanner recursed into dir-symlink (should skip)")
	}

	// Invariant #5: exactly 2 yaml hashes (the two file-symlinks resolved).
	// If we see 3+, either dir-symlink got followed OR the backing dir
	// is visible inside the scan root.
	yamlCount := 0
	for path, h := range hashes {
		if filepath.Ext(path) == ".yaml" && h != "" {
			yamlCount++
		}
	}
	if yamlCount != 2 {
		t.Errorf("yaml hash count = %d, want exactly 2 (the two file-symlinks)", yamlCount)
	}
}

// TestScanDirHierarchical_MixedValidInvalid (A-8d, planning §12.2) locks
// the "poison pill isolation" invariant: a malformed YAML file in the
// scan tree must not block discovery / hashing of sibling valid files.
//
// Also verifies the v2.8.0 A-8d observability metric
// `da_config_parse_failure_total{file_basename=...}` is incremented
// so ops can alert on persistently broken tenant files.
//
// Per Gemini R3 #3: per-file error-skip already exists in scanner
// (config_hierarchy.go yaml.Unmarshal warn+return nil). This test nails
// the behavior down AND exposes the observability hook.
func TestScanDirHierarchical_MixedValidInvalid(t *testing.T) {
	root := t.TempDir()

	// Reset metrics so the parseFailures counter is fresh for assertion.
	// Save+restore rather than nil-out in cleanup — getConfigMetrics does
	// not re-init after sync.Once fires, so a nil substitution would crash
	// subsequent tests in the same process.
	origMetrics := getConfigMetrics()
	freshMetrics := newConfigMetrics()
	setConfigMetrics(freshMetrics)
	t.Cleanup(func() { setConfigMetrics(origMetrics) })

	// Valid sibling — must survive the broken neighbor.
	writeFile(t, filepath.Join(root, "team-a", "t-good.yaml"), `tenants:
  t-good:
    threshold:
      cpu: 80
`)

	// Poison pill — unclosed brace, YAML parser will error.
	writeFile(t, filepath.Join(root, "team-a", "broken.yaml"),
		"tenants:\n  broken-tenant:\n    threshold: {unclosed\n")

	// Second valid sibling in a different directory — broken.yaml in
	// team-a must not bleed into team-b scanning.
	writeFile(t, filepath.Join(root, "team-b", "t-ok.yaml"), `tenants:
  t-ok:
    threshold:
      cpu: 70
`)

	tenants, _, hashes, _, _, err := scanDirHierarchical(root, nil)
	if err != nil {
		t.Fatalf("scan should not return error on per-file parse failure: %v", err)
	}

	// Invariant #1: valid siblings discovered normally.
	if _, ok := tenants["t-good"]; !ok {
		t.Errorf("t-good missing; broken sibling poisoned the scan")
	}
	if _, ok := tenants["t-ok"]; !ok {
		t.Errorf("t-ok (different dir) missing; error propagated across dirs")
	}

	// Invariant #2: broken tenant NOT discovered (yaml.Unmarshal failed
	// before `decls` append). Good — we don't want ghost entries.
	if _, ok := tenants["broken-tenant"]; ok {
		t.Errorf("broken-tenant should NOT be registered; its YAML didn't parse")
	}

	// Invariant #3: broken.yaml IS in the hashes map (hash of raw bytes
	// happens before yaml parse). This is important for change detection
	// — if a tenant file becomes malformed, subsequent scans should still
	// notice the hash changed so WatchLoop can trigger a reload attempt.
	brokenHashFound := false
	for path, h := range hashes {
		if filepath.Base(path) == "broken.yaml" && h != "" {
			brokenHashFound = true
			break
		}
	}
	if !brokenHashFound {
		t.Errorf("broken.yaml not in hashes; change detection will miss recovery")
	}

	// Invariant #4: parse-failure metric incremented exactly once for
	// the broken file's basename. Pulls counter value via the test-only
	// collector API.
	ch := make(chan prometheus.Metric, 1)
	freshMetrics.parseFailures.WithLabelValues("broken.yaml").Collect(ch)
	close(ch)
	var count float64
	for m := range ch {
		var dto dto.Metric
		if err := m.Write(&dto); err != nil {
			t.Fatalf("metric.Write: %v", err)
		}
		count = dto.GetCounter().GetValue()
	}
	if count != 1 {
		t.Errorf("da_config_parse_failure_total{file_basename=broken.yaml} = %v, want 1", count)
	}
}
