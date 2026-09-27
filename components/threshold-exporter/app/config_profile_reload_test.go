package main

// #2117 — the exporter's merged_hash includes the profile a tenant elects
// (`_profile`; /metrics fills its keys in, ApplyProfiles), and a reload that
// edits ONLY a root platform file's `profiles:` block recomputes the
// merged_hash of exactly the tenants on a changed profile, attributed like
// an edit of a platform `tenants:` entry (TestReload_PlatformTenantsEdit_
// MovesMergedHash): reason defaults, scope global, effect applied /
// shadowed / cosmetic.
//
// Measured on main 9079583d before the fix: editing only std's value in
// `_profiles.yaml` moved /metrics 60 → 50 while tx's merged_hash stayed put
// and the reload counted neither reloaded nor no-op.
//
// Seams: metrics via freshMetrics + SetMetrics, logger via SetLogger.

import (
	"io"
	"log"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestReload_ProfileEdit_MovesMergedHash(t *testing.T) {
	t.Parallel()
	const (
		carrier  = "defaults:\n  mysql_connections: 80\n  pg_connections: 100\n"
		profiles = "profiles:\n  std:\n    mysql_connections: 60\n  alt:\n    mysql_connections: 55\n  unused:\n    mysql_connections: 40\n"
		onStd    = "tenants:\n  tx:\n    _profile: std\n"
	)
	f := func(v float64) *float64 { return &v }
	cases := []struct {
		name      string
		carrier   string // _defaults.yaml ("" = carrier)
		tenantTx  string // tx.yaml ("" = onStd)
		file      string // the file the mutation rewrites
		body      string
		txMoves   bool
		wantTx    *float64
		reloaded  int
		noOp      int
		effect    string // "" = no (defaults, global, *) observation expected
		effectCnt int
	}{
		{
			name: "profile value edited (the measured defect)",
			file: "_profiles.yaml", body: strings.Replace(profiles, "mysql_connections: 60", "mysql_connections: 50", 1),
			txMoves: true, wantTx: f(50), reloaded: 1, noOp: 0, effect: "applied", effectCnt: 1,
		},
		{
			name: "edit to a profile no tenant elects: nothing recomputed",
			file: "_profiles.yaml", body: strings.Replace(profiles, "mysql_connections: 40", "mysql_connections: 45", 1),
			wantTx: f(60), reloaded: 0, noOp: 0,
		},
		{
			name:     "shadowed: the tenant file sets the changed key",
			tenantTx: "tenants:\n  tx:\n    _profile: std\n    mysql_connections: 70\n",
			file:     "_profiles.yaml", body: strings.Replace(profiles, "mysql_connections: 60", "mysql_connections: 50", 1),
			wantTx: f(70), reloaded: 0, noOp: 1, effect: "shadowed", effectCnt: 1,
		},
		{
			name:    "shadowed: the platform entry sets the changed key",
			carrier: carrier + "tenants:\n  tx:\n    mysql_connections: 65\n",
			file:    "_profiles.yaml", body: strings.Replace(profiles, "mysql_connections: 60", "mysql_connections: 50", 1),
			wantTx: f(65), reloaded: 0, noOp: 1, effect: "shadowed", effectCnt: 1,
		},
		{
			name:     "profile the tenant elects is added",
			tenantTx: "tenants:\n  tx:\n    _profile: newp\n",
			file:     "_profiles.yaml", body: profiles + "  newp:\n    mysql_connections: 45\n",
			txMoves: true, wantTx: f(45), reloaded: 1, noOp: 0, effect: "applied", effectCnt: 1,
		},
		{
			name: "profile the tenant elects is removed",
			file: "_profiles.yaml", body: "profiles:\n  alt:\n    mysql_connections: 55\n  unused:\n    mysql_connections: 40\n",
			txMoves: true, wantTx: f(80), reloaded: 1, noOp: 0, effect: "applied", effectCnt: 1,
		},
		{
			name:     "_profile switched by the platform entry",
			carrier:  carrier + "tenants:\n  tx:\n    _profile: std\n",
			tenantTx: "tenants:\n  tx: {}\n",
			file:     "_defaults.yaml", body: carrier + "tenants:\n  tx:\n    _profile: alt\n",
			// ty's chain file moved too: recomputed, same hash → cosmetic.
			txMoves: true, wantTx: f(55), reloaded: 1, noOp: 1, effect: "applied", effectCnt: 1,
		},
		{
			name:    "profile carried by the chain carrier edited",
			carrier: carrier + "profiles:\n  std:\n    pg_connections: 90\n",
			file:    "_defaults.yaml", body: carrier + "profiles:\n  std:\n    pg_connections: 95\n",
			txMoves: true, wantTx: f(60), reloaded: 1, noOp: 1, effect: "applied", effectCnt: 1,
		},
		{
			name: "control: comment-only edit of the profiles file",
			file: "_profiles.yaml", body: "# reformatted\n" + profiles,
			wantTx: f(60), reloaded: 0, noOp: 0,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			c, tx := tc.carrier, tc.tenantTx
			if c == "" {
				c = carrier
			}
			if tx == "" {
				tx = onStd
			}
			writeOverlayTree(t, dir, map[string]string{
				"_defaults.yaml": c,
				"_profiles.yaml": profiles,
				"tx.yaml":        tx,
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
			assertServed(t, m, tc.name, "tx", tc.wantTx)

			for tid, wantMove := range map[string]bool{"tx": tc.txMoves, "ty": false} {
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
			if reloaded != tc.reloaded || noOp != tc.noOp {
				t.Errorf("reloaded=%d noOp=%d, want %d/%d", reloaded, noOp, tc.reloaded, tc.noOp)
			}
			for _, effect := range []string{"applied", "shadowed", "cosmetic"} {
				want := 0
				if effect == tc.effect {
					want = tc.effectCnt
				}
				if effect == "cosmetic" && tc.effect == "applied" {
					want = tc.noOp // ty's recomputed-but-unchanged chain tenant
				}
				if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "global", effect); int(n) != want {
					t.Errorf("blast-radius (defaults, global, %s) sampleCount = %d, want %d", effect, n, want)
				}
			}
			if n, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "unknown", "applied"); n != 0 {
				t.Errorf("blast-radius (defaults, unknown, applied) sampleCount = %d, want 0", n)
			}
		})
	}
}
