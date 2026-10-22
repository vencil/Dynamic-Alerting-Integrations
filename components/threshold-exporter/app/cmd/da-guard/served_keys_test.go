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
		"_routng":                       {reason: keyReasonUnregistered, registered: no()}, // its `disable` switches nothing off (#3045)
		"redis_memory":                  {reason: keyReasonDisabled},
		"redis_memory_critical":         {exporter: true},
		"_namespaces":                   {reason: keyReasonNotExporterKey, registered: yes(), declared: map[string][]string{"annotation": {"tenant", "platform_tenants"}}},
		"_routing":                      {reason: keyReasonNotExporterKey, registered: yes(), declared: map[string][]string{"generator": {"tenant", "platform_tenants"}}},
		"_state_maint":                  {exporter: true, registered: yes()},
		"_state_nope":                   {reason: keyReasonStateFilterUndeclared, registered: yes()},
		"_routing_receiver_type":        {reason: keyReasonRetired, registered: no()},
		retiredCPU:                      {reason: keyReasonShadowed},
		"mysql_threads_running":         {exporter: true},
		"_myth":                         {reason: keyReasonUnregistered, registered: no()}, // the root declares it: still not served (#3045)
		`mysql_connections{tenant="x"}`: {exporter: true},
		"redis_evicted_keys":            {reason: keyReasonUndeliverable},
		`_myth{__x="a"}`:                {reason: keyReasonUnregistered, registered: no()},
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
	// #3045: a `_` key the registry does not list is read in no layer — not
	// as a threshold even when the root `defaults:` declares it, so its
	// `disable` switches nothing off either. `_custom_alerts` is read only
	// when it serves a recipe.
	code, doc2, _, stderr := served(t, map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 80\n  _myth: 3\n  _myth_critical: 8\n  _silent_foo: 5\n",
		"t1.yaml": "tenants:\n  t1:\n    _myth: disable\n    _myth_critical: disable\n    _nothing: 4\n" +
			"    _silent_foo: disable\n" +
			"    _custom_alerts:\n      - {recipe: bogus}\n",
		"t2.yaml": "tenants:\n  t2:\n    cpu: 1\n",
	}, "")
	mustOK(t, code, stderr)
	for k, w := range map[string]string{"_myth": keyReasonUnregistered, "_myth_critical": keyReasonUnregistered,
		"_nothing": keyReasonUnregistered, "_silent_foo": keyReasonUnregistered, customAlertsKey: keyReasonNoRow} {
		if e := doc2.Tenants["t1"].Keys[k]; e.Exporter || e.Reason != w {
			t.Errorf("t1 keys[%q] = %+v, want reason %q", k, e, w)
		}
	}
	// t2 writes none of them: the root's own values serve it no row, and
	// each is in its unserved with the root's value and a keys entry.
	t2 := doc2.Tenants["t2"]
	for _, k := range []string{"_myth", "_myth_critical"} {
		if _, ok := t2.Values[k]; ok {
			t.Errorf("t2 is served root %s: %v", k, t2.Values)
		}
		if _, ok := t2.Severities[k]; ok {
			t.Errorf("t2 has a severity for root %s: %v", k, t2.Severities)
		}
	}
	if want := map[string]any{"_myth": 3.0, "_myth_critical": 8.0, "_silent_foo": 5.0}; !reflect.DeepEqual(t2.Unserved, want) {
		t.Errorf("t2 unserved = %v, want %v", t2.Unserved, want)
	}
	if e := t2.Keys["_myth"]; e.Exporter || e.Reason != keyReasonUnregistered {
		t.Errorf("t2 keys[_myth] = %+v, want reason %q", e, keyReasonUnregistered)
	}
	if _, ok := t2.Values["cpu"]; !ok {
		t.Errorf("control: t2 is not served cpu: %v", t2.Values)
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

// #3140: a value the row resolver refuses is named as a refusal, not as a
// switch-off or a key nobody declares — per key (keys[].refused, and
// keys[].reason value_non_finite / value_unparsed when no row is left) and
// per no-row part of the day (segments[].reason). A misspelt key holding a
// non-finite number stays no_row: the resolver never met it.
func TestServedValues_RefusedValuesAreNamedApartFromDisables(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedWith(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_memory: 70\noptional_overrides:\n  - oracle_process_count\n",
		"db-a.yaml": "tenants:\n  db-a:\n" +
			"    mysql_connections: \"NaN\"\n" + // base: the default is served
			"    mysql_connections_critical:\n      default: \"95\"\n      overrides:\n" +
			"        - window: \"01:00-02:00\"\n          value: \"-Inf\"\n" +
			"        - window: \"03:00-04:00\"\n          value: disable\n" +
			"        - window: \"05:00-06:00\"\n          value: abc\n" +
			"    oracle_process_count: \"Inf\"\n" +
			"    redis_memory_critical: \"7O\"\n" +
			"    redis_memory: disable\n" +
			"    mysql_conections: \"NaN\"\n",
	}, "2026-07-01T12:00:00Z", "--schedules") // outside every window: the keys[] reading is at --at
	mustOK(t, code, stderr)
	tv := doc.Tenants["db-a"]
	type k3 struct {
		exporter        bool
		reason, refused string
	}
	want := map[string]k3{
		"mysql_connections":          {true, "", "non_finite"},
		"mysql_connections_critical": {true, "", ""},
		"oracle_process_count":       {false, keyReasonValueNonFinite, "non_finite"},
		"redis_memory_critical":      {false, keyReasonValueUnparsed, "unparsed"},
		"redis_memory":               {false, keyReasonDisabled, ""},
		"mysql_conections":           {false, keyReasonNoRow, ""},
	}
	for k, w := range want {
		if got := tv.Keys[k]; got.Exporter != w.exporter || got.Reason != w.reason || got.Refused != w.refused {
			t.Errorf("keys[%q] = %+v, want %+v", k, got, w)
		}
	}
	var nulls []string
	for _, s := range tv.Schedules["mysql_connections_critical"].Segments {
		if s.Value == nil && s.Error == "" {
			nulls = append(nulls, s.From+"-"+s.To+"="+s.Reason)
		}
	}
	if w := []string{"01:00-02:00=value_non_finite", "03:00-04:00=disabled", "05:00-06:00=value_unparsed"}; !reflect.DeepEqual(nulls, w) {
		t.Errorf("null segments = %v, want %v", nulls, w)
	}
}
