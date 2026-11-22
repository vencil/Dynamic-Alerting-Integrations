package main

// #4173: a root `defaults:` `<base>_critical` is every tenant's critical
// default. When it serves a tenant no row, key-verdict says why as for a
// root dimensional default (#2031, #3046): switched off with its base
// (read, switched_off_with) or no row at all (no_row) — not not_served. And
// a tenant `_critical` value the reader refuses is advised as a whole number.

import (
	"encoding/json"
	"strings"
	"testing"
)

type verdictRow struct {
	Key             string  `json:"key"`
	File            *string `json:"file"`
	Verdict         string  `json:"verdict"`
	Reason          *string `json:"reason"`
	SwitchedOffWith *string `json:"switched_off_with"`
}

func rootCriticalVerdict(t *testing.T, files map[string]string, tenant, key string) (verdictRow, bool) {
	t.Helper()
	dir := builtinTree(t, files)
	_, stdout, stderr := runOnce(t, keyVerdictCmd, "--config-dir", dir)
	var doc struct {
		Blocks  bool                    `json:"blocks"`
		Tenants map[string][]verdictRow `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("key-verdict: %v\n%s\n%s", err, stdout, stderr)
	}
	for _, r := range doc.Tenants[tenant] {
		if r.Key == key {
			return r, doc.Blocks
		}
	}
	t.Fatalf("no verdict for %s/%s:\n%s", tenant, key, stdout)
	return verdictRow{}, doc.Blocks
}

func TestKeyVerdict_ARootCriticalOffWithItsBaseIsRead(t *testing.T) {
	t.Parallel()
	r, blocks := rootCriticalVerdict(t, map[string]string{
		"_defaults.yaml": "defaults:\n  pg_connections: 80\n  pg_connections_critical: 95\n",
		"t.yaml":         "tenants:\n  tb:\n    pg_connections: disable\n",
	}, "tb", "pg_connections_critical")
	if blocks || r.Verdict != "read" || r.SwitchedOffWith == nil || *r.SwitchedOffWith != "pg_connections" {
		t.Errorf("verdict %+v (blocks %v), want read, switched off with pg_connections, no block", r, blocks)
	}
}

func TestKeyVerdict_ARootCriticalOverAnUnvaluedBaseHasNoRow(t *testing.T) {
	t.Parallel()
	r, _ := rootCriticalVerdict(t, map[string]string{
		"_defaults.yaml": "defaults:\n  pg_connections: 80\n  foo_bar_critical: 5\n",
		"t.yaml":         "tenants:\n  tb: {}\n",
	}, "tb", "foo_bar_critical")
	if r.Verdict != "unread" || r.Reason == nil || *r.Reason != keyReasonNoRow {
		t.Errorf("verdict %+v, want unread / no_row (as a dead root dimensional default)", r)
	}
}

func TestGuard_AnUnreadableTenantCriticalIsAdvisedAWholeNumber(t *testing.T) {
	t.Parallel()
	dir := builtinTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  pg_connections: 80\n  pg_connections_critical: 95\n",
		"t.yaml":         "tenants:\n  tb:\n    pg_connections_critical: \"90:critical\"\n",
	})
	_, stdout, _ := runOnce(t, "--config-dir", dir, "--format", "json")
	var doc struct {
		Report struct {
			Findings []struct {
				Kind    string `json:"kind"`
				Message string `json:"message"`
			} `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("%v\n%s", err, stdout)
	}
	found := false
	for _, f := range doc.Report.Findings {
		if f.Kind != "value_not_served" {
			continue
		}
		found = true
		if strings.Contains(f.Message, "`70:critical`") || !strings.Contains(f.Message, "root `defaults:` critical value") {
			t.Errorf("advice %q: want the whole-number advice and the root fallback", f.Message)
		}
	}
	if !found {
		t.Errorf("no value_not_served finding:\n%s", stdout)
	}
}
