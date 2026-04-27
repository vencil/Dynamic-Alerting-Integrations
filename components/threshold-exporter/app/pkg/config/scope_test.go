package config

// scope_test.go — exercises ScopeEffective against fabricated conf.d
// trees. We don't reuse tests/golden/fixtures here because those are
// pinned to ResolveEffective's hash output and we don't want
// scope-loop changes to be coupled to the merged_hash contract.
//
// Determinism, containment safety, and the duplicate-tenant guard
// are the three behaviours worth pinning — everything else is
// inherited from ResolveEffective and tested there.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// writeTree renders a literal directory layout for a single test.
// Keys are forward-slash repo-relative paths under tmpdir; values
// are the file body. Empty value means "create the directory".
func writeTree(t *testing.T, tmp string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		clean := filepath.Join(tmp, filepath.FromSlash(rel))
		if body == "" {
			if err := os.MkdirAll(clean, 0o755); err != nil {
				t.Fatalf("mkdir %q: %v", clean, err)
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(clean), 0o755); err != nil {
			t.Fatalf("mkdir parent of %q: %v", clean, err)
		}
		if err := os.WriteFile(clean, []byte(body), 0o644); err != nil {
			t.Fatalf("write %q: %v", clean, err)
		}
	}
}

func TestScopeEffective_WholeTree(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":        "defaults:\n  cpu: 70\n",
		"conf.d/db/_defaults.yaml":     "defaults:\n  cpu: 80\n",
		"conf.d/db/tenant-a.yaml":      "tenants:\n  tenant-a:\n    cpu: 85\n",
		"conf.d/db/prod/tenant-b.yaml": "tenants:\n  tenant-b:\n    cpu: 90\n",
		"conf.d/web/tenant-c.yaml":     "tenants:\n  tenant-c:\n    cpu: 95\n",
	})
	root := filepath.Join(tmp, "conf.d")
	got, err := ScopeEffective(root, "")
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(got.Tenants) != 3 {
		t.Fatalf("got %d tenants, want 3", len(got.Tenants))
	}
	want := []string{"tenant-a", "tenant-b", "tenant-c"}
	for i, ec := range got.Tenants {
		if ec.TenantID != want[i] {
			t.Errorf("tenant[%d] = %q, want %q (sort drift)", i, ec.TenantID, want[i])
		}
	}
	// Spot-check the chain made it through ResolveEffective: tenant-b
	// should have inherited L0 + L1 (db) defaults.
	for _, ec := range got.Tenants {
		if ec.TenantID != "tenant-b" {
			continue
		}
		if len(ec.DefaultsChain) != 2 {
			t.Errorf("tenant-b chain = %v, want length 2", ec.DefaultsChain)
		}
		// Effective cpu must be the tenant's 90, override winning over both defaults.
		if v, _ := ec.EffectiveConfig["cpu"].(int); v != 90 {
			t.Errorf("tenant-b cpu = %v (type %T), want 90", ec.EffectiveConfig["cpu"], ec.EffectiveConfig["cpu"])
		}
	}
}

func TestScopeEffective_SubScope(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":    "defaults:\n  cpu: 70\n",
		"conf.d/db/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 85\n",
		"conf.d/web/tenant-c.yaml": "tenants:\n  tenant-c:\n    cpu: 95\n",
	})
	root := filepath.Join(tmp, "conf.d")
	scope := filepath.Join(root, "db")
	got, err := ScopeEffective(root, scope)
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(got.Tenants) != 1 {
		t.Fatalf("got %d tenants, want 1 (only db/ subtree)", len(got.Tenants))
	}
	if got.Tenants[0].TenantID != "tenant-a" {
		t.Errorf("got %q, want tenant-a", got.Tenants[0].TenantID)
	}
	// Source files should be repo-relative under root, with forward
	// slashes regardless of OS (matches DefaultsChain shape).
	want := "db/tenant-a.yaml"
	if len(got.SourceFiles) != 1 || got.SourceFiles[0] != want {
		t.Errorf("SourceFiles = %v, want [%s]", got.SourceFiles, want)
	}
}

func TestScopeEffective_ScopeOutsideRoot(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":  "defaults: {}\n",
		"other/tenant-evil.yaml": "tenants:\n  evil:\n    cpu: 1\n",
	})
	root := filepath.Join(tmp, "conf.d")
	scope := filepath.Join(tmp, "other")
	_, err := ScopeEffective(root, scope)
	if err == nil {
		t.Fatal("expected containment error, got nil")
	}
	if !strings.Contains(err.Error(), "outside configDir") {
		t.Errorf("error %q should mention containment", err.Error())
	}
}

func TestScopeEffective_DuplicateTenant(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":       "defaults: {}\n",
		"conf.d/a/tenant-x.yaml":      "tenants:\n  tenant-x:\n    cpu: 1\n",
		"conf.d/b/tenant-x-copy.yaml": "tenants:\n  tenant-x:\n    cpu: 2\n",
	})
	root := filepath.Join(tmp, "conf.d")
	_, err := ScopeEffective(root, "")
	if err == nil {
		t.Fatal("expected duplicate-tenant error, got nil")
	}
	if !strings.Contains(err.Error(), "duplicate tenant ID") {
		t.Errorf("error %q should call out duplicate", err.Error())
	}
}

func TestScopeEffective_EmptyScopeReturnsEmptySet(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"conf.d/empty/":         "",
	})
	root := filepath.Join(tmp, "conf.d")
	scope := filepath.Join(root, "empty")
	got, err := ScopeEffective(root, scope)
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(got.Tenants) != 0 {
		t.Errorf("expected zero tenants, got %d", len(got.Tenants))
	}
	if got.SourceFiles != nil {
		t.Errorf("expected nil SourceFiles, got %v", got.SourceFiles)
	}
}

func TestScopeEffective_ConfigDirMissing(t *testing.T) {
	_, err := ScopeEffective(filepath.Join(t.TempDir(), "nope"), "")
	if err == nil {
		t.Fatal("expected stat error, got nil")
	}
	if !strings.Contains(err.Error(), "stat configDir") {
		t.Errorf("error %q should mention stat", err.Error())
	}
}

func TestScopeEffective_SkipsHiddenAndUnderscoredFiles(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":   "defaults: {}\n",
		"conf.d/_profiles.yaml":   "tenants:\n  not-a-tenant:\n    cpu: 1\n",
		"conf.d/.hidden.yaml":     "tenants:\n  hidden-tenant:\n    cpu: 1\n",
		"conf.d/tenant-real.yaml": "tenants:\n  real:\n    cpu: 1\n",
	})
	root := filepath.Join(tmp, "conf.d")
	got, err := ScopeEffective(root, "")
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(got.Tenants) != 1 || got.Tenants[0].TenantID != "real" {
		t.Errorf("got tenants = %v, want only [real]", tenantIDsOf(got.Tenants))
	}
}

func TestScopeEffective_DeterministicAcrossRuns(t *testing.T) {
	// Three tenants in different subdirs; both runs should return
	// the same alphabetical order regardless of filesystem
	// enumeration whim.
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":  "defaults: {}\n",
		"conf.d/z/tenant-z.yaml": "tenants:\n  z:\n    cpu: 1\n",
		"conf.d/m/tenant-m.yaml": "tenants:\n  m:\n    cpu: 1\n",
		"conf.d/a/tenant-a.yaml": "tenants:\n  a:\n    cpu: 1\n",
	})
	root := filepath.Join(tmp, "conf.d")
	for i := 0; i < 3; i++ {
		got, err := ScopeEffective(root, "")
		if err != nil {
			t.Fatalf("ScopeEffective run %d: %v", i, err)
		}
		ids := tenantIDsOf(got.Tenants)
		want := []string{"a", "m", "z"}
		if !sliceEqual(ids, want) {
			t.Errorf("run %d order = %v, want %v", i, ids, want)
		}
	}
}

// Sanity: the symlink-tolerance behavior of WalkDir on POSIX
// matches what the exporter relies on at runtime (file-level
// symlinks ARE followed, dir-level symlinks are NOT). This is
// pinned in app/config_hierarchy_test.go::TestScanDirHierarchical_K8sSymlinkLayout
// — the scope walker uses the same WalkDir semantics, so we only
// add a small smoke test here.
func TestScopeEffective_FileSymlink(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlinks need admin privileges on Windows; covered by Linux CI")
	}
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"actual/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 1\n",
	})
	target := filepath.Join(tmp, "actual", "tenant-a.yaml")
	link := filepath.Join(tmp, "conf.d", "tenant-a-link.yaml")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("symlink unsupported on this filesystem: %v", err)
	}
	got, err := ScopeEffective(filepath.Join(tmp, "conf.d"), "")
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(got.Tenants) != 1 {
		t.Errorf("expected 1 tenant via symlink, got %d", len(got.Tenants))
	}
}

// --- helpers ---

func tenantIDsOf(ts []*EffectiveConfig) []string {
	out := make([]string, 0, len(ts))
	for _, t := range ts {
		out = append(out, t.TenantID)
	}
	return out
}

func sliceEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
