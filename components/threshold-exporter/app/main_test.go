package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// newTestManager creates a ConfigManager preloaded with the given config.
func newTestManager(cfg *ThresholdConfig) *ConfigManager {
	m := &ConfigManager{}
	m.config = cfg
	m.loaded = cfg != nil
	return m
}

// ============================================================
// HTTP Handler Tests — healthHandler, readyHandler, configViewHandler
// ============================================================

func TestHealthHandler(t *testing.T) {
	req := httptest.NewRequest("GET", "/health", nil)
	rec := httptest.NewRecorder()
	healthHandler(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", rec.Code)
	}
	if body := rec.Body.String(); !strings.Contains(body, "ok") {
		t.Errorf("expected body containing 'ok', got %q", body)
	}
}

func TestConfigViewHandler_RegexLabels(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"oracle_tablespace": 90,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_tablespace{tablespace=~"SYS.*"}`: SV("95"),
			},
		},
	}

	manager := newTestManager(cfg)
	handler := configViewHandler(manager)
	req := httptest.NewRequest("GET", "/api/v1/config", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()

	// Should contain regex label display with =~ notation
	if !strings.Contains(body, `tablespace=~"SYS.*"`) {
		t.Errorf("expected regex label display with =~ notation, got:\n%s", body)
	}

	// Should contain resolved threshold section
	if !strings.Contains(body, "Resolved thresholds:") {
		t.Errorf("expected 'Resolved thresholds:' section, got:\n%s", body)
	}
}

func TestConfigViewHandler_ScheduledOverrideCount(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SVScheduled("70",
					TimeWindowOverride{Window: "01:00-09:00", Value: "1000"},
					TimeWindowOverride{Window: "22:00-06:00", Value: "disable"},
				),
			},
		},
	}

	manager := newTestManager(cfg)
	handler := configViewHandler(manager)
	req := httptest.NewRequest("GET", "/api/v1/config", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()

	// Should display override count
	if !strings.Contains(body, "(+ 2 time overrides)") {
		t.Errorf("expected '(+ 2 time overrides)', got:\n%s", body)
	}
}

func TestConfigViewHandler_AtTimeOverride(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SVScheduled("70",
					TimeWindowOverride{Window: "01:00-09:00", Value: "1000"},
				),
			},
		},
	}

	manager := newTestManager(cfg)
	handler := configViewHandler(manager)

	// Request with ?at= inside the window (03:00 UTC)
	req := httptest.NewRequest("GET", "/api/v1/config?at=2026-01-15T03:00:00Z", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	body := rec.Body.String()
	if !strings.Contains(body, "(overridden)") {
		t.Errorf("expected '(overridden)' marker when ?at= is provided, got:\n%s", body)
	}
	// During window, value should be 1000
	if !strings.Contains(body, "value=1000") {
		t.Errorf("expected resolved value=1000 during window, got:\n%s", body)
	}

	// Request with ?at= outside the window (12:00 UTC)
	req2 := httptest.NewRequest("GET", "/api/v1/config?at=2026-01-15T12:00:00Z", nil)
	rec2 := httptest.NewRecorder()
	handler.ServeHTTP(rec2, req2)

	body2 := rec2.Body.String()
	// Outside window, value should be 70
	if !strings.Contains(body2, "value=70") {
		t.Errorf("expected resolved value=70 outside window, got:\n%s", body2)
	}
}

func TestReadyHandler_NotLoaded(t *testing.T) {
	manager := newTestManager(nil)

	handler := readyHandler(manager)
	req := httptest.NewRequest("GET", "/ready", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503, got %d", rec.Code)
	}
}

func TestReadyHandler_Loaded(t *testing.T) {
	manager := newTestManager(&ThresholdConfig{})

	handler := readyHandler(manager)
	req := httptest.NewRequest("GET", "/ready", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", rec.Code)
	}
}

// ============================================================
// B3: Collector Prometheus Metric Integration Test
// ============================================================

func TestCollector_RegexLabelOutput(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_tablespace{tablespace=~"SYS.*"}`: SV("95"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// Verify the _re suffix label is present in output
	expected := `
		# HELP user_threshold User-defined alerting threshold (config-driven, three-state: custom/default/disable)
		# TYPE user_threshold gauge
		user_threshold{component="oracle",metric="tablespace",severity="warning",tablespace_re="SYS.*",tenant="db-a"} 95
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "user_threshold"); err != nil {
		t.Errorf("collector output mismatch: %v", err)
	}
}

func TestCollector_MixedExactAndRegexLabels(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_ts{env='prod', tablespace=~"TEMP.*"}`: SV("200"),
			},
		},
	}

	manager := newTestManager(cfg)
	collector := NewThresholdCollector(manager)

	// Should have both exact label (env) and regex label (tablespace_re)
	count := testutil.CollectAndCount(collector, "user_threshold")
	if count != 1 {
		t.Errorf("expected 1 metric, got %d", count)
	}

	// Verify specific label values via full comparison
	expected := `
		# HELP user_threshold User-defined alerting threshold (config-driven, three-state: custom/default/disable)
		# TYPE user_threshold gauge
		user_threshold{component="oracle",env="prod",metric="ts",severity="warning",tablespace_re="TEMP.*",tenant="db-a"} 200
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "user_threshold"); err != nil {
		t.Errorf("collector output mismatch: %v", err)
	}
}

func TestCollector_StateFilter(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {},
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

	expected := `
		# HELP user_state_filter State-based monitoring filter flag (1=enabled, absent=disabled). Scenario C: state/string matching.
		# TYPE user_state_filter gauge
		user_state_filter{filter="container_crashloop",severity="critical",tenant="db-a"} 1
	`
	if err := testutil.CollectAndCompare(collector, strings.NewReader(expected), "user_state_filter"); err != nil {
		t.Errorf("state filter output mismatch: %v", err)
	}
}
