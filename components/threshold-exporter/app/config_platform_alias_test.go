package main

// config_platform_alias_test.go — #2368 in package main: the platform
// `tenants:` layer and the tenant file stack per THRESHOLD, not per
// spelling, through the reload paths and the reload classifier. The shared
// matrix rows (tests/shared/platform_tenant_overlay_matrix.json, a-rows)
// pin the cold load; this file pins what the matrix cannot carry.
//
// Its own file, and on the forbid-legacy-mysql-cpu-key exclude list, because
// its fixtures ARE the retired spelling as a live value by design (#1231).
//
// Seams: logger / metrics via newOverlayManager; t.TempDir() trees.

import (
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

// TestClassifyDefaultsNoOpEffect_PlatformKeyShadowedAcrossSpellings: a
// platform `tenants:` entry's change to `mysql_threads_running` does not
// reach a tenant whose file writes the legacy `mysql_cpu` — the tenant wins
// across spellings — so it is "shadowed", as the canonical spelling is.
// Control: a tenant writing neither spelling leaves it "cosmetic".
func TestClassifyDefaultsNoOpEffect_PlatformKeyShadowedAcrossSpellings(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ name, body, want string }{
		{"legacy", "tenants:\n  t1:\n    mysql_cpu: 90\n", "shadowed"},
		{"canonical", "tenants:\n  t1:\n    mysql_threads_running: 90\n", "shadowed"},
		{"control-neither", "tenants:\n  t1:\n    redis_connections: 5\n", "cosmetic"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := classifyDefaultsNoOpEffect([]byte(tc.body), "t1", nil, nil, nil, nil, nil, nil, nil,
				[]string{"mysql_threads_running"}, nil, nil, nil)
			if got != tc.want {
				t.Errorf("got %q, want %q", got, tc.want)
			}
		})
	}
}

// TestPlatformTenantAliasSurvivesReload (#2368): the tenant's legacy
// `mysql_cpu` keeps beating the platform entry's canonical
// `mysql_threads_running` after a tenant-file-only edit and after a
// platform-file edit, through the watch path's reload — on this carrier
// tree, the hierarchical one.
//
// ⚠️ CARRIER LAYOUT ONLY, AND THAT IS A LOSS (#1577). This test reads what
// /metrics serves, and a tree with no `_defaults` carrier serves no threshold
// row at all (measured: Resolve() returns 0 rows for a tenant file setting
// mysql_connections; 1 row once a carrier exists) — every key is "not in
// defaults" — so on the only layout where the watch path takes
// incrementalLoadFrom there is nothing for these assertions to read. It used
// to reach patchTenants through the removed `IncrementalLoad()` on the
// carrier tree, a combination production never runs. On the flat layout
// the fast path's multi-source precedence is pinned by the flat tree of
// TestTheFastPathAlwaysLandsWhereAFullLoadWould (every tenant override
// against a full load), which never writes the legacy spelling; the
// ACROSS-SPELLING half of reclaimTenantFrom's union is pinned on the flat
// layout by TestTheFlatFastPathUnionsAcrossSpellings below, at the config
// layer (`Tenants`) rather than /metrics — the difference is visible there
// even though the flat tree serves no threshold row.
func TestPlatformTenantAliasSurvivesReload(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)
	f := func(v float64) *float64 { return &v }
	const key = "mysql_threads_running"
	steps := []struct {
		name   string
		mutate func(t *testing.T, dir string)
		want   *float64
	}{
		{"tenant-file-edit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx:\n    mysql_cpu: \"91\"\n    redis_x: \"1\"\n")
		}, f(91)},
		{"platform-file-edit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
				"defaults:\n  mysql_threads_running: 30\ntenants:\n  tx:\n    mysql_threads_running: 75\n    redis_x: \"2\"\n")
		}, f(91)},
		{"tenant-drops-its-key", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx:\n    redis_x: \"1\"\n")
		}, f(75)},
	}
	dir := t.TempDir()
	writeOverlayTree(t, dir, overlayTree(m, t, "a1-alias-tenant-legacy-beats-platform-canonical"))
	mgr, _ := newOverlayManager(t, dir)
	assertServedKey(t, mgr, "load", "tx", key, f(90))
	for i, st := range steps {
		st.mutate(t, dir)
		touchTreeAt(t, dir, time.Now().Add(time.Duration(i+3)*time.Second))
		if err := watchReload(mgr); err != nil {
			t.Fatalf("%s: %v", st.name, err)
		}
		assertServedKey(t, mgr, st.name, "tx", key, st.want)
	}
}

// TestTheFlatFastPathUnionsAcrossSpellings (#1577): on a tree with no
// `_defaults` carrier — the only layout where the watch path reaches
// incrementalLoadFrom and patchTenants — a tenant declared by a platform file
// (`_profiles.yaml`, canonical `mysql_threads_running`) and by its own file
// (legacy `mysql_cpu`) must come out of a tenant-only reload with the same
// `Tenants` entry a fresh Load builds: one threshold, the tenant's spelling.
//
// ⛔ CONFIG LAYER ON PURPOSE. A flat tree serves no threshold row (see the
// test above), so /metrics cannot tell the two apart; `Tenants` can.
// MEASURED: with reclaimTenantFrom's cross-spelling overlay (since #2370
// config.OverlayTenantLayer) replaced by
// a per-key assignment, the reload keeps BOTH spellings
// (mysql_cpu:91 mysql_threads_running:75 redis_x:1) while a fresh Load keeps
// only mysql_cpu — this test fails; restored, it passes.
func TestTheFlatFastPathUnionsAcrossSpellings(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"),
		"tenants:\n  tx:\n    mysql_threads_running: \"75\"\n")
	tenantFile := filepath.Join(dir, "tx.yaml")
	writeTestYAML(t, tenantFile, "tenants:\n  tx:\n    mysql_cpu: \"90\"\n    redis_x: \"1\"\n")

	mgr, _ := newOverlayManager(t, dir)
	requireFlatWatchPath(t, mgr)

	writeTestYAML(t, tenantFile, "tenants:\n  tx:\n    mysql_cpu: \"91\"\n    redis_x: \"1\"\n")
	if err := watchReload(mgr); err != nil {
		t.Fatalf("reload: %v", err)
	}
	reloaded := tenantDefaults(mgr.GetConfig().Tenants["tx"])
	if reloaded["mysql_cpu"] != "91" {
		t.Fatalf("premise: the tenant edit did not land (tx = %v), so this run proves nothing", reloaded)
	}

	ref, _ := newOverlayManager(t, dir)
	if full := tenantDefaults(ref.GetConfig().Tenants["tx"]); !reflect.DeepEqual(reloaded, full) {
		t.Errorf("tenant-only reload on the flat tree diverged from a fresh Load across spellings\n"+
			"reload=%v\nfull=  %v", reloaded, full)
	}
}

// tenantDefaults is a tenant's override map reduced to key → Default.
func tenantDefaults(ov map[string]ScheduledValue) map[string]string {
	out := make(map[string]string, len(ov))
	for k, v := range ov {
		out[k] = v.Default
	}
	return out
}
