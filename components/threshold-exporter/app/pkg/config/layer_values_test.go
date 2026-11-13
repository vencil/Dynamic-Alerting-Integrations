package config

// layer_values_test.go (#3591) — the reserved values the exporter does not
// read are judged in the file and layer that writes them, never on a merged
// value; and the two new value checks (`metadata`, `severity_dedup`) read a
// value as the exporter does.

import (
	"encoding/json"
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func loadForValues(t *testing.T, files map[string]string) LoadReport {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	quiet := log.New(io.Discard, "", 0)
	scan, err := ScanDirTreeWith(dir, nil, nil, quiet, ScanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	_, rep, _, err := LoadDirFromScan(scan, dir, quiet)
	if err != nil {
		t.Fatal(err)
	}
	return rep
}

type layerValueRow struct{ File, Layer, Owner, Key string }

func layerValueRows(ps []LayerValueProblem) []layerValueRow {
	out := []layerValueRow{}
	for _, p := range ps {
		if p.Problem == "" {
			continue
		}
		out = append(out, layerValueRow{p.File, p.Layer, p.Owner, p.Key})
	}
	return out
}

// A profile's and a platform entry's unread value is ONE problem, on the
// file and layer that writes it — never on each tenant that inherits it, as
// a check of the merged tenant maps (ValidateTenantKeysByTenant) charged it.
func TestLayerValueProblems_JudgedWhereWritten(t *testing.T) {
	t.Parallel()
	rep := loadForValues(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\nprofiles:\n  gold:\n" +
			"    _silent_mode: purple\n    _metadata: {owner: p, bogus_profile: x}\n" +
			"    _severity_dedup: maybe\n", // not read in a profile: the verdict's unread, no value problem
		"_platform.yaml": "tenants:\n  ta:\n    _silent_mode: orange\n  tb:\n    _severity_dedup: maybe\n",
		"ta.yaml":        "tenants:\n  ta:\n    _profile: gold\n    _metadata: {bogus_tenant: z}\n",
		"tb.yaml":        "tenants:\n  tb:\n    _profile: silver\n    mysql_connections: 5\n",
		"tc.yaml":        "tenants:\n  tc:\n    _profile: gold\n    _silent_mode: warning\n",
	})
	got := layerValueRows(rep.ValueProblems)
	want := []layerValueRow{
		{"_defaults.yaml", ValueLayerProfile, "gold", "_metadata"},
		{"_defaults.yaml", ValueLayerProfile, "gold", "_silent_mode"},
		{"_platform.yaml", ValueLayerPlatform, "ta", "_silent_mode"},
		{"_platform.yaml", ValueLayerPlatform, "tb", "_severity_dedup"},
		{"ta.yaml", ValueLayerTenant, "ta", "_metadata"},
		{"tb.yaml", ValueLayerTenant, "tb", "_profile"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ValueProblems =\n%v\nwant\n%v", got, want)
	}
	for _, p := range rep.ValueProblems {
		if p.Key == "_profile" && p.Problem != `references a profile that is not defined under the root profiles:` {
			t.Errorf("_profile problem = %q", p.Problem)
		}
	}
}

// The exporter's merged view does carry the profile's and the platform's
// values into the tenants (the reason LayerValueProblems does not read it):
// pinned, so the test above keeps measuring the case that matters.
func TestLayerValueProblems_MergedViewWouldCharge(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\nprofiles:\n  gold:\n    _silent_mode: purple\n",
		"tc.yaml":        "tenants:\n  tc:\n    _profile: gold\n",
	}
	for rel, body := range files {
		if err := os.WriteFile(filepath.Join(dir, rel), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	quiet := log.New(io.Discard, "", 0)
	scan, err := ScanDirTreeWith(dir, nil, nil, quiet, ScanOptions{})
	if err != nil {
		t.Fatal(err)
	}
	cfg, rep, _, err := LoadDirFromScan(scan, dir, quiet)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(cfg.ValidateTenantKeysByTenant().Tenants["tc"].Values); n != 1 {
		t.Fatalf("merged view: %d value findings on tc, want 1 (the profile's, charged to tc)", n)
	}
	for _, p := range rep.ValueProblems {
		if p.Layer != ValueLayerProfile {
			t.Errorf("%+v: the profile's value charged to another layer", p)
		}
	}
}

func TestMetadataCheck(t *testing.T) {
	t.Parallel()
	cases := []struct {
		yaml    string
		problem bool
	}{
		{"_metadata: {owner: a, tier: t1, tags: [x]}", false},
		{"_metadata: {environment: 123}", false},        // a non-string scalar is read as its text
		{"_metadata: \"owner: a\\n\"", true},            // #3638: a string holding YAML is no mapping, not read
		{"_metadata: null", false},                      // no metadata
		{"_metadata: {owner: a, bogus_field: x}", true}, // a field TenantMetadata does not know
		{"_metadata: {owner: [a, b]}", true},            // does not decode: every field empty
		{"_metadata: {dbType: postgresql}", true},       // #880's typo
	}
	for _, c := range cases {
		var m map[string]ScheduledValue
		if err := yamlUnmarshalForTest(c.yaml, &m); err != nil {
			t.Fatalf("%s: %v", c.yaml, err)
		}
		got := CheckReservedValue("_metadata", m["_metadata"])
		if (got != "") != c.problem {
			t.Errorf("%s: problem %q, want problem=%v", c.yaml, got, c.problem)
		}
	}
	// Two values with different unread parts are not read alike (a write
	// replacing one with the other is no ratchet).
	var a, b map[string]ScheduledValue
	_ = yamlUnmarshalForTest("_metadata: {owner: a, x: 1}", &a)
	_ = yamlUnmarshalForTest("_metadata: {owner: a, y: 1}", &b)
	if readAlike("_metadata", a["_metadata"], b["_metadata"]) {
		t.Error("two different unread `_metadata` values read alike")
	}
	_ = yamlUnmarshalForTest("_metadata: {owner: a, tier: t}", &a)
	_ = yamlUnmarshalForTest("_metadata: {tier: t, owner: a}", &b)
	if !readAlike("_metadata", a["_metadata"], b["_metadata"]) {
		t.Error("the same metadata in another key order does not read alike")
	}
}

func TestSeverityDedupCheck(t *testing.T) {
	t.Parallel()
	for _, w := range []string{"enable", "Enabled", " ON ", "true", "disable", "disabled", "off", "False"} {
		if p := CheckReservedValue("_severity_dedup", ScheduledValue{Default: w}); p != "" {
			t.Errorf("%q: %s", w, p)
		}
	}
	for _, w := range []string{"maybe", "no", "yes", "", "0"} {
		if p := CheckReservedValue("_severity_dedup", ScheduledValue{Default: w}); p == "" {
			t.Errorf("%q: no problem, but the resolver does not read it", w)
		}
	}
	if p := CheckReservedValue("_severity_dedup", ScheduledValue{deleted: true}); p != "" {
		t.Errorf("null: %s (the resolver reads it as no key)", p)
	}
	// Every value the check refuses is one the resolver serves as enable
	// with a WARN, and every one it passes the resolver reads without one.
	for _, w := range []string{"enable", "off", "maybe", ""} {
		var warned bool
		c := ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"t": {"_severity_dedup": {Default: w}}}}
		c.ResolveSeverityDedupLogf(func(string, ...any) { warned = true })
		if refused := CheckReservedValue("_severity_dedup", ScheduledValue{Default: w}) != ""; refused != warned {
			t.Errorf("%q: refused=%v, resolver warned=%v", w, refused, warned)
		}
	}
	if readAlike("_severity_dedup", ScheduledValue{Default: "maybe"}, ScheduledValue{Default: "enable"}) {
		t.Error("an unread value reads alike a valid one")
	}
	if !readAlike("_severity_dedup", ScheduledValue{Default: "on"}, ScheduledValue{Default: "enable"}) {
		t.Error("two words of one mode do not read alike")
	}
}

// The schema's `definitions.metadata` is the reader's: its properties are
// exactly TenantMetadata's yaml tags, and it refuses any other field — in
// both directions, so a field added to one side alone fails here.
func TestMetadataSchemaPropertiesMatchReader(t *testing.T) {
	t.Parallel()
	raw, path := tenantSchemaRaw(t)
	var schema struct {
		Definitions struct {
			Metadata struct {
				Properties           map[string]any `json:"properties"`
				AdditionalProperties *bool          `json:"additionalProperties"`
			} `json:"metadata"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	md := schema.Definitions.Metadata
	var props []string
	for k := range md.Properties {
		props = append(props, k)
	}
	sort.Strings(props)
	if !reflect.DeepEqual(props, TenantMetadataFields()) {
		t.Errorf("%s definitions.metadata.properties = %v, TenantMetadata reads %v", path, props, TenantMetadataFields())
	}
	if md.AdditionalProperties == nil || *md.AdditionalProperties {
		t.Errorf("%s definitions.metadata must refuse other fields (additionalProperties: false)", path)
	}
}

// yamlUnmarshalForTest decodes s as the exporter decodes a tenant map
// (ScheduledValue.UnmarshalYAML).
func yamlUnmarshalForTest(s string, out *map[string]ScheduledValue) error {
	return yaml.Unmarshal([]byte(s), out)
}

// #3703: LayerValueProblem.Problem is the key verdict's `reason`, which the
// CI readers print as it is — so every sentence a value check gives is built
// from constants (an index or a count at most), never from the value, a
// field name the author wrote, or a parser's error. Every check the registry
// binds (a key's or a prefix's) and `_profile` is fed a canary in each place
// a value can hold one; a check that is added without a shape here that it
// refuses fails the "judged" half.
func TestLayerValueProblems_CarryNoAuthorText(t *testing.T) {
	t.Parallel()
	// Mixed case on purpose: `_silent_mode` and `_severity_dedup` lower-case
	// what they read, so the comparison is case-insensitive.
	const canary = "https://u:CaNaRyPw@hooks.example/CaNaRy"
	const q = `"` + canary + `"`
	shapes := []string{
		q,
		"[" + q + "]",
		"{" + q + ": x}",
		"{owner: [" + q + "]}",
		"{owner: {" + q + ": 1}}",
		"{target: " + q + "}",
		"{target: [" + q + "]}",
		"{target: all, expires: " + q + "}",
		"{target: enable, expires: " + q + "}",
		"{target: enable, expires: [" + q + "]}",
		"{target: enable, enabled: " + q + "}",
		"{target: " + q + ", enabled: true}",
		"{target: enable, reason: [" + q + "]}",
		"{recurring: " + q + "}",
		"{recurring: [" + q + "]}",
		"{recurring: [{" + q + ": 1}]}",
		"{recurring: [{weekdays: [" + q + "], window: \"02:00-06:00\"}]}",
		"{recurring: [{weekdays: [sun], window: " + q + "}]}",
		"{recurring: [{weekdays: [sun], window: \"02:00-06:00\", reason: [" + q + "]}]}",
	}
	// The keys: every key and prefix the registry gives a check (the prefix
	// as its structured filter and as any other), and `_profile`.
	keys := map[string]string{"_profile": "_profile"} // key → the check (or `_profile`) it proves
	for k, name := range reservedValueChecks {
		keys[k] = name
	}
	for p, name := range reservedPrefixValueChecks {
		keys[p+maintenanceFilterName] = name
		keys[p+"anyfilter"] = name
	}
	judged := map[string]bool{}
	for key, check := range keys {
		for _, shape := range shapes {
			var m map[string]ScheduledValue
			if err := yamlUnmarshalForTest(key+": "+shape, &m); err != nil {
				t.Fatalf("%s: %s: %v", key, shape, err)
			}
			values := map[string]ScheduledValue{key: m[key]}
			p := layerValueProblem(ValueLayerTenant, key, m[key], values, nil)
			if p == "" {
				continue
			}
			judged[check] = true
			if strings.Contains(strings.ToLower(p), strings.ToLower("CaNaRy")) {
				t.Errorf("%s: %s: the problem carries the author's text: %q", key, shape, p)
			}
		}
	}
	for name := range valueChecks {
		if !judged[name] {
			t.Errorf("check %q refused none of the shapes: it is not measured here", name)
		}
	}
	if !judged["_profile"] {
		t.Error("`_profile` naming no profile gave no problem")
	}
}
