package main

// ============================================================
// Simulate primitive tests (v2.8.0 Phase .c C-7a + C-7b)
// ============================================================
//
// Three layers:
//   1. config.SimulateEffective unit tests — basic merge / chain ordering /
//      error paths (no HTTP).
//   2. simulateHandler HTTP tests — request shape, error codes,
//      content-type contract.
//   3. **Parity gate** — TestSimulate_VsResolve_ParityHash writes the
//      same bytes to disk under a tmp dir, lets ConfigManager.Resolve
//      compute its merged_hash, then calls config.SimulateEffective with the
//      identical bytes and asserts byte-identical SourceHash, MergedHash,
//      DefaultsChain length, and effective Config map. This is the
//      contract Phase .c relies on: a /simulate response is the same
//      thing /effective will produce after the caller commits.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// --- Layer 1: config.SimulateEffective unit tests ---------------------------

func TestSimulate_BasicHierarchy(t *testing.T) {
	defaults := []byte("defaults:\n  mysql_connections: 80\n  cpu_threshold: 75\n")
	tenantBytes := []byte("tenants:\n  tenant-a:\n    mysql_connections: \"90\"\n")

	resp, err := config.SimulateEffective(config.SimulateRequest{
		TenantID:          "tenant-a",
		TenantYAML:        tenantBytes,
		DefaultsChainYAML: [][]byte{defaults},
	})
	if err != nil {
		t.Fatalf("config.SimulateEffective: %v", err)
	}
	if resp.TenantID != "tenant-a" {
		t.Errorf("TenantID = %q, want tenant-a", resp.TenantID)
	}
	if len(resp.SourceHash) != 16 {
		t.Errorf("SourceHash len = %d, want 16", len(resp.SourceHash))
	}
	if len(resp.MergedHash) != 16 {
		t.Errorf("MergedHash len = %d, want 16", len(resp.MergedHash))
	}
	if len(resp.DefaultsChain) != 1 {
		t.Errorf("DefaultsChain len = %d, want 1", len(resp.DefaultsChain))
	}
	if got := resp.Config["mysql_connections"]; got != "90" {
		t.Errorf("mysql_connections = %v (%T), want \"90\" (tenant override)", got, got)
	}
	if got := resp.Config["cpu_threshold"]; got != 75 {
		// cpu_threshold inherited from defaults; integer literal stays int.
		t.Errorf("cpu_threshold = %v (%T), want 75 (inherited)", got, got)
	}
}

func TestSimulate_NoDefaults(t *testing.T) {
	tenantBytes := []byte("tenants:\n  tenant-flat:\n    cpu_threshold: 80\n")
	resp, err := config.SimulateEffective(config.SimulateRequest{
		TenantID:   "tenant-flat",
		TenantYAML: tenantBytes,
	})
	if err != nil {
		t.Fatalf("config.SimulateEffective: %v", err)
	}
	if len(resp.DefaultsChain) != 0 {
		t.Errorf("DefaultsChain should be empty, got %v", resp.DefaultsChain)
	}
	if got := resp.Config["cpu_threshold"]; got != 80 {
		t.Errorf("cpu_threshold = %v, want 80", got)
	}
}

func TestSimulate_DeepChainOrdering(t *testing.T) {
	// L0 sets X=1, L1 sets X=2, tenant overrides X=3 → expect X=3.
	// Also: L0 sets Y=10, L1 inherits but overrides Z=20 → tenant
	// inherits Y=10 from L0 and Z=20 from L1.
	l0 := []byte("defaults:\n  X: 1\n  Y: 10\n")
	l1 := []byte("defaults:\n  X: 2\n  Z: 20\n")
	tenantBytes := []byte("tenants:\n  t1:\n    X: 3\n")

	resp, err := config.SimulateEffective(config.SimulateRequest{
		TenantID:          "t1",
		TenantYAML:        tenantBytes,
		DefaultsChainYAML: [][]byte{l0, l1},
	})
	if err != nil {
		t.Fatalf("config.SimulateEffective: %v", err)
	}
	if got := resp.Config["X"]; got != 3 {
		t.Errorf("X = %v, want 3 (tenant)", got)
	}
	if got := resp.Config["Y"]; got != 10 {
		t.Errorf("Y = %v, want 10 (L0)", got)
	}
	if got := resp.Config["Z"]; got != 20 {
		t.Errorf("Z = %v, want 20 (L1)", got)
	}
	if len(resp.DefaultsChain) != 2 {
		t.Errorf("DefaultsChain len = %d, want 2", len(resp.DefaultsChain))
	}
}

func TestSimulate_TenantNotFound(t *testing.T) {
	tenantBytes := []byte("tenants:\n  someone-else:\n    foo: 1\n")
	_, err := config.SimulateEffective(config.SimulateRequest{
		TenantID:   "missing",
		TenantYAML: tenantBytes,
	})
	if err != config.ErrSimulateTenantNotFound {
		t.Fatalf("err = %v, want config.ErrSimulateTenantNotFound", err)
	}
}

func TestSimulate_EmptyTenantID(t *testing.T) {
	_, err := config.SimulateEffective(config.SimulateRequest{
		TenantYAML: []byte("tenants:\n  t1:\n    x: 1\n"),
	})
	if err == nil || !strings.Contains(err.Error(), "tenant_id is required") {
		t.Fatalf("err = %v, want tenant_id required", err)
	}
}

func TestSimulate_EmptyTenantYAML(t *testing.T) {
	_, err := config.SimulateEffective(config.SimulateRequest{
		TenantID: "t1",
	})
	if err == nil || !strings.Contains(err.Error(), "tenant_yaml is required") {
		t.Fatalf("err = %v, want tenant_yaml required", err)
	}
}

func TestSimulate_MalformedTenantYAML(t *testing.T) {
	_, err := config.SimulateEffective(config.SimulateRequest{
		TenantID:   "t1",
		TenantYAML: []byte("tenants:\n  t1:\n    [unclosed-bracket\n"),
	})
	if err == nil {
		t.Fatalf("err = nil, want parse failure")
	}
}

// --- Layer 2: simulateHandler HTTP tests -----------------------------

func TestSimulateHandler_Happy(t *testing.T) {
	body := config.SimulateRequest{
		TenantID:          "tenant-a",
		TenantYAML:        []byte("tenants:\n  tenant-a:\n    cpu_threshold: 70\n"),
		DefaultsChainYAML: [][]byte{[]byte("defaults:\n  cpu_threshold: 50\n  mem: 80\n")},
	}
	bodyBytes, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate",
		bytes.NewReader(bodyBytes))
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q", ct)
	}

	var resp config.SimulateResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode resp: %v", err)
	}
	if resp.TenantID != "tenant-a" {
		t.Errorf("TenantID = %q", resp.TenantID)
	}
	if got := resp.Config["cpu_threshold"]; got != float64(70) && got != 70 {
		// JSON unmarshal turns numbers into float64; tolerate both.
		t.Errorf("cpu_threshold = %v, want 70 (tenant override)", got)
	}
}

func TestSimulateHandler_MethodNotAllowed(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/tenants/simulate", nil)
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET status = %d, want 405", rec.Code)
	}
}

func TestSimulateHandler_BadJSON(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate",
		strings.NewReader("not-json"))
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestSimulateHandler_TenantNotFound(t *testing.T) {
	body := config.SimulateRequest{
		TenantID:   "ghost",
		TenantYAML: []byte("tenants:\n  alive:\n    x: 1\n"),
	}
	bodyBytes, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate",
		bytes.NewReader(bodyBytes))
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404", rec.Code)
	}
}

func TestSimulateHandler_BodyTooLarge(t *testing.T) {
	// Build an oversized payload: 2 MiB of tenant YAML padding.
	pad := strings.Repeat("a", 2<<20)
	body := config.SimulateRequest{
		TenantID:   "t1",
		TenantYAML: []byte("tenants:\n  t1:\n    note: \"" + pad + "\"\n"),
	}
	bodyBytes, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate",
		bytes.NewReader(bodyBytes))
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status = %d, want 413", rec.Code)
	}
}

func TestSimulateHandler_EmptyBody(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate", nil)
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for empty body (not 413)", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "empty request body") {
		t.Errorf("body = %q, want 'empty request body' marker", rec.Body.String())
	}
}

func TestSimulateHandler_MalformedDefaults(t *testing.T) {
	// Defaults bytes that don't parse as YAML must surface as 400 from
	// the handler — the contract is "preview will fail the same way a
	// commit would fail".
	body := config.SimulateRequest{
		TenantID:          "t1",
		TenantYAML:        []byte("tenants:\n  t1:\n    x: 1\n"),
		DefaultsChainYAML: [][]byte{[]byte("defaults:\n  [unclosed\n")},
	}
	bodyBytes, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate",
		bytes.NewReader(bodyBytes))
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for malformed defaults", rec.Code)
	}
}

func TestSimulateHandler_UnknownField(t *testing.T) {
	// DisallowUnknownFields surfaces typos in the request shape.
	req := httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate",
		strings.NewReader(`{"tenant_id":"t1","tenant_yaml":"YQ==","oops":1}`))
	rec := httptest.NewRecorder()
	simulateHandler()(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for unknown field", rec.Code)
	}
}

// --- Layer 3: Parity gate -------------------------------------------

// TestSimulate_VsResolve_ParityHash is the contract Phase .c relies on:
// for a given (tenant.yaml, _defaults.yaml chain), the simulate path and
// the disk-backed Resolve path must produce byte-identical SourceHash,
// MergedHash, and effective Config. If this drifts, /simulate stops being
// a useful preview of what would happen after commit, and the C-7a/C-7b
// design is broken.
//
// Intentionally exercises a non-trivial case: 2-level chain, tenant
// override at every level, plus a `_metadata` block in the tenant file
// (which must be stripped by both paths).
func TestSimulate_VsResolve_ParityHash(t *testing.T) {
	dir := t.TempDir()

	// Layout:
	//   <dir>/_defaults.yaml             (L0 - flat block, no `defaults:` wrapper)
	//   <dir>/team-a/_defaults.yaml      (L1 - wrapped form)
	//   <dir>/team-a/tenant-a.yaml       (tenant)
	l0Bytes := []byte("mysql_connections: 80\ncpu_threshold: 75\nreceivers:\n  - email\n")
	l1Bytes := []byte("defaults:\n  mysql_connections: 90\n  custom_label: \"team-a\"\n")
	tenantBytes := []byte("tenants:\n  tenant-a:\n    cpu_threshold: 70\n    receivers:\n      - pagerduty\n    _metadata:\n      contact: \"alice\"\n")

	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), l0Bytes, 0o644); err != nil {
		t.Fatalf("write L0: %v", err)
	}
	teamDir := filepath.Join(dir, "team-a")
	if err := os.MkdirAll(teamDir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(teamDir, "_defaults.yaml"), l1Bytes, 0o644); err != nil {
		t.Fatalf("write L1: %v", err)
	}
	if err := os.WriteFile(filepath.Join(teamDir, "tenant-a.yaml"), tenantBytes, 0o644); err != nil {
		t.Fatalf("write tenant: %v", err)
	}

	// Disk path
	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	disk, ok := m.Resolve("tenant-a")
	if !ok {
		t.Fatalf("Resolve(tenant-a) not ok")
	}

	// Simulate path: same bytes, manually-ordered chain.
	sim, err := config.SimulateEffective(config.SimulateRequest{
		TenantID:          "tenant-a",
		TenantYAML:        tenantBytes,
		DefaultsChainYAML: [][]byte{l0Bytes, l1Bytes},
	})
	if err != nil {
		t.Fatalf("config.SimulateEffective: %v", err)
	}

	if disk.SourceHash != sim.SourceHash {
		t.Errorf("SourceHash drift: disk=%q sim=%q", disk.SourceHash, sim.SourceHash)
	}
	if disk.MergedHash != sim.MergedHash {
		t.Errorf("MergedHash drift: disk=%q sim=%q\ndisk Config=%v\nsim  Config=%v",
			disk.MergedHash, sim.MergedHash, disk.Config, sim.Config)
	}
	if len(disk.DefaultsChain) != len(sim.DefaultsChain) {
		t.Errorf("chain length drift: disk=%d sim=%d", len(disk.DefaultsChain), len(sim.DefaultsChain))
	}
	if !reflect.DeepEqual(disk.Config, sim.Config) {
		t.Errorf("Config drift:\ndisk=%v\nsim =%v", disk.Config, sim.Config)
	}
	// Spot-check merged values.
	if got := sim.Config["mysql_connections"]; got != 90 {
		// L1 overrides L0; tenant didn't touch this key.
		t.Errorf("mysql_connections = %v, want 90", got)
	}
	if got := sim.Config["cpu_threshold"]; got != 70 {
		t.Errorf("cpu_threshold = %v, want 70 (tenant override)", got)
	}
	if _, hasMeta := sim.Config["_metadata"]; hasMeta {
		t.Errorf("_metadata leaked into merged Config")
	}
}

// --- Source layer tests ---------------------------------------------

func TestInMemoryConfigSource_FiltersByRoot(t *testing.T) {
	files := map[string][]byte{
		"/sim/a.yaml":         []byte("tenants:\n  a:\n    x: 1\n"),
		"/sim/sub/b.yaml":     []byte("tenants:\n  b:\n    x: 2\n"),
		"/elsewhere/c.yaml":   []byte("tenants:\n  c:\n    x: 3\n"),
		"/sim/skip-me.txt":    []byte("not yaml"),
		"/sim/.hidden.yaml":   []byte("tenants:\n  hidden:\n    x: 4\n"),
		"/sim/_defaults.yaml": []byte("defaults:\n  x: 0\n"),
	}
	src := config.NewInMemoryConfigSource(files)
	out, err := src.YAMLFiles("/sim")
	if err != nil {
		t.Fatalf("YAMLFiles: %v", err)
	}
	// /elsewhere/c.yaml filtered out; non-yaml filtered; .hidden.yaml
	// is still returned by YAMLFiles (the scan layer is the one that
	// skips dot-prefixed names — same split as scanDirHierarchical
	// where filepath.WalkDir yields the file and the visitor filters).
	wantPaths := []string{"/sim/_defaults.yaml", "/sim/.hidden.yaml", "/sim/a.yaml", "/sim/sub/b.yaml"}
	if len(out) != len(wantPaths) {
		t.Errorf("got %d files, want %d: %v", len(out), len(wantPaths), keysOf(out))
	}
	for _, p := range wantPaths {
		// Cleaned path key — on POSIX this is byte-identical to the
		// input; on Windows path.Clean would convert / → \.
		if _, ok := out[filepath.Clean(p)]; !ok {
			t.Errorf("missing %q in YAMLFiles output", p)
		}
	}
}

func TestScanFromConfigSource_DuplicateTenantError(t *testing.T) {
	files := map[string][]byte{
		"/sim/team-a.yaml": []byte("tenants:\n  shared:\n    x: 1\n"),
		"/sim/team-b.yaml": []byte("tenants:\n  shared:\n    x: 2\n"),
	}
	src := config.NewInMemoryConfigSource(files)
	_, _, _, _, err := config.ScanFromConfigSource(src, "/sim")
	if err == nil || !strings.Contains(err.Error(), "duplicate tenant ID") {
		t.Fatalf("err = %v, want duplicate tenant error", err)
	}
}

func keysOf(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
