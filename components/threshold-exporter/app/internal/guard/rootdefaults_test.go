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

// #2386: the root carrier's shape, judged from its bytes.
func TestCheckRootDefaultsWrapper(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name        string
		files       []config.DefaultsFile
		parseFailed []string
		want        []string // Field of each finding
		wantKeys    string   // substring of the single finding's message
	}{
		{
			name:     "unwrapped-root-threshold",
			files:    []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("mysql_connections: 80\ncontainer_cpu: 60\n")}},
			want:     []string{"_defaults.yaml"},
			wantKeys: "`container_cpu`, `mysql_connections`",
		},
		{
			name:     "yml-root-carrier",
			files:    []config.DefaultsFile{{Name: "_defaults.yml", Data: []byte("mysql_connections: 80\n")}},
			want:     []string{"_defaults.yml"},
			wantKeys: "`mysql_connections`",
		},
		{
			name: "unwrapped-root-beside-decoded-and-reserved-keys",
			files: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte(
				"state_filters: {}\nmax_metrics_per_tenant: 10\n_routing_defaults: {}\nmysql_connections: 80\n")}},
			want:     []string{"_defaults.yaml"},
			wantKeys: "key(s) `mysql_connections` are",
		},
		{
			name:  "wrapped-root",
			files: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("defaults:\n  mysql_connections: 80\n")}},
		},
		{
			name:  "null-defaults-key-is-wrapped",
			files: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("defaults:\nstate_filters: {}\n")}},
		},
		{
			name:  "only-decoded-and-reserved-keys",
			files: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("state_filters: {}\n_routing_defaults: {}\n_custom_alerts: []\n")}},
		},
		{
			name:  "empty-and-comment-only-root",
			files: []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("# placeholder\n")}},
		},
		{
			name:  "subtree-carrier-is-served-unwrapped",
			files: []config.DefaultsFile{{Name: "team-a/_defaults.yaml", Data: []byte("mysql_connections: 70\n")}},
		},
		{
			name:        "parse-failed-root-is-named-by-exit-3-only",
			files:       []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("mysql_connections: 80\n")}},
			parseFailed: []string{"_defaults.yaml"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := checkRootDefaultsWrapper(CheckInput{DefaultsFiles: tc.files, ParseFailed: tc.parseFailed})
			fields := []string{}
			for _, f := range got {
				if f.Kind != FindingRootDefaultsUnwrapped || f.Severity != SeverityError || f.TenantID != "" {
					t.Errorf("finding %+v: want error %s with empty tenant", f, FindingRootDefaultsUnwrapped)
				}
				fields = append(fields, f.Field)
			}
			want := tc.want
			if want == nil {
				want = []string{}
			}
			if strings.Join(fields, ",") != strings.Join(want, ",") {
				t.Fatalf("fields %q, want %q", fields, want)
			}
			if tc.wantKeys != "" && !strings.Contains(got[0].Message, tc.wantKeys) {
				t.Errorf("message %q does not name %s", got[0].Message, tc.wantKeys)
			}
		})
	}
}

// The finding is an error in the report: it blocks (exit 1 in da-guard).
func TestCheckDefaultsImpact_RootDefaultsUnwrappedCountsAsError(t *testing.T) {
	t.Parallel()
	report, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"tx": {"mysql_connections": 80}},
		DefaultsFiles:    []config.DefaultsFile{{Name: "_defaults.yaml", Data: []byte("mysql_connections: 80\n")}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if report.Summary.Errors != 1 || len(report.Findings) != 1 ||
		report.Findings[0].Kind != FindingRootDefaultsUnwrapped {
		t.Fatalf("report %+v: want one root_defaults_unwrapped error", report)
	}
}

// rootDecodedKeys (reflected from config.ThresholdConfig) and the non-`_`
// properties of platform-defaults.schema.json are the two spellings of "a
// top-level key the root decode reads": da-guard uses the first,
// validate-config's root_defaults row the second. They must be one set.
func TestRootDecodedKeysMatchSchema(t *testing.T) {
	t.Parallel()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "..", "docs", "schemas", "platform-defaults.schema.json"))
	if err != nil {
		t.Fatal(err)
	}
	var schema struct {
		Properties map[string]json.RawMessage `json:"properties"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
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
