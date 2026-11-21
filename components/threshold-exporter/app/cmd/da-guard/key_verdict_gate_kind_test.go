package main

// key_verdict_gate_kind_test.go — gate_kind (#3996): the key verdict names,
// on a finding, the kind of da-guard's own (main gate) finding that reports
// the same fact, read off the set the gate reports it from. validate-config
// shows such a fact once (its guard_errors row); without the mark it showed
// it in both rows, and the names differ for three of the four facts, so no
// reader can pair them by name.

import (
	"testing"
)

func TestKeyVerdict_GateKindNamesTheGatesFindingOfTheSameFact(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_connection_usage_percent: 80\n"
	tenant := "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70\"\n"
	for name, tc := range map[string]struct {
		files    map[string]string
		key      string
		reason   string
		gateKind any
	}{
		"subtree-reserved": {map[string]string{"_defaults.yaml": root, "team/_defaults.yaml": "_silent_mode: warning\n",
			"team/ta.yaml": tenant}, "_silent_mode", "subtree_default_reserved_key", "subtree_default_reserved_key"},
		"subtree-undeliverable": {map[string]string{"_defaults.yaml": root,
			// #4163: a registry key is in the built-in layer, so the root
			// declares it; a key outside the layer is what stays undeliverable.
			"team/_defaults.yaml": "defaults:\n  my_own_threshold: 30\n", "team/ta.yaml": tenant},
			"my_own_threshold", "undeliverable", "subtree_default_undeliverable"},
		// The verdict's reason is `retired`; the gate's finding is the
		// undeliverable one — paired by the set, never by the name.
		"subtree-retired": {map[string]string{"_defaults.yaml": root,
			"team/_defaults.yaml": "defaults:\n  mysql_connections: 30\n", "team/ta.yaml": tenant},
			"mysql_connections", "retired", "subtree_default_undeliverable"},
		"root-reserved": {map[string]string{"_defaults.yaml": root + "  _silent_mode: warning\n", "ta.yaml": tenant},
			"_silent_mode", "not_served", "root_defaults_reserved_key"},
		// Control: unread, and no gate finding says so — null.
		"misspelt": {map[string]string{"_defaults.yaml": root,
			"ta.yaml": tenant + "    mysql_conections: \"70\"\n"}, "mysql_conections", "no_row", nil},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			doc := runDoc(t, keyVerdictCmd, "--config-dir", writeConfD(t, tc.files), "--at", "2026-07-01T12:00:00Z")
			var hit map[string]any
			for _, f := range doc["findings"].([]any) {
				if f := f.(map[string]any); f["kind"] == "unread" && f["key"] == tc.key {
					hit = f
				}
			}
			if hit == nil || hit["reason"] != tc.reason || hit["gate_kind"] != tc.gateKind {
				t.Fatalf("unread %s: %v, want reason %v and gate_kind %v", tc.key, hit, tc.reason, tc.gateKind)
			}
		})
	}
}
