package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEffectiveMaxMetricsPerTenant(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ in, want int }{
		{0, DefaultMaxMetricsPerTenant},
		{2000, 2000},
		{-1, -1},
	} {
		if got := EffectiveMaxMetricsPerTenant(c.in); got != c.want {
			t.Errorf("EffectiveMaxMetricsPerTenant(%d) = %d, want %d", c.in, got, c.want)
		}
	}
}

func TestRootMaxMetricsPerTenant(t *testing.T) {
	t.Parallel()
	write := func(t *testing.T, files map[string]string) string {
		t.Helper()
		dir := t.TempDir()
		for name, body := range files {
			p := filepath.Join(dir, name)
			if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		return dir
	}

	t.Run("no root carrier is the built-in default", func(t *testing.T) {
		t.Parallel()
		dir := write(t, map[string]string{"t.yaml": "tenants:\n  a: {}\n"})
		got, src, err := RootMaxMetricsPerTenant(dir)
		if err != nil || got != DefaultMaxMetricsPerTenant || src != "" {
			t.Errorf("got (%d, %q, %v), want (%d, \"\", nil)", got, src, err, DefaultMaxMetricsPerTenant)
		}
	})
	t.Run("root value is read, .yml carrier included", func(t *testing.T) {
		t.Parallel()
		dir := write(t, map[string]string{"_defaults.yml": "max_metrics_per_tenant: 42\n"})
		got, src, err := RootMaxMetricsPerTenant(dir)
		if err != nil || got != 42 || filepath.Base(src) != "_defaults.yml" {
			t.Errorf("got (%d, %q, %v), want (42, .../_defaults.yml, nil)", got, src, err)
		}
	})
	t.Run("a subtree value is not the root's", func(t *testing.T) {
		t.Parallel()
		dir := write(t, map[string]string{
			"_defaults.yaml":     "defaults: {}\n",
			"sub/_defaults.yaml": "max_metrics_per_tenant: 7\n",
		})
		got, _, err := RootMaxMetricsPerTenant(dir)
		if err != nil || got != DefaultMaxMetricsPerTenant {
			t.Errorf("got (%d, %v), want (%d, nil)", got, err, DefaultMaxMetricsPerTenant)
		}
	})
	t.Run("an undecodable root is an error", func(t *testing.T) {
		t.Parallel()
		dir := write(t, map[string]string{"_defaults.yaml": "max_metrics_per_tenant: [1]\n"})
		if _, _, err := RootMaxMetricsPerTenant(dir); err == nil {
			t.Error("want an error for a non-integer cap")
		}
	})
}
