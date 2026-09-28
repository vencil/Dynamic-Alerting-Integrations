package main

// #2019: da-guard's redundant-override check reads EffectiveConfig
// .MergedDefaults as "what the tenant inherits". With a root platform
// file's per-tenant block in play, a finding is only honest if following
// it (deleting the override) leaves the effective config unchanged. The
// platform layer replaces per TOP-LEVEL key, so a leaf inside a mapping
// the tenant writes (`_routing`) falls back to the chain, not to the
// platform's mapping — while a scalar the tenant writes falls back to the
// platform's scalar.

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

const overlayGuardPlatform = "defaults:\n  mysql_connections: 80\n" +
	"tenants:\n  tx:\n    mysql_connections: \"60\"\n" +
	"    _routing:\n      group_wait: 10s\n      repeat_interval: 2h\n"

func redundantFields(t *testing.T, dir string) map[string]bool {
	t.Helper()
	scoped, err := config.ScopeEffective(dir, dir)
	if err != nil {
		t.Fatal(err)
	}
	report, err := guard.CheckDefaultsImpact(buildCheckInput(scoped, &flags{}))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	for _, f := range report.Findings {
		if f.Kind == guard.FindingRedundantOverride && f.TenantID == "tx" {
			out[f.Field] = true
		}
	}
	return out
}

// effectiveWith is tx's effective config with the tenant file rewritten.
func effectiveWith(t *testing.T, dir, tenantFile string) map[string]any {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "tx.yaml"), []byte(tenantFile), 0o600); err != nil {
		t.Fatal(err)
	}
	ec, err := config.ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	return ec.EffectiveConfig
}

func TestGuard_RedundantOverrideFollowsThePlatformLayersReplaceSemantics(t *testing.T) {
	t.Parallel()
	const tenant = "tenants:\n  tx:\n    mysql_connections: \"60\"\n    _routing:\n      group_wait: 10s\n"
	dir := t.TempDir()
	testutil.WriteTree(t, dir, map[string]string{
		"_defaults.yaml": overlayGuardPlatform,
		"tx.yaml":        tenant,
	})
	got := redundantFields(t, dir)

	// Ground truth, measured rather than asserted: what deleting each
	// override actually does to the effective config.
	base := effectiveWith(t, dir, tenant)
	noScalar := effectiveWith(t, dir, "tenants:\n  tx:\n    _routing:\n      group_wait: 10s\n")
	noLeaf := effectiveWith(t, dir, "tenants:\n  tx:\n    mysql_connections: \"60\"\n    _routing: {}\n")
	if !reflect.DeepEqual(base, noScalar) {
		t.Fatalf("premise: deleting mysql_connections changed the config (%v → %v)", base, noScalar)
	}
	if reflect.DeepEqual(base, noLeaf) {
		t.Fatalf("premise: deleting _routing.group_wait left the config unchanged (%v)", base)
	}

	if !got["mysql_connections"] {
		t.Errorf("mysql_connections equals the platform value it falls back to — want redundant; findings %v", got)
	}
	if got["_routing.group_wait"] {
		t.Errorf("_routing.group_wait flagged redundant, but deleting it changes _routing %v → %v", base["_routing"], noLeaf["_routing"])
	}
}

// servedWarningAt is what /metrics serves for tx's warning row of `key` at
// each instant in `at`, with tx.yaml set to tenantFile (a missing row fails
// the test). The flat plane (LoadDir + ResolveAt), not ResolveEffective, so
// the premise is not measured on the walker plane under test.
func servedWarningAt(t *testing.T, dir, tenantFile, key string, at []time.Time) []float64 {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "tx.yaml"), []byte(tenantFile), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	out := make([]float64, len(at))
	for i, now := range at {
		found := false
		for _, r := range cfg.ResolveAt(now) {
			if r.Tenant == "tx" && r.Severity == "warning" && r.Component+"_"+r.Metric == key {
				out[i], found = r.Value, true
			}
		}
		if !found {
			t.Fatalf("premise: no %s row served for tx at %s", key, now.Format(time.RFC3339))
		}
	}
	return out
}

// #2191: a platform value in the SCHEDULE form (a mapping) over a scalar the
// tenant writes. Deleting the scalar falls back to the platform's mapping
// (55, or the window's value), not to the chain's 80 — so 80 must not be
// called redundant. platformInherited used to drop every mapping-valued
// platform key, leaving MergedDefaults at the chain's 80.
// Oracle: /metrics with and without the tenant's key, at one instant inside
// and one outside the schedule window; redundant ⇔ unchanged at both.
func TestGuard_RedundantOverrideUnderAScheduledPlatformValue(t *testing.T) {
	t.Parallel()
	at := []time.Time{
		time.Date(2026, 7, 1, 3, 0, 0, 0, time.UTC),  // inside 01:00-09:00
		time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC), // outside
	}
	const without = "tenants:\n  tx:\n    pg_connections: 100\n"
	cases := []struct {
		name      string
		platform  string // tx's mysql_connections under `tenants:` in _defaults.yaml
		tenant    string // tx's mysql_connections in tx.yaml
		redundant bool
	}{
		// The measured false advice (FALSE-POSITIVE before the fix).
		{"scheduled-default-over-a-chain-valued-scalar-is-not-redundant",
			"\n      default: \"55\"\n", "80", false},
		{"scheduled-default-with-a-window-over-a-chain-valued-scalar-is-not-redundant",
			"\n      default: \"55\"\n      overrides:\n        - window: \"01:00-09:00\"\n          value: \"66\"\n",
			"80", false},
		// Control: the platform's own scalar IS redundant.
		{"scalar-platform-value-equal-to-the-tenants-is-redundant",
			" 80\n", "80", true},
		// Negative: a differing value is not.
		{"scalar-platform-value-differing-from-the-tenants-is-not-redundant",
			" 80\n", "70", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tenant := "tenants:\n  tx:\n    pg_connections: 100\n    mysql_connections: " + tc.tenant + "\n"
			dir := t.TempDir()
			testutil.WriteTree(t, dir, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  pg_connections: 100\n" +
					"tenants:\n  tx:\n    mysql_connections:" + tc.platform,
				"tx.yaml": tenant,
			})

			withoutV := servedWarningAt(t, dir, without, "mysql_connections", at)
			withV := servedWarningAt(t, dir, tenant, "mysql_connections", at) // leaves tx.yaml WITH the override
			if unchanged := reflect.DeepEqual(withV, withoutV); unchanged != tc.redundant {
				t.Fatalf("premise: /metrics mysql_connections %v with the override, %v without — want redundant=%v", withV, withoutV, tc.redundant)
			}

			if got := redundantFields(t, dir)["mysql_connections"]; got != tc.redundant {
				t.Errorf("redundant_override on mysql_connections = %v, want %v (/metrics at %v: %v with the override, %v without)", got, tc.redundant, at, withV, withoutV)
			}
		})
	}
}
