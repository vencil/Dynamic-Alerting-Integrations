package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

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

// #2386: the shared table both da-guard and validate-config are judged by.
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
				got = append(got, string(f.Kind)+" "+f.Field)
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
// top-level key the root decode reads": da-guard uses the first,
// validate-config the second. They must be one set.
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
				"Today it has no effect; delete it from this file to keep things as they are.",
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
			if len(got) != 1 || got[0].Kind != FindingDefaultsTopLevelIgnored {
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
