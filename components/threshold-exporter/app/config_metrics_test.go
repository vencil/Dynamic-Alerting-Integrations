package main

// ============================================================
// Phase 4 metrics tests — v2.7.0 B-2
// ============================================================
//
// These tests exercise the three new observability metrics added for
// hierarchical reload. They use an isolated metrics instance + registry
// per test so counters don't leak between runs (especially problematic
// when running with `go test -count=3 -race`).

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// withIsolatedMetrics swaps in a fresh metrics instance for the duration
// of t, restoring the previous instance on cleanup. The returned registry
// has the fresh metrics pre-registered so testutil.CollectAndCount works.
func withIsolatedMetrics(t *testing.T) (*configMetrics, *prometheus.Registry) {
	t.Helper()
	prev := getConfigMetrics()
	fresh := newConfigMetrics()
	setConfigMetrics(fresh)
	reg := prometheus.NewRegistry()
	registerConfigMetrics(reg, fresh)
	t.Cleanup(func() { setConfigMetrics(prev) })
	return fresh, reg
}

func TestObserveScanDuration_RecordsOneSample(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	done := ObserveScanDuration()
	// Minimum sleep to avoid a zero-duration sample that could confuse
	// bucket boundary assertions. 1ms is the smallest bucket.
	done()

	if got := testutil.CollectAndCount(fresh.scanDuration); got != 1 {
		t.Errorf("expected 1 scan duration observation, got %d", got)
	}
}

func TestScanDirHierarchical_IncrementsScanDuration(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	if _, _, _, _, _, err := scanDirHierarchical(dir, nil); err != nil {
		t.Fatalf("scan: %v", err)
	}
	if _, _, _, _, _, err := scanDirHierarchical(dir, nil); err != nil {
		t.Fatalf("scan: %v", err)
	}

	if got := testutil.CollectAndCount(fresh.scanDuration); got != 1 {
		// Histogram is 1 metric; its sample count is what we want — but
		// CollectAndCount returns the number of *metric families*, not
		// samples. Assert via the text export below for sample count.
		t.Errorf("expected 1 metric family, got %d", got)
	}

	// Two scans → at least two samples. Use the _count sub-sample via
	// testutil.ToFloat64 on a sum-only wrapper isn't exposed, so grep
	// the text export.
	text, err := prometheusTextExport(fresh.scanDuration)
	if err != nil {
		t.Fatalf("export: %v", err)
	}
	if !strings.Contains(text, "da_config_scan_duration_seconds_count 2") {
		t.Errorf("expected _count = 2 after 2 scans; export:\n%s", text)
	}
}

func TestIncReloadTrigger_AccumulatesPerReason(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	IncReloadTrigger(ReloadReasonSource)
	IncReloadTrigger(ReloadReasonSource)
	IncReloadTrigger(ReloadReasonDefaults)
	IncReloadTriggerBy(ReloadReasonNewTenant, 3)
	IncReloadTriggerBy(ReloadReasonDelete, 0) // no-op

	checks := map[string]float64{
		ReloadReasonSource:    2,
		ReloadReasonDefaults:  1,
		ReloadReasonNewTenant: 3,
	}
	for reason, want := range checks {
		if got := testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(reason)); got != want {
			t.Errorf("reason=%q: got %v want %v", reason, got, want)
		}
	}

	// "delete" should be zero (IncReloadTriggerBy(..., 0) is a no-op).
	if got := testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(ReloadReasonDelete)); got != 0 {
		t.Errorf("reason=delete should be 0, got %v", got)
	}
}

func TestIncDefaultsNoop_AccumulatesTotal(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	IncDefaultsNoop()
	IncDefaultsNoop()
	IncDefaultsNoopBy(5)
	IncDefaultsNoopBy(0) // no-op
	IncDefaultsNoopBy(-1) // no-op (defensive)

	if got := testutil.ToFloat64(fresh.defaultsNoop); got != 7 {
		t.Errorf("expected noop=7, got %v", got)
	}
}

// ============================================================
// Issue #61 — blast-radius histogram + shadowed counter
// ============================================================

func TestIncDefaultsShadowed_AccumulatesTotal(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	IncDefaultsShadowed()
	IncDefaultsShadowed()
	IncDefaultsShadowedBy(4)
	IncDefaultsShadowedBy(0)  // no-op
	IncDefaultsShadowedBy(-2) // no-op (defensive)

	if got := testutil.ToFloat64(fresh.defaultsShadowed); got != 6 {
		t.Errorf("expected shadowed=6, got %v", got)
	}
}

func TestObserveBlastRadius_RecordsBucketAndSumCount(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	// One observation: 21 tenants in (defaults, region, applied).
	ObserveBlastRadius("defaults", "region", "applied", 21)

	text, err := prometheusTextExport(fresh.blastRadius)
	if err != nil {
		t.Fatalf("export: %v", err)
	}

	// Sample count = 1 (one Observe call).
	if !strings.Contains(text, "da_config_blast_radius_tenants_affected_count 1") {
		t.Errorf("expected _count=1; export:\n%s", text)
	}
	// Bucket boundary: 21 falls in le=25, not le=5. We can't grep
	// per-label bucket counts from prometheusTextExport (it doesn't
	// emit them), so use testutil.CollectAndCount as a sanity proxy
	// — exact bucket validation lives in
	// TestObserveBlastRadius_BucketBoundary below.
	if got := testutil.CollectAndCount(fresh.blastRadius); got != 1 {
		t.Errorf("expected 1 metric series, got %d", got)
	}
}

func TestObserveBlastRadius_BucketBoundary(t *testing.T) {
	// Verify N=21 lands in bucket le=25 (not le=5) by reading the
	// Histogram's underlying proto via a per-test registry.
	fresh, _ := withIsolatedMetrics(t)
	ObserveBlastRadius("defaults", "region", "applied", 21)

	reg := prometheus.NewRegistry()
	if err := reg.Register(fresh.blastRadius); err != nil {
		t.Fatalf("register: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, fam := range families {
		if fam.GetName() != "da_config_blast_radius_tenants_affected" {
			continue
		}
		for _, metric := range fam.Metric {
			h := metric.Histogram
			if h == nil {
				t.Fatalf("expected Histogram, got %v", metric)
			}
			// Buckets are sorted ascending; locate le=5 and le=25.
			var le5, le25 uint64
			for _, b := range h.Bucket {
				if b.GetUpperBound() == 5 {
					le5 = b.GetCumulativeCount()
				}
				if b.GetUpperBound() == 25 {
					le25 = b.GetCumulativeCount()
				}
			}
			if le5 != 0 {
				t.Errorf("expected le=5 cumulative=0, got %d", le5)
			}
			if le25 != 1 {
				t.Errorf("expected le=25 cumulative=1, got %d", le25)
			}
			if h.GetSampleSum() != 21 {
				t.Errorf("expected _sum=21, got %v", h.GetSampleSum())
			}
		}
	}
}

func TestObserveBlastRadius_ZeroIsNoOp(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	ObserveBlastRadius("defaults", "global", "applied", 0)
	ObserveBlastRadius("defaults", "global", "applied", -5)

	if got := testutil.CollectAndCount(fresh.blastRadius); got != 0 {
		t.Errorf("expected 0 metric series after no-op observes, got %d", got)
	}
}

func TestObserveBlastRadius_DistinctLabelsCreateDistinctSeries(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	ObserveBlastRadius("defaults", "region", "applied", 1)
	ObserveBlastRadius("defaults", "region", "shadowed", 1)
	ObserveBlastRadius("defaults", "region", "cosmetic", 1)
	ObserveBlastRadius("source", "tenant", "applied", 1)

	if got := testutil.CollectAndCount(fresh.blastRadius); got != 4 {
		t.Errorf("expected 4 distinct series, got %d", got)
	}
}

// TestDiffAndReload_EmitsMetricsForSourceChange ensures the full pipeline
// (scan → diff → counter increment) hooks up end-to-end.
func TestDiffAndReload_EmitsMetricsForSourceChange(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("first diff: %v", err)
	}

	// Modify tenant file to trigger a "source" reload.
	writeTestYAML(t, filepath.Join(dir, "team-a", "tenant-a.yaml"), `
tenants:
  tenant-a:
    mysql_connections: "99"
`)

	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("second diff: %v", err)
	}

	if got := testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(ReloadReasonSource)); got < 1 {
		t.Errorf("expected reloadTriggers{reason=source} >= 1, got %v", got)
	}
}

// prometheusTextExport renders a metric's text format for substring
// assertions. Package-local helper — testutil doesn't expose a direct
// text export for a single metric.
func prometheusTextExport(c prometheus.Collector) (string, error) {
	reg := prometheus.NewRegistry()
	if err := reg.Register(c); err != nil {
		return "", err
	}
	families, err := reg.Gather()
	if err != nil {
		return "", err
	}
	var b strings.Builder
	for _, mf := range families {
		b.WriteString(mf.GetName())
		b.WriteByte(' ')
		for _, metric := range mf.Metric {
			// Histogram: write _count via the text-ish shape the test
			// assertions above expect.
			if h := metric.Histogram; h != nil {
				if h.SampleCount != nil {
					b.WriteString(mf.GetName())
					b.WriteString("_count ")
					// Best-effort decimal; no FormatUint import needed.
					b.WriteString(itoa(int64(*h.SampleCount)))
					b.WriteByte('\n')
				}
			}
		}
	}
	return b.String(), nil
}

// itoa is a tiny decimal helper to avoid importing strconv just for this.
func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
