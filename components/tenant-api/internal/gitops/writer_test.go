package gitops

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestValidate(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		tenantID string
		yaml     string
		wantErrs int // 0 = valid
	}{
		{
			name:     "valid tenant config",
			tenantID: "db-a",
			// ValidateTenantKeys warns on unknown keys not in defaults.
			// Keys starting with _ are reserved keys (silently accepted).
			yaml:     "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
			wantErrs: 0,
		},
		{
			name:     "invalid YAML syntax",
			tenantID: "db-a",
			yaml:     "{{not yaml",
			wantErrs: 1,
		},
		{
			name:     "missing tenant section",
			tenantID: "db-a",
			yaml:     "tenants:\n  db-b:\n    cpu_threshold: \"80\"\n",
			wantErrs: 1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			errs := validate(tt.tenantID, tt.yaml)
			if (len(errs) > 0) != (tt.wantErrs > 0) {
				t.Errorf("validate(%q, ...) returned %d errors %v, want %d",
					tt.tenantID, len(errs), errs, tt.wantErrs)
			}
		})
	}
}

// TestValidateVersionThreshold locks in the tenant-api boundary enforcement of
// the ADR-024 version label rules (the validate() path that Writer.Write blocks
// on). The threshold-exporter owns validateVersionLabel's unit tests; this proves
// those rules actually flow through the tenant-api's validate() so a bad version
// threshold is rejected at write time even if CI's da-guard is bypassed.
//
// NOTE: validate() does NOT merge the _defaults.yaml chain, so the YAML must
// carry a `defaults:` block for a metric base key to be recognised (otherwise it
// warns "unknown base metric" before reaching version validation). This mirrors
// the documented PUT-body requirement; the GET/PUT defaults asymmetry is tracked
// separately.
func TestValidateVersionThreshold(t *testing.T) {
	t.Parallel()
	const defaults = "defaults:\n  container_cpu: 80\n  container_memory: 85\n  mysql_cpu: 80\n"
	tests := []struct {
		name    string
		key     string // the tenant threshold key under db-a
		wantOK  bool   // true = no validation errors
		wantMsg string // substring expected in the (single) error when !wantOK
	}{
		{"valid exact version", `container_cpu{version="v2"}`, true, ""},
		{"valid dotted version", `container_cpu{version="v2.1.0"}`, true, ""},
		{"valid memory pilot version", `container_memory{version="v2"}`, true, ""},
		{"uppercase violates charset", `container_cpu{version="V2"}`, false, "violates"},
		{"regex matcher rejected", `container_cpu{version=~"v.*"}`, false, "regex version matcher"},
		{"regex matcher rejected (memory)", `container_memory{version=~"v.*"}`, false, "regex version matcher"},
		{"non-pilot metric rejected", `mysql_cpu{version="v2"}`, false, "non-pilot metric"},
		{"empty version rejected", `container_cpu{version=""}`, false, "empty version label"},
		{"literal default reserved", `container_cpu{version="default"}`, false, "reserved"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			y := defaults + "tenants:\n  db-a:\n    " + tt.key + ": \"60\"\n"
			errs := validate("db-a", y)
			if tt.wantOK {
				if len(errs) != 0 {
					t.Fatalf("validate(%q) = %v, want no errors", tt.key, errs)
				}
				return
			}
			if len(errs) == 0 {
				t.Fatalf("validate(%q) returned no errors, want a version-rule violation", tt.key)
			}
			joined := strings.Join(errs, "; ")
			if !strings.Contains(joined, tt.wantMsg) {
				t.Errorf("validate(%q) = %q, want substring %q", tt.key, joined, tt.wantMsg)
			}
		})
	}
}

// TestValidateMultiVersionCoexistence covers the core ADR-024 rolling scenario:
// a tenant declares DIFFERENT thresholds for two coexisting versions of the same
// metric (v1 baseline + v2 tightened). Both must validate cleanly — the per-
// version dimension is the whole point of the feature.
func TestValidateMultiVersionCoexistence(t *testing.T) {
	t.Parallel()
	y := "defaults:\n  container_cpu: 80\n" +
		"tenants:\n  db-a:\n" +
		"    container_cpu{version=\"v1\"}: \"80\"\n" +
		"    container_cpu{version=\"v2\"}: \"60\"\n"
	if errs := validate("db-a", y); len(errs) != 0 {
		t.Fatalf("coexisting v1+v2 thresholds should validate, got: %v", errs)
	}
}

// TestWrite_RejectsBadVersionThreshold proves the SECURITY property the PR
// claims — a bad version threshold cannot be committed via the API — by driving
// the real Writer.Write path (not just validate()). This is the mutation-
// resistant guard: if writer.go's `len(errs) > 0` block were removed or made to
// skip version warnings, THIS test fails (whereas a validate()-only test would
// still pass). The file must not be written on rejection (no partial commit).
func TestWrite_RejectsBadVersionThreshold(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	w := NewWriter(dir, dir)

	// version="V2" violates the ^[a-z0-9]... charset → validate() warns →
	// Write must block before touching disk.
	bad := "defaults:\n  container_cpu: 80\n" +
		"tenants:\n  db-a:\n    container_cpu{version=\"V2\"}: \"60\"\n"
	err := w.Write("db-a", "op@example.com", bad)
	if err == nil {
		t.Fatal("Write must reject an invalid version label, but returned nil")
	}
	if !strings.Contains(err.Error(), "validation failed") {
		t.Errorf("expected 'validation failed' in error, got: %v", err)
	}
	if !strings.Contains(err.Error(), "version") {
		t.Errorf("error should name the version violation, got: %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, "db-a.yaml")); statErr == nil {
		t.Error("db-a.yaml must not exist after a rejected version write")
	}
}

func TestNewWriter(t *testing.T) {
	t.Parallel()
	w := NewWriter("/conf.d", "")
	if w.gitDir != "/conf.d" {
		t.Errorf("gitDir should default to configDir, got %q", w.gitDir)
	}

	w2 := NewWriter("/conf.d", "/repo")
	if w2.gitDir != "/repo" {
		t.Errorf("gitDir should be /repo, got %q", w2.gitDir)
	}
}

func TestDiffNewFile(t *testing.T) {
	t.Parallel()
	// Diff against a non-existent file should show all lines as additions
	w := NewWriter(t.TempDir(), "")
	diff, err := w.Diff("new-tenant", "line1\nline2\n")
	if err != nil {
		t.Fatalf("Diff returned error: %v", err)
	}
	if !strings.Contains(diff, "+line1") {
		t.Errorf("Diff should show additions, got: %s", diff)
	}
}
