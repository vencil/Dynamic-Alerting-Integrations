package handler

// ============================================================
// GET /api/v1/tenants/{id}/effective — v2.7.0 tests
// ============================================================
//
// Coverage:
//   * Success with flat conf.d (no hierarchy): defaults + tenant → merged
//   * Success with L0→L1 hierarchy (nested _defaults.yaml)
//   * 404 when tenant isn't found anywhere in the tree
//   * 400 when tenant ID fails sanitize (../, path sep, empty)
//   * Hash stability: two calls produce identical merged_hash
//   * DefaultsChain ordering: root first, leaf last
//
// Byte-for-byte parity with describe_tenant.py is covered at a lower layer by
// the exporter's config_golden_parity_test.go — we rely on the shared
// pkg/config/hierarchy.go implementation to stay in lockstep.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// writeFile is a small helper that mkdirs + writes with test-failure cleanup.
func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func TestGetTenantEffective_Success_Flat(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: \"80\"\n  mysql_cpu: \"90\"\n")
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_connections: \"70\"\n")

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/effective", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}

	var ec cfg.EffectiveConfig
	if err := json.Unmarshal(w.Body.Bytes(), &ec); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if ec.TenantID != "db-a" {
		t.Errorf("TenantID = %q, want db-a", ec.TenantID)
	}
	if ec.SourceFile != "db-a.yaml" {
		t.Errorf("SourceFile = %q, want db-a.yaml", ec.SourceFile)
	}
	if len(ec.SourceHash) != 16 {
		t.Errorf("SourceHash length = %d, want 16", len(ec.SourceHash))
	}
	if len(ec.MergedHash) != 16 {
		t.Errorf("MergedHash length = %d, want 16", len(ec.MergedHash))
	}
	if len(ec.DefaultsChain) != 1 || ec.DefaultsChain[0] != "_defaults.yaml" {
		t.Errorf("DefaultsChain = %v, want [_defaults.yaml]", ec.DefaultsChain)
	}

	// Merged config: tenant override wins for mysql_connections, default for mysql_cpu.
	if got := ec.EffectiveConfig["mysql_connections"]; got != "70" {
		t.Errorf("mysql_connections = %v, want \"70\"", got)
	}
	if got := ec.EffectiveConfig["mysql_cpu"]; got != "90" {
		t.Errorf("mysql_cpu = %v, want \"90\"", got)
	}
}

func TestGetTenantEffective_Success_Hierarchy(t *testing.T) {
	dir := t.TempDir()
	// L0 root defaults
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: \"80\"\n  mysql_cpu: \"90\"\n  mysql_slow_queries: \"100\"\n")
	// L1 team-level defaults
	writeFile(t, filepath.Join(dir, "team-a", "_defaults.yaml"),
		"defaults:\n  mysql_cpu: \"85\"\n") // L1 overrides L0 for mysql_cpu
	// Tenant file at L1
	writeFile(t, filepath.Join(dir, "team-a", "tenant-a.yaml"),
		"tenants:\n  tenant-a:\n    mysql_connections: \"70\"\n") // tenant overrides mysql_connections

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/tenant-a/effective", "id", "tenant-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}

	var ec cfg.EffectiveConfig
	if err := json.Unmarshal(w.Body.Bytes(), &ec); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	// DefaultsChain ordering: L0 (root) first, L1 (team-a) last.
	if len(ec.DefaultsChain) != 2 {
		t.Fatalf("DefaultsChain length = %d, want 2; got %v", len(ec.DefaultsChain), ec.DefaultsChain)
	}
	if ec.DefaultsChain[0] != "_defaults.yaml" {
		t.Errorf("DefaultsChain[0] = %q, want _defaults.yaml", ec.DefaultsChain[0])
	}
	if ec.DefaultsChain[1] != "team-a/_defaults.yaml" {
		t.Errorf("DefaultsChain[1] = %q, want team-a/_defaults.yaml", ec.DefaultsChain[1])
	}
	if ec.SourceFile != "team-a/tenant-a.yaml" {
		t.Errorf("SourceFile = %q, want team-a/tenant-a.yaml", ec.SourceFile)
	}

	// Merge resolution: tenant > L1 > L0
	if got := ec.EffectiveConfig["mysql_connections"]; got != "70" {
		t.Errorf("mysql_connections = %v (%T), want \"70\" (tenant override)", got, got)
	}
	if got := ec.EffectiveConfig["mysql_cpu"]; got != "85" {
		t.Errorf("mysql_cpu = %v, want \"85\" (L1 override of L0)", got)
	}
	if got := ec.EffectiveConfig["mysql_slow_queries"]; got != "100" {
		t.Errorf("mysql_slow_queries = %v, want \"100\" (L0 inherited)", got)
	}
}

func TestGetTenantEffective_NotFound(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: \"80\"\n")
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_connections: \"70\"\n")

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/nonexistent/effective", "id", "nonexistent", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404, body: %s", w.Code, w.Body.String())
	}

	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["error"] == "" {
		t.Error("expected non-empty error body")
	}
}

func TestGetTenantEffective_InvalidID(t *testing.T) {
	dir := t.TempDir()

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/../etc/effective", "id", "../etc", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

func TestGetTenantEffective_EmptyID(t *testing.T) {
	dir := t.TempDir()

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants//effective", "id", "", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

func TestGetTenantEffective_HashStable(t *testing.T) {
	// Two calls on an unchanged tree must produce identical hashes — otherwise
	// Alertmanager would see spurious reload events on every scrape.
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: \"80\"\n")
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_connections: \"70\"\n")

	h := GetTenantEffective(dir)

	callOnce := func() cfg.EffectiveConfig {
		req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/effective", "id", "db-a", nil)
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200", w.Code)
		}
		var ec cfg.EffectiveConfig
		if err := json.Unmarshal(w.Body.Bytes(), &ec); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return ec
	}

	first := callOnce()
	second := callOnce()

	if first.MergedHash != second.MergedHash {
		t.Errorf("merged_hash unstable: first=%s second=%s", first.MergedHash, second.MergedHash)
	}
	if first.SourceHash != second.SourceHash {
		t.Errorf("source_hash unstable: first=%s second=%s", first.SourceHash, second.SourceHash)
	}
}

func TestGetTenantEffective_HashChangesOnContentEdit(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: \"80\"\n")
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_connections: \"70\"\n")

	h := GetTenantEffective(dir)

	callOnce := func() cfg.EffectiveConfig {
		req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/effective", "id", "db-a", nil)
		w := httptest.NewRecorder()
		h(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
		}
		var ec cfg.EffectiveConfig
		if err := json.Unmarshal(w.Body.Bytes(), &ec); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return ec
	}

	before := callOnce()

	// Edit tenant file — merged_hash must change.
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_connections: \"99\"\n")

	after := callOnce()

	if before.MergedHash == after.MergedHash {
		t.Errorf("merged_hash unchanged after content edit: %s", before.MergedHash)
	}
	if before.SourceHash == after.SourceHash {
		t.Errorf("source_hash unchanged after content edit: %s", before.SourceHash)
	}
}

// TestGetTenantEffective_NullDeletesInheritedKey verifies that YAML `null` in
// a tenant override deletes the inherited default — matching
// describe_tenant.py trap #6.
func TestGetTenantEffective_NullDeletesInheritedKey(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: \"80\"\n  mysql_cpu: \"90\"\n")
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_connections: ~\n") // YAML null

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/effective", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}

	var ec cfg.EffectiveConfig
	if err := json.Unmarshal(w.Body.Bytes(), &ec); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if _, present := ec.EffectiveConfig["mysql_connections"]; present {
		t.Errorf("mysql_connections should be deleted by YAML null; got %v", ec.EffectiveConfig["mysql_connections"])
	}
	if got := ec.EffectiveConfig["mysql_cpu"]; got != "90" {
		t.Errorf("mysql_cpu = %v, want \"90\" (unaffected)", got)
	}
}

// TestGetTenantEffective_MetadataSkipped verifies that _metadata in defaults
// is NEVER inherited — describe_tenant.py trap #4.
func TestGetTenantEffective_MetadataSkipped(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  _metadata:\n    owner: platform\n  mysql_cpu: \"90\"\n")
	writeFile(t, filepath.Join(dir, "db-a.yaml"),
		"tenants:\n  db-a:\n    mysql_cpu: \"85\"\n")

	h := GetTenantEffective(dir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a/effective", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}

	var ec cfg.EffectiveConfig
	if err := json.Unmarshal(w.Body.Bytes(), &ec); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if _, present := ec.EffectiveConfig["_metadata"]; present {
		t.Errorf("_metadata should not be inherited; got %v", ec.EffectiveConfig["_metadata"])
	}
	if got := ec.EffectiveConfig["mysql_cpu"]; got != "85" {
		t.Errorf("mysql_cpu = %v, want \"85\"", got)
	}
}
