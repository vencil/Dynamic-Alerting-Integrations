package main

// #2019 — the exporter's merged_hash includes a root platform file's
// per-tenant block, and a reload that edits ONLY that block recomputes it.
//
// Measured before the fix: changing `_defaults.yaml`'s `tenants.tx` value
// moved /metrics 60 → 55 while tx's merged_hash stayed identical, so the
// reload was attributed as a cosmetic no-op; an edit to `_profiles.yaml`
// (a platform file outside every chain) was not even recomputed. Each case
// below: Load → mutate → pin mtimes → diffAndReload → the cached
// merged_hash must (a) move or stay as stated, (b) equal what
// pkg/config.ResolveEffective (/effective) computes for the same tree, and
// (c) be attributed to the stated blast-radius bucket.
//
// Seams: metrics via freshMetrics + SetMetrics, logger via SetLogger.

import (
	"encoding/json"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func cachedMergedHash(m *ConfigManager, tid string) string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.hierarchy.mergedHashes[tid]
}

// committedTenant is what the last commit cached for one tenant on the
// hierarchical plane. It reads no file, so it moves only when a reload
// commits — the same state /metrics is served from. (#2586: it replaces
// ConfigManager.Resolve, which re-read the tenant and defaults files on
// every call and so, inside a debounce window, answered with bytes the
// exporter had not committed.)
type committedTenant struct {
	SourceFile    string
	DefaultsChain []string // root first
	MergedHash    string
}

// committedTenantState returns tid's committed hierarchy entry; ok is false
// when the last commit does not know the tenant.
func committedTenantState(m *ConfigManager, tid string) (committedTenant, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	src, ok := m.hierarchy.tenantSources[tid]
	if !ok {
		return committedTenant{}, false
	}
	var chain []string
	if m.hierarchy.graph != nil {
		chain = append(chain, m.hierarchy.graph.TenantDefaults[tid]...)
	}
	return committedTenant{SourceFile: src, DefaultsChain: chain, MergedHash: m.hierarchy.mergedHashes[tid]}, true
}

// cachedLayers is what the cached merged_hash of tid was computed with:
// its platform entries (#2019) and the tree's profiles (#2117).
func cachedLayers(m *ConfigManager, tid string) config.TenantLayers {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return config.TenantLayers{
		Overlay:  config.PlatformOverlayFor(m.hierarchy.platform, tid),
		Profiles: m.hierarchy.profiles,
	}
}

func readAll(t *testing.T, paths ...string) [][]byte {
	t.Helper()
	out := make([][]byte, 0, len(paths))
	for _, p := range paths {
		b, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, b)
	}
	return out
}

// TestCommittedMergedHash_AgreesWithResolveEffective: the exporter's
// committed merged_hash (which includes the platform per-tenant layer) must
// describe the same merge /effective (config.ResolveEffective) serves for
// the same tree — on a cold load and after a reload that edits only a
// platform file's entry. Checked with the tree at rest (nothing written
// after the commit), so disk and commit are the same bytes.
func TestCommittedMergedHash_AgreesWithResolveEffective(t *testing.T) {
	t.Parallel()
	check := func(t *testing.T, m *ConfigManager, dir, where, tid string) {
		t.Helper()
		got, ok := committedTenantState(m, tid)
		if !ok {
			t.Fatalf("%s: %s is not in the committed hierarchy", where, tid)
		}
		pe, err := config.ResolveEffective(dir, tid)
		if err != nil {
			t.Fatalf("%s: ResolveEffective(%s): %v", where, tid, err)
		}
		if got.MergedHash != pe.MergedHash {
			t.Errorf("%s: committed merged_hash(%s) = %s, /effective = %s", where, tid, got.MergedHash, pe.MergedHash)
		}
		// The committed hash is the hash of /effective's own merge inputs
		// under the committed layers.
		h, err := config.ComputeMergedHash(readAll(t, got.SourceFile)[0], tid, readAll(t, got.DefaultsChain...), cachedLayers(m, tid))
		if err != nil || h != got.MergedHash {
			t.Errorf("%s: committed merged_hash(%s) %s is not the merge of its committed chain (%s, %v)", where, tid, got.MergedHash, h, err)
		}
		// The committed chain merges to the same map /effective resolved —
		// its MergedConfig, the leaf-by-leaf merge merged_hash hashes and
		// the view /effective reports is laid from (#2115). Compared as a
		// map, so a merge that differs only where the view hides it (a
		// shadowed spelling, a lower schedule's window) is still caught.
		wantCfg, _ := json.Marshal(pe.MergedConfig)
		gotCfg, err := config.ComputeEffectiveConfig(readAll(t, got.SourceFile)[0], tid, readAll(t, got.DefaultsChain...), cachedLayers(m, tid))
		if gotJSON, _ := json.Marshal(gotCfg); err != nil || string(gotJSON) != string(wantCfg) {
			t.Errorf("%s: the committed chain of %s merges to %s, /effective's merge = %s (%v)", where, tid, gotJSON, wantCfg, err)
		}
	}
	mx := loadOverlayMatrix(t)
	for _, tree := range mx.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, tree.Files)
			m, _ := newOverlayManager(t, dir)
			for tid, want := range tree.Expect {
				if want.Walker != nil {
					check(t, m, dir, "cold", tid)
				}
			}
		})
	}
	t.Run("after-platform-only-reload", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"60\"\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		})
		m, _ := newOverlayManager(t, dir)
		writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"55\"\n")
		touchTreeAt(t, dir, time.Now().Add(3*time.Second))
		if _, _, err := m.diffAndReload(); err != nil {
			t.Fatal(err)
		}
		check(t, m, dir, "reload", "tx")
	})
}

func TestReload_PlatformTenantsEdit_MovesMergedHash(t *testing.T) {
	t.Parallel()
	const (
		carrier  = "defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"60\"\n  ty:\n    mysql_connections: \"65\"\n"
		profiles = "tenants:\n  tx:\n    redis_memory_used_bytes: \"1\"\n"
	)
	f := func(v float64) *float64 { return &v }
	cases := []struct {
		name     string
		tenantTx string // tx.yaml body ("" = `tenants:\n  tx: {}`)
		file     string // the platform file the mutation rewrites
		body     string
		// per-tenant: does the cached merged_hash move?
		txMoves, tyMoves bool
		wantTxMetric     *float64
		wantReloaded     int
		wantNoOp         int
		wantEffect       string // "" = no (defaults, global, *) observation expected
	}{
		{
			name: "carrier entry for tx edited (the measured defect)",
			file: "_defaults.yaml", body: "defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n    mysql_connections: \"55\"\n  ty:\n    mysql_connections: \"65\"\n",
			txMoves: true, wantTxMetric: f(55),
			// ty's entry did not move but its chain file did: recomputed,
			// same hash, nothing changed for it → cosmetic.
			wantReloaded: 1, wantNoOp: 1, wantEffect: "applied",
		},
		{
			name: "non-chain platform file entry for tx edited",
			file: "_profiles.yaml", body: "tenants:\n  tx:\n    redis_memory_used_bytes: \"2\"\n",
			txMoves: true, wantTxMetric: f(60),
			wantReloaded: 1, wantNoOp: 0, wantEffect: "applied",
		},
		{
			name: "entry for tx added to a non-chain platform file",
			file: "_profiles.yaml", body: profiles + "    _silent_mode: warning\n",
			txMoves: true, wantTxMetric: f(60),
			wantReloaded: 1, wantNoOp: 0, wantEffect: "applied",
		},
		{
			name: "entry for tx removed from a non-chain platform file",
			file: "_profiles.yaml", body: "tenants: {}\n",
			txMoves: true, wantTxMetric: f(60),
			wantReloaded: 1, wantNoOp: 0, wantEffect: "applied",
		},
		{
			name:     "shadowed: platform value the tenant file overrides",
			tenantTx: "tenants:\n  tx:\n    redis_memory_used_bytes: \"9\"\n",
			file:     "_profiles.yaml", body: "tenants:\n  tx:\n    redis_memory_used_bytes: \"2\"\n",
			wantTxMetric: f(60),
			wantReloaded: 0, wantNoOp: 1, wantEffect: "shadowed",
		},
		{
			name: "only ty's entry edited: tx is not recomputed",
			file: "_profiles.yaml", body: profiles + "  ty:\n    redis_memory_used_bytes: \"3\"\n",
			tyMoves: true, wantTxMetric: f(60),
			wantReloaded: 1, wantNoOp: 0, wantEffect: "applied",
		},
		{
			// A CHAIN edit every tenant's platform entry overrides: the
			// platform layer shadows it just as a tenant-file key would.
			name: "shadowed by the platform layer: chain default both tenants' platform entries override",
			file: "_defaults.yaml", body: strings.Replace(carrier, "mysql_connections: 80", "mysql_connections: 90", 1),
			wantTxMetric: f(60),
			wantReloaded: 0, wantNoOp: 2, wantEffect: "shadowed",
		},
		{
			name: "control: comment-only edit of a platform file",
			file: "_profiles.yaml", body: "# reformatted\n" + profiles,
			wantTxMetric: f(60),
			wantReloaded: 0, wantNoOp: 0,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			txBody := tc.tenantTx
			if txBody == "" {
				txBody = "tenants:\n  tx: {}\n"
			}
			writeOverlayTree(t, dir, map[string]string{
				"_defaults.yaml": carrier,
				"_profiles.yaml": profiles,
				"tx.yaml":        txBody,
				"ty.yaml":        "tenants:\n  ty: {}\n",
			})
			fresh, _ := freshMetrics(t)
			m := NewConfigManagerWithDebounce(dir, 0)
			m.SetMetrics(fresh)
			m.SetLogger(log.New(io.Discard, "", 0))
			defer m.Close()
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			before := map[string]string{"tx": cachedMergedHash(m, "tx"), "ty": cachedMergedHash(m, "ty")}

			writeTestYAML(t, filepath.Join(dir, tc.file), tc.body)
			touchTreeAt(t, dir, time.Now().Add(3*time.Second))
			reloaded, noOp, err := m.diffAndReload()
			if err != nil {
				t.Fatalf("diffAndReload: %v", err)
			}
			assertServed(t, m, tc.name, "tx", tc.wantTxMetric)

			for tid, wantMove := range map[string]bool{"tx": tc.txMoves, "ty": tc.tyMoves} {
				after := cachedMergedHash(m, tid)
				if moved := after != before[tid]; moved != wantMove {
					t.Errorf("%s merged_hash moved=%v (%s → %s), want moved=%v", tid, moved, before[tid], after, wantMove)
				}
				pe, err := config.ResolveEffective(dir, tid)
				if err != nil {
					t.Fatalf("ResolveEffective(%s): %v", tid, err)
				}
				if after != pe.MergedHash {
					t.Errorf("%s: exporter merged_hash=%s, /effective merged_hash=%s — DIVERGED", tid, after, pe.MergedHash)
				}
			}
			if reloaded != tc.wantReloaded || noOp != tc.wantNoOp {
				t.Errorf("reloaded=%d noOp=%d, want %d/%d", reloaded, noOp, tc.wantReloaded, tc.wantNoOp)
			}
			if tc.wantEffect != "" {
				if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "global", tc.wantEffect); n != 1 {
					t.Errorf("blast-radius (defaults, global, %s) sampleCount = %d, want 1", tc.wantEffect, n)
				}
			}
			if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "unknown", "applied"); n != 0 {
				t.Errorf("blast-radius (defaults, unknown, applied) sampleCount = %d, want 0", n)
			}
		})
	}
}

// TestColdLoad_MergedHashIncludesPlatformOverlay: the cold path
// (populateHierarchyStateFrom → coldMergedHash) must hash the same merged
// config /effective serves, for every row of the shared matrix.
func TestColdLoad_MergedHashIncludesPlatformOverlay(t *testing.T) {
	t.Parallel()
	mx := loadOverlayMatrix(t)
	for _, tree := range mx.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, tree.Files)
			m, _ := newOverlayManager(t, dir)
			for tid, want := range tree.Expect {
				if want.Walker == nil {
					continue
				}
				pe, err := config.ResolveEffective(dir, tid)
				if err != nil {
					t.Fatalf("ResolveEffective(%s): %v", tid, err)
				}
				if got := cachedMergedHash(m, tid); got != pe.MergedHash {
					t.Errorf("%s: cold-load merged_hash=%s, /effective=%s", tid, got, pe.MergedHash)
				}
			}
		})
	}
}
