package main

import (
	"fmt"
	"testing"
	"time"
)

// ── Helpers ─────────────────────────────────────────────────

// buildScalarConfig creates a ThresholdConfig with N tenants, each having
// simple scalar thresholds (the v0.1.0 baseline case).
func buildScalarConfig(numTenants int) *ThresholdConfig {
	defaults := map[string]float64{
		"mysql_connections":          80,
		"mysql_cpu":                  80,
		"container_cpu":              80,
		"container_memory":           85,
		"oracle_sessions_active":     200,
		"oracle_tablespace_used_pct": 85,
		"db2_connections_active":     200,
		"db2_bufferpool_hit_ratio":   0.95,
	}
	tenants := make(map[string]map[string]ScheduledValue, numTenants)
	for i := 0; i < numTenants; i++ {
		name := fmt.Sprintf("tenant-%04d", i)
		tenants[name] = map[string]ScheduledValue{
			"mysql_connections":          SV(fmt.Sprintf("%d", 50+i%100)),
			"mysql_cpu":                  SV(fmt.Sprintf("%d", 60+i%40)),
			"oracle_sessions_active":     SV(fmt.Sprintf("%d", 100+i%200)),
			"oracle_tablespace_used_pct": SV(fmt.Sprintf("%d", 75+i%20)),
			"db2_connections_active":     SV(fmt.Sprintf("%d", 100+i%150)),
		}
	}
	return &ThresholdConfig{Defaults: defaults, Tenants: tenants}
}

// buildMixedConfig creates a ThresholdConfig with N tenants, each having
// a mix of scalar, regex dimensional, and scheduled thresholds.
func buildMixedConfig(numTenants int) *ThresholdConfig {
	defaults := map[string]float64{
		"mysql_connections":          80,
		"mysql_cpu":                  80,
		"container_cpu":              80,
		"container_memory":           85,
		"oracle_sessions_active":     200,
		"oracle_tablespace_used_pct": 85,
		"db2_connections_active":     200,
		"db2_bufferpool_hit_ratio":   0.95,
	}
	tenants := make(map[string]map[string]ScheduledValue, numTenants)
	for i := 0; i < numTenants; i++ {
		name := fmt.Sprintf("tenant-%04d", i)
		t := map[string]ScheduledValue{
			// Scalar
			"mysql_connections":      SV(fmt.Sprintf("%d", 50+i%100)),
			"mysql_cpu":              SV(fmt.Sprintf("%d", 60+i%40)),
			"oracle_sessions_active": SV(fmt.Sprintf("%d", 100+i%200)),
			"db2_connections_active": SV(fmt.Sprintf("%d", 100+i%150)),
			// Regex dimensional
			`oracle_tablespace_used_pct{tablespace_name=~"USERS|DATA.*"}`: SV("90"),
			`db2_bufferpool_hit_ratio{bufferpool_name=~"IBMDEFAULT.*"}`:   SV("0.90"),
			// Scheduled (time-window overrides)
			"container_cpu": SVScheduled("80",
				TimeWindowOverride{Window: "22:00-06:00", Value: "95"},
			),
			"container_memory": SVScheduled("85",
				TimeWindowOverride{Window: "02:00-05:00", Value: "disable"},
			),
		}
		tenants[name] = t
	}
	return &ThresholdConfig{Defaults: defaults, Tenants: tenants}
}

// ── Benchmarks ──────────────────────────────────────────────

func BenchmarkResolve_10Tenants_Scalar(b *testing.B) {
	cfg := buildScalarConfig(10)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.Resolve()
	}
}

func BenchmarkResolve_100Tenants_Scalar(b *testing.B) {
	cfg := buildScalarConfig(100)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.Resolve()
	}
}

func BenchmarkResolve_1000Tenants_Scalar(b *testing.B) {
	cfg := buildScalarConfig(1000)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.Resolve()
	}
}

func BenchmarkResolveAt_10Tenants_Mixed(b *testing.B) {
	cfg := buildMixedConfig(10)
	now := time.Date(2026, 2, 28, 14, 0, 0, 0, time.UTC)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.ResolveAt(now)
	}
}

func BenchmarkResolveAt_100Tenants_Mixed(b *testing.B) {
	cfg := buildMixedConfig(100)
	now := time.Date(2026, 2, 28, 14, 0, 0, 0, time.UTC)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.ResolveAt(now)
	}
}

func BenchmarkResolveAt_1000Tenants_Mixed(b *testing.B) {
	cfg := buildMixedConfig(1000)
	now := time.Date(2026, 2, 28, 14, 0, 0, 0, time.UTC)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.ResolveAt(now)
	}
}

func BenchmarkResolveAt_NightWindow_1000Tenants(b *testing.B) {
	cfg := buildMixedConfig(1000)
	// 03:00 — inside the 02:00-05:00 window for container_memory disable
	now := time.Date(2026, 2, 28, 3, 0, 0, 0, time.UTC)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.ResolveAt(now)
	}
}
