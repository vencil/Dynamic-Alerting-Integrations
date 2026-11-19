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
			"error unread _bogus _defaults.yaml unregistered [ta]",
			"error unread _metadata _defaults.yaml merge_dropped [ta]",
		}},
	} {
		dir := writeReservedConfD(t, c.files)
		var out, errOut bytes.Buffer
		if code := run([]string{keyVerdictCmd, "--config-dir", dir}, &out, &errOut); code != exitOK {
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
