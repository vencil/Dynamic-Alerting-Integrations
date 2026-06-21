package account

import (
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

	err := VerifyRegistryNotResetWithFleet(dir)
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

	if err := VerifyRegistryNotResetWithFleet(dir); err == nil {
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

	if err := VerifyRegistryNotResetWithFleet(dir); err == nil {
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
	if err := VerifyRegistryNotResetWithFleet(dir); err != nil {
		t.Errorf("Day-0 (empty fleet) + blank registry should be OK, got: %v", err)
	}

	// ...and also with NO registry file at all.
	dir2 := t.TempDir()
	if err := VerifyRegistryNotResetWithFleet(dir2); err != nil {
		t.Errorf("Day-0 (empty fleet) + missing registry should be OK, got: %v", err)
	}
}

// TestVerifyRegistryNotResetWithFleet_Day0IgnoresUnderscoreFiles: `_`-prefixed
// files (defaults / groups / the registry itself) are NOT tenants, so a conf.d
// with ONLY those + a blank registry is still Day-0 → OK.
func TestVerifyRegistryNotResetWithFleet_Day0IgnoresUnderscoreFiles(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, dir, "_defaults.yaml", "defaults:\n  mysql_cpu: 80\n")
	writeFile(t, dir, "_groups.yaml", "groups: {}\n")
	writeFile(t, dir, RegistryFileName, "") // blank, but no real tenant → Day-0

	if err := VerifyRegistryNotResetWithFleet(dir); err != nil {
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

	if err := VerifyRegistryNotResetWithFleet(dir); err != nil {
		t.Errorf("populated registry + non-empty fleet is the steady state, should be OK, got: %v", err)
	}
}

// TestVerifyRegistryNotResetWithFleet_MissingConfigDir: a config dir that does
// not exist is a misconfiguration → error (don't boot silently).
func TestVerifyRegistryNotResetWithFleet_MissingConfigDir(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "does-not-exist")
	if err := VerifyRegistryNotResetWithFleet(missing); err == nil {
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
