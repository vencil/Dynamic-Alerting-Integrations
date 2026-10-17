package main

// unregistered_underscore_test.go — #3045: a `_` key the reserved-key
// registry does not list serves no /metrics row in any layer, the root
// `defaults:` included, and every shape of it (base, `_critical`,
// dimensional). served-values lists it in `unserved` (keys reason
// `unregistered`); `da-guard effective` names it in `not_served` with the
// reason `unregistered`. Before #3045 a root `defaults:` entry made such a
// key a threshold: `_myth: 5` at the root and `_myth: "7"` in a tenant
// served 7.

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// unregisteredTree writes `_` keys the registry does not list into every
// layer: the root `defaults:` (base, `_critical`, dimensional), a profile,
// a root platform `tenants:` entry, a subtree `_defaults.yaml` and the
// tenant's own file. The root declares each, which before #3045 is what made
// it a threshold. tb writes none of them.
func unregisteredTree() map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 80\n  _myth: 5\n  _myth_critical: 9\n" +
			"  _myth{x=\"a\"}: 4\n  _mprof: 1\n  _mplat: 1\n  _msub: 1\n",
		"_profiles.yaml":      "profiles:\n  p1:\n    _mprof: 2\n",
		"_platform.yaml":      "tenants:\n  ta:\n    _mplat: 3\n",
		"team/_defaults.yaml": "defaults:\n  _msub: 7\n",
		"team/ta.yaml": "tenants:\n  ta:\n    _profile: p1\n    cpu: 70\n    _myth: \"7\"\n" +
			"    _myth_critical: 8\n    _myth{y=\"b\"}: 6\n",
		"tb.yaml": "tenants:\n  tb: {}\n",
	}
}

func TestServedValues_UnregisteredUnderscoreKeyIsServedInNoLayer(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, unregisteredTree(), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	for _, id := range []string{"ta", "tb"} {
		tv := doc.Tenants[id]
		// Severities: exactly the threshold keys with a /metrics row.
		for k := range tv.Severities {
			if strings.HasPrefix(k, "_") {
				t.Errorf("%s: /metrics serves %s (severity %q)", id, k, tv.Severities[k])
			}
		}
		if len(tv.Dropped) > 0 {
			t.Errorf("%s: dropped = %v, want none", id, tv.Dropped)
		}
		for k := range tv.Values {
			if config.IsUnregisteredUnderscoreKey(k) {
				t.Errorf("%s: values carries %s = %v", id, k, tv.Values[k])
			}
		}
		if _, ok := tv.Severities["cpu"]; !ok {
			t.Errorf("control: %s is not served cpu: %v", id, tv.Values)
		}
	}
	ta := doc.Tenants["ta"]
	wantTA := map[string]any{
		"_myth": "7", "_myth_critical": "8", `_myth{y="b"}`: "6", // the tenant's own
		"_mprof": "2", "_mplat": "3", "_msub": "7", // profile, platform entry, subtree
		`_myth{x="a"}`: 4.0, // the root's, which ta does not write
	}
	if !reflect.DeepEqual(ta.Unserved, merged(wantTA, map[string]any{"_profile": "p1"})) {
		t.Errorf("ta unserved = %v, want %v and _profile", ta.Unserved, wantTA)
	}
	for k := range wantTA {
		if e := ta.Keys[k]; e.Exporter || e.Reason != keyReasonUnregistered {
			t.Errorf("ta keys[%s] = %+v, want reason %q", k, e, keyReasonUnregistered)
		}
	}
	wantTB := map[string]any{"_myth": 5.0, "_myth_critical": 9.0, `_myth{x="a"}`: 4.0,
		"_mprof": 1.0, "_mplat": 1.0, "_msub": 1.0}
	if tb := doc.Tenants["tb"]; !reflect.DeepEqual(tb.Unserved, wantTB) {
		t.Errorf("tb unserved = %v, want %v", tb.Unserved, wantTB)
	}
}

// merged is a copy of a with b's entries added.
func merged(a, b map[string]any) map[string]any {
	out := make(map[string]any, len(a)+len(b))
	for k, v := range a {
		out[k] = v
	}
	for k, v := range b {
		out[k] = v
	}
	return out
}

func TestEffective_UnregisteredUnderscoreKeyIsNotServed(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, unregisteredTree())
	code, doc, stderr := runEffectiveOn(t, dir)
	mustOK(t, code, stderr)
	var ta struct {
		EffectiveConfig map[string]any                 `json:"effective_config"`
		NotServed       map[string]config.NotServedKey `json:"not_served"`
	}
	if err := json.Unmarshal(doc.Tenants["ta"], &ta); err != nil {
		t.Fatal(err)
	}
	un := func(file string) config.NotServedKey {
		return config.NotServedKey{Reason: config.NotServedUnregistered, File: file}
	}
	want := map[string]config.NotServedKey{
		"_myth": un("team/ta.yaml"), "_myth_critical": un("team/ta.yaml"), `_myth{y="b"}`: un("team/ta.yaml"),
		`_myth{x="a"}`: un("_defaults.yaml"),
		"_mprof":       un("_profiles.yaml"),
		"_mplat":       un("_platform.yaml"),
		"_msub":        un("team/_defaults.yaml"),
	}
	if !reflect.DeepEqual(ta.NotServed, want) {
		t.Errorf("ta not_served = %v, want %v", ta.NotServed, want)
	}
	// The written view keeps the value as written.
	if ta.EffectiveConfig["_myth"] != "7" {
		t.Errorf("effective_config[_myth] = %v, want \"7\"", ta.EffectiveConfig["_myth"])
	}
	assertMatchesResolveEffective(t, dir, doc)
}
