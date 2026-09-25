package main

// config_platform_tenant_overlay_test.go — Go's half of
// tests/shared/platform_tenant_overlay_matrix.json (#1982), plus the
// exporter-only properties the table cannot carry: the WARN lines and the
// reload paths.
//
// The semantics under test: a ROOT platform file's `tenants:` block is the
// platform's per-tenant DEFAULT. The tenant's own file wins key by key,
// whatever either file is called; a key the tenant file does not write keeps
// the platform value; a platform file cannot create a tenant; a NESTED
// platform file's `tenants:` block is read by no plane and says so.
//
// Seams: logger via SetLogger (never log.SetOutput); metrics via
// freshMetrics + SetMetrics on every manager. t.TempDir() trees.

import (
	"bytes"
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// overlayMetricKey is the one threshold the matrix pins. The collector
// serves it as component `mysql`, metric `connections`.
const overlayMetricKey = "mysql_connections"

// orphanAnchor / nestedAnchor identify the two producers' lines. Each is
// specific to its emitter (log_assertions_test.go: anchor on the producer,
// not on the message class).
const (
	orphanAnchor = "no tenant file declares tenant"
	nestedAnchor = "tenants: block in nested platform file"
)

// ⛔ DisallowUnknownFields and a declared `_comment`, as the sibling
// defaults_symlink_parity matrix does (#1967): a misspelt key would make a
// row test nothing while staying green.
type overlayMatrix struct {
	Comment []string `json:"_comment"`
	Trees   []struct {
		Name   string            `json:"name"`
		Files  map[string]string `json:"files"`
		Expect map[string]struct {
			Metric    *float64 `json:"metric"`
			Dedup     *string  `json:"dedup"`
			GroupWait *string  `json:"group_wait"`
		} `json:"expect"`
	} `json:"trees"`
}

func loadOverlayMatrix(t *testing.T) overlayMatrix {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..",
		"tests", "shared", "platform_tenant_overlay_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m overlayMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	return m
}

func writeOverlayTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", rel, err)
		}
		writeTestYAML(t, p, content)
	}
}

// newOverlayManager loads a tree through the real entry with injected
// metrics and a captured logger.
func newOverlayManager(t *testing.T, dir string) (*ConfigManager, *bytes.Buffer) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	var buf bytes.Buffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&buf, "", 0))
	t.Cleanup(m.Close)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return m, &buf
}

// servedValue is what /metrics carries for (tenant, overlayMetricKey): the
// resolved warning-severity row the collector emits, or ok=false when the
// tenant has none.
func servedValue(m *ConfigManager, tenant string) (float64, bool) {
	cfg := m.GetConfig()
	if cfg == nil {
		return 0, false
	}
	for _, r := range cfg.Resolve() {
		if r.Tenant == tenant && r.Component+"_"+r.Metric == overlayMetricKey && r.Severity == "warning" {
			return r.Value, true
		}
	}
	return 0, false
}

func assertServed(t *testing.T, m *ConfigManager, where, tenant string, want *float64) {
	t.Helper()
	got, ok := servedValue(m, tenant)
	switch {
	case want == nil && ok:
		t.Errorf("%s: %s is served with %s=%v, want the tenant ABSENT from /metrics", where, tenant, overlayMetricKey, got)
	case want != nil && !ok:
		t.Errorf("%s: %s serves no %s, want %v", where, tenant, overlayMetricKey, *want)
	case want != nil && got != *want:
		t.Errorf("%s: %s serves %s=%v, want %v", where, tenant, overlayMetricKey, got, *want)
	}
}

func TestPlatformTenantOverlayMatrix(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)
	for _, tree := range m.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, tree.Files)
			mgr, _ := newOverlayManager(t, dir)
			for tenant, want := range tree.Expect {
				assertServed(t, mgr, tree.Name, tenant, want.Metric)
			}
			// No tenant the table does not name is served — otherwise an
			// orphan row passes by checking only the tenants it lists.
			for tenant := range mgr.GetConfig().Tenants {
				if _, listed := tree.Expect[tenant]; !listed {
					t.Errorf("%s: /metrics carries %s, which the table does not name", tree.Name, tenant)
				}
			}
		})
	}
}

func overlayTree(m overlayMatrix, t *testing.T, name string) map[string]string {
	t.Helper()
	for _, tree := range m.Trees {
		if tree.Name == name {
			return tree.Files
		}
	}
	t.Fatalf("matrix has no tree %q", name)
	return nil
}

func TestPlatformFileOrphanTenantIsNamed(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)

	t.Run("orphan", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, overlayTree(m, t, "c1-declared-only-in-profiles-file"))
		_, buf := newOverlayManager(t, dir)
		assertLogLineWith(t, buf.String(), orphanAnchor, "WARN:", "_profiles.yaml", `"tx"`, "already exists")
	})
	// Control: the same platform entry with a tenant file declaring the
	// tenant (named so it sorts BEFORE the platform file) says nothing.
	t.Run("control-tenant-exists", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, overlayTree(m, t, "c2-c5-platform-only-keys-tenant-file-TX.yaml"))
		_, buf := newOverlayManager(t, dir)
		if lines := logLinesWith(buf.String(), orphanAnchor); len(lines) != 0 {
			t.Errorf("tenant exists, yet: %q", lines)
		}
	})
}

func TestNestedPlatformFileTenantsBlockIsNamed(t *testing.T) {
	t.Parallel()
	m := loadOverlayMatrix(t)

	t.Run("nested-tenants-block", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, overlayTree(m, t, "c4-nested-platform-file-tenants-block"))
		_, buf := newOverlayManager(t, dir)
		assertLogLineWith(t, buf.String(), nestedAnchor,
			"WARN:", filepath.Join("sub", "_defaults.yaml"), "tx, ty", "not read by any plane")
	})
	// Control: a nested platform file with no `tenants:` block is silent.
	t.Run("control-no-tenants-block", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		writeOverlayTree(t, dir, map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		})
		_, buf := newOverlayManager(t, dir)
		if lines := logLinesWith(buf.String(), nestedAnchor); len(lines) != 0 {
			t.Errorf("no tenants: block, yet: %q", lines)
		}
	})
}

// TestPlatformTenantOverlaySurvivesReload drives the same edits through
// both reload entries and checks precedence and existence after each one:
//
//   - IncrementalLoad: a tenant-file-only edit takes the patchTenants fast
//     path (reclaimTenantFrom's order), a platform-file edit the
//     incremental full rebuild (mergePartialConfigs);
//   - diffAndReload: the debounced production path (commitFlatFrom on the
//     reload's own scan).
//
// The tenant file is `TX.yaml` on purpose: it sorts BEFORE `_defaults.yaml`,
// which is the spelling that used to lose to the platform value.
func TestPlatformTenantOverlaySurvivesReload(t *testing.T) {
	t.Parallel()
	f := func(v float64) *float64 { return &v }
	const platform = "defaults:\n  mysql_connections: 80\n" +
		"tenants:\n  tx:\n    mysql_connections: \"60\"\n"
	steps := []struct {
		name   string
		mutate func(t *testing.T, dir string)
		want   *float64
		orphan bool // the reload names tx as an orphan of _defaults.yaml
	}{
		{"tenant-file-edit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "TX.yaml"),
				"tenants:\n  tx:\n    mysql_connections: \"70\"\n    redis_memory_used_bytes: \"1\"\n")
		}, f(70), false},
		{"platform-file-edit", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), platform+"    redis_memory_used_bytes: \"2\"\n")
		}, f(70), false},
		{"tenant-file-removed", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "TX.yaml")); err != nil {
				t.Fatal(err)
			}
		}, nil, true},
		{"tenant-file-back-silent", func(t *testing.T, dir string) {
			writeTestYAML(t, filepath.Join(dir, "0tx.yaml"), "tenants:\n  tx: {}\n")
		}, f(60), false},
	}
	reloaders := map[string]func(m *ConfigManager) error{
		"IncrementalLoad": func(m *ConfigManager) error { return m.IncrementalLoad() },
		"diffAndReload": func(m *ConfigManager) error {
			_, _, err := m.diffAndReload()
			return err
		},
	}
	for rname, reload := range reloaders {
		t.Run(rname, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeOverlayTree(t, dir, map[string]string{
				"_defaults.yaml": platform,
				"TX.yaml":        "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
				"ty.yaml":        "tenants:\n  ty: {}\n",
			})
			m, buf := newOverlayManager(t, dir)
			assertServed(t, m, rname+"/load", "tx", f(70))
			for i, st := range steps {
				buf.Reset()
				st.mutate(t, dir)
				touchTreeAt(t, dir, time.Now().Add(time.Duration(i+3)*time.Second))
				if err := reload(m); err != nil {
					t.Fatalf("%s/%s: %v", rname, st.name, err)
				}
				assertServed(t, m, rname+"/"+st.name, "tx", st.want)
				assertServed(t, m, rname+"/"+st.name+" (control)", "ty", f(80))
				got := len(logLinesWith(buf.String(), orphanAnchor)) > 0
				if got != st.orphan {
					t.Errorf("%s/%s: orphan WARN emitted=%v, want %v; log:\n%s", rname, st.name, got, st.orphan, buf.String())
				}
			}
		})
	}
}
