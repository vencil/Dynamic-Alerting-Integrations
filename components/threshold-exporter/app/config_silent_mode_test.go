package main

// Silent-mode + Maintenance-mode tests — _silent_mode (warning / critical
// / all / disable), _state_maintenance (with expires + recurring), and
// related ResolveStateFilters branches. Split out of config_test.go in
// PR-2; shared helpers live in config_test.go.

import (
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// region SilentModeResolution — silent mode states and expiry handling

// ============================================================
// Silent Mode Tests
// ============================================================

func TestResolveSilentModes(t *testing.T) {
	tests := []struct {
		name       string
		tenants    map[string]map[string]ScheduledValue
		expectedLen int
		validate   func(t *testing.T, result []ResolvedSilentMode)
	}{
		{"Default", map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": SV("70")}}, 0, nil},
		{"Warning", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("warning")}}, 1, func(t *testing.T, r []ResolvedSilentMode) {
			if r[0].Tenant != "db-a" || r[0].TargetSeverity != "warning" {
				t.Errorf("unexpected: %+v", r[0])
			}
		}},
		{"Critical", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("critical")}}, 1, func(t *testing.T, r []ResolvedSilentMode) {
			if r[0].TargetSeverity != "critical" {
				t.Errorf("expected critical, got %s", r[0].TargetSeverity)
			}
		}},
		{"All", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("all")}}, 2, func(t *testing.T, r []ResolvedSilentMode) {
			sev := map[string]bool{}
			for _, x := range r {
				sev[x.TargetSeverity] = true
			}
			if !sev["warning"] || !sev["critical"] {
				t.Errorf("expected warning+critical, got %v", sev)
			}
		}},
		{"Disable", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("disable")}}, 0, nil},
		{"InvalidFallback", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("invalid_value")}}, 0, nil},
		{"CaseInsensitive", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("WARNING")}, "db-b": {"_silent_mode": SV("All")}, "db-c": {"_silent_mode": SV(" Critical ")}}, 4, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &ThresholdConfig{Tenants: tt.tenants}
			result := cfg.ResolveSilentModes()
			if len(result) != tt.expectedLen {
				t.Fatalf("expected %d, got %d", tt.expectedLen, len(result))
			}
			if tt.validate != nil {
				tt.validate(t, result)
			}
		})
	}
}

func TestResolveAt_SkipsSilentKey(t *testing.T) {
	// _silent_mode must NOT produce a user_threshold metric
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_silent_mode":     SV("warning"),
			},
		},
	}
	resolved := cfg.Resolve()
	for _, r := range resolved {
		if r.Metric == "mode" || r.Component == "silent" {
			t.Errorf("_silent_mode leaked into thresholds: %+v", r)
		}
	}
	if len(resolved) != 1 {
		t.Errorf("expected 1 threshold (mysql_connections), got %d", len(resolved))
	}
}

func TestResolveSilentModes_MixedTenants(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_silent_mode": SV("warning")},
			"db-b": {"_silent_mode": SV("all")},
			"db-c": {"_silent_mode": SV("disable")},
			"db-d": {"mysql_connections": SV("70")}, // no silent_mode → Normal
		},
	}
	result := cfg.ResolveSilentModes()
	// db-a: 1, db-b: 2, db-c: 0, db-d: 0 = 3
	if len(result) != 3 {
		t.Errorf("expected 3, got %d", len(result))
	}

	tenantSev := map[string][]string{}
	for _, r := range result {
		tenantSev[r.Tenant] = append(tenantSev[r.Tenant], r.TargetSeverity)
	}
	if len(tenantSev["db-a"]) != 1 || tenantSev["db-a"][0] != "warning" {
		t.Errorf("db-a: expected [warning], got %v", tenantSev["db-a"])
	}
	sort.Strings(tenantSev["db-b"])
	if len(tenantSev["db-b"]) != 2 {
		t.Errorf("db-b: expected 2, got %v", tenantSev["db-b"])
	}
	if _, ok := tenantSev["db-c"]; ok {
		t.Errorf("db-c should not appear (disabled)")
	}
	if _, ok := tenantSev["db-d"]; ok {
		t.Errorf("db-d should not appear (no silent mode)")
	}
}

// ============================================================
// v1.2.0 Severity Dedup Tests
// ============================================================

func TestResolveSeverityDedup(t *testing.T) {
	tests := []struct {
		name       string
		tenants    map[string]map[string]ScheduledValue
		expectedLen int
		validate   func(t *testing.T, r []ResolvedSeverityDedup)
	}{
		{"DefaultEnable", map[string]map[string]ScheduledValue{"db-a": {}}, 1, func(t *testing.T, r []ResolvedSeverityDedup) {
			if r[0].Tenant != "db-a" || r[0].Mode != "enable" {
				t.Errorf("expected db-a/enable, got %s/%s", r[0].Tenant, r[0].Mode)
			}
		}},
		{"ExplicitEnable", map[string]map[string]ScheduledValue{"db-a": {"_severity_dedup": SV("enable")}}, 1, func(t *testing.T, r []ResolvedSeverityDedup) {
			if r[0].Mode != "enable" {
				t.Errorf("expected mode=enable, got %s", r[0].Mode)
			}
		}},
		{"ExplicitDisable", map[string]map[string]ScheduledValue{"db-a": {"_severity_dedup": SV("disable")}}, 0, nil},
		{"MultiTenant", map[string]map[string]ScheduledValue{"db-a": {}, "db-b": {"_severity_dedup": SV("disable")}, "db-c": {"_severity_dedup": SV("enable")}}, 2, func(t *testing.T, r []ResolvedSeverityDedup) {
			ten := map[string]bool{}
			for _, x := range r {
				ten[x.Tenant] = true
			}
			if !ten["db-a"] || !ten["db-c"] {
				t.Errorf("expected db-a and db-c, got %v", ten)
			}
			if ten["db-b"] {
				t.Errorf("db-b should not appear (disabled)")
			}
		}},
		{"CaseInsensitive", map[string]map[string]ScheduledValue{"db-a": {"_severity_dedup": SV("DISABLE")}, "db-b": {"_severity_dedup": SV("Enable")}}, 1, func(t *testing.T, r []ResolvedSeverityDedup) {
			if r[0].Tenant != "db-b" {
				t.Errorf("expected db-b, got %s", r[0].Tenant)
			}
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &ThresholdConfig{Tenants: tt.tenants}
			resolved := cfg.ResolveSeverityDedup()
			if len(resolved) != tt.expectedLen {
				t.Fatalf("expected %d entries, got %d", tt.expectedLen, len(resolved))
			}
			if tt.validate != nil {
				tt.validate(t, resolved)
			}
		})
	}
}

func TestResolveAt_SkipsSeverityDedupKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_severity_dedup": SV("enable"),
			},
		},
	}
	resolved := cfg.Resolve()
	for _, r := range resolved {
		if r.Metric == "severity_dedup" || r.Metric == "dedup" {
			t.Errorf("_severity_dedup leaked into threshold metrics: %+v", r)
		}
	}
	// Should only have 1 metric: mysql_connections (from defaults)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 threshold metric, got %d", len(resolved))
	}
}

// ============================================================
// ResolveRouting Tests
// ============================================================

func TestResolveRouting_ValidConfig(t *testing.T) {
	routingYAML := `receiver:
  type: "webhook"
  url: "https://webhook.example.com/alerts"
group_by: ["alertname", "severity"]
group_wait: "30s"
group_interval: "1m"
repeat_interval: "4h"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.Tenant != "db-a" {
		t.Errorf("expected tenant db-a, got %s", rc.Tenant)
	}
	if rc.ReceiverType != "webhook" {
		t.Errorf("expected receiver type webhook, got %s", rc.ReceiverType)
	}
	if rc.ReceiverConfig["url"] != "https://webhook.example.com/alerts" {
		t.Errorf("expected receiver url, got %v", rc.ReceiverConfig["url"])
	}
	if len(rc.GroupBy) != 2 || rc.GroupBy[0] != "alertname" || rc.GroupBy[1] != "severity" {
		t.Errorf("unexpected group_by: %v", rc.GroupBy)
	}
	if rc.GroupWait != "30s" {
		t.Errorf("expected group_wait 30s, got %s", rc.GroupWait)
	}
	if rc.GroupInterval != "1m" {
		t.Errorf("expected group_interval 1m, got %s", rc.GroupInterval)
	}
	if rc.RepeatInterval != "4h" {
		t.Errorf("expected repeat_interval 4h, got %s", rc.RepeatInterval)
	}
}

func TestResolveRouting_GuardrailClamp(t *testing.T) {
	// group_wait below minimum (5s), repeat_interval above maximum (72h)
	routingYAML := `receiver:
  type: "webhook"
  url: "https://webhook.example.com/alerts"
group_wait: "1s"
repeat_interval: "100h"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.GroupWait != "5s" {
		t.Errorf("expected group_wait clamped to 5s, got %s", rc.GroupWait)
	}
	if rc.RepeatInterval != "72h" {
		t.Errorf("expected repeat_interval clamped to 72h, got %s", rc.RepeatInterval)
	}
}

func TestResolveRouting_MissingReceiver(t *testing.T) {
	routingYAML := `group_wait: "30s"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 0 {
		t.Fatalf("expected 0 routing configs (missing receiver), got %d", len(resolved))
	}
}

func TestResolveRouting_NoRoutingKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 0 {
		t.Fatalf("expected 0 routing configs, got %d", len(resolved))
	}
}

func TestResolveRouting_MultiTenant(t *testing.T) {
	routingA := `receiver:
  type: "webhook"
  url: "https://webhook-a.example.com/alerts"
group_wait: "10s"`

	routingB := `receiver:
  type: "webhook"
  url: "https://webhook-b.example.com/alerts"
repeat_interval: "2h"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingA),
			},
			"db-b": {
				"_routing": SV(routingB),
			},
			"db-c": {
				"mysql_connections": SV("70"),
				// No routing
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 2 {
		t.Fatalf("expected 2 routing configs, got %d", len(resolved))
	}

	// Sort for deterministic test
	sort.Slice(resolved, func(i, j int) bool {
		return resolved[i].Tenant < resolved[j].Tenant
	})

	if resolved[0].Tenant != "db-a" || resolved[0].ReceiverType != "webhook" {
		t.Errorf("unexpected db-a config: %+v", resolved[0])
	}
	if resolved[1].Tenant != "db-b" || resolved[1].ReceiverType != "webhook" {
		t.Errorf("unexpected db-b config: %+v", resolved[1])
	}
}

func TestResolveRouting_MinimalConfig(t *testing.T) {
	routingYAML := `receiver:
  type: "webhook"
  url: "https://webhook.example.com/alerts"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.ReceiverType != "webhook" {
		t.Errorf("expected receiver type webhook, got %s", rc.ReceiverType)
	}
	if rc.ReceiverConfig["url"] != "https://webhook.example.com/alerts" {
		t.Errorf("expected receiver url, got %v", rc.ReceiverConfig["url"])
	}
	// Optional fields should be empty
	if rc.GroupWait != "" || rc.GroupInterval != "" || rc.RepeatInterval != "" {
		t.Errorf("expected empty timing params, got wait=%s interval=%s repeat=%s",
			rc.GroupWait, rc.GroupInterval, rc.RepeatInterval)
	}
	if len(rc.GroupBy) != 0 {
		t.Errorf("expected empty group_by, got %v", rc.GroupBy)
	}
}

func TestResolveAt_SkipsRoutingKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing":         SV(`receiver: "https://example.com"`),
				"mysql_connections": SV("70"),
			},
		},
	}
	resolved := cfg.Resolve()
	for _, r := range resolved {
		if r.Metric == "routing" || r.Component == "_routing" {
			t.Errorf("_routing leaked into threshold metrics: %+v", r)
		}
	}
	if len(resolved) != 1 {
		t.Fatalf("expected 1 threshold metric, got %d", len(resolved))
	}
}

// TestScheduledValue_RoutingMapRoundTrip verifies that _routing nested maps
// survive YAML unmarshalling → ScheduledValue → ResolveRouting pipeline.
// This is the integration path: YAML file → UnmarshalYAML → ResolveRouting.
func TestScheduledValue_RoutingMapRoundTrip(t *testing.T) {
	yamlInput := `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_wait: "30s"
      group_by: ["alertname", "tenant"]
      repeat_interval: "4h"
`
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(yamlInput), &cfg); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	sv, exists := cfg.Tenants["db-a"]["_routing"]
	if !exists {
		t.Fatal("_routing key not found after unmarshal")
	}
	if sv.Default == "" {
		t.Fatal("_routing ScheduledValue.Default is empty — nested map was lost during unmarshalling")
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.ReceiverType != "webhook" {
		t.Errorf("receiver type = %q, want webhook", rc.ReceiverType)
	}
	if rc.GroupWait != "30s" {
		t.Errorf("group_wait = %q, want 30s", rc.GroupWait)
	}
	if rc.RepeatInterval != "4h" {
		t.Errorf("repeat_interval = %q, want 4h", rc.RepeatInterval)
	}
	if len(rc.GroupBy) != 2 || rc.GroupBy[0] != "alertname" || rc.GroupBy[1] != "tenant" {
		t.Errorf("group_by = %v, want [alertname tenant]", rc.GroupBy)
	}
}

// TestFormatDuration_NoDay verifies formatDuration never outputs "d" suffix
// (Prometheus/Alertmanager only supports s/m/h).
func TestFormatDuration_NoDay(t *testing.T) {
	tests := []struct {
		input time.Duration
		want  string
	}{
		{72 * time.Hour, "72h"},
		{24 * time.Hour, "24h"},
		{48 * time.Hour, "48h"},
		{1 * time.Hour, "1h"},
		{5 * time.Minute, "5m"},
		{30 * time.Second, "30s"},
	}
	for _, tt := range tests {
		got := formatDuration(tt.input)
		if got != tt.want {
			t.Errorf("formatDuration(%v) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

// ============================================================
// Cardinality Guard (v1.5.0)
// ============================================================

func TestCardinalityGuard(t *testing.T) {
	defsWith20 := make(map[string]float64)
	for i := 0; i < 20; i++ {
		defsWith20[fmt.Sprintf("metric_%d", i)] = float64(i)
	}
	defsWith10 := make(map[string]float64)
	for i := 0; i < 10; i++ {
		defsWith10[fmt.Sprintf("m_%d", i)] = float64(i)
	}
	tests := []struct {
		name string
		cfg  ThresholdConfig
		expected int
	}{
		{"UnderLimit", ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70, "redis_memory": 80}, Tenants: map[string]map[string]ScheduledValue{"db-a": {}}, MaxMetricsPerTenant: 10}, 2},
		{"AtLimit", ThresholdConfig{Defaults: map[string]float64{"m1": 1, "m2": 2}, Tenants: map[string]map[string]ScheduledValue{"t": {}}, MaxMetricsPerTenant: 2}, 2},
		{"OverLimitTruncated", ThresholdConfig{Defaults: defsWith20, Tenants: map[string]map[string]ScheduledValue{"t": {}}, MaxMetricsPerTenant: 5}, 5},
		{"DefaultLimit", ThresholdConfig{Defaults: map[string]float64{"m1": 1}, Tenants: map[string]map[string]ScheduledValue{"t": {}}}, 1},
		{"MultiTenantIndependent", ThresholdConfig{Defaults: defsWith10, Tenants: map[string]map[string]ScheduledValue{"t1": {}, "t2": {}}, MaxMetricsPerTenant: 3}, 6},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := tt.cfg.Resolve()
			if len(result) != tt.expected {
				t.Errorf("expected %d metrics, got %d", tt.expected, len(result))
			}
		})
	}
}

// ============================================================
// ValidateTenantKeys (v1.5.0)
// ============================================================

func TestValidateTenantKeys(t *testing.T) {
	tests := []struct {
		name    string
		cfg     ThresholdConfig
		wantLen int
		check   func(t *testing.T, w []string)
	}{
		{"NoWarningsForValidKeys",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70, "redis_memory": 80}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": {Default: "60"}, "mysql_connections_critical": {Default: "90"}, "_silent_mode": {Default: "warning"}, "_severity_dedup": {Default: "enable"}, "_state_maintenance": {Default: "enable"}, "_routing": {Default: "receiver: ..."}}}},
			0, nil},
		{"TypoReservedKey",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silence_mode": {Default: "warning"}}}},
			1, func(t *testing.T, w []string) {
				if !strings.Contains(w[0], "unknown reserved key") || !strings.Contains(w[0], "_silence_mode") {
					t.Errorf("expected unknown reserved key with _silence_mode, got %q", w[0])
				}
			}},
		{"UnknownMetricKey",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"postgres_connections": {Default: "60"}}}},
			1, func(t *testing.T, w []string) {
				if !strings.Contains(w[0], "not in defaults") {
					t.Errorf("expected 'not in defaults', got %q", w[0])
				}
			}},
		{"CriticalSuffixValid",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections_critical": {Default: "90"}}}},
			0, nil},
		{"NamespacesReservedKey",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": {Default: "60"}, "_namespaces": {Default: "[\"ns-a\", \"ns-b\"]"}}}},
			0, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			w := tt.cfg.ValidateTenantKeys()
			if len(w) != tt.wantLen {
				t.Fatalf("expected %d warnings, got %d: %v", tt.wantLen, len(w), w)
			}
			if tt.check != nil {
				tt.check(t, w)
			}
		})
	}
}

// ============================================================
// Structured Silent Mode Tests (v1.7.0)
// ============================================================

func TestResolveSilentModes_Structured(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	past := time.Now().Add(-1 * time.Hour).Format(time.RFC3339)
	tests := []struct {
		name    string
		cfg     *ThresholdConfig
		wantLen int
		check   func(t *testing.T, r []ResolvedSilentMode)
	}{
		{"WithExpires_Active",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("expires: " + future + "\nreason: Planned DB migration\ntarget: warning\n")}}},
			1, func(t *testing.T, r []ResolvedSilentMode) {
				if r[0].Tenant != "db-a" || r[0].TargetSeverity != "warning" || r[0].Expired || r[0].Reason != "Planned DB migration" {
					t.Errorf("unexpected: %+v", r[0])
				}
			}},
		{"WithExpires_Expired",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("expires: " + past + "\nreason: DB migration done\ntarget: all\n")}}},
			2, func(t *testing.T, r []ResolvedSilentMode) {
				for _, x := range r {
					if !x.Expired {
						t.Errorf("expected expired for %s", x.TargetSeverity)
					}
				}
			}},
		{"StructuredNoExpires",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("reason: Long-term silencing\ntarget: critical\n")}}},
			1, func(t *testing.T, r []ResolvedSilentMode) {
				if r[0].Expired || r[0].Expires != (time.Time{}) {
					t.Errorf("should not be expired, expires should be zero")
				}
			}},
		{"StructuredDisable",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("target: disable\n")}}},
			0, nil},
		{"ScalarBackwardCompat",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("warning")}, "db-b": {"_silent_mode": SV("all")}}},
			3, func(t *testing.T, r []ResolvedSilentMode) {
				for _, x := range r {
					if x.Expired || !x.Expires.IsZero() {
						t.Error("scalar should never be expired")
					}
				}
			}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := tt.cfg.ResolveSilentModesAt(time.Now())
			if len(result) != tt.wantLen {
				t.Fatalf("expected %d, got %d", tt.wantLen, len(result))
			}
			if tt.check != nil {
				tt.check(t, result)
			}
		})
	}
}

// endregion

// region MaintenanceModeResolution — maintenance mode states, expiry handling, and validation

// ============================================================
// Structured Maintenance Mode Tests (v1.7.0)
// ============================================================

func TestResolveMaintenanceExpiries(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	past := time.Now().Add(-2 * time.Hour).Format(time.RFC3339)
	tests := []struct {
		name    string
		cfg     *ThresholdConfig
		wantLen int
		check   func(t *testing.T, r []ResolvedMaintenanceExpiry)
	}{
		{"NoMaintenance",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": SV("70")}}},
			0, nil},
		{"ScalarEnable",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("enable")}}},
			0, nil},
		{"StructuredActive",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + future + "\nreason: Scheduled upgrade\ntarget: enable\n")}}},
			1, func(t *testing.T, r []ResolvedMaintenanceExpiry) {
				if r[0].Expired || r[0].Reason != "Scheduled upgrade" {
					t.Errorf("expected active with reason, got: %+v", r[0])
				}
			}},
		{"StructuredExpired",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + past + "\nreason: Upgrade complete\ntarget: enable\n")}}},
			1, func(t *testing.T, r []ResolvedMaintenanceExpiry) {
				if !r[0].Expired {
					t.Error("should be expired")
				}
			}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := tt.cfg.ResolveMaintenanceExpiriesAt(time.Now())
			if len(result) != tt.wantLen {
				t.Fatalf("expected %d, got %d", tt.wantLen, len(result))
			}
			if tt.check != nil {
				tt.check(t, result)
			}
		})
	}
}

func TestResolveStateFilters_MaintenanceExpired(t *testing.T) {
	// When structured _state_maintenance has expired, the state filter should NOT be emitted
	past := time.Now().Add(-1 * time.Hour).Format(time.RFC3339)
	yamlStr := "expires: " + past + "\ntarget: enable\n"
	cfg := &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_state_maintenance": SV(yamlStr)},
		},
	}
	result := cfg.ResolveStateFiltersAt(time.Now())
	if len(result) != 0 {
		t.Errorf("expected 0 (maintenance expired → filter disabled), got %d", len(result))
	}
}

func TestResolveStateFilters_MaintenanceActive(t *testing.T) {
	// When structured _state_maintenance has future expires, the state filter should be emitted
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	yamlStr := "expires: " + future + "\ntarget: enable\n"
	cfg := &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_state_maintenance": SV(yamlStr)},
		},
	}
	result := cfg.ResolveStateFiltersAt(time.Now())
	if len(result) != 1 {
		t.Fatalf("expected 1, got %d", len(result))
	}
	if result[0].Tenant != "db-a" || result[0].FilterName != "maintenance" {
		t.Errorf("unexpected: %+v", result[0])
	}
}

func TestResolveStateFilters_MaintenanceScalarBackwardCompat(t *testing.T) {
	// Scalar "enable" should still work as before
	cfg := &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_state_maintenance": SV("enable")},
		},
	}
	result := cfg.ResolveStateFiltersAt(time.Now())
	if len(result) != 1 {
		t.Errorf("expected 1, got %d", len(result))
	}
}

func TestIsMaintenanceActive(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	past := time.Now().Add(-1 * time.Hour).Format(time.RFC3339)
	tests := []struct {
		name     string
		cfg      *ThresholdConfig
		tenant   string
		isActive bool
	}{
		{"ScalarEnable", &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("enable")}}}, "db-a", true},
		{"Expired", &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + past + "\ntarget: enable\n")}}}, "db-a", false},
		{"NotExpiredYet", &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + future + "\ntarget: enable\n")}}}, "db-a", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if active := tt.cfg.IsMaintenanceActive(tt.tenant, time.Now()); active != tt.isActive {
				t.Errorf("expected active=%v, got %v", tt.isActive, active)
			}
		})
	}
}

// ============================================================
// endregion
