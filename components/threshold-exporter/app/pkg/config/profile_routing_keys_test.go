package config

// #2914: a threshold profile may not carry a routing-layer key
// (`_severity_dedup`, `_routing*`). Both planes leave it out of the tenant
// (profileFill), and ProfileRoutingKeys names every one a profile source
// the exporter reads writes — whether or not a tenant elects the profile.

import (
	"io"
	"log"
	"reflect"
	"testing"
)

func TestProfileRoutingLayerKeys_NotFilledOnEitherPlane(t *testing.T) {
	t.Parallel()
	dir := writeSimProfileTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  std:\n    mysql_connections: 60\n" +
			"    _severity_dedup: disable\n" +
			"    _routing:\n      receiver: {type: webhook, url: \"https://h.example/profile\"}\n" +
			"    _routing_profile: team\n" +
			"    _metadata: {owner: team-dba}\n",
		"tx.yaml": "tenants:\n  tx:\n    _profile: std\n",
	})

	// /metrics (the flat plane).
	cfg, _, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"_severity_dedup", "_routing", "_routing_profile"} {
		if sv, filled := cfg.Tenants["tx"][k]; filled {
			t.Errorf("/metrics: %s filled in from the profile (%q)", k, sv.Default)
		}
	}
	// ResolveSeverityDedup lists a tenant only when dedup is on (a disable
	// has no entry), so the tenant must be listed, as enable.
	dedupOn := false
	for _, d := range cfg.ResolveSeverityDedup() {
		if d.Tenant == "tx" {
			dedupOn = d.Mode == "enable"
		}
	}
	if !dedupOn {
		t.Errorf("severity dedup for tx is not the default enable (the profile's disable was applied)")
	}
	if routes := cfg.ResolveRouting(); len(routes) != 0 {
		t.Errorf("/metrics: routing from the profile: %+v", routes)
	}
	if got := cfg.Tenants["tx"]["mysql_connections"].Default; got != "60" {
		t.Errorf("control: /metrics mysql_connections = %q, want the profile's 60", got)
	}

	// /effective (the walker plane).
	ec, err := ResolveEffective(dir, "tx", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"_severity_dedup", "_routing", "_routing_profile"} {
		if v, ok := ec.EffectiveConfig[k]; ok {
			t.Errorf("/effective carries %s = %v from the profile", k, v)
		}
	}
	want := []ProfileOverlaySource{{Profile: "std", File: "_profiles.yaml", Keys: []string{"mysql_connections"}}}
	if !reflect.DeepEqual(ec.ProfileOverlay, want) {
		t.Errorf("profile_overlay = %+v, want %+v", ec.ProfileOverlay, want)
	}
	if got := ec.EffectiveConfig["mysql_connections"]; got != 60 {
		t.Errorf("control: /effective mysql_connections = %v, want the profile's 60", got)
	}
}

func TestProfileRoutingKeys_EveryProfileSourceTheExporterReads(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		files map[string]string
		want  []ProfileRoutingKey
	}{
		{
			name: "profiles-yaml-and-another-root-platform-file",
			files: map[string]string{
				"_profiles.yaml": "profiles:\n  b: {_routing: {receiver: {type: slack}}}\n" +
					"  a: {cpu: 1, _severity_dedup: disable, _routing_profile: x}\n",
				"_platform.yaml": "profiles:\n  c: {_routingx: 1, _routing_defaults: {}}\n",
			},
			want: []ProfileRoutingKey{
				{File: "_platform.yaml", Profile: "c", Key: "_routing_defaults"},
				{File: "_platform.yaml", Profile: "c", Key: "_routingx"},
				{File: "_profiles.yaml", Profile: "a", Key: "_routing_profile"},
				{File: "_profiles.yaml", Profile: "a", Key: "_severity_dedup"},
				{File: "_profiles.yaml", Profile: "b", Key: "_routing"},
			},
		},
		{
			// No tenant elects it: still reported.
			name: "unbound-profile",
			files: map[string]string{
				"_profiles.yaml": "profiles:\n  unused: {_severity_dedup: disable}\n",
			},
			want: []ProfileRoutingKey{{File: "_profiles.yaml", Profile: "unused", Key: "_severity_dedup"}},
		},
		{
			// A null is still a key the profile writes.
			name: "null-value",
			files: map[string]string{
				"_profiles.yaml": "profiles:\n  p: {_severity_dedup: ~, cpu: 1}\n",
			},
			want: []ProfileRoutingKey{{File: "_profiles.yaml", Profile: "p", Key: "_severity_dedup"}},
		},
		{
			// Read by no plane: a nested `_profiles.yaml`, a tenant file's
			// `profiles:` (stripped). `_metadata` / `_silent_mode` are not
			// routing-layer keys.
			name: "sources-the-exporter-does-not-read-and-other-reserved-keys",
			files: map[string]string{
				"_profiles.yaml":      "profiles:\n  p: {_metadata: {owner: x}, _silent_mode: warning, cpu: 1}\n",
				"team/_profiles.yaml": "profiles:\n  q: {_routing: {receiver: {type: slack}}}\n",
				"ty.yaml":             "tenants:\n  ty:\n    cpu: \"1\"\nprofiles:\n  r: {_severity_dedup: disable}\n",
			},
			want: nil,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{"tx.yaml": "tenants:\n  tx:\n    cpu: \"1\"\n"}
			for k, v := range tc.files {
				files[k] = v
			}
			dir := writeSimProfileTree(t, files)
			scoped, err := EffectiveTree(dir, fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			if len(scoped.ParseFailed) > 0 {
				t.Fatalf("premise: the exporter drops %v", scoped.ParseFailed)
			}
			if got := scoped.ProfileRoutingKeys(); !reflect.DeepEqual(got, tc.want) {
				t.Errorf("ProfileRoutingKeys = %+v\nwant %+v", got, tc.want)
			}
		})
	}
}
