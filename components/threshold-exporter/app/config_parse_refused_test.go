package main

// config_parse_refused_test.go — ADR-036 step 6 (#3206): the exporter does not
// serve a conf.d file `da-guard parse-tree` refuses, and says so with the
// kind: da_config_parse_failure_total as for any parse failure, plus
// da_config_parse_refused_total{file_basename,kind}, and the kind in the
// log line. Driven through the real ConfigManager with injected metrics and
// logger (the seams of docs/internal/test-map.md); pkg/config's
// parse_verdict_test.go holds the same matrix against LoadDir and
// ResolveEffective.

import (
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"
)

// parseRefusedShapes: the nine shapes ADR-036 measured (seven the exporter
// read before step 6, two it already refused) and a control (kind "").
var parseRefusedShapes = []struct{ name, extra, kind string }{
	{"multiple documents", "---\nx: 1\n", "multiple_documents"},
	{"!!binary", "x: !!binary aGVsbG8=\n", "tag_not_allowed"},
	{"!!set", "x: !!set {a: null}\n", "tag_not_allowed"},
	{"BOM mid-file", "# note \uFEFF here\n", "bom_not_at_start"},
	{"null key", "~: 1\n", "null_key"},
	{"non-specific tag", "x: ! \"y\"\n", "non_specific_tag"},
	{"trailing --- only", "---\n", "multiple_documents"},
	{"duplicate key", "x: 1\nx: 2\n", "duplicate_key"},
	{"non-scalar key", "? [a]\n: 1\n", "non_scalar_key"},
	{"control", "", ""},
}

const (
	prTenant1  = "tenants:\n  t1:\n    mysql_connections: \"80\"\n"
	prTenant2  = "tenants:\n  t2:\n    mysql_connections: \"90\"\n"
	prDefaults = "defaults:\n  mysql_connections: 70\n  mysql_slow_queries: 50\n"
)

func TestParseRefusedMatrix_MetricAndLog(t *testing.T) {
	t.Parallel()
	for _, s := range parseRefusedShapes {
		for _, inDefaults := range []bool{false, true} {
			s, inDefaults := s, inDefaults
			broken := map[bool]string{false: "t1.yaml", true: "_defaults.yaml"}[inDefaults]
			t.Run(s.name+"/"+broken, func(t *testing.T) {
				t.Parallel()
				dir := t.TempDir()
				files := map[string]string{"_defaults.yaml": prDefaults, "t1.yaml": prTenant1, "t2.yaml": prTenant2}
				files[broken] += s.extra
				for name, body := range files {
					writeTestYAML(t, filepath.Join(dir, name), body)
				}
				m, fresh, logs := newAuditedManager(t, dir)
				if err := m.Load(); err != nil {
					t.Fatalf("Load: %v", err)
				}
				failures := parseFailureCount(fresh, broken)
				refused := 0.0
				if s.kind != "" {
					refused = promtest.ToFloat64(fresh.parseRefused.WithLabelValues(broken, s.kind))
				}
				t1, t1ok := seriesFor(t, m, "t1", "connections")
				_, t2ok := seriesFor(t, m, "t2", "connections")
				_, cpuOK := seriesFor(t, m, "t2", "slow_queries")

				if s.kind == "" {
					if failures != 0 || promtest.CollectAndCount(fresh.parseRefused) != 0 {
						t.Errorf("control: parse_failure %v, parse_refused series %d; want 0 and 0", failures, promtest.CollectAndCount(fresh.parseRefused))
					}
					if !t1ok || t1 != 80 || !t2ok || !cpuOK {
						t.Errorf("control: t1 %v (%v), t2 %v, t2 slow_queries %v; want all served", t1, t1ok, t2ok, cpuOK)
					}
					return
				}
				// Same unit: every refused increment is a failure increment.
				if failures == 0 || refused != failures {
					t.Errorf("parse_failure{%s} = %v, parse_refused{%s,%s} = %v; want equal and > 0",
						broken, failures, broken, s.kind, refused)
				}
				if got := promtest.CollectAndCount(fresh.parseRefused); got != 1 {
					t.Errorf("parse_refused has %d series, want 1 (one file, one kind)", got)
				}
				line := map[bool]string{false: "WARN: skip unparseable file", true: "ERROR: skip unparseable defaults/profiles file"}[inDefaults]
				assertLogLineWith(t, logs.String(), line, broken+`"`, "(kind "+s.kind+")")
				if inDefaults {
					// Dropped whole: every tenant is still loaded, without the
					// defaults only this file supplied — here every threshold,
					// as a key no root default declares is not served.
					body := scrapeBody(t, m)
					if !strings.Contains(body, `tenant="t1"`) || !strings.Contains(body, `tenant="t2"`) || t1ok || cpuOK {
						t.Errorf("t1 %v (%v), t2 slow_queries %v, tenants on /metrics t1=%v t2=%v; want both tenants, no threshold",
							t1, t1ok, cpuOK, strings.Contains(body, `tenant="t1"`), strings.Contains(body, `tenant="t2"`))
					}
					return
				}
				// The tenant file's tenant is not served; the other tenant is.
				if t1ok || !t2ok || !cpuOK {
					t.Errorf("t1 served %v, t2 %v, t2 slow_queries %v; want t1 gone, t2 and its defaults served", t1ok, t2ok, cpuOK)
				}
			})
		}
	}
}

// The same verdict on a hot reload (the flat plane's incremental path and the
// hierarchical one): a file edited into a refused shape drops what it served.
func TestParseRefused_OnReload(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name         string
		hierarchical bool
		file         string
	}{
		{"flat, tenant file", false, "t1.yaml"},
		{"hierarchical, tenant file", true, "t1.yaml"},
		{"hierarchical, _defaults.yaml", true, "_defaults.yaml"},
	} {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			// Flat mode: no `_defaults.yaml`, so no threshold is declared;
			// a tenant shows on /metrics by its per-tenant series.
			bodies := map[string]string{"t1.yaml": prTenant1, "t2.yaml": prTenant2}
			if tc.hierarchical {
				bodies["_defaults.yaml"] = prDefaults
			}
			for name, body := range bodies {
				writeTestYAML(t, filepath.Join(dir, name), body)
			}
			m, fresh, logs := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if !strings.Contains(scrapeBody(t, m), `tenant="t1"`) {
				t.Fatal("fixture precondition: t1 not served before the edit")
			}
			writeTestYAML(t, filepath.Join(dir, tc.file), bodies[tc.file]+"---\nx: 1\n")
			logs.Reset()
			if err := watchReload(m); err != nil {
				t.Fatalf("reload: %v", err)
			}
			if got := promtest.ToFloat64(fresh.parseRefused.WithLabelValues(tc.file, "multiple_documents")); got == 0 {
				t.Errorf("parse_refused{%s,multiple_documents} did not move on the reload", tc.file)
			}
			if !strings.Contains(logs.String(), "(kind multiple_documents)") {
				t.Errorf("the reload log does not name the kind:\n%s", logs.String())
			}
			t1ok := strings.Contains(scrapeBody(t, m), `tenant="t1"`)
			_, cpuOK := seriesFor(t, m, "t2", "slow_queries")
			if tc.file == "t1.yaml" && t1ok {
				t.Errorf("t1 still served after its file became a second-document file")
			}
			if tc.file == "_defaults.yaml" && (cpuOK || !t1ok) {
				t.Errorf("t2 slow_queries %v; want t1 kept and the dropped defaults' thresholds gone", cpuOK)
			}
		})
	}
}

// scrapeBody is one /metrics response of m.
func scrapeBody(t *testing.T, m *ConfigManager) string {
	t.Helper()
	rec := httptest.NewRecorder()
	NewThresholdCollector(m).MetricsHandler().ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	return rec.Body.String()
}
