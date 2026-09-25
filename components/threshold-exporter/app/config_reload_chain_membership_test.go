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
//	<dir>/_defaults.yaml       cpu_pct: 50, mem_pct: 60
//	<dir>/sub/_defaults.yaml   cpu_pct: 90
//	<dir>/sub/t.yaml           tenants: t: {}
func chainMembershipBaseTree(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o755); err != nil {
		t.Fatalf("mkdir sub: %v", err)
	}
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  cpu_pct: 50\n  mem_pct: 60\n")
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
	write := func(rel, content string) func(t *testing.T, dir string) {
		return func(t *testing.T, dir string) {
			t.Helper()
			writeTestYAML(t, filepath.Join(dir, filepath.FromSlash(rel)), content)
		}
	}
	remove := func(rel string) func(t *testing.T, dir string) {
		return func(t *testing.T, dir string) {
			t.Helper()
			rm(t, filepath.Join(dir, filepath.FromSlash(rel)))
		}
	}

	// -1 = not pinned.
	cases := []struct {
		name string
		// extra runs on the base tree BEFORE Load (optional).
		extra func(t *testing.T, dir string)
		// mutate runs after Load, before the reload.
		mutate func(t *testing.T, dir string)
		// wantReloaded / wantNoOp pin diffAndReload's counts when >= 0.
		wantReloaded, wantNoOp int
		// wantScope + wantEffect, when non-empty, pin exactly one
		// (defaults, wantScope, wantEffect) blast-radius observation and
		// none under scope "unknown".
		wantScope, wantEffect string
	}{
		// ── membership changes that MOVE merged_hash (the #1964 defect) ──
		{
			name:         "A delete subtree _defaults.yaml",
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: 1, wantNoOp: 0,
			wantScope: "domain", wantEffect: "applied",
		},
		{
			name:         "D co-located .yml with different content, delete .yaml",
			extra:        write("sub/_defaults.yml", "defaults:\n  cpu_pct: 30\n"),
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: 1, wantNoOp: 0,
			wantScope: "domain", wantEffect: "applied",
		},
		{
			// mem_pct is set only at the root, so removing the root moves
			// the merged result even though sub still sets cpu_pct.
			name:         "delete root _defaults.yaml carrying a key nothing overrides",
			mutate:       remove("_defaults.yaml"),
			wantReloaded: 1, wantNoOp: 0,
			wantScope: "global", wantEffect: "applied",
		},

		// ── membership changes that leave merged_hash unchanged (no-op) ──
		{
			// Root sets only cpu_pct, which sub overrides for this tenant.
			name:         "no-op: delete root _defaults.yaml whose only key sub overrides",
			extra:        write("_defaults.yaml", "defaults:\n  cpu_pct: 50\n"),
			mutate:       remove("_defaults.yaml"),
			wantReloaded: 0, wantNoOp: 1,
			wantScope: "global", wantEffect: "cosmetic",
		},
		{
			// The tenant overrides the only key the removed file set.
			name:         "no-op shadowed: delete subtree _defaults.yaml the tenant overrides",
			extra:        write("sub/t.yaml", "tenants:\n  t:\n    cpu_pct: \"10\"\n"),
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: 0, wantNoOp: 1,
			wantScope: "domain", wantEffect: "shadowed",
		},
		{
			// The removed file restated the root's value; the tenant
			// overrides nothing, so nothing was blocked — cosmetic.
			name:         "no-op cosmetic: delete subtree _defaults.yaml restating the root value",
			extra:        write("sub/_defaults.yaml", "defaults:\n  cpu_pct: 50\n"),
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: 0, wantNoOp: 1,
			wantScope: "domain", wantEffect: "cosmetic",
		},

		{
			// Carrier switch to identical content: no key changed, so even
			// a tenant overriding every key must not read as shadowed.
			name: "no-op cosmetic: carrier switch to identical .yml, tenant overrides all keys",
			extra: func(t *testing.T, dir string) {
				write("sub/_defaults.yml", "defaults:\n  cpu_pct: 90\n")(t, dir)
				write("sub/t.yaml", "tenants:\n  t:\n    cpu_pct: \"10\"\n")(t, dir)
			},
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: 0, wantNoOp: 1,
			wantScope: "domain", wantEffect: "cosmetic",
		},
		{
			// Carrier switch 90 → 30 on a key the tenant overrides.
			name: "no-op shadowed: carrier switch to different .yml on a key the tenant overrides",
			extra: func(t *testing.T, dir string) {
				write("sub/_defaults.yml", "defaults:\n  cpu_pct: 30\n")(t, dir)
				write("sub/t.yaml", "tenants:\n  t:\n    cpu_pct: \"10\"\n")(t, dir)
			},
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: 0, wantNoOp: 1,
			wantScope: "domain", wantEffect: "shadowed",
		},

		// ── controls: hash-visible changes the old logic already handled ──
		{
			name:         "control: edit subtree _defaults.yaml content",
			mutate:       write("sub/_defaults.yaml", "defaults:\n  cpu_pct: 70\n"),
			wantReloaded: -1, wantNoOp: -1,
		},
		{
			name:         "control: add subtree _defaults.yaml where none existed",
			extra:        remove("sub/_defaults.yaml"),
			mutate:       write("sub/_defaults.yaml", "defaults:\n  cpu_pct: 70\n"),
			wantReloaded: -1, wantNoOp: -1,
		},
		{
			name:         "control: co-located .yml with identical content, delete .yaml",
			extra:        write("sub/_defaults.yml", "defaults:\n  cpu_pct: 90\n"),
			mutate:       remove("sub/_defaults.yaml"),
			wantReloaded: -1, wantNoOp: -1,
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

			reloaded, noOp, err := m.diffAndReload()
			if err != nil {
				t.Fatalf("diffAndReload: %v", err)
			}
			if tc.wantReloaded >= 0 && reloaded != tc.wantReloaded {
				t.Errorf("reloaded = %d, want %d", reloaded, tc.wantReloaded)
			}
			if tc.wantNoOp >= 0 && noOp != tc.wantNoOp {
				t.Errorf("noOp = %d, want %d", noOp, tc.wantNoOp)
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

			if tc.wantEffect != "" {
				if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, tc.wantScope, tc.wantEffect); n != 1 {
					t.Errorf("blast-radius (defaults, %s, %s) sampleCount = %d, want 1", tc.wantScope, tc.wantEffect, n)
				}
				if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "unknown", tc.wantEffect); n != 0 {
					t.Errorf("blast-radius (defaults, unknown, %s) sampleCount = %d, want 0", tc.wantEffect, n)
				}
			}
		})
	}
}
