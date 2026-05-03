package config_test

// Library-side benchmark for SimulateEffective.
//
// v2.8.0 PR-8 promoted SimulateEffective to pkg/config; this benchmark
// validates the per-call cost at a realistic L4 (4-level) defaults
// chain. cmd/da-guard's predict-flow could call SimulateEffective at
// per-tenant cardinality — knowing the per-call latency informs whether
// it's safe to call N=1000 times in a CI gate.
//
// Naming pattern matches the Makefile bench regex extension PR-9 adds
// for `Simulate` (and `Churn10pct` for the app-side bench) so nightly
// bench-record auto-tracks it.

import (
	"fmt"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// BenchmarkSimulate_DeepChain_L4 runs SimulateEffective with a 4-level
// defaults chain (L0 platform / L1 domain / L2 region / L3 tenant) and
// a tenant.yaml that overrides 5 keys. Each level's _defaults.yaml has
// 8 distinct threshold keys so deep-merge actually walks. Total bytes
// processed per call: ~2 KiB defaults + ~512 B tenant = ~2.5 KiB.
//
// Realistic shape: matches what `cmd/da-guard defaults-impact` would
// see when classifying a tenant in a 4-deep production conf.d.
func BenchmarkSimulate_DeepChain_L4(b *testing.B) {
	defaultsL0 := []byte(`defaults:
  mysql_connections: 80
  mysql_cpu: 80
  container_cpu: 80
  container_memory: 85
  oracle_sessions_active: 200
  oracle_tablespace_used_pct: 85
  redis_memory: 90
  es_index_store_size_bytes: 107374182400
`)
	defaultsL1 := []byte(`defaults:
  mysql_connections: 75
  mysql_cpu: 75
  container_cpu: 75
  container_memory: 80
  oracle_sessions_active: 180
  oracle_tablespace_used_pct: 80
  redis_memory: 85
  es_index_store_size_bytes: 85899345920
`)
	defaultsL2 := []byte(`defaults:
  mysql_connections: 70
  mysql_cpu: 70
  container_cpu: 70
  container_memory: 75
  oracle_sessions_active: 160
  oracle_tablespace_used_pct: 75
  redis_memory: 80
  es_index_store_size_bytes: 64424509440
`)
	defaultsL3 := []byte(`defaults:
  mysql_connections: 65
  mysql_cpu: 65
  container_cpu: 65
  container_memory: 70
  oracle_sessions_active: 140
  oracle_tablespace_used_pct: 70
  redis_memory: 75
  es_index_store_size_bytes: 53687091200
`)
	tenantYAML := []byte(`tenants:
  tenant-deep:
    mysql_connections: "60"
    mysql_cpu: "55:critical"
    container_cpu: "50"
    redis_memory: "70"
    es_index_store_size_bytes: "42949672960"
`)

	req := config.SimulateRequest{
		TenantID:          "tenant-deep",
		TenantYAML:        tenantYAML,
		DefaultsChainYAML: [][]byte{defaultsL0, defaultsL1, defaultsL2, defaultsL3},
	}

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		resp, err := config.SimulateEffective(req)
		if err != nil {
			b.Fatal(err)
		}
		// Touch the response so the compiler can't dead-code-eliminate
		// the call.
		if !strings.HasPrefix(resp.MergedHash, fmt.Sprintf("%c", resp.MergedHash[0])) {
			b.Fatal("merged hash empty")
		}
	}
}

// BenchmarkScanFromConfigSource_1000_InMemory builds a 1000-tenant
// in-memory corpus (4-deep hierarchy, 8-tenant leaves under
// domain/region/env) and runs ScanFromConfigSource end-to-end:
// classification + dedup check + InheritanceGraph construction.
//
// Customer scenario: cmd/da-guard pre-merge gate validates a
// candidate conf.d/ corpus held in memory before any disk write.
// Knowing this baseline lets us answer "can guard validate a
// 1000-tenant change in <1s?" without going through the disk path.
//
// Naming matches the Makefile bench regex _1000(_|$$) so nightly
// bench-record auto-tracks it.
func BenchmarkScanFromConfigSource_1000_InMemory(b *testing.B) {
	const tenantCount = 1000
	domains := []string{"finance", "logistics", "healthcare", "retail", "media", "infra", "analytics", "iot"}
	regions := []string{"us-east", "us-west", "eu-central", "eu-west", "ap-northeast", "ap-southeast"}
	envs := []string{"prod", "staging", "dev"}

	files := make(map[string][]byte, tenantCount+50)
	files["/sim/_defaults.yaml"] = []byte("defaults:\n  mysql_connections: 80\n  mysql_cpu: 80\n")

	leafCardinality := len(domains) * len(regions) * len(envs)
	for di, d := range domains {
		domainPath := "/sim/" + d
		files[domainPath+"/_defaults.yaml"] = []byte(fmt.Sprintf("defaults:\n  mysql_connections: %d\n", 75-di))
		for ri, r := range regions {
			regionPath := domainPath + "/" + r
			files[regionPath+"/_defaults.yaml"] = []byte(fmt.Sprintf("defaults:\n  mysql_cpu: %d\n", 70-ri))
			for ei, e := range envs {
				envPath := regionPath + "/" + e
				files[envPath+"/_defaults.yaml"] = []byte(fmt.Sprintf("defaults:\n  container_cpu: %d\n", 65-ei))
			}
		}
	}

	for i := 0; i < tenantCount; i++ {
		leafIdx := i % leafCardinality
		envIdx := leafIdx % len(envs)
		regionIdx := (leafIdx / len(envs)) % len(regions)
		domainIdx := leafIdx / (len(envs) * len(regions))
		leafPath := fmt.Sprintf("/sim/%s/%s/%s", domains[domainIdx], regions[regionIdx], envs[envIdx])
		tenantName := fmt.Sprintf("tenant-%04d", i)
		files[leafPath+"/"+tenantName+".yaml"] = []byte(fmt.Sprintf(
			"tenants:\n  %s:\n    mysql_connections: \"%d\"\n", tenantName, 50+i%100))
	}

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		src := config.NewInMemoryConfigSource(files)
		tenants, _, _, graph, err := config.ScanFromConfigSource(src, "/sim")
		if err != nil {
			b.Fatal(err)
		}
		if len(tenants) != tenantCount {
			b.Fatalf("got %d tenants, want %d", len(tenants), tenantCount)
		}
		// Touch the graph so the compiler can't dead-code-eliminate.
		if graph == nil || len(graph.TenantDefaults) != tenantCount {
			b.Fatal("graph missing tenants")
		}
	}
}
