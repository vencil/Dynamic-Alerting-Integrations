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
		{"unwrapped-root", "_defaults.yaml", "mysql_connections: 80\ncontainer_cpu: 60\nstate_filters: {}\n_x: 1\n",
			"key(s) `container_cpu`, `mysql_connections` are", "state_filters"},
		{"subtree-trap", "team/_defaults.yaml", "defaults: {}\n_severity_dedup: disable\nmysql_connections: 70\n_routing_defaults: {}\n",
			"key(s) `_severity_dedup`, `mysql_connections` reach no tenant. Move them under `defaults:`", "_routing_defaults"},
		{"root-trap", "_defaults.yaml", "defaults:\n  a: 1\n_severity_dedup: disable\n",
			"the root file holds numbers only", "Move them"},
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
