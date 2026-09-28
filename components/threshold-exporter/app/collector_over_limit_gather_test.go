package main

import (
	"fmt"
	"sync"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"

	"github.com/vencil/threshold-exporter/internal/scrape"
)

// ============================================================
// da_tenant_metrics_over_limit survives every Gather (found in #2266)
// ============================================================
//
// Registry.Gather runs every registered Collector on its own goroutine.
// When the gauge was a registered GaugeVec that ThresholdCollector.Collect
// Reset()+Set() on each scrape, the GaugeVec's own Collect could run
// before, during or after that Reset+Set, so a scrape could carry no
// series or only some tenants. Found while dumping /metrics for every
// repo conf.d tree (#2266): the family went missing from a different set
// of trees on each run.
//
// These tests gather a registry wired by scrape.Register exactly as
// MetricsHandler wires it, many times, and require the full family every time.

const overLimitGatherRounds = 2000

// overLimitGatherFixture returns a config with over-limit and compliant
// tenants, and the magnitude each one must report.
func overLimitGatherFixture() (*ThresholdConfig, map[string]float64) {
	defs := map[string]float64{"metric_a": 1, "metric_b": 2, "metric_c": 3}
	tenants := map[string]map[string]ScheduledValue{}
	want := map[string]float64{}
	for i := 0; i < 20; i++ {
		over := fmt.Sprintf("tenant-over-%02d", i)
		tenants[over] = map[string]ScheduledValue{}
		want[over] = 1 // 3 produced against a cap of 2
		compliant := fmt.Sprintf("tenant-compliant-%02d", i)
		tenants[compliant] = disableAllForCollectorTest(defs)
		want[compliant] = 0
	}
	return &ThresholdConfig{Defaults: defs, Tenants: tenants, MaxMetricsPerTenant: 2}, want
}

// overLimitGatherRegistry is MetricsHandler's registry over cfg, with a
// fresh configMetrics in place of the process-wide singleton.
func overLimitGatherRegistry(cfg *ThresholdConfig) *prometheus.Registry {
	manager := newTestManager(cfg)
	fresh := newConfigMetrics()
	manager.SetMetrics(fresh)
	reg := prometheus.NewRegistry()
	scrape.Register(reg, NewThresholdCollector(manager), fresh.set, collectors.NewGoCollector())
	return reg
}

// checkOverLimitFamily reports how one gathered snapshot's
// da_tenant_metrics_over_limit family differs from want; "" means it matches.
func checkOverLimitFamily(reg prometheus.Gatherer, want map[string]float64) string {
	mfs, err := reg.Gather()
	if err != nil {
		return fmt.Sprintf("Gather error: %v", err)
	}
	for _, mf := range mfs {
		if mf.GetName() != "da_tenant_metrics_over_limit" {
			continue
		}
		got := map[string]float64{}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "tenant" {
					got[lp.GetValue()] = m.GetGauge().GetValue()
				}
			}
		}
		if len(got) != len(want) {
			return fmt.Sprintf("family has %d tenants, want %d", len(got), len(want))
		}
		for tenant, v := range want {
			if g, ok := got[tenant]; !ok || g != v {
				return fmt.Sprintf("tenant %q = %v (present=%v), want %v", tenant, g, ok, v)
			}
		}
		return ""
	}
	return "family missing"
}

func TestMetricsRegistry_OverLimitFamilyCompleteOnEveryGather(t *testing.T) {
	t.Parallel()
	cfg, want := overLimitGatherFixture()
	reg := overLimitGatherRegistry(cfg)

	failed, first := 0, ""
	for i := 0; i < overLimitGatherRounds; i++ {
		if msg := checkOverLimitFamily(reg, want); msg != "" {
			if failed == 0 {
				first = msg
			}
			failed++
		}
	}
	if failed > 0 {
		t.Errorf("da_tenant_metrics_over_limit wrong on %d of %d gathers; first: %s", failed, overLimitGatherRounds, first)
	}
}

// Two scrapes can overlap (Prometheus HA pair, a human curl). Each must
// still see the whole family: nothing one scrape does may reach into
// what the other emits.
func TestMetricsRegistry_OverLimitFamilyCompleteUnderConcurrentGathers(t *testing.T) {
	t.Parallel()
	cfg, want := overLimitGatherFixture()
	reg := overLimitGatherRegistry(cfg)

	var (
		mu     sync.Mutex
		failed int
		first  string
		wg     sync.WaitGroup
	)
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < overLimitGatherRounds/4; i++ {
				if msg := checkOverLimitFamily(reg, want); msg != "" {
					mu.Lock()
					if failed == 0 {
						first = msg
					}
					failed++
					mu.Unlock()
				}
			}
		}()
	}
	wg.Wait()
	if failed > 0 {
		t.Errorf("da_tenant_metrics_over_limit wrong on %d of %d concurrent gathers; first: %s", failed, overLimitGatherRounds, first)
	}
}
