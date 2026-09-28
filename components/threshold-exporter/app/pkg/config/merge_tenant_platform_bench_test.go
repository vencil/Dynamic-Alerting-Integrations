package config

// merge_tenant_platform_bench_test.go — what the root platform layer (#2208)
// costs the tenant-api merge core on the reviewer's 1000-file tree
// (buildMergeBenchTree) plus one root `_platform.yaml` carrying 0 / 1000 /
// 10000 `tenants:` entries: MergeParsedTenantWithRootDefaults +
// ValidateTenantKeys, the work gitops.validate does inside the writer token.
//
// Cached = the decode cache hits (a platform file that did not change since
// the last call — the steady state). Uncached = the cache emptied before
// every call (the first call after an edit).

import (
	"fmt"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func buildPlatformBenchTree(b *testing.B, entries int) string {
	b.Helper()
	dir := buildMergeBenchTree(b)
	if entries > 0 {
		var s strings.Builder
		s.WriteString("tenants:\n")
		for i := 0; i < entries; i++ {
			fmt.Fprintf(&s, "  tenant-%03d:\n    metric_002: \"%d\"\n    metric_003: \"%d\"\n", i, 60+i%10, 61+i%10)
		}
		rootWrite(b, filepath.Join(dir, "_platform.yaml"), s.String())
	}
	return dir
}

func benchPlatformMerge(b *testing.B, entries int, uncached bool) {
	// The decode cache is package-global: a benchmark that leaves a
	// 1000/10000-entry parse in it inflates the live heap every later
	// benchmark in the same binary runs against. The PR bench gate selects
	// these (`_1000(_|$)`) and runs them before Simulate_DeepChain and
	// ScanFromConfigSource_1000_InMemory, which then read as a 5–13%
	// regression that is only GC pressure from this leftover. Empty it on
	// the way out.
	b.Cleanup(func() {
		rootPlatformParses.reset()
		runtime.GC()
	})
	dir := buildPlatformBenchTree(b, entries)
	body := ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
		"tenant-001": {"metric_001": {Default: "70"}}}}
	want := "50" // metric_002's default, applied as a threshold only by the platform layer
	if entries > 1 {
		want = "61"
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if uncached {
			rootPlatformParses.reset()
		}
		m := MergeParsedTenantWithRootDefaults(dir, body)
		if kv := m.ValidateTenantKeys(); len(kv.Errors) != 0 {
			b.Fatalf("errors: %v", kv.Errors)
		}
		if len(m.Defaults) != 100 {
			b.Fatalf("Defaults = %d keys", len(m.Defaults))
		}
		if entries > 1 && m.Tenants["tenant-001"]["metric_002"].Default != want {
			b.Fatalf("platform layer not applied: %+v", m.Tenants["tenant-001"])
		}
	}
}

func BenchmarkMergeParsedPlatform_0(b *testing.B)             { benchPlatformMerge(b, 0, false) }
func BenchmarkMergeParsedPlatform_1000_Cached(b *testing.B)   { benchPlatformMerge(b, 1000, false) }
func BenchmarkMergeParsedPlatform_1000_Uncached(b *testing.B) { benchPlatformMerge(b, 1000, true) }
func BenchmarkMergeParsedPlatform_10000_Cached(b *testing.B)  { benchPlatformMerge(b, 10000, false) }
func BenchmarkMergeParsedPlatform_10000_Uncached(b *testing.B) {
	benchPlatformMerge(b, 10000, true)
}
