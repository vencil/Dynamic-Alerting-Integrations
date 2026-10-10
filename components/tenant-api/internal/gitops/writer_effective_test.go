package gitops

// #1977: Writer.ResolveEffective — /effective's resolve on the Writer's
// prior. Pinned here: it answers what a cold resolve answers, a write that
// returned is in the next answer (same-size rewrites inside one mtime tick
// included), it never joins a read walk in flight, a stuck walk fails it
// without failing writes, and a file that no longer hashes to the prior's
// carried hash is answered from a cold walk instead.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const effDefaults = "defaults:\n  mysql_connections: 80\n"

func effTenant(id, v string) string {
	return "tenants:\n  " + id + ":\n    mysql_connections: \"" + v + "\"\n"
}

// effJSON is the comparable form of a resolve: the body, or the error text.
func effJSON(ec *cfg.EffectiveConfig, err error) string {
	if err != nil {
		return "error: " + err.Error()
	}
	b, merr := json.Marshal(ec)
	if merr != nil {
		return "marshal: " + merr.Error()
	}
	return string(b)
}

func effValue(t *testing.T, w *Writer, id string) any {
	t.Helper()
	ec, err := w.ResolveEffective(id)
	if err != nil {
		t.Fatalf("ResolveEffective(%s): %v", id, err)
	}
	return ec.EffectiveConfig["mysql_connections"]
}

// The answer on the prior is the cold answer, and the second call reuses the
// prior the first published (every file but none on the fast-path would
// mean the endpoint still walks cold).
func TestResolveEffective_WarmEqualsColdAndReusesThePrior(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), effDefaults)
	writeFile(t, filepath.Join(dir, "sub", "_defaults.yaml"), "defaults:\n  mysql_connections: 70\n  only_sub: 3\n")
	writeFile(t, filepath.Join(dir, "sub", "t1.yaml"), effTenant("t1", "61"))
	writeFile(t, filepath.Join(dir, "t2.yaml"), effTenant("t2", "62"))
	ageTree(t, dir)
	w := &Writer{configDir: dir, treeScanTimeout: time.Minute}
	for _, id := range []string{"t1", "t2", "t-absent"} {
		want := effJSON(cfg.ResolveEffective(dir, id))
		for i := 0; i < 2; i++ {
			if got := effJSON(w.ResolveEffective(id)); got != want {
				t.Errorf("%s call %d:\n got  %s\n want %s", id, i+1, got, want)
			}
		}
	}
	prior := w.treePrior.Load()
	if prior == nil {
		t.Fatal("no prior published")
	}
	for k, f := range prior.Files {
		if !f.Reused {
			t.Errorf("%s: not on the fast-path on a quiet tree", k)
		}
	}
}

// A write that returned is in the next answer — also when it rewrote the
// file with the same size within the same second, and back again.
func TestResolveEffective_SeesAWriteThatReturned(t *testing.T) {
	t.Parallel()
	dir := seedTreeRepo(t, map[string]string{"_defaults.yaml": effDefaults, "t1.yaml": effTenant("t1", "61")})
	ageTree(t, dir)
	w := NewWriter(dir, dir)
	if got := effValue(t, w, "t1"); got != "61" {
		t.Fatalf("before the write: %v", got)
	}
	for _, v := range []string{"62", "61", "63"} {
		if _, err := w.Write(context.Background(), "t1", "op@example.com", effTenant("t1", v)); err != nil {
			t.Fatalf("write %s: %v", v, err)
		}
		if got := effValue(t, w, "t1"); got != v {
			t.Fatalf("after writing %s: GET answered %v", v, got)
		}
	}
}

// ⛔ Never a joined walk: with a read walk held open in flight, a call
// starts its own walk (and answers from it) rather than waiting for the held
// one — whose tree may predate a write that has since returned.
func TestResolveEffective_DoesNotJoinAReadWalkInFlight(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "t1.yaml"), effTenant("t1", "61"))
	w := &Writer{configDir: dir, treeScanTimeout: time.Minute}
	walks, entered, release := holdFirstWalk(t, w)
	joined := make(chan struct{}, 1)
	w.onReadScanJoin = func() { joined <- struct{}{} }
	go func() { _, _ = w.scanTreeForRead() }()
	awaitSignal(t, entered, "the read walk to start")

	answer := make(chan string, 1)
	go func() { answer <- effJSON(w.ResolveEffective("t1")) }()
	select {
	case got := <-answer:
		if want := effJSON(cfg.ResolveEffective(dir, "t1")); got != want {
			t.Fatalf("answer:\n got  %s\n want %s", got, want)
		}
	case <-joined:
		t.Fatal("ResolveEffective joined the read walk in flight")
	case <-time.After(10 * time.Second):
		t.Fatal("ResolveEffective waited on the read walk in flight")
	}
	if n := walks.Load(); n != 2 {
		t.Errorf("walks = %d, want 2 (the held read walk, and the resolve's own)", n)
	}
	release()
}

// A walk this endpoint left blocked fails it — closed, past the bound — and
// later calls at once through the read breaker; a write meanwhile goes
// through on its own walk and breaker.
func TestResolveEffective_StuckWalkFailsReadsNotWrites(t *testing.T) {
	t.Parallel()
	dir := seedTreeRepo(t, map[string]string{"_defaults.yaml": effDefaults, "t1.yaml": effTenant("t1", "61")})
	w := NewWriter(dir, dir)
	w.treeScanTimeout = 100 * time.Millisecond
	walks, _, release := holdFirstWalk(t, w)

	if _, err := w.ResolveEffective("t1"); !errors.Is(err, errTreeScanTimeout) {
		t.Fatalf("resolve over a held walk: err = %v, want the walk timeout", err)
	}
	if _, err := w.ResolveEffective("t1"); !errors.Is(err, errTreeScanStuck) {
		t.Fatalf("resolve while the walk is stuck: err = %v, want the breaker", err)
	}
	if n := w.stuckReadTreeScans.Load(); n != 1 {
		t.Errorf("stuckReadTreeScans = %d, want 1", n)
	}
	w.treeScanTimeout = time.Minute
	if _, err := w.Write(context.Background(), "t1", "op@example.com", effTenant("t1", "62")); err != nil {
		t.Fatalf("write while an /effective walk is stuck: %v", err)
	}
	if n := w.stuckTreeScans.Load(); n != 0 {
		t.Errorf("stuckTreeScans = %d: the /effective walk was counted against writes", n)
	}
	if n := walks.Load(); n != 2 {
		t.Errorf("walks = %d, want 2 (the held walk, then the write's own)", n)
	}
	release()
	waitDrained(t, &w.stuckReadTreeScans)
	if got := effValue(t, w, "t1"); got != "62" {
		t.Errorf("after the walk drained: %v", got)
	}
}

// The walker's known gap, met head on: the prior carries a hash for a file
// whose bytes changed without moving its stat. The resolve does not answer
// from those bytes against the stale attribution; it walks again cold —
// answering the file's current content — and that walk becomes the prior.
func TestResolveEffective_StalePriorHashWalksCold(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	p := filepath.Join(dir, "t1.yaml")
	writeFile(t, p, effTenant("t1", "61"))
	ageTree(t, dir)
	w := &Writer{configDir: dir, treeScanTimeout: time.Minute}
	if got := effValue(t, w, "t1"); got != "61" {
		t.Fatalf("first answer = %v", got)
	}
	// Same size, same mtime: invisible to the fast-path.
	st, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	writeFile(t, p, effTenant("t1", "62"))
	if err := os.Chtimes(p, st.ModTime(), st.ModTime()); err != nil {
		t.Fatal(err)
	}
	if got := effValue(t, w, "t1"); got != "62" {
		t.Fatalf("after a stat-preserving rewrite: %v, want 62", got)
	}
	if f := w.treePrior.Load().Files["t1.yaml"]; f.Reused {
		t.Error("the prior is still the fast-path walk; the cold walk did not replace it")
	}
}

// A tree that changes between every walk and the resolve's reads fails
// closed with cfg.ErrScanStale after effectiveResolveAttempts walks — the
// retry is bounded, not a loop that chases a tree rewritten faster than it
// can walk.
func TestResolveEffective_StaleRetriesAreBounded(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	p := filepath.Join(dir, "t1.yaml")
	writeFile(t, p, effTenant("t1", "61"))
	w := &Writer{configDir: dir, treeScanTimeout: time.Minute}
	var walks atomic.Int32
	w.onTreeScan = func() { walks.Add(1) }
	n := 0
	w.onEffectiveWalked = func() { // a different body after every walk
		n++
		// Not writeFile: this runs off the test goroutine, where t.Fatal
		// must not be called. A failed write leaves the bytes the walk
		// hashed, and the assertions below fail.
		_ = os.WriteFile(p, []byte(effTenant("t1", fmt.Sprintf("%02d", n%100))), 0o644)
	}
	done := make(chan error, 1)
	go func() {
		_, err := w.ResolveEffective("t1")
		done <- err
	}()
	select {
	case err := <-done:
		if !errors.Is(err, cfg.ErrScanStale) {
			t.Fatalf("err = %v, want cfg.ErrScanStale", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("ResolveEffective kept retrying a tree that changes after every walk")
	}
	if got := walks.Load(); got != effectiveResolveAttempts {
		t.Errorf("walks = %d, want %d", got, effectiveResolveAttempts)
	}
}
