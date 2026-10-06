package config

// platform_alias_layer_test.go — #2368: a root platform file's `tenants:`
// entry and the tenant's own file stack per THRESHOLD, not per spelling, on
// all three planes that apply that layer:
//
//   - /metrics (the flat merge: LoadDir → ResolveAt; mergePartialInto),
//   - the walker (ResolveEffective → /effective, da-guard, merged_hash;
//     overlayTenant), whose platform_overlay attribution names the
//     CANONICAL spelling (owner ruling E9, the key /metrics serves),
//   - the tenant-api merge core (MergeTenantWithRootDefaults; supplyFor).
//
// Measured before the fix on the first row: /metrics and the tenant-api core
// served the platform's 70 over the tenant's own `mysql_cpu: 90`, and the
// walker attributed `mysql_threads_running` to `_defaults.yaml`.

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestPlatformTenantsLayerStacksAcrossAliasSpellings(t *testing.T) {
	t.Parallel()
	const carrier = "defaults:\n  mysql_threads_running: 30\n"
	cases := []struct {
		name  string
		files map[string]string
		// rows is every mysql row /metrics must serve for tx (canonical and
		// legacy twin carry the same value).
		rows []string
		// effective is the walker's mysql_* keys — one per threshold, in the
		// winning layer's spelling (#2115); overlay its attribution.
		effective map[string]any
		overlay   []PlatformOverlaySource
	}{
		{
			name: "tenant-legacy-over-platform-canonical",
			files: map[string]string{
				"_defaults.yaml": carrier + "tenants:\n  tx:\n    mysql_threads_running: 70\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_cpu: \"90\"\n",
			},
			rows:      []string{"mysql_threads_running{}=90/warning", "mysql_cpu{}=90/warning"},
			effective: map[string]any{"mysql_cpu": "90"}, // #2115: one threshold, one key
		},
		{
			name: "tenant-canonical-over-platform-legacy",
			files: map[string]string{
				"_defaults.yaml": carrier + "tenants:\n  tx:\n    mysql_cpu: 70\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_threads_running: \"90\"\n",
			},
			rows:      []string{"mysql_threads_running{}=90/warning", "mysql_cpu{}=90/warning"},
			effective: map[string]any{"mysql_threads_running": "90"},
		},
		{
			name: "control-same-spelling",
			files: map[string]string{
				"_defaults.yaml": carrier + "tenants:\n  tx:\n    mysql_threads_running: 70\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_threads_running: \"90\"\n",
			},
			rows:      []string{"mysql_threads_running{}=90/warning", "mysql_cpu{}=90/warning"},
			effective: map[string]any{"mysql_threads_running": "90"},
		},
		{
			// The platform alone writes the legacy spelling: its value is
			// served, and the attribution names the canonical key.
			name: "platform-legacy-only-attributed-canonical",
			files: map[string]string{
				"_defaults.yaml": carrier + "tenants:\n  tx:\n    mysql_cpu: 70\n",
				"tx.yaml":        "tenants:\n  tx:\n    redis_x: \"1\"\n",
			},
			rows:      []string{"mysql_threads_running{}=70/warning", "mysql_cpu{}=70/warning"},
			effective: map[string]any{"mysql_cpu": 70.0},
			overlay:   []PlatformOverlaySource{{File: "_defaults.yaml", Keys: []string{"mysql_threads_running"}}},
		},
		{
			// Two platform files: the later one (sort order) wins per
			// threshold, whichever spelling each uses.
			name: "later-platform-file-legacy-over-earlier-canonical",
			files: map[string]string{
				"_defaults.yaml": carrier,
				"_a.yaml":        "tenants:\n  tx:\n    mysql_threads_running: 60\n",
				"_b.yaml":        "tenants:\n  tx:\n    mysql_cpu: 70\n",
				"tx.yaml":        "tenants:\n  tx:\n    redis_x: \"1\"\n",
			},
			rows:      []string{"mysql_threads_running{}=70/warning", "mysql_cpu{}=70/warning"},
			effective: map[string]any{"mysql_cpu": 70.0},
			overlay:   []PlatformOverlaySource{{File: "_b.yaml", Keys: []string{"mysql_threads_running"}}},
		},
		{
			// The `_critical` shape canonicalizes too (canonicalKeyFor's
			// suffix derivation): the tenant's legacy critical key wins.
			name: "tenant-legacy-critical-over-platform-canonical-critical",
			files: map[string]string{
				"_defaults.yaml": carrier + "tenants:\n  tx:\n    mysql_threads_running_critical: 95\n",
				"tx.yaml":        "tenants:\n  tx:\n    mysql_cpu_critical: \"99\"\n",
			},
			rows: []string{
				"mysql_threads_running{}=30/warning", "mysql_cpu{}=30/warning",
				"mysql_threads_running{}=99/critical", "mysql_cpu{}=99/critical",
			},
			effective: map[string]any{"mysql_cpu_critical": "99", "mysql_threads_running": 30.0},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)

			flat, _, err := LoadDir(dir, nil)
			if err != nil {
				t.Fatal(err)
			}
			metrics := mysqlRows(resolvedRows(flat, "tx"))
			if !sameRowSet(metrics, tc.rows) {
				t.Errorf("/metrics serves %v, want %v", metrics, tc.rows)
			}

			m := MergeTenantWithRootDefaults(dir, "tx", []byte(tc.files["tx.yaml"]))
			if api := mysqlRows(resolvedRows(&m, "tx")); !sameRowSet(api, tc.rows) {
				t.Errorf("tenant-api merge core serves %v, want %v", api, tc.rows)
			}

			ec, err := ResolveEffective(dir, "tx")
			if err != nil {
				t.Fatal(err)
			}
			raw, _ := json.Marshal(ec.EffectiveConfig)
			var eff map[string]any
			if err := json.Unmarshal(raw, &eff); err != nil {
				t.Fatal(err)
			}
			got := map[string]any{}
			for k, v := range eff {
				if strings.HasPrefix(k, "mysql_") {
					got[k] = v
				}
			}
			if !reflect.DeepEqual(got, tc.effective) {
				t.Errorf("walker effective mysql_* = %v, want %v", got, tc.effective)
			}
			if !reflect.DeepEqual(ec.PlatformOverlay, tc.overlay) {
				t.Errorf("walker platform_overlay = %+v, want %+v", ec.PlatformOverlay, tc.overlay)
			}
		})
	}
}

// TestOverlayAcrossSpellingsKeepsOneLayersOwnPair: when ONE layer writes both
// spellings, neither removes the other — the canonical-wins dedup inside that
// layer decides, as before #2368 — while an earlier layer's spelling goes.
func TestOverlayAcrossSpellingsKeepsOneLayersOwnPair(t *testing.T) {
	t.Parallel()
	dst := map[string]int{"mysql_cpu": 1, "mysql_threads_running_critical": 2, "redis_x": 3}
	overlayAcrossSpellings(dst, map[string]int{"mysql_cpu": 10, "mysql_threads_running": 20, "mysql_cpu_critical": 30})
	want := map[string]int{"mysql_cpu": 10, "mysql_threads_running": 20, "mysql_cpu_critical": 30, "redis_x": 3}
	if !reflect.DeepEqual(dst, want) {
		t.Errorf("got %v, want %v", dst, want)
	}
}

// TestTouchesAliasIsExactlyTheAliasShapes: touchesAlias (otherSpellings'
// fast reject, #2420 bench gate) says "no" only for keys the full lookup
// finds no other spelling for, and "yes" for every shape it does — so the
// reject never changes an answer.
func TestTouchesAliasIsExactlyTheAliasShapes(t *testing.T) {
	t.Parallel()
	slow := func(key string) []string {
		canon, _ := canonicalKeyFor(key)
		var out []string
		if canon != key {
			out = append(out, canon)
		}
		if legacy, ok := legacySpellingFor(canon); ok && legacy != key {
			out = append(out, legacy)
		}
		return out
	}
	var keys []string
	for legacy, canon := range deprecatedKeyAliases {
		for _, base := range []string{legacy, canon} {
			keys = append(keys, base, base+"_critical", base+`{version="v2"}`, base+"{}",
				base+"_util", base+"_critical_x", base+`_critical{a="b"}`, "x"+base, base+"_")
		}
	}
	keys = append(keys, "", "_routing", "_critical", "{a}", "mysql_connections", "pg_connections_critical",
		`redis_x{a="b"}`, "_state_x_critical", "_silent_x{a}")
	for _, k := range keys {
		var buf [2]string
		fast := otherSpellings(k, &buf)
		want := slow(k)
		if len(fast) != len(want) {
			t.Errorf("otherSpellings(%q) = %q, full lookup %q", k, fast, want)
			continue
		}
		for i := range want {
			if fast[i] != want[i] {
				t.Errorf("otherSpellings(%q) = %q, full lookup %q", k, fast, want)
			}
		}
		if (len(want) > 0) != touchesAlias(k) {
			t.Errorf("touchesAlias(%q) = %v, but the full lookup finds %q", k, touchesAlias(k), want)
		}
	}
}

func mysqlRows(rows []string) []string {
	var out []string
	for _, r := range rows {
		if strings.HasPrefix(r, "mysql_") {
			out = append(out, r)
		}
	}
	return out
}

func sameRowSet(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for _, w := range want {
		if !containsRow(got, w) {
			return false
		}
	}
	return true
}
