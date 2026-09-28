package main

// #2100 — a reload tick whose merged_hash recompute fails to READ a file
// keeps the tenant's last-known-good merged_hash but commits the tick's
// inputs (hashes, graph, platform). Measured before the fix: the next tick
// with nothing changed on disk found no change (detectChange compares the
// committed hashes), ran no reload, and merged_hash stayed on the pre-edit
// merge until some other input moved. The fix marks such a tenant; tickOnce
// retries it on a tick that finds no change (retryFailedMergedHashes), and
// classifyTenant retries it on a tick that reloads.
//
// Every test runs through tickOnce — the production entry (WatchLoop calls
// it per interval), so the detectChange gate is on the path. The
// diffAndReload entry is kept alongside for its (reloaded, noOp) return,
// the attribution a reload reports, and for classifyTenant's retry.
//
// The failure is injected at the recompute's read (reloadMergeRead seam),
// not with chmod: the tree scan carries the unmoved chain file by its mtime
// without reading it, so only the recompute sees the error — the shape the
// chmod-000 probe on the issue measured, without needing a non-root run.
//
// Seams: metrics via freshMetrics + SetMetrics, logger via SetLogger,
// reads via reloadMergeRead. No process-global state, so t.Parallel.

import (
	"bytes"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// mergeRetryFixture is a two-level tree: a root `_defaults.yaml` that also
// carries a platform `tenants:` entry for tx (#2019), a nested
// `team/_defaults.yaml` in tx's chain, and tx's own file.
type mergeRetryFixture struct {
	dir, root, nested, tenant string
	m                         *ConfigManager
	reg                       *prometheus.Registry
	log                       *bytes.Buffer
	entry                     string // "tickOnce" or "diffAndReload"
	failNested                bool   // reloadMergeRead fails the nested chain file
	nestedReads               int    // reloadMergeRead calls for the nested chain file
	injected                  error  // what a failed read returns
}

var mergeRetryEntries = []string{"tickOnce", "diffAndReload"}

const mergeRetryTenant = "tx"

func newMergeRetryFixture(t *testing.T, entry string) *mergeRetryFixture {
	t.Helper()
	dir := t.TempDir()
	f := &mergeRetryFixture{
		dir:      dir,
		entry:    entry,
		root:     filepath.Join(dir, "_defaults.yaml"),
		nested:   filepath.Join(dir, "team", "_defaults.yaml"),
		tenant:   filepath.Join(dir, "team", "tx.yaml"),
		injected: errors.New("injected read failure (#2100)"),
	}
	writeOverlayTree(t, dir, map[string]string{
		"_defaults.yaml":      "defaults:\n  mysql_connections: 80\n  mysql_cpu_usage: 70\ntenants:\n  tx:\n    mysql_cpu_usage: \"1\"\n",
		"team/_defaults.yaml": "defaults:\n  mysql_connections: 81\n",
		"team/tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
	})
	// Old mtimes: this tick's edit is the only file the scan re-reads.
	touchTreeAt(t, dir, time.Now().Add(-time.Hour))

	fresh, reg := freshMetrics(t)
	f.reg = reg
	f.log = &bytes.Buffer{}
	f.m = NewConfigManagerWithDebounce(dir, 0)
	f.m.SetMetrics(fresh)
	f.m.SetLogger(log.New(f.log, "", 0))
	t.Cleanup(f.m.Close)
	// Test goroutine drives every tick (debounce window 0: tickOnce reloads
	// synchronously), so the seam and the fields it touches are
	// single-goroutine.
	f.m.reloadMergeRead = func(p string) ([]byte, error) {
		if p == f.nested {
			f.nestedReads++
			if f.failNested {
				return nil, f.injected
			}
		}
		return os.ReadFile(p)
	}
	if err := f.m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return f
}

// tick runs one tick through f.entry. counted reports whether (reloaded,
// noOp) are known — only diffAndReload returns them.
func (f *mergeRetryFixture) tick(t *testing.T) (reloaded, noOp int, counted bool) {
	t.Helper()
	if f.entry == "tickOnce" {
		f.m.tickOnce()
		return 0, 0, false
	}
	r, n, err := f.m.diffAndReload()
	if err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	return r, n, true
}

// edit applies the variant's input change: "chain" edits tx's own file,
// "platform" edits tx's entry in the root platform file's `tenants:`.
func (f *mergeRetryFixture) edit(t *testing.T, variant string) {
	t.Helper()
	switch variant {
	case "chain":
		writeTestYAML(t, f.tenant, "tenants:\n  tx:\n    mysql_connections: \"99\"\n")
	case "platform":
		writeTestYAML(t, f.root, "defaults:\n  mysql_connections: 80\n  mysql_cpu_usage: 70\ntenants:\n  tx:\n    mysql_cpu_usage: \"2\"\n")
	default:
		t.Fatalf("unknown variant %q", variant)
	}
}

// effectiveHash is what /effective (config.ResolveEffective) computes for
// tx from the tree as it stands — the value the cached merged_hash owes.
func (f *mergeRetryFixture) effectiveHash(t *testing.T) string {
	t.Helper()
	pe, err := config.ResolveEffective(f.dir, mergeRetryTenant)
	if err != nil {
		t.Fatalf("ResolveEffective: %v", err)
	}
	return pe.MergedHash
}

// attribution is the reload-attribution metrics as text: reload triggers,
// the defaults no-op / shadowed split and the blast-radius histogram. Two
// equal snapshots = no attribution emitted between them. (Parse failures
// are left out: the flat plane re-reports an unparseable file on every
// commit, independently of merged_hash.)
func (f *mergeRetryFixture) attribution(t *testing.T) string {
	t.Helper()
	keep := map[string]bool{
		"da_config_reload_trigger_total":          true,
		"da_config_defaults_change_noop_total":    true,
		"da_config_defaults_shadowed_total":       true,
		"da_config_blast_radius_tenants_affected": true,
	}
	mfs, err := f.reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	var out []string
	for _, mf := range mfs {
		if keep[mf.GetName()] {
			out = append(out, mf.String())
		}
	}
	sort.Strings(out)
	return strings.Join(out, "\n")
}

// reloadActivity is what a reload run leaves in the metrics besides
// attribution: reload duration and debounce batch samples, the debounce
// fired count and the last-reload stamp. Two equal snapshots = no reload ran.
func (f *mergeRetryFixture) reloadActivity(t *testing.T) string {
	t.Helper()
	keep := map[string]bool{
		"da_config_reload_duration_seconds":               true,
		"da_config_debounce_batch_size":                   true,
		"da_config_last_reload_complete_unixtime_seconds": true,
	}
	mfs, err := f.reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	out := []string{fmt.Sprintf("debounce_fired=%d", f.m.DebounceFiredCount())}
	for _, mf := range mfs {
		if keep[mf.GetName()] {
			out = append(out, mf.String())
		}
	}
	sort.Strings(out)
	return strings.Join(out, "\n")
}

func (f *mergeRetryFixture) skipLines() int {
	return strings.Count(f.log.String(), "skipping merged_hash for tenant="+mergeRetryTenant+" ")
}

// TestReload_MergeReadFailure_RetriedOnNextEmptyTick: one failed read on the
// tick that carries an edit → the next tick, with nothing changed on disk,
// must bring merged_hash to /effective's value. The control (no failure)
// pins that the edit itself moves merged_hash on its own tick.
//
// Attribution is the same as before the fix, by design (owner decision on
// #2100): the failing tick emits nothing for the tenant (it returns before
// classification, as it always has), and the retry emits nothing either —
// it is a catch-up recompute, not a change on disk. Through tickOnce the
// retry is not a reload at all: no reload metric moves on that tick.
func TestReload_MergeReadFailure_RetriedOnNextEmptyTick(t *testing.T) {
	t.Parallel()
	for _, entry := range mergeRetryEntries {
		for _, variant := range []string{"chain", "platform"} {
			for _, inject := range []bool{true, false} {
				name := entry + "/" + variant + "/control"
				if inject {
					name = entry + "/" + variant + "/inject"
				}
				t.Run(name, func(t *testing.T) {
					t.Parallel()
					f := newMergeRetryFixture(t, entry)
					h1 := cachedMergedHash(f.m, mergeRetryTenant)
					if h1 == "" || h1 != f.effectiveHash(t) {
						t.Fatalf("cold merged_hash %q, /effective %q", h1, f.effectiveHash(t))
					}
					base := f.attribution(t)

					f.edit(t, variant)
					f.failNested = inject
					r2, n2, counted := f.tick(t)
					h2 := cachedMergedHash(f.m, mergeRetryTenant)
					afterEdit := f.attribution(t)
					activity := f.reloadActivity(t)
					f.failNested = false

					r3, n3, _ := f.tick(t) // nothing changed on disk
					h3 := cachedMergedHash(f.m, mergeRetryTenant)
					want := f.effectiveHash(t)
					if want == h1 {
						t.Fatalf("fixture: the %s edit does not move /effective's merged_hash", variant)
					}

					if h3 != want {
						t.Errorf("merged_hash after the empty tick = %.12s, /effective = %.12s (still the pre-edit %.12s? %v)", h3, want, h1, h3 == h1)
					}
					if got := f.attribution(t); got != afterEdit {
						t.Errorf("the empty tick emitted reload attribution:\nbefore:\n%s\nafter:\n%s", afterEdit, got)
					}
					if counted && (r3 != 0 || n3 != 0) {
						t.Errorf("empty tick (reloaded, noOp) = (%d, %d), want (0, 0)", r3, n3)
					}
					if entry == "tickOnce" {
						if got := f.reloadActivity(t); got != activity {
							t.Errorf("the empty tick ran a reload:\nbefore:\n%s\nafter:\n%s", activity, got)
						}
					}

					if inject {
						if h2 != h1 {
							t.Errorf("failing tick: merged_hash %.12s, want last-known-good %.12s", h2, h1)
						}
						if afterEdit != base {
							t.Errorf("failing tick emitted reload attribution:\nbefore:\n%s\nafter:\n%s", base, afterEdit)
						}
						if counted && (r2 != 0 || n2 != 0) {
							t.Errorf("failing tick (reloaded, noOp) = (%d, %d), want (0, 0)", r2, n2)
						}
						if got := f.skipLines(); got != 1 {
							t.Errorf("skip WARN lines = %d, want 1; log:\n%s", got, f.log.String())
						}
					} else {
						if h2 != want {
							t.Errorf("edit tick: merged_hash %.12s, /effective %.12s", h2, want)
						}
						if counted && (r2 != 1 || n2 != 0) {
							t.Errorf("edit tick (reloaded, noOp) = (%d, %d), want (1, 0)", r2, n2)
						}
						if afterEdit == base {
							t.Errorf("edit tick emitted no reload attribution")
						}
						if got := f.skipLines(); got != 0 {
							t.Errorf("skip WARN lines = %d, want 0; log:\n%s", got, f.log.String())
						}
					}
				})
			}
		}
	}
}

// TestReload_MergeReadFailure_PersistentRetriesOncePerTickLogsOnce: while
// the read keeps failing, every tick retries the tenant exactly once (one
// read of the failing file per tick — no loop within a tick) and the skip
// WARN is written once, on the tick the failure starts, not once per tick.
// When the read recovers, the next tick catches up and says so once.
// Through tickOnce, none of those ticks is a reload: reload duration,
// debounce batch / fired count and the last-reload stamp do not move, and
// no attribution is emitted.
func TestReload_MergeReadFailure_PersistentRetriesOncePerTickLogsOnce(t *testing.T) {
	t.Parallel()
	for _, entry := range mergeRetryEntries {
		for _, variant := range []string{"chain", "platform"} {
			t.Run(entry+"/"+variant, func(t *testing.T) {
				t.Parallel()
				f := newMergeRetryFixture(t, entry)
				h1 := cachedMergedHash(f.m, mergeRetryTenant)

				f.edit(t, variant)
				f.failNested = true
				f.tick(t)
				afterEdit := f.attribution(t)
				activity := f.reloadActivity(t)
				const failingTicks = 4
				for i := 0; i < failingTicks; i++ {
					before := f.nestedReads
					if r, n, counted := f.tick(t); counted && (r != 0 || n != 0) {
						t.Errorf("failing empty tick %d (reloaded, noOp) = (%d, %d), want (0, 0)", i, r, n)
					}
					if got := f.nestedReads - before; got != 1 {
						t.Errorf("failing empty tick %d: %d reads of the failing file, want 1 (one retry per tick)", i, got)
					}
					if h := cachedMergedHash(f.m, mergeRetryTenant); h != h1 {
						t.Errorf("failing empty tick %d: merged_hash %.12s, want last-known-good %.12s", i, h, h1)
					}
				}
				if got := f.skipLines(); got != 1 {
					t.Errorf("skip WARN lines over %d failing ticks = %d, want 1; log:\n%s", failingTicks+1, got, f.log.String())
				}

				f.failNested = false
				f.tick(t)
				if h, want := cachedMergedHash(f.m, mergeRetryTenant), f.effectiveHash(t); h != want {
					t.Errorf("after recovery: merged_hash %.12s, /effective %.12s", h, want)
				}
				if got := strings.Count(f.log.String(), "merged_hash for tenant="+mergeRetryTenant+" recomputed"); got != 1 {
					t.Errorf("recovery lines = %d, want 1; log:\n%s", got, f.log.String())
				}
				if got := f.attribution(t); got != afterEdit {
					t.Errorf("retry ticks emitted reload attribution:\nbefore:\n%s\nafter:\n%s", afterEdit, got)
				}
				if entry == "tickOnce" {
					if got := f.reloadActivity(t); got != activity {
						t.Errorf("retry ticks ran a reload:\nbefore:\n%s\nafter:\n%s", activity, got)
					}
				}

				// Recovered: the next empty tick is clean again — no read at all.
				before := f.nestedReads
				f.tick(t)
				if got := f.nestedReads - before; got != 0 {
					t.Errorf("empty tick after recovery read the chain file %d times, want 0", got)
				}
			})
		}
	}
}

// TestReload_MergeParseFailure_NotRetried: the retry is for a failed READ
// only. A chain file that no longer parses fails the same way on every
// attempt over the same bytes, so the next empty tick does not re-read it,
// and the merge's skip / chain-index ERROR lines are not re-emitted per tick
// — the same as before #2100. (Through diffAndReload, the flat plane and the
// parsedDefaults cache re-report the file on every call on their own; that
// is not this path.)
func TestReload_MergeParseFailure_NotRetried(t *testing.T) {
	t.Parallel()
	for _, entry := range mergeRetryEntries {
		t.Run(entry, func(t *testing.T) {
			t.Parallel()
			f := newMergeRetryFixture(t, entry)
			h1 := cachedMergedHash(f.m, mergeRetryTenant)

			writeTestYAML(t, f.nested, "defaults:\n  mysql_connections: [81\n")
			f.tick(t)
			afterEdit := f.attribution(t)
			if !strings.Contains(f.log.String(), "(chain index") || f.skipLines() != 1 {
				t.Fatalf("fixture: the broken chain file did not fail the merge as a parse error:\n%s", f.log.String())
			}
			if h := cachedMergedHash(f.m, mergeRetryTenant); h != h1 {
				t.Fatalf("parse-failing tick: merged_hash %.12s, want last-known-good %.12s", h, h1)
			}
			logAfterEdit := f.log.Len()

			before := f.nestedReads
			f.tick(t) // nothing changed on disk
			if got := f.nestedReads - before; got != 0 {
				t.Errorf("empty tick re-read the unparseable chain file %d times, want 0", got)
			}
			if got := f.attribution(t); got != afterEdit {
				t.Errorf("empty tick emitted reload attribution:\nbefore:\n%s\nafter:\n%s", afterEdit, got)
			}
			if tail := f.log.String()[logAfterEdit:]; strings.Contains(tail, "skipping merged_hash") || strings.Contains(tail, "(chain index") {
				t.Errorf("empty tick re-ran the failing merge:\n%s", tail)
			}
		})
	}
}
