package main

// config_platform_tenant_overlay_test.go — Go's half of
// tests/shared/platform_tenant_overlay_matrix.json (#1982), plus the
// exporter-only properties the table cannot carry: the WARN lines and the
// reload paths.
//
// The semantics under test: a ROOT platform file's `tenants:` block is the
// platform's per-tenant DEFAULT. The tenant's own file wins key by key,
// whatever either file is called; a key the tenant file does not write keeps
// the platform value; a platform file cannot create a tenant; a NESTED
// platform file's `tenants:` block is read by no plane and says so.
//
// Seams: logger via SetLogger (never log.SetOutput); metrics via
// freshMetrics + SetMetrics on every manager. t.TempDir() trees.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// overlayMetricKey is the one threshold the matrix pins. The collector
// serves it as component `mysql`, metric `connections`.
const overlayMetricKey = "mysql_connections"

// orphanAnchor / nestedAnchor identify the two producers' lines. Each is
// specific to its emitter (log_assertions_test.go: anchor on the producer,
// not on the message class).
const (
	orphanAnchor = "no tenant file declares tenant"
	nestedAnchor = "tenants: block in nested platform file"
)

// ⛔ DisallowUnknownFields and a declared `_comment`, as the sibling
// defaults_symlink_parity matrix does (#1967): a misspelt key would make a
// row test nothing while staying green.
type overlayMatrix struct {
	Comment []string `json:"_comment"`
	Trees   []struct {
		Name string `json:"name"`
		// MetricKey is the threshold the metric / tenant_api columns read
		// (#2368 alias rows); empty = overlayMetricKey.
		MetricKey string            `json:"metric_key"`
		Files     map[string]string `json:"files"`
		Expect    map[string]struct {
			Metric        *float64       `json:"metric"`
			TenantAPI     *float64       `json:"tenant_api"` // #2208: the tenant-api merge core (GET / write gate)
			Dedup         *string        `json:"dedup"`      // Python routing plane
			GroupWait     *string        `json:"group_wait"` // Python routing plane
			ExporterDedup *string        `json:"exporter_dedup"`
			SilentMode    *string        `json:"silent_mode"`
			Walker        *overlayWalker `json:"walker"` // #2019: /effective + describe_tenant
		} `json:"expect"`
	} `json:"trees"`
}

// overlayWalker is the matrix's walker-plane column (#2019): what
// config.ResolveEffective (and describe_tenant.py) serve for the tenant.
type overlayWalker struct {
	EffectiveConfig map[string]any                 `json:"effective_config"`
	PlatformOverlay []config.PlatformOverlaySource `json:"platform_overlay"`
	ProfileOverlay  []config.ProfileOverlaySource  `json:"profile_overlay"` // #2117
	// DescribeTenantEffectiveConfig is the Python half's column where
	// describe_tenant.py still differs from /effective (#2115); Go only
	// checks that it does differ, so a stale entry cannot linger.
	DescribeTenantEffectiveConfig map[string]any `json:"describe_tenant_effective_config"`
}

func loadOverlayMatrix(t *testing.T) overlayMatrix {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..",
		"tests", "shared", "platform_tenant_overlay_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m overlayMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	return m
}

func writeOverlayTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", rel, err)
		}
		writeTestYAML(t, p, content)
	}
}

// newOverlayManager loads a tree through the real entry with injected
// metrics and a captured logger.
func newOverlayManager(t *testing.T, dir string) (*ConfigManager, *bytes.Buffer) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	var buf bytes.Buffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&buf, "", 0))
	t.Cleanup(m.Close)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return m, &buf
}

// servedValue is what /metrics carries for (tenant, overlayMetricKey): the
// resolved warning-severity row the collector emits, or ok=false when the
// tenant has none.
func servedValue(m *ConfigManager, tenant string) (float64, bool) {
	return servedValueFor(m, tenant, overlayMetricKey)
}

// servedValueFor is servedValue for any threshold key (#2368 alias rows).
func servedValueFor(m *ConfigManager, tenant, key string) (float64, bool) {
	cfg := m.GetConfig()
	if cfg == nil {
		return 0, false
	}
	for _, r := range cfg.Resolve() {
		if r.Tenant == tenant && r.Component+"_"+r.Metric == key && r.Severity == "warning" {
			return r.Value, true
		}
	}
	return 0, false
}

// metricKeyOf is the threshold a matrix tree's metric columns read.
func metricKeyOf(key string) string {
	if key == "" {
		return overlayMetricKey
	}
	return key
}

// exporterDedup is the exporter's resolved _severity_dedup for a tenant:
// "enable" when ResolveSeverityDedup lists it, "disable" when the tenant is
// configured but unlisted, nil when the tenant is absent.
func exporterDedup(m *ConfigManager, tenant string) *string {
	cfg := m.GetConfig()
	if _, ok := cfg.Tenants[tenant]; !ok {
		return nil
	}
	mode := "disable"
	for _, d := range cfg.ResolveSeverityDedup() {
		if d.Tenant == tenant {
			mode = d.Mode
		}
	}
	return &mode
}

// exporterSilentMode is the sorted, comma-joined target severities
// ResolveSilentModes yields for a tenant ("" = none), nil when absent.
func exporterSilentMode(m *ConfigManager, tenant string) *string {
	cfg := m.GetConfig()
	if _, ok := cfg.Tenants[tenant]; !ok {
		return nil
	}
	var targets []string
	for _, s := range cfg.ResolveSilentModes() {
		if s.Tenant == tenant {
			targets = append(targets, s.TargetSeverity)
		}
	}
	sort.Strings(targets)
	joined := strings.Join(targets, ",")
	return &joined
}

func sameOptString(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func showOpt(s *string) string {
	if s == nil {
		return "<absent>"
	}
	return strconv.Quote(*s)
}

func assertServed(t *testing.T, m *ConfigManager, where, tenant string, want *float64) {
	t.Helper()
	assertServedKey(t, m, where, tenant, overlayMetricKey, want)
}

// assertServedKey is assertServed for any threshold key. For a key that is
// the target of a #1231 alias it also checks the legacy twin series
// (`metric="cpu"` for mysql_threads_running): the transition-window
// dual-emit must carry the same value, or a stale twin passes unseen.
func assertServedKey(t *testing.T, m *ConfigManager, where, tenant, key string, want *float64) {
	t.Helper()
	keys := []string{key}
	if legacy, ok := config.LegacySpellingFor(key); ok {
		keys = append(keys, legacy)
	}
	for _, k := range keys {
		got, ok := servedValueFor(m, tenant, k)
		switch {
		case want == nil && ok:
			t.Errorf("%s: %s is served with %s=%v, want the tenant ABSENT from /metrics", where, tenant, k, got)
		case want != nil && !ok:
			t.Errorf("%s: %s serves no %s, want %v", where, tenant, k, *want)
		case want != nil && got != *want:
			t.Errorf("%s: %s serves %s=%v, want %v", where, tenant, k, got, *want)
		}
	}
}

func TestPlatformTenantOverlayMatrix(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)
	for _, tree := range m.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, tree.Files)
			mgr, _ := newOverlayManager(t, dir)
			for tenant, want := range tree.Expect {
				key := metricKeyOf(tree.MetricKey)
				assertServedKey(t, mgr, tree.Name, tenant, key, want.Metric)
				// ⛔ Not the metric alone: every per-tenant value the
				// exporter resolves goes through the same merge, and a
				// table that checks one threshold would stay green if a
				// reserved key took a different path.
				if got := exporterDedup(mgr, tenant); !sameOptString(got, want.ExporterDedup) {
					t.Errorf("%s: %s exporter _severity_dedup = %s, want %s", tree.Name, tenant, showOpt(got), showOpt(want.ExporterDedup))
				}
				if got := exporterSilentMode(mgr, tenant); !sameOptString(got, want.SilentMode) {
					t.Errorf("%s: %s exporter _silent_mode = %s, want %s", tree.Name, tenant, showOpt(got), showOpt(want.SilentMode))
				}
				// #2019: the walker plane must serve the same tenant values.
				// The column is checked against ResolveEffective here and
				// against the /metrics columns of the same row, so a row
				// cannot pin a walker answer that disagrees with /metrics.
				assertWalkerRow(t, dir, tree.Name, tenant, want.Walker)
				assertWalkerAgreesWithMetrics(t, tree.Name, tenant, key, want.Walker, want.Metric, want.SilentMode, want.ExporterDedup)
				assertWalkerCriticalRowsServed(t, mgr, tree.Name, tenant, want.Walker)
				// #2208: the tenant-api merge core, and why it may differ.
				assertTenantAPIRow(t, dir, tree.Name, tenant, key, want.TenantAPI)
				assertTenantAPIAgreesWithMetrics(t, tree.Name, tenant, want.TenantAPI, want.Metric, want.Walker)
			}
			// No tenant the table does not name is served — otherwise an
			// orphan row passes by checking only the tenants it lists.
			for tenant := range mgr.GetConfig().Tenants {
				if _, listed := tree.Expect[tenant]; !listed {
					t.Errorf("%s: /metrics carries %s, which the table does not name", tree.Name, tenant)
				}
			}
		})
	}
}

// tenantAPIServed is what the tenant-api merge core — the one behind GET
// /api/v1/tenants/{id}, POST …/validate and the write gate — resolves for
// (tenant, key) when handed the tenant's declaring file.
// reached=false when tenant-api never gets that far: the tenant is declared
// by no file, or by one below the root (tenant-api serves top-level files).
//
// ⚠️ Not the whole GET: which top-level file GET opens is confd's
// filename-addressed lookup in the tenant-api module, which this module
// cannot import. The column pins the merge given the declaring file's bytes.
func tenantAPIServed(t *testing.T, dir, tenant, key string) (value float64, served, reached bool) {
	t.Helper()
	scan, err := config.ScanDirTree(dir, nil, nil, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("ScanDirTree: %v", err)
	}
	loc, lerr := scan.Locate(tenant)
	if lerr != nil || filepath.Dir(loc) != scan.AbsRoot {
		return 0, false, false
	}
	body, err := os.ReadFile(loc)
	if err != nil {
		t.Fatalf("read %s: %v", loc, err)
	}
	merged := config.MergeTenantWithRootDefaults(dir, tenant, body)
	for _, r := range merged.ResolveAt(time.Now()) {
		if r.Tenant == tenant && r.Component+"_"+r.Metric == key && r.Severity == "warning" && len(r.CustomLabels) == 0 {
			return r.Value, true, true
		}
	}
	return 0, false, true
}

// assertTenantAPIRow checks the tenant-api merge core against the
// tenant_api column: null ⇔ not reached.
func assertTenantAPIRow(t *testing.T, dir, tree, tenant, key string, want *float64) {
	t.Helper()
	got, served, reached := tenantAPIServed(t, dir, tenant, key)
	switch {
	case want == nil && reached:
		t.Errorf("%s: tenant-api merge core reaches %s (%s=%v, served=%v), want not reached", tree, tenant, key, got, served)
	case want != nil && !served:
		t.Errorf("%s: tenant-api merge core serves no %s for %s (reached=%v), want %v", tree, key, tenant, reached, *want)
	case want != nil && got != *want:
		t.Errorf("%s: tenant-api merge core serves %s=%v for %s, want %v", tree, key, got, tenant, *want)
	}
}

// assertTenantAPIAgreesWithMetrics is the column's oracle: the tenant-api
// core serves what /metrics serves, with exactly one sanctioned gap: not
// reached (null) while /metrics serves the tenant, only for a tenant
// declared below the root (the walker column still resolves it). A profile
// /metrics expands, the core expands too (#1385) — no exception for it.
func assertTenantAPIAgreesWithMetrics(t *testing.T, tree, tenant string, api, metric *float64, w *overlayWalker) {
	t.Helper()
	switch {
	case api == nil && metric == nil:
	case api == nil:
		// Nested declaring file: /metrics serves it, tenant-api cannot reach it.
		if w == nil {
			t.Errorf("%s: %s tenant_api null while /metrics serves %v and the walker finds nothing", tree, tenant, *metric)
		}
	case metric == nil:
		t.Errorf("%s: %s tenant_api %v for a tenant /metrics does not serve", tree, tenant, *api)
	case *api != *metric:
		t.Errorf("%s: %s tenant_api %v differs from /metrics %v", tree, tenant, *api, *metric)
	}
}

// assertWalkerRow checks config.ResolveEffective against the walker column:
// not-found ⇔ null; otherwise the effective config (compared as JSON, the
// wire form /effective serves), platform_overlay, and that the JSON omits
// `platform_overlay` exactly when the column says null.
func assertWalkerRow(t *testing.T, dir, tree, tenant string, want *overlayWalker) {
	t.Helper()
	ec, err := config.ResolveEffective(dir, tenant)
	if want == nil {
		if !errors.Is(err, config.ErrTenantNotFound) {
			t.Errorf("%s: /effective for %s = (%v, %v), want not found", tree, tenant, ec, err)
		}
		return
	}
	if err != nil {
		t.Errorf("%s: /effective for %s: %v", tree, tenant, err)
		return
	}
	gotCfg, _ := json.Marshal(ec.EffectiveConfig)
	wantCfg, _ := json.Marshal(want.EffectiveConfig)
	if !bytes.Equal(gotCfg, wantCfg) {
		t.Errorf("%s: /effective %s effective_config = %s, want %s", tree, tenant, gotCfg, wantCfg)
	}
	if want.DescribeTenantEffectiveConfig != nil {
		if py, _ := json.Marshal(want.DescribeTenantEffectiveConfig); bytes.Equal(py, wantCfg) {
			t.Errorf("%s: %s describe_tenant_effective_config equals effective_config; drop it", tree, tenant)
		}
	}
	if !reflect.DeepEqual(ec.PlatformOverlay, want.PlatformOverlay) {
		t.Errorf("%s: /effective %s platform_overlay = %+v, want %+v", tree, tenant, ec.PlatformOverlay, want.PlatformOverlay)
	}
	if !reflect.DeepEqual(ec.ProfileOverlay, want.ProfileOverlay) {
		t.Errorf("%s: /effective %s profile_overlay = %+v, want %+v", tree, tenant, ec.ProfileOverlay, want.ProfileOverlay)
	}
	body, _ := json.Marshal(ec)
	if has := bytes.Contains(body, []byte(`"platform_overlay"`)); has != (want.PlatformOverlay != nil) {
		t.Errorf("%s: /effective %s JSON carries platform_overlay=%v, want %v: %s", tree, tenant, has, want.PlatformOverlay != nil, body)
	}
	if has := bytes.Contains(body, []byte(`"profile_overlay"`)); has != (want.ProfileOverlay != nil) {
		t.Errorf("%s: /effective %s JSON carries profile_overlay=%v, want %v: %s", tree, tenant, has, want.ProfileOverlay != nil, body)
	}
}

// assertWalkerCriticalRowsServed is the #2117 `_critical` cross-check: every
// `<base>_critical` key the walker column carries is the value of the
// critical-severity row /metrics serves for <base>. A profile supplying
// `pg_connections_critical` produces a real critical row (ApplyProfiles'
// `_critical` exemption), so the walker plane must report it.
func assertWalkerCriticalRowsServed(t *testing.T, m *ConfigManager, tree, tenant string, w *overlayWalker) {
	t.Helper()
	if w == nil {
		return
	}
	served := map[string]float64{}
	for _, r := range m.GetConfig().Resolve() {
		if r.Tenant == tenant && r.Severity == "critical" {
			served[r.Component+"_"+r.Metric] = r.Value
		}
	}
	for k, v := range w.EffectiveConfig {
		base, ok := strings.CutSuffix(k, "_critical")
		if !ok || strings.HasPrefix(k, "_") {
			continue
		}
		want, err := strconv.ParseFloat(fmt.Sprint(v), 64)
		if err != nil {
			t.Errorf("%s: %s walker %s=%v is not a number", tree, tenant, k, v)
			continue
		}
		if got, ok := served[base]; !ok || got != want {
			t.Errorf("%s: %s walker %s=%v, /metrics critical row for %s = (%v, served=%v)", tree, tenant, k, v, base, got, ok)
		}
	}
}

// assertWalkerAgreesWithMetrics is the row's own consistency: the walker
// column's mysql_connections / _silent_mode / _severity_dedup are the values
// the /metrics columns say the exporter resolves (absent `_severity_dedup` =
// "enable", absent `_silent_mode` = "").
//
// For an alias target key (#2368 rows) the walker's effective config carries
// the threshold under exactly ONE spelling — the winning layer's (#2115):
// both spellings present is a failure on its own, whatever their values.
// A schedule value is compared by its `default:` and only when it has no
// window (the metric column is read at the wall clock, so a window would
// make the row time-dependent).
func assertWalkerAgreesWithMetrics(t *testing.T, tree, tenant, key string, w *overlayWalker, metric *float64, silent, dedup *string) {
	t.Helper()
	if (w == nil) != (metric == nil) {
		t.Errorf("%s: %s walker present=%v but metric present=%v", tree, tenant, w != nil, metric != nil)
		return
	}
	if w == nil {
		return
	}
	wk := key
	if legacy, ok := config.LegacySpellingFor(key); ok {
		_, canonSet := w.EffectiveConfig[key]
		_, legacySet := w.EffectiveConfig[legacy]
		if canonSet && legacySet {
			t.Errorf("%s: %s walker carries both %s and %s; /metrics serves one threshold", tree, tenant, key, legacy)
		}
		if legacySet {
			wk = legacy
		}
	}
	wv := w.EffectiveConfig[wk]
	if sched, isMap := wv.(map[string]any); isMap {
		if ov, has := sched["overrides"]; has && ov != nil {
			t.Errorf("%s: %s walker %s=%v carries windows; the row's metric column cannot check them", tree, tenant, wk, wv)
		}
		wv = sched["default"]
	}
	v, err := strconv.ParseFloat(fmt.Sprint(wv), 64)
	if err != nil || v != *metric {
		t.Errorf("%s: %s walker %s=%v, metric column %v", tree, tenant, wk, w.EffectiveConfig[wk], *metric)
	}
	sm, _ := w.EffectiveConfig["_silent_mode"].(string)
	if silent == nil || sm != *silent {
		t.Errorf("%s: %s walker _silent_mode=%q, silent_mode column %s", tree, tenant, sm, showOpt(silent))
	}
	dd, ok := w.EffectiveConfig["_severity_dedup"].(string)
	if !ok {
		dd = "enable"
	}
	if dedup == nil || dd != *dedup {
		t.Errorf("%s: %s walker _severity_dedup=%q, exporter_dedup column %s", tree, tenant, dd, showOpt(dedup))
	}
}

func overlayTree(m overlayMatrix, t *testing.T, name string) map[string]string {
	t.Helper()
	for _, tree := range m.Trees {
		if tree.Name == name {
			return tree.Files
		}
	}
	t.Fatalf("matrix has no tree %q", name)
	return nil
}

func TestPlatformFileOrphanTenantIsNamed(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)

	t.Run("orphan", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, overlayTree(m, t, "c1-declared-only-in-profiles-file"))
		_, buf := newOverlayManager(t, dir)
		assertLogLineWith(t, buf.String(), orphanAnchor, "WARN:", "_profiles.yaml", `"tx"`, "already exists")
	})
	// Control: the same platform entry with a tenant file declaring the
	// tenant (named so it sorts BEFORE the platform file) says nothing.
	t.Run("control-tenant-exists", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, overlayTree(m, t, "c2-c5-platform-only-keys-tenant-file-TX.yaml"))
		_, buf := newOverlayManager(t, dir)
		if lines := logLinesWith(buf.String(), orphanAnchor); len(lines) != 0 {
			t.Errorf("tenant exists, yet: %q", lines)
		}
	})
}

func TestNestedPlatformFileTenantsBlockIsNamed(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)

	t.Run("nested-tenants-block", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, overlayTree(m, t, "c4-nested-platform-file-tenants-block"))
		_, buf := newOverlayManager(t, dir)
		assertLogLineWith(t, buf.String(), nestedAnchor,
			"WARN:", filepath.Join("sub", "_defaults.yaml"), `"tx", "ty"`, "not read by any plane")
	})
	// Control: a nested platform file with no `tenants:` block is silent.
	t.Run("control-no-tenants-block", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		})
		_, buf := newOverlayManager(t, dir)
		if lines := logLinesWith(buf.String(), nestedAnchor); len(lines) != 0 {
			t.Errorf("no tenants: block, yet: %q", lines)
		}
	})
}

// TestPlatformTenantOverlaySurvivesReload drives tenant-file and
// platform-file edits through the watch path's reload and checks precedence
// and existence after each one. On the carrier layout that reload is the
// hierarchical one (commitFlatFrom on the reload's own scan) on every step.
//
// ⚠️ CARRIER LAYOUT ONLY, AND THAT IS A LOSS (#1577). This test reads what
// /metrics serves, and a tree with no `_defaults` carrier serves no threshold
// row at all (measured: Resolve() returns 0 rows for a tenant file setting
// mysql_connections; 1 row once a carrier exists) — every key is "not in
// defaults" — so on the only layout where the watch path takes
// incrementalLoadFrom there is nothing for these assertions to read. It used
// to reach patchTenants through the removed `IncrementalLoad()` on the
// carrier tree, a combination production never runs. On the flat layout
// the fast path's precedence is pinned by the flat tree of
// TestTheFastPathAlwaysLandsWhereAFullLoadWould (every tenant override
// against a full load), and which tenants exist by the flat leg of
// TestPlatformTenantOverlayReloadMatchesFreshLoad.
//
// The tenant file is `TX.yaml` on purpose: it sorts BEFORE `_defaults.yaml`,
// which is the spelling that used to lose to the platform value.
func TestPlatformTenantOverlaySurvivesReload(t *testing.T) {
	t.Parallel()
	f := func(v float64) *float64 { return &v }
	for _, tree := range overlayReloadTrees() {
		if tree.flat {
			continue // no threshold rows to read — see above
		}
		overlaySurvivesReload(t, tree, f)
	}
}

// overlayReloadTree is one platform-file layout for the reload tests below:
// `platform` names tx at 60, `ty` serves 80 as the control. The flat layout
// has no `_defaults` carrier, so the watch path reloads it through
// incrementalLoadFrom; the carrier layout is reloaded hierarchically (#1577).
type overlayReloadTree struct {
	name, platformFile, platform, ty string
	flat                             bool
}

func overlayReloadTrees() []overlayReloadTree {
	return []overlayReloadTree{
		{"flat", "_profiles.yaml",
			"tenants:\n  tx:\n    mysql_connections: \"60\"\n",
			"tenants:\n  ty:\n    mysql_connections: \"80\"\n", true},
		{"carrier", "_defaults.yaml",
			"defaults:\n  mysql_connections: 80\n" +
				"tenants:\n  tx:\n    mysql_connections: \"60\"\n",
			"tenants:\n  ty: {}\n", false},
	}
}

func overlaySurvivesReload(t *testing.T, tree overlayReloadTree, f func(float64) *float64) {
	t.Helper()
	platform := tree.platform
	steps := []struct {
		name   string
		mutate func(t *testing.T, dir string)
		want   *float64
		orphan bool // the reload names tx as an orphan of _defaults.yaml
	}{
		{"tenant-file-edit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "TX.yaml"),
				"tenants:\n  tx:\n    mysql_connections: \"70\"\n    redis_memory_used_bytes: \"1\"\n")
		}, f(70), false},
		{"platform-file-edit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, tree.platformFile), platform+"    redis_memory_used_bytes: \"2\"\n")
		}, f(70), false},
		{"tenant-file-removed", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "TX.yaml")); err != nil {
				t.Fatal(err)
			}
		}, nil, true},
		{"tenant-file-back-silent", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "0tx.yaml"), "tenants:\n  tx: {}\n")
		}, f(60), false},
	}
	rname := tree.name
	dir := t.TempDir()
	writeOverlayTree(t, dir, map[string]string{
		tree.platformFile: platform,
		"TX.yaml":         "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
		"ty.yaml":         tree.ty,
	})
	m, buf := newOverlayManager(t, dir)
	if tree.flat {
		requireFlatWatchPath(t, m)
	}
	assertServed(t, m, rname+"/load", "tx", f(70))
	for i, st := range steps {
		buf.Reset()
		st.mutate(t, dir)
		touchTreeAt(t, dir, time.Now().Add(time.Duration(i+3)*time.Second))
		if err := watchReload(m); err != nil {
			t.Fatalf("%s/%s: %v", rname, st.name, err)
		}
		assertServed(t, m, rname+"/"+st.name, "tx", st.want)
		assertServed(t, m, rname+"/"+st.name+" (control)", "ty", f(80))
		got := len(logLinesWith(buf.String(), orphanAnchor)) > 0
		if got != st.orphan {
			t.Errorf("%s/%s: orphan WARN emitted=%v, want %v; log:\n%s", rname, st.name, got, st.orphan, buf.String())
		}
	}
}

// servedSnapshot is every (tenant → overlayMetricKey value) /metrics
// carries, plus the tenants among `probe` the committed hierarchy
// (tenantSources) knows.
type servedSnapshot struct {
	values    map[string]float64
	committed map[string]bool
}

func snapshotServed(m *ConfigManager, probe []string) servedSnapshot {
	snap := servedSnapshot{values: map[string]float64{}, committed: map[string]bool{}}
	for tenant := range m.GetConfig().Tenants {
		v, ok := servedValue(m, tenant)
		if !ok {
			v = -1 // configured, no row for the key
		}
		snap.values[tenant] = v
	}
	for _, tid := range probe {
		_, found := committedTenantState(m, tid)
		snap.committed[tid] = found
	}
	return snap
}

// TestPlatformTenantOverlayReloadMatchesFreshLoad is the reload-vs-Load
// differential for the two ways a tenant leaves a tenant file that STAYS on
// disk — `tenants: {}` and re-declaring it as another tenant — while a
// platform file still names it. After every step the reloaded manager must
// serve exactly what a fresh Load of the same tree serves, and its committed
// hierarchy must know the same tenants (an orphan: not known).
//
// Each half of the fix is pinned by it, measured by removing it: the
// `tenantExists` condition in patchTenants' changed-file loop (without it
// the platform entry kept the deleted tenant alive after an incremental
// reload) and the `_`-file skip in refreshTenantSources (without it the
// committed tenantSources kept the orphan, attributed to `_defaults.yaml`).
// Both halves live in incrementalLoadFrom, which the watch path reaches only
// on the flat layout of overlayReloadTrees (#1577); the carrier layout pins
// the hierarchical reload against the same oracle.
func TestPlatformTenantOverlayReloadMatchesFreshLoad(t *testing.T) {
	t.Parallel()
	probe := []string{"tx", "tw", "ty"}
	steps := []struct {
		name, body string
	}{
		{"tenant-emptied", "tenants: {}\n"},
		{"tenant-renamed", "tenants:\n  tw:\n    mysql_connections: \"75\"\n"},
		{"tenant-back", "tenants:\n  tx:\n    mysql_connections: \"70\"\n"},
		{"tenant-emptied-again", "tenants: {}\n"},
	}
	for _, tree := range overlayReloadTrees() {
		tree := tree
		rname := tree.name
		t.Run(rname, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, map[string]string{
				tree.platformFile: tree.platform,
				"TX.yaml":         "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
				"ty.yaml":         tree.ty,
			})
			m, _ := newOverlayManager(t, dir)
			if tree.flat {
				requireFlatWatchPath(t, m)
			}
			for i, st := range steps {
				writeTestYAML(t, filepath.Join(dir, "TX.yaml"), st.body)
				touchTreeAt(t, dir, time.Now().Add(time.Duration(i+3)*time.Second))
				if err := watchReload(m); err != nil {
					t.Fatalf("%s/%s: %v", rname, st.name, err)
				}
				fresh, _ := newOverlayManager(t, dir)
				got, want := snapshotServed(m, probe), snapshotServed(fresh, probe)
				if !reflect.DeepEqual(got, want) {
					t.Errorf("%s/%s: reloaded %+v, fresh Load %+v", rname, st.name, got, want)
				}
				// The differential alone would pass if BOTH sides kept the
				// orphan; state the expected answer too.
				if strings.HasPrefix(st.name, "tenant-emptied") {
					if _, served := got.values["tx"]; served || got.committed["tx"] {
						t.Errorf("%s/%s: orphan tx still served=%v committed=%v", rname, st.name, served, got.committed["tx"])
					}
				}
			}
		})
	}
}

// TestPlatformOrphanWarnFollowsWhatIsServed: a tenant file that turns
// unparseable keeps its tenant on the last good values on the incremental
// patch path (the fail-safe, #1980) — platform-supplied keys included. The
// orphan WARN must not then claim the platform entry is ignored while the
// same commit serves it.
//
// ⚠️ TWO TREES, TWO ANSWERS, ONE RULE (#1577). The watch path reaches the
// patch path only for a tree with no `_defaults` carrier: the flat leg, whose
// per-tenant platform values live in `_profiles.yaml`, keeps tx. With a
// carrier every reload is the hierarchical path's full flat rebuild, which
// drops tx as a restart does — so that leg asserts the other half of the
// rule: tx is not served, and the WARN does say its platform entry is
// ignored. (This leg used to drive the removed `IncrementalLoad()` and assert
// the fail-safe on the carrier tree too: a state the watch path never
// produces.)
func TestPlatformOrphanWarnFollowsWhatIsServed(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		platform   string
		body       string
		wantServed bool
	}{
		{"carrier (hierarchical path drops it)", "_defaults.yaml",
			"defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"60\"\n", false},
		{"flat-profiles (patch path keeps it)", "_profiles.yaml",
			"tenants:\n  tx:\n    mysql_connections: \"60\"\n", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, map[string]string{
				tc.platform: tc.body,
				"tx.yaml":   "tenants:\n  tx:\n    redis_x: \"1\"\n",
				"ty.yaml":   "tenants:\n  ty: {}\n",
			})
			m, buf := newOverlayManager(t, dir)
			if got := m.GetConfig().Tenants["tx"]["mysql_connections"].Default; got != "60" {
				t.Fatalf("load: tx mysql_connections=%q, want the platform's 60", got)
			}
			if tc.wantServed {
				requireFlatWatchPath(t, m)
			}
			buf.Reset()
			writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx: [1\n")
			touchTreeAt(t, dir, time.Now().Add(3*time.Second))
			if err := watchReload(m); err != nil {
				t.Fatal(err)
			}
			ov, served := m.GetConfig().Tenants["tx"]
			lines := logLinesWith(buf.String(), orphanAnchor)
			if !tc.wantServed {
				if served {
					t.Fatalf("tx served (%v) after its only file stopped parsing on the hierarchical path — "+
						"this leg's premise is gone", ov)
				}
				if len(lines) != 1 {
					t.Errorf("tx is not served, so its platform entry is ignored — want exactly one orphan WARN, got %d: %q",
						len(lines), lines)
				}
				return
			}
			if !served || ov["mysql_connections"].Default != "60" || ov["redis_x"].Default != "1" {
				t.Fatalf("fail-safe not in effect (served=%v, %v) — this test's premise is gone", served, ov)
			}
			if len(lines) != 0 {
				t.Errorf("tx is still served with the platform value, yet: %q", lines)
			}
		})
	}
}

// TestPlatformOrphanStaysOutAfterPlatformFileEdit: an orphan platform entry
// must stay out of /metrics when the PLATFORM file itself is edited, which
// on the flat tree sends incrementalLoadFrom down its full-rebuild branch
// (mergePartialConfigs(newConfigs, exists)). Measured: passing nil there
// put tx on /metrics while the WARN still said the entry was ignored, and
// no other test noticed. Two trees: the defaults carrier, and a flat one
// whose per-tenant block is in `_profiles.yaml`.
func TestPlatformOrphanStaysOutAfterPlatformFileEdit(t *testing.T) {
	t.Parallel()
	cases := []struct{ name, platform, body string }{
		{"carrier", "_defaults.yaml",
			"defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"60\"\n"},
		{"flat-profiles", "_profiles.yaml",
			"tenants:\n  tx:\n    mysql_connections: \"60\"\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, map[string]string{
				tc.platform: tc.body,
				"ty.yaml":   "tenants:\n  ty: {}\n",
			})
			m, buf := newOverlayManager(t, dir)
			if _, served := m.GetConfig().Tenants["tx"]; served {
				t.Fatal("load: orphan tx served — premise broken")
			}
			buf.Reset()
			writeTestYAML(t, filepath.Join(dir, tc.platform), tc.body+"    redis_x: \"1\"\n")
			touchTreeAt(t, dir, time.Now().Add(3*time.Second))
			if err := watchReload(m); err != nil {
				t.Fatal(err)
			}
			if ov, served := m.GetConfig().Tenants["tx"]; served {
				t.Errorf("after editing %s: orphan tx served with %v", tc.platform, ov)
			}
			if _, ok := m.GetConfig().Tenants["ty"]; !ok {
				t.Error("control: ty vanished")
			}
			if lines := logLinesWith(buf.String(), orphanAnchor); len(lines) != 1 {
				t.Errorf("want exactly one orphan WARN, got %d: %q", len(lines), lines)
			}
		})
	}
}
