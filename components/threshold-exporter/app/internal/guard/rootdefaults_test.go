package guard

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func repoFile(t *testing.T, rel ...string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(append([]string{"..", "..", "..", "..", ".."}, rel...)...))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// #2386: the shapes table (validate-config reports these findings through its
// guard_errors row, #3387).
func TestDefaultsWrapperMatrix(t *testing.T) {
	t.Parallel()
	var m struct {
		ReadElsewhere []string `json:"top_level_read_elsewhere"`
		MergeDropped  []string `json:"merge_dropped_keys"`
		Cases         []struct {
			Name   string            `json:"name"`
			Files  map[string]string `json:"files"`
			Expect [][2]string       `json:"expect"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(repoFile(t, "tests", "shared", "defaults_wrapper_matrix.json"), &m); err != nil {
		t.Fatal(err)
	}
	if strings.Join(m.ReadElsewhere, ",") != strings.Join(TopLevelReadElsewhere, ",") {
		t.Fatalf("matrix top_level_read_elsewhere %q != TopLevelReadElsewhere %q", m.ReadElsewhere, TopLevelReadElsewhere)
	}
	if strings.Join(m.MergeDropped, ",") != strings.Join(config.MergeDroppedKeys(), ",") {
		t.Fatalf("matrix merge_dropped_keys %q != config.MergeDroppedKeys %q", m.MergeDropped, config.MergeDroppedKeys())
	}
	if len(m.Cases) == 0 {
		t.Fatal("matrix has no cases")
	}
	for _, tc := range m.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			t.Parallel()
			var files []config.DefaultsFile
			for name, body := range tc.Files {
				files = append(files, config.DefaultsFile{Name: name, Data: []byte(body)})
			}
			sort.Slice(files, func(i, j int) bool { return files[i].Name < files[j].Name })
			got := []string{}
			for _, f := range checkDefaultsWrapper(CheckInput{DefaultsFiles: files}) {
				if f.Severity != SeverityError || f.TenantID != "" {
					t.Errorf("finding %+v: want error with empty tenant", f)
				}
				got = append(got, f.Kind.String()+" "+f.Field)
			}
			want := []string{}
			for _, e := range tc.Expect {
				want = append(want, e[0]+" "+e[1])
			}
			sort.Strings(got)
			sort.Strings(want)
			if strings.Join(got, "|") != strings.Join(want, "|") {
				t.Fatalf("findings %q, matrix says %q", got, want)
			}
		})
	}
}

// The messages name the keys, and only the keys no reader takes.
func TestDefaultsWrapperMessagesNameTheKeys(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, file, body, want, notWant string
	}{
		{"unwrapped-root", "_defaults.yaml", "mysql_connections: 80\ncontainer_cpu: 60\nstate_filters: {}\n_x: 1\n_severity_dedup: disable\n_metadata: {}\n",
			"key(s) `_severity_dedup`, `container_cpu`, `mysql_connections`, but", "state_filters"},
		{"subtree-trap", "team/_defaults.yaml", "defaults: {}\n_severity_dedup: disable\nmysql_connections: 70\n_routing_defaults: {}\n_metadata: {}\n",
			"key(s) `_severity_dedup`, `mysql_connections` are left out of every tenant's merged config (/effective), " +
				"and a threshold among them is not served on /metrics. Move `mysql_connections` under `defaults:`", "_metadata"},
		{"root-trap", "_defaults.yaml", "defaults:\n  a: 1\n_severity_dedup: disable\n",
			"the root `defaults:` holds numbers only", "Move them"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := checkDefaultsWrapper(CheckInput{DefaultsFiles: []config.DefaultsFile{{Name: tc.file, Data: []byte(tc.body)}}})
			if len(got) != 1 {
				t.Fatalf("findings %+v, want one", got)
			}
			if !strings.Contains(got[0].Message, tc.want) || strings.Contains(got[0].Message, tc.notWant) {
				t.Errorf("message %q: want %q, not %q", got[0].Message, tc.want, tc.notWant)
			}
		})
	}
}

// A file the exporter drops is named by exit 3 only.
func TestDefaultsWrapperSkipsParseFailed(t *testing.T) {
	t.Parallel()
	got := checkDefaultsWrapper(CheckInput{
		DefaultsFiles: []config.DefaultsFile{
			{Name: "_defaults.yaml", Data: []byte("mysql_connections: 80\n")},
			{Name: "team/_defaults.yaml", Data: []byte("defaults: {}\n_severity_dedup: disable\n")},
		},
		ParseFailed: []string{"_defaults.yaml", "team/_defaults.yaml"},
	})
	if len(got) != 0 {
		t.Fatalf("findings %+v, want none", got)
	}
}

// Both findings are errors in the report: they block (exit 1 in da-guard).
func TestCheckDefaultsImpact_DefaultsWrapperFindingsCountAsErrors(t *testing.T) {
	t.Parallel()
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"tx": {"mysql_connections": 80}},
		DefaultsFiles: []config.DefaultsFile{
			{Name: "_defaults.yaml", Data: []byte("mysql_connections: 80\n")},
			{Name: "team/_defaults.yaml", Data: []byte("defaults: {}\n_severity_dedup: disable\n")},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if report.Summary.Errors != 2 {
		t.Fatalf("report %+v: want two errors", report)
	}
}

// rootDecodedKeys (reflected from config.ThresholdConfig) and the non-`_`
// properties of platform-defaults.schema.json are the two spellings of "a
// top-level key the root decode reads": da-guard uses the first, the
// schema's readers the second. They must be one set.
func TestRootDecodedKeysMatchSchema(t *testing.T) {
	t.Parallel()
	var schema struct {
		Properties map[string]json.RawMessage `json:"properties"`
	}
	if err := json.Unmarshal(repoFile(t, "docs", "schemas", "platform-defaults.schema.json"), &schema); err != nil {
		t.Fatal(err)
	}
	var fromSchema, fromStruct []string
	for k := range schema.Properties {
		if !strings.HasPrefix(k, "_") {
			fromSchema = append(fromSchema, k)
		}
	}
	for k := range rootDecodedKeys {
		fromStruct = append(fromStruct, k)
	}
	sort.Strings(fromSchema)
	sort.Strings(fromStruct)
	if strings.Join(fromSchema, ",") != strings.Join(fromStruct, ",") {
		t.Fatalf("schema non-`_` properties %q != ThresholdConfig yaml keys %q", fromSchema, fromStruct)
	}
}

// #2388 r4: in a SUBTREE file, a top-level key subtree defaults refuse
// (config.SubtreeDefaultsRefusedKey) is not told to move under `defaults:` —
// that is subtree_default_reserved_key's finding — but gets that finding's
// own fix, by shape: (c) recognised → each tenant's entry, (a) `_state_<f>`
// with f undeclared → declare or delete, (b) unrecognised (`_silent_x`, a bare
// `_state_`) → delete. Other keys keep the generic fix; the ROOT file keeps
// its own (rootNumbersOnly) whatever the key.
func TestDefaultsWrapperSubtreeRefusedKeyFix(t *testing.T) {
	t.Parallel()
	const moveUnder = "Move `mysql_connections` under `defaults:`"
	cases := []struct {
		name, file, body string
		want, notWant    []string
	}{
		{"recognised", "a/_defaults.yaml", "defaults:\n  mysql_connections: 70\n_silent_mode: warning\n",
			[]string{"`_silent_mode`: subtree defaults do not support it, so do not move it under `defaults:`",
				"Today it has no effect here", "delete it from this file to keep things as they are.",
				"set `_silent_mode` in each tenant's own entry under `tenants:`"},
			[]string{"Move them", "Move `_silent_mode`"}},
		{"undeclared-state", "a/_defaults.yaml", "defaults:\n  x: 1\n_state_nope: enable\n",
			[]string{"does not declare a filter `nope`", "or delete it from this file"},
			[]string{"Move them", "Set `_state_nope`"}},
		{"declared-state", "a/_defaults.yaml", "defaults:\n  x: 1\n_state_maintenance: enable\n",
			[]string{"set `_state_maintenance` in each tenant's own entry", "affects every tenant in the tree"},
			[]string{"Move them", "does not declare"}},
		// (No (b) key reaches this finding: actsWhenMerged needs IsReservedKey
		// and leaves out the `_routing` prefix, and IsReservedKey minus that is
		// recognised. A bare `_state_` is the filter "" — undeclared here, (a).)
		{"bare-state", "a/_defaults.yaml", "defaults:\n  x: 1\n_state_: 1\n",
			[]string{"`_state_`: subtree defaults do not support it", "does not declare a filter `\"\"`",
				"or delete it from this file"},
			[]string{"Move them", "not a recognised key"}},
		{"mixed", "a/_defaults.yaml", "defaults:\n  x: 1\nmysql_connections: 70\n_silent_mode: warning\n",
			[]string{moveUnder + ".", "set `_silent_mode` in each tenant's own entry"},
			// A refused key is in the file: no "leave `defaults:` with no value" (r5).
			[]string{"Move them", "Move `_silent_mode`", "leave `defaults:` with no value"}},
		// Root control: unchanged by #2388 r4.
		{"root", "_defaults.yaml", "defaults:\n  a: 1\n_silent_mode: warning\n",
			[]string{"the root `defaults:` holds numbers only"},
			[]string{"subtree_default_reserved_key", "set `_silent_mode`", "Move them"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := checkDefaultsWrapper(CheckInput{
				DefaultsFiles:        []config.DefaultsFile{{Name: tc.file, Data: []byte(tc.body)}},
				DeclaredStateFilters: map[string]bool{"maintenance": true},
			})
			if len(got) != 1 || got[0].Kind != findingkind.FindingDefaultsTopLevelIgnored {
				t.Fatalf("findings %+v, want one defaults_toplevel_ignored", got)
			}
			for _, w := range tc.want {
				if !strings.Contains(got[0].Message, w) {
					t.Errorf("message lacks %q: %s", w, got[0].Message)
				}
			}
			for _, d := range tc.notWant {
				if strings.Contains(got[0].Message, d) {
					t.Errorf("message has %q: %s", d, got[0].Message)
				}
			}
		})
	}
}

// #3042: no reader takes custom alerts from a `_defaults.yaml`, so a root
// file's `_custom_alerts` is reported by both shapes — with where the list
// goes instead; a finding without it does not say so.
func TestDefaultsWrapperRootCustomAlertsFix(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		body string
		kind findingkind.Kind
		want bool
	}{
		"unwrapped":         {"_custom_alerts: []\nmysql_connections: 70\n", findingkind.FindingRootDefaultsUnwrapped, true},
		"wrapped":           {"defaults:\n  x: 1\n_custom_alerts: []\n", findingkind.FindingDefaultsTopLevelIgnored, true},
		"wrapped-no-alerts": {"defaults:\n  x: 1\nmysql_connections: 70\n", findingkind.FindingDefaultsTopLevelIgnored, false},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got := checkDefaultsWrapper(CheckInput{DefaultsFiles: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte(tc.body)}}})
			if len(got) != 1 || got[0].Kind != tc.kind || got[0].Severity != SeverityError {
				t.Fatalf("findings = %+v", got)
			}
			if has := strings.Contains(got[0].Message, customAlertsRootFix); has != tc.want {
				t.Errorf("custom-alerts fix in %q: %v, want %v", got[0].Message, has, tc.want)
			}
		})
	}
}

// #3140, #3250: a root threshold whose value the exporter refuses is an error
// finding, one per key, named by the exporter's own decode
// (config.RootDefaultRefusals) with the text written and why; a subtree
// carrier, a `_` key, a read value and a dropped file are not.
func TestCheckRootDefaultsRefused(t *testing.T) {
	t.Parallel()
	got := checkRootDefaultsRefused(CheckInput{
		DefaultsFiles: []config.DefaultsFile{
			{Name: "_defaults.yaml", Data: []byte("defaults:\n  b: abc\n  a: NaN\n  c: 80\n  d: \"70\"\n  _x: .inf\n")},
			{Name: "sub/_defaults.yaml", Data: []byte("defaults:\n  a: .nan\n")},
		},
	})
	var fields []string
	for _, f := range got {
		if f.Severity != SeverityError || f.Kind != findingkind.FindingRootDefaultRefused {
			t.Errorf("finding %+v, want an error root_default_refused", f)
		}
		why := map[string]string{"_defaults.yaml:defaults.a": "`a: NaN` under the conf.d root `defaults:` is not a finite number",
			"_defaults.yaml:defaults.b": "`b: abc` under the conf.d root `defaults:` is not a number the exporter reads"}[f.Field]
		if why == "" || !strings.Contains(f.Message, why) || !strings.Contains(f.Message, "optional_overrides:") {
			t.Errorf("message %q, want it to say %q", f.Message, why)
		}
		fields = append(fields, f.Field)
	}
	if want := []string{"_defaults.yaml:defaults.a", "_defaults.yaml:defaults.b"}; strings.Join(fields, ",") != strings.Join(want, ",") {
		t.Errorf("fields = %v, want %v", fields, want)
	}
	if n := len(checkRootDefaultsRefused(CheckInput{
		DefaultsFiles: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("defaults:\n  a: .nan\n")}},
		ParseFailed:   []string{"_defaults.yaml"},
	})); n != 0 {
		t.Errorf("a dropped file got %d findings", n)
	}
	// Wired into the run.
	rep, err := CheckDefaultsImpact(CheckInput{DefaultsFiles: []config.DefaultsFile{
		{Name: "_defaults.yaml", Data: []byte("defaults:\n  a: .nan\n")}}})
	if err != nil || rep.Summary.Errors != 1 {
		t.Fatalf("CheckDefaultsImpact: %+v, %v; want one error", rep, err)
	}
}

// #3556: a `_critical` tier under the root `dimensions:` is an error, in
// either #1231 spelling (the retired one taken from the alias table, so this
// file need not spell it), with or without the `defaults:` wrapper; a
// subtree carrier's `dimensions:` is ignored by the exporter and not judged.
func TestCheckRootDimensionsCriticalBase(t *testing.T) {
	t.Parallel()
	var legacy, canon string
	for l, c := range config.DeprecatedKeyAliases() {
		legacy, canon = l, c
	}
	if legacy == "" {
		t.Skip("no #1231 alias left to spell the retired tier with")
	}
	root := "defaults:\n  " + canon + ": 10\ndimensions:\n  " + canon + ": [db]\n  " +
		canon + "_critical: [db]\n  " + legacy + "_critical: [db]\n"
	got := checkRootDimensionsCriticalBase(CheckInput{DefaultsFiles: []config.DefaultsFile{
		{Name: "_defaults.yaml", Data: []byte(root)},
		{Name: "sub/_defaults.yaml", Data: []byte("dimensions:\n  x_critical: [db]\n")},
	}})
	var fields []string
	for _, f := range got {
		if f.Severity != SeverityError || f.Kind != findingkind.FindingRootDimensionsCriticalBase || f.TenantID != "" {
			t.Errorf("finding %+v, want a tree-level error root_dimensions_critical_base", f)
		}
		if !strings.Contains(f.Message, `{label="v"}: "N:critical"`) {
			t.Errorf("message %q, want it to name the N:critical form", f.Message)
		}
		fields = append(fields, f.Field)
	}
	want := []string{"_defaults.yaml:dimensions." + canon + "_critical", "_defaults.yaml:dimensions." + legacy + "_critical"}
	sort.Strings(want)
	if strings.Join(fields, ",") != strings.Join(want, ",") {
		t.Errorf("fields = %v, want %v", fields, want)
	}
	unwrapped := checkRootDimensionsCriticalBase(CheckInput{DefaultsFiles: []config.DefaultsFile{
		{Name: "_defaults.yaml", Data: []byte("dimensions:\n  x_critical: [db]\n")}}})
	if len(unwrapped) != 1 {
		t.Errorf("a root without `defaults:` still declares `dimensions:`; got %v", unwrapped)
	}
	if n := len(checkRootDimensionsCriticalBase(CheckInput{
		DefaultsFiles: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte(root)}},
		ParseFailed:   []string{"_defaults.yaml"},
	})); n != 0 {
		t.Errorf("a dropped file got %d findings", n)
	}
	rep, err := CheckDefaultsImpact(CheckInput{DefaultsFiles: []config.DefaultsFile{
		{Name: "_defaults.yaml", Data: []byte("dimensions:\n  x_critical: [db]\n")}}})
	if err != nil || rep.Summary.Errors < 1 {
		t.Fatalf("CheckDefaultsImpact: %+v, %v; want the error counted", rep, err)
	}
}

// #4236: the disable advice of root_default_refused names exactly the keys
// the root reads `disable` for (config.RootDisableText): every key of the
// built-in layer — its table of values and its off table (#4163) — is told
// to write `<key>: disable` for an alias, a retired spelling is pointed at
// its current one, and a key the platform ships no value for is not sent to
// `optional_overrides:`.
func TestCheckRootDefaultsRefused_DisableAdviceFollowsTheReader(t *testing.T) {
	t.Parallel()
	msg := func(t *testing.T, y string) string {
		t.Helper()
		got := checkRootDefaultsRefused(CheckInput{DefaultsFiles: []config.DefaultsFile{
			{Name: "_defaults.yaml", Data: []byte(y)}}})
		if len(got) != 1 {
			t.Fatalf("%q: %d findings, want 1: %+v", y, len(got), got)
		}
		return got[0].Message
	}
	keys := config.BuiltinOffKeys()
	for k := range config.BuiltinDefaults() {
		keys = append(keys, k)
	}
	for _, k := range keys {
		if !config.RootDisableText(k, "disable") {
			t.Fatalf("%s: the reader reads no root `disable` for a key of the built-in layer", k)
		}
		if n := len(checkRootDefaultsRefused(CheckInput{DefaultsFiles: []config.DefaultsFile{
			{Name: "_defaults.yaml", Data: []byte("defaults:\n  " + k + ": disable\n")}}})); n != 0 {
			t.Errorf("%s: disable got %d findings, want none (the reader takes it)", k, n)
		}
		m := msg(t, "defaults:\n  "+k+": \"off\"\n")
		if !strings.Contains(m, "write `"+k+": disable`") || strings.Contains(m, "is not one") {
			t.Errorf("%s: alias advice %q, want it to name `%s: disable`", k, m, k)
		}
		if config.BuiltinOff(k) && (strings.Contains(m, "optional_overrides") ||
			!strings.Contains(m, "The platform ships no value for `"+k+"`")) {
			t.Errorf("%s: off-key message %q", k, m)
		}
	}

	// A retired spelling: the root reads `disable` under the current one only.
	const retired = "mysql_cpu"
	canon, _ := config.CanonicalKeyFor(retired)
	if canon == retired || !config.BuiltinOff(canon) || config.RootDisableText(retired, "disable") {
		t.Fatalf("fixture: %s → %s is no retired spelling of an off key", retired, canon)
	}
	if m := msg(t, "defaults:\n  "+retired+": disable\n"); !strings.Contains(m, "retired spelling of `"+canon+"`") ||
		!strings.Contains(m, "write `"+canon+": disable`") || strings.Contains(m, "optional_overrides") {
		t.Errorf("retired disable: %q", m)
	}
	if m := msg(t, "defaults:\n  "+retired+": abc\n"); !strings.Contains(m, "The platform ships no value for `"+canon+"`") {
		t.Errorf("retired refused: %q", m)
	}

	// Outside the registry the root gives the key nothing to switch off.
	if m := msg(t, "defaults:\n  nosuch_key: disable\n"); !strings.Contains(m, "is not one") ||
		!strings.Contains(m, "optional_overrides:") {
		t.Errorf("unregistered disable: %q", m)
	}
}

// servedRows is every row the exporter serves for a tree of one root
// `_defaults.yaml` (root) and one tenant that writes nothing, sorted.
func servedRows(t *testing.T, root string) []string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range map[string]string{"_defaults.yaml": root, "t.yaml": "tenants:\n  tx: {}\n"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("LoadDir: %v", err)
	}
	var out []string
	for _, r := range cfg.ResolveAt(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)) {
		out = append(out, fmt.Sprintf("%s/%s_%s %s %g", r.Tenant, r.Component, r.Metric, r.Severity, r.Value))
	}
	sort.Strings(out)
	return out
}

// #4236: what root_default_refused says the line does is what the exporter
// does. Each case is a root `defaults:` of a refused line (first) and the
// lines beside it; the tree is loaded with and without the refused line. A
// message that says the line is as if not there, or ignored, must serve the
// same rows both ways; one that says the line stands in place of another
// spelling must not.
func TestCheckRootDefaultsRefused_WhatItSaysTheLineDoesIsServed(t *testing.T) {
	t.Parallel()
	const retired = "mysql_cpu"
	canon, _ := config.CanonicalKeyFor(retired)
	if canon == retired {
		t.Fatalf("fixture: %s is no retired spelling", retired)
	}
	type tc struct{ line, rest string }
	var cases []tc
	for k := range config.BuiltinDefaults() {
		cases = append(cases, tc{k + `: "off"`, ""})
	}
	for _, k := range config.BuiltinOffKeys() {
		cases = append(cases, tc{k + `: "off"`, ""})
	}
	cases = append(cases,
		tc{retired + ": abc", ""},
		tc{retired + ": disable", ""},
		tc{retired + ": abc", canon + ": 7"},
		tc{retired + ": disable", canon + ": 7"},
		tc{canon + `: "off"`, retired + ": 7"},
		tc{canon + ": abc", retired + ": 7"},
		tc{retired + ": abc", canon + ": null"},
		tc{canon + `: "off"`, retired + ": null"},
		tc{canon + ": abc", retired + `: "off"`},
		tc{retired + `: "off"`, canon + ": abc"},
		tc{retired + ": abc", canon + `: "off"`},
		tc{canon + `: "off"`, retired + ": abc"},
	)
	for _, c := range cases {
		without := "defaults:\n"
		if c.rest != "" {
			without += "  " + c.rest + "\n"
		}
		with := without + "  " + c.line + "\n"
		got := checkRootDefaultsRefused(CheckInput{DefaultsFiles: []config.DefaultsFile{
			{Name: "_defaults.yaml", Data: []byte(with)}}})
		key := strings.SplitN(c.line, ":", 2)[0]
		var m string
		for _, f := range got {
			if f.Field == "_defaults.yaml:defaults."+key {
				m = f.Message
			}
		}
		same := strings.Join(servedRows(t, with), "\n") == strings.Join(servedRows(t, without), "\n")
		absent := strings.Contains(m, "as if the line were not there") || strings.Contains(m, "ignores this line")
		inPlace := strings.Contains(m, "reads this line in its place")
		// Both spellings refused: the message claims no effect of the line.
		both := strings.Contains(m, "and refuses it too")
		switch {
		case m == "":
			t.Errorf("%q: no finding for `%s`", with, key)
		case both:
			if absent || inPlace {
				t.Errorf("%q: a message on two refused spellings claims an effect: %q", with, m)
			}
		case absent == inPlace:
			t.Errorf("%q: the message says neither or both of what the line does: %q", with, m)
		case absent && !same:
			t.Errorf("%q: the message says the line does nothing, the exporter serves otherwise: %q", with, m)
		case inPlace && same:
			t.Errorf("%q: the message says the line takes another's place, the exporter serves the same: %q", with, m)
		}
	}
}

// #4236: two refused spellings of one threshold still get the disable
// advice when the line is an alias of it.
func TestCheckRootDefaultsRefused_TwoRefusedSpellingsKeepTheDisableAdvice(t *testing.T) {
	t.Parallel()
	const retired = "mysql_cpu"
	canon, _ := config.CanonicalKeyFor(retired)
	got := checkRootDefaultsRefused(CheckInput{DefaultsFiles: []config.DefaultsFile{{Name: "_defaults.yaml",
		Data: []byte("defaults:\n  " + canon + ": \"off\"\n  " + retired + ": disable\n")}}})
	for _, f := range got {
		if f.Field == "_defaults.yaml:defaults."+canon &&
			strings.Contains(f.Message, "and refuses it too") &&
			strings.Contains(f.Message, "write `"+canon+": disable`") {
			return
		}
	}
	t.Errorf("findings %+v: want the two-refused message on %s with its disable advice", got, canon)
}
