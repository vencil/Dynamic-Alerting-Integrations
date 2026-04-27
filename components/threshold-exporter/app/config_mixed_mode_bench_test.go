package main

// ============================================================
// B-5 — Mixed-mode benchmarks (v2.8.0 Phase B Track B)
// ============================================================
//
// Mixed-mode is the *transient* state during a flat→hierarchical
// migration. Customers running 1000+ tenants will spend days-to-
// weeks in this state. We need to characterize whether mixed-mode
// scanning is materially slower than pure-hierarchical at the
// same total tenant count, so the migration playbook can decide
// (a) whether to recommend customers complete cutover quickly to
// recover scan perf, or (b) whether mixed-mode is itself a
// supportable steady state.
//
// **Comparison reference**: planning §B-5 sets a ≥10% degradation
// threshold vs pure-hierarchical at the same total tenant count
// as the trigger for follow-up work. These benchmarks emit the
// numbers; the threshold check happens in the bench-record /
// nightly comparison flow, not in this file.
//
// Fixture: 500 flat tenants at root + 500 nested across 8×6×3=144
// leaves (avg ~3.5 tenants/leaf) — total 1000, so latencies are
// directly comparable to BenchmarkFullDirLoad_Hierarchical_1000.
// `_defaults.yaml` files are present at root + per-domain (L1)
// per the realistic mid-migration shape (some inheritance
// established but not all leaves yet).

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// mixedFixtureCache mirrors hierFixtureCache pattern — read-only
// benchmarks share a fixture across iterations / b.N runs.
var (
	mixedFixtureCache    = make(map[mixedFixtureKey]string)
	mixedFixtureCacheMu  sync.Mutex
	mixedFixtureCacheGen sync.Map
)

type mixedFixtureKey struct {
	flatTenants int
	hierTenants int
}

// buildDirConfigMixed returns a cached fixture with the given
// flat:hierarchical split. Cached because cold benches are read-
// only — same content can be reused across b.N iterations.
func buildDirConfigMixed(b *testing.B, flatTenants, hierTenants int) string {
	b.Helper()
	key := mixedFixtureKey{flatTenants, hierTenants}
	mixedFixtureCacheMu.Lock()
	if cached, ok := mixedFixtureCache[key]; ok {
		mixedFixtureCacheMu.Unlock()
		return cached
	}
	mixedFixtureCacheMu.Unlock()

	once, _ := mixedFixtureCacheGen.LoadOrStore(key, &sync.Once{})
	root, err := os.MkdirTemp("", fmt.Sprintf("mixed-bench-%d-%d-", flatTenants, hierTenants))
	if err != nil {
		b.Fatalf("mkdir mixed bench tempdir: %v", err)
	}
	once.(*sync.Once).Do(func() {
		if werr := writeMixedBenchFixtureContent(root, flatTenants, hierTenants); werr != nil {
			b.Fatalf("write mixed fixture: %v", werr)
		}
		mixedFixtureCacheMu.Lock()
		mixedFixtureCache[key] = root
		mixedFixtureCacheMu.Unlock()
	})
	return root
}

// buildDirConfigMixedFresh returns a non-cached temp dir with a
// fresh fixture — for benches that mutate the tree.
func buildDirConfigMixedFresh(b *testing.B, flatTenants, hierTenants int) string {
	b.Helper()
	root := b.TempDir()
	if err := writeMixedBenchFixtureContent(root, flatTenants, hierTenants); err != nil {
		b.Fatalf("write mixed fixture: %v", err)
	}
	return root
}

// writeMixedBenchFixtureContent builds a realistic mid-migration
// mixed-mode tree:
//
//   - L0 root `_defaults.yaml` (platform-wide floor — shared by
//     both flat and nested tenants)
//   - L1 per-domain `_defaults.yaml` (slightly tightened thresholds,
//     applies only to nested tenants under that domain)
//   - `flatTenants` flat tenants at root, named `flat-NNNN.yaml`
//   - `hierTenants` nested tenants distributed across the
//     8 domains × 6 regions × 3 envs = 144 leaves
//
// L2 (region) and L3 (env) `_defaults.yaml` files are deliberately
// NOT created here — mixed-mode mid-migration typically only has
// the top two cascading levels established. This shapes the test
// closer to what customers will see during cutover, not the fully-
// settled hierarchical baseline that pure-hier benchmarks measure.
func writeMixedBenchFixtureContent(root string, flatTenants, hierTenants int) error {
	// L0 root _defaults — same shape as hierarchical fixture so
	// numbers are comparable.
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

	// Flat tenants at root.
	for i := 0; i < flatTenants; i++ {
		tenantName := fmt.Sprintf("flat-%04d", i)
		content := fmt.Sprintf(`tenants:
  %s:
    mysql_connections: "%d"
    mysql_cpu: "%d"
    container_cpu: "%d"
`,
			tenantName,
			60+i%50,
			65+i%30,
			70+i%25,
		)
		if err := writeYAMLFile(filepath.Join(root, tenantName+".yaml"), content); err != nil {
			return fmt.Errorf("flat tenant %s: %w", tenantName, err)
		}
	}

	// L1 per-domain defaults + nested tenants distributed across
	// domain/region/env triplets (no L2/L3 — mid-migration shape).
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
		// region/env subdirs without their own _defaults.yaml.
		for _, region := range benchRegions {
			rDir := filepath.Join(dDir, region)
			if err := os.MkdirAll(rDir, 0o755); err != nil {
				return fmt.Errorf("mkdir %s: %w", rDir, err)
			}
			for _, env := range benchEnvs {
				eDir := filepath.Join(rDir, env)
				if err := os.MkdirAll(eDir, 0o755); err != nil {
					return fmt.Errorf("mkdir %s: %w", eDir, err)
				}
			}
		}
	}

	leafCount := len(benchDomains) * len(benchRegions) * len(benchEnvs)
	for i := 0; i < hierTenants; i++ {
		leafIdx := i % leafCount
		envIdx := leafIdx % len(benchEnvs)
		regionIdx := (leafIdx / len(benchEnvs)) % len(benchRegions)
		domainIdx := leafIdx / (len(benchEnvs) * len(benchRegions))

		leafDir := filepath.Join(root, benchDomains[domainIdx], benchRegions[regionIdx], benchEnvs[envIdx])
		tenantName := fmt.Sprintf("hier-%04d", i)
		content := fmt.Sprintf(`tenants:
  %s:
    mysql_connections: "%d"
    mysql_cpu: "%d"
    container_cpu: "%d"
    container_memory: "%d"
    oracle_sessions_active: "%d"
`,
			tenantName,
			50+i%100,
			60+i%40,
			70+i%30,
			80+i%15,
			100+i%200,
		)
		if err := writeYAMLFile(filepath.Join(leafDir, tenantName+".yaml"), content); err != nil {
			return fmt.Errorf("hier tenant %s: %w", tenantName, err)
		}
	}
	return nil
}

// ─────────────────────────────────────────────────────────────────
// 500flat + 500hier = 1000-tenant total bench triplet
// (directly comparable to *_Hierarchical_1000 series)
// ─────────────────────────────────────────────────────────────────

func BenchmarkScanDirHierarchical_MixedMode_500flat_500hier(b *testing.B) {
	dir := buildDirConfigMixed(b, 500, 500)
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

func BenchmarkFullDirLoad_MixedMode_500flat_500hier(b *testing.B) {
	dir := buildDirConfigMixed(b, 500, 500)
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

func BenchmarkDiffAndReload_MixedMode_500flat_500hier_NoChange(b *testing.B) {
	dir := buildDirConfigMixed(b, 500, 500)
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

// ─────────────────────────────────────────────────────────────────
// 100flat + 900hier = 1000-tenant total bench (late-migration
// shape — most tenants migrated, few stragglers at root)
// ─────────────────────────────────────────────────────────────────

func BenchmarkFullDirLoad_MixedMode_100flat_900hier(b *testing.B) {
	dir := buildDirConfigMixed(b, 100, 900)
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
