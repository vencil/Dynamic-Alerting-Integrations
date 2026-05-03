package main

// Dimensional metric tests — exact-match labels (`{queue='tasks'}`) and
// regex labels (`{tablespace=~'SYS.*'}`). Split out of config_test.go in
// PR-2; shared helpers (SV / SVScheduled / writeTestFile) live in
// config_test.go.

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"
)

// region DimensionalMetrics — label parsing and dimensional resolution

// ============================================================
// Phase 2B: Dimensional Metrics Tests
// ============================================================

func TestParseKeyWithLabels(t *testing.T) {
	tests := []struct {
		input       string
		wantBase    string
		wantLabels  map[string]string
		wantRegex   map[string]string
	}{
		// No labels
		{"redis_memory", "redis_memory", nil, nil},
		{"standalone", "standalone", nil, nil},
		// Single label (double quotes)
		{`redis_db_keys{db="db0"}`, "redis_db_keys", map[string]string{"db": "db0"}, nil},
		// Single label (single quotes)
		{`redis_db_keys{db='db0'}`, "redis_db_keys", map[string]string{"db": "db0"}, nil},
		// Multiple labels
		{`redis_queue_length{queue="tasks", priority="high"}`, "redis_queue_length", map[string]string{"queue": "tasks", "priority": "high"}, nil},
		// Spaces around equals and commas
		{`es_index_size{index = "logstash-*" , tier = "hot"}`, "es_index_size", map[string]string{"index": "logstash-*", "tier": "hot"}, nil},
		// B1: Regex label
		{`oracle_tablespace{tablespace=~"SYS.*"}`, "oracle_tablespace", nil, map[string]string{"tablespace": "SYS.*"}},
		// B1: Mixed exact + regex
		{`oracle_ts{env="prod", tablespace=~"SYS.*"}`, "oracle_ts", map[string]string{"env": "prod"}, map[string]string{"tablespace": "SYS.*"}},
	}

	for _, tt := range tests {
		base, labels, regex := parseKeyWithLabels(tt.input)
		if base != tt.wantBase {
			t.Errorf("parseKeyWithLabels(%q): base = %q, want %q", tt.input, base, tt.wantBase)
		}
		// Check exact labels
		if tt.wantLabels == nil {
			if labels != nil {
				t.Errorf("parseKeyWithLabels(%q): labels = %v, want nil", tt.input, labels)
			}
		} else {
			if len(labels) != len(tt.wantLabels) {
				t.Errorf("parseKeyWithLabels(%q): labels count = %d, want %d", tt.input, len(labels), len(tt.wantLabels))
				continue
			}
			for k, v := range tt.wantLabels {
				if labels[k] != v {
					t.Errorf("parseKeyWithLabels(%q): labels[%q] = %q, want %q", tt.input, k, labels[k], v)
				}
			}
		}
		// Check regex labels
		if tt.wantRegex == nil {
			if regex != nil {
				t.Errorf("parseKeyWithLabels(%q): regex = %v, want nil", tt.input, regex)
			}
		} else {
			if len(regex) != len(tt.wantRegex) {
				t.Errorf("parseKeyWithLabels(%q): regex count = %d, want %d", tt.input, len(regex), len(tt.wantRegex))
				continue
			}
			for k, v := range tt.wantRegex {
				if regex[k] != v {
					t.Errorf("parseKeyWithLabels(%q): regex[%q] = %q, want %q", tt.input, k, regex[k], v)
				}
			}
		}
	}
}

func TestResolve_DimensionalBasic(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"redis_memory": 80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"redis_memory": SV("75"),
				`redis_queue_length{queue="tasks"}`:                   SV("500"),
				`redis_queue_length{queue="events", priority="high"}`: SV("1000:critical"),
			},
		},
	}

	resolved := cfg.Resolve()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Metric != resolved[j].Metric {
			return resolved[i].Metric < resolved[j].Metric
		}
		return resolved[i].Value < resolved[j].Value
	})

	// Expected: 1 base metric (redis_memory=75) + 2 dimensional (queue_length 500 + 1000)
	if len(resolved) != 3 {
		t.Fatalf("expected 3 resolved, got %d: %+v", len(resolved), resolved)
	}

	// Find the base metric
	var base *ResolvedThreshold
	var dims []ResolvedThreshold
	for i := range resolved {
		if len(resolved[i].CustomLabels) == 0 {
			base = &resolved[i]
		} else {
			dims = append(dims, resolved[i])
		}
	}

	if base == nil {
		t.Fatal("expected a base metric without custom labels")
	}
	if base.Metric != "memory" || base.Value != 75 || base.Component != "redis" {
		t.Errorf("base metric: got metric=%s value=%.0f component=%s", base.Metric, base.Value, base.Component)
	}

	if len(dims) != 2 {
		t.Fatalf("expected 2 dimensional metrics, got %d", len(dims))
	}

	sort.Slice(dims, func(i, j int) bool { return dims[i].Value < dims[j].Value })

	// queue_length 500 (warning)
	if dims[0].Metric != "queue_length" || dims[0].Value != 500 || dims[0].Severity != "warning" {
		t.Errorf("dim[0]: got metric=%s value=%.0f severity=%s", dims[0].Metric, dims[0].Value, dims[0].Severity)
	}
	if dims[0].CustomLabels["queue"] != "tasks" {
		t.Errorf("dim[0]: expected queue=tasks, got %v", dims[0].CustomLabels)
	}

	// queue_length 1000 (critical)
	if dims[1].Metric != "queue_length" || dims[1].Value != 1000 || dims[1].Severity != "critical" {
		t.Errorf("dim[1]: got metric=%s value=%.0f severity=%s", dims[1].Metric, dims[1].Value, dims[1].Severity)
	}
	if dims[1].CustomLabels["queue"] != "events" || dims[1].CustomLabels["priority"] != "high" {
		t.Errorf("dim[1]: expected queue=events priority=high, got %v", dims[1].CustomLabels)
	}
}

func TestResolve_DimensionalDisable(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`redis_queue_length{queue="tasks"}`:  SV("500"),
				`redis_queue_length{queue="events"}`: SV("disable"),
			},
		},
	}

	resolved := cfg.Resolve()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 (disabled one skipped), got %d: %+v", len(resolved), resolved)
	}
	if resolved[0].CustomLabels["queue"] != "tasks" {
		t.Errorf("expected queue=tasks, got %v", resolved[0].CustomLabels)
	}
}

func TestResolve_DimensionalBackwardCompat(t *testing.T) {
	// Non-dimensional config should still work identically
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"mysql_connections": 80,
			"mysql_cpu":         80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"mysql_connections": SV("disable"), "mysql_cpu": SV("40")},
		},
	}

	resolved := cfg.Resolve()
	for _, r := range resolved {
		if len(r.CustomLabels) > 0 {
			t.Errorf("non-dimensional config should have no CustomLabels, got %v", r.CustomLabels)
		}
	}

	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].Metric < resolved[j].Metric
	})

	if len(resolved) != 3 {
		t.Fatalf("expected 3, got %d", len(resolved))
	}
}

func TestResolve_DimensionalWithDirMode(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  redis_memory: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    redis_memory: "75"
    "redis_db_keys{db=\"db0\"}": "1000"
    "redis_db_keys{db=\"db1\"}": "disable"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	resolved := mgr.GetConfig().Resolve()
	// Expected: redis_memory=75 + redis_db_keys{db=db0}=1000 = 2 (db1 disabled)
	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d: %+v", len(resolved), resolved)
	}

	var hasDimensional bool
	for _, r := range resolved {
		if len(r.CustomLabels) > 0 {
			hasDimensional = true
			if r.CustomLabels["db"] != "db0" || r.Value != 1000 {
				t.Errorf("expected db=db0 value=1000, got %v value=%.0f", r.CustomLabels, r.Value)
			}
		}
	}
	if !hasDimensional {
		t.Error("expected at least one dimensional metric")
	}
}

// endregion
// region RegexDimensionalMetrics — regex label parsing and regex-based threshold resolution

// ============================================================
// Phase 11 B1: Regex Dimensional Labels Tests
// ============================================================

func TestParseLabelsStringWithOp(t *testing.T) {
	tests := []struct {
		input     string
		wantExact map[string]string
		wantRegex map[string]string
	}{
		// Pure exact
		{`queue="tasks"`, map[string]string{"queue": "tasks"}, map[string]string{}},
		// Pure regex
		{`tablespace=~"SYS.*"`, map[string]string{}, map[string]string{"tablespace": "SYS.*"}},
		// Mixed
		{`env="prod", tablespace=~"SYS.*"`, map[string]string{"env": "prod"}, map[string]string{"tablespace": "SYS.*"}},
		// Multiple regex
		{`ns=~"db-.*", table=~"SYS.*"`, map[string]string{}, map[string]string{"ns": "db-.*", "table": "SYS.*"}},
	}

	for _, tt := range tests {
		exact, regex := parseLabelsStringWithOp(tt.input)
		for k, v := range tt.wantExact {
			if exact[k] != v {
				t.Errorf("parseLabelsStringWithOp(%q): exact[%q] = %q, want %q", tt.input, k, exact[k], v)
			}
		}
		for k, v := range tt.wantRegex {
			if regex[k] != v {
				t.Errorf("parseLabelsStringWithOp(%q): regex[%q] = %q, want %q", tt.input, k, regex[k], v)
			}
		}
	}
}

func TestResolve_RegexDimensional(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"oracle_tablespace": 80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"oracle_tablespace":                              SV("75"),
				`oracle_tablespace{tablespace=~"SYS.*"}`:         SV("95"),
				`oracle_tablespace{tablespace=~"USER.*"}`:        SV("500:critical"),
				`oracle_tablespace{env="prod", ts=~"TEMP.*"}`:    SV("200"),
			},
		},
	}

	resolved := cfg.Resolve()

	// Should get: 1 base + 3 dimensional = 4
	if len(resolved) != 4 {
		t.Fatalf("expected 4, got %d: %+v", len(resolved), resolved)
	}

	var base *ResolvedThreshold
	var dims []ResolvedThreshold
	for i := range resolved {
		if len(resolved[i].CustomLabels) == 0 && len(resolved[i].RegexLabels) == 0 {
			base = &resolved[i]
		} else {
			dims = append(dims, resolved[i])
		}
	}

	if base == nil || base.Value != 75 {
		t.Errorf("base: expected value=75, got %+v", base)
	}

	if len(dims) != 3 {
		t.Fatalf("expected 3 dimensional, got %d", len(dims))
	}

	sort.Slice(dims, func(i, j int) bool { return dims[i].Value < dims[j].Value })

	// 95: regex only
	if dims[0].RegexLabels["tablespace"] != "SYS.*" || dims[0].Value != 95 {
		t.Errorf("dim[0]: expected tablespace_re=SYS.* value=95, got %+v", dims[0])
	}
	if len(dims[0].CustomLabels) != 0 {
		t.Errorf("dim[0]: expected no exact labels, got %v", dims[0].CustomLabels)
	}

	// 200: mixed exact + regex
	if dims[1].CustomLabels["env"] != "prod" || dims[1].RegexLabels["ts"] != "TEMP.*" || dims[1].Value != 200 {
		t.Errorf("dim[1]: got %+v", dims[1])
	}

	// 500: regex + critical severity
	if dims[2].RegexLabels["tablespace"] != "USER.*" || dims[2].Value != 500 || dims[2].Severity != "critical" {
		t.Errorf("dim[2]: got %+v", dims[2])
	}
}

func TestResolve_RegexDimensionalWithYAML(t *testing.T) {
	content := `
defaults:
  oracle_tablespace: 80
tenants:
  db-a:
    oracle_tablespace: "75"
    "oracle_tablespace{tablespace=~\"SYS.*\"}": "95"
`
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}

	mgr := NewConfigManager(path)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	resolved := mgr.GetConfig().Resolve()
	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d: %+v", len(resolved), resolved)
	}

	var hasRegex bool
	for _, r := range resolved {
		if len(r.RegexLabels) > 0 {
			hasRegex = true
			if r.RegexLabels["tablespace"] != "SYS.*" || r.Value != 95 {
				t.Errorf("expected tablespace_re=SYS.* value=95, got %v value=%.0f", r.RegexLabels, r.Value)
			}
		}
	}
	if !hasRegex {
		t.Error("expected at least one regex dimensional metric")
	}
}

func TestResolve_RegexScheduled(t *testing.T) {
	// B1 + B4 combined: regex dimensional with time-window override
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"oracle_tablespace": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_tablespace{tablespace=~"SYS.*"}`: SVScheduled("95",
					TimeWindowOverride{Window: "01:00-09:00", Value: "disable"},
				),
			},
		},
	}

	// During window — disabled
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	resolved := cfg.ResolveAt(inside)
	// Only base metric (from default), regex one is disabled
	baseCount := 0
	for _, r := range resolved {
		if len(r.RegexLabels) == 0 && len(r.CustomLabels) == 0 {
			baseCount++
		}
	}
	if baseCount != 1 {
		t.Errorf("during window: expected 1 base metric only, got %d total: %+v", len(resolved), resolved)
	}

	// Outside window — regex metric active
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	resolved = cfg.ResolveAt(outside)
	if len(resolved) != 2 {
		t.Errorf("outside window: expected 2 (base + regex), got %d: %+v", len(resolved), resolved)
	}
}

// ============================================================
// Negative Tests: Unsupported Combinations
// ============================================================

// TestResolve_RegexDimensionalCriticalNotSupported verifies that
// _critical suffix on regex dimensional keys has no effect.
// Regex dimensional keys must use "value:severity" syntax instead.
func TestResolve_RegexDimensionalCriticalNotSupported(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"oracle_tablespace": 90,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"oracle_tablespace":                                   SV("85"),
				`oracle_tablespace{tablespace=~"SYS.*"}`:             SV("95"),
				`oracle_tablespace{tablespace=~"SYS.*"}_critical`:    SV("99"),
			},
		},
	}

	resolved := cfg.Resolve()

	// The _critical key with {} is NOT processed by the _critical scanner
	// because it doesn't match: it contains "{" so it enters the dimensional block,
	// but "oracle_tablespace{tablespace=~\"SYS.*\"}_critical" won't parse as
	// a valid dimensional key (the _critical suffix is outside the braces and
	// the base key becomes "oracle_tablespace{tablespace=~\"SYS" which is invalid).
	// Result: the _critical key is silently ignored.
	// The _critical scanner also won't pick it up because it contains "{".
	//
	// Expected: base metric (85) + regex dimensional (95) = 2
	// The _critical key produces nothing.
	var hasRegex bool
	var hasCriticalSeverity bool
	for _, r := range resolved {
		if len(r.RegexLabels) > 0 {
			hasRegex = true
		}
		if r.Severity == "critical" {
			hasCriticalSeverity = true
		}
	}

	if !hasRegex {
		t.Error("expected regex dimensional metric to be present")
	}
	if hasCriticalSeverity {
		t.Error("regex dimensional + _critical suffix should NOT produce a critical metric; use 'value:critical' syntax instead")
	}

	// Correct approach: use "95:critical" value syntax for regex dimensional
	cfg2 := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_tablespace{tablespace=~"SYS.*"}`: SV("95:critical"),
			},
		},
	}
	resolved2 := cfg2.Resolve()
	if len(resolved2) != 1 || resolved2[0].Severity != "critical" {
		t.Errorf("value:critical syntax should work for regex dimensional, got %+v", resolved2)
	}
}

// endregion
