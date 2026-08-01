package main

// The end-to-end half of the declared-key emission contract (#1189 / TRK-337).
//
// Why this file exists separately from the unit pins in pkg/config: a row
// COUNT cannot distinguish "we correctly emitted one row" from "we emitted two
// rows that Prometheus will reject at scrape time". The failure mode of a
// duplicated label set is not a wrong number — it is `Gather` returning an
// error, which makes the whole /metrics response a 500 and takes every metric
// family down with it, for every tenant, not just the misconfigured one. Only
// a real registry can assert that, so this asserts against a real registry.

import (
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// gatherOrFail registers the collector in a pedantic registry — the one that
// enforces the duplicate-metric contract — and returns the exposition text.
func gatherOrFail(t *testing.T, cfg *ThresholdConfig) string {
	t.Helper()
	reg := prometheus.NewPedanticRegistry()
	if err := reg.Register(NewThresholdCollector(newTestManager(cfg))); err != nil {
		t.Fatalf("register: %v", err)
	}
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather failed — a duplicate label set makes the ENTIRE /metrics "+
			"response a 500, for every tenant: %v", err)
	}
	var sb strings.Builder
	for _, mf := range mfs {
		for _, m := range mf.GetMetric() {
			sb.WriteString(mf.GetName())
			sb.WriteByte('{')
			for _, l := range m.GetLabel() {
				sb.WriteString(l.GetName() + "=" + l.GetValue() + ",")
			}
			sb.WriteString("} ")
			if m.GetGauge() != nil {
				sb.WriteString(m.GetGauge().String())
			}
			sb.WriteByte('\n')
		}
	}
	return sb.String()
}

// ⛔ The shape that would take /metrics down. Nothing forbids a key from being
// in both `defaults:` and `optional_overrides:` — the validator explicitly
// accepts that overlap (it is a row in the shared verdict matrix) — so the
// emission loop has to skip what resolveBaseRows already owns. If it does not,
// both loops emit {tenant, metric, component, severity} identically.
func TestGather_DeclaredKeyAlsoInDefaults_DoesNotBreakScrape(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: []string{"mysql_connections"},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"mysql_connections": SV("200")},
		},
	}

	out := gatherOrFail(t, cfg)

	if n := strings.Count(out, "metric=connections,"); n != 1 {
		t.Fatalf("want exactly 1 series for the overlapping key, got %d:\n%s", n, out)
	}
	if !strings.Contains(out, "tenant=t-1") {
		t.Errorf("tenant label missing:\n%s", out)
	}
}

// Same hazard, second shape: a dimensional override on a declared base has
// been emitting since PR-B (resolveDimensionalRows never consults defaults).
func TestGather_DimensionalOnDeclaredBase_DoesNotBreakScrape(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		OptionalOverrides: []string{"oracle_wait_time_rate"},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {`oracle_wait_time_rate{db="ORCL"}`: SV("50")},
		},
	}

	out := gatherOrFail(t, cfg)

	if n := strings.Count(out, "metric=wait_time_rate,"); n != 1 {
		t.Fatalf("want exactly 1 series, got %d:\n%s", n, out)
	}
	if !strings.Contains(out, "db=ORCL") {
		t.Errorf("dimensional label lost:\n%s", out)
	}
}

// The positive end-to-end case: a declared key the tenant set reaches the
// scrape surface as a real series.
func TestGather_DeclaredKeyReachesTheScrapeSurface(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: []string{"oracle_wait_time_rate"},
		Tenants: map[string]map[string]ScheduledValue{
			"t-1": {"oracle_wait_time_rate": SV("50")},
			"t-2": {}, // same platform surface, no value → no series
		},
	}

	out := gatherOrFail(t, cfg)

	if n := strings.Count(out, "metric=wait_time_rate,"); n != 1 {
		t.Fatalf("exactly the tenant that set it should get a series, got %d:\n%s", n, out)
	}
	if !strings.Contains(out, "component=oracle,") {
		t.Errorf("component label wrong:\n%s", out)
	}
	// t-2 shares the declared surface and set nothing — the dormant state must
	// stay off the wire, or "declared" would just be "armed" with extra steps.
	if strings.Contains(out, "tenant=t-2,") && strings.Contains(out, "metric=wait_time_rate,") {
		lines := strings.Split(out, "\n")
		for _, l := range lines {
			if strings.Contains(l, "tenant=t-2,") && strings.Contains(l, "wait_time_rate") {
				t.Errorf("a tenant that set nothing must not get a row: %s", l)
			}
		}
	}
}
