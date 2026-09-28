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
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// #2123: a file the decode rejects is still err == nil (the walker skips it,
// as the exporter does) but is listed in ParseFailed — relative, sorted, and
// only when it lies at-or-below the scope.
func TestScopeEffective_ListsParseFailedFilesInScope(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":    "defaults:\n  cpu: 70\n",
		"conf.d/a/tenant-a.yaml":   "tenants:\n  tenant-a:\n    cpu: 80\n",
		"conf.d/a/z-dup-key.yaml":  "tenants:\n  tenant-z:\n    cpu: 1\n    cpu: 2\n",
		"conf.d/a/b-syntax.yaml":   "tenants: [unclosed\n",
		"conf.d/other/broken.yaml": "tenants:\ntenants:\n",
	})
	root := filepath.Join(tmp, "conf.d")

	got, err := ScopeEffective(root, filepath.Join(root, "a"))
	if err != nil {
		t.Fatalf("ScopeEffective err = %v, want nil (a broken file is skipped, not fatal)", err)
	}
	want := []string{"a/b-syntax.yaml", "a/z-dup-key.yaml"}
	if strings.Join(got.ParseFailed, ",") != strings.Join(want, ",") {
		t.Errorf("ParseFailed = %v, want %v", got.ParseFailed, want)
	}
	if len(got.Tenants) != 1 || got.Tenants[0].TenantID != "tenant-a" {
		t.Errorf("Tenants = %v, want only tenant-a", got.Tenants)
	}

	whole, err := ScopeEffective(root, "")
	if err != nil {
		t.Fatalf("whole tree err = %v", err)
	}
	if len(whole.ParseFailed) != 3 {
		t.Errorf("whole-tree ParseFailed = %v, want 3 files", whole.ParseFailed)
	}
}

// #2179: ParseFailed is the exporter's own build's parse-failure list, so a
// root `_` platform file the decode rejects is listed even when the scope is
// a subdirectory (it shapes every tenant there), while a nested
// `_defaults.yaml` with a wrong-typed value is not (the exporter skips that
// key, not the file). A duplicate tenant outside the scope still does not
// fail it — the exporter's build runs on a scan with no inheritance graph.
func TestScopeEffective_ParseFailedIsTheExportersVerdict(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":   "defaults:\n  cpu: abc\n",
		"conf.d/_profiles.yaml":   "profiles:\n  gold:\n    mem: 1\n    mem: 2\n",
		"conf.d/a/_defaults.yaml": "defaults:\n  mem: abc\n",
		"conf.d/a/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
		"conf.d/b/_defaults.yaml": "defaults: [unclosed\n",
		"conf.d/x/one.yaml":       "tenants:\n  tenant-x: {}\n",
		"conf.d/x/two.yaml":       "tenants:\n  tenant-x: {}\n",
	})
	root := filepath.Join(tmp, "conf.d")

	got, err := ScopeEffective(root, filepath.Join(root, "a"))
	if err != nil {
		t.Fatalf("ScopeEffective err = %v", err)
	}
	want := []string{"_defaults.yaml", "_profiles.yaml"}
	if strings.Join(got.ParseFailed, ",") != strings.Join(want, ",") {
		t.Errorf("ParseFailed = %v, want %v (b/_defaults.yaml is outside the scope; "+
			"a/_defaults.yaml's type error is not a dropped file)", got.ParseFailed, want)
	}
}

// #2123 round 2: a decode failure met while resolving (a chain defaults file,
// or a tenant file the walker accepted) is still an error of ScopeEffective,
// with its historical text, but errors.As finds a *DecodeError naming the file.
func TestScopeEffective_ResolveDecodeFailureIsADecodeError(t *testing.T) {
	for _, tc := range []struct {
		name, want, text string
		tree             map[string]string
	}{
		{"defaults", "sub/_defaults.yaml", "parse defaults[1]:", map[string]string{
			"conf.d/_defaults.yaml":     "defaults:\n  cpu: 70\n",
			"conf.d/sub/_defaults.yaml": "defaults:\n  cpu: 1\n  cpu: 2\n",
			"conf.d/sub/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
		}},
		{"tenant", "tenant-b.yaml", "parse tenant:", map[string]string{
			"conf.d/tenant-b.yaml": "extra:\n  k: 1\n  k: 2\ntenants:\n  tenant-b:\n    cpu: 80\n",
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tmp := t.TempDir()
			testutil.WriteTree(t, tmp, tc.tree)
			_, err := ScopeEffective(filepath.Join(tmp, "conf.d"), "")
			if err == nil {
				t.Fatal("want an error")
			}
			var de *DecodeError
			if !errors.As(err, &de) || de.Path != tc.want {
				t.Fatalf("errors.As DecodeError = %v (path %q), want path %q; err=%v", de != nil, pathOf(de), tc.want, err)
			}
			if !strings.Contains(err.Error(), "resolve tenant") || !strings.Contains(err.Error(), tc.text) {
				t.Errorf("error text changed: %v", err)
			}
		})
	}
}

func pathOf(de *DecodeError) string {
	if de == nil {
		return ""
	}
	return de.Path
}

func TestScopeEffective_WholeTree(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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

// PR-5: confirm the EffectiveConfig.MergedDefaults + TenantOverridesRaw
// pre-merge snapshots are populated and reflect the chain at each
// tenant's level. This is the contract C-12 redundant-override depends
// on (different tenants under cascading _defaults.yaml see different
// merged defaults).
func TestScopeEffective_ExposesPreMergeSnapshots(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":    "defaults:\n  cpu: 70\n",
		"conf.d/db/_defaults.yaml": "defaults:\n  cpu: 80\n",
		"conf.d/db/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n", // redundant! same as L1 db defaults
		"conf.d/web/tenant-c.yaml": "tenants:\n  tenant-c:\n    cpu: 80\n", // NOT redundant — web inherits L0 (cpu=70)
	})
	root := filepath.Join(tmp, "conf.d")
	got, err := ScopeEffective(root, "")
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	for _, ec := range got.Tenants {
		// Both new fields must be non-nil for any tenant the resolver
		// returns successfully; the guard caller keys off nilness as
		// "skip this tenant".
		if ec.TenantOverridesRaw == nil {
			t.Errorf("tenant %q: TenantOverridesRaw nil; expected raw override map", ec.TenantID)
		}
		if ec.MergedDefaults == nil {
			t.Errorf("tenant %q: MergedDefaults nil; expected pre-merge defaults snapshot", ec.TenantID)
		}
		switch ec.TenantID {
		case "tenant-a":
			// tenant-a's MergedDefaults = L0+L1 merged → cpu=80
			if v, _ := ec.MergedDefaults["cpu"].(int); v != 80 {
				t.Errorf("tenant-a MergedDefaults.cpu = %v, want 80 (L1 wins L0)", ec.MergedDefaults["cpu"])
			}
			// tenant-a's raw override = {cpu: 80} → matches MergedDefaults → redundant
			if v, _ := ec.TenantOverridesRaw["cpu"].(int); v != 80 {
				t.Errorf("tenant-a TenantOverridesRaw.cpu = %v, want 80", ec.TenantOverridesRaw["cpu"])
			}
		case "tenant-c":
			// tenant-c's MergedDefaults = L0 only → cpu=70
			if v, _ := ec.MergedDefaults["cpu"].(int); v != 70 {
				t.Errorf("tenant-c MergedDefaults.cpu = %v, want 70 (L0 only, no db/_defaults)", ec.MergedDefaults["cpu"])
			}
			// tenant-c's override is 80; doesn't match its 70 inherited → NOT redundant
		}
	}
}

// PR-5: MergedDefaults must NOT alias EffectiveConfig — they share
// inputs but a mutation to one must not be visible in the other.
// This guards the "snapshot before tenant merge" contract that
// computeEffectiveConfigBytesDetailed promises.
func TestScopeEffective_MergedDefaultsIsIndependentSnapshot(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n  shared:\n    nested: 1\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 99\n",
	})
	root := filepath.Join(tmp, "conf.d")
	got, err := ScopeEffective(root, "")
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	if len(got.Tenants) != 1 {
		t.Fatalf("got %d tenants, want 1", len(got.Tenants))
	}
	ec := got.Tenants[0]
	// Mutate MergedDefaults; EffectiveConfig must be untouched.
	if m, ok := ec.MergedDefaults["shared"].(map[string]any); ok {
		m["nested"] = 999
	}
	// Re-read effective.shared.nested via path traversal; should still be 1
	// because effective is a separate map post-deepMerge.
	if shared, ok := ec.EffectiveConfig["shared"].(map[string]any); ok {
		if v, _ := shared["nested"].(int); v != 1 {
			t.Errorf("EffectiveConfig.shared.nested = %v, want 1 (snapshot aliasing leak)", shared["nested"])
		}
	} else {
		t.Errorf("EffectiveConfig.shared missing or wrong type: %T", ec.EffectiveConfig["shared"])
	}
}

func TestScopeEffective_SubScope(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	testutil.WriteTree(t, tmp, map[string]string{
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
	testutil.WriteTree(t, tmp, map[string]string{
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
	// C6-A (#127): the walker now returns a typed *DuplicateTenantError so
	// library consumers can errors.As it instead of string-matching. Pin
	// that the type unwraps at the pkg/config layer AND that its fields
	// carry the offending tenant + both files (paths differ, non-empty).
	var dupErr *DuplicateTenantError
	if !errors.As(err, &dupErr) {
		t.Fatalf("error should unwrap to *DuplicateTenantError, got %T: %v", err, err)
	}
	if dupErr.TenantID != "tenant-x" {
		t.Errorf("DuplicateTenantError.TenantID = %q, want tenant-x", dupErr.TenantID)
	}
	if dupErr.PathA == "" || dupErr.PathB == "" || dupErr.PathA == dupErr.PathB {
		t.Errorf("DuplicateTenantError paths malformed: A=%q B=%q", dupErr.PathA, dupErr.PathB)
	}
}

func TestScopeEffective_EmptyScopeReturnsEmptySet(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	testutil.WriteTree(t, tmp, map[string]string{
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
	testutil.WriteTree(t, tmp, map[string]string{
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
	testutil.WriteTree(t, tmp, map[string]string{
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
