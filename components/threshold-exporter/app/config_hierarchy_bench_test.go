// Hierarchical 1000-tenant baseline benchmarks (v2.8.0 B-1 Phase 1).
//
// These benchmarks measure the production hot path introduced by A-10
// (PR #54): WatchLoop now uses `scanDirHierarchical`, which walks a
// domain/region/env tree with `_defaults.yaml` at every level and tenant
// YAML at leaves. The existing flat benchmarks in `config_bench_test.go`
// measure the legacy `scanDirFileHashes` path, which is still used but
// no longer the primary scanner.
//
// Scope clarifications (from v2.8.0-planning.md §4 B-1):
//   - Phase 1 (this file): synthetic 1000-tenant fixture, infrastructure
//     SLOs (scan / reload / effective). No Prometheus + Alertmanager
//     integration, no customer sample calibration.
//   - Phase 2 (future, blocked on customer data): alert fire-through
//     e2e, customer anonymized sample, definitive SLO sign-off.
//
// Pair of B-8 (blast-radius) bench is in this file because it uses the
// same hierarchical fixture and natural extension of the change-detection
// benchmarks.
package main

import (
	"fmt"
	"io/ioutil"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
)

// ---------------------------------------------------------------------------
// Hierarchical fixture layout (matches scripts/tools/dx/generate_tenant_fixture.py)
// ---------------------------------------------------------------------------
//
// Layout:
//   <root>/
//     _defaults.yaml                              ← L0 platform defaults
//     <domain>/                                   ← L1 per-domain
//       _defaults.yaml
//       <region>/                                 ← L2 per-region
//         _defaults.yaml
//         <env>/                                  ← L3 leaf (prod/staging/dev)
//           _defaults.yaml
//           tenant-NNNN.yaml
//           tenant-NNNN.yaml
//           ...
//
// Dimensions: 8 domains × 6 regions × 3 envs = 144 leaf dirs. For
// numTenants=1000 that's ~7 tenants per leaf (average). Remainder spread
// over low-index leaves so distribution is close to uniform.

var benchDomains = []string{"finance", "logistics", "healthcare", "retail", "media", "infra", "analytics", "iot"}
var benchRegions = []string{"us-east", "us-west", "eu-central", "eu-west", "ap-northeast", "ap-southeast"}
var benchEnvs = []string{"prod", "staging", "dev"}

// buildDirConfigHierarchical builds a hierarchical conf.d/ fixture with
// `numTenants` tenant files spread across domain/region/env subtrees.
// Returns the fixture root path.
//
// Filesystem writes are NOT counted against benchmark time (b.Helper +
// fixture built before b.ResetTimer by the caller). Fixtures are cached
// per numTenants+benchSignature via hierarchicalFixtureCache — repeated
// benchmark invocations in one `go test` run reuse the same tmpdir.
func buildDirConfigHierarchical(b *testing.B, numTenants int) string {
	b.Helper()
	return fetchHierarchicalFixture(b, numTenants, false /*withMutation*/)
}

// buildDirConfigHierarchicalFresh is the non-cached variant for benchmarks
// that mutate the fixture (e.g. blast-radius bench that rewrites
// _defaults.yaml). Each call returns a fresh tmpdir.
func buildDirConfigHierarchicalFresh(b *testing.B, numTenants int) string {
	b.Helper()
	return fetchHierarchicalFixture(b, numTenants, true /*freshDir*/)
}

// ---------------------------------------------------------------------------
// Fixture cache (shared across read-only benchmarks in one test run)
// ---------------------------------------------------------------------------

type cachedFixture struct {
	dir  string
	once sync.Once
	err  error
}

var (
	hierFixtureCache   = map[int]*cachedFixture{}
	hierFixtureCacheMu sync.Mutex
)

func fetchHierarchicalFixture(b *testing.B, numTenants int, freshDir bool) string {
	b.Helper()
	if freshDir {
		return writeHierarchicalBenchFixture(b, numTenants, b.TempDir())
	}
	hierFixtureCacheMu.Lock()
	entry, ok := hierFixtureCache[numTenants]
	if !ok {
		entry = &cachedFixture{}
		hierFixtureCache[numTenants] = entry
	}
	hierFixtureCacheMu.Unlock()

	entry.once.Do(func() {
		// Cached fixtures go in os.TempDir() so they outlive the per-test
		// tmpdir cleanup — otherwise the second benchmark using the same
		// cache entry would find the dir deleted.
		dir, err := ioutil.TempDir("", fmt.Sprintf("hierbench-%d-", numTenants))
		if err != nil {
			entry.err = err
			return
		}
		entry.dir = dir
		if werr := writeHierarchicalBenchFixtureContent(dir, numTenants); werr != nil {
			entry.err = werr
			os.RemoveAll(dir)
		}
	})
	if entry.err != nil {
		b.Fatalf("build cached fixture (%d tenants): %v", numTenants, entry.err)
	}
	return entry.dir
}

// writeHierarchicalBenchFixture writes into the given root (tmpdir from caller).
// Returns root on success; fatals on error.
func writeHierarchicalBenchFixture(b *testing.B, numTenants int, root string) string {
	b.Helper()
	if err := writeHierarchicalBenchFixtureContent(root, numTenants); err != nil {
		b.Fatalf("write fixture: %v", err)
	}
	return root
}

// writeHierarchicalBenchFixtureContent is the pure write path (no testing.B
// dependency) so it can be called from `once.Do` without capturing b.
func writeHierarchicalBenchFixtureContent(root string, numTenants int) error {
	// L0 root _defaults.yaml: platform-wide floor thresholds.
	if err := writeYAMLFile(filepath.Join(root, "_defaults.yaml"), `defaults:
  mysql_connections: 80
  mysql_cpu: 80
  container_cpu: 80
  container_memory: 85
  oracle_sessions_active: 200
  oracle_tablespace_used_pct: 85
  db2_connections_active: 200
  db2_bufferpool_hit_ratio: 0.95
`); err != nil {
		return fmt.Errorf("L0 defaults: %w", err)
	}

	// L1 per-domain _defaults.yaml (mild threshold tweaks).
	for _, domain := range benchDomains {
		dDir := filepath.Join(root, domain)
		if err := os.MkdirAll(dDir, 0o755); err != nil {
			return fmt.Errorf("mkdir %s: %w", dDir, err)
		}
		if err := writeYAMLFile(filepath.Join(dDir, "_defaults.yaml"), fmt.Sprintf(`defaults:
  mysql_connections: %d
  container_cpu: %d
`, 75+len(domain)%10, 70+len(domain)%20)); err != nil {
			return fmt.Errorf("L1 %s defaults: %w", domain, err)
		}

		// L2 per-region _defaults.yaml.
		for _, region := range benchRegions {
			rDir := filepath.Join(dDir, region)
			if err := os.MkdirAll(rDir, 0o755); err != nil {
				return fmt.Errorf("mkdir %s: %w", rDir, err)
			}
			// Region defaults include a key that tenants DO NOT override, so
			// that B-8 blast-radius bench measures actual propagation. If we
			// only set keys that tenant files also define, the tenant-level
			// override shadows the region change and diffAndReload correctly
			// reports noOp ("quiet defaults edit" — see config_debounce.go
			// L313-318). `region_alert_schedule` is not present in tenant
			// files below, so region-level changes DO reach merged_hash.
			if err := writeYAMLFile(filepath.Join(rDir, "_defaults.yaml"), fmt.Sprintf(`defaults:
  container_memory: %d
  region_alert_schedule: "%s-%d"
`, 80+len(region)%10, region, len(region))); err != nil {
				return fmt.Errorf("L2 %s/%s defaults: %w", domain, region, err)
			}

			// L3 per-env _defaults.yaml.
			for _, env := range benchEnvs {
				eDir := filepath.Join(rDir, env)
				if err := os.MkdirAll(eDir, 0o755); err != nil {
					return fmt.Errorf("mkdir %s: %w", eDir, err)
				}
				if err := writeYAMLFile(filepath.Join(eDir, "_defaults.yaml"), fmt.Sprintf(`defaults:
  oracle_sessions_active: %d
`, 150+len(env)%50)); err != nil {
					return fmt.Errorf("L3 %s/%s/%s defaults: %w", domain, region, env, err)
				}
			}
		}
	}

	// Distribute tenants across 144 leaf dirs (8 domains × 6 regions × 3 envs).
	leafCount := len(benchDomains) * len(benchRegions) * len(benchEnvs)
	for i := 0; i < numTenants; i++ {
		leafIdx := i % leafCount
		envIdx := leafIdx % len(benchEnvs)
		regionIdx := (leafIdx / len(benchEnvs)) % len(benchRegions)
		domainIdx := leafIdx / (len(benchEnvs) * len(benchRegions))

		leafDir := filepath.Join(root, benchDomains[domainIdx], benchRegions[regionIdx], benchEnvs[envIdx])
		tenantName := fmt.Sprintf("tenant-%04d", i)
		content := fmt.Sprintf(`tenants:
  %s:
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
			tenantName,
			50+i%100,
			60+i%40,
			70+i%30,
			80+i%15,
			100+i%200,
			75+i%20,
			100+i%150,
			90+i%9,
		)
		if err := writeYAMLFile(filepath.Join(leafDir, tenantName+".yaml"), content); err != nil {
			return fmt.Errorf("tenant %s: %w", tenantName, err)
		}
	}
	return nil
}

func writeYAMLFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o600)
}

// ---------------------------------------------------------------------------
// Resource metrics helper (v2.8.0 B-2 Phase 1)
// ---------------------------------------------------------------------------
//
// Emits custom benchmark metrics via b.ReportMetric so they appear in the
// bench output and can be aggregated later. Forces two GC cycles before
// reading MemStats to collect finalizers that survive the first GC (Go
// runtime lore: one GC cycle may not fully reap because finalizers run
// concurrently; a second cycle hoovers the stragglers).
//
// Metrics:
//   - MB-heap-after-gc: HeapAlloc after forced GC (steady-state working set)
//   - MB-sys:           total virtual memory (OS view)
//   - goroutines:       runtime.NumGoroutine() (leak signal)
//
// Called at the end of a benchmark run (post-b.ResetTimer / post-iteration).
// The caller is responsible for invoking reportResourceMetrics(b) inside the
// bench function, typically right before it returns.
func reportResourceMetrics(b *testing.B) {
	b.Helper()
	runtime.GC()
	runtime.GC() // second pass for finalizer stragglers
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	b.ReportMetric(float64(m.HeapAlloc)/1024/1024, "MB-heap-after-gc")
	b.ReportMetric(float64(m.Sys)/1024/1024, "MB-sys")
	b.ReportMetric(float64(runtime.NumGoroutine()), "goroutines")
}

// ---------------------------------------------------------------------------
// Hierarchical Scaling Benchmarks (1000 / 2000 / 5000)
// ---------------------------------------------------------------------------
//
// These mirror the flat-layout benchmarks in config_bench_test.go but use
// scanDirHierarchical as the production hot path. Pair them off:
//
//   Flat                                            Hierarchical
//   ────                                            ────────────
//   BenchmarkFullDirLoad_1000                       BenchmarkFullDirLoad_Hierarchical_{1000,2000,5000}
//   BenchmarkIncrementalLoad_1000_NoChange          BenchmarkDiffAndReload_Hierarchical_{1000,2000,5000}_NoChange
//   BenchmarkIncrementalLoad_1000_OneFileChanged    BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged
//   BenchmarkScanDirFileHashes_1000                 BenchmarkScanDirHierarchical_{1000,2000,5000}
//
// Plus the new B-8 blast-radius bench (see below).
//
// 2000 / 5000 variants added so we can verify scaling characteristics
// (linear vs super-linear) and inform the sharding decision empirically
// — not via 10× extrapolation from a single 1000-tenant data point.

// Note on IncrementalLoad vs diffAndReload (methodology finding from first
// bench run):
//   - IncrementalLoad (v2.6.0 path) uses scanDirFileHashes — flat, root-only.
//     It DOES NOT see nested files under domain/region/env subdirs.
//   - diffAndReload (post-A-10 path, PR #54) uses scanDirHierarchical and
//     is what WatchLoop calls in production post-A-10. Updates
//     hierarchyHashes + mergedHashes.
// All hierarchical benchmarks below use diffAndReload to measure the real
// production hot path. The v2.6.0 IncrementalLoad path is left in place for
// flat-mode callers and is already covered by the flat benchmarks in
// config_bench_test.go (those use a flat fixture where the two paths
// coincide).

// ── Size-parameterized cores (called by the per-N wrappers below) ──────

func benchScanDirHierarchicalAtSize(b *testing.B, n int) {
	b.Helper()
	dir := buildDirConfigHierarchical(b, n)
	silenceLogs(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, _, _, _, _, err := scanDirHierarchical(dir, nil)
		if err != nil {
			b.Fatal(err)
		}
	}
	b.StopTimer()
	reportResourceMetrics(b)
}

func benchFullDirLoadHierarchicalAtSize(b *testing.B, n int) {
	b.Helper()
	dir := buildDirConfigHierarchical(b, n)
	silenceLogs(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		mgr := NewConfigManager(dir)
		if err := mgr.fullDirLoad(); err != nil {
			b.Fatal(err)
		}
	}
	b.StopTimer()
	reportResourceMetrics(b)
}

func benchDiffAndReloadHierarchicalNoChangeAtSize(b *testing.B, n int) {
	b.Helper()
	dir := buildDirConfigHierarchical(b, n)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, _, err := mgr.diffAndReload(); err != nil {
			b.Fatal(err)
		}
	}
	b.StopTimer()
	reportResourceMetrics(b)
}

// ── 1000-tenant baseline (the "primary" data point published in the
// playbook + CHANGELOG) ────────────────────────────────────────────────

func BenchmarkScanDirHierarchical_1000(b *testing.B) {
	benchScanDirHierarchicalAtSize(b, 1000)
}

func BenchmarkFullDirLoad_Hierarchical_1000(b *testing.B) {
	benchFullDirLoadHierarchicalAtSize(b, 1000)
}

func BenchmarkDiffAndReload_Hierarchical_1000_NoChange(b *testing.B) {
	benchDiffAndReloadHierarchicalNoChangeAtSize(b, 1000)
}

// BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged mutates a
// single tenant file per iteration and measures the reload time. Uses a
// fresh dir (not the shared cache) because we mutate. Kept at 1000-only
// scale because the dominant cost component (trailing fullDirLoad) is
// already separately measured via the *_NoChange variants at 2000/5000.
func BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged(b *testing.B) {
	dir := buildDirConfigHierarchicalFresh(b, 1000)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}
	// Pick a deterministic leaf: tenant-0500 lands in
	// domain[500 / (6*3) % 8] / region[(500/3) % 6] / env[500 % 3].
	leafIdx := 500 % (len(benchDomains) * len(benchRegions) * len(benchEnvs))
	envIdx := leafIdx % len(benchEnvs)
	regionIdx := (leafIdx / len(benchEnvs)) % len(benchRegions)
	domainIdx := leafIdx / (len(benchEnvs) * len(benchRegions))
	targetFile := filepath.Join(dir,
		benchDomains[domainIdx], benchRegions[regionIdx], benchEnvs[envIdx],
		"tenant-0500.yaml")
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		content := fmt.Sprintf("tenants:\n  tenant-0500:\n    mysql_connections: \"%d\"\n    mysql_cpu: \"%d\"\n",
			50+i%100, 60+i%40)
		if err := os.WriteFile(targetFile, []byte(content), 0o600); err != nil {
			b.Fatal(err)
		}
		if _, _, err := mgr.diffAndReload(); err != nil {
			b.Fatal(err)
		}
	}
	b.StopTimer()
	reportResourceMetrics(b)
}

// ── 2000-tenant scaling bench (sharding decision data point) ───────────

func BenchmarkScanDirHierarchical_2000(b *testing.B) {
	benchScanDirHierarchicalAtSize(b, 2000)
}

func BenchmarkFullDirLoad_Hierarchical_2000(b *testing.B) {
	benchFullDirLoadHierarchicalAtSize(b, 2000)
}

func BenchmarkDiffAndReload_Hierarchical_2000_NoChange(b *testing.B) {
	benchDiffAndReloadHierarchicalNoChangeAtSize(b, 2000)
}

// ── 5000-tenant scaling bench (sharding decision data point) ───────────

func BenchmarkScanDirHierarchical_5000(b *testing.B) {
	benchScanDirHierarchicalAtSize(b, 5000)
}

func BenchmarkFullDirLoad_Hierarchical_5000(b *testing.B) {
	benchFullDirLoadHierarchicalAtSize(b, 5000)
}

func BenchmarkDiffAndReload_Hierarchical_5000_NoChange(b *testing.B) {
	benchDiffAndReloadHierarchicalNoChangeAtSize(b, 5000)
}

// ---------------------------------------------------------------------------
// B-8: Blast-radius benchmark
// ---------------------------------------------------------------------------
//
// Target scenario: operator changes a REGION-level _defaults.yaml (e.g.
// tightens a threshold for all prod/staging/dev in us-east under the
// `finance` domain). How many tenants see a merged_hash diff, and how
// long does the incremental reload take?
//
// Relation to existing benchmarks:
//   - BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged measures
//     the smallest-possible change (1 tenant).
//   - This bench measures a typical ops-significant change: one mid-tree
//     _defaults.yaml affecting ~7 × 3 = 21 tenants (1 region × 3 envs ×
//     ~7 tenants-per-leaf under that region).
//
// Emits custom metrics:
//   - affected-tenants: count of tenants whose merged_hash changed post-reload
//   - plus the usual MB-heap / goroutines via reportResourceMetrics
//
// 2000 / 5000 variants confirm scaling: blast-radius scales with N (1 region
// × 3 envs × tenants-per-leaf, where tenants-per-leaf = N / 144).
func benchBlastRadiusDefaultsChangeAtSize(b *testing.B, n int) {
	b.Helper()
	dir := buildDirConfigHierarchicalFresh(b, n)
	silenceLogs(b)
	mgr := NewConfigManager(dir)
	if err := mgr.fullDirLoad(); err != nil {
		b.Fatal(err)
	}

	// Snapshot pre-change merged hashes to diff against post-change.
	preHashes := make(map[string]string, len(mgr.hierarchy.mergedHashes))
	for k, v := range mgr.hierarchy.mergedHashes {
		preHashes[k] = v
	}

	// Target: finance/us-east/_defaults.yaml (region-level). We mutate the
	// `region_alert_schedule` key which tenants do NOT override, so the
	// region-level change propagates into every affected tenant's
	// merged_hash (as opposed to being shadowed by tenant overrides — the
	// "quiet defaults edit" noOp path).
	targetFile := filepath.Join(dir, "finance", "us-east", "_defaults.yaml")
	affected := -1 // sentinel; set in first iteration, stable across b.N runs

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		// Toggle the alert schedule between two values so each iteration
		// changes the file hash. container_memory is included as a stable
		// second key (same value across iterations) to keep the defaults
		// file a reasonable shape.
		schedule := "off-hours"
		if i%2 == 0 {
			schedule = "business-hours"
		}
		content := fmt.Sprintf("defaults:\n  container_memory: 87\n  region_alert_schedule: \"%s\"\n", schedule)
		if err := os.WriteFile(targetFile, []byte(content), 0o600); err != nil {
			b.Fatal(err)
		}
		if _, _, err := mgr.diffAndReload(); err != nil {
			b.Fatal(err)
		}
		if affected < 0 {
			// Count tenants whose merged_hash changed on the first iteration.
			// Subsequent iterations should affect the same set (deterministic).
			count := 0
			for k, post := range mgr.hierarchy.mergedHashes {
				if preHashes[k] != post {
					count++
				}
			}
			affected = count
		}
	}
	b.StopTimer()
	if affected < 0 {
		affected = 0
	}
	b.ReportMetric(float64(affected), "affected-tenants")
	reportResourceMetrics(b)
}

func BenchmarkBlastRadius_DefaultsChange_Hierarchical_1000(b *testing.B) {
	benchBlastRadiusDefaultsChangeAtSize(b, 1000)
}

func BenchmarkBlastRadius_DefaultsChange_Hierarchical_2000(b *testing.B) {
	benchBlastRadiusDefaultsChangeAtSize(b, 2000)
}

func BenchmarkBlastRadius_DefaultsChange_Hierarchical_5000(b *testing.B) {
	benchBlastRadiusDefaultsChangeAtSize(b, 5000)
}
