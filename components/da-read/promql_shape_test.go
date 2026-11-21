package main

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/prometheus/prometheus/promql/parser"
)

func shapeFor(t *testing.T, expr string) map[string]any {
	t.Helper()
	out, err := readPromQLShape([]string{expr})
	if err != nil {
		t.Fatalf("%s: %v", expr, err)
	}
	var doc struct {
		Documents [][]map[string]any `json:"documents"`
	}
	if err := json.Unmarshal(out, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Documents) != 1 || len(doc.Documents[0]) != 1 {
		t.Fatalf("%s: %s", expr, out)
	}
	return doc.Documents[0][0]
}

func strList(t *testing.T, v any) []string {
	t.Helper()
	raw, ok := v.([]any)
	if !ok {
		t.Fatalf("not an array: %#v", v)
	}
	out := make([]string, len(raw))
	for i, x := range raw {
		out[i] = x.(string)
	}
	return out
}

// `by` and `without` are told apart, and an empty grouping is an empty list
// in either form — "no labels" is an answer, not a missing key.
func TestPromQLShape_Aggregation(t *testing.T) {
	cases := []struct {
		expr    string
		without bool
		labels  string
	}{
		{`sum by (tenant, instance) (x)`, false, "tenant,instance"},
		{`sum(x) by (tenant)`, false, "tenant"},
		{`sum without (pod) (x)`, true, "pod"},
		{`sum(x)`, false, ""},
		{`count without () (x)`, true, ""},
		{`topk by (a) (3, x)`, false, "a"},
	}
	for _, c := range cases {
		n := shapeFor(t, c.expr)
		if n["kind"] != shapeAggregate {
			t.Fatalf("%s: kind %v", c.expr, n["kind"])
		}
		if n["without"] != c.without {
			t.Errorf("%s: without = %v", c.expr, n["without"])
		}
		if got := strings.Join(strList(t, n["labels"]), ","); got != c.labels {
			t.Errorf("%s: labels = %q, want %q", c.expr, got, c.labels)
		}
		if _, has := n["param"]; !has {
			t.Errorf("%s: no param key", c.expr)
		}
	}
	if p := shapeFor(t, `topk(3, x)`)["param"].(map[string]any); p["kind"] != shapeNumber {
		t.Errorf("topk param: %v", p)
	}
	if shapeFor(t, `sum(x)`)["param"] != nil {
		t.Error("sum has a param")
	}
}

// The matching of a binary operator is Prometheus' own reading: `on` vs
// `ignoring`, the cardinality group_left / group_right give it, the labels
// they carry — and null where one side is a scalar.
func TestPromQLShape_VectorMatching(t *testing.T) {
	cases := []struct {
		expr                  string
		card                  string
		on                    bool
		labels, include, side string
	}{
		{`a > on(tenant) group_left b`, "many-to-one", true, "tenant", "", ""},
		{`a * on(tenant) group_right(sev, x) b`, "one-to-many", true, "tenant", "sev,x", ""},
		{`a unless on(tenant, instance) b`, "many-to-many", true, "tenant,instance", "", ""},
		{`a and ignoring(pod) b`, "many-to-many", false, "pod", "", ""},
		{`a / b`, "one-to-one", false, "", "", ""},
		{`a or on() b`, "many-to-many", true, "", "", ""},
	}
	for _, c := range cases {
		n := shapeFor(t, c.expr)
		m, ok := n["matching"].(map[string]any)
		if !ok {
			t.Fatalf("%s: matching = %#v", c.expr, n["matching"])
		}
		if m["card"] != c.card || m["on"] != c.on {
			t.Errorf("%s: card %v on %v", c.expr, m["card"], m["on"])
		}
		if got := strings.Join(strList(t, m["labels"]), ","); got != c.labels {
			t.Errorf("%s: labels %q", c.expr, got)
		}
		if got := strings.Join(strList(t, m["include"]), ","); got != c.include {
			t.Errorf("%s: include %q", c.expr, got)
		}
	}
	for _, e := range []string{`a > 5`, `5 < a`, `a == bool 1`} {
		n := shapeFor(t, e)
		if v, has := n["matching"]; !has || v != nil {
			t.Errorf("%s: matching = %#v, want null", e, v)
		}
	}
	if shapeFor(t, `a == bool 1`)["bool"] != true || shapeFor(t, `a == 1`)["bool"] != false {
		t.Error("bool modifier not read")
	}
}

// Operands hang off their operator, so "which selectors feed the left side"
// is a walk and not a guess from byte offsets; a `by` inside a label value
// is not a grouping.
func TestPromQLShape_OperandsAndSelectors(t *testing.T) {
	n := shapeFor(t, `sum by (tenant) (rate(x_total{q="by (pod)"}[5m])) / on(tenant) (y offset 1h)`)
	lhs := n["lhs"].(map[string]any)
	call := lhs["expr"].(map[string]any)
	if call["kind"] != shapeCall || call["name"] != "rate" {
		t.Fatalf("lhs.expr = %v", call)
	}
	sel := call["args"].([]any)[0].(map[string]any)
	if sel["kind"] != shapeSelector || sel["name"] != "x_total" || sel["range"] != true {
		t.Fatalf("selector = %v", sel)
	}
	found := false
	for _, m := range sel["matchers"].([]any) {
		mm := m.(map[string]any)
		if mm["name"] == "q" && mm["type"] == "=" && mm["value"] == "by (pod)" {
			found = true
		}
	}
	if !found {
		t.Errorf("matcher q not read: %v", sel["matchers"])
	}
	rhs := n["rhs"].(map[string]any)
	if rhs["kind"] != shapeParen {
		t.Fatalf("rhs = %v", rhs)
	}
	inner := rhs["expr"].(map[string]any)
	if inner["kind"] != shapeSelector || inner["name"] != "y" || inner["range"] != false {
		t.Errorf("rhs selector = %v", inner)
	}
	nameless := shapeFor(t, `{__name__=~"a|b", tenant="t"}`)
	if nameless["name"] != "" {
		t.Errorf("nameless selector name = %v", nameless["name"])
	}
	for _, e := range []string{`x[5m:1m]`, `-x`, `"s"`, `1`} {
		if shapeFor(t, e)["kind"] == nil {
			t.Errorf("%s: no kind", e)
		}
	}
}

// Every node type the parser can build is printed or refused by name: a new
// one in a later Prometheus must not fall through as a hole in the tree.
func TestPromQLShape_UnknownNodeIsARefusal(t *testing.T) {
	if _, err := shapeOf(&parser.StepInvariantExpr{Expr: &parser.NumberLiteral{}}); err == nil ||
		!strings.Contains(err.Error(), "does not print") {
		t.Errorf("err = %v", err)
	}
	if _, err := readPromQLShape([]string{`sum by (`}); err == nil {
		t.Error("an unparsable expression was read")
	}
}

// start / end are byte offsets of the node in the expression as written.
func TestPromQLShape_Offsets(t *testing.T) {
	expr := `sum by (a) (x) > on(a) y`
	n := shapeFor(t, expr)
	lhs := n["lhs"].(map[string]any)
	if got := expr[int(lhs["start"].(float64)):int(lhs["end"].(float64))]; got != `sum by (a) (x)` {
		t.Errorf("lhs spans %q", got)
	}
	rhs := n["rhs"].(map[string]any)
	if got := expr[int(rhs["start"].(float64)):int(rhs["end"].(float64))]; got != `y` {
		t.Errorf("rhs spans %q", got)
	}
}
