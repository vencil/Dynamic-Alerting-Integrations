package config

// reserved_null_sunset_test.go — #3341 (ruling B′), on the production paths:
// LoadDir (the exporter's build) → ResolveSeverityDedup / ResolveSilentModesAt
// / ResolveStateFiltersAt, beside ResolveEffective (/effective), asserting
// that what /effective shows of each reserved key is what is served.
//
//  1. A subtree `_defaults.yaml` does not carry a reserved key: whatever it
//     writes — `disable`, a number, a word the overlay never applied, a null
//     in any spelling — the tenant is served exactly what it is served with
//     no subtree value, /effective does not show the value, and not_served
//     names the file (value_rejected) for every non-null write.
//  2. A tenant-side null of a reserved key deletes the inherited value: the
//     tenant gets the platform default — dedup on, nothing silenced, a state
//     filter per its default_state — whether the null is written `key:`,
//     `~` or `{default: null}`, and whatever it displaces (a root platform
//     `tenants:` entry, a profile). Before #3341 the null was a present `""`:
//     `_state_*` then turned ON even under `default_state: disable`.
//
// Seams: none — t.TempDir() trees (writeUndeliverableTree).

import (
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

var rnNow = time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)

// rnRoot is a root `_defaults.yaml` declaring the maintenance filter with the
// given default_state.
func rnRoot(defaultState string) string {
	return "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: " + defaultState + "\n"
}

// rnServed is what the resolvers serve t1 for key: the reading a person
// checks (dedup on / silenced severities / filter on).
func rnServed(t *testing.T, cfg *ThresholdConfig, key string) string {
	t.Helper()
	switch key {
	case "_severity_dedup":
		for _, r := range cfg.ResolveSeverityDedup() {
			if r.Tenant == "t1" {
				return "dedup=" + r.Mode
			}
		}
		return "dedup=off"
	case "_silent_mode":
		var sev []string
		for _, r := range cfg.resolveSilentModesAt(rnNow, nil) {
			if r.Tenant == "t1" {
				sev = append(sev, r.TargetSeverity)
			}
		}
		sort.Strings(sev)
		return fmt.Sprintf("silenced=%v", sev)
	default:
		for _, r := range cfg.resolveStateFiltersAt(rnNow, nil) {
			if r.Tenant == "t1" && "_state_"+r.FilterName == key {
				return "filter=on"
			}
		}
		return "filter=off"
	}
}

// rnLoad is LoadDir over files (no file may be dropped) and t1's /effective.
func rnLoad(t *testing.T, files map[string]string) (*ThresholdConfig, *EffectiveConfig) {
	t.Helper()
	dir := writeUndeliverableTree(t, files)
	cfg, failed, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil || len(failed) != 0 {
		t.Fatalf("LoadDir: %v, dropped %v", err, failed)
	}
	ec, err := ResolveEffective(dir, "t1")
	if err != nil {
		t.Fatalf("ResolveEffective: %v", err)
	}
	return cfg, ec
}

// rnNullSpellings is every way a value is written as null.
var rnNullSpellings = map[string]string{"bare": "", "tilde": " ~", "default-null": " {default: null}"}

// rnKeys is the reserved keys whose readers part "" from "no key", each with
// the root maintenance filter's default_state it is checked under (it moves
// only `_state_maintenance`).
var rnKeys = []struct{ key, defaultState string }{
	{"_severity_dedup", "enable"},
	{"_silent_mode", "enable"},
	{"_state_maintenance", "enable"},
	{"_state_maintenance", "disable"},
}

func TestLoadDir_SubtreeReservedKeyIsNotCarried(t *testing.T) {
	t.Parallel()
	values := map[string]string{
		"disable":  " disable", // applied until #3341
		"number":   " 1",       // applied until #3341
		"word":     " enable",  // never applied
		"severity": " warning",
	}
	for k, v := range rnNullSpellings {
		values["null-"+k] = v
	}
	for _, rk := range rnKeys {
		root := rnRoot(rk.defaultState)
		base := map[string]string{"_defaults.yaml": root, "team/sub/t1.yaml": "tenants:\n  t1: {}\n"}
		wantCfg, wantEC := rnLoad(t, base)
		want := rnServed(t, wantCfg, rk.key)
		if _, shown := wantEC.EffectiveConfig[rk.key]; shown {
			t.Fatalf("control: /effective shows %s with no layer writing it", rk.key)
		}
		for name, v := range values {
			t.Run(rk.key+"/"+rk.defaultState+"/"+name, func(t *testing.T) {
				t.Parallel()
				files := map[string]string{
					"_defaults.yaml":      root,
					"team/_defaults.yaml": "defaults:\n  mysql_connections: 70\n  " + rk.key + ":" + v + "\n",
					"team/sub/t1.yaml":    "tenants:\n  t1: {}\n",
				}
				cfg, ec := rnLoad(t, files)
				if got := rnServed(t, cfg, rk.key); got != want {
					t.Errorf("served %s, want %s (as with no subtree value)", got, want)
				}
				if sv, in := cfg.Tenants["t1"][rk.key]; in {
					t.Errorf("tenant map holds %s = %+v: the overlay applied it", rk.key, sv)
				}
				if got, shown := ec.EffectiveConfig[rk.key]; shown {
					t.Errorf("/effective shows %s = %#v; /metrics serves no subtree value", rk.key, got)
				}
				ns, named := ec.NotServed[rk.key]
				if strings.HasPrefix(name, "null-") {
					if named {
						t.Errorf("not_served names a null (it writes nothing): %+v", ns)
					}
					return
				}
				if wantNS := (NotServedKey{Reason: NotServedValueRejected, File: "team/_defaults.yaml"}); ns != wantNS {
					t.Errorf("not_served[%s] = %+v, want %+v", rk.key, ns, wantNS)
				}
			})
		}
	}
}

func TestLoadDir_TenantReservedNullIsThePlatformDefault(t *testing.T) {
	t.Parallel()
	// What the tenant side writes as null displaces; "" = nothing below.
	displaced := map[string]func(key, value string) map[string]string{
		"nothing": func(string, string) map[string]string { return nil },
		// a root platform file's `tenants:` entry
		"platform-entry": func(key, value string) map[string]string {
			return map[string]string{"_platform.yaml": "tenants:\n  t1:\n    " + key + ": " + value + "\n"}
		},
		// a profile the tenant elects
		"profile": func(key, value string) map[string]string {
			return map[string]string{"_profiles.yaml": "profiles:\n  p1:\n    " + key + ": " + value + "\n"}
		},
		// a subtree level (not carried anyway since #3341)
		"subtree": func(key, value string) map[string]string {
			return map[string]string{"team/_defaults.yaml": "defaults:\n  " + key + ": " + value + "\n"}
		},
	}
	for _, rk := range rnKeys {
		root := rnRoot(rk.defaultState)
		baseCfg, _ := rnLoad(t, map[string]string{"_defaults.yaml": root, "team/t1.yaml": "tenants:\n  t1: {}\n"})
		want := rnServed(t, baseCfg, rk.key) // the platform default
		// The value below the null: one that moves the reading off the
		// platform default either way.
		below := map[string]string{"_severity_dedup": "disable", "_silent_mode": "warning"}[rk.key]
		if below == "" {
			below = map[string]string{"enable": "disable", "disable": "enable"}[rk.defaultState]
		}
		for where, layer := range displaced {
			for spelling, null := range rnNullSpellings {
				t.Run(rk.key+"/"+rk.defaultState+"/"+where+"/"+spelling, func(t *testing.T) {
					t.Parallel()
					tenant := "tenants:\n  t1:\n    " + rk.key + ":" + null + "\n"
					if where == "profile" {
						tenant += "    _profile: p1\n"
					}
					files := map[string]string{"_defaults.yaml": root, "team/t1.yaml": tenant}
					for k, v := range layer(rk.key, below) {
						files[k] = v
					}
					cfg, ec := rnLoad(t, files)
					if got := rnServed(t, cfg, rk.key); got != want {
						t.Errorf("served %s, want %s (the platform default)", got, want)
					}
					if _, present := tenantReserved(cfg.Tenants["t1"], rk.key); present {
						t.Errorf("the readers see %s as written: %+v", rk.key, cfg.Tenants["t1"][rk.key])
					}
					// /effective == served: the null deleted the key there too.
					if got, shown := ec.EffectiveConfig[rk.key]; shown {
						t.Errorf("/effective shows %s = %#v; the tenant's null deletes it", rk.key, got)
					}
				})
			}
		}
	}
}

// The marker survives the exporter's in-place steps: ApplyProfiles run a
// second time (the incremental reload re-runs it over published maps) still
// does not fill the deleted key from the profile.
func TestReservedNullMarker_ProfileFillIsIdempotent(t *testing.T) {
	t.Parallel()
	cfg, err := ParseConfigFile([]byte("profiles:\n  p1:\n    _silent_mode: warning\n" +
		"tenants:\n  t1:\n    _profile: p1\n    _silent_mode: ~\n"))
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		cfg.applyProfiles(nil)
		if got := cfg.resolveSilentModesAt(rnNow, nil); len(got) != 0 {
			t.Fatalf("pass %d: silenced %+v, want none (the tenant's null deletes the profile's warning)", i+1, got)
		}
	}
	if sv := cfg.Tenants["t1"]["_silent_mode"]; !reflect.DeepEqual(sv, reservedNullMarker) {
		t.Errorf("t1 _silent_mode = %+v, want reservedNullMarker", sv)
	}
}

// A written `""` is a value, not a null: it keeps today's reading (the
// marker is only for a null).
func TestReservedNull_EmptyStringIsWritten(t *testing.T) {
	t.Parallel()
	cfg, err := ParseConfigFile([]byte("tenants:\n  t1:\n    _state_maintenance: \"\"\n"))
	if err != nil {
		t.Fatal(err)
	}
	if sv, ok := tenantReserved(cfg.Tenants["t1"], "_state_maintenance"); !ok || sv.Default != "" {
		t.Errorf("_state_maintenance: \"\" = %+v, %v; want a written empty value", sv, ok)
	}
}
