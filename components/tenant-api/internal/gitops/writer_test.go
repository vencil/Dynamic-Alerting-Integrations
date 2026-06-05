package gitops

import (
	"context"
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
			errs := validate("", tt.tenantID, tt.yaml)
			if (len(errs) > 0) != (tt.wantErrs > 0) {
				t.Errorf("validate(%q, ...) returned %d errors %v, want %d",
					tt.tenantID, len(errs), errs, tt.wantErrs)
			}
		})
	}
}

// pilotDefaultsDir creates a configDir containing a root _defaults.yaml with the
// ADR-024 pilot metrics, so validate()/Write resolve a *tenant-only* body's
// metric keys against the inherited platform defaults — the production shape
// (conf.d/{id}.yaml carries "Only 'tenants' block"; defaults live in
// _defaults.yaml). Returns the directory path.
func pilotDefaultsDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	const defaults = "defaults:\n  container_cpu: 80\n  container_memory: 85\n  mysql_cpu: 80\n"
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults), 0o644); err != nil {
		t.Fatalf("write _defaults.yaml: %v", err)
	}
	return dir
}

// TestValidateVersionThreshold locks in the tenant-api boundary enforcement of
// the ADR-024 version label rules (the validate() path that Writer.Write blocks
// on). The threshold-exporter owns validateVersionLabel's unit tests; this proves
// those rules actually flow through the tenant-api's validate() so a bad version
// threshold is rejected at write time even if CI's da-guard is bypassed.
//
// The body is a real tenant-only document; the metric base key is recognised by
// merging the on-disk _defaults.yaml (ADR-024 PR4 / #704 closed the prior
// write-vs-read asymmetry that forced tests to inline a `defaults:` block here).
func TestValidateVersionThreshold(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)
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
			y := "tenants:\n  db-a:\n    " + tt.key + ": \"60\"\n"
			errs := validate(dir, "db-a", y)
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
// metric (v1 baseline + v2 tightened). Both must validate cleanly from a
// tenant-only body — the per-version dimension is the whole point of the feature.
func TestValidateMultiVersionCoexistence(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)
	y := "tenants:\n  db-a:\n" +
		"    container_cpu{version=\"v1\"}: \"80\"\n" +
		"    container_cpu{version=\"v2\"}: \"60\"\n"
	if errs := validate(dir, "db-a", y); len(errs) != 0 {
		t.Fatalf("coexisting v1+v2 thresholds should validate, got: %v", errs)
	}
}

// TestValidate_TenantOnlyMetricBody is the regression guard for the ADR-024 PR4
// / #704 write-vs-read defaults asymmetry. A tenant-only PUT body (the real
// conf.d/{id}.yaml shape) carrying plain metric thresholds must validate clean
// once the _defaults.yaml chain is merged — the same body POST /validate already
// blesses. Before the fix, validate() saw an empty Defaults map and flagged every
// metric key as "unknown key not in defaults", blocking the write.
func TestValidate_TenantOnlyMetricBody(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)

	// Mirrors conf.d/db-a.yaml: tenants block only, plain metric keys.
	ok := "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n    container_cpu: \"70\"\n"
	if errs := validate(dir, "db-a", ok); len(errs) != 0 {
		t.Fatalf("tenant-only metric body should validate against merged defaults, got: %v", errs)
	}

	// Negative: validation is NOT neutered — a genuinely unknown metric key
	// (absent from _defaults.yaml, not a reserved key) still warns.
	bad := "tenants:\n  db-a:\n    not_a_real_metric: \"70\"\n"
	errs := validate(dir, "db-a", bad)
	if len(errs) == 0 {
		t.Fatal("an unknown metric key must still warn after the defaults merge")
	}
	if !strings.Contains(strings.Join(errs, "; "), "not_a_real_metric") {
		t.Errorf("warning should name the unknown key, got: %v", errs)
	}
}

// TestValidate_RejectsNonTenantRootKeys is the #705 fold-in guard: a body that
// carries any top-level key other than `tenants` (a stray defaults/state_filters/
// profiles block, or a typo) must be rejected at the write boundary, so the API
// never persists a conf.d/{id}.yaml that violates its "Only 'tenants' block"
// invariant. Pairs with the same check in POST /{id}/validate (dry-run parity).
func TestValidate_RejectsNonTenantRootKeys(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)
	tests := []struct {
		name string
		yaml string
	}{
		{"full config with defaults", "defaults:\n  container_cpu: 80\ntenants:\n  db-a:\n    container_cpu: \"70\"\n"},
		{"stray state_filters", "state_filters:\n  x:\n    reasons: []\ntenants:\n  db-a:\n    container_cpu: \"70\"\n"},
		{"typo'd tenants key", "tenant:\n  db-a:\n    container_cpu: \"70\"\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			errs := validate(dir, "db-a", tt.yaml)
			if len(errs) == 0 {
				t.Fatalf("validate(%q) returned no errors, want a root-key rejection", tt.yaml)
			}
			joined := strings.Join(errs, "; ")
			if !strings.Contains(joined, "only") || !strings.Contains(joined, "tenants") {
				t.Errorf("validate(%q) = %q, want a 'only tenants' root-key message", tt.yaml, joined)
			}
		})
	}
}

// TestValidate_RejectsFlatKV codifies a long-standing invariant that surfaced in
// adversarial review: the write path has NEVER accepted a flat key-value body
// (metric keys at the document root, no `tenants:` wrapper). Before the #705
// root-key guard it was rejected by the structural "must contain tenants.{id}"
// check; now it is rejected one step earlier by CheckTenantRootKeys. Either way
// the OUTCOME is unchanged — a flat body is rejected, never silently wrapped on
// write. (The flat-KV fallback in MergeTenantWithRootDefaults serves the GET read
// path for legacy on-disk files, not the write path.) This locks the contract so
// a future refactor cannot quietly start accepting flat bodies on write.
func TestValidate_RejectsFlatKV(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)
	flat := "container_cpu: \"80\"\nmysql_cpu: \"70\"\n" // no tenants: wrapper
	errs := validate(dir, "db-a", flat)
	if len(errs) == 0 {
		t.Fatal("flat key-value body must be rejected on the write path, got no errors")
	}
	joined := strings.Join(errs, "; ")
	if !strings.Contains(joined, "container_cpu") && !strings.Contains(joined, "tenants") {
		t.Errorf("rejection should name the offending root key or the tenants requirement, got: %q", joined)
	}
}

// TestWrite_RejectsFullConfigBody drives the real Writer.Write path: a body that
// smuggles a top-level defaults block must be rejected (not committed verbatim
// as a dirty file). Mutation-resistant: removing the root-key guard fails here.
func TestWrite_RejectsFullConfigBody(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)
	full := "defaults:\n  container_cpu: 80\ntenants:\n  db-a:\n    container_cpu: \"70\"\n"
	if err := w.Write(context.Background(), "db-a", "op@example.com", full); err == nil {
		t.Fatal("Write must reject a body with a non-tenants root key, but returned nil")
	}
	if _, statErr := os.Stat(filepath.Join(dir, "db-a.yaml")); statErr == nil {
		t.Error("db-a.yaml must not exist after a rejected full-config write")
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
	dir := pilotDefaultsDir(t)
	w := NewWriter(dir, dir)

	// version="V2" violates the ^[a-z0-9]... charset → validate() warns →
	// Write must block before touching disk. Body is tenant-only.
	bad := "tenants:\n  db-a:\n    container_cpu{version=\"V2\"}: \"60\"\n"
	err := w.Write(context.Background(), "db-a", "op@example.com", bad)
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

// TestWrite_TenantOnlyMetricBody_Commits proves the fix end-to-end through the
// real Writer.Write path: a tenant-only body with metric thresholds commits
// successfully (no validation block) AND is persisted verbatim — defaults are
// merged for VALIDATION only, never written into the tenant file (which would
// pollute conf.d's "Only 'tenants' block" invariant).
func TestWrite_TenantOnlyMetricBody_Commits(t *testing.T) {
	t.Parallel()
	dir := pilotDefaultsDir(t)
	initGitRepo(t, dir)

	w := NewWriter(dir, dir)
	body := "tenants:\n  db-a:\n    container_cpu: \"70\"\n"
	if err := w.Write(context.Background(), "db-a", "op@example.com", body); err != nil {
		t.Fatalf("tenant-only metric body must commit, got: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		t.Fatalf("read written file: %v", err)
	}
	if string(got) != body {
		t.Errorf("file must be written verbatim (tenant-only, no defaults pollution)\n got: %q\nwant: %q", string(got), body)
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
