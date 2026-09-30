package config

// #2433 — a `_profile` scalar that YAML types as something other than a
// plain string. /metrics (ScheduledValue) reads the scalar's TEXT: bare
// `010` elects profile "010", `!!binary MDEw` elects "MDEw" (unknown). The
// walker plane (/effective, tenant-api, da-guard, merged_hash) read the
// generic decode instead — int 8 (no profile) and "010" (profile bound) —
// so each disagreed with what /metrics serves. The oracle is the flat plane.

import (
	"testing"
)

func TestProfileRefIsReadAsTextLikeTheFlatPlane(t *testing.T) {
	t.Parallel()
	const profiles = "profiles:\n  '010':\n    mysql_connections: 11\n"
	cases := []struct {
		name     string
		profile  string // the `_profile` value exactly as written
		platform bool   // written in the root platform file's `tenants:` block, not the tenant file
		file     string // the whole tenant file, when the case needs more than one line
		wantName any    // effective `_profile`
		want     float64
	}{
		// The mapping form with a `default:` key: /metrics elects its
		// default's text (ScheduledValue.Default); /effective carries that
		// text as `_profile` (see withProfileText).
		{name: "mapping-default-bare", profile: "\n      default: 010", wantName: "010", want: 11},
		{name: "platform-mapping-default-quoted", profile: "\n      default: '010'", platform: true, wantName: "010", want: 11},
		// The flat plane resolves merge keys and aliases before reading the
		// text; so does profileTexts.
		{
			name: "merge-key", wantName: "010", want: 11,
			file: "tenants:\n  other: &base\n    _profile: 010\n  tx:\n    <<: *base\n    _metadata:\n      owner: x\n",
		},
		{
			name: "alias", wantName: "010", want: 11,
			file: "tenants:\n  other:\n    _profile: &p 010\n  tx:\n    _profile: *p\n",
		},
		// Controls: a plain string elected the same profile before #2433.
		{name: "control-quoted", profile: "'010'", wantName: "010", want: 11},
		{name: "control-unknown", profile: "nope", wantName: "nope", want: 80},
		{name: "control-platform-quoted", profile: "'010'", platform: true, wantName: "010", want: 11},
		// The bug: the generic decode's value is not the scalar's text.
		{name: "bare-octal", profile: "010", wantName: "010", want: 11},
		{name: "binary", profile: "!!binary MDEw", wantName: "MDEw", want: 80},
		{name: "platform-bare-octal", profile: "010", platform: true, wantName: "010", want: 11},
		{name: "platform-binary", profile: "!!binary MDEw", platform: true, wantName: "MDEw", want: 80},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			defaults := "defaults:\n  mysql_connections: 80\n"
			tenant := "tenants:\n  tx:\n    _metadata:\n      owner: x\n"
			line := "    _profile: " + tc.profile + "\n"
			switch {
			case tc.file != "":
				tenant = tc.file
			case tc.platform:
				defaults += "tenants:\n  tx:\n" + line
			default:
				tenant += line
			}
			dir := writePlatformTree(t, map[string]string{
				"_defaults.yaml": defaults,
				"_profiles.yaml": profiles,
				"tx.yaml":        tenant,
			})
			if got := servedRows(t, dir)["mysql_connections/warning"]; got != tc.want {
				t.Fatalf("oracle: /metrics serves %v, case expects %v", got, tc.want)
			}
			ec, err := ResolveEffective(dir, "tx")
			if err != nil {
				t.Fatalf("ResolveEffective: %v", err)
			}
			if got := effectiveMySQLConnections(t, ec); got != tc.want {
				t.Errorf("ResolveEffective mysql_connections = %v, /metrics serves %v", got, tc.want)
			}
			if got := ec.EffectiveConfig["_profile"]; got != tc.wantName {
				t.Errorf("ResolveEffective _profile = %#v, want %#v (the scalar's text)", got, tc.wantName)
			}
			scoped, err := ScopeEffective(dir, dir)
			if err != nil {
				t.Fatalf("ScopeEffective: %v", err)
			}
			found := false
			for _, s := range scoped.Tenants {
				if s.TenantID == "tx" {
					found = true
					if got := effectiveMySQLConnections(t, s); got != tc.want {
						t.Errorf("ScopeEffective mysql_connections = %v, /metrics serves %v", got, tc.want)
					}
				}
			}
			if !found {
				t.Fatal("ScopeEffective does not list tx")
			}
		})
	}
}
