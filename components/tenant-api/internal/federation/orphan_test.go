package federation

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

func TestScanOrphans(t *testing.T) {
	known := map[string]struct{}{"db-a": {}, "db-b": {}}
	records := []Record{
		{TokenID: "ftk_live_a", TenantID: "db-a"},
		{TokenID: "ftk_zombie", TenantID: "db-gone"},
		{TokenID: "ftk_live_b", TenantID: "db-b"},
	}
	subsets := []string{"db-b", "db-stale", "db-a"}

	rep := scanOrphans(known, records, subsets)

	if !reflect.DeepEqual(rep.Tokens, []string{"ftk_zombie"}) {
		t.Errorf("orphaned tokens = %v, want [ftk_zombie]", rep.Tokens)
	}
	if !reflect.DeepEqual(rep.Subsets, []string{"db-stale"}) {
		t.Errorf("orphaned subsets = %v, want [db-stale]", rep.Subsets)
	}
}

func TestScanOrphans_AllKnown(t *testing.T) {
	known := map[string]struct{}{"db-a": {}}
	rep := scanOrphans(known,
		[]Record{{TokenID: "t1", TenantID: "db-a"}},
		[]string{"db-a"})
	if !rep.empty() {
		t.Errorf("expected no orphans, got %+v", rep)
	}
}

func TestScanOrphans_Empty(t *testing.T) {
	rep := scanOrphans(map[string]struct{}{}, nil, nil)
	if !rep.empty() {
		t.Errorf("expected empty report, got %+v", rep)
	}
}

// All tenants absent (the conf.d-wiped disaster). The detector reports
// everything orphaned — a true, alarming signal, not damage: it only
// observes and never revokes, so there is no misfire to guard against.
func TestScanOrphans_AllOrphaned(t *testing.T) {
	rep := scanOrphans(map[string]struct{}{},
		[]Record{{TokenID: "t1", TenantID: "db-a"}, {TokenID: "t2", TenantID: "db-b"}},
		[]string{"db-a"})
	if len(rep.Tokens) != 2 || len(rep.Subsets) != 1 {
		t.Errorf("expected 2 tokens + 1 subset orphaned, got %+v", rep)
	}
}

func TestScanKnownTenants(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{
		"db-a.yaml", "db-b.yml", // tenants
		"_defaults.yaml", "_rbac.yaml", "_federation_policy.yaml", // specials — skipped
		"notes.txt", // non-YAML — skipped
	} {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("{}"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	// a subdirectory (e.g. _federation/) — skipped
	if err := os.Mkdir(filepath.Join(dir, federationSubsetDir), 0o700); err != nil {
		t.Fatal(err)
	}
	known, err := scanKnownTenants(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]struct{}{"db-a": {}, "db-b": {}}
	if !reflect.DeepEqual(known, want) {
		t.Errorf("known tenants = %v, want %v", known, want)
	}
}

// A read error must surface as an error — never as an empty set, which
// scanOrphans would read as "every tenant is gone".
func TestScanKnownTenants_ReadError(t *testing.T) {
	if _, err := scanKnownTenants(filepath.Join(t.TempDir(), "nope")); err == nil {
		t.Fatal("expected an error for a missing configDir, got nil")
	}
}

func TestScanSubsetTenants(t *testing.T) {
	dir := t.TempDir()
	fedDir := filepath.Join(dir, federationSubsetDir)
	if err := os.Mkdir(fedDir, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"db-a.yaml", "db-stale.yaml", "README.md"} {
		if err := os.WriteFile(filepath.Join(fedDir, f), []byte("{}"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	got, err := scanSubsetTenants(dir)
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(got)
	want := []string{"db-a", "db-stale"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("subset tenants = %v, want %v", got, want)
	}
}

// A missing _federation/ directory is the common case (no tenant has
// configured a subset) — it must not be an error.
func TestScanSubsetTenants_NoDir(t *testing.T) {
	got, err := scanSubsetTenants(t.TempDir())
	if err != nil {
		t.Fatalf("missing _federation/ should not error: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("expected no subset tenants, got %v", got)
	}
}
