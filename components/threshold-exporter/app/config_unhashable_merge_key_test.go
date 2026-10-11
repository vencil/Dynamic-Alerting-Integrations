package main

// #2913 — file mode's reload path (WatchLoop → tickOnce → loadFile →
// config.ParseTenantFile, and Load) given a config file whose mapping holds a
// non-scalar key beside a merge key `<<`. yaml.v3's typed decode panicked on
// it ("hash of unhashable type"), and nothing on this path recovers, so the
// exporter died. It must now be what any other unparseable file is (the
// invalid-YAML control below): the tick WARNs and keeps the last good config,
// Load returns the parse error and keeps it too. The directory-mode reload
// paths are covered by TestABrokenTenantFileDropsItsTenantsOnEveryReloadPath.
//
// Injection discipline (test-map.md): logger via SetLogger, no global swaps.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// failOnPanic runs f and turns a panic in it into a t.Fatalf of the calling
// (sub)test. Nothing on the reload path recovers, so without it a regression
// (the vendored yaml.v3 patch gone) panics the test goroutine and ends the
// whole test binary instead of failing this one case.
func failOnPanic(t *testing.T, what string, f func()) {
	t.Helper()
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("%s panicked: %v", what, r)
		}
	}()
	f()
}

func TestFileModeReloadTreatsUnhashableMergeKeyAsParseError(t *testing.T) {
	t.Parallel()
	const good = "defaults:\n  mysql_connections: 80\ntenants:\n  acme:\n    mysql_connections: \"70\"\n"
	for name, body := range map[string]string{
		// The control: what an unparseable file has always done here.
		"invalid YAML (control)":               "tenants:\n  acme:\n    mysql_connections: [1\n",
		"sequence key, then merge of an alias": "base: &b {x: 1}\ntenants:\n  acme:\n    ? [a]\n    : 1\n    <<: *b\n",
		"sequence key, then merge of a list":   "tenants:\n  acme:\n    ? [a]\n    : 1\n    <<: &y [{x: 1}]\n",
		"merge, then mapping key":              "tenants:\n  acme:\n    <<: {x: 1}\n    ? {k: v}\n    : 1\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			path := filepath.Join(dir, "config.yaml")
			writeTestFile(t, dir, "config.yaml", good)
			logger, buf := newTestLogger()
			m := NewConfigManager(path)
			m.SetLogger(logger)
			if err := m.Load(); err != nil {
				t.Fatalf("initial Load: %v", err)
			}
			if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}

			buf.Reset()
			failOnPanic(t, "tick", m.tickOnce)
			if !strings.Contains(buf.String(), "WARN: cannot check config "+path+": parse config ") {
				t.Errorf("tick: want the parse-failure WARN, log was:\n%s", buf.String())
			}
			var err error
			failOnPanic(t, "Load", func() { err = m.Load() })
			if err == nil || !strings.HasPrefix(err.Error(), "parse config "+path+": ") {
				t.Errorf("Load: err = %v, want the parse-config error", err)
			}
			if !m.IsLoaded() || m.GetConfig().Defaults["mysql_connections"] != 80 {
				t.Errorf("the last good config was not kept: loaded=%v defaults=%v",
					m.IsLoaded(), m.GetConfig().Defaults)
			}
		})
	}
}
