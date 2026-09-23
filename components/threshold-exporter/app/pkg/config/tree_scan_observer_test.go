package config

// tree_scan_observer_test.go — the ScanObserver contract of ScanDirTree
// (#1941: the *configMetrics parameter became this interface when the walker
// moved out of package main). The exporter-side pins against the real
// *configMetrics — including the typed-nil conversion — live in
// app/config_tree_scan_test.go; this file pins the contract at the
// interface, where W2's callers will meet it.

import (
	"io"
	"log"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

// recordingObserver counts every ScanObserver call. Its methods run on a
// non-nil receiver only; a nil observer is passed as a nil interface.
type recordingObserver struct {
	durationStarts int
	durationStops  int
	lastScan       []time.Time
	parseFailures  []string
}

func (r *recordingObserver) ObserveScanElapsed(d time.Duration) {
	r.durationStarts++
	if d >= 0 {
		r.durationStops++
	}
}
func (r *recordingObserver) SetLastScanComplete(t time.Time) { r.lastScan = append(r.lastScan, t) }
func (r *recordingObserver) IncParseFailure(b string)        { r.parseFailures = append(r.parseFailures, b) }

func TestScanDirTree_ObserverContract(t *testing.T) {
	t.Parallel()
	quiet := log.New(io.Discard, "", 0)

	t.Run("clean scan: duration once, last-scan stamped once, parse failure by basename", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		parityWrite(t, filepath.Join(root, "ok.yaml"), "tenants:\n  t1: {}\n")
		parityWrite(t, filepath.Join(root, "sub", "broken.yaml"), "tenants: [unclosed\n")
		obs := &recordingObserver{}
		scan, err := ScanDirTree(root, nil, obs, quiet)
		if err != nil {
			t.Fatal(err)
		}
		if obs.durationStarts != 1 || obs.durationStops != 1 {
			t.Errorf("scan duration observed start=%d stop=%d, want 1/1", obs.durationStarts, obs.durationStops)
		}
		if len(obs.lastScan) != 1 {
			t.Errorf("last-scan-complete stamped %d times on a clean scan, want 1", len(obs.lastScan))
		}
		if want := []string{"broken.yaml"}; !reflect.DeepEqual(obs.parseFailures, want) {
			t.Errorf("parse failures = %v, want %v (basename, not path)", obs.parseFailures, want)
		}
		if f := scan.Files["sub/broken.yaml"]; f == nil || !f.ParseFailed {
			t.Errorf("broken file must stay hashed and be marked ParseFailed: %+v", f)
		}
	})

	t.Run("error return: duration observed, last-scan NOT stamped", func(t *testing.T) {
		t.Parallel()
		obs := &recordingObserver{}
		if _, err := ScanDirTree(filepath.Join(t.TempDir(), "missing"), nil, obs, quiet); err == nil {
			t.Fatal("missing root must be an error")
		}
		if obs.durationStarts != 1 || obs.durationStops != 1 {
			t.Errorf("duration must be observed on error returns too: start=%d stop=%d", obs.durationStarts, obs.durationStops)
		}
		if len(obs.lastScan) != 0 {
			t.Errorf("last-scan-complete stamped on an error return")
		}
	})

	t.Run("duplicate-tenant conflict: duration observed, last-scan NOT stamped", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tx: {}\n")
		parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tx: {}\n")
		obs := &recordingObserver{}
		scan, err := ScanDirTree(root, nil, obs, quiet)
		if err != nil || scan.Conflict == nil {
			t.Fatalf("want a conflict on the scan and no error, got err=%v conflict=%v", err, scan)
		}
		if obs.durationStarts != 1 || obs.durationStops != 1 {
			t.Errorf("duration start=%d stop=%d, want 1/1", obs.durationStarts, obs.durationStops)
		}
		if len(obs.lastScan) != 0 {
			t.Errorf("a rejected (conflicting) tree must not look like a completed scan")
		}
	})

	t.Run("nil observer: no panic, same products", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		parityWrite(t, filepath.Join(root, "ok.yaml"), "tenants:\n  t1: {}\n")
		parityWrite(t, filepath.Join(root, "broken.yaml"), "tenants: [unclosed\n")
		scan, err := ScanDirTree(root, nil, nil, quiet)
		if err != nil {
			t.Fatal(err)
		}
		if got := len(scan.Tenants); got != 1 {
			t.Errorf("tenants = %v, want t1 only", scan.Tenants)
		}
	})
}
