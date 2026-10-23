package config

import (
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func caSpec(t *testing.T, y string) CustomAlertSpec {
	t.Helper()
	var specs []CustomAlertSpec
	if err := yaml.Unmarshal([]byte(y), &specs); err != nil {
		t.Fatal(err)
	}
	return specs[0]
}

func caEntry(t *testing.T, status string, sevs []string, y string) CustomAlertEntry {
	t.Helper()
	spec := caSpec(t, y)
	id, err := RecipeID(spec)
	if err != nil && status != CustomAlertRejected {
		t.Fatal(err)
	}
	return CustomAlertEntry{Spec: spec, Status: status, RecipeID: id, Severities: sevs}
}

func packRules(t *testing.T, p CustomAlertPack) map[string]orderedMap {
	t.Helper()
	out := map[string]orderedMap{}
	for _, g := range p.Groups {
		for _, r := range g.get("rules").([]any) {
			rule := r.(orderedMap)
			id := rule.get("record")
			if id == nil {
				id = rule.get("alert")
			}
			key := id.(string)
			if l, ok := rule.get("labels").(orderedMap); ok && rule.get("alert") != nil {
				key += "/" + l.get("severity").(string)
			}
			if rule.get("record") == "custom_recipe_info" {
				key += "/" + rule.get("labels").(orderedMap).get("recipe_id").(string)
			}
			out[key] = rule
		}
	}
	return out
}

const thrSpec = `- {recipe: threshold, name: q, metric: qd, op: ">", window: 5m, threshold: "1"}`

// Served and switched-off entries compile; rejected and duplicate ones do not.
// A shape's severities are the union over every entry, in sorted order.
func TestCompileCustomAlertPack_CompilesServedAndDisabledOnly(t *testing.T) {
	served := caEntry(t, CustomAlertServed, []string{"warning"}, thrSpec)
	off := caEntry(t, CustomAlertDisabled, []string{"critical"}, thrSpec)
	rejected := caEntry(t, CustomAlertRejected, []string{"warning"},
		`- {recipe: rate, name: r, metric: rr, window: 5m, threshold: "1"}`)
	dup := caEntry(t, CustomAlertDuplicated, []string{"warning"},
		`- {recipe: rate, name: r2, metric: rd, window: 5m, threshold: "1"}`)
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{
		{Tenant: "a", Entries: []CustomAlertEntry{served, rejected}},
		{Tenant: "b", Entries: []CustomAlertEntry{off, dup}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.Shapes != 1 {
		t.Fatalf("shapes = %d, want 1 (rejected and duplicate entries compile nothing)", p.Shapes)
	}
	rules := packRules(t, p)
	for _, want := range []string{
		"custom:" + served.RecipeID + ":critical:core", "custom:" + served.RecipeID + ":warning:core",
		"Custom_" + served.RecipeID + "/critical", "Custom_" + served.RecipeID + "/warning",
	} {
		if _, ok := rules[want]; !ok {
			t.Errorf("missing %s; have %v", want, keys(rules))
		}
	}
	// critical sorts before warning.
	var order []string
	for _, g := range p.Groups {
		if g.get("name") == "custom-alerts-normalization" {
			for _, r := range g.get("rules").([]any) {
				order = append(order, r.(orderedMap).get("record").(string))
			}
		}
	}
	if !strings.HasSuffix(order[len(order)-2], ":critical:core") || !strings.HasSuffix(order[len(order)-1], ":warning:core") {
		t.Errorf("core records not in severity order: %v", order)
	}
}

func keys(m map[string]orderedMap) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}

// Two unequal shapes on one recipe_id would emit one shape's selectors for
// both: refused.
func TestCompileCustomAlertPack_RefusesARecipeIDTwoShapesShare(t *testing.T) {
	a := caEntry(t, CustomAlertServed, []string{"warning"}, thrSpec)
	b := caEntry(t, CustomAlertServed, []string{"warning"},
		`- {recipe: threshold, name: q, metric: qd, op: ">", window: 10m, threshold: "1"}`)
	b.RecipeID = a.RecipeID // a forced collision
	_, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "a", Entries: []CustomAlertEntry{a}}, {Tenant: "b", Entries: []CustomAlertEntry{b}}})
	if err == nil || !strings.Contains(err.Error(), "collides between two distinct shapes") {
		t.Fatalf("err = %v, want a collision", err)
	}
	// Equal shapes on one id are one rule, not a collision.
	c := caEntry(t, CustomAlertServed, []string{"critical"}, thrSpec)
	if _, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "a", Entries: []CustomAlertEntry{a}}, {Tenant: "c", Entries: []CustomAlertEntry{c}}}); err != nil {
		t.Fatalf("equal shapes refused: %v", err)
	}
}

// An absence entry's op is in neither its slug nor its rules: two entries on
// one recipe_id that differ only in a stray `op:` are one shape, not a
// collision that would refuse the whole tree.
func TestCompileCustomAlertPack_AbsenceOpIsNotPartOfTheShape(t *testing.T) {
	a := caEntry(t, CustomAlertServed, []string{"warning"},
		`- {recipe: absence, name: hb, metric: hb_total, window: 5m, threshold: "1"}`)
	b := caEntry(t, CustomAlertServed, []string{"warning"},
		`- {recipe: absence, name: hb, metric: hb_total, window: 5m, threshold: "1", op: "<"}`)
	if a.RecipeID != b.RecipeID {
		t.Fatalf("precondition: one recipe_id, got %s / %s", a.RecipeID, b.RecipeID)
	}
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "a", Entries: []CustomAlertEntry{a}}, {Tenant: "b", Entries: []CustomAlertEntry{b}}})
	if err != nil || p.Shapes != 1 {
		t.Fatalf("shapes = %d, err = %v; want one shape", p.Shapes, err)
	}
}

func TestCompileCustomAlertPack_EmptyHasNoGroups(t *testing.T) {
	p, err := CompileCustomAlertPack(nil)
	if err != nil || len(p.Groups) != 0 || p.Shapes != 0 {
		t.Fatalf("p = %+v, err = %v", p, err)
	}
}

// The emit-time invariant: a template action or backtick the platform did
// not write fails the compile; the platform's own pass.
func TestAssertTemplateSafe(t *testing.T) {
	group := func(v string) []orderedMap {
		return []orderedMap{{{"name", "g"}, {"rules", []any{orderedMap{
			{"alert", "A"}, {"annotations", orderedMap{{"description", v}}}}}}}}
	}
	for _, ok := range []string{"{{ $labels.tenant }}", `value {{ $value | printf "%.2f" }}`, "plain"} {
		if err := assertTemplateSafe(group(ok)); err != nil {
			t.Errorf("%q refused: %v", ok, err)
		}
	}
	// Each of the three markers alone fails it: an opening action, a closing
	// one, a backtick.
	for _, bad := range []string{`{{ query "up" }}`, "{{ x", "x }}", "a `b`", "{{ $labels.x }} }}", `{{ $value | printf "%d" }}`} {
		if err := assertTemplateSafe(group(bad)); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

// Selector values are escaped for a PromQL double-quoted string: backslash
// first, then the quote, then newlines.
func TestSelectorEscaping(t *testing.T) {
	sh, err := recipeShape(caSpec(t, `- {recipe: threshold, name: q, metric: m, window: 5m, threshold: "1", `+
		`selectors: {p: 'C:\d"x', n: "a\nb"}, selectors_re: {s: 'api-.*'}}`))
	if err != nil {
		t.Fatal(err)
	}
	if got, want := sh.selector(), `{n="a\nb", p="C:\\d\"x", s=~"api-.*"}`; got != want {
		t.Fatalf("selector = %s, want %s", got, want)
	}
}

// The pack's key order is the order it has always had, and `>` is not
// HTML-escaped by the map itself (an encoder with SetEscapeHTML(false) keeps
// it so; json.Marshal escapes any Marshaler's output again).
func TestOrderedMapMarshal(t *testing.T) {
	b, err := orderedMap{{"record", "r"}, {"expr", "a > b"}, {"labels", orderedMap{{"z", "1"}, {"a", "2"}}}}.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"record":"r","expr":"a > b","labels":{"z":"1","a":"2"}}`; string(b) != want {
		t.Fatalf("got %s, want %s", b, want)
	}
}

// recipeShape and RecipeID agree, and a shape's ID is its recipe_id.
func TestRecipeShapeIDIsRecipeID(t *testing.T) {
	for _, y := range []string{
		thrSpec,
		`- {recipe: forecast, name: f, metric: avail, horizon: 4h, capacity_metric: cap, threshold: "0.1", group_by: [persistentvolumeclaim]}`,
		`- {recipe: slo_burn_rate, name: s, metric: err, denominator_metric: req, objective: "99.9", min_events: 20}`,
		`- {recipe: p99_latency, name: p, metric: lat, window: 5m, quantile: ".95", threshold: "1", selectors: {svc: a}}`,
	} {
		spec := caSpec(t, y)
		id, err := RecipeID(spec)
		if err != nil {
			t.Fatal(err)
		}
		sh, _ := recipeShape(spec)
		if sh.ID() != id {
			t.Errorf("%s: shape ID %s != RecipeID %s", y, sh.ID(), id)
		}
	}
}
