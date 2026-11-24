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
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	var got []string
	for _, r := range m.GetConfig().ResolveAt(time.Now()) {
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
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	// A tenant-only edit of the very file that holds the expired value.
	write("t.yaml", "tenants:\n  tx:\n    _profile: p\n    _metadata: {owner: x}\n"+line)
	m.tickOnce()
	var got []string
	for _, r := range m.GetConfig().ResolveAt(time.Now()) {
		if r.Tenant == "tx" && r.Component == "redis" {
			got = append(got, strconv.FormatFloat(r.Value, 'g', -1, 64))
		}
	}
	if len(got) != 1 || got[0] != "70" {
		t.Errorf("after the edit: rows %v, want the profile's 70", got)
	}
}
