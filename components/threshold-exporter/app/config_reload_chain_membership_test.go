package main

// ============================================================
// #1964 — reload recomputes merged_hash when defaults-chain MEMBERSHIP changes
// ============================================================
//
// classifyTenant used to decide "defaults changed" by comparing the hash of
// every entry in the NEW chain against the prior scan. A chain whose
// membership changed without any surviving entry changing hash — a subtree
// `_defaults.yaml` deleted, or a co-located `.yaml`/`.yml` pair losing its
// `.yaml` so the (already-known) `.yml` becomes the carrier — was read as
// "nothing moved", and the reload kept the stale merged_hash while
// pkg/config.ResolveEffective (tenant-api /effective) computed the new one.
//
// Each case: Load → mutate → pin every surviving file's mtime to a fixed
// future instant (os.Chtimes, so the scanner's mtime fast-path cannot mask
// a content change and no sleep is needed) → diffAndReload → the exporter's
// merged_hash must equal /effective's for the same tree.

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// chainMembershipBaseTree writes the shared base tree:
//
//	<dir>/_defaults.yaml       cpu_pct: 50
//	<dir>/sub/_defaults.yaml   cpu_pct: 90
//	<dir>/sub/t.yaml           tenants: t: {}
func chainMembershipBaseTree(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o755); err != nil {
		t.Fatalf("mkdir sub: %v", err)
	}
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n")
	writeTestYAML(t, filepath.Join(dir, "sub", "_defaults.yaml"), "defaults:\n  cpu_pct: 90\n")
	writeTestYAML(t, filepath.Join(dir, "sub", "t.yaml"), "tenants:\n  t: {}\n")
}

// touchTreeAt sets every regular file's mtime under dir to at.
func touchTreeAt(t *testing.T, dir string, at time.Time) {
	t.Helper()
	err := filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.Mode().IsRegular() {
			return os.Chtimes(p, at, at)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("touch tree: %v", err)
	}
}

func TestReload_DefaultsChainMembershipChange_MatchesEffective(t *testing.T) {
	t.Parallel()

	rm := func(t *testing.T, p string) {
		t.Helper()
		if err := os.Remove(p); err != nil {
			t.Fatalf("remove %s: %v", p, err)
		}
	}

	cases := []struct {
		name string
		// extra runs on the base tree BEFORE Load (optional).
		extra func(t *testing.T, dir string)
		// mutate runs after Load, before the reload.
		mutate func(t *testing.T, dir string)
		// wantReloaded, when >= 0, pins diffAndReload's reloaded count.
		wantReloaded int
		// wantScope, when non-empty, pins the blast-radius scope of the
		// (defaults, <scope>, applied) observation.
		wantScope string
	}{
		{
			name: "A delete subtree _defaults.yaml",
			mutate: func(t *testing.T, dir string) {
				rm(t, filepath.Join(dir, "sub", "_defaults.yaml"))
			},
			wantReloaded: 1,
			wantScope:    "domain",
		},
		{
			name: "D co-located .yml with different content, delete .yaml",
			extra: func(t *testing.T, dir string) {
				writeTestYAML(t, filepath.Join(dir, "sub", "_defaults.yml"), "defaults:\n  cpu_pct: 30\n")
			},
			mutate: func(t *testing.T, dir string) {
				rm(t, filepath.Join(dir, "sub", "_defaults.yaml"))
			},
			wantReloaded: 1,
			wantScope:    "domain",
		},
		{
			name: "control: edit subtree _defaults.yaml content",
			mutate: func(t *testing.T, dir string) {
				writeTestYAML(t, filepath.Join(dir, "sub", "_defaults.yaml"), "defaults:\n  cpu_pct: 70\n")
			},
			wantReloaded: -1,
		},
		{
			name: "control: add subtree _defaults.yaml where none existed",
			extra: func(t *testing.T, dir string) {
				rm(t, filepath.Join(dir, "sub", "_defaults.yaml"))
			},
			mutate: func(t *testing.T, dir string) {
				writeTestYAML(t, filepath.Join(dir, "sub", "_defaults.yaml"), "defaults:\n  cpu_pct: 70\n")
			},
			wantReloaded: -1,
		},
		{
			name: "control: co-located .yml with identical content, delete .yaml",
			extra: func(t *testing.T, dir string) {
				writeTestYAML(t, filepath.Join(dir, "sub", "_defaults.yml"), "defaults:\n  cpu_pct: 90\n")
			},
			mutate: func(t *testing.T, dir string) {
				rm(t, filepath.Join(dir, "sub", "_defaults.yaml"))
			},
			wantReloaded: -1,
		},
		{
			name: "control: delete root _defaults.yaml",
			mutate: func(t *testing.T, dir string) {
				rm(t, filepath.Join(dir, "_defaults.yaml"))
			},
			wantReloaded: -1,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			chainMembershipBaseTree(t, dir)
			if tc.extra != nil {
				tc.extra(t, dir)
			}

			fresh, _ := freshMetrics(t)
			m := NewConfigManagerWithDebounce(dir, 0)
			m.SetMetrics(fresh)
			m.SetLogger(log.New(io.Discard, "", 0))
			defer m.Close()
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}

			tc.mutate(t, dir)
			touchTreeAt(t, dir, time.Now().Add(3*time.Second))

			reloaded, _, err := m.diffAndReload()
			if err != nil {
				t.Fatalf("diffAndReload: %v", err)
			}
			if tc.wantReloaded >= 0 && reloaded != tc.wantReloaded {
				t.Errorf("reloaded = %d, want %d", reloaded, tc.wantReloaded)
			}

			ec, ok := m.Resolve("t")
			if !ok {
				t.Fatalf("Resolve(t): unknown tenant after reload")
			}
			pe, err := config.ResolveEffective(dir, "t")
			if err != nil {
				t.Fatalf("ResolveEffective: %v", err)
			}
			if ec.MergedHash != pe.MergedHash {
				t.Errorf("exporter merged_hash=%s, /effective merged_hash=%s (chain exporter=%v effective=%v) — DIVERGED",
					ec.MergedHash, pe.MergedHash, chainBases(ec.DefaultsChain), pe.DefaultsChain)
			}

			if tc.wantScope != "" {
				if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, tc.wantScope, "applied"); n != 1 {
					t.Errorf("blast-radius (defaults, %s, applied) sampleCount = %d, want 1", tc.wantScope, n)
				}
				if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "unknown", "applied"); n != 0 {
					t.Errorf("blast-radius (defaults, unknown, applied) sampleCount = %d, want 0", n)
				}
			}
		})
	}
}
