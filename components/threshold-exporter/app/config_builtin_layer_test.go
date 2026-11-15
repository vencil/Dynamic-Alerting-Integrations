package main

// config_builtin_layer_test.go — the exporter's own merged_hash leaves the
// built-in layer (#3832) out exactly when /metrics does: a root
// `_defaults.yaml` that cannot be read. Its answer is compared with
// /effective's (config.ResolveEffective) on the cold load and after a tick
// that makes the root readable again.

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestBuiltinLayer_ExporterMergedHashWithholdsItAsMetricsDoes(t *testing.T) {
	dir := t.TempDir()
	// A subtree carrier keeps the tree hierarchical while the root one is
	// a dangling link.
	writeTestYAML(t, filepath.Join(dir, "team", "_defaults.yaml"), "defaults:\n  redis_connected_clients: 1000\n")
	writeTestYAML(t, filepath.Join(dir, "team", "t1.yaml"), "tenants:\n  t1:\n    redis_connected_clients: \"900\"\n")
	root := filepath.Join(dir, "_defaults.yaml")
	if err := os.Symlink("root-target.yaml", root); err != nil {
		t.Skipf("symlink: %v", err)
	}

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	_ = m.Load() // a root that cannot be read is reported, not fatal to the hash

	hashOf := func() string {
		m.mu.RLock()
		defer m.mu.RUnlock()
		return m.hierarchy.mergedHashes["t1"]
	}
	effOf := func() *config.EffectiveConfig {
		ec, err := config.ResolveEffective(dir, "t1", time.Now())
		if err != nil {
			t.Fatalf("ResolveEffective: %v", err)
		}
		return ec
	}

	cold, ec := hashOf(), effOf()
	if cold == "" {
		t.Fatal("no merged_hash for t1 after Load")
	}
	if cold != ec.MergedHash {
		t.Errorf("cold load: exporter merged_hash %s, /effective %s", cold, ec.MergedHash)
	}
	for k := range config.BuiltinDefaults() {
		if _, ok := ec.EffectiveConfig[k]; ok {
			t.Errorf("/effective shows built-in %q over a root that cannot be read", k)
		}
	}

	// The link's target appears: the root reads, the built-in layer is laid.
	writeTestYAML(t, filepath.Join(dir, "root-target.yaml"), "defaults:\n  redis_connected_clients: 1000\n")
	m.tickOnce()
	warm, ec2 := hashOf(), effOf()
	if warm == cold {
		t.Errorf("merged_hash %s did not move when the root became readable", warm)
	}
	if warm != ec2.MergedHash {
		t.Errorf("after the tick: exporter merged_hash %s, /effective %s", warm, ec2.MergedHash)
	}
	if _, ok := ec2.EffectiveConfig["pg_connections"]; !ok {
		t.Error("/effective has no built-in pg_connections once the root reads")
	}
}

// The chain does not move when a dangling `_defaults.yml` appears beside a
// readable root `_defaults.yaml` (the `.yaml` stays the selected carrier),
// yet the root now holds a carrier that cannot be read: the reload computes
// every merged_hash again rather than reusing the cached one. A file that
// cannot be read is no change-detection input (config.go, #2592), so the
// reload is the next one an unrelated tenant file triggers.
func TestBuiltinLayer_ExporterRecomputesWhenOnlyTheWithholdingMoves(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  redis_connected_clients: 1000\n")
	writeTestYAML(t, filepath.Join(dir, "team", "t1.yaml"), "tenants:\n  t1:\n    redis_connected_clients: \"900\"\n")

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	m.tickOnce()
	hashOf := func() string {
		m.mu.RLock()
		defer m.mu.RUnlock()
		return m.hierarchy.mergedHashes["t1"]
	}
	before := hashOf()

	if err := os.Symlink("nowhere.yaml", filepath.Join(dir, "_defaults.yml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	writeTestYAML(t, filepath.Join(dir, "other", "t2.yaml"), "tenants:\n  t2: {}\n")
	m.tickOnce()
	ec, err := config.ResolveEffective(dir, "t1", time.Now())
	if err != nil {
		t.Fatalf("ResolveEffective: %v", err)
	}
	after := hashOf()
	if after != ec.MergedHash || after == before {
		t.Errorf("exporter merged_hash %s (before the link: %s), /effective %s", after, before, ec.MergedHash)
	}
}
