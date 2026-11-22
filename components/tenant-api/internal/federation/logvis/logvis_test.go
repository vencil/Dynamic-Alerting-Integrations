package logvis

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
	"gopkg.in/yaml.v3"
)

func fields(ss ...string) *[]string { return &ss }

// TestValidate_RejectsFieldsOutsidePlatformSet is the containment rule: a
// selection may only narrow the platform-curated set. A topology field the
// Vector allowlist structurally drops (upstream, pod_node) must not be
// selectable, nor may an always-visible field be "selected" or a field repeat.
func TestValidate_RejectsFieldsOutsidePlatformSet(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		fields *[]string
		want   string // substring of the single expected violation; "" = valid
	}{
		{"default", nil, ""},
		{"empty keeps only always-visible", fields(), ""},
		{"subset", fields("status", "path"), ""},
		{"every selectable", fields(SelectableFields...), ""},
		{"topology field", fields("status", "upstream"), `"upstream" is not a field the platform makes visible`},
		{"node field", fields("pod_node"), `"pod_node" is not a field`},
		{"empty name", fields(""), `"" is not a field`},
		{"case differs", fields("Status"), `"Status" is not a field`},
		{"always visible", fields("log_event_id"), "always visible"},
		{"duplicate", fields("status", "status"), `duplicate field "status"`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			v := Validate(&Selection{Fields: tc.fields})
			if tc.want == "" {
				if len(v) != 0 {
					t.Fatalf("Validate = %+v, want valid", v)
				}
				return
			}
			if len(v) != 1 || !strings.Contains(v[0].Reason, tc.want) {
				t.Fatalf("Validate = %+v, want one violation containing %q", v, tc.want)
			}
		})
	}
}

func TestParse_ExplicitNullIsDefault(t *testing.T) {
	t.Parallel()
	s, err := Parse([]byte("fields: null\n"))
	if err != nil {
		t.Fatalf("Parse(fields: null): %v", err)
	}
	if s.Fields != nil {
		t.Errorf("Parse(fields: null).Fields = %v, want nil (default)", *s.Fields)
	}
	s, err = Parse([]byte("fields: []\n"))
	if err != nil {
		t.Fatal(err)
	}
	if s.Fields == nil || len(*s.Fields) != 0 {
		t.Errorf("Parse(fields: []) = %v, want a non-nil empty list", s.Fields)
	}
}

// TestParse_DamagedRecordFailsClosed: tenant-api always writes the `fields`
// key, so a record without it, with another key, or empty is damaged. Read
// leniently each would be `fields: null` — every curated field — so each must
// be an error, never the default.
func TestParse_DamagedRecordFailsClosed(t *testing.T) {
	t.Parallel()
	for _, doc := range []string{
		"",
		"{}\n",
		"bogus: 1\n",
		"fields: [status]\nbogus: 1\n",
		"Fields: [status]\n",
		"- status\n",
		"fields: {status: true}\n",
	} {
		if s, err := Parse([]byte(doc)); err == nil {
			t.Errorf("Parse(%q) = %+v, want an error (a damaged record must not read as a selection)", doc, s)
		}
	}
}

func TestEffective_DropsFieldsNoLongerSelectable(t *testing.T) {
	t.Parallel()
	got := Effective(&Selection{Fields: fields("status", "retired_field", "path")})
	if got.Fields == nil || !reflect.DeepEqual(*got.Fields, []string{"status", "path"}) {
		t.Fatalf("Effective = %v, want [status path]", got.Fields)
	}
	if Effective(&Selection{}).Fields != nil {
		t.Error("Effective(default) must stay the default")
	}
}

func TestVisibleFields(t *testing.T) {
	t.Parallel()
	all := append(append([]string{}, AlwaysVisibleFields...), SelectableFields...)
	if got := VisibleFields(&Selection{}); !reflect.DeepEqual(got, all) {
		t.Errorf("default = %v, want %v", got, all)
	}
	want := append(append([]string{}, AlwaysVisibleFields...), "status", "path")
	if got := VisibleFields(&Selection{Fields: fields("path", "status")}); !reflect.DeepEqual(got, want) {
		t.Errorf("subset = %v, want %v", got, want)
	}
}

// TestVocabularyMatchesVectorChart pins the vocabulary to the chart that
// enforces it: AlwaysVisibleFields ∪ SelectableFields must equal
// tenantProjectionKeepFields, and AlwaysVisibleFields must equal
// tenantProjectionAlwaysVisibleFields. A field added to the chart's allowlist
// without being added here is merely unselectable (the default still shows
// it); a field removed from the chart but still accepted here would make the
// chart refuse to render a tenant's copied selection — this test catches
// both before release.
func TestVocabularyMatchesVectorChart(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile(filepath.Join(testutil.RepoRoot(t), "helm", "vector", "values.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var vals struct {
		Keep   []string `yaml:"tenantProjectionKeepFields"`
		Always []string `yaml:"tenantProjectionAlwaysVisibleFields"`
	}
	if err := yaml.Unmarshal(raw, &vals); err != nil {
		t.Fatal(err)
	}
	sorted := func(ss []string) []string {
		out := append([]string{}, ss...)
		sort.Strings(out)
		return out
	}
	union := append(append([]string{}, AlwaysVisibleFields...), SelectableFields...)
	if !reflect.DeepEqual(sorted(union), sorted(vals.Keep)) {
		t.Errorf("AlwaysVisibleFields ∪ SelectableFields = %v, chart tenantProjectionKeepFields = %v", sorted(union), sorted(vals.Keep))
	}
	if !reflect.DeepEqual(sorted(AlwaysVisibleFields), sorted(vals.Always)) {
		t.Errorf("AlwaysVisibleFields = %v, chart tenantProjectionAlwaysVisibleFields = %v", AlwaysVisibleFields, vals.Always)
	}
}
