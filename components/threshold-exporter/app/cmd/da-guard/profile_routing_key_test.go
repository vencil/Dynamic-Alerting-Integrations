package main

// #2914: a routing-layer key (`_severity_dedup`, `_routing*`) in a threshold
// profile was a routing_in_unread_location error, for every root platform
// file and whether or not a tenant elects the profile; and the exporter's
// view da-guard reads (effective, served-values) no longer carries it for the
// tenant. Since #2904 the default check judges no routing: the same trees are
// undetermined (assertRoutingNotJudged), the route generator names the key
// (`_grar_parse._unread_routing`, routing_in_unread_location).

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

func TestRun_ProfileRoutingLayerKeyIsUnreadLocation(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		files    map[string]string
		scope    string
		wantCode int
		want     []string
	}{
		{
			name: "every-routing-layer-key-in-every-root-profile-source",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"_profiles.yaml": "profiles:\n  std:\n    mysql_connections: 60\n    _severity_dedup: disable\n" +
					"    _routing:\n      receiver: {type: webhook, url: \"https://h.example/profile\"}\n",
				"_platform.yaml": "profiles:\n  other:\n    _routing_profile: team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _profile: std\n",
			},
			wantCode: exitFindings,
			want: []string{
				"error routing_in_unread_location  _platform.yaml:profiles.other._routing_profile",
				"error routing_in_unread_location  _profiles.yaml:profiles.std._routing",
				"error routing_in_unread_location  _profiles.yaml:profiles.std._severity_dedup",
			},
		},
		{
			// Nobody elects the profile, and --scope covers no file of it.
			name: "unbound-profile-outside-the-scope",
			files: map[string]string{
				"_defaults.yaml":  "defaults:\n  mysql_connections: 80\n",
				"_profiles.yaml":  "profiles:\n  unused:\n    _severity_dedup: disable\n",
				"team/tx.yaml":    "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
				"other/ty.yaml":   "tenants:\n  ty:\n    mysql_connections: \"70\"\n",
				"team/_keep.yaml": "{}\n",
			},
			scope:    "team",
			wantCode: exitFindings,
			want:     []string{"error routing_in_unread_location  _profiles.yaml:profiles.unused._severity_dedup"},
		},
		{
			// Control: `_metadata` in a profile is a documented fill-in, and a
			// nested `_profiles.yaml` is read by no threshold plane (the
			// generator reads every `_` file below the root, so its
			// `_severity_dedup` is undetermined since #2904).
			name: "metadata-and-nested-profiles-are-not-reported",
			files: map[string]string{
				"_defaults.yaml":      "defaults:\n  mysql_connections: 80\n",
				"_profiles.yaml":      "profiles:\n  std:\n    _metadata: {owner: team-dba}\n",
				"team/_profiles.yaml": "profiles:\n  q:\n    _severity_dedup: disable\n",
				"tx.yaml":             "tenants:\n  tx:\n    _profile: std\n",
			},
			wantCode: exitOK,
			want:     []string{},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			assertRoutingNotJudged(t, tc.wantCode, tc.want, false, runJudged(t, tc.files, tc.scope))
		})
	}
}

// The repro of #2914: neither effective nor served-values hands the
// profile's `_routing` / `_severity_dedup` to the tenant.
func TestEffectiveAndServedValues_ProfileRoutingKeyNotApplied(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  container_cpu: 80\n",
		"conf.d/_profiles.yaml": "profiles:\n  std:\n    container_cpu: 55\n    _severity_dedup: disable\n" +
			"    _routing:\n      receiver: {type: webhook, url: \"https://h.example/profile\"}\n",
		"conf.d/tx.yaml": "tenants:\n  tx:\n    _profile: std\n",
	})
	root := filepath.Join(tmp, "conf.d")

	var eff strings.Builder
	var errOut strings.Builder
	if code := runEffective([]string{"--config-dir", root}, &eff, &errOut); code != exitOK {
		t.Fatalf("effective exit %d: %s", code, errOut.String())
	}
	var doc struct {
		Tenants map[string]struct {
			EffectiveConfig map[string]any `json:"effective_config"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(eff.String()), &doc); err != nil {
		t.Fatal(err)
	}
	ec := doc.Tenants["tx"].EffectiveConfig
	for _, k := range []string{"_routing", "_severity_dedup"} {
		if v, ok := ec[k]; ok {
			t.Errorf("effective: tx carries %s = %v from the profile", k, v)
		}
	}
	if ec["container_cpu"] != float64(55) {
		t.Errorf("control: effective container_cpu = %v, want the profile's 55", ec["container_cpu"])
	}

	var srv strings.Builder
	errOut.Reset()
	if code := runServedValues([]string{"--config-dir", root}, &srv, &errOut); code != exitOK {
		t.Fatalf("served-values exit %d: %s", code, errOut.String())
	}
	var sdoc struct {
		Tenants map[string]struct {
			Values map[string]any `json:"values"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(srv.String()), &sdoc); err != nil {
		t.Fatal(err)
	}
	vals := sdoc.Tenants["tx"].Values
	if _, ok := vals["_routing"]; ok {
		t.Errorf("served-values: tx carries _routing from the profile: %v", vals["_routing"])
	}
	if got := vals["_severity_dedup"]; got != "enable" {
		t.Errorf("served-values: tx _severity_dedup = %v, want the default enable", got)
	}
}
