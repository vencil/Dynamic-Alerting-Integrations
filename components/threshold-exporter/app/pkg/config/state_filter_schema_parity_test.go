package config

// state_filter_schema_parity_test.go (#3269) — the words
// docs/schemas/tenant-config.schema.json takes for a `_state_<filter>` key
// are the state-filter reader's (resolveStateFiltersAt): `enable`, and the
// words that switch a key off (disabledWords). Every `_state_` key but
// `_state_maintenance` reaches that definition through one patternProperty
// (before #3269 it fell to additionalProperties, which took the scheduled
// form), and `_state_maintenance`'s scalar branch is the same definition.

import (
	"encoding/json"
	"fmt"
	"slices"
	"sort"
	"testing"
	"time"
)

const stateFilterRef = "#/definitions/stateFilterScalar"

func TestStateFilterSchemaMatchesReader(t *testing.T) {
	t.Parallel()
	raw, path := tenantSchemaRaw(t)
	var schema struct {
		Definitions struct {
			StateFilterScalar struct {
				Type string   `json:"type"`
				Enum []string `json:"enum"`
			} `json:"stateFilterScalar"`
			MaintenanceMode struct {
				OneOf []map[string]any `json:"oneOf"`
			} `json:"maintenanceMode"`
			TenantConfig struct {
				PatternProperties map[string]map[string]any `json:"patternProperties"`
			} `json:"tenantConfig"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	def := schema.Definitions

	// Wired: the patternProperty for every other `_state_` key, and
	// `_state_maintenance`'s scalar branch.
	var stateRefs []string
	for pat, sub := range def.TenantConfig.PatternProperties {
		if sub["$ref"] == stateFilterRef {
			stateRefs = append(stateRefs, pat)
		}
	}
	if !slices.Equal(stateRefs, []string{"^_state_(?!maintenance$)"}) {
		t.Errorf("tenantConfig.patternProperties referring to %s = %q, want the one `_state_` pattern", stateFilterRef, stateRefs)
	}
	scalarRefs := 0
	for _, b := range def.MaintenanceMode.OneOf {
		if b["type"] == "string" {
			t.Errorf("maintenanceMode has a string branch of its own (%v); it must be %s", b, stateFilterRef)
		}
		if b["$ref"] == stateFilterRef {
			scalarRefs++
		}
	}
	if scalarRefs != 1 {
		t.Errorf("maintenanceMode.oneOf refers to %s %d times, want once", stateFilterRef, scalarRefs)
	}

	// The words, both ways.
	got := append([]string{}, def.StateFilterScalar.Enum...)
	sort.Strings(got)
	want := append([]string{"enable"}, disabledWords(t)...)
	sort.Strings(want)
	if def.StateFilterScalar.Type != "string" || !slices.Equal(got, want) {
		t.Errorf("definitions.stateFilterScalar = type %q enum %q, want string %q (enable + disabledWords)",
			def.StateFilterScalar.Type, got, want)
	}

	// And the reader reads each as the schema says: `enable` turns a filter
	// on, every other word turns it off.
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	for _, w := range def.StateFilterScalar.Enum {
		c := ThresholdConfig{
			StateFilters: map[string]StateFilter{"crashloop": {Severity: "critical", DefaultState: "enable"}},
			Tenants:      map[string]map[string]ScheduledValue{"t1": {"_state_crashloop": {Default: w}}},
		}
		on := len(c.resolveStateFiltersAt(now, nil)) == 1
		if on != (w == "enable") {
			t.Errorf("_state_crashloop: %q reads as on=%v, want %v", w, on, w == "enable")
		}
	}
}

// #3620: the structured branch of definitions.maintenanceMode is the
// reader's structured form (stateValueOf), in both directions: the same
// fields, the same switch words, no default switch, the fields that make an
// object say something (anyOf), and the target / enabled pairs that
// disagree (not).
func TestMaintenanceSchemaMatchesReader(t *testing.T) {
	t.Parallel()
	raw, path := tenantSchemaRaw(t)
	type prop struct {
		Type     string   `json:"type"`
		Enum     []any    `json:"enum"`
		Default  any      `json:"default"`
		Items    any      `json:"items"`
		MinItems *int     `json:"minItems"`
		Req      []string `json:"required"`
	}
	type branch struct {
		Type                 string          `json:"type"`
		Properties           map[string]prop `json:"properties"`
		AdditionalProperties *bool           `json:"additionalProperties"`
		AnyOf                []prop          `json:"anyOf"`
		Not                  *struct {
			AnyOf []struct {
				Required   []string        `json:"required"`
				Properties map[string]prop `json:"properties"`
			} `json:"anyOf"`
		} `json:"not"`
	}
	var schema struct {
		Definitions struct {
			MaintenanceMode struct {
				OneOf []json.RawMessage `json:"oneOf"`
			} `json:"maintenanceMode"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	var obj *branch
	for _, rb := range schema.Definitions.MaintenanceMode.OneOf {
		var b branch
		if err := json.Unmarshal(rb, &b); err != nil {
			t.Fatal(err)
		}
		if b.Type == "object" {
			if obj != nil {
				t.Fatal("maintenanceMode has more than one object branch")
			}
			obj = &b
		}
	}
	if obj == nil {
		t.Fatal("maintenanceMode has no object branch")
	}

	// The fields, both ways, and nothing else.
	var fields []string
	for k := range obj.Properties {
		fields = append(fields, k)
	}
	sort.Strings(fields)
	if !slices.Equal(fields, stateValueFields) {
		t.Errorf("structured _state_maintenance fields: schema %q, reader %q", fields, stateValueFields)
	}
	if obj.AdditionalProperties == nil || *obj.AdditionalProperties {
		t.Error("the structured branch must set additionalProperties: false (the reader refuses unknown fields)")
	}
	r := func(v string) stateValueReading {
		return stateValueOf(maintenanceFilterName, ScheduledValue{Default: v})
	}
	if got := r("bogus: 1\n"); got.kind != stateValueProblem {
		t.Errorf("an unknown field reads as %d, want a problem", got.kind)
	}

	// The switch: target's words are the scalar's, with no default; enabled
	// is a boolean.
	target := obj.Properties["target"]
	if target.Default != nil {
		t.Errorf("target has a default %v: an omitted switch is no switch (#3620)", target.Default)
	}
	var words []string
	for _, w := range target.Enum {
		words = append(words, fmt.Sprint(w))
	}
	gotWords := slices.Clone(words)
	sort.Strings(gotWords)
	wantWords := append([]string{"enable"}, disabledWords(t)...)
	sort.Strings(wantWords)
	if target.Type != "string" || !slices.Equal(gotWords, wantWords) {
		t.Errorf("target = type %q enum %q, want string %q", target.Type, gotWords, wantWords)
	}
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	for _, w := range words {
		got := r(fmt.Sprintf("target: %q\n", w))
		if got.kind == stateValueProblem || got.onAt(now) != (w == "enable") {
			t.Errorf("target %q reads as kind %d on=%v, want on=%v", w, got.kind, got.onAt(now), w == "enable")
		}
	}
	if got := r("target: all\n"); got.kind != stateValueProblem {
		t.Error("a target outside the enum is read")
	}
	if obj.Properties["enabled"].Type != "boolean" {
		t.Errorf("enabled type = %q, want boolean", obj.Properties["enabled"].Type)
	}

	// anyOf: the fields that make an object say something — each alone is
	// read; reason alone is not.
	var says []string
	for _, a := range obj.AnyOf {
		if len(a.Req) != 1 {
			t.Fatalf("anyOf entry %+v: want one required field", a)
		}
		says = append(says, a.Req[0])
	}
	sort.Strings(says)
	if want := []string{"enabled", "expires", "recurring", "target"}; !slices.Equal(says, want) {
		t.Errorf("anyOf required = %q, want %q", says, want)
	}
	alone := map[string]string{
		"target": "target: enable\n", "enabled": "enabled: true\n",
		"expires": "expires: \"2099-01-01T00:00:00Z\"\n", "recurring": "recurring:\n- cron: \"0 2 * * *\"\n  duration: 1h\n",
	}
	for _, f := range says {
		if got := r(alone[f]); got.kind == stateValueProblem {
			t.Errorf("%s alone reads as a problem (%s)", f, got.problem)
		}
	}
	// An empty recurring says nothing either: the schema needs one entry.
	if mi := obj.Properties["recurring"].MinItems; mi == nil || *mi != 1 {
		t.Errorf("recurring.minItems = %v, want 1", mi)
	}
	if got := r("recurring: []\n"); got.kind != stateValueProblem {
		t.Error("an empty recurring alone is read, but the schema's minItems refuses it")
	}
	if got := r("reason: x\n"); got.kind != stateValueProblem {
		t.Error("reason alone is read, but the schema's anyOf refuses it")
	}
	// No switch: on exactly when expires is given; recurring alone is off.
	if !r(alone["expires"]).onAt(now) || r(alone["recurring"]).onAt(now) {
		t.Error("no switch: expires must turn it on and recurring alone must not")
	}

	// not: the pairs that disagree — each is a problem to the reader; the
	// pairs that agree are not.
	if obj.Not == nil || len(obj.Not.AnyOf) != 2 {
		t.Fatalf("not = %+v, want the two disagreeing target / enabled pairs", obj.Not)
	}
	pairs := 0
	for _, n := range obj.Not.AnyOf {
		if !slices.Equal(n.Required, []string{"target", "enabled"}) || len(n.Properties["enabled"].Enum) != 1 {
			t.Fatalf("not branch %+v", n)
		}
		enabled := n.Properties["enabled"].Enum[0]
		var ts []string
		for _, w := range n.Properties["target"].Enum {
			ts = append(ts, fmt.Sprint(w))
		}
		sort.Strings(ts)
		wantTs := []string{"enable"}
		if enabled == true {
			wantTs = slices.Clone(disabledWords(t))
			sort.Strings(wantTs)
		}
		if !slices.Equal(ts, wantTs) {
			t.Errorf("not branch enabled=%v: targets %q, want %q", enabled, ts, wantTs)
		}
		for _, w := range ts {
			if got := r(fmt.Sprintf("target: %q\nenabled: %v\n", w, enabled)); got.kind != stateValueProblem {
				t.Errorf("target %q with enabled %v is read, but the schema refuses it", w, enabled)
			}
			if got := r(fmt.Sprintf("target: %q\nenabled: %v\n", w, enabled != true)); got.kind == stateValueProblem {
				t.Errorf("target %q with enabled %v (agreeing) is a problem: %s", w, enabled != true, got.problem)
			}
			pairs++
		}
	}
	if pairs != 1+len(disabledWords(t)) {
		t.Errorf("not covers %d target words, want every one", pairs)
	}
}
