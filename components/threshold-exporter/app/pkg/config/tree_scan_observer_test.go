package config

// tree_scan_observer_test.go — the ScanObserver contract of ScanDirTree
// (#1941: the *configMetrics parameter became this interface when the walker
// moved out of package main). The exporter-side pins against the real
// *configMetrics — including both typed-nil defences — live in
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

// recordingObserver records every ScanObserver call.
type recordingObserver struct {
	elapsedCalls  int
	elapsed       []time.Duration
	lastScan      []time.Time
	parseFailures []string
}

func (r *recordingObserver) ObserveScanElapsed(d time.Duration) {
	r.elapsedCalls++
	r.elapsed = append(r.elapsed, d)
}
func (r *recordingObserver) SetLastScanComplete(t time.Time) { r.lastScan = append(r.lastScan, t) }
func (r *recordingObserver) IncParseFailure(b string)        { r.parseFailures = append(r.parseFailures, b) }

// slowWriter makes every log line cost at least `delay`. The walk logs the
// parse failure of a broken file from INSIDE the walk, so a scan over a tree
// holding one is guaranteed to take at least `delay` between ScanDirTree's
// entry and its return — which is what lets the elapsed assertion below
// prove the recorded duration spans the walk, instead of only proving a
// call happened.
type slowWriter struct{ delay time.Duration }

func (w slowWriter) Write(p []byte) (int, error) {
	time.Sleep(w.delay)
	return len(p), nil
}

func TestScanDirTree_ObserverContract(t *testing.T) {
	t.Parallel()
	quiet := log.New(io.Discard, "", 0)

	t.Run("clean scan: elapsed once and spanning the walk, last-scan once, parse failure by basename", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		parityWrite(t, filepath.Join(root, "ok.yaml"), "tenants:\n  t1: {}\n")
		parityWrite(t, filepath.Join(root, "sub", "broken.yaml"), "tenants: [unclosed\n")
		const delay = 30 * time.Millisecond
		obs := &recordingObserver{}
		scan, err := ScanDirTree(root, nil, obs, log.New(slowWriter{delay: delay}, "", 0))
		if err != nil {
			t.Fatal(err)
		}
		if obs.elapsedCalls != 1 {
			t.Fatalf("scan duration observed %d times, want exactly 1", obs.elapsedCalls)
		}
		if obs.elapsed[0] < delay {
			t.Errorf("recorded elapsed %v < %v, the time the walk itself spent logging the parse failure: "+
				"the duration does not span the walk", obs.elapsed[0], delay)
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

	t.Run("error return: elapsed observed, last-scan NOT stamped", func(t *testing.T) {
		t.Parallel()
		obs := &recordingObserver{}
		if _, err := ScanDirTree(filepath.Join(t.TempDir(), "missing"), nil, obs, quiet); err == nil {
			t.Fatal("missing root must be an error")
		}
		if obs.elapsedCalls != 1 {
			t.Errorf("duration must be observed on error returns too: %d calls, want 1", obs.elapsedCalls)
		}
		if len(obs.lastScan) != 0 {
			t.Errorf("last-scan-complete stamped on an error return")
		}
	})

	t.Run("duplicate-tenant conflict: elapsed observed, last-scan NOT stamped", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		parityWrite(t, filepath.Join(root, "a.yaml"), "tenants:\n  tx: {}\n")
		parityWrite(t, filepath.Join(root, "b.yaml"), "tenants:\n  tx: {}\n")
		obs := &recordingObserver{}
		scan, err := ScanDirTree(root, nil, obs, quiet)
		if err != nil || scan.Conflict == nil {
			t.Fatalf("want a conflict on the scan and no error, got err=%v scan=%v", err, scan)
		}
		if obs.elapsedCalls != 1 {
			t.Errorf("duration observed %d times, want 1", obs.elapsedCalls)
		}
		if len(obs.lastScan) != 0 {
			t.Errorf("a rejected (conflicting) tree must not look like a completed scan")
		}
	})

	t.Run("nil and non-nil observer yield the same products", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		parityWrite(t, filepath.Join(root, "ok.yaml"), "tenants:\n  t1: {}\n")
		parityWrite(t, filepath.Join(root, "sub", "_defaults.yaml"), "defaults:\n  cpu: 1\n")
		parityWrite(t, filepath.Join(root, "sub", "t2.yaml"), "tenants:\n  t2: {}\n")
		parityWrite(t, filepath.Join(root, "broken.yaml"), "tenants: [unclosed\n")
		withNil, err := ScanDirTree(root, nil, nil, quiet)
		if err != nil {
			t.Fatal(err)
		}
		withObs, err := ScanDirTree(root, nil, &recordingObserver{}, quiet)
		if err != nil {
			t.Fatal(err)
		}
		if withNil.Composite != withObs.Composite {
			t.Errorf("Composite differs: nil observer %s, recording observer %s", withNil.Composite, withObs.Composite)
		}
		if !reflect.DeepEqual(withNil.Tenants, withObs.Tenants) {
			t.Errorf("Tenants differ: nil observer %v, recording observer %v", withNil.Tenants, withObs.Tenants)
		}
		if len(withNil.Tenants) != 2 {
			t.Errorf("harness: want tenants t1 and t2, got %v", withNil.Tenants)
		}
	})
}
