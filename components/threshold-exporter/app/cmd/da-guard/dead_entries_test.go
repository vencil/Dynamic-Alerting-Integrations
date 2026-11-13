package main

// #3502: the entries of a schedule /metrics serves at no minute of the day,
// through the three documents that carry them — served-values' keys[],
// effective's dead_entries, key-verdict's rows and findings — on one tree:
// a tenant file whose second window is inside its first, and a profile whose
// windows cover the whole day. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"
)

var deadEntriesTree = map[string]string{
	"_defaults.yaml": kvRoot,
	"_profiles.yaml": "profiles:\n  p1:\n    redis_memory:\n      default: \"60\"\n      overrides:\n" +
		"        - window: \"00:00-12:00\"\n          value: \"61\"\n" +
		"        - window: \"12:00-00:00\"\n          value: \"62\"\n",
	"a.yaml": "tenants:\n  t1:\n    _profile: p1\n    mysql_connection_usage_percent:\n      default: \"70\"\n      overrides:\n" +
		"        - window: \"22:00-06:00\"\n          value: \"1000\"\n" +
		"        - window: \"01:00-03:00\"\n          value: \"5\"\n" +
		"        - window: \"05:00-08:00\"\n          value: \"7\"\n" +
		"    mysql_slave_lag: \"40\"\n",
}

var (
	deadTenantEntry  = map[string]any{"entry": "overrides[1]", "window": "01:00-03:00", "value": "5", "reason": "window_covered"}
	deadProfileEntry = map[string]any{"entry": "default", "value": "60", "reason": "default_covered"}
)

func runDoc(t *testing.T, args ...string) map[string]any {
	t.Helper()
	var out, errOut bytes.Buffer
	if code := run(args, &out, &errOut); code != exitOK {
		t.Fatalf("%v: exit %d: %s", args, code, errOut.String())
	}
	var doc map[string]any
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	return doc
}

func TestDeadEntries_ThroughEveryDocument(t *testing.T) {
	t.Parallel()
	root := writeTree(t, deadEntriesTree)
	at := "2026-07-01T03:00:00Z"

	served := runDoc(t, "served-values", "--config-dir", root, "--at", at)
	keys := served["tenants"].(map[string]any)["t1"].(map[string]any)["keys"].(map[string]any)
	if got := keys["mysql_connection_usage_percent"].(map[string]any)["dead_entries"]; !reflect.DeepEqual(got, []any{deadTenantEntry}) {
		t.Errorf("served-values keys.mysql_connection_usage_percent.dead_entries = %v", got)
	}
	if _, has := keys["mysql_slave_lag"].(map[string]any)["dead_entries"]; has {
		t.Errorf("a scalar key carries dead_entries: %v", keys["mysql_slave_lag"])
	}

	eff := runDoc(t, "effective", "--config-dir", root)
	dead := eff["tenants"].(map[string]any)["t1"].(map[string]any)["dead_entries"].(map[string]any)
	want := map[string]any{"mysql_connection_usage_percent": []any{deadTenantEntry}, "redis_memory": []any{deadProfileEntry}}
	if !reflect.DeepEqual(dead, want) {
		t.Errorf("effective dead_entries = %v, want %v", dead, want)
	}

	kv := runDoc(t, keyVerdictCmd, "--config-dir", root, "--at", at)
	var got []map[string]any
	for _, f := range kv["findings"].([]any) {
		if f := f.(map[string]any); f["kind"] == "dead_entry" {
			got = append(got, f)
		}
	}
	wantF := []map[string]any{
		{"severity": "error", "kind": "dead_entry", "key": "redis_memory", "file": "_profiles.yaml",
			"tenants": []any{"t1"}, "reason": "default_covered", "hint": nil, "base": nil,
			"dead_entries": []any{deadProfileEntry}, "blocks": true},
		{"severity": "error", "kind": "dead_entry", "key": "mysql_connection_usage_percent", "file": "a.yaml",
			"tenants": []any{"t1"}, "reason": "window_covered", "hint": nil, "base": nil,
			"dead_entries": []any{deadTenantEntry}, "blocks": true},
	}
	if !reflect.DeepEqual(got, wantF) {
		t.Errorf("key-verdict dead_entry findings\n got %v\nwant %v", got, wantF)
	}
	if kv["blocks"] != true {
		t.Errorf("key-verdict blocks = %v", kv["blocks"])
	}
}

// A key /metrics serves nothing of has no dead entry in either document
// (#3502 review): an undeclared key and an undeliverable subtree value say
// why through their own reason, effective's not_served and served-values'
// keys[].reason alike.
func TestDeadEntries_NoneOnAKeyServedNothing(t *testing.T) {
	t.Parallel()
	sched := "      default: \"70\"\n      overrides:\n" +
		"        - window: \"22:00-06:00\"\n          value: \"1000\"\n" +
		"        - window: \"01:00-03:00\"\n          value: \"5\"\n"
	root := writeTree(t, map[string]string{
		"_defaults.yaml":      kvRoot,
		"team/_defaults.yaml": "defaults:\n  kafka_lag:\n" + sched,
		"team/a.yaml":         "tenants:\n  t1:\n    mysql_conections:\n" + sched,
	})
	served := runDoc(t, "served-values", "--config-dir", root, "--at", "2026-07-01T03:00:00Z")
	keys := served["tenants"].(map[string]any)["t1"].(map[string]any)["keys"].(map[string]any)
	for _, k := range []string{"mysql_conections", "kafka_lag"} {
		e, ok := keys[k].(map[string]any)
		if !ok {
			t.Fatalf("served-values has no key %s: %v", k, keys)
		}
		if _, has := e["dead_entries"]; has || e["exporter"] != false {
			t.Errorf("served-values keys.%s = %v", k, e)
		}
	}
	eff := runDoc(t, "effective", "--config-dir", root)
	te := eff["tenants"].(map[string]any)["t1"].(map[string]any)
	if dead := te["dead_entries"].(map[string]any); len(dead) != 0 {
		t.Errorf("effective dead_entries = %v (not_served %v)", dead, te["not_served"])
	}
	ns := te["not_served"].(map[string]any)
	for _, k := range []string{"mysql_conections", "kafka_lag"} {
		if _, named := ns[k]; !named {
			t.Errorf("effective not_served lacks %s: %v", k, ns)
		}
	}
}
