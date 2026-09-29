package config

// merge_tenant_max_metrics_test.go — the tenant-api merge core carries the
// ROOT carrier's max_metrics_per_tenant, so GET /tenants/{id}'s ResolveAt
// cuts at the cap /metrics cuts at (#2369). Before the fix the merge left the
// field 0, so GET always cut at the built-in DefaultMaxMetricsPerTenant (500):
// it listed rows /metrics had truncated (cap < key count) and dropped rows
// /metrics served (cap > 500).
//
// The oracle is /metrics itself (LoadDir, the flat plane), as in
// merge_tenant_platform_test.go.
//
// The cap is a READ-side concern only: the write gate (ValidateTenantKeys on
// the MergeParsed* merge) must accept and refuse exactly what it did before,
// whatever the cap. TestMergeTenantMaxMetricsDoesNotReachTheWriteGate pins it.
//
// Seams: none — t.TempDir() trees; LoadDir discards its log.

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// capTree returns a conf.d tree: a root `_defaults.yaml` with `capLine`
// (verbatim, may be "") and n default keys, and tx.yaml overriding m1_x.
func capTree(capLine string, n int) map[string]string {
	var d strings.Builder
	if capLine != "" {
		d.WriteString(capLine + "\n")
	}
	d.WriteString("defaults:\n")
	for i := 1; i <= n; i++ {
		fmt.Fprintf(&d, "  m%d_x: 1\n", i)
	}
	return map[string]string{
		"_defaults.yaml": d.String(),
		"tx.yaml":        "tenants:\n  tx:\n    m1_x: \"2\"\n",
	}
}

func TestMergeTenantHonoursRootMaxMetricsPerTenant(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name     string
		files    map[string]string
		wantRows int
	}{
		// The issue's direction: GET listed rows /metrics had cut.
		{"cap below key count", capTree("max_metrics_per_tenant: 2", 4), 2},
		// The reverse: GET cut at 500 while /metrics served all 510.
		{"cap above built-in 500", capTree("max_metrics_per_tenant: 1000", 510), 510},
		// A negative cap disables truncation on /metrics.
		{"negative cap is no limit", capTree("max_metrics_per_tenant: -1", 510), 510},
		// Controls: no cap → built-in 500, both sides.
		{"no cap, few keys", capTree("", 4), 4},
		{"no cap, above 500", capTree("", 510), 500},
		// Only the ROOT carrier sets it (#2028): a tenant file's own cap is
		// ignored by /metrics and must be by GET too.
		{"tenant file cap ignored", map[string]string{
			"_defaults.yaml": "defaults:\n  m1_x: 1\n  m2_x: 1\n  m3_x: 1\n",
			"tx.yaml":        "max_metrics_per_tenant: 1\ntenants:\n  tx:\n    m1_x: \"2\"\n",
		}, 3},
		// Nor does a nested carrier, nor a non-carrier root platform file.
		{"nested carrier cap ignored", map[string]string{
			"_defaults.yaml":     "defaults:\n  m1_x: 1\n  m2_x: 1\n  m3_x: 1\n",
			"sub/_defaults.yaml": "max_metrics_per_tenant: 1\n",
			"tx.yaml":            "tenants:\n  tx:\n    m1_x: \"2\"\n",
		}, 3},
		{"root platform file cap ignored", map[string]string{
			"_defaults.yaml": "defaults:\n  m1_x: 1\n  m2_x: 1\n  m3_x: 1\n",
			"_platform.yaml": "max_metrics_per_tenant: 1\n",
			"tx.yaml":        "tenants:\n  tx:\n    m1_x: \"2\"\n",
		}, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)
			oracle, _, err := LoadDir(dir, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}
			want := resolvedRows(oracle, "tx")
			// Must-fire: the tree really exercises the count it names on
			// /metrics, so a match below is not two sides agreeing on nothing.
			if len(want) != tc.wantRows {
				t.Fatalf("/metrics serves %d rows for tx, the table says %d", len(want), tc.wantRows)
			}
			body, err := os.ReadFile(filepath.Join(dir, "tx.yaml"))
			if err != nil {
				t.Fatal(err)
			}

			byteMerge := MergeTenantWithRootDefaults(dir, "tx", body)
			if got := resolvedRows(&byteMerge, "tx"); !reflect.DeepEqual(got, want) {
				if len(got) <= 10 && len(want) <= 10 {
					t.Errorf("GET merge serves %d rows, /metrics %d\n got: %v\nwant: %v", len(got), len(want), got, want)
				} else {
					t.Errorf("GET merge serves %d rows, /metrics %d", len(got), len(want))
				}
			}
			parsed, err := ParseConfigFile(body)
			if err != nil {
				t.Fatal(err)
			}
			parsedMerge := MergeParsedTenantWithRootDefaults(dir, parsed)
			if got := resolvedRows(&parsedMerge, "tx"); !reflect.DeepEqual(got, want) {
				t.Errorf("write-gate merge serves %d rows, /metrics %d", len(got), len(want))
			}

			// Same reading as da-guard's RootMaxMetricsPerTenant.
			limit, _, err := RootMaxMetricsPerTenant(dir)
			if err != nil {
				t.Fatal(err)
			}
			if got := EffectiveMaxMetricsPerTenant(byteMerge.MaxMetricsPerTenant); got != limit {
				t.Errorf("merge's effective cap = %d, RootMaxMetricsPerTenant = %d", got, limit)
			}
		})
	}
}

// TestMergeTenantMaxMetricsDoesNotReachTheWriteGate: the write gate
// (MergeParsedTenantWithRootDefaults + ValidateTenantKeys, what
// tenant-api's gitops.validate runs) gives the same Errors and Notices under
// a cap of 1 as under no cap — for a clean body with more keys than the cap,
// and for a body with an unknown key (which must still be refused).
func TestMergeTenantMaxMetricsDoesNotReachTheWriteGate(t *testing.T) {
	t.Parallel()
	bodies := map[string]string{
		"clean, over cap":   "tenants:\n  tx:\n    m1_x: \"2\"\n    m2_x: \"3\"\n    m3_x: \"4\"\n",
		"unknown key":       "tenants:\n  tx:\n    m1_x: \"2\"\n    not_a_default_x: \"3\"\n",
		"tenant sets a cap": "max_metrics_per_tenant: 1\ntenants:\n  tx:\n    m1_x: \"2\"\n",
	}
	validateUnder := func(t *testing.T, capLine, body string) KeyValidation {
		t.Helper()
		dir := t.TempDir()
		files := capTree(capLine, 4)
		files["tx.yaml"] = body
		writeMergeTree(t, dir, files)
		parsed, err := ParseConfigFile([]byte(body))
		if err != nil {
			t.Fatal(err)
		}
		m := MergeParsedTenantWithRootDefaults(dir, parsed)
		return m.ValidateTenantKeys()
	}
	for name, body := range bodies {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			capped := validateUnder(t, "max_metrics_per_tenant: 1", body)
			uncapped := validateUnder(t, "", body)
			if !reflect.DeepEqual(capped.Errors, uncapped.Errors) || !reflect.DeepEqual(capped.Notices, uncapped.Notices) {
				t.Errorf("cap changed the write gate\n capped:   %+v\n uncapped: %+v", capped, uncapped)
			}
			// Must-fire controls: the gate does refuse the unknown key and
			// does accept the clean body, so equality above is not two empties
			// by accident.
			switch name {
			case "unknown key":
				if len(capped.Errors) == 0 {
					t.Error("unknown key was not refused")
				}
			case "clean, over cap":
				if len(capped.Errors) != 0 {
					t.Errorf("clean body refused under a cap of 1: %v", capped.Errors)
				}
			}
		})
	}
}
