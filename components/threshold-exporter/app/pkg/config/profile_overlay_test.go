package config

// #2117: the walker plane's profile expansion picks the keys ApplyProfiles
// picks — the two edge rules the shared matrix does not carry: the #1231
// alias boundary (a tenant key in either spelling blocks the fill-in) and
// the #1189 declared-key exemption (a key declared in `optional_overrides`
// is never filled in, except the `_critical` shape). Each case checks the
// walker (ResolveEffective) against /metrics (LoadDir + Resolve).

import (
	"io"
	"log"
	"reflect"
	"testing"
)

// servedRows is every (component_metric/severity → value) /metrics serves for tx.
func servedRows(t *testing.T, dir string) map[string]float64 {
	t.Helper()
	cfg, _, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]float64{}
	for _, r := range cfg.Resolve() {
		if r.Tenant == "tx" {
			out[r.Component+"_"+r.Metric+"/"+r.Severity] = r.Value
		}
	}
	return out
}

func TestProfileExpansion_EdgeRulesMatchApplyProfiles(t *testing.T) {
	t.Parallel()
	// The deprecated spelling, taken from the alias table rather than
	// written out: the #1231 pre-commit hook forbids the retired key as a
	// live value, and this test must follow the table when its window closes.
	legacy, ok := legacySpellingFor("mysql_threads_running")
	if !ok {
		t.Skip("no deprecated spelling of mysql_threads_running is in its alias window")
	}
	cases := []struct {
		name        string
		files       map[string]string
		wantServed  map[string]float64 // /metrics rows the premise pins
		wantPresent map[string]any     // walker keys and values
		wantAbsent  []string           // walker keys the profile must NOT fill
		wantSources []ProfileOverlaySource
	}{
		{
			name: "legacy-spelling-in-tenant-blocks-canonical-fill",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 50\n",
				"_profiles.yaml": "profiles:\n  std:\n    mysql_threads_running: 40\n",
				"tx.yaml":        "tenants:\n  tx:\n    _profile: std\n    " + legacy + ": 45\n",
			},
			wantServed:  map[string]float64{"mysql_threads_running/warning": 45},
			wantPresent: map[string]any{legacy: 45},
			wantAbsent:  nil, // mysql_threads_running stays the chain's (walker is a raw view)
		},
		{
			name: "legacy-spelling-in-profile-lands-canonical",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 50\n",
				"_profiles.yaml": "profiles:\n  std:\n    " + legacy + ": 40\n",
				"tx.yaml":        "tenants:\n  tx:\n    _profile: std\n",
			},
			wantServed:  map[string]float64{"mysql_threads_running/warning": 40},
			wantPresent: map[string]any{"mysql_threads_running": 40},
			wantAbsent:  []string{legacy},
			wantSources: []ProfileOverlaySource{{Profile: "std", File: "_profiles.yaml", Keys: []string{"mysql_threads_running"}}},
		},
		{
			name: "declared-key-is-not-filled-but-its-critical-twin-is",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides:\n  - redis_x\n",
				"_profiles.yaml": "profiles:\n  std:\n    redis_x: 5\n    redis_x_critical: 9\n    mysql_connections: 60\n",
				"tx.yaml":        "tenants:\n  tx:\n    _profile: std\n",
			},
			wantServed:  map[string]float64{"mysql_connections/warning": 60},
			wantPresent: map[string]any{"mysql_connections": 60, "redis_x_critical": 9},
			wantAbsent:  []string{"redis_x"},
			wantSources: []ProfileOverlaySource{{Profile: "std", File: "_profiles.yaml", Keys: []string{"mysql_connections", "redis_x_critical"}}},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := writeSimProfileTree(t, tc.files)
			served := servedRows(t, dir)
			for k, v := range tc.wantServed {
				if served[k] != v {
					t.Fatalf("premise: /metrics %s = %v, want %v (all rows %v)", k, served[k], v, served)
				}
			}
			ec, err := ResolveEffective(dir, "tx")
			if err != nil {
				t.Fatal(err)
			}
			for k, v := range tc.wantPresent {
				if got := ec.EffectiveConfig[k]; got != v {
					t.Errorf("walker %s = %v, want %v (%v)", k, got, v, ec.EffectiveConfig)
				}
			}
			for _, k := range tc.wantAbsent {
				if got, ok := ec.EffectiveConfig[k]; ok {
					t.Errorf("walker carries %s = %v, which /metrics does not fill in", k, got)
				}
			}
			if !reflect.DeepEqual(ec.ProfileOverlay, tc.wantSources) {
				t.Errorf("profile_overlay = %+v, want %+v", ec.ProfileOverlay, tc.wantSources)
			}
		})
	}
}
