package main

// config_nonutf8_label_test.go — #2266: a string that is not valid UTF-8
// reaching a Prometheus label value. client_golang's WithLabelValues panics
// on one; two data sources fed it:
//
//   - a tenant id (`!!binary dP8=` decodes to "t\xff") — the panic fired in
//     Collect, on the goroutine Registry.Gather starts without a recover, so
//     the first scrape killed the process;
//   - the basename of a file that fails to parse (Linux file names are
//     bytes) — the panic fired inside Load.
//
// The tree tests drive the real ConfigManager.Load and the real /metrics
// handler, so the first two fail (the binary panics) on a tree without the
// fix. The rest pin the fix's edges: the rejection is the TENANT file's
// verdict only, and no log line carries a file name's raw bytes.

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"
)

func writeNonUTF8Tree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range files {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatalf("write %q: %v", name, err)
		}
	}
	return dir
}

func loadAndScrapeNonUTF8(t *testing.T, dir string) (*configMetrics, string, string) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	var logBuf bytes.Buffer
	mgr := NewConfigManager(dir)
	mgr.SetMetrics(fresh)
	mgr.SetLogger(log.New(&logBuf, "", 0))
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	rec := httptest.NewRecorder()
	NewThresholdCollector(mgr).MetricsHandler().ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/metrics status = %d, want 200; body: %.300s", rec.Code, rec.Body.String())
	}
	return fresh, rec.Body.String(), logBuf.String()
}

// Tree A: a non-UTF-8 tenant id rejects its whole file, like any other
// decode failure; another file's tenant is served as usual.
func TestNonUTF8TenantID_RejectsTheFile_ScrapeSurvives(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  !!binary dP8=:\n    mysql_connections: \"70\"\n  tb:\n    mysql_connections: \"60\"\n",
		"b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
	})
	fresh, body, logs := loadAndScrapeNonUTF8(t, dir)

	if !strings.Contains(body, `tenant="tc"`) {
		t.Errorf("tenant tc (declared by b.yaml) missing from /metrics")
	}
	if strings.Contains(body, `tenant="tb"`) {
		t.Errorf("tenant tb served, but a.yaml declares a non-UTF-8 tenant id and must be skipped whole")
	}
	assertLogLineWith(t, logs, "WARN: skip unparseable file", `a.yaml"`, `tenant id "t\xff" is not valid UTF-8`)
	if got := promtest.ToFloat64(fresh.parseFailures.WithLabelValues("a.yaml")); got != 1 {
		t.Errorf("da_config_parse_failure_total{file_basename=a.yaml} = %v, want 1", got)
	}
}

// Tree B: a file that fails to parse and whose name is not valid UTF-8 is
// still counted — under the U+FFFD-sanitised name — and Load goes on.
func TestNonUTF8FileName_ParseFailureCountedSanitised(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"b\xff.yaml":     "tenants:\n  tx:\n    mysql_connections: [80\n",
		"c.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
	})
	fresh, body, logs := loadAndScrapeNonUTF8(t, dir)

	if !strings.Contains(body, `tenant="tc"`) {
		t.Errorf("tenant tc missing from /metrics")
	}
	if got := promtest.ToFloat64(fresh.parseFailures.WithLabelValues("b�.yaml")); got != 1 {
		t.Errorf("da_config_parse_failure_total{file_basename=%q} = %v, want 1", "b�.yaml", got)
	}
	assertLogLineWith(t, logs, "WARN: skip unparseable file", `b\xff.yaml"`)
	if strings.Contains(logs, "b\xff") {
		t.Errorf("raw non-UTF-8 file name bytes written to the log; want %%q-escaped")
	}
}

// The backstop: whatever source a non-UTF-8 tenant id comes from, the scrape
// skips that tenant's series instead of panicking. No Load path produces
// such a config any more (Load rejects the id), so it is preloaded directly.
func TestScrape_NonUTF8TenantInConfig_Skipped(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"t\xff": {"mysql_connections": SV("70")},
			"tb":    {"mysql_connections": SV("60")},
		},
	}
	rec := httptest.NewRecorder()
	NewThresholdCollector(newTestManager(cfg)).MetricsHandler().ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/metrics status = %d, want 200; body: %.300s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	for _, want := range []string{`da_tenant_metrics_over_limit{tenant="tb"} 0`, `tenant="tb"} 60`} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics lacks %s", want)
		}
	}
	if strings.Contains(body, "t\xff") || strings.Contains(body, `t\xff`) {
		t.Errorf("/metrics carries the non-UTF-8 tenant")
	}
}

// A `_` platform file's `tenants:` entry never declares a tenant, so a
// non-UTF-8 key there cannot reach a label: it is dropped as an orphan with a
// %q-quoted WARN, and the rest of the file — every tenant's defaults — still
// applies. Rejecting the whole file dropped them all (#2266 blind review, F1).
func TestNonUTF8TenantEntryInPlatformFile_DroppedNotTheFile(t *testing.T) {
	t.Parallel()
	for name, tree := range map[string]map[string]string{
		"_defaults.yaml carries it": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 7\ntenants:\n  !!binary dP8=:\n    mysql_connections: \"70\"\n",
			"b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
		},
		"_platform.yaml carries it": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 7\n",
			"_platform.yaml": "tenants:\n  !!binary dP8=:\n    mysql_connections: \"70\"\n  tc:\n    mysql_slow_queries: \"9\"\n",
			"b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			fresh, body, logs := loadAndScrapeNonUTF8(t, writeNonUTF8Tree(t, tree))
			want := `user_threshold{component="mysql",metric="slow_queries",severity="warning",tenant="tc"} 7`
			if _, platform := tree["_platform.yaml"]; platform {
				want = `user_threshold{component="mysql",metric="slow_queries",severity="warning",tenant="tc"} 9`
			}
			if !strings.Contains(body, want) {
				t.Errorf("platform-supplied value missing from /metrics, want line %s", want)
			}
			if got := promtest.CollectAndCount(fresh.parseFailures); got != 0 {
				t.Errorf("da_config_parse_failure_total has %d series, want 0 — the platform file is not broken", got)
			}
			assertLogLineWith(t, logs, "WARN: tenants entry", `"t\xff"`, "_", "not valid UTF-8")
			if strings.Contains(logs, "\xff") {
				t.Errorf("raw non-UTF-8 bytes written to the log")
			}
		})
	}
}

// Every log line that names a broken file quotes it (%q), so a file name that
// is not valid UTF-8 never puts raw bytes into the log: a broken root `_`
// file (parsePartialConfig's ERROR) and a broken nested defaults file (the
// nested-platform probe, the parsedDefaults caches and the per-tenant chain
// ERROR in emitParseFailureSignal).
func TestNonUTF8BrokenPlatformFileName_NoRawBytesInLog(t *testing.T) {
	t.Parallel()
	for name, tree := range map[string]map[string]string{
		"root _ file": {
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"_x\xff.yaml":    "defaults: [80\n",
			"b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
		},
		"nested defaults file": {
			"_defaults.yaml":       "defaults:\n  mysql_connections: 80\n",
			"s\xff/_defaults.yaml": "defaults: [80\n",
			"s\xff/b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
		},
		"nested platform file with a tenants: block": {
			"_defaults.yaml":       "defaults:\n  mysql_connections: 80\n",
			"_x.yaml":              "defaults: [80\n", // keeps the "reported" anchor below
			"s\xff/_defaults.yaml": "defaults:\n  mysql_connections: 70\ntenants:\n  !!binary dP8=:\n    mysql_connections: \"1\"\n  tc:\n    mysql_connections: \"2\"\n",
			"s\xff/b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			_, body, logs := loadAndScrapeNonUTF8(t, writeNonUTF8Tree(t, tree))
			if !strings.Contains(body, `tenant="tc"`) {
				t.Errorf("tenant tc missing from /metrics")
			}
			if !strings.Contains(logs, "skip unparseable defaults/profiles file") {
				t.Fatalf("the broken file was not reported; log:\n%q", logs)
			}
			if strings.Contains(name, "tenants:") {
				assertLogLineWith(t, logs, "tenants: block in nested platform file", `"t\xff"`, `\xff/_defaults.yaml"`)
			}
			for _, l := range strings.Split(logs, "\n") {
				if strings.Contains(l, "\xff") {
					t.Errorf("raw non-UTF-8 bytes in log line %q", l)
				}
			}
		})
	}
}

// Several non-UTF-8 ids in one tenant file: the rejection names all of them,
// sorted, so the line is the same on every load (map order is random).
func TestNonUTF8TenantIDs_AllNamedInAStableOrder(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		// !!binary: "c\xff", "a\xff", "b\xff"
		"a.yaml": "tenants:\n  !!binary Y/8=:\n    mysql_connections: \"1\"\n  !!binary Yf8=:\n    mysql_connections: \"2\"\n  !!binary Yv8=:\n    mysql_connections: \"3\"\n",
		"b.yaml": "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
	})
	const want = `tenant ids ["a\xff" "b\xff" "c\xff"] are not valid UTF-8`
	for i := 0; i < 20; i++ {
		_, _, logs := loadAndScrapeNonUTF8(t, dir)
		lines := logLinesWith(logs, "WARN: skip unparseable file")
		if len(lines) != 1 || !strings.Contains(lines[0], want) {
			t.Fatalf("load %d: rejection line = %q, want one line containing %s", i, lines, want)
		}
	}
}

// The reload path has its own parsedDefaults rebuild (rebuildParsedDefaults)
// with its own WARN; a cold Load never reaches it. Edit the broken nested
// defaults file (still broken) and reload.
func TestNonUTF8BrokenNestedDefaults_Reload_NoRawBytesInLog(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"_defaults.yaml":       "defaults:\n  mysql_connections: 80\n",
		"s\xff/_defaults.yaml": "defaults: [80\n",
		"s\xff/b.yaml":         "tenants:\n  tc:\n    mysql_connections: \"50\"\n",
	})
	var logBuf bytes.Buffer
	mgr := NewConfigManager(dir)
	mgr.SetMetrics(newConfigMetrics())
	mgr.SetLogger(log.New(&logBuf, "", 0))
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	p := filepath.Join(dir, "s\xff", "_defaults.yaml")
	if err := os.WriteFile(p, []byte("defaults: [81\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(time.Minute)
	if err := os.Chtimes(p, future, future); err != nil {
		t.Fatal(err)
	}
	logBuf.Reset()
	if _, _, err := mgr.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	logs := logBuf.String()
	assertLogLineWith(t, logs, "WARN: parsedDefaults: parse", `\xff/_defaults.yaml"`)
	if strings.Contains(logs, "\xff") {
		t.Errorf("raw non-UTF-8 bytes in the reload log:\n%q", logs)
	}
}

// File mode: the one config file declares the tenants, so it gets the tenant
// file's verdict — Load fails — instead of loading and panicking the scrape.
func TestNonUTF8TenantID_SingleFileMode_LoadFails(t *testing.T) {
	t.Parallel()
	dir := writeNonUTF8Tree(t, map[string]string{
		"cfg.yaml": "defaults:\n  mysql_connections: 80\ntenants:\n  !!binary dP8=:\n    mysql_connections: \"70\"\n  tb:\n    mysql_connections: \"60\"\n",
	})
	mgr := NewConfigManager(filepath.Join(dir, "cfg.yaml"))
	mgr.SetMetrics(newConfigMetrics())
	mgr.SetLogger(log.New(io.Discard, "", 0))
	err := mgr.Load()
	if err == nil || !strings.Contains(err.Error(), `tenant id "t\xff" is not valid UTF-8`) {
		t.Fatalf("Load() = %v, want the non-UTF-8 tenant id rejection", err)
	}
}
