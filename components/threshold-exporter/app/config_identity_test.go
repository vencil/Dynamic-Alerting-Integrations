package main

// config_identity_test.go — GET /api/v1/config/identity (#2069).
//
// The identity is only worth anything if EVERY install path writes it: a
// path that commits a new config but leaves config_hash or parse_failed at
// the previous install's value makes patch-config accept the wrong bytes or
// judge a parse failure that is no longer (or not yet) there. So each path
// the exporter installs through gets the same five-step script (identityScript),
// and each step asserts on the log header that the path under test is the
// one that actually ran — a script that silently took another path would
// otherwise pass while covering nothing.
//
// The expected config_hash is computed here from the bytes the test wrote
// (compositeOf), not by the walker, so a walker that hashed the wrong bytes
// or the wrong file set goes red. tests/shared/config_identity_golden.json
// pins the same rule against patch-config's Python copy.

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

// compositeOf is the directory-mode config_hash of a tree holding exactly
// `files` (root-relative slash key → content): SHA-256 over the per-file
// SHA-256 hex digests in sorted key order. Every key must be one the walker
// keeps; the tests below only write such keys.
func compositeOf(files map[string]string) string {
	keys := make([]string, 0, len(files))
	for k := range files {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		fmt.Fprintf(h, "%x", sha256.Sum256([]byte(files[k])))
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}

// identityTree is a conf.d directory plus the content map compositeOf needs.
type identityTree struct {
	t     *testing.T
	dir   string
	files map[string]string
}

func newIdentityTree(t *testing.T) *identityTree {
	return &identityTree{t: t, dir: t.TempDir(), files: map[string]string{}}
}

func (tr *identityTree) put(key, content string) {
	tr.t.Helper()
	p := filepath.Join(tr.dir, filepath.FromSlash(key))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		tr.t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		tr.t.Fatal(err)
	}
	tr.files[key] = content
}

func (tr *identityTree) del(key string) {
	tr.t.Helper()
	if err := os.Remove(filepath.Join(tr.dir, filepath.FromSlash(key))); err != nil {
		tr.t.Fatal(err)
	}
	delete(tr.files, key)
}

// identityManager is a synchronous (debounce 0) manager on `path` with its
// own metrics and a captured log (test-map.md: metrics / logger go through
// the seams).
func identityManager(t *testing.T, path string) (*ConfigManager, *bytes.Buffer) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	var buf bytes.Buffer
	m := NewConfigManagerWithDebounce(path, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&buf, "", 0))
	t.Cleanup(m.Close)
	return m, &buf
}

func tenantDoc(tenant, value string) string {
	return fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: %q\n", tenant, value)
}

const brokenYAML = "tenants: [this is not\n"

// wantIdentity asserts the manager's identity: loaded, directory mode, the
// hash of the tree's current bytes and exactly `failed` as parse_failed.
func wantIdentity(t *testing.T, m *ConfigManager, tr *identityTree, failed ...string) ConfigIdentity {
	t.Helper()
	id := m.Identity()
	if !id.Loaded || id.Mode != "directory" {
		t.Fatalf("identity: loaded=%v mode=%q, want loaded directory", id.Loaded, id.Mode)
	}
	if want := compositeOf(tr.files); id.ConfigHash != want {
		t.Errorf("config_hash = %s, want %s (the composite of the bytes on disk)", id.ConfigHash, want)
	}
	if failed == nil {
		failed = []string{}
	}
	if !reflect.DeepEqual(id.ParseFailed, failed) {
		t.Errorf("parse_failed = %q, want %q", id.ParseFailed, failed)
	}
	return id
}

// ranPath asserts that the reload just run logged `header` (which path
// committed), then clears the log.
func ranPath(t *testing.T, buf *bytes.Buffer, header string) {
	t.Helper()
	if !strings.Contains(buf.String(), header) {
		t.Fatalf("the reload did not take the path under test: want a %q line, log:\n%s", header, buf.String())
	}
	buf.Reset()
}

// identityScript is the five steps every directory install path must pass:
// a valid write moves config_hash (and last_reload) with an empty
// parse_failed; a tenant file that does not parse is named in parse_failed
// while config_hash covers its new bytes; fixing it clears the set; a
// duplicate tenant (the whole tree rejected) changes nothing.
//
// dirOf maps a bare file name to its key (a subdirectory for the nested
// path); reload runs the path under test; header is the log line that path
// writes on a commit.
func identityScript(t *testing.T, tr *identityTree, m *ConfigManager, buf *bytes.Buffer,
	key func(string) string, reload func() error, header string) {
	t.Helper()
	before := wantIdentity(t, m, tr)
	buf.Reset()

	tr.put(key("t-a.yaml"), tenantDoc("t-a", "71"))
	if err := reload(); err != nil {
		t.Fatalf("valid write: %v", err)
	}
	ranPath(t, buf, header)
	valid := wantIdentity(t, m, tr)
	if valid.ConfigHash == before.ConfigHash || !valid.LastReload.After(before.LastReload) {
		t.Fatalf("a valid write did not move the identity: %+v -> %+v", before, valid)
	}

	tr.put(key("t-b.yaml"), brokenYAML)
	if err := reload(); err != nil {
		t.Fatalf("broken write: %v", err)
	}
	ranPath(t, buf, header)
	wantIdentity(t, m, tr, key("t-b.yaml"))

	tr.put(key("t-b.yaml"), tenantDoc("t-b", "61"))
	if err := reload(); err != nil {
		t.Fatalf("fix: %v", err)
	}
	ranPath(t, buf, header)
	fixed := wantIdentity(t, m, tr)

	tr.put(key("t-dup.yaml"), tenantDoc("t-a", "99"))
	if err := reload(); err == nil {
		t.Fatalf("a duplicate tenant was accepted")
	}
	if got := m.Identity(); !reflect.DeepEqual(got, fixed) {
		t.Errorf("a rejected tree changed the identity: %+v -> %+v", fixed, got)
	}
}

func bare(name string) string { return name }

// Load → fullDirLoad → fullDirLoadFrom → commitFlatFrom.
func TestConfigIdentity_FullLoad(t *testing.T) {
	t.Parallel()
	tr := newIdentityTree(t)
	tr.put("t-a.yaml", tenantDoc("t-a", "70"))
	tr.put("t-b.yaml", tenantDoc("t-b", "60"))
	m, buf := identityManager(t, tr.dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	identityScript(t, tr, m, buf, bare, m.Load, "Config loaded (directory)")
}

// A cold load that meets a broken file installs without it and says so.
func TestConfigIdentity_ColdLoadWithABrokenFile(t *testing.T) {
	t.Parallel()
	tr := newIdentityTree(t)
	tr.put("t-a.yaml", tenantDoc("t-a", "70"))
	tr.put("t-b.yaml", brokenYAML)
	m, _ := identityManager(t, tr.dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	wantIdentity(t, m, tr, "t-b.yaml")
}

// The flat-mode watch path: diffAndReload → incrementalLoadFrom, tenant-only
// changes (the patch branch).
func TestConfigIdentity_IncrementalPatchBranch(t *testing.T) {
	t.Parallel()
	tr := newIdentityTree(t)
	tr.put("t-a.yaml", tenantDoc("t-a", "70"))
	tr.put("t-b.yaml", tenantDoc("t-b", "60"))
	m, buf := identityManager(t, tr.dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	reload := func() error { _, _, err := m.diffAndReload(); return err }
	identityScript(t, tr, m, buf, bare, reload, "Config reloaded (incremental")
}

// A change below the root takes incrementalLoadFrom's redirect into
// fullDirLoadFrom (anyNestedKey); the keys are root-relative paths.
func TestConfigIdentity_IncrementalNestedRedirect(t *testing.T) {
	t.Parallel()
	tr := newIdentityTree(t)
	nested := func(name string) string { return "team/" + name }
	tr.put(nested("t-a.yaml"), tenantDoc("t-a", "70"))
	tr.put(nested("t-b.yaml"), tenantDoc("t-b", "60"))
	m, buf := identityManager(t, tr.dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	reload := func() error { _, _, err := m.diffAndReload(); return err }
	identityScript(t, tr, m, buf, nested, reload, "Config loaded (directory)")
}

// The hierarchical watch path: a tree with a defaults carrier reloads
// through diffAndReload → installNewHierarchyState → commitFlatFrom.
func TestConfigIdentity_Hierarchical(t *testing.T) {
	t.Parallel()
	tr := newIdentityTree(t)
	tr.put("_defaults.yaml", "defaults:\n  mysql_connections: 80\n")
	tr.put("t-a.yaml", tenantDoc("t-a", "70"))
	tr.put("t-b.yaml", tenantDoc("t-b", "60"))
	m, buf := identityManager(t, tr.dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	reload := func() error { _, _, err := m.diffAndReload(); return err }
	identityScript(t, tr, m, buf, bare, reload, "Config loaded (directory)")
	if !m.hierarchy.enabled {
		t.Fatal("the tree never went hierarchical; the path under test did not run")
	}

	// A broken defaults carrier is committed without its block and named.
	tr.del("t-dup.yaml")
	tr.put("_defaults.yaml", "defaults: [this is not\n")
	if err := reload(); err != nil {
		t.Fatal(err)
	}
	ranPath(t, buf, "Config loaded (directory)")
	wantIdentity(t, m, tr, "_defaults.yaml")
}

// A `_` file the incremental path rejects is named on the commit that
// rejected it (full-rebuild branch), STILL named on a later tenant-only
// commit that does not re-read it (patch branch: the carry), and cleared
// when fixed.
func TestConfigIdentity_IncrementalUnderscoreFileCarry(t *testing.T) {
	t.Parallel()
	tr := newIdentityTree(t)
	tr.put("_profiles.yaml", "profiles:\n  gold:\n    mysql_connections: \"95\"\n")
	tr.put("t-a.yaml", tenantDoc("t-a", "70"))
	m, buf := identityManager(t, tr.dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	reload := func() error { _, _, err := m.diffAndReload(); return err }
	buf.Reset()

	tr.put("_profiles.yaml", "profiles: [this is not\n")
	if err := reload(); err != nil {
		t.Fatal(err)
	}
	ranPath(t, buf, "Config reloaded (incremental, 1 changed")
	wantIdentity(t, m, tr, "_profiles.yaml")

	tr.put("t-a.yaml", tenantDoc("t-a", "71"))
	if err := reload(); err != nil {
		t.Fatal(err)
	}
	ranPath(t, buf, "Config reloaded (incremental, 1 changed")
	wantIdentity(t, m, tr, "_profiles.yaml")

	tr.put("_profiles.yaml", "profiles:\n  gold:\n    mysql_connections: \"96\"\n")
	if err := reload(); err != nil {
		t.Fatal(err)
	}
	ranPath(t, buf, "Config reloaded (incremental, 1 changed")
	wantIdentity(t, m, tr)
}

// Single-file mode: config_hash is the SHA-256 of the file's bytes; a file
// that does not parse is never installed, so the identity stays put.
func TestConfigIdentity_SingleFile(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	write := func(s string) {
		if err := os.WriteFile(path, []byte(s), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(tenantDoc("t-a", "70"))
	m, _ := identityManager(t, path)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	first := m.Identity()
	if first.Mode != "single-file" || !first.Loaded || len(first.ParseFailed) != 0 ||
		first.ConfigHash != fmt.Sprintf("%x", sha256.Sum256([]byte(tenantDoc("t-a", "70")))) {
		t.Fatalf("single-file identity: %+v", first)
	}

	write(brokenYAML)
	if err := m.Load(); err == nil {
		t.Fatal("a broken single file was installed")
	}
	if got := m.Identity(); !reflect.DeepEqual(got, first) {
		t.Errorf("a rejected single file changed the identity: %+v -> %+v", first, got)
	}

	write(tenantDoc("t-a", "71"))
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	if got := m.Identity(); got.ConfigHash != fmt.Sprintf("%x", sha256.Sum256([]byte(tenantDoc("t-a", "71")))) {
		t.Errorf("config_hash did not follow the file: %+v", got)
	}
}

// configIdentityGolden is tests/shared/config_identity_golden.json.
type configIdentityGolden struct {
	Data                   map[string]string `json:"data"`
	ExpectedScannedKeys    []string          `json:"expected_scanned_keys"`
	ExpectedDirectoryHash  string            `json:"expected_directory_hash"`
	SingleFileKey          string            `json:"single_file_key"`
	ExpectedSingleFileHash string            `json:"expected_single_file_hash"`
}

// The cross-language golden: the exporter, loading a ConfigMap-shaped tree
// (hidden file, non-YAML files, `.yml`, upper-case extension, `_` files,
// UTF-8 bytes), serves the config_hash patch-config computes for the same
// data (tests/ops/test_patch_config_identity.py asserts the Python half).
func TestConfigIdentity_CrossLanguageGolden(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile(filepath.Join(findRepoRoot(t), "tests", "shared", "config_identity_golden.json")) // #nosec G304 -- repo test fixture
	if err != nil {
		t.Fatal(err)
	}
	var g configIdentityGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	if len(g.Data) < 8 || len(g.ExpectedScannedKeys) == 0 || len(g.ExpectedScannedKeys) >= len(g.Data) {
		t.Fatalf("golden gutted or no longer excludes anything: %d keys, %d scanned", len(g.Data), len(g.ExpectedScannedKeys))
	}
	dir := t.TempDir()
	for k, v := range g.Data {
		if err := os.WriteFile(filepath.Join(dir, k), []byte(v), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	scan, err := scanDirTree(dir, nil, nil, log.New(&bytes.Buffer{}, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(scan.Keys, g.ExpectedScannedKeys) {
		t.Errorf("scanned keys = %q, want %q", scan.Keys, g.ExpectedScannedKeys)
	}
	m, _ := identityManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	if got := m.Identity(); got.ConfigHash != g.ExpectedDirectoryHash || len(got.ParseFailed) != 0 {
		t.Errorf("directory identity = %+v, want config_hash %s and nothing failed", got, g.ExpectedDirectoryHash)
	}

	single, _ := identityManager(t, filepath.Join(dir, g.SingleFileKey))
	if err := single.Load(); err != nil {
		t.Fatal(err)
	}
	if got := single.Identity().ConfigHash; got != g.ExpectedSingleFileHash {
		t.Errorf("single-file config_hash = %s, want %s", got, g.ExpectedSingleFileHash)
	}
}

// The wire shape, through the real routing table.
func TestConfigIdentityHandler_Shape(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 9, 27, 1, 2, 3, 456789000, time.FixedZone("x", 8*3600))
	m := &ConfigManager{isDir: true, loaded: true, lastReload: at, lastHash: "abc",
		flat: flatScanState{parseFailed: []string{"a/_defaults.yaml", "t-b.yaml"}}}
	mux := buildMux(m, NewThresholdCollector(m))

	get := func(method string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(method, "/api/v1/config/identity", nil))
		return rec
	}
	rec := get(http.MethodGet)
	if rec.Code != http.StatusOK || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("GET: %d %q", rec.Code, rec.Header().Get("Content-Type"))
	}
	var doc map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"schema": float64(1), "loaded": true, "mode": "directory",
		"last_reload": "2026-09-26T17:02:03.456789Z", "config_hash": "abc",
		"parse_failed": []any{"a/_defaults.yaml", "t-b.yaml"},
	}
	if !reflect.DeepEqual(doc, want) {
		t.Errorf("identity document = %v, want %v", doc, want)
	}

	if rec := get(http.MethodPost); rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("POST: %d, want 405", rec.Code)
	}

	// Nothing installed yet: still 200, loaded false, and parse_failed is a
	// list (a consumer iterating it must not meet null).
	empty := &ConfigManager{isDir: true}
	rec = httptest.NewRecorder()
	configIdentityHandler(empty)(rec, httptest.NewRequest(http.MethodGet, "/api/v1/config/identity", nil))
	body := strings.TrimSpace(rec.Body.String())
	if rec.Code != http.StatusOK || body !=
		`{"schema":1,"loaded":false,"mode":"directory","last_reload":"","config_hash":"","parse_failed":[]}` {
		t.Errorf("not loaded: %d %s", rec.Code, body)
	}

	// Single-file mode never reports a parse-failed set.
	file := &ConfigManager{loaded: true, lastReload: at, lastHash: "h",
		flat: flatScanState{parseFailed: []string{"stale.yaml"}}}
	if id := file.Identity(); id.Mode != "single-file" || len(id.ParseFailed) != 0 || id.ParseFailed == nil {
		t.Errorf("single-file identity: %+v", id)
	}
}
