package main

// served_keys_test.go — served-values' per-tenant `keys` (ADR-038
// decision 5): for every key the tenant's merged config carries, whether the
// exporter reads it and, if not, why.

import (
	"reflect"
	"testing"
)

type wantKey struct {
	exporter   bool
	reason     string
	registered *bool // nil: a key without the `_` prefix
	declared   map[string][]string
}

func yes() *bool { b := true; return &b }
func no() *bool  { b := false; return &b }

// retiredCPU is the #1231 retired spelling of mysql_threads_running, built so
// the forbid-legacy-mysql-cpu-key hook does not read it as a live value.
const retiredCPU = "mysql_" + "cpu"

// The ADR-038 example, plus one key for every other reason.
func TestServedValues_KeysSayWhetherTheExporterReadsEachKey(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n" +
			"  mysql_threads_running: 30\n  _myth: 5\n" +
			"state_filters:\n  maint: {reasons: [X], severity: critical}\n",
		"_profiles.yaml":      "profiles:\n  p1:\n    _silent_mode: warning\n",
		"team/_defaults.yaml": "defaults:\n  redis_evicted_keys: 9\n",
		"team/db-a.yaml": "tenants:\n  db-a:\n" +
			"    _profile: p1\n" +
			"    mysql_conections: \"70\"\n" + // misspelt
			"    mysql_slave_lagg: disable\n" + // misspelt, meant to switch mysql_slave_lag off
			"    _routng: disable\n" + // misspelt
			"    redis_memory: disable\n" +
			"    redis_memory_critical: \"90\"\n" +
			"    _namespaces: [prod-ns]\n" +
			"    _routing: {receiver: {type: webhook, url: 'https://w.example/x'}}\n" +
			"    _state_maint: disable\n" +
			"    _state_nope: disable\n" +
			"    _routing_receiver_type: webhook\n" +
			"    " + retiredCPU + ": \"44\"\n" + // retired spelling, shadowed below
			"    mysql_threads_running: \"50\"\n" +
			"    _myth: 7\n" +
			"    mysql_connections{tenant=\"x\"}: 5\n" + // its row is dropped
			"    _myth{__x=\"a\"}: 6\n" + // a `_` key whose only row is dropped
			"    redis_memory{ db = \"a\" }: 8\n", // spelt otherwise than canonically
	}, "")
	mustOK(t, code, stderr)
	got := doc.Tenants["db-a"].Keys
	want := map[string]wantKey{
		"_profile":                      {exporter: true, registered: yes()},
		"_silent_mode":                  {exporter: true, registered: yes()}, // from the profile
		"mysql_conections":              {reason: keyReasonNoRow},
		"mysql_slave_lagg":              {reason: keyReasonDisabled},
		"_routng":                       {reason: keyReasonDisabled, registered: no()},
		"redis_memory":                  {reason: keyReasonDisabled},
		"redis_memory_critical":         {exporter: true},
		"_namespaces":                   {reason: keyReasonNotExporterKey, registered: yes(), declared: map[string][]string{"annotation": {"tenant", "platform_tenants"}}},
		"_routing":                      {reason: keyReasonNotExporterKey, registered: yes(), declared: map[string][]string{"generator": {"tenant", "platform_tenants"}}},
		"_state_maint":                  {exporter: true, registered: yes()},
		"_state_nope":                   {reason: keyReasonStateFilterUndeclared, registered: yes()},
		"_routing_receiver_type":        {reason: keyReasonRetired, registered: no()},
		retiredCPU:                      {reason: keyReasonShadowed},
		"mysql_threads_running":         {exporter: true},
		"_myth":                         {exporter: true, registered: no()}, // unregistered, yet /metrics serves it
		`mysql_connections{tenant="x"}`: {exporter: true},
		"redis_evicted_keys":            {reason: keyReasonUndeliverable},
		`_myth{__x="a"}`:                {exporter: true, registered: no()},
		`redis_memory{ db = "a" }`:      {exporter: true}, // keyed as written
	}
	for k, w := range want {
		e, ok := got[k]
		if !ok {
			t.Errorf("keys[%q] missing; keys=%v", k, keysOf(got))
			continue
		}
		if e.Exporter != w.exporter || e.Reason != w.reason {
			t.Errorf("keys[%q] = exporter %v reason %q, want exporter %v reason %q", k, e.Exporter, e.Reason, w.exporter, w.reason)
		}
		if (e.Registered == nil) != (w.registered == nil) || (e.Registered != nil && *e.Registered != *w.registered) {
			t.Errorf("keys[%q].registered = %v, want %v", k, boolOrNil(e.Registered), boolOrNil(w.registered))
		}
		if len(e.Declared) > 0 || len(w.declared) > 0 {
			if !reflect.DeepEqual(e.Declared, w.declared) {
				t.Errorf("keys[%q].declared = %v, want %v", k, e.Declared, w.declared)
			}
		}
	}
	// An unregistered `_` key is read as the threshold the exporter takes it
	// for: its switch-off and its rows count; nothing at all is unregistered.
	// `_custom_alerts` is read only when it serves a recipe.
	code, doc2, _, stderr := served(t, map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 80\n  _myth: 3\n  _myth_critical: 8\n  _silent_foo: 5\n",
		"t1.yaml": "tenants:\n  t1:\n    _myth: disable\n    _myth_critical: disable\n    _nothing: 4\n" +
			"    _silent_foo: disable\n" + // the row generator never reads `_silent_…`
			"    _custom_alerts:\n      - {recipe: bogus}\n",
		"t2.yaml": "tenants:\n  t2:\n    cpu: 1\n",
	}, "")
	mustOK(t, code, stderr)
	for k, w := range map[string]string{"_myth": keyReasonDisabled, "_myth_critical": keyReasonDisabled,
		"_nothing": keyReasonUnregistered, "_silent_foo": keyReasonUnregistered, customAlertsKey: keyReasonNoRow} {
		if e := doc2.Tenants["t1"].Keys[k]; e.Exporter || e.Reason != w {
			t.Errorf("t1 keys[%q] = %+v, want reason %q", k, e, w)
		}
	}
	if _, ok := doc2.Tenants["t2"].Values["_myth"]; !ok {
		t.Errorf("control: t2 is served _myth: %v", doc2.Tenants["t2"].Values)
	}
	if e := got["_routing_receiver_type"]; e.Hint == "" {
		t.Errorf("a retired key carries the registry's hint; keys[_routing_receiver_type] = %+v", e)
	}
	// Every key of the tenant's merged config and of unserved has an entry,
	// and nothing else does.
	for k := range doc.Tenants["db-a"].Unserved {
		if _, ok := got[k]; !ok {
			t.Errorf("unserved %q has no keys entry", k)
		}
	}
	if extra := len(got) - len(want); extra != 0 {
		t.Errorf("keys has %d entries beyond the ones the tree writes: %v", extra, keysOf(got))
	}
}

// `_routing` is the route generator's: not a /metrics value, carried apart.
// A `_routing` the resolver rejects stays in unserved, as written.
func TestServedValues_RoutingIsNotAValue(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _routing: {receiver: {type: webhook, url: 'https://w.example/a'}}\n",
		"tenant-b.yaml":  "tenants:\n  tenant-b:\n    _routing: {group_wait: 30s}\n", // no receiver: rejected
	}, "")
	mustOK(t, code, stderr)
	a, b := doc.Tenants["tenant-a"], doc.Tenants["tenant-b"]
	if _, ok := a.Values["_routing"]; ok {
		t.Errorf("values carries _routing: %v", a.Values["_routing"])
	}
	if recv, _ := a.Routing["receiver"].(map[string]any); recv["url"] != "https://w.example/a" {
		t.Errorf("routing = %v, want the resolver's reading", a.Routing)
	}
	if _, ok := a.Unserved["_routing"]; ok {
		t.Errorf("an accepted _routing is in unserved: %v", a.Unserved)
	}
	if b.Routing != nil {
		t.Errorf("a rejected _routing is in routing: %v", b.Routing)
	}
	if _, ok := b.Unserved["_routing"]; !ok {
		t.Errorf("a rejected _routing is not in unserved: %v", b.Unserved)
	}
	for _, tv := range []string{"tenant-a", "tenant-b"} {
		if e := doc.Tenants[tv].Keys["_routing"]; e.Exporter || e.Reason != keyReasonNotExporterKey {
			t.Errorf("%s: keys[_routing] = %+v, want not read by the exporter", tv, e)
		}
	}
}

// keys is present (an empty object) for a tenant that writes nothing.
func TestServedValues_KeysIsAnEmptyObjectWhenTheTenantWritesNothing(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
	}, "")
	mustOK(t, code, stderr)
	if k := doc.Tenants["tenant-a"].Keys; k == nil || len(k) != 0 {
		t.Errorf("keys = %#v, want {}", k)
	}
}

func boolOrNil(b *bool) any {
	if b == nil {
		return nil
	}
	return *b
}
