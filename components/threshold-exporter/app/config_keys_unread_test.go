package main

// ADR-038 §11: the exporter states what it reads itself — the keys no row of
// it reads (the resolver's unconsumed record, #3378; a `_` key the registry
// lists no reader for, #3045) — in one line with no severity word, and no
// longer logs pkg/config's second judgement of them (`unknown key … not in
// defaults`). Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"fmt"
	"log"
	"path/filepath"
	"strings"
	"testing"
)

func TestKeysUnread_TheExporterStatesWhatItDoesNotRead(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, "defaults:\n  mysql_connections: 80\ntenants:\n  tx:\n"+
		"    mysql_conections: \"5\"\n"+ // misspelt: no row reads it
		"    _myth: \"1\"\n"+ // a `_` key the registry does not list
		"    _silent_mode: warning\n"+ // registered: read
		"    mysql_conectionz: disable\n"+ // a misspelt key switched off: the disable switches nothing off
		"    mysql_connections: \"70\"\n"+
		"  ty:\n    mysql_connections: disable\n") // a real key switched off: read, not listed
	fresh, _ := freshMetrics(t)
	var logBuf bytes.Buffer
	m := NewConfigManager(path)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logBuf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	lines := logLinesWith(logBuf.String(), keysUnreadLogPrefix)
	if len(lines) != 1 {
		t.Fatalf("keys-unread lines %q, want one\n%s", lines, logBuf.String())
	}
	line := lines[0]
	for _, want := range []string{`tenant="tx" key="mysql_conections" reason=unconsumed`,
		`tenant="tx" key="_myth" reason=unregistered`, `tenant="tx" key="mysql_conectionz" reason=disabled_unread`} {
		if !strings.Contains(line, want) {
			t.Errorf("line lacks %q: %s", want, line)
		}
	}
	for _, not := range []string{"_silent_mode", `key="mysql_connections"`, `tenant="ty"`, "WARN", "ERROR"} {
		if strings.Contains(line, not) {
			t.Errorf("line carries %q: %s", not, line)
		}
	}
	if strings.Contains(logBuf.String(), "not in defaults") || strings.Contains(logBuf.String(), "unknown reserved key") {
		t.Errorf("the second judgement of unread keys is still logged:\n%s", logBuf.String())
	}

	// Once per change of the set: an unchanged reload says nothing again.
	before := len(logLinesWith(logBuf.String(), keysUnreadLogPrefix))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if after := len(logLinesWith(logBuf.String(), keysUnreadLogPrefix)); after != before {
		t.Errorf("keys-unread line repeated on an unchanged reload (%d → %d)", before, after)
	}
}

// A tenant id or key is the tenant's own text: quoted, it cannot open a log
// line of its own nor pass for another entry.
func TestKeysUnread_FieldsAreQuoted(t *testing.T) {
	t.Parallel()
	const forged = "_a\nWARN: forged; tenant=ty key=x"
	for _, tc := range []struct{ name, tenant, key string }{
		{"key", "tx", forged},
		{"tenant", forged, "_a"},
	} {
		line := formatKeysUnreadLog([]valueNotServed{{Tenant: tc.tenant, Key: tc.key, Reason: "unregistered"}}, "Config loaded")
		if strings.Contains(line, "\n") || strings.Contains(line, "; tenant=ty key=x reason") {
			t.Errorf("%s: an unquoted field in the line: %s", tc.name, line)
		}
	}
}

// Every unread key is named, however many: the line is the only place the
// exporter states them, so a sample would hide the rest.
func TestKeysUnread_NamesEveryKey(t *testing.T) {
	t.Parallel()
	var ku []valueNotServed
	for i := 0; i < valuesNotServedLogSampleLimit+5; i++ {
		ku = append(ku, valueNotServed{Tenant: "tx", Key: fmt.Sprintf("k%02d", i), Reason: "unconsumed"})
	}
	line := formatKeysUnreadLog(ku, "Config loaded")
	for _, v := range ku {
		if !strings.Contains(line, fmt.Sprintf("key=%q", v.Key)) {
			t.Errorf("line omits %q: %s", v.Key, line)
		}
	}
	if strings.Contains(line, "more") {
		t.Errorf("line samples the keys: %s", line)
	}
}
