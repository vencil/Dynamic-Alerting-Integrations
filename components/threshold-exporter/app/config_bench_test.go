package main

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// silenceLogs suppresses log output for the duration of the benchmark,
// eliminating ~732KB of noise from per-load log lines (100 tenants × N iterations).
func silenceLogs(b *testing.B) {
	b.Helper()
	orig := log.Writer()
	log.SetOutput(io.Discard)
	b.Cleanup(func() { log.SetOutput(orig) })
}

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

// buildSilentConfig creates a ThresholdConfig with N tenants, each with
// a mix of silent mode settings for benchmarking ResolveSilentModes.
func buildSilentConfig(numTenants int) *ThresholdConfig {
	modes := []string{"warning", "critical", "all", "disable"}
	tenants := make(map[string]map[string]ScheduledValue, numTenants)
	for i := 0; i < numTenants; i++ {
		name := fmt.Sprintf("tenant-%04d", i)
		tenants[name] = map[string]ScheduledValue{
			"_silent_mode": SV(modes[i%len(modes)]),
		}
	}
	return &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants:  tenants,
	}
}

func BenchmarkResolveSilentModes_1000(b *testing.B) {
	cfg := buildSilentConfig(1000)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		cfg.ResolveSilentModes()
	}
}

// ── Incremental Reload Benchmarks (v2.1.0 §5.6) ────────────

// buildDirConfig creates a temporary directory with N tenant YAML files
// plus a _defaults.yaml, suitable for benchmarking directory-mode reload.
// Each tenant has 8 metrics (matching real-world config density) including
// scalar, regex dimensional, and scheduled overrides.
func buildDirConfig(b *testing.B, numTenants int) string {
	b.Helper()
	dir := b.TempDir()

	defaults := `defaults:
  mysql_connections: 80
  mysql_cpu: 80
  container_cpu: 80
  container_memory: 85
  oracle_sessions_active: 200
  oracle_tablespace_used_pct: 85
  db2_connections_active: 200
  db2_bufferpool_hit_ratio: 0.95
`
	os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults), 0600)

	for i := 0; i < numTenants; i++ {
		name := fmt.Sprintf("tenant-%04d.yaml", i)
		content := fmt.Sprintf(`tenants:
  tenant-%04d:
    mysql_connections: "%d"
    mysql_cpu: "%d"
    container_cpu:
      default: "%d"
      overrides:
        - window: "22:00-06:00"
          value: "95"
    container_memory: "%d"
    oracle_sessions_active: "%d"
    oracle_tablespace_used_pct: "%d"
    db2_connections_active: "%d"
    db2_bufferpool_hit_ratio: "0.%d"
`,
			i,
			50+i%100,  // mysql_connections
			60+i%40,   // mysql_cpu
			70+i%30,   // container_cpu
			80+i%15,   // container_memory
			100+i%200, // oracle_sessions_active
			75+i%20,   // oracle_tablespace_used_pct
			100+i%150, // db2_connections_active
			90+i%9,    // db2_bufferpool_hit_ratio
		)
		os.WriteFile(filepath.Join(dir, name), []byte(content), 0600)
	}
	return dir
}

// BenchmarkFullDirLoad_100 benchmarks full directory load with 100 tenants.
func BenchmarkFullDirLoad_100(b *testing.B) {
	dir := buildDirConfig(b, 100)
	silenceLogs(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		mgr := NewConfigManager(dir)
		if err := mgr.fullDirLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkIncrementalLoad_100_NoChange benchmarks incremental reload
// when nothing has changed (should be near-zero cost after hash check).
func BenchmarkIncrementalLoad_100_NoChange(b *testing.B) {
	dir := buildDirConfig(b, 100)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if err := mgr.IncrementalLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkIncrementalLoad_100_OneFileChanged benchmarks incremental reload
// when exactly one tenant file has changed out of 100.
func BenchmarkIncrementalLoad_100_OneFileChanged(b *testing.B) {
	dir := buildDirConfig(b, 100)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	targetFile := filepath.Join(dir, "tenant-0050.yaml")
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		// Modify one file each iteration (full 8-metric content)
		content := fmt.Sprintf("tenants:\n  tenant-0050:\n    mysql_connections: \"%d\"\n    mysql_cpu: \"%d\"\n    container_cpu: \"%d\"\n    container_memory: \"%d\"\n",
			50+i%100, 60+i%40, 70+i%30, 80+i%15)
		os.WriteFile(targetFile, []byte(content), 0600)
		if err := mgr.IncrementalLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkScanDirFileHashes_100 benchmarks the cheap hash-scan phase.
func BenchmarkScanDirFileHashes_100(b *testing.B) {
	dir := buildDirConfig(b, 100)
	silenceLogs(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		scanDirFileHashes(dir, nil, nil)
	}
}

// backdateFiles sets all YAML files in dir to 10 seconds in the past,
// ensuring the mtime guard's 2-second freshness window allows cache hits.
func backdateFiles(b *testing.B, dir string) {
	b.Helper()
	past := time.Now().Add(-10 * time.Second)
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if !e.IsDir() {
			os.Chtimes(filepath.Join(dir, e.Name()), past, past)
		}
	}
}

// BenchmarkScanDirFileHashes_100_MtimeGuard benchmarks the mtime-guarded scan
// where all files have unchanged mtime+size, so SHA-256 is skipped entirely.
func BenchmarkScanDirFileHashes_100_MtimeGuard(b *testing.B) {
	dir := buildDirConfig(b, 100)
	silenceLogs(b)
	backdateFiles(b, dir)
	// Initial scan to populate mtime cache
	hashes, _, mtimes, _, _ := scanDirFileHashes(dir, nil, nil)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		scanDirFileHashes(dir, hashes, mtimes)
	}
}

// BenchmarkMergePartialConfigs_100 benchmarks the merge phase from cache.
func BenchmarkMergePartialConfigs_100(b *testing.B) {
	dir := buildDirConfig(b, 100)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	mgr.fullDirLoad()
	configs := mgr.fileConfigs
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		mergePartialConfigs(configs)
	}
}

// ── 1000-Tenant Incremental Benchmarks ──────────────────────

func BenchmarkFullDirLoad_1000(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		mgr := NewConfigManager(dir)
		if err := mgr.fullDirLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkIncrementalLoad_1000_NoChange(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if err := mgr.IncrementalLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkIncrementalLoad_1000_NoChange_MtimeGuard measures NoChange with
// mtime guard active (files backdated so stat-only path fires).
func BenchmarkIncrementalLoad_1000_NoChange_MtimeGuard(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	backdateFiles(b, dir)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if err := mgr.IncrementalLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkIncrementalLoad_1000_OneFileChanged(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	targetFile := filepath.Join(dir, "tenant-0500.yaml")
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		content := fmt.Sprintf("tenants:\n  tenant-0500:\n    mysql_connections: \"%d\"\n    mysql_cpu: \"%d\"\n    container_cpu: \"%d\"\n    container_memory: \"%d\"\n",
			50+i%100, 60+i%40, 70+i%30, 80+i%15)
		os.WriteFile(targetFile, []byte(content), 0600)
		if err := mgr.IncrementalLoad(); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkScanDirFileHashes_1000(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		scanDirFileHashes(dir, nil, nil)
	}
}

// BenchmarkScanDirFileHashes_1000_MtimeGuard benchmarks mtime-guarded scan at 1000T.
func BenchmarkScanDirFileHashes_1000_MtimeGuard(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	backdateFiles(b, dir)
	hashes, _, mtimes, _, _ := scanDirFileHashes(dir, nil, nil)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		scanDirFileHashes(dir, hashes, mtimes)
	}
}

func BenchmarkMergePartialConfigs_1000(b *testing.B) {
	dir := buildDirConfig(b, 1000)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	mgr.fullDirLoad()
	configs := mgr.fileConfigs
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		mergePartialConfigs(configs)
	}
}
