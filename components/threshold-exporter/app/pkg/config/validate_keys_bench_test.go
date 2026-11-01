package config

// validate_keys_bench_test.go — ValidateTenantKeysByTenant on 2000 tenants ×
// 10 threshold keys, all readable (the common write-gate / exporter-load
// case): what the #3267 threshold value finding costs a tree with nothing
// to report.

import (
	"fmt"
	"testing"
)

func BenchmarkValidateTenantKeysByTenant_2000x10(b *testing.B) {
	c := &ThresholdConfig{Defaults: map[string]float64{}, Tenants: map[string]map[string]ScheduledValue{}}
	for k := 0; k < 10; k++ {
		c.Defaults[fmt.Sprintf("metric_%03d", k)] = 50
	}
	for i := 0; i < 2000; i++ {
		keys := make(map[string]ScheduledValue, 10)
		for k := 0; k < 10; k++ {
			keys[fmt.Sprintf("metric_%03d", k)] = ScheduledValue{Default: "70"}
		}
		c.Tenants[fmt.Sprintf("tenant-%04d", i)] = keys
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if kv := c.ValidateTenantKeysByTenant(); len(kv.Tenants) != 2000 {
			b.Fatal("tenants")
		}
	}
}
