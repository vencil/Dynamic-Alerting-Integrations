package main

// #2859: da_config_metadata_string_form. Observed through the metrics seam
// (freshMetrics + SetMetrics, test-map.md).

import (
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestLoad_DirMode_PublishesMetadataStringForm(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  key_0: 10\n")
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a:\n    _metadata: \"environment: production\"\n")
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b:\n    _metadata:\n      environment: production\n")

	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.metadataStringForm); got != 1 {
		t.Fatalf("after Load: da_config_metadata_string_form=%v, want 1 (only t-a)", got)
	}
	// Still read: the string form keeps serving its metadata.
	if env := metadataEnvironment(m, "t-a"); env != "production" {
		t.Fatalf("t-a environment=%q, want production (the string form is still read)", env)
	}

	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a:\n    _metadata:\n      environment: production\n")
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	if got := testutil.ToFloat64(fresh.metadataStringForm); got != 0 {
		t.Fatalf("after the fix: da_config_metadata_string_form=%v, want 0", got)
	}
}

// Flat directory mode reloads through incrementalLoadFrom: an unchanged file
// that writes the string form must still count.
func TestIncrementalLoad_MetadataStringFormCountsUnchangedFiles(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a:\n    _metadata: \"owner: team-a\"\n")
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b:\n    key_0: \"1\"\n")

	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b:\n    key_0: \"2\"\n")
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if got := testutil.ToFloat64(fresh.metadataStringForm); got != 1 {
		t.Fatalf("after reload: da_config_metadata_string_form=%v, want 1", got)
	}
}

func TestLoad_SingleFile_PublishesMetadataStringForm(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, "profiles:\n  gold:\n    _metadata: \"tier: gold\"\ntenants:\n  t-a:\n    _metadata: \"owner: a\"\n  t-b:\n    _metadata: ~\n")
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(path, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.metadataStringForm); got != 2 {
		t.Fatalf("da_config_metadata_string_form=%v, want 2 (profile gold and t-a; a null is not a string)", got)
	}
}

func metadataEnvironment(m *ConfigManager, tenant string) string {
	cfg := m.GetConfig()
	for _, md := range cfg.ResolveMetadata() {
		if md.Tenant == tenant {
			return md.Environment
		}
	}
	return ""
}
