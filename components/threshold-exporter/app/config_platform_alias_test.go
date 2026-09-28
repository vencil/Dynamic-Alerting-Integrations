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
// `mysql_threads_running` after a tenant-file-only edit — the patchTenants
// fast path, which rebuilds the tenant from every declaring file
// (reclaimTenantFrom's multi-source union) — and after a platform-file edit
// (the incremental full rebuild). Both reload entries.
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
	reloaders := map[string]func(m *ConfigManager) error{
		"IncrementalLoad": func(m *ConfigManager) error { return m.IncrementalLoad() },
		"diffAndReload": func(m *ConfigManager) error {
			_, _, err := m.diffAndReload()
			return err
		},
	}
	for rname, reload := range reloaders {
		t.Run(rname, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, overlayTree(m, t, "a1-alias-tenant-legacy-beats-platform-canonical"))
			mgr, _ := newOverlayManager(t, dir)
			assertServedKey(t, mgr, rname+"/load", "tx", key, f(90))
			for i, st := range steps {
				st.mutate(t, dir)
				touchTreeAt(t, dir, time.Now().Add(time.Duration(i+3)*time.Second))
				if err := reload(mgr); err != nil {
					t.Fatalf("%s/%s: %v", rname, st.name, err)
				}
				assertServedKey(t, mgr, rname+"/"+st.name, "tx", key, st.want)
			}
		})
	}
}
