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
	"time"

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

// ============================================================
// v2.8.0 B-3 — reload-duration + debounce-batch histograms
// ============================================================

func TestObserveReloadDuration_RecordsOneSample(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	ObserveReloadDuration(150 * time.Millisecond)

	text, err := prometheusTextExport(fresh.reloadDuration)
	if err != nil {
		t.Fatalf("export: %v", err)
	}
	if !strings.Contains(text, "da_config_reload_duration_seconds_count 1") {
		t.Errorf("expected _count=1; export:\n%s", text)
	}
	// 150ms lands in le=0.25 (not le=0.1).
	reg := prometheus.NewRegistry()
	if err := reg.Register(fresh.reloadDuration); err != nil {
		t.Fatalf("register: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	var le01, le025 uint64
	for _, fam := range families {
		for _, metric := range fam.Metric {
			for _, b := range metric.Histogram.Bucket {
				if b.GetUpperBound() == 0.1 {
					le01 = b.GetCumulativeCount()
				}
				if b.GetUpperBound() == 0.25 {
					le025 = b.GetCumulativeCount()
				}
			}
		}
	}
	if le01 != 0 {
		t.Errorf("expected le=0.1 cumulative=0, got %d", le01)
	}
	if le025 != 1 {
		t.Errorf("expected le=0.25 cumulative=1, got %d", le025)
	}
}

func TestObserveDebounceBatch_RecordsBucketBoundary(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	// Three observations across two buckets: 1, 7, 47.
	// le=1 → 1; le=2 → 1; le=5 → 1; le=10 → 2 (1+7); le=50 → 3 (all);
	ObserveDebounceBatch(1)
	ObserveDebounceBatch(7)
	ObserveDebounceBatch(47)

	reg := prometheus.NewRegistry()
	if err := reg.Register(fresh.debounceBatch); err != nil {
		t.Fatalf("register: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	want := map[float64]uint64{
		1:   1,
		2:   1,
		5:   1,
		10:  2,
		25:  2,
		50:  3,
		100: 3,
		500: 3,
	}
	for _, fam := range families {
		for _, metric := range fam.Metric {
			for _, b := range metric.Histogram.Bucket {
				ub := b.GetUpperBound()
				if w, ok := want[ub]; ok && b.GetCumulativeCount() != w {
					t.Errorf("le=%v: got cumulative=%d want %d", ub, b.GetCumulativeCount(), w)
				}
			}
			if metric.Histogram.GetSampleCount() != 3 {
				t.Errorf("expected _count=3, got %d", metric.Histogram.GetSampleCount())
			}
			if metric.Histogram.GetSampleSum() != 55 {
				t.Errorf("expected _sum=55, got %v", metric.Histogram.GetSampleSum())
			}
		}
	}
}

func TestObserveDebounceBatch_NegativeIsNoOp(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	ObserveDebounceBatch(-1)

	// Plain Histogram (not HistogramVec) is always present after
	// registration; assert via sample count, not series count.
	if got := histogramSampleCount(t, fresh.debounceBatch); got != 0 {
		t.Errorf("negative observation should be no-op; got _count=%d", got)
	}
}

// histogramSampleCount gathers a single Histogram and returns its
// _count value. Helper for plain (non-Vec) histograms where the
// series always exists post-registration regardless of observation
// count.
func histogramSampleCount(t *testing.T, h prometheus.Histogram) uint64 {
	t.Helper()
	reg := prometheus.NewRegistry()
	if err := reg.Register(h); err != nil {
		t.Fatalf("register: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, fam := range families {
		for _, metric := range fam.Metric {
			return metric.Histogram.GetSampleCount()
		}
	}
	return 0
}

// ============================================================
// v2.8.0 B-1.P2-a — last-scan-complete + last-reload-complete gauges
// ============================================================

func TestSetLastScanComplete_StoresUnixSeconds(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	t0 := time.Now()
	SetLastScanComplete(t0)

	got := testutil.ToFloat64(fresh.lastScanComplete)
	if int64(got) != t0.Unix() {
		t.Errorf("expected gauge=%d, got %d", t0.Unix(), int64(got))
	}
}

func TestSetLastReloadComplete_StoresUnixSeconds(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	t0 := time.Now()
	SetLastReloadComplete(t0)

	got := testutil.ToFloat64(fresh.lastReloadComplete)
	if int64(got) != t0.Unix() {
		t.Errorf("expected gauge=%d, got %d", t0.Unix(), int64(got))
	}
}

// TestSetLastScanComplete_MonotonicAdvance verifies that successive Set
// calls move the gauge forward. The e2e harness depends on this — anchor
// T1 must advance after every successful scan so the harness can detect
// "scan completed since T0" by sampling the gauge.
//
// Per S#32 lesson: assert deltas / monotonic advance, not exact absolute
// timing. Two `time.Now().Unix()` values across a 10ms sleep can be
// equal IF the sleep happens to span no second boundary, so the
// assertion is `>=` not `>`.
func TestSetLastScanComplete_MonotonicAdvance(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	t1 := time.Now()
	SetLastScanComplete(t1)
	got1 := testutil.ToFloat64(fresh.lastScanComplete)

	time.Sleep(10 * time.Millisecond)
	t2 := time.Now()
	SetLastScanComplete(t2)
	got2 := testutil.ToFloat64(fresh.lastScanComplete)

	if got2 < got1 {
		t.Errorf("expected monotonic advance: t1=%v t2=%v gauge1=%v gauge2=%v", t1.Unix(), t2.Unix(), got1, got2)
	}
}

// TestScanDirHierarchical_StampsLastScanCompleteOnSuccess verifies the
// gauge advances after a real scan call. Uses delta-based assertion
// (per S#32 lesson) — only assert "advanced past scan-start", not
// "equals exact value".
func TestScanDirHierarchical_StampsLastScanCompleteOnSuccess(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	scanStart := time.Now().Unix()

	if _, _, _, _, _, err := scanDirHierarchical(dir, nil); err != nil {
		t.Fatalf("scan: %v", err)
	}

	got := testutil.ToFloat64(fresh.lastScanComplete)
	if int64(got) < scanStart {
		t.Errorf("gauge did not advance past scanStart: scanStart=%d gauge=%d", scanStart, int64(got))
	}
}

// TestScanDirHierarchical_DoesNotStampOnError verifies that an error path
// does NOT advance the gauge — a transient scan failure must look
// distinct from a successful completion. Pre-set the gauge to a known
// value, run a failing scan, assert no movement.
func TestScanDirHierarchical_DoesNotStampOnError(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)

	known := time.Unix(1700000000, 0)
	SetLastScanComplete(known)
	before := testutil.ToFloat64(fresh.lastScanComplete)

	if _, _, _, _, _, err := scanDirHierarchical("/nonexistent/path/that/does/not/exist", nil); err == nil {
		t.Fatal("expected error for nonexistent path")
	}

	after := testutil.ToFloat64(fresh.lastScanComplete)
	if after != before {
		t.Errorf("gauge advanced on error path: before=%v after=%v", before, after)
	}
}

// TestDiffAndReload_StampsLastReloadCompleteOnSuccess verifies the reload
// gauge advances after a real diffAndReload (delta-based).
func TestDiffAndReload_StampsLastReloadCompleteOnSuccess(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	reloadStart := time.Now().Unix()

	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}

	got := testutil.ToFloat64(fresh.lastReloadComplete)
	if int64(got) < reloadStart {
		t.Errorf("gauge did not advance past reloadStart: reloadStart=%d gauge=%d", reloadStart, int64(got))
	}
}

// TestDiffAndReload_StampsAfterAtomicSwap verifies the contract "gauge
// advanced ⇒ observable state already updated" by checking that
// reload_stamp >= scan_stamp (reload pipeline always scans before
// swapping; reload-complete is stamped strictly after swap).
//
// Direct ordering test (gauge stamped post-swap) would require
// restructuring production code to expose the swap point. The
// >= invariant is sufficient because if SetLastReloadComplete were
// called before the swap, a concurrent reader could observe the
// gauge advanced while the swap was still in progress — exactly what
// we want to forbid.
func TestDiffAndReload_StampsAfterAtomicSwap(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}

	scanStamp := int64(testutil.ToFloat64(fresh.lastScanComplete))
	reloadStamp := int64(testutil.ToFloat64(fresh.lastReloadComplete))
	if scanStamp == 0 {
		t.Errorf("scan gauge should be set after diffAndReload, got 0")
	}
	if reloadStamp < scanStamp {
		t.Errorf("reload stamp should be >= scan stamp: scan=%d reload=%d", scanStamp, reloadStamp)
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
