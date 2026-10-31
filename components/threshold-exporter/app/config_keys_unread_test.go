package main

// ADR-038 §11: the exporter states what it reads itself — the keys no row of
// it reads (the resolver's unconsumed record, #3378; a `_` key the registry
// lists no reader for, #3045) — in one line with no severity word, and no
// longer logs pkg/config's second judgement of them (`unknown key … not in
// defaults`). Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
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

// A key is the tenant's own text: quoted, it cannot open a log line of its
// own nor pass for another entry.
func TestKeysUnread_KeysAreQuoted(t *testing.T) {
	t.Parallel()
	line := formatKeysUnreadLog([]valueNotServed{{Tenant: "tx", Key: "_a\nWARN: forged; tenant=ty key=x", Reason: "unregistered"}}, "Config loaded")
	if strings.Contains(line, "\n") || strings.Contains(line, "; tenant=ty key=x reason") {
		t.Errorf("an unquoted key in the line: %s", line)
	}
}
