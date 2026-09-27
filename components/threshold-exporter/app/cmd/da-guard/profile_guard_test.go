package main

// #2117: da-guard's redundant-override check reads EffectiveConfig
// .MergedDefaults as "what the tenant falls back to if it deletes the key".
// For a tenant on a profile that fallback is the PROFILE's value (/metrics
// fills a deleted key in from the profile, ApplyProfiles), not the defaults
// chain's. Before the fix MergedDefaults ignored the profile, so a tenant
// writing the chain's value next to `_profile` was told to delete it — and
// deleting it moved /metrics to the profile's value.
//
// Ground truth here is /metrics (config.LoadDir + Resolve, the flat plane),
// NOT ResolveEffective: the walker plane is the one this issue fixed, so a
// premise measured on it would be measured on the thing under test.

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

const (
	profileGuardDefaults = "defaults:\n  mysql_connections: 80\n  pg_connections: 100\n"
	profileGuardProfiles = "profiles:\n  std:\n    mysql_connections: 60\n"
)

// servedWarning is what /metrics serves for tx's warning row of `key`
// (component_metric) with tx.yaml set to tenantFile; ok=false when no row.
func servedWarning(t *testing.T, dir, tenantFile, key string) (float64, bool) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "tx.yaml"), []byte(tenantFile), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range cfg.Resolve() {
		if r.Tenant == "tx" && r.Severity == "warning" && r.Component+"_"+r.Metric == key {
			return r.Value, true
		}
	}
	return 0, false
}

func profilesOr(p string) string {
	if p == "" {
		return profileGuardProfiles
	}
	return p
}

func TestGuard_RedundantOverrideFollowsTheProfile(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name      string
		tenant    string // tx.yaml with the override
		without   string // tx.yaml with the override deleted
		field     string
		redundant bool
		profiles  string // _profiles.yaml ("" = profileGuardProfiles)
	}{
		// The measured false advice: 80 is the chain's value, but deleting
		// it falls back to the profile's 60.
		{"chain-value-under-a-profile-is-not-redundant",
			"tenants:\n  tx:\n    _profile: std\n    mysql_connections: 80\n",
			"tenants:\n  tx:\n    _profile: std\n",
			"mysql_connections", false, ""},
		// Control: the profile's own value IS redundant (deleting it keeps 60).
		{"profile-value-is-redundant",
			"tenants:\n  tx:\n    _profile: std\n    mysql_connections: 60\n",
			"tenants:\n  tx:\n    _profile: std\n",
			"mysql_connections", true, ""},
		// Control: a key the profile does not set still falls back to the
		// chain, so the chain's value stays redundant there.
		{"key-the-profile-does-not-set-follows-the-chain",
			"tenants:\n  tx:\n    _profile: std\n    pg_connections: 100\n",
			"tenants:\n  tx:\n    _profile: std\n",
			"pg_connections", true, ""},
		// A profile value in the SCHEDULE form (a mapping) over a scalar
		// the tenant writes: deleting the scalar falls back to the
		// profile's mapping (55), not the chain's 80, so 80 is not
		// redundant. (Blind review of a9837f1f: inherited() used to skip
		// every mapping-valued profile key.)
		{"chain-value-under-a-scheduled-profile-value-is-not-redundant",
			"tenants:\n  tx:\n    _profile: std\n    mysql_connections: 80\n",
			"tenants:\n  tx:\n    _profile: std\n",
			"mysql_connections", false,
			"profiles:\n  std:\n    mysql_connections:\n      default: \"55\"\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			testutil.WriteTree(t, dir, map[string]string{
				"_defaults.yaml": profileGuardDefaults,
				"_profiles.yaml": profilesOr(tc.profiles),
				"tx.yaml":        tc.tenant,
			})

			// Premise, measured on /metrics: does deleting the override
			// change what is served?
			without, okW := servedWarning(t, dir, tc.without, tc.field)
			with, okV := servedWarning(t, dir, tc.tenant, tc.field)
			if !okW || !okV {
				t.Fatalf("premise: no %s row served (with=%v without=%v)", tc.field, okV, okW)
			}
			if (with == without) != tc.redundant {
				t.Fatalf("premise: /metrics %s with the override %v, without %v — want redundant=%v", tc.field, with, without, tc.redundant)
			}

			if got := redundantFields(t, dir)[tc.field]; got != tc.redundant {
				t.Errorf("redundant_override on %s = %v, want %v (/metrics: %v with the override, %v without)", tc.field, got, tc.redundant, with, without)
			}
		})
	}
}
