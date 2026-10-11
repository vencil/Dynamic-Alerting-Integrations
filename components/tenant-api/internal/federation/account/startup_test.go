package account

import (
	"context"

	"github.com/vencil/tenant-api/internal/statestore"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeFile is a tiny test helper: write content to <dir>/<name>.
func writeFile(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

// TestVerifyRegistryNotResetWithFleet_FleetNonEmptyBlankRegistry: the headline
// corruption — conf.d holds a tenant but the registry is blank (truncated) →
// must ERROR (refuse to start) so we never silently re-issue from the floor.
func TestVerifyRegistryNotResetWithFleet_FleetNonEmptyBlankRegistry(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
	writeFile(t, dir, RegistryFileName, "") // blank / 0-byte registry

	err := VerifyRegistryNotResetWithFleet(context.Background(), dir, dirStore(t, dir))
	if err == nil {
		t.Fatal("expected an error: blank registry + non-empty fleet must refuse to start")
	}
	if !strings.Contains(err.Error(), "refusing to start") {
		t.Errorf("error should explain the refusal, got: %v", err)
	}
}

// TestVerifyRegistryNotResetWithFleet_FleetNonEmptyMissingRegistry: a MISSING
// (not just blank) registry with a non-empty fleet is the same hazard.
func TestVerifyRegistryNotResetWithFleet_FleetNonEmptyMissingRegistry(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
	// No registry file at all.

	if err := VerifyRegistryNotResetWithFleet(context.Background(), dir, dirStore(t, dir)); err == nil {
		t.Fatal("expected an error: missing registry + non-empty fleet must refuse to start")
	}
}

// TestVerifyRegistryNotResetWithFleet_FleetNonEmptyWhitespaceRegistry: a
// whitespace-only registry counts as blank (isBlank), same as 0-byte.
func TestVerifyRegistryNotResetWithFleet_FleetNonEmptyWhitespaceRegistry(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
	writeFile(t, dir, RegistryFileName, "  \n\t\n")

	if err := VerifyRegistryNotResetWithFleet(context.Background(), dir, dirStore(t, dir)); err == nil {
		t.Fatal("expected an error: whitespace-only registry + non-empty fleet must refuse to start")
	}
}

// TestVerifyRegistryNotResetWithFleet_Day0BlankRegistryOK: an empty fleet with a
// blank/missing registry is a genuine Day-0 — must PASS (first onboarding will
// create the registry from the floor).
func TestVerifyRegistryNotResetWithFleet_Day0BlankRegistryOK(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	// No tenant files. A blank registry present...
	writeFile(t, dir, RegistryFileName, "")
	if err := VerifyRegistryNotResetWithFleet(context.Background(), dir, dirStore(t, dir)); err != nil {
		t.Errorf("Day-0 (empty fleet) + blank registry should be OK, got: %v", err)
	}

	// ...and also with NO registry file at all.
	dir2 := t.TempDir()
	if err := VerifyRegistryNotResetWithFleet(context.Background(), dir2, dirStore(t, dir2)); err != nil {
		t.Errorf("Day-0 (empty fleet) + missing registry should be OK, got: %v", err)
	}
}

// TestVerifyRegistryNotResetWithFleet_Day0IgnoresUnderscoreFiles: `_`-prefixed
// files (defaults / groups / the registry itself) are NOT tenants, so a conf.d
// with ONLY those + a blank registry is still Day-0 → OK.
func TestVerifyRegistryNotResetWithFleet_Day0IgnoresUnderscoreFiles(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "_defaults.yaml", "defaults:\n  mysql_threads_running: 80\n")
	writeFile(t, dir, "_groups.yaml", "groups: {}\n")
	writeFile(t, dir, RegistryFileName, "") // blank, but no real tenant → Day-0

	if err := VerifyRegistryNotResetWithFleet(context.Background(), dir, dirStore(t, dir)); err != nil {
		t.Errorf("only _-prefixed files + blank registry is Day-0, should be OK, got: %v", err)
	}
}

// TestVerifyRegistryNotResetWithFleet_PopulatedRegistryOK: a present, non-blank
// registry with a non-empty fleet is the normal steady state → OK.
func TestVerifyRegistryNotResetWithFleet_PopulatedRegistryOK(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
	writeFile(t, dir, RegistryFileName,
		"schema_version: v1\nnext_account_id: 1001\nallocations:\n  db-a: 1000\n")

	if err := VerifyRegistryNotResetWithFleet(context.Background(), dir, dirStore(t, dir)); err != nil {
		t.Errorf("populated registry + non-empty fleet is the steady state, should be OK, got: %v", err)
	}
}

// TestVerifyRegistryNotResetWithFleet_MissingConfigDir: a config dir that does
// not exist is a misconfiguration → error (don't boot silently).
func TestVerifyRegistryNotResetWithFleet_MissingConfigDir(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "does-not-exist")
	if err := VerifyRegistryNotResetWithFleet(context.Background(), missing, dirStore(t, t.TempDir())); err == nil {
		t.Fatal("expected an error for a non-existent config dir")
	}
}

// TestListTenantIDs_SkipsNonTenants pins the shared enumeration: only
// non-hidden, non-`_`-prefixed *.yaml/*.yml files count as tenants.
func TestListTenantIDs_SkipsNonTenants(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
	writeFile(t, dir, "db-b.yml", "tenants:\n  db-b: {}\n")
	writeFile(t, dir, "_defaults.yaml", "defaults: {}\n") // _-prefixed → skip
	writeFile(t, dir, RegistryFileName, "")               // _-prefixed → skip
	writeFile(t, dir, ".hidden.yaml", "x: 1\n")           // hidden → skip
	writeFile(t, dir, "notes.txt", "not yaml\n")          // wrong ext → skip

	ids, err := ListTenantIDs(dir)
	if err != nil {
		t.Fatalf("ListTenantIDs: %v", err)
	}
	got := map[string]bool{}
	for _, id := range ids {
		got[id] = true
	}
	if len(ids) != 2 || !got["db-a"] || !got["db-b"] {
		t.Errorf("ListTenantIDs = %v, want exactly [db-a db-b]", ids)
	}
}

// dirStore reads the registry where these tests write it: a directory store
// over dir holds the record as dir/_account_registry.yaml.
func dirStore(t *testing.T, dir string) statestore.Store {
	t.Helper()
	s, err := statestore.NewDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// #2884: an older registry put back over the live one. t-b's logs token,
// issued after the backup, carries 1001 — at the restored counter, so the
// next allocation would hand 1001 to someone else. Startup refuses.
func TestVerifyLiveTokensAgainstRegistry(t *testing.T) {
	ctx := context.Background()
	newDir := func(reg string) statestore.Store {
		s, err := statestore.NewDir(t.TempDir(), nil)
		if err != nil {
			t.Fatal(err)
		}
		if reg != "" {
			if _, err := s.Mutate(ctx, RegistryFileName, statestore.Meta{Entity: "test"},
				func([]byte) ([]byte, error) { return []byte(reg), nil }); err != nil {
				t.Fatal(err)
			}
		}
		return s
	}
	live := "schema_version: v1\nnext_account_id: 1002\nallocations:\n  t-a: 1000\n  t-b: 1001\n"
	old := "schema_version: v1\nnext_account_id: 1001\nallocations:\n  t-a: 1000\n"
	tokA := TokenAllocation{TenantID: "t-a", AccountID: 1000, TokenID: "fed_a"}
	tokB := TokenAllocation{TenantID: "t-b", AccountID: 1001, TokenID: "fed_b"}

	for name, c := range map[string]struct {
		reg    string
		tokens []TokenAllocation
		want   string // "" = passes
	}{
		"agrees":                 {live, []TokenAllocation{tokA, tokB}, ""},
		"no live tokens":         {old, nil, ""},
		"older backup applied":   {old, []TokenAllocation{tokA, tokB}, "at/above next_account_id 1001"},
		"registry lost":          {"", []TokenAllocation{tokA}, "at/above next_account_id 1000"},
		"tenant at another id":   {live, []TokenAllocation{{TenantID: "t-a", AccountID: 1001, TokenID: "fed_x"}}, "registry allocates 1000"},
		"tenant not in registry": {"schema_version: v1\nnext_account_id: 1005\nallocations:\n  t-a: 1000\n", []TokenAllocation{tokB}, "no allocation for the tenant"},
	} {
		err := VerifyLiveTokensAgainstRegistry(ctx, newDir(c.reg), c.tokens)
		switch {
		case c.want == "" && err != nil:
			t.Errorf("%s: refused: %v", name, err)
		case c.want != "" && (err == nil || !strings.Contains(err.Error(), c.want)):
			t.Errorf("%s: err = %v, want it to name %q", name, err, c.want)
		}
	}
}
