package main

// Transport-layer tests for handlers.go — healthHandler, readyHandler,
// configViewHandler. Split out of main_test.go / collector_test.go so the
// handler tests sit beside their implementation (handlers.go); the collector
// and resolveConfigPath tests own collector_test.go / main_test.go
// respectively. Pure in-memory (newTestManager + httptest), so every test
// here is t.Parallel-safe.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ============================================================
// HTTP Handler Tests — healthHandler, readyHandler, configViewHandler
// ============================================================

func TestHealthHandler(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
// configViewHandler — nil config path
// ============================================================

func TestConfigViewHandler_NilConfig(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
	t.Parallel()
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
