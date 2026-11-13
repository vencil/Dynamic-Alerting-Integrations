package main

import (
	"bytes"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// runSD runs `da-guard severity-dedup` over a tree of files and decodes its
// document (nil when stdout is empty).
func runSD(t *testing.T, files map[string]string) (int, *severityDedupDoc, string) {
	t.Helper()
	dir := writeTree(t, files)
	var out, errOut bytes.Buffer
	code := run([]string{severityDedupCmd, "--config-dir", dir}, &out, &errOut)
	if out.Len() == 0 {
		return code, nil, errOut.String()
	}
	var doc severityDedupDoc
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatalf("stdout is not the document: %v\n%s", err, out.String())
	}
	if doc.Schema != severityDedupSchema {
		t.Fatalf("schema %q", doc.Schema)
	}
	return code, &doc, errOut.String()
}

func modesOf(doc *severityDedupDoc) map[string]string {
	out := map[string]string{}
	for t, v := range doc.Tenants {
		out[t] = v.Mode
	}
	return out
}

const sdRoot = "defaults:\n  cpu: 80\n"

// #3621: each spelling is read as the exporter's resolver reads it; a value
// it does not read is served as enable and named in problems, with the
// exporter's own words — one fixed sentence that does not quote the value
// (#3703) — and the value as written, in its own field.
func TestSeverityDedup_Spellings(t *testing.T) {
	t.Parallel()
	cases := []struct {
		yaml, mode string
		unread     bool   // the exporter does not read it: a problem is reported
		value      string // then: Value, the value as the file wrote it (a list as re-serialised)
	}{
		{"enable", "enable", false, ""}, {"disable", "disable", false, ""},
		{"Enabled", "enable", false, ""}, {`"  DISABLED "`, "disable", false, ""},
		{"on", "enable", false, ""}, {"off", "disable", false, ""}, {"'Off'", "disable", false, ""},
		{"true", "enable", false, ""}, {"false", "disable", false, ""}, {"FALSE", "disable", false, ""},
		{"~", "enable", false, ""},
		{`"no"`, "enable", true, "no"}, {"no", "enable", true, "no"}, {"yes", "enable", true, "yes"},
		{`""`, "enable", true, ""}, {"1.00", "enable", true, "1.00"}, {"0x1F", "enable", true, "0x1F"},
		{"maybe", "enable", true, "maybe"}, {"[disable]", "enable", true, "- disable\n"},
	}
	var b strings.Builder
	b.WriteString("tenants:\n")
	for i, c := range cases {
		b.WriteString("  t" + string(rune('a'+i)) + ":\n    _severity_dedup: " + c.yaml + "\n")
	}
	code, doc, errOut := runSD(t, map[string]string{"_defaults.yaml": sdRoot, "x.yaml": b.String()})
	if code != exitOK || doc == nil {
		t.Fatalf("exit %d, stderr %q", code, errOut)
	}
	problems := map[string]severityDedupProblem{}
	for _, p := range doc.Problems {
		problems[p.Owner] = p
	}
	for i, c := range cases {
		tenant := "t" + string(rune('a'+i))
		if got := doc.Tenants[tenant].Mode; got != c.mode {
			t.Errorf("%s: mode %q, want %q", c.yaml, got, c.mode)
		}
		p, has := problems[tenant]
		if has != c.unread {
			t.Errorf("%s: problem %+v (present %v), want present %v", c.yaml, p, has, c.unread)
			continue
		}
		if has {
			if p.File != "x.yaml" || p.Layer != "tenant" ||
				p.Problem != "unknown severity_dedup value (valid: enable, disable)" {
				t.Errorf("%s: problem %+v", c.yaml, p)
			}
			// #3703: the sentence no longer quotes the value, so what was
			// written is pinned on the field that carries it.
			if p.Value != c.value {
				t.Errorf("%s: value %q, want %q", c.yaml, p.Value, c.value)
			}
		}
	}
	if len(doc.Schedules) != 0 || len(doc.ParseFailed) != 0 || len(doc.Unreadable) != 0 {
		t.Errorf("schedules %+v parse_failed %v unreadable %v", doc.Schedules, doc.ParseFailed, doc.Unreadable)
	}
}

// The merge is the exporter's: a root platform file's `tenants:` entry is
// the tenant's default, its own file wins (`~` deletes the platform value:
// the default, enable); a subtree `_defaults.yaml` and a profile do not carry
// the key (no mode changes, no problem judged for the profile).
func TestSeverityDedup_Layers(t *testing.T) {
	t.Parallel()
	code, doc, errOut := runSD(t, map[string]string{
		"_defaults.yaml": sdRoot + "tenants:\n  ta:\n    _severity_dedup: disable\n" +
			"  tb:\n    _severity_dedup: disable\n  tc:\n    _severity_dedup: disable\n" +
			"  td:\n    _severity_dedup: purple\n" +
			"profiles:\n  p1:\n    _severity_dedup: disable\n  p2:\n    _severity_dedup: purple\n",
		"ta.yaml":            "tenants:\n  ta:\n    cpu: 70\n",
		"tb.yaml":            "tenants:\n  tb:\n    _severity_dedup: enable\n",
		"tc.yaml":            "tenants:\n  tc:\n    _severity_dedup: ~\n",
		"td.yaml":            "tenants:\n  td:\n    _severity_dedup: disable\n",
		"te.yaml":            "tenants:\n  te:\n    _profile: p1\n",
		"sub/_defaults.yaml": "_severity_dedup: disable\n",
		"sub/tf.yaml":        "tenants:\n  tf:\n    cpu: 1\n",
	})
	if code != exitOK || doc == nil {
		t.Fatalf("exit %d, stderr %q", code, errOut)
	}
	want := map[string]string{"ta": "disable", "tb": "enable", "tc": "enable", "td": "disable", "te": "enable", "tf": "enable"}
	if got := modesOf(doc); !reflect.DeepEqual(got, want) {
		t.Errorf("modes %v, want %v", got, want)
	}
	// The platform entry's unread value is judged where it is written, though
	// the tenant's own file overrides it.
	wantProblems := []severityDedupProblem{{File: "_defaults.yaml", Layer: "platform_tenants", Owner: "td",
		Problem: `unknown severity_dedup value (valid: enable, disable)`, Value: "purple"}}
	if !reflect.DeepEqual(doc.Problems, wantProblems) {
		t.Errorf("problems %+v, want %+v", doc.Problems, wantProblems)
	}
}

// A value written with a time window or expires: the exporter reads its
// `default` alone; every such `tenants:` entry is named, in a tenant file and
// a platform `tenants:` entry alike. A profile's is not: the exporter does not
// apply a profile's `_severity_dedup` at all (#2914), as no problem is
// reported of it either. `{default: x}` alone is no schedule.
func TestSeverityDedup_Schedules(t *testing.T) {
	t.Parallel()
	code, doc, errOut := runSD(t, map[string]string{
		"_defaults.yaml": sdRoot + "tenants:\n  tb:\n    _severity_dedup: {default: disable, expires: \"2099-01-01T00:00:00Z\"}\n" +
			"profiles:\n  p1:\n    _severity_dedup: {default: disable, overrides: [{window: \"01:00-02:00\", value: enable}]}\n",
		"ta.yaml": "tenants:\n  ta:\n    _severity_dedup: {default: disable, overrides: [{window: \"03:00-04:00\", value: enable}]}\n" +
			"    _silent_mode: {default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}\n",
		"tb.yaml": "tenants:\n  tb:\n    cpu: 1\n",
		"tc.yaml": "tenants:\n  tc:\n    _severity_dedup: {default: disable}\n",
	})
	if code != exitOK || doc == nil {
		t.Fatalf("exit %d, stderr %q", code, errOut)
	}
	if got, want := modesOf(doc), map[string]string{"ta": "disable", "tb": "disable", "tc": "disable"}; !reflect.DeepEqual(got, want) {
		t.Errorf("modes %v, want %v", got, want)
	}
	want := []severityDedupSchedule{
		{File: "_defaults.yaml", Section: "tenants", Owner: "tb"},
		{File: "ta.yaml", Section: "tenants", Owner: "ta"},
	}
	if !reflect.DeepEqual(doc.Schedules, want) {
		t.Errorf("schedules %+v, want %+v (`_silent_mode` is another key's)", doc.Schedules, want)
	}
	if len(doc.Problems) != 0 {
		t.Errorf("problems %+v", doc.Problems)
	}
}

// Every mode is the one served-values serves as `values._severity_dedup`
// over the same tree — the reading the collector's scrape makes.
func TestSeverityDedup_MatchesServedValues(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": sdRoot + "tenants:\n  ta:\n    _severity_dedup: disable\n  tc:\n    _severity_dedup: Off\n",
		"ta.yaml":        "tenants:\n  ta:\n    cpu: 70\n",
		"tb.yaml":        "tenants:\n  tb:\n    _severity_dedup: \"no\"\n  tc:\n    cpu: 1\n  td:\n    _severity_dedup: false\n",
		"te.yaml":        "tenants:\n  te:\n    _severity_dedup: {default: disable, overrides: [{window: \"03:00-04:00\", value: enable}]}\n",
	}
	code, doc, errOut := runSD(t, files)
	if code != exitOK || doc == nil {
		t.Fatalf("exit %d, stderr %q", code, errOut)
	}
	dir := writeTree(t, files)
	var out, sErr bytes.Buffer
	if c := run([]string{servedValuesCmd, "--config-dir", dir}, &out, &sErr); c != exitOK {
		t.Fatalf("served-values exit %d: %s", c, sErr.String())
	}
	var served struct {
		Tenants map[string]struct {
			Values map[string]any `json:"values"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal(out.Bytes(), &served); err != nil {
		t.Fatal(err)
	}
	if len(served.Tenants) != len(doc.Tenants) {
		t.Fatalf("tenants: served %d, severity-dedup %d", len(served.Tenants), len(doc.Tenants))
	}
	for tenant, sv := range served.Tenants {
		if got, want := doc.Tenants[tenant].Mode, sv.Values["_severity_dedup"]; got != want {
			t.Errorf("%s: severity-dedup %q, served-values %v", tenant, got, want)
		}
	}
}

// Exit codes: a tenant declared twice and a tree with no config file are
// trees the exporter's load rejects (2, no document); a file the load drops
// is 3 with the document naming it; -h is 0 and the top-level -h names the
// subcommand.
func TestSeverityDedup_ExitCodes(t *testing.T) {
	t.Parallel()
	if code, doc, errOut := runSD(t, map[string]string{
		"a/t.yaml": "tenants:\n  ta:\n    cpu: 1\n", "b/t.yaml": "tenants:\n  ta:\n    cpu: 2\n",
	}); code != exitCallerErr || doc != nil || !strings.Contains(errOut, severityDedupCmd) {
		t.Errorf("duplicate tenant: exit %d, doc %v, stderr %q", code, doc, errOut)
	}
	if code, doc, _ := runSD(t, map[string]string{"README": "x"}); code != exitCallerErr || doc != nil {
		t.Errorf("no config file: exit %d, doc %v", code, doc)
	}
	code, doc, errOut := runSD(t, map[string]string{
		"_defaults.yaml": sdRoot,
		"ta.yaml":        "tenants:\n  ta:\n    _severity_dedup: disable\n",
		"tb.yaml":        "tenants:\n  tb: [not, a, mapping]\n",
	})
	if code != exitParseFailed || doc == nil {
		t.Fatalf("broken file: exit %d, doc %v, stderr %q", code, doc, errOut)
	}
	if !reflect.DeepEqual(doc.ParseFailed, []string{"tb.yaml"}) ||
		!reflect.DeepEqual(modesOf(doc), map[string]string{"ta": "disable"}) {
		t.Errorf("broken file: parse_failed %v, modes %v", doc.ParseFailed, modesOf(doc))
	}
	var o, e bytes.Buffer
	if code := run([]string{severityDedupCmd, "-h"}, &o, &e); code != exitOK ||
		!strings.Contains(e.String(), "Exit codes") || !strings.Contains(e.String(), severityDedupSchema) {
		t.Errorf("-h: exit %d, %q", code, e.String())
	}
	o.Reset()
	e.Reset()
	if code := run([]string{"-h"}, &o, &e); code != exitOK || !strings.Contains(e.String(), severityDedupCmd) {
		t.Errorf("top-level -h does not name %s: %q", severityDedupCmd, e.String())
	}
	e.Reset()
	if code := run([]string{severityDedupCmd, "--config-dir", t.TempDir(), "extra"}, &o, &e); code != exitCallerErr {
		t.Errorf("stray argument: exit %d", code)
	}
	if code := run([]string{severityDedupCmd}, &o, &e); code != exitCallerErr {
		t.Errorf("no --config-dir: exit %d", code)
	}
}

// The document's wire names, read as raw JSON: encoding/json matches keys
// case-insensitively, so a decode into severityDedupDoc cannot pin them, nor
// tell an empty list from null. The Python reader relies on both.
func TestSeverityDedup_WireNames(t *testing.T) {
	t.Parallel()
	dir := writeTree(t, map[string]string{
		"_defaults.yaml": sdRoot,
		"ta.yaml":        "tenants:\n  ta:\n    _severity_dedup: \"no\"\n",
		"tb.yaml":        "tenants:\n  tb:\n    _severity_dedup: {default: disable, expires: \"2099-01-01T00:00:00Z\"}\n",
	})
	var out, errOut bytes.Buffer
	if code := run([]string{severityDedupCmd, "--config-dir", dir}, &out, &errOut); code != exitOK {
		t.Fatalf("exit %d, stderr %q", code, errOut.String())
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(out.Bytes(), &raw); err != nil {
		t.Fatal(err)
	}
	keys := func(m map[string]json.RawMessage) []string {
		var ks []string
		for k := range m {
			ks = append(ks, k)
		}
		sort.Strings(ks)
		return ks
	}
	if got, want := keys(raw), []string{"parse_failed", "problems", "schedules", "schema", "tenants", "unreadable"}; !reflect.DeepEqual(got, want) {
		t.Errorf("top-level keys %v, want %v", got, want)
	}
	for _, k := range []string{"parse_failed", "unreadable"} {
		if string(raw[k]) != "[]" {
			t.Errorf("%s = %s, want [] (never null)", k, raw[k])
		}
	}
	var problems, schedules []map[string]json.RawMessage
	var tenants map[string]map[string]json.RawMessage
	for k, v := range map[string]any{"problems": &problems, "schedules": &schedules, "tenants": &tenants} {
		if err := json.Unmarshal(raw[k], v); err != nil {
			t.Fatalf("%s: %v", k, err)
		}
	}
	if len(problems) != 1 || !reflect.DeepEqual(keys(problems[0]), []string{"file", "layer", "owner", "problem", "value"}) {
		t.Errorf("problems %s", raw["problems"])
	}
	if len(schedules) != 1 || !reflect.DeepEqual(keys(schedules[0]), []string{"file", "owner", "section"}) {
		t.Errorf("schedules %s", raw["schedules"])
	}
	if !reflect.DeepEqual(keys(tenants["ta"]), []string{"mode"}) {
		t.Errorf("tenants %s", raw["tenants"])
	}
}
