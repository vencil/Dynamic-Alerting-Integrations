package config

// #2117 (ii): /simulate expands a tenant's `_profile` from the `profiles:`
// block of the request chain's ROOT entry (L0) — the only chain file whose
// `profiles:` reaches /metrics — and from nothing else: `_profiles.yaml` is
// not part of the request shape. Each case builds the same tree on disk and
// compares the simulated config with ResolveEffective (/effective) and with
// what /metrics serves (LoadDir + Resolve).

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func writeSimProfileTree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// servedMySQL is tx's mysql_connections warning row on /metrics.
func servedMySQL(t *testing.T, dir string) float64 {
	t.Helper()
	cfg, _, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range cfg.Resolve() {
		if r.Tenant == "tx" && r.Severity == "warning" && r.Component == "mysql" && r.Metric == "connections" {
			return r.Value
		}
	}
	t.Fatal("no mysql_connections row served for tx")
	return 0
}

func TestSimulate_ExpandsProfilesCarriedByTheChainRoot(t *testing.T) {
	t.Parallel()
	const (
		l0     = "defaults:\n  mysql_connections: 80\nprofiles:\n  std:\n    mysql_connections: 60\n"
		tenant = "tenants:\n  tx:\n    _profile: std\n"
	)
	dir := writeSimProfileTree(t, map[string]string{"_defaults.yaml": l0, "tx.yaml": tenant})

	sim, err := SimulateEffective(SimulateRequest{TenantID: "tx", TenantYAML: []byte(tenant), DefaultsChainYAML: [][]byte{[]byte(l0)}})
	if err != nil {
		t.Fatal(err)
	}
	ec, err := ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	if got := sim.Config["mysql_connections"]; got != 60 {
		t.Errorf("simulate mysql_connections = %v, want the profile's 60", got)
	}
	if served := servedMySQL(t, dir); served != 60 {
		t.Fatalf("premise: /metrics serves %v, want 60", served)
	}
	if !reflect.DeepEqual(sim.Config, ec.EffectiveConfig) || sim.MergedHash != ec.MergedHash {
		t.Errorf("simulate vs /effective drift:\nsim=%v %s\neff=%v %s", sim.Config, sim.MergedHash, ec.EffectiveConfig, ec.MergedHash)
	}
}

// A NESTED chain entry's `profiles:` is read by no plane (/metrics drops
// nested platform files), so simulate must not expand it either.
func TestSimulate_IgnoresProfilesInANestedChainEntry(t *testing.T) {
	t.Parallel()
	const (
		l0     = "defaults:\n  mysql_connections: 80\n"
		l1     = "profiles:\n  std:\n    mysql_connections: 60\n"
		tenant = "tenants:\n  tx:\n    _profile: std\n"
	)
	dir := writeSimProfileTree(t, map[string]string{"_defaults.yaml": l0, "team/_defaults.yaml": l1, "team/tx.yaml": tenant})

	sim, err := SimulateEffective(SimulateRequest{TenantID: "tx", TenantYAML: []byte(tenant), DefaultsChainYAML: [][]byte{[]byte(l0), []byte(l1)}})
	if err != nil {
		t.Fatal(err)
	}
	ec, err := ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	if served := servedMySQL(t, dir); served != 80 {
		t.Fatalf("premise: /metrics serves %v, want 80 (nested profiles unread)", served)
	}
	if got := sim.Config["mysql_connections"]; got != 80 {
		t.Errorf("simulate mysql_connections = %v, want 80", got)
	}
	if !reflect.DeepEqual(sim.Config, ec.EffectiveConfig) || sim.MergedHash != ec.MergedHash {
		t.Errorf("simulate vs /effective drift:\nsim=%v %s\neff=%v %s", sim.Config, sim.MergedHash, ec.EffectiveConfig, ec.MergedHash)
	}
}

// The documented limit: `_profiles.yaml` is not part of the request shape,
// so a profile defined only there expands on /effective and /metrics and
// NOT on /simulate. Pinned so that widening the request shape is a
// deliberate change, not a silent one.
func TestSimulate_ProfilesFileIsNotPartOfTheRequest(t *testing.T) {
	t.Parallel()
	const (
		l0     = "defaults:\n  mysql_connections: 80\n"
		tenant = "tenants:\n  tx:\n    _profile: std\n"
	)
	dir := writeSimProfileTree(t, map[string]string{
		"_defaults.yaml": l0,
		"_profiles.yaml": "profiles:\n  std:\n    mysql_connections: 60\n",
		"tx.yaml":        tenant,
	})
	sim, err := SimulateEffective(SimulateRequest{TenantID: "tx", TenantYAML: []byte(tenant), DefaultsChainYAML: [][]byte{[]byte(l0)}})
	if err != nil {
		t.Fatal(err)
	}
	ec, err := ResolveEffective(dir, "tx")
	if err != nil {
		t.Fatal(err)
	}
	if got := ec.EffectiveConfig["mysql_connections"]; got != 60 {
		t.Fatalf("premise: /effective mysql_connections = %v, want 60", got)
	}
	if got := sim.Config["mysql_connections"]; got != 80 {
		t.Errorf("simulate mysql_connections = %v, want 80 — `_profiles.yaml` is not in the request", got)
	}
}
