package main

// config_explicit_tag_test.go — #3063: a tenant file with a scalar whose
// explicit tag its text does not satisfy (`mysql_connections: !!int x`) is
// skipped whole by the real ConfigManager.Load, with the signals every other
// unparseable tenant file gets (WARN, da_config_parse_failure_total), and the
// file's other tenant goes with it. Before #3063 the file was served, the
// threshold at its default, while the merge plane refused it.

import (
	"path/filepath"
	"strings"
	"testing"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"
)

func TestMismatchedExplicitTag_RejectsTheFile(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  ta:\n    mysql_connections: \"70\"\n  tb:\n    mysql_connections: !!int zzleak42\n",
		"b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
	})
	fresh, body, logs := loadAndScrapeNonUTF8(t, dir)

	if !strings.Contains(body, `tenant="tc"`) {
		t.Errorf("tenant tc (declared by b.yaml) missing from /metrics")
	}
	for _, tid := range []string{"ta", "tb"} {
		if strings.Contains(body, `tenant="`+tid+`"`) {
			t.Errorf("tenant %s served, but a.yaml must be skipped whole", tid)
		}
	}
	assertLogLineWith(t, logs, "WARN: skip unparseable file", `a.yaml"`, "as a !!int")
	if got := promtest.ToFloat64(fresh.parseFailures.WithLabelValues("a.yaml")); got != 1 {
		t.Errorf("da_config_parse_failure_total{file_basename=a.yaml} = %v, want 1", got)
	}
	if strings.Contains(body, "zzleak42") {
		t.Errorf("/metrics carries the file's value")
	}
}

// Control: a tag the text satisfies is served as written.
func TestSatisfiedExplicitTag_Served(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  ta:\n    mysql_connections: !!int 70\n  tb:\n    mysql_connections: !!float 60.5\n",
	})
	fresh, body, logs := loadAndScrapeNonUTF8(t, dir)
	for _, want := range []string{`tenant="ta"} 70`, `tenant="tb"} 60.5`} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics lacks %s", want)
		}
	}
	if strings.Contains(logs, "skip unparseable file") {
		t.Errorf("file skipped:\n%s", logs)
	}
	if got := promtest.ToFloat64(fresh.parseFailures.WithLabelValues("a.yaml")); got != 0 {
		t.Errorf("da_config_parse_failure_total{file_basename=a.yaml} = %v, want 0", got)
	}
}

// The watch path, not only a cold Load: a.yaml and b.yaml are served, then
// a.yaml's value is rewritten to `!!int zz` and the tree reloaded the way the
// watch loop reloads it — the flat tree's incremental reload and the
// hierarchical one (root _defaults.yaml). a.yaml's tenant must leave
// /metrics, b.yaml's must stay, and the reload must count the failure once.
func TestMismatchedExplicitTag_RejectsTheFileOnReload(t *testing.T) {
	t.Parallel()
	for _, tree := range []struct {
		name         string
		hierarchical bool
	}{
		{"flat (incrementalLoadFrom)", false},
		{"hierarchical (root _defaults.yaml)", true},
	} {
		tree := tree
		t.Run(tree.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			if tree.hierarchical {
				writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults: {}\n")
			}
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  acme:\n    _silent_mode: warning\n")
			writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  zeta:\n    _silent_mode: warning\n")
			m, fresh, logs := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			m.mu.RLock()
			hier := m.hierarchy.enabled
			m.mu.RUnlock()
			if hier != tree.hierarchical {
				t.Fatalf("fixture precondition: hierarchical mode = %v, want %v", hier, tree.hierarchical)
			}
			if got := strings.Join(silentModeSeries(t, m), ","); got != "acme=warning,zeta=warning" {
				t.Fatalf("fixture precondition: user_silent_mode before the edit = %s", got)
			}
			before := parseFailureCount(fresh, "a.yaml")

			// _silent_mode stays valid, so a reload that served the file
			// would keep acme=warning on user_silent_mode.
			writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  acme:\n    _silent_mode: warning\n    mysql_connections: !!int zz\n")
			logs.Reset()
			if err := watchReload(m); err != nil {
				t.Fatalf("reload: %v", err)
			}

			if got := strings.Join(silentModeSeries(t, m), ","); got != "zeta=warning" {
				t.Errorf("user_silent_mode after the reload = %s, want zeta=warning (acme dropped with a.yaml, zeta kept)", got)
			}
			if d := parseFailureCount(fresh, "a.yaml") - before; d != 1 {
				t.Errorf("da_config_parse_failure_total{file_basename=a.yaml} moved by %v on the reload, want 1", d)
			}
			assertLogLineWith(t, logs.String(), "WARN: skip unparseable file", `a.yaml"`, "as a !!int")
		})
	}
}
