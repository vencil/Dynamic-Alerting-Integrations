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
	"os"
	"path/filepath"
	"reflect"
	"testing"

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
