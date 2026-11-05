package main

// #3556: a `_critical` tier declared under `dimensions:` is logged in both
// load modes — directory mode through applyBoundaryRules, the single file by
// Load itself, which applies no boundary rules.

import (
	"bytes"
	"log"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoad_WarnsCriticalTierDimensionBaseInBothModes(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  app_load: 10\ndimensions:\n  app_load_critical: [db]\n"
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), root)
	single := filepath.Join(t.TempDir(), "config.yaml")
	writeFile(t, single, root)
	for name, path := range map[string]string{"directory": dir, "single file": single} {
		var buf bytes.Buffer
		m := NewConfigManager(path)
		t.Cleanup(m.Close)
		m.SetLogger(log.New(&buf, "", 0))
		if err := m.Load(); err != nil {
			t.Fatalf("%s: Load: %v", name, err)
		}
		if !strings.Contains(buf.String(), `declares "app_load_critical", a `+"`_critical`"+` tier`) {
			t.Errorf("%s: want the #3556 WARN, got:\n%s", name, buf.String())
		}
	}
}
