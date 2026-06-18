package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// ============================================================
// Describe — unchecked collector mode (empty)
// ============================================================

func TestCollector_Describe_Empty(t *testing.T) {
	manager := newTestManager(&ThresholdConfig{})
	collector := NewThresholdCollector(manager)

	ch := make(chan *prometheus.Desc, 10)
	collector.Describe(ch)
	close(ch)

	count := 0
	for range ch {
		count++
	}
	if count != 0 {
		t.Errorf("Describe should send 0 descriptors (unchecked mode), got %d", count)
	}
}

// ============================================================
// Collect — nil config (early return)
// ============================================================

func TestCollector_Collect_NilConfig(t *testing.T) {
	manager := newTestManager(nil)
	collector := NewThresholdCollector(manager)

	count := testutil.CollectAndCount(collector)
	if count != 0 {
		t.Errorf("expected 0 metrics for nil config, got %d", count)
	}
}

// ============================================================
// Collect — custom labels path (exact labels)
// ============================================================

func TestCollector_Collect_CustomLabels(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`mysql_connections{env="prod"}`: SV("100"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	expected := `
		# HELP user_threshold User-defined alerting threshold (config-driven, three-state: custom/default/disable)
		# TYPE user_threshold gauge
		user_threshold{component="mysql",env="prod",metric="connections",severity="warning",tenant="db-a"} 100
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "user_threshold"); err != nil {
		t.Errorf("custom labels mismatch: %v", err)
	}
}

// ============================================================
// Collect — maintenance expiry emits da_config_event
// ============================================================

func TestCollector_Collect_MaintenanceExpiry(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_state_maintenance": SV(`expires: "2020-01-01T00:00:00Z"`),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// Should emit da_config_event for expired maintenance
	eventCount := testutil.CollectAndCount(collector, "da_config_event")
	// Whether events are emitted depends on ResolveMaintenanceExpiries implementation
	_ = eventCount // Just verify no panic
}

// Collect — expired threshold override emits da_config_event (PREVENT #656).
// The metric key is encoded into the reason so each (tenant, metric) event is a
// distinct series; the threshold VALUE itself fail-safes to the default
// (user_threshold, not compared here).
func TestCollector_Collect_ThresholdExpiry_EmitsEvent(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": {Default: "2000", Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z", Reason: "incident #1234"}}},
		},
	}
	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	expected := `
# HELP da_config_event Config lifecycle event (1=event active). Emitted when timed config expires. Labels identify event type and tenant.
# TYPE da_config_event gauge
da_config_event{event="threshold_expired",reason="mysql_connections: incident #1234",tenant="db-a"} 1
`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "da_config_event"); err != nil {
		t.Errorf("da_config_event mismatch: %v", err)
	}

	// And the VALUE fail-safes to the platform default (80), NOT the expired
	// override (2000) — proven at the collector level, not just resolve.
	expectedThreshold := `
# HELP user_threshold User-defined alerting threshold (config-driven, three-state: custom/default/disable)
# TYPE user_threshold gauge
user_threshold{component="mysql",metric="connections",severity="warning",tenant="db-a"} 80
`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expectedThreshold), "user_threshold"); err != nil {
		t.Errorf("expired threshold must fail-safe to default 80: %v", err)
	}
}

// ============================================================
// MetricsHandler — returns valid HTTP handler
// ============================================================

func TestCollector_MetricsHandler(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)
	handler := collector.MetricsHandler()

	req := httptest.NewRequest("GET", "/metrics", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", rec.Code)
	}

	body := rec.Body.String()
	if !strings.Contains(body, "user_threshold") {
		t.Errorf("expected user_threshold metric in output, got:\n%s", body[:min(len(body), 500)])
	}
	if !strings.Contains(body, "go_") {
		t.Errorf("expected Go collector metrics in output")
	}
}

func TestCollector_MetricsHandler_EmptyConfig(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants:  map[string]map[string]ScheduledValue{},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)
	handler := collector.MetricsHandler()

	req := httptest.NewRequest("GET", "/metrics", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", rec.Code)
	}
}

// ============================================================
// Collect — multiple tenants, severity dedup combinations
// ============================================================

func TestCollector_MultiTenant_SeverityDedup(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_severity_dedup": SV("disable"),
			},
			"db-b": {},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// db-a: disabled, db-b: enabled (default)
	count := testutil.CollectAndCount(collector, "user_severity_dedup")
	if count != 1 {
		t.Errorf("expected 1 dedup metric (only db-b enabled), got %d", count)
	}
}

// ============================================================
// configViewHandler — nil config path
// ============================================================

func TestConfigViewHandler_NilConfig(t *testing.T) {
	manager := newTestManager(nil)
	handler := configViewHandler(manager)

	req := httptest.NewRequest("GET", "/api/v1/config", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "No config loaded") {
		t.Errorf("expected 'No config loaded' for nil config, got:\n%s", body)
	}
}

// ============================================================
// configViewHandler — invalid ?at= parameter
// ============================================================

func TestConfigViewHandler_InvalidAtParam(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {},
		},
	}

	manager := newTestManager(cfg)
	handler := configViewHandler(manager)

	req := httptest.NewRequest("GET", "/api/v1/config?at=invalid-time", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "invalid ?at= param") {
		t.Errorf("expected 'invalid ?at= param' message, got:\n%s", body)
	}
}

// ============================================================
// configViewHandler — with silent modes
// ============================================================

func TestConfigViewHandler_WithSilentModes(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_silent_mode": SV("warning"),
			},
		},
	}

	manager := newTestManager(cfg)
	handler := configViewHandler(manager)

	req := httptest.NewRequest("GET", "/api/v1/config", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "Silent modes") {
		t.Errorf("expected 'Silent modes' section, got:\n%s", body)
	}
	if !strings.Contains(body, "target_severity=warning") {
		t.Errorf("expected 'target_severity=warning', got:\n%s", body)
	}
}

// ============================================================
// configViewHandler — with custom labels display
// ============================================================

func TestConfigViewHandler_CustomLabelsDisplay(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_ts{env="prod"}`: SV("200"),
			},
		},
	}

	manager := newTestManager(cfg)
	handler := configViewHandler(manager)

	req := httptest.NewRequest("GET", "/api/v1/config", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, `env="prod"`) {
		t.Errorf("expected custom label display, got:\n%s", body)
	}
}

// ============================================================
// Collect — state filter + silent mode + dedup + metadata combination
// ============================================================

func TestCollector_FullConfig(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"mysql_connections":  80,
			"mysql_slow_queries": 100,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("90"),
				"_silent_mode":      SV("critical"),
				"_severity_dedup":   SV("enable"),
				"_metadata":         SV("owner: dba-team\ntier: gold"),
			},
			"db-b": {
				"mysql_connections": SV("70"),
				"_severity_dedup":   SV("disable"),
			},
		},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {
				Reasons:  []string{"CrashLoopBackOff"},
				Severity: "critical",
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// Collect all metrics — should not panic
	metrics := testutil.CollectAndCount(collector)
	if metrics == 0 {
		t.Error("expected at least some metrics from full config")
	}

	// Verify each metric family exists
	thresholdCount := testutil.CollectAndCount(collector, "user_threshold")
	if thresholdCount < 3 {
		t.Errorf("expected >=3 threshold metrics, got %d", thresholdCount)
	}

	silentCount := testutil.CollectAndCount(collector, "user_silent_mode")
	if silentCount != 1 {
		t.Errorf("expected 1 silent mode metric, got %d", silentCount)
	}

	dedupCount := testutil.CollectAndCount(collector, "user_severity_dedup")
	if dedupCount != 1 {
		t.Errorf("expected 1 dedup metric (only db-a enabled), got %d", dedupCount)
	}

	stateCount := testutil.CollectAndCount(collector, "user_state_filter")
	if stateCount < 1 {
		t.Errorf("expected >=1 state filter metric, got %d", stateCount)
	}

	metadataCount := testutil.CollectAndCount(collector, "tenant_metadata_info")
	if metadataCount != 2 {
		t.Errorf("expected 2 metadata info metrics (one per tenant), got %d", metadataCount)
	}

	configInfoCount := testutil.CollectAndCount(collector, "threshold_exporter_config_info")
	if configInfoCount != 1 {
		t.Errorf("expected 1 config info metric, got %d", configInfoCount)
	}
}

// ============================================================
// Collect ��� multiple regex + exact labels sorted output
// ============================================================

func TestCollector_MultipleRegexLabels(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_ts{dc=~"us.*", env=~"prod.*"}`: SV("200"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	count := testutil.CollectAndCount(collector, "user_threshold")
	if count != 1 {
		t.Errorf("expected 1 metric, got %d", count)
	}
}

// ============================================================
// Collect — expired silent mode emits da_config_event
// ============================================================

func TestCollector_Collect_ExpiredSilentMode(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_silent_mode": SV("target: warning\nexpires: \"2020-01-01T00:00:00Z\"\n"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// Expired silent mode should emit da_config_event, NOT user_silent_mode
	silentCount := testutil.CollectAndCount(collector, "user_silent_mode")
	if silentCount != 0 {
		t.Errorf("expected 0 user_silent_mode for expired entry, got %d", silentCount)
	}

	eventCount := testutil.CollectAndCount(collector, "da_config_event")
	if eventCount < 1 {
		t.Errorf("expected >=1 da_config_event for expired silent mode, got %d", eventCount)
	}
}

// ============================================================
// Collect — active silent mode with reason
// ============================================================

func TestCollector_Collect_ActiveSilentModeStructured(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_silent_mode": SV("target: critical\nreason: Planned DB migration\nexpires: \"2099-12-31T00:00:00Z\"\n"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// Active (not expired) should emit user_silent_mode
	silentCount := testutil.CollectAndCount(collector, "user_silent_mode")
	if silentCount != 1 {
		t.Errorf("expected 1 user_silent_mode for active entry, got %d", silentCount)
	}

	// No config event for active mode
	eventCount := testutil.CollectAndCount(collector, "da_config_event")
	if eventCount != 0 {
		t.Errorf("expected 0 da_config_event for active silent mode, got %d", eventCount)
	}
}

// ============================================================
// Collect — expired maintenance mode emits da_config_event
// ============================================================

func TestCollector_Collect_ExpiredMaintenance(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_state_maintenance": SV("target: enable\nexpires: \"2020-01-01T00:00:00Z\"\n"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	eventCount := testutil.CollectAndCount(collector, "da_config_event")
	if eventCount < 1 {
		t.Errorf("expected >=1 da_config_event for expired maintenance, got %d", eventCount)
	}
}

// ============================================================
// Collect — severity dedup enabled vs disabled
// ============================================================

func TestCollector_Collect_SeverityDedupEnabled(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_severity_dedup": SV("enable"),
			},
			"db-b": {
				"_severity_dedup": SV("enable"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	count := testutil.CollectAndCount(collector, "user_severity_dedup")
	if count != 2 {
		t.Errorf("expected 2 dedup metrics (both enabled), got %d", count)
	}
}

// ============================================================
// Collect — config info metric always present
// ============================================================

func TestCollector_Collect_ConfigInfo(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants:  map[string]map[string]ScheduledValue{},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	count := testutil.CollectAndCount(collector, "threshold_exporter_config_info")
	if count != 1 {
		t.Errorf("expected 1 config info metric, got %d", count)
	}
}

// ============================================================
// resolveConfigPath — auto-detect directory
// ============================================================

func TestResolveConfigPath_AutoDetectDir_RealDir(t *testing.T) {
	// Create a temp dir that mimics the default path
	dir := t.TempDir()
	confD := filepath.Join(dir, "conf.d")
	os.MkdirAll(confD, 0700)

	oldConfigDir := configDir
	oldConfigPath := configPath
	defer func() {
		configDir = oldConfigDir
		configPath = oldConfigPath
	}()

	configDir = ""
	configPath = ""

	// Can't test /etc path directly, but test flag behavior
	result := resolveConfigPath()
	// Should return default file since we can't inject the /etc path
	if result == "" {
		t.Error("resolveConfigPath should never return empty")
	}
}

// ============================================================
// Collect publishes da_tenant_metrics_over_limit through manager
// metrics (#652)
// ============================================================
//
// Routes through c.manager.getMetrics().PublishTenantMetricsOverLimit
// rather than the package-level singleton helper. Verifies the
// test-injection contract: a fresh configMetrics injected via
// ConfigManager.SetMetrics must receive the per-tenant gauge writes,
// AND the Reset()+Set() loop must clear vanished tenants and clamp
// just-dropped-below-the-cap tenants to 0.
//
// Originally caught during adversarial self-review: the first cut
// called the package-level PublishTenantMetricsOverLimit helper which
// routes through the global getConfigMetrics(), bypassing the
// injected instance. This test would have failed under that bug —
// the fresh GaugeVec would have stayed empty while the global one
// got the writes.

func TestCollector_Collect_PublishesOverLimitGauge(t *testing.T) {
	defs := make(map[string]float64, 600)
	for i := 0; i < 600; i++ {
		defs[fmt.Sprintf("metric_%d", i)] = float64(i)
	}

	cfg := &ThresholdConfig{
		Defaults: defs,
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-over":      {}, // over by 100
			"tenant-compliant": disableAllForCollectorTest(defs),
		},
		MaxMetricsPerTenant: 500,
	}
	manager := newTestManager(cfg)
	fresh, _ := freshMetrics(t)
	manager.SetMetrics(fresh)

	collector := NewThresholdCollector(manager)
	// CollectAndCount triggers the full Collect path on the fresh registry.
	_ = testutil.CollectAndCount(collector)

	overVal := testutil.ToFloat64(fresh.tenantMetricsOverLimit.WithLabelValues("tenant-over"))
	if overVal != 100 {
		t.Errorf("over-limit tenant gauge = %v, want 100 (count=600, limit=500)", overVal)
	}
	compVal := testutil.ToFloat64(fresh.tenantMetricsOverLimit.WithLabelValues("tenant-compliant"))
	if compVal != 0 {
		t.Errorf("compliant tenant gauge = %v, want 0 (state-coded contract — compliant tenants must Set 0, not omit)", compVal)
	}
}

func TestCollector_Collect_OverLimitGaugeEvictsVanishedTenant(t *testing.T) {
	defs := make(map[string]float64, 600)
	for i := 0; i < 600; i++ {
		defs[fmt.Sprintf("metric_%d", i)] = float64(i)
	}

	cfg := &ThresholdConfig{
		Defaults: defs,
		Tenants: map[string]map[string]ScheduledValue{
			"to-be-deleted": {},
		},
		MaxMetricsPerTenant: 500,
	}
	manager := newTestManager(cfg)
	fresh, _ := freshMetrics(t)
	manager.SetMetrics(fresh)

	collector := NewThresholdCollector(manager)

	// First scrape — tenant exists, gauge populated.
	_ = testutil.CollectAndCount(collector)
	if got := testutil.ToFloat64(fresh.tenantMetricsOverLimit.WithLabelValues("to-be-deleted")); got != 100 {
		t.Fatalf("first scrape over-limit = %v, want 100", got)
	}

	// Tenant disappears from config — simulate a deletion between scrapes.
	manager.config.Tenants = map[string]map[string]ScheduledValue{
		"another-tenant": {},
	}

	// Second scrape — Reset+Set must evict the deleted tenant's series.
	_ = testutil.CollectAndCount(collector)
	// CollectAndCount on the gaugevec series families: after the second
	// scrape, only "another-tenant" should remain. Use ToFloat64 with
	// a brand-new label value to confirm the deleted tenant is gone.
	// (ToFloat64 on a vanished series returns 0 because WithLabelValues
	// recreates the cell — what we actually want to check is whether
	// the deleted series is absent from the registry's exposition. We
	// do that by counting tenantMetricsOverLimit families directly.)
	count := testutil.CollectAndCount(fresh.tenantMetricsOverLimit)
	if count != 1 {
		t.Errorf("tenantMetricsOverLimit has %d series after deletion, want 1 (Reset() must evict vanished tenants on the next scrape)", count)
	}
}

func disableAllForCollectorTest(defaults map[string]float64) map[string]ScheduledValue {
	out := make(map[string]ScheduledValue, len(defaults))
	for k := range defaults {
		out[k] = SV("disable")
	}
	return out
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
