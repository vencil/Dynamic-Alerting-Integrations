package main

import (
	"bytes"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// #4031: a `_` key of the root `defaults:` no reader reads is the key
// verdict's error on the root carrier, with or without a tenant — the one
// verdict on it (validate-config's root_defaults row no longer says it).
func TestKeyVerdict_RootDefaultsUnreadKeyWithOrWithoutATenant(t *testing.T) {
	t.Parallel()
	const defaults = "defaults:\n  mysql_connection_usage_percent: 80\n  _bogus: 1\n  _metadata: x\n"
	for name, c := range map[string]struct {
		files map[string]string
		want  []string
	}{
		"no tenant": {map[string]string{"_defaults.yaml": defaults}, []string{
			"error unread _bogus _defaults.yaml unregistered []",
			"error unread _metadata _defaults.yaml merge_dropped []",
		}},
		"a tenant": {map[string]string{"_defaults.yaml": defaults,
			"ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"}, []string{
			// A key the tenant inherits is its row's finding, naming it;
			// `_metadata` the merge drops reaches no tenant.
			"error unread _bogus _defaults.yaml unregistered [ta]",
			"error unread _metadata _defaults.yaml merge_dropped []",
		}},
		// The load drops the root carrier (a mapping as a threshold's
		// value): its parse_failed speaks for the file and the root's own
		// findings are not given — `_metadata` is absent. (`_bogus` is the
		// tenant row's: effective still reads a dropped root's values.)
		"a root the load drops": {map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: {a: 1}\n  _bogus: 1\n  _metadata: x\n",
			"ta.yaml":        "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n"},
			[]string{"error unread _bogus _defaults.yaml unregistered [ta]"}},
	} {
		dir := writeReservedConfD(t, c.files)
		var out, errOut bytes.Buffer
		if code := run([]string{keyVerdictCmd, "--config-dir", dir}, &out, &errOut); code != exitOK &&
			(name != "a root the load drops" || code != exitParseFailed) {
			t.Fatalf("%s: exit %d: %s", name, code, errOut.String())
		}
		var doc struct {
			Findings []struct {
				Severity, Kind, Key string
				File                *string
				Tenants             []string
				Reason              *string
			} `json:"findings"`
		}
		if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
			t.Fatal(err)
		}
		var got []string
		for _, f := range doc.Findings {
			if strings.HasPrefix(f.Key, "_") {
				got = append(got, strings.Join([]string{f.Severity, f.Kind, f.Key, *f.File, *f.Reason,
					"[" + strings.Join(f.Tenants, ",") + "]"}, " "))
			}
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: findings %q, want %q", name, got, c.want)
		}
	}
}

// #4031: root_defaults_reserved_key's fix by the key's shape — only a key
// some reader reads takes effect in a tenant's own entry.
func TestGuard_RootReservedKeyFixByShape(t *testing.T) {
	t.Parallel()
	dir := writeReservedConfD(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _routingProfile: x\n  _state_foo: 1\n  _silent_mode: warning\n",
		"t.yaml":         "tenants:\n  t1:\n    _metadata:\n      owner: x\n",
	})
	_, stdout, _ := runOnce(t, "--format", "json", "--config-dir", dir)
	var doc struct {
		Report *reportedReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	msg := map[string]string{}
	for _, f := range doc.Report.Findings {
		if f.Kind == "root_defaults_reserved_key" {
			msg[strings.TrimPrefix(f.Field, "_defaults.yaml:defaults.")] = f.Message
		}
	}
	for k, want := range map[string]string{
		"_routingProfile": "It is not a recognised key",
		"_state_foo":      "does not declare a filter `foo`",
		"_silent_mode":    "set `_silent_mode` in each tenant's own entry",
	} {
		if !strings.Contains(msg[k], want) {
			t.Errorf("%s fix = %q; want %q", k, msg[k], want)
		}
	}
	for _, k := range []string{"_routingProfile", "_state_foo"} {
		if strings.Contains(msg[k], "To have it take effect, set") {
			t.Errorf("%s fix = %q; tells a tenant entry it is unread in", k, msg[k])
		}
	}
}

// #4031 review: the root's unread `_` keys are a fault of the file, judged
// whether or not every tenant is read — a write that brings a dropped or set
// aside file back while editing the root is not refused for a key the tree
// already had; one that adds such a key is.
func TestWrite_RootDefaultsUnreadKeyIsTheTreesNotTheWrites(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connection_usage_percent: 80\n  _metadata: x\n"
	setRoot := `{"path":"_defaults.yaml","ops":[{"op":"set","path":["defaults","mysql_connection_usage_percent"],` +
		`"value":{"int":"85"}}]}`
	for name, c := range map[string]struct {
		files map[string]string
		fix   string
	}{
		"a duplicate tenant set aside": {map[string]string{"_defaults.yaml": root,
			"ta.yaml":  "tenants:\n  ta:\n    mysql_connection_usage_percent: 70\n",
			"tb1.yaml": "tenants:\n  tb:\n    mysql_connection_usage_percent: 70\n",
			"tb2.yaml": "tenants:\n  tb:\n    mysql_connection_usage_percent: 71\n"},
			`{"path":"tb2.yaml","delete":true}`},
	} {
		dir := writeTree(t, c.files)
		code, res := runBatch(t, writeCmd, `{"files":[`+c.fix+`,`+setRoot+`]}`, "--config-dir", dir, "--dry-run")
		if code != exitOK || len(worsenedOf(res)) != 0 {
			t.Errorf("%s: exit %d worsened %v; the tree had `_metadata` before the write", name, code, worsenedOf(res))
		}
	}
	// No tenant, a new `_bogus` under the root `defaults:`: the write's.
	dir := writeTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n"})
	in := `{"files":[{"path":"_defaults.yaml","ops":[{"op":"set","path":["defaults","_bogus"],"value":{"int":"1"}}]}]}`
	if code, res := runBatch(t, writeCmd, in, "--config-dir", dir, "--dry-run"); code != exitFindings ||
		!reflect.DeepEqual(worsenedOf(res), []string{"unread _defaults.yaml "}) {
		t.Errorf("a new root `_bogus` with no tenant: exit %d worsened %v", code, worsenedOf(res))
	}
}
