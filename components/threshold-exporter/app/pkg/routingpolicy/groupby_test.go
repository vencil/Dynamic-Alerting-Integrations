package routingpolicy

import (
	"reflect"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestGroupByProblems (#2503) holds the rules to the same case list as the
// Python half (tests/ops/test_grar_group_by.py::TestPredicate::test_rules).
func TestGroupByProblems(t *testing.T) {
	t.Parallel()
	type p = [2]any // index field suffix, kind
	cases := []struct {
		in       []any
		kept     []string
		problems []p
	}{
		{[]any{"alertname", "tenant"}, []string{"alertname", "tenant"}, nil},
		{[]any{"..."}, []string{"..."}, nil},
		{[]any{"alertname", "8"}, []string{"alertname", "8"}, nil},
		{[]any{"alertname", "a b"}, []string{"alertname", "a b"}, nil},
		{[]any{"alertname", true, 8}, []string{"alertname"}, []p{{1, GroupByNotString}, {2, GroupByNotString}}},
		{[]any{true, true}, nil, []p{{0, GroupByNotString}, {1, GroupByNotString}}},
		{[]any{"alertname", nil}, []string{"alertname"}, []p{{1, GroupByNotString}}},
		{[]any{"alertname", ""}, []string{"alertname"}, []p{{1, GroupByEmpty}}},
		{[]any{"alertname", "alertname", "tenant", "tenant"}, []string{"alertname", "tenant"},
			[]p{{1, GroupByDuplicate}, {3, GroupByDuplicate}}},
		{[]any{"alertname", "..."}, []string{"alertname"}, []p{{1, GroupByWildcardMixed}}},
		// #2503 round 2 F2: AM's repeat check skips the wildcard.
		{[]any{"...", "alertname", "..."}, []string{"alertname"}, []p{{0, GroupByWildcardMixed}, {2, GroupByWildcardMixed}}},
		{[]any{"...", "..."}, []string{"...", "..."}, nil},
		{[]any{"alertname", "...", "..."}, []string{"alertname"}, []p{{1, GroupByWildcardMixed}, {2, GroupByWildcardMixed}}},
		{[]any{"...", 8}, []string{"..."}, []p{{1, GroupByNotString}}},
		{[]any{}, nil, nil},
	}
	for _, tc := range cases {
		kept, problems := GroupByProblems("x.", tc.in)
		var got []p
		for _, pr := range problems {
			if !strings.HasPrefix(pr.Field, "x.group_by[") {
				t.Errorf("%v: field %q", tc.in, pr.Field)
			}
			var idx int
			for _, c := range strings.TrimSuffix(strings.TrimPrefix(pr.Field, "x.group_by["), "]") {
				idx = idx*10 + int(c-'0')
			}
			got = append(got, p{idx, pr.Kind})
		}
		if !reflect.DeepEqual(kept, tc.kept) || !reflect.DeepEqual(got, tc.problems) {
			t.Errorf("%#v: kept %#v problems %v; want %#v %v", tc.in, kept, got, tc.kept, tc.problems)
		}
	}
}

// TestGroupByInvalid_ReadAsPyYAML (#2503): the values judged are PyYAML's —
// yaml.v3 alone reads a plain `on` as the string "on", which the predicate
// accepts; WithPyYAMLRouting makes it a bool (and `8` an int).
// Quoted, they stay strings. Order: main, overrides, routes.
func TestGroupByInvalid_ReadAsPyYAML(t *testing.T) {
	t.Parallel()
	src := "group_by: [alertname, on, 8]\n" +
		"overrides:\n- {alertname: X, group_by: [alertname, alertname]}\n- {alertname: Y, group_by: ['on', \"8\"]}\n" +
		"routes:\n- {match: {team: db}, group_by: [alertname, '...']}\n- {match: {team: x}, group_by: ['']}\n"
	var n yaml.Node
	if err := yaml.Unmarshal([]byte(src), &n); err != nil {
		t.Fatal(err)
	}
	v3 := decode(t, src)
	if got := GroupByInvalid(v3); len(got) != 4 || got[0].Field != "group_by[2]" {
		t.Fatalf("yaml.v3 alone: %+v — expected `on` read as the string \"on\" (not judged)", got)
	}
	got := GroupByInvalid(withPyYAMLRoutingFrom(v3, &n))
	var fields []string
	for _, p := range got {
		fields = append(fields, p.Field+" "+p.Kind)
	}
	want := []string{"group_by[1] not_string", "group_by[2] not_string", "overrides[0].group_by[1] duplicate",
		"routes[0].group_by[1] wildcard_mixed", "routes[1].group_by[0] empty"}
	if !reflect.DeepEqual(fields, want) {
		t.Errorf("GroupByInvalid = %v, want %v", fields, want)
	}
	if msg := got[0].Message(); msg != "group_by[1] is bool True, not a string — quote it in YAML (e.g. \"8\", \"on\") "+
		"so it is a label name; Alertmanager would group by a label named after its text" {
		t.Errorf("message = %q", msg)
	}
	if msg := got[2].Message(); msg != "overrides[0].group_by[1] repeats label 'alertname' listed earlier — "+
		"Alertmanager refuses a repeated non-wildcard group_by label; remove it" {
		t.Errorf("message = %q", msg)
	}
}

// TestGroupByInvalid_FailClosed (#2503): a group_by with no PyYAML reading is
// never judged as yaml.v3 read it — each element is UnmatchedValue, refused.
func TestGroupByInvalid_FailClosed(t *testing.T) {
	t.Parallel()
	v3 := map[string]any{"group_by": []any{"alertname", "on"},
		"routes": []any{map[string]any{"group_by": []any{"x"}}, map[any]any{1: "y", "group_by": []any{"z"}}}}
	got := GroupByInvalid(WithPyYAMLRouting(v3, nil))
	var fields []string
	for _, p := range got {
		fields = append(fields, p.Field)
	}
	want := []string{"group_by[0]", "group_by[1]", "routes[0].group_by[0]", "routes[1].group_by[0]"}
	if !reflect.DeepEqual(fields, want) {
		t.Errorf("fields = %v, want %v", fields, want)
	}
	if v3["group_by"].([]any)[1] != "on" {
		t.Error("WithPyYAMLRouting modified its routing argument")
	}
	if got := GroupByInvalid(map[string]any{"group_by": "alertname", "overrides": "x"}); got != nil {
		t.Errorf("non-list group_by judged: %+v", got)
	}
}

// TestGroupByInvalidForTenant (#2503 round 3): an unresolved routing is
// judged after `{{tenant}}` substitution, at every position; routing is not
// modified.
func TestGroupByInvalidForTenant(t *testing.T) {
	t.Parallel()
	routing := map[string]any{"group_by": []any{"alertname", "{{tenant}}"},
		"routes": []any{map[string]any{"group_by": []any{"{{tenant}}", "x", "alertname"}}}}
	var got []string
	for _, p := range GroupByInvalidForTenant("alertname", routing) {
		got = append(got, p.Field+" "+p.Kind)
	}
	if want := []string{"group_by[1] duplicate", "routes[0].group_by[2] duplicate"}; !reflect.DeepEqual(got, want) {
		t.Errorf("GroupByInvalidForTenant = %v, want %v", got, want)
	}
	if len(GroupByInvalidForTenant("demo", routing)) != 0 || routing["group_by"].([]any)[1] != "{{tenant}}" {
		t.Error("another tenant id is judged, or routing was modified")
	}
}
