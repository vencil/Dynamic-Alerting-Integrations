package config

// #3638 stage 2: ScheduledValue.structured is the structure's kind — set
// wherever a mapping or sequence is put into Default as text, kept by every
// copy that keeps the text, and never set for a scalar. Each test below
// turns red when one construction site stops setting or keeping it.

import (
	"fmt"
	"strconv"
	"strings"
	"testing"
	"unsafe"

	"gopkg.in/yaml.v3"
)

func decodedValue(t *testing.T, text string) ScheduledValue {
	t.Helper()
	var sv ScheduledValue
	if err := yaml.Unmarshal([]byte(text), &sv); err != nil {
		t.Fatalf("decode %q: %v", text, err)
	}
	return sv
}

func TestStructured_SetForAWrittenMappingOrSequenceOnly(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		text string
		want bool
	}{
		{"warning", false},
		{`"target: critical"`, false},               // a scalar that reads like a mapping
		{"{target: all}", true},                     // the arbitrary-mapping branch
		{"[a, b]", true},                            // the sequence branch
		{"{default: \"70\", overrides: []}", false}, // the schedule form: Default is its `default`
	} {
		if got := decodedValue(t, c.text).structured; got != c.want {
			t.Errorf("decode %q: structured=%v, want %v", c.text, got, c.want)
		}
	}
}

// A reserved mapping that happens to hold a `default` key is re-read whole
// (keepReservedMappings) — still the mapping it was written as.
func TestStructured_KeptByKeepReservedMappings(t *testing.T) {
	t.Parallel()
	cfg, err := ParseConfigFile([]byte("tenants:\n  t1:\n    _metadata:\n      default: x\n      owner: dba\n"))
	if err != nil {
		t.Fatal(err)
	}
	if sv := cfg.Tenants["t1"]["_metadata"]; !sv.structured {
		t.Errorf("_metadata with a `default` key: structured=false, Default=%q", sv.Default)
	}
}

// Two _metadata mapping layers merge into a mapping.
func TestStructured_KeptByOverlayMetadataValue(t *testing.T) {
	t.Parallel()
	got := overlayMetadataValue(decodedValue(t, "{owner: a}"), decodedValue(t, "{tier: gold}"))
	if !got.structured {
		t.Errorf("overlay of two mappings: structured=false, Default=%q", got.Default)
	}
}

// A reserved key's schedule is dropped to its `default`; the default's kind
// goes with it.
func TestStructured_KeptByServedValue(t *testing.T) {
	t.Parallel()
	sv := decodedValue(t, "{target: all}")
	sv.Overrides = []TimeWindowOverride{{Window: "01:00-02:00", Value: "x"}}
	if got := servedValue("_silent_mode", sv); !got.structured {
		t.Errorf("servedValue dropped structured: %+v", got)
	}
	body := map[string]map[string]ScheduledValue{"t1": {"_silent_mode": sv}}
	stripScheduledReserved("tenants", body)
	if !body["t1"]["_silent_mode"].structured {
		t.Errorf("stripScheduledReserved dropped structured: %+v", body["t1"]["_silent_mode"])
	}
}

// The flag sits in ScheduledValue's padding: the value is held per key per
// tenant and copied on every merge and scrape (bench-gate #871), so a
// layout that grows it is a decision, not an accident.
func TestStructured_ScheduledValueSizeUnchanged(t *testing.T) {
	t.Parallel()
	if strconv.IntSize != 64 {
		t.Skip("the pinned size is the 64-bit layout")
	}
	if got := unsafe.Sizeof(ScheduledValue{}); got != 56 {
		t.Errorf("unsafe.Sizeof(ScheduledValue{}) = %d, want 56", got)
	}
}

// structureOf never reads a scalar's text as YAML.
func TestStructureOf_AScalarIsNoStructure(t *testing.T) {
	t.Parallel()
	if n, structured, err := structureOf(ScheduledValue{Default: "target: all\nexpires: x\n"}); n != nil || structured || err != nil {
		t.Errorf("structureOf(scalar) = %v, %v, %v", n, structured, err)
	}
	n, structured, err := structureOf(decodedValue(t, "{target: all}"))
	if !structured || err != nil || !mappingHasKey(n, "target") {
		t.Errorf("structureOf(mapping) = %v, %v, %v", n, structured, err)
	}
}

// The same text as a block scalar and as a mapping is not the same value.
func TestSameSchedule_ScalarAndMappingOfOneTextDiffer(t *testing.T) {
	t.Parallel()
	scalar := decodedValue(t, "|\n  target: critical\n")
	mapping := decodedValue(t, "{target: critical}")
	if scalar.Default != mapping.Default {
		t.Fatalf("fixture: Defaults differ: %q vs %q", scalar.Default, mapping.Default)
	}
	if sameSchedule(scalar, mapping) {
		t.Error("sameSchedule(block scalar, mapping of the same text) = true")
	}
}

// A mapping or sequence with no top-level `target` is an unknown value
// (WARN), whatever its text holds.
func TestSilentModeOf_StructureWithoutTargetIsUnknown(t *testing.T) {
	t.Parallel()
	for _, text := range []string{`{reason: "target: x"}`, "{note: {target: all}}", "[{target: all}]"} {
		if r := silentModeOf(decodedValue(t, text)); r.kind != silentModeUnknownScalar {
			t.Errorf("silentModeOf(%s): kind=%d, want unknown scalar", text, r.kind)
		}
	}
}

// valueAsWritten is a value as a conf.d file holding text would give it:
// text decoded as YAML (a mapping or sequence comes out structured, #3638),
// or — text that is no YAML, such as "expires: [" — the scalar a file
// holds when it quotes that text.
func valueAsWritten(text string) ScheduledValue {
	var sv ScheduledValue
	if err := yaml.Unmarshal([]byte(text), &sv); err != nil {
		return ScheduledValue{Default: text}
	}
	return sv
}

// A structured value whose text does not parse (not reachable for a value
// UnmarshalYAML built) is a structured problem, never on.
func TestStateValueOf_StructuredTextThatDoesNotParse(t *testing.T) {
	t.Parallel()
	r := stateValueOf(maintenanceFilterName, ScheduledValue{Default: "expires: [\n", structured: true})
	if r.kind != stateValueProblem || !r.structured || !strings.Contains(r.problem, "does not parse as YAML") {
		t.Errorf("stateValueOf(broken structured) = %+v", r)
	}
}

// #3638 4/5: `_custom_alerts` is read from the list it was written as. The
// same recipe text written as a string — a block scalar, which used to be
// decoded as the list it spelled — is refused by both readers: the
// exporter's walk (CustomAlertEntries / the served rows) and the write gate
// (ValidateTenantCustomAlerts). An empty string still declares none.
func TestCustomAlertSpecsOf_StructureNotText(t *testing.T) {
	t.Parallel()
	const recipe = "{recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100:warning\"}"
	parse := func(body string) map[string]ScheduledValue {
		t.Helper()
		cfg, err := ParseConfigFile([]byte("tenants:\n  t1:\n    _custom_alerts:" + body))
		if err != nil {
			t.Fatal(err)
		}
		return cfg.Tenants["t1"]
	}
	list := parse("\n      - " + recipe + "\n")
	// The string holds the list's text exactly: only the kind differs.
	block := " |\n"
	for _, line := range strings.SplitAfter(list["_custom_alerts"].Default, "\n") {
		if line != "" {
			block += "      " + line
		}
	}
	text, empty := parse(block), parse(" \"\"\n")
	if list["_custom_alerts"].Default != text["_custom_alerts"].Default {
		t.Fatalf("fixture: the list and the string must hold one text: %q vs %q",
			list["_custom_alerts"].Default, text["_custom_alerts"].Default)
	}

	if entries, blockErr := CustomAlertEntries("t1", list); len(entries) != 1 || entries[0].Status != CustomAlertServed || blockErr != "" {
		t.Errorf("list: entries %+v, blockErr %q", entries, blockErr)
	}
	if v := ValidateTenantCustomAlerts("t1", list, 20); v != nil {
		t.Errorf("list: write gate %v", v)
	}

	if entries, blockErr := CustomAlertEntries("t1", text); entries != nil || !strings.Contains(blockErr, "written as a string, not a list") {
		t.Errorf("string: entries %+v, blockErr %q", entries, blockErr)
	}
	if v := ValidateTenantCustomAlerts("t1", text, 20); len(v) != 1 || !strings.Contains(v[0], "written as a string, not a list") {
		t.Errorf("string: write gate %v", v)
	}

	if entries, blockErr := CustomAlertEntries("t1", empty); len(entries) != 0 || blockErr != "" {
		t.Errorf("empty string: entries %+v, blockErr %q", entries, blockErr)
	}
	if v := ValidateTenantCustomAlerts("t1", empty, 20); v != nil {
		t.Errorf("empty string: write gate %v", v)
	}
}

// _metadata (#3638): no value is no metadata and no error; a string is an
// error whatever its text reads like; a mapping decodes.
func TestDecodeTenantMetadata_StructureNotText(t *testing.T) {
	t.Parallel()
	if tm, err := decodeTenantMetadata(ScheduledValue{}); err != nil || tm.Owner != "" {
		t.Errorf("zero value: %+v, %v; want empty, nil", tm, err)
	}
	if _, err := decodeTenantMetadata(decodedValue(t, `"owner: dba"`)); err == nil {
		t.Error("a string holding YAML decoded as metadata")
	}
	if tm, err := decodeTenantMetadata(decodedValue(t, "{owner: dba}")); err != nil || tm.Owner != "dba" {
		t.Errorf("mapping: %+v, %v", tm, err)
	}
}

// A reserved mapping whose `default` text holds `:` is still kept whole
// (scheduleForm, #3638; the text sniff it replaced missed it).
func TestStructured_KeepReservedMappingsDefaultWithColon(t *testing.T) {
	t.Parallel()
	cfg, err := ParseConfigFile([]byte("tenants:\n  t1:\n    _metadata: {default: \"environment: dev\", environment: production}\n"))
	if err != nil {
		t.Fatal(err)
	}
	tm, err := decodeTenantMetadata(cfg.Tenants["t1"]["_metadata"])
	if err != nil || tm.Environment != "production" {
		t.Errorf("_metadata with a `default: \"a: b\"` key: %+v, %v; want environment production", tm, err)
	}
}

// The string form is named once per commit, not on every scrape; a mapping
// that does not decode still WARNs where it is read.
func TestTenantMetadataOf_StringFormIsNotWarnedPerRead(t *testing.T) {
	t.Parallel()
	var logged []string
	logf := func(format string, args ...any) { logged = append(logged, fmt.Sprintf(format, args...)) }
	if _, decoded := tenantMetadataOf("t1", map[string]ScheduledValue{"_metadata": decodedValue(t, `"owner: x"`)}, logf); decoded || len(logged) != 0 {
		t.Errorf("string form: decoded=%v logged=%q; want false, nothing", decoded, logged)
	}
	if _, decoded := tenantMetadataOf("t1", map[string]ScheduledValue{"_metadata": decodedValue(t, "{environment: {a: b}}")}, logf); decoded || len(logged) != 1 {
		t.Errorf("undecodable mapping: decoded=%v logged=%q; want false, one WARN", decoded, logged)
	}
}
