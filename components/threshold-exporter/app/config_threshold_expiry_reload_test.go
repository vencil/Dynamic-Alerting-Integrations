package main

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"testing"
	"time"

	"github.com/jonboulle/clockwork"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// #4263: the exporter's committed config answers for the instant it was
// built at; at the tree's next `expires:` it is merged again
// (armExpiryReload), with nothing changed on disk, and /metrics serves what
// the expired value gave way to. Both reload paths: a tree with a root
// `_defaults.yaml` (the hierarchical build) and one without (the flat
// merge).
func TestThresholdExpiry_ExporterMergesAgainAtTheNextExpiry(t *testing.T) {
	t.Parallel()
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	k := "redis_connected_clients"
	for name, root := range map[string]string{"hierarchical": "defaults: {}\n", "flat": ""} {
		root := root
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			files := map[string]string{
				"_profiles.yaml": "profiles:\n  p:\n    " + k + ": \"70\"\n",
				"t.yaml": "tenants:\n  tx:\n    _profile: p\n    " + k + ":\n      default: \"5\"\n" +
					"      expires: \"2026-01-01T01:00:00Z\"\n",
			}
			if root != "" {
				files["_defaults.yaml"] = root
			}
			for n, body := range files {
				if err := os.WriteFile(filepath.Join(dir, n), []byte(body), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			m := NewConfigManagerWithDebounce(dir, 0)
			t.Cleanup(m.Close)
			fresh, _ := freshMetrics(t)
			m.SetMetrics(fresh)
			fc := clockwork.NewFakeClockAt(start)
			m.SetClock(fc)
			m.SetLogger(log.New(io.Discard, "", 0))
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			served := func() []string {
				var out []string
				for _, r := range m.GetConfig().ResolveAt(fc.Now()) {
					if r.Tenant == "tx" && r.Component == "redis" {
						out = append(out, r.Metric+" "+strconv.FormatFloat(r.Value, 'g', -1, 64))
					}
				}
				sort.Strings(out)
				return out
			}
			if got := served(); len(got) != 1 || got[0] != "connected_clients 5" {
				t.Fatalf("before expiry: %v, want the tenant's 5", got)
			}
			fc.Advance(2 * time.Hour)
			deadline := time.Now().Add(5 * time.Second)
			for {
				if got := served(); len(got) == 1 && got[0] == "connected_clients 70" {
					return
				}
				if time.Now().After(deadline) {
					t.Fatalf("after expiry: %v, want the profile's 70 (the tree merged again)", served())
				}
				time.Sleep(10 * time.Millisecond)
			}
		})
	}
}

// #4263: single-file mode decides `expires:` as a tree does: an expired
// tenant value gives way to its profile's in the same file.
func TestThresholdExpiry_SingleFileGivesWayToTheProfile(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	body := "defaults:\n  pg_connections: 80\nprofiles:\n  p:\n    redis_connected_clients: \"70\"\n" +
		"tenants:\n  tx:\n    _profile: p\n    redis_connected_clients:\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	m := NewConfigManager(path)
	t.Cleanup(m.Close)
	fresh, _ := freshMetrics(t)
	m.SetMetrics(fresh)
	fc := clockwork.NewFakeClockAt(time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC))
	m.SetClock(fc)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	var got []string
	for _, r := range m.GetConfig().ResolveAt(fc.Now()) {
		if r.Tenant == "tx" && r.Component == "redis" {
			got = append(got, strconv.FormatFloat(r.Value, 'g', -1, 64))
		}
	}
	if len(got) != 1 || got[0] != "70" {
		t.Errorf("rows %v, want the profile's 70", got)
	}
}

// #4263: a flat tree holding a time-boxed value is merged whole on a
// tenant-only edit — a patch keeps the previous merge, which decided
// `expires:` for no tenant it re-merges.
func TestThresholdExpiry_FlatEditMergesWhole(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	k := "redis_connected_clients"
	write := func(n, body string) {
		if err := os.WriteFile(filepath.Join(dir, n), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("_profiles.yaml", "profiles:\n  p:\n    "+k+": \"70\"\n")
	line := "    " + k + ":\n      default: \"5\"\n      expires: \"2020-01-01T00:00:00Z\"\n"
	write("t.yaml", "tenants:\n  tx:\n    _profile: p\n"+line)
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	fresh, _ := freshMetrics(t)
	m.SetMetrics(fresh)
	fc := clockwork.NewFakeClockAt(time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC))
	m.SetClock(fc)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	// A tenant-only edit of the very file that holds the expired value.
	write("t.yaml", "tenants:\n  tx:\n    _profile: p\n    _metadata: {owner: x}\n"+line)
	m.tickOnce()
	var got []string
	for _, r := range m.GetConfig().ResolveAt(fc.Now()) {
		if r.Tenant == "tx" && r.Component == "redis" {
			got = append(got, strconv.FormatFloat(r.Value, 'g', -1, 64))
		}
	}
	if len(got) != 1 || got[0] != "70" {
		t.Errorf("after the edit: rows %v, want the profile's 70", got)
	}
}

// #4263: single-file mode loads the unchanged file again once its config no
// longer answers for the clock — here the clock set back before the
// `expires:` the load saw pass.
func TestThresholdExpiry_SingleFileTickSeesTheClockSetBack(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	body := "defaults:\n  redis_connected_clients: 70\n" +
		"tenants:\n  tx:\n    redis_connected_clients:\n      default: \"5\"\n      expires: \"2026-01-01T01:00:00Z\"\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	m := NewConfigManager(path)
	t.Cleanup(m.Close)
	fresh, _ := freshMetrics(t)
	m.SetMetrics(fresh)
	fc := clockwork.NewFakeClockAt(time.Date(2026, 1, 1, 2, 0, 0, 0, time.UTC))
	m.SetClock(fc)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	served := func() string {
		for _, r := range m.GetConfig().ResolveAt(fc.Now()) {
			if r.Tenant == "tx" && r.Component == "redis" {
				return strconv.FormatFloat(r.Value, 'g', -1, 64)
			}
		}
		return ""
	}
	if got := served(); got != "70" {
		t.Fatalf("after expiry: %s, want the root's 70", got)
	}
	fc.Advance(-2 * time.Hour)
	m.tickOnce()
	if got := served(); got != "5" {
		t.Errorf("clock set back before expires: %s, want the tenant's 5", got)
	}
}

// #4263: da_config_valid_until_unixtime_seconds is the committed config's
// next `expires:` — set at every commit, in every config mode — and 0 once
// none lies ahead: what ConfigExpiryRebuildOverdue compares with time().
func TestThresholdExpiry_ValidUntilGaugeFollowsTheCommit(t *testing.T) {
	t.Parallel()
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	edge := float64(time.Date(2026, 1, 1, 1, 0, 0, 0, time.UTC).Unix())
	body := "tenants:\n  tx:\n    redis_connected_clients:\n      default: \"5\"\n      expires: \"2026-01-01T01:00:00Z\"\n"
	for _, mode := range []string{"hierarchical", "flat", "single-file"} {
		mode := mode
		t.Run(mode, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			path := dir
			switch mode {
			case "hierarchical":
				os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte("defaults: {}\n"), 0o600)
				os.WriteFile(filepath.Join(dir, "t.yaml"), []byte(body), 0o600)
			case "flat":
				os.WriteFile(filepath.Join(dir, "t.yaml"), []byte(body), 0o600)
			default:
				path = filepath.Join(dir, "config.yaml")
				os.WriteFile(path, []byte("defaults: {}\n"+body), 0o600)
			}
			fresh, _ := freshMetrics(t)
			m := NewConfigManagerWithDebounce(path, 0)
			t.Cleanup(m.Close)
			fc := clockwork.NewFakeClockAt(start)
			m.SetClock(fc)
			m.SetMetrics(fresh)
			m.SetLogger(log.New(io.Discard, "", 0))
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got := testutil.ToFloat64(fresh.validUntil); got != edge {
				t.Fatalf("after Load: %v, want %v (01:00)", got, edge)
			}
			fc.Advance(2 * time.Hour)
			m.tickOnce() // the timer's reload, or the tick's safety net
			deadline := time.Now().Add(5 * time.Second)
			for testutil.ToFloat64(fresh.validUntil) != 0 {
				if time.Now().After(deadline) {
					t.Fatalf("after the rebuild: %v, want 0 (no expires: ahead)", testutil.ToFloat64(fresh.validUntil))
				}
				time.Sleep(10 * time.Millisecond)
			}
		})
	}
}

// #4263: the expiry's merge is not starved by the watch tick. With a
// debounce window no shorter than the tick, re-triggering the debounced
// reload on every tick would push its timer out for good; the tick leaves a
// pending one alone, so the merge commits within one window.
func TestThresholdExpiry_TheTickDoesNotStarveTheMerge(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte("defaults:\n  redis_connected_clients: 70\n"), 0o600)
	os.WriteFile(filepath.Join(dir, "t.yaml"), []byte("tenants:\n  tx:\n    redis_connected_clients:\n"+
		"      default: \"5\"\n      expires: \"2026-01-01T01:00:00Z\"\n"), 0o600)
	m := NewConfigManagerWithDebounce(dir, 60*time.Second)
	t.Cleanup(m.Close)
	fresh, _ := freshMetrics(t)
	m.SetMetrics(fresh)
	fc := clockwork.NewFakeClockAt(time.Date(2026, 1, 1, 0, 59, 0, 0, time.UTC))
	m.SetClock(fc)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	for i := 0; i < 20; i++ { // 10 minutes of 30s ticks
		fc.Advance(30 * time.Second)
		m.tickOnce()
		time.Sleep(5 * time.Millisecond)
		expired := fc.Now().After(time.Date(2026, 1, 1, 1, 0, 0, 0, time.UTC))
		if expired && m.GetConfig().ValidAt(fc.Now()) {
			return
		}
	}
	t.Errorf("10 minutes past the expires: the merge never committed (debounce fired %d times)", m.DebounceFiredCount())
}

// #4333 review: in single-file mode the expiry timer reloads through
// triggerDebouncedReload, under reloadMu; the tick's own reload of a changed
// file takes reloadMu too, so the two never interleave (an older read
// committing over a newer one).
func TestThresholdExpiry_SingleFileTickReloadsUnderReloadMu(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	write := func(v string) {
		t.Helper()
		if err := os.WriteFile(path, []byte("defaults:\n  redis_connected_clients: "+v+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("70")
	m := NewConfigManager(path)
	t.Cleanup(m.Close)
	fresh, _ := freshMetrics(t)
	m.SetMetrics(fresh)
	m.SetClock(clockwork.NewFakeClockAt(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)))
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	before := m.GetConfig()
	write("80")
	m.reloadMu.Lock()
	done := make(chan struct{})
	go func() { m.tickOnce(); close(done) }()
	select {
	case <-done:
		m.reloadMu.Unlock()
		t.Fatal("the tick reloaded while another reload held reloadMu")
	case <-time.After(200 * time.Millisecond):
	}
	if m.GetConfig() != before {
		t.Error("the tick committed while another reload held reloadMu")
	}
	m.reloadMu.Unlock()
	<-done
	if m.GetConfig().Defaults["redis_connected_clients"] != 80 {
		t.Errorf("after reloadMu: %v, want the changed file's 80", m.GetConfig().Defaults)
	}
}
