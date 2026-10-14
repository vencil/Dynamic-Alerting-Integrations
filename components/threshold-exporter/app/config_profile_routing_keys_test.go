package main

// #2914: da_config_profile_routing_keys and its ERROR. Observed through the
// metrics and logger seams (freshMetrics + SetMetrics, SetLogger;
// test-map.md).

import (
	"bytes"
	"log"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

const profileRoutingAnchor = "routing-layer keys in threshold profiles are not applied (#2914)"

func TestLoad_DirMode_PublishesProfileRoutingKeys(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"), "profiles:\n  std:\n    mysql_connections: 60\n"+
		"    _severity_dedup: disable\n    _routing:\n      receiver: {type: webhook, url: \"https://h.example/p\"}\n")
	writeTestYAML(t, filepath.Join(dir, "_platform.yaml"), "profiles:\n  unused:\n    _routing_profile: team\n")
	writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx:\n    _profile: std\n")

	fresh, _ := freshMetrics(t)
	var logs bytes.Buffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logs, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.profileRoutingKeys); got != 3 {
		t.Fatalf("after Load: da_config_profile_routing_keys=%v, want 3", got)
	}
	assertLogLineWith(t, logs.String(), profileRoutingAnchor, "ERROR: 3 ",
		`_platform.yaml: profile "unused" key _routing_profile`,
		`_profiles.yaml: profile "std" key _routing`,
		`_profiles.yaml: profile "std" key _severity_dedup`)
	// Not applied: the tenant keeps the default dedup and gets no routing.
	cfg := m.GetConfig()
	for _, k := range []string{"_routing", "_severity_dedup"} {
		if _, filled := cfg.Tenants["tx"][k]; filled {
			t.Errorf("%s filled in from the profile", k)
		}
	}

	// A reload that leaves the profile files alone still counts them (cached
	// partials), and the fix drives the gauge back to 0.
	writeTestYAML(t, filepath.Join(dir, "tx.yaml"), "tenants:\n  tx:\n    _profile: std\n    mysql_connections: \"70\"\n")
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	if got := testutil.ToFloat64(fresh.profileRoutingKeys); got != 3 {
		t.Fatalf("after an unrelated reload: da_config_profile_routing_keys=%v, want 3", got)
	}
	writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"), "profiles:\n  std:\n    mysql_connections: 60\n")
	writeTestYAML(t, filepath.Join(dir, "_platform.yaml"), "profiles:\n  unused:\n    mysql_connections: 1\n")
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	if got := testutil.ToFloat64(fresh.profileRoutingKeys); got != 0 {
		t.Fatalf("after the fix: da_config_profile_routing_keys=%v, want 0", got)
	}
}

func TestLoad_SingleFile_PublishesProfileRoutingKeys(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, "defaults:\n  mysql_connections: 80\nprofiles:\n  gold:\n    _severity_dedup: disable\n"+
		"tenants:\n  t-a:\n    _profile: gold\n")
	fresh, _ := freshMetrics(t)
	var logs bytes.Buffer
	m := NewConfigManagerWithDebounce(path, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logs, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.profileRoutingKeys); got != 1 {
		t.Fatalf("da_config_profile_routing_keys=%v, want 1", got)
	}
	assertLogLineWith(t, logs.String(), profileRoutingAnchor, `config.yaml: profile "gold" key _severity_dedup`)
	// ResolveSeverityDedup lists a tenant only when dedup is on (a disable
	// has no entry), so the tenant must be listed, as enable.
	dedupOn := false
	for _, d := range m.GetConfig().ResolveSeverityDedup() {
		if d.Tenant == "t-a" {
			dedupOn = d.Mode == "enable"
		}
	}
	if !dedupOn {
		t.Errorf("severity dedup for t-a is not the default enable (the profile's disable was applied)")
	}
}
