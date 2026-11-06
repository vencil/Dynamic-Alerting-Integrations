package main

// effective_maintenance_recurring_test.go (#3620) — `da-guard effective`'s
// maintenance_recurring: the windows maintenance-scheduler silences, by the
// exporter's one reading of `_state_maintenance`. A value whose switch says
// off, or that the reader does not read, has none; always present.

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestEffective_MaintenanceRecurring(t *testing.T) {
	t.Parallel()
	rec := "      recurring:\n        - cron: \"0 2 * * 0\"\n          duration: 4h\n          reason: backup\n" +
		"        - cron: \"0 3 * * *\"\n          duration: 3600\n"
	tenant := func(id, head string) string {
		return "tenants:\n  " + id + ":\n    _state_maintenance:\n" + head + rec
	}
	files := map[string]string{
		"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
		"only.yaml":        tenant("only", ""),
		"on.yaml":          tenant("on", "      enabled: true\n"),
		"off-enabled.yaml": tenant("off-enabled", "      enabled: false\n"),
		"off-target.yaml":  tenant("off-target", "      target: disable\n"),
		"problem.yaml":     tenant("problem", "      target: all\n"),
		"bad-expires.yaml": tenant("bad-expires", "      expires: \"2026-07-01\"\n"),
		"scalar.yaml":      "tenants:\n  scalar:\n    _state_maintenance: enable\n",
		"none.yaml":        "tenants:\n  none:\n    mysql_connections: 70\n",
	}
	dir := writeParityTree(t, files)
	code, doc, stderr := runEffectiveOn(t, dir)
	mustOK(t, code, stderr)
	// A duration not written as a YAML string is left out (the scheduler
	// skips the entry with a WARN), not "3600".
	windows := []recurringWindow{{Cron: "0 2 * * 0", Duration: "4h", Reason: "backup"}, {Cron: "0 3 * * *"}}
	want := map[string][]recurringWindow{
		"only": windows, "on": windows,
		"off-enabled": {}, "off-target": {}, "problem": {}, "bad-expires": {}, "scalar": {}, "none": {},
	}
	for id, w := range want {
		var tv map[string]json.RawMessage
		if err := json.Unmarshal(doc.Tenants[id], &tv); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		raw, ok := tv["maintenance_recurring"]
		if !ok {
			t.Errorf("%s: no maintenance_recurring (it is always present)", id)
			continue
		}
		var got []recurringWindow
		if err := json.Unmarshal(raw, &got); err != nil || got == nil {
			t.Errorf("%s: maintenance_recurring = %s (%v), want a list", id, raw, err)
			continue
		}
		if !reflect.DeepEqual(got, w) {
			t.Errorf("%s: maintenance_recurring = %+v, want %+v", id, got, w)
		}
	}
}
