package config

// effective_view_test.go — #2115 F1 / F4: the effective config the walker
// reports (/effective, `da-guard effective`) is what /metrics serves, per
// threshold. Every row is checked on BOTH planes: the walker's
// effective_config (+ key_sources) and the row LoadDir → ResolveAt serves at
// 23:00 UTC (inside the 22:00-06:00 window the F4 rows write), so a row
// cannot pin a walker answer /metrics disagrees with.
//
// Measured on main fc5438c4 (before effectiveView): every row marked
// `diverged` below gave a different walker answer — both spellings
// ({mysql_cpu: 40, mysql_threads_running: 30} etc.) for F1, the subtree's
// window merged into the tenant's schedule for F4. The rows not marked are
// controls that held on main too.

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"sort"
	"testing"
	"time"
)

// effectiveViewAt is inside the F4 rows' 22:00-06:00 window.
var effectiveViewAt = time.Date(2026, 7, 1, 23, 0, 0, 0, time.UTC)

func TestEffectiveView_MatchesMetricsPerThreshold(t *testing.T) {
	t.Parallel()
	const subtreeSchedule = "defaults:\n  pg_connections:\n    default: \"150\"\n" +
		"    overrides:\n      - window: \"22:00-06:00\"\n        value: \"300\"\n"
	cases := []struct {
		name     string
		files    map[string]string
		diverged bool // main fc5438c4 reported something else
		// effective is the walker's threshold keys (no `_` key), as JSON.
		effective string
		// key / source: the one threshold key and the layer it is credited to.
		key, layer, file string
		level            int // -1: not the defaults layer
		// rows is what /metrics serves for tx at effectiveViewAt (a
		// mysql_threads_running value is served with its legacy twin,
		// metric "cpu", at the same number).
		row []string
	}{
		// ── F1: one threshold, two spellings, across layers ──
		{
			name: "f1-subtree-legacy-over-root-canonical", diverged: true,
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  mysql_threads_running: 30\n",
				"sub/_defaults.yaml": "defaults:\n  mysql_cpu: 40\n",
				"sub/tx.yaml":        "tenants:\n  tx:\n    _silent_mode: disable\n",
			},
			effective: `{"mysql_cpu":40}`, key: "mysql_cpu",
			layer: KeyLayerDefaults, file: "sub/_defaults.yaml", level: 1,
			row: twin("40"),
		},
		{
			name: "f1-control-subtree-canonical-over-root-canonical",
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  mysql_threads_running: 30\n",
				"sub/_defaults.yaml": "defaults:\n  mysql_threads_running: 40\n",
				"sub/tx.yaml":        "tenants:\n  tx:\n    _silent_mode: disable\n",
			},
			effective: `{"mysql_threads_running":40}`, key: "mysql_threads_running",
			layer: KeyLayerDefaults, file: "sub/_defaults.yaml", level: 1,
			row: twin("40"),
		},
		{
			name: "f1-tenant-legacy-over-root-canonical", diverged: true,
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_cpu: \"60\"\n",
			},
			effective: `{"mysql_cpu":"60"}`, key: "mysql_cpu",
			layer: KeyLayerTenant, file: "tx.yaml", level: -1,
			row: twin("60"),
		},
		{
			name: "f1-control-tenant-canonical-over-root-canonical",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_threads_running: \"60\"\n",
			},
			effective: `{"mysql_threads_running":"60"}`, key: "mysql_threads_running",
			layer: KeyLayerTenant, file: "tx.yaml", level: -1,
			row: twin("60"),
		},
		{
			name: "f1-tenant-canonical-over-subtree-legacy", diverged: true,
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  mysql_threads_running: 30\n",
				"sub/_defaults.yaml": "defaults:\n  mysql_cpu: 40\n",
				"sub/tx.yaml":        "tenants:\n  tx:\n    mysql_threads_running: \"50\"\n",
			},
			effective: `{"mysql_threads_running":"50"}`, key: "mysql_threads_running",
			layer: KeyLayerTenant, file: "sub/tx.yaml", level: -1,
			row: twin("50"),
		},
		// One layer writing both spellings: the canonical one wins there.
		{
			name: "f1-subtree-writes-both-spellings", diverged: true,
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  mysql_threads_running: 30\n",
				"sub/_defaults.yaml": "defaults:\n  mysql_cpu: 40\n  mysql_threads_running: 45\n",
				"sub/tx.yaml":        "tenants:\n  tx:\n    _silent_mode: disable\n",
			},
			effective: `{"mysql_threads_running":45}`, key: "mysql_threads_running",
			layer: KeyLayerDefaults, file: "sub/_defaults.yaml", level: 1,
			row: twin("45"),
		},
		{
			name: "f1-tenant-writes-both-spellings", diverged: true,
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_cpu: \"60\"\n    mysql_threads_running: \"65\"\n",
			},
			effective: `{"mysql_threads_running":"65"}`, key: "mysql_threads_running",
			layer: KeyLayerTenant, file: "tx.yaml", level: -1,
			row: twin("65"),
		},
		// A canonical spelling written as null shadows nothing (#2418): the
		// level's legacy value is what it hands down.
		{
			name: "f1-subtree-canonical-null-beside-legacy", diverged: true,
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  mysql_threads_running: 30\n",
				"sub/_defaults.yaml": "defaults:\n  mysql_cpu: 40\n  mysql_threads_running: null\n",
				"sub/tx.yaml":        "tenants:\n  tx:\n    _silent_mode: disable\n",
			},
			effective: `{"mysql_cpu":40}`, key: "mysql_cpu",
			layer: KeyLayerDefaults, file: "sub/_defaults.yaml", level: 1,
			row: twin("40"),
		},
		// ── F4: a schedule is one value, laid whole ──
		{
			name: "f4-tenant-default-over-subtree-schedule", diverged: true,
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  pg_connections: 100\n",
				"sub/_defaults.yaml": subtreeSchedule,
				"sub/tx.yaml":        "tenants:\n  tx:\n    pg_connections:\n      default: \"160\"\n",
			},
			effective: `{"pg_connections":{"default":"160"}}`, key: "pg_connections",
			layer: KeyLayerTenant, file: "sub/tx.yaml", level: -1,
			row: []string{"pg_connections{}=160/warning"},
		},
		{
			name: "f4-control-tenant-default-over-platform-schedule",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  pg_connections: 100\ntenants:\n  tx:\n" +
					"    pg_connections:\n      default: \"150\"\n" +
					"      overrides:\n        - window: \"22:00-06:00\"\n          value: \"300\"\n",
				"sub/tx.yaml": "tenants:\n  tx:\n    pg_connections:\n      default: \"160\"\n",
			},
			effective: `{"pg_connections":{"default":"160"}}`, key: "pg_connections",
			layer: KeyLayerTenant, file: "sub/tx.yaml", level: -1,
			row: []string{"pg_connections{}=160/warning"},
		},
		{
			name: "f4-deeper-subtree-default-over-shallower-schedule", diverged: true,
			files: map[string]string{
				"_defaults.yaml":          "defaults:\n  pg_connections: 100\n",
				"sub/_defaults.yaml":      subtreeSchedule,
				"sub/deep/_defaults.yaml": "defaults:\n  pg_connections:\n    default: \"170\"\n",
				"sub/deep/tx.yaml":        "tenants:\n  tx:\n    _silent_mode: disable\n",
			},
			effective: `{"pg_connections":{"default":"170"}}`, key: "pg_connections",
			layer: KeyLayerDefaults, file: "sub/deep/_defaults.yaml", level: 2,
			row: []string{"pg_connections{}=170/warning"},
		},
		// The winning layer's own windows stay: the view drops what a layer
		// below wrote, never what the winner wrote.
		{
			name: "f4-control-subtree-schedule-inherited-whole",
			files: map[string]string{
				"_defaults.yaml":     "defaults:\n  pg_connections: 100\n",
				"sub/_defaults.yaml": subtreeSchedule,
				"sub/tx.yaml":        "tenants:\n  tx:\n    _silent_mode: disable\n",
			},
			effective: `{"pg_connections":{"default":"150","overrides":[{"value":"300","window":"22:00-06:00"}]}}`,
			key:       "pg_connections",
			layer:     KeyLayerDefaults, file: "sub/_defaults.yaml", level: 1,
			row: []string{"pg_connections{}=300/warning"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)

			flat, _, err := LoadDir(dir, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatal(err)
			}
			if rows := servedRowsAt(flat, "tx", effectiveViewAt); fmt.Sprint(rows) != fmt.Sprint(tc.row) {
				t.Errorf("/metrics serves %v, want %v", rows, tc.row)
			}

			scoped, err := EffectiveTree(dir)
			if err != nil {
				t.Fatal(err)
			}
			if len(scoped.Tenants) != 1 {
				t.Fatalf("tenants %d, want 1", len(scoped.Tenants))
			}
			ec := scoped.Tenants[0]
			if got := thresholdKeysJSON(t, ec.EffectiveConfig); got != tc.effective {
				t.Errorf("walker effective_config = %s, want %s", got, tc.effective)
			}
			src, ok := ec.KeySources[tc.key]
			switch {
			case !ok:
				t.Errorf("key_sources has no %s: %+v", tc.key, ec.KeySources)
			case src.Layer != tc.layer || src.File != tc.file:
				t.Errorf("key_sources[%s] = %s %s, want %s %s", tc.key, src.Layer, src.File, tc.layer, tc.file)
			case tc.level >= 0 && (src.Level == nil || *src.Level != tc.level):
				t.Errorf("key_sources[%s].level = %v, want %d", tc.key, src.Level, tc.level)
			}

			// /effective (ResolveEffective) is the same answer.
			one, err := ResolveEffective(dir, "tx")
			if err != nil {
				t.Fatal(err)
			}
			if got := thresholdKeysJSON(t, one.EffectiveConfig); got != tc.effective {
				t.Errorf("ResolveEffective effective_config = %s, want %s", got, tc.effective)
			}
		})
	}
}

// TestEffectiveView_MergedHashStaysTheMerges pins the split effectiveView
// documents: merged_hash is the hash of the deepMerge result — the
// exporter's merged_hash, describe_tenant.py's — not of the reported view.
// The F1 tree's view ({mysql_cpu: 40}) hashes differently from its merge
// ({mysql_cpu: 40, mysql_threads_running: 30}), so a merged_hash that had
// followed the view fails here.
func TestEffectiveView_MergedHashStaysTheMerges(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_threads_running: 30\n"
	sub := "defaults:\n  mysql_cpu: 40\n"
	tenant := "tenants:\n  tx:\n    _silent_mode: disable\n"
	dir := t.TempDir()
	writeMergeTree(t, dir, map[string]string{
		"_defaults.yaml": root, "sub/_defaults.yaml": sub, "sub/tx.yaml": tenant,
	})
	ec, err := ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	merge, err := ComputeMergedHash([]byte(tenant), "tx", [][]byte{[]byte(root), []byte(sub)})
	if err != nil {
		t.Fatal(err)
	}
	if ec.MergedHash != merge {
		t.Errorf("merged_hash = %s, want the merge's %s", ec.MergedHash, merge)
	}
	view, err := mergedHashOf(ec.EffectiveConfig)
	if err != nil {
		t.Fatal(err)
	}
	if view == merge {
		t.Errorf("the view hashes like the merge (%s): this tree no longer tells the two apart", view)
	}
}

// twin is the two rows /metrics serves for a mysql_threads_running value v:
// the canonical metric and its legacy twin (sorted, as servedRowsAt is).
func twin(v string) []string {
	return []string{"mysql_cpu{}=" + v + "/warning", "mysql_threads_running{}=" + v + "/warning"}
}

// servedRowsAt is every row /metrics serves for tenantID at `at`, rendered
// like resolvedRows.
func servedRowsAt(c *ThresholdConfig, tenantID string, at time.Time) []string {
	var out []string
	for _, r := range c.ResolveAt(at) {
		if r.Tenant != tenantID {
			continue
		}
		out = append(out, fmt.Sprintf("%s_%s{}=%g/%s", r.Component, r.Metric, r.Value, r.Severity))
	}
	sort.Strings(out)
	return out
}

// thresholdKeysJSON is m without its `_` keys, as JSON (sorted keys).
func thresholdKeysJSON(t *testing.T, m map[string]any) string {
	t.Helper()
	th := map[string]any{}
	for k, v := range m {
		if len(k) > 0 && k[0] != '_' {
			th[k] = v
		}
	}
	b, err := json.Marshal(th)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
