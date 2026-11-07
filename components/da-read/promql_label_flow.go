package main

import (
	"encoding/json"
	"fmt"
	"slices"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read promql-label-flow <label>`: for every `and` / `unless` of each
// PromQL expression that matches `on(… <label> …)`, whether the series its
// LEFT side yields can carry <label> — judged on Prometheus' own parse tree
// (#3678 F2). `x unless on(tenant) (m == 1)` removes a left series only when
// some right series has the same tenant; a left side that has already lost
// `tenant` (`sum without(tenant) (…)`, `sum(…)`, `vector(1)`) never matches,
// and the clause is there only in its spelling.
//
// Standard input is
//
//	{"expressions": [<expr>, …], "records": {<record name>: [<its expr>, …]}}
//
// "records" are the recording rules of the same rule files: a selector that
// names one is judged by that rule's expression (every definition of it),
// so a record that aggregated the label away cannot hide it from the
// expression reading it. stdout is
//
//	{"documents": [[[{"op": "and"|"unless", "start": <byte offset>,
//	                  "end": <byte offset, exclusive>,
//	                  "lhs": "kept"|"dropped"|"unknown"}, …], …]]}
//
// one list per expression, in input order. "kept": EVERY series the left
// side yields carries the label, with the value of a series it was computed
// from (the series a selector of a raw metric reads are taken to carry it).
// "dropped": some series it yields lack it. "unknown": this reading cannot
// tell (a function that writes the label, one it does not model, a record
// cycle) — a caller that needs a proof refuses it.
func readPromQLLabelFlow(label string, data []byte) ([]byte, error) {
	if label == "" {
		return nil, fmt.Errorf("label is empty")
	}
	var in struct {
		Expressions []string            `json:"expressions"`
		Records     map[string][]string `json:"records"`
	}
	if err := json.Unmarshal(data, &in); err != nil || in.Expressions == nil {
		return nil, fmt.Errorf(`standard input is not {"expressions": [...], "records": {...}}: %v`, err)
	}
	f := &flowReader{label: label, records: map[string][]parser.Expr{}, visiting: map[string]bool{}}
	for name, exprs := range in.Records {
		for _, e := range exprs {
			node, err := ruleParser.ParseExpr(e)
			if err != nil {
				return nil, fmt.Errorf("record %s: %w", name, err)
			}
			f.records[name] = append(f.records[name], node)
		}
	}
	out := make([][]setMatch, 0, len(in.Expressions))
	for i, e := range in.Expressions {
		node, err := ruleParser.ParseExpr(e)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		out = append(out, f.setMatchesOn(node))
	}
	return json.Marshal(struct {
		Documents [][][]setMatch `json:"documents"`
	}{Documents: [][][]setMatch{out}})
}

// flowReader judges label flow for one label, resolving recording rules.
type flowReader struct {
	label    string
	records  map[string][]parser.Expr
	visiting map[string]bool
}

type setMatch struct {
	Op    string `json:"op"`
	Start int    `json:"start"`
	End   int    `json:"end"`
	LHS   string `json:"lhs"`
}

// flow is what a node's output says about one label.
type flow int

const (
	flowDropped flow = iota
	flowKept
	flowUnknown
)

func (f flow) String() string {
	switch f {
	case flowKept:
		return "kept"
	case flowDropped:
		return "dropped"
	default:
		return "unknown"
	}
}

func (f *flowReader) setMatchesOn(node parser.Node) []setMatch {
	label := f.label
	hits := []setMatch{}
	parser.Inspect(node, func(n parser.Node, _ []parser.Node) error {
		b, ok := n.(*parser.BinaryExpr)
		if !ok || (b.Op != parser.LAND && b.Op != parser.LUNLESS) || b.VectorMatching == nil ||
			!b.VectorMatching.On || !slices.Contains(b.VectorMatching.MatchingLabels, label) {
			return nil
		}
		pr := b.PositionRange()
		hits = append(hits, setMatch{Op: b.Op.String(), Start: int(pr.Start), End: int(pr.End),
			LHS: f.carries(b.LHS).String()})
		return nil
	})
	return hits
}

// carries: can the series *e* yields carry *label*? The rules are
// Prometheus' (promql/engine.go resultMetric, aggregations, functions); what
// is not modelled answers flowUnknown, never a guess.
func (f *flowReader) carries(e parser.Expr) flow {
	switch n := e.(type) {
	case *parser.ParenExpr:
		return f.carries(n.Expr)
	case *parser.StepInvariantExpr:
		return f.carries(n.Expr)
	case *parser.UnaryExpr:
		return f.carries(n.Expr)
	case *parser.VectorSelector:
		return f.selectorCarries(n)
	case *parser.MatrixSelector:
		return f.carries(n.VectorSelector)
	case *parser.SubqueryExpr:
		return f.carries(n.Expr)
	case *parser.NumberLiteral, *parser.StringLiteral:
		return flowDropped
	case *parser.AggregateExpr:
		return f.aggregateCarries(n)
	case *parser.Call:
		return f.callCarries(n)
	case *parser.BinaryExpr:
		return f.binaryCarries(n)
	}
	return flowUnknown
}

func (f *flowReader) aggregateCarries(n *parser.AggregateExpr) flow {
	label := f.label
	switch n.Op {
	case parser.TOPK, parser.BOTTOMK, parser.LIMITK, parser.LIMIT_RATIO:
		// Select input series whole; the grouping only partitions them.
		return f.carries(n.Expr)
	case parser.COUNT_VALUES:
		// The label would hold the sample VALUE, not the input's label.
		if s, ok := n.Param.(*parser.StringLiteral); !ok || s.Val == label {
			return flowUnknown
		}
	}
	grouped := slices.Contains(n.Grouping, label)
	if n.Without {
		if grouped {
			return flowDropped
		}
		return f.carries(n.Expr)
	}
	if !grouped {
		return flowDropped
	}
	return f.carries(n.Expr)
}

// labelWriters set or remove their destination label (argument 1).
var labelWriters = map[string]bool{"label_replace": true, "label_join": true}

// noLabels are the functions whose result has no labels at all.
var noLabels = map[string]bool{"vector": true, "time": true, "pi": true, "scalar": true}

func (f *flowReader) callCarries(n *parser.Call) flow {
	label := f.label
	name := n.Func.Name
	if noLabels[name] || n.Func.ReturnType == parser.ValueTypeScalar {
		return flowDropped
	}
	if labelWriters[name] {
		dst, ok := n.Args[1].(*parser.StringLiteral)
		if !ok {
			return flowUnknown
		}
		if dst.Val != label {
			return f.carries(n.Args[0])
		}
		if src, whole := copiesWhole(n); whole {
			// The label becomes the source label's value, whole: it is
			// carried wherever the source is (kubernetes pack: tenant from
			// namespace).
			return f.forLabel(src).carries(n.Args[0])
		}
		return flowUnknown
	}
	if name == "absent" || name == "absent_over_time" {
		// The result's labels are the argument's equality matchers.
		var ms []*parser.VectorSelector
		parser.Inspect(n.Args[0], func(c parser.Node, _ []parser.Node) error {
			if vs, ok := c.(*parser.VectorSelector); ok {
				ms = append(ms, vs)
			}
			return nil
		})
		if len(ms) != 1 {
			return flowUnknown
		}
		for _, m := range ms[0].LabelMatchers {
			if m.Name == label && m.Type == labels.MatchEqual {
				return flowKept
			}
		}
		return flowDropped
	}
	// Every other function maps each input series to one output series with
	// its labels (the metric name aside): follow the vector argument.
	for i, t := range n.Func.ArgTypes {
		if i >= len(n.Args) {
			break
		}
		if t == parser.ValueTypeVector || t == parser.ValueTypeMatrix {
			return f.carries(n.Args[i])
		}
	}
	return flowUnknown
}

func (f *flowReader) binaryCarries(n *parser.BinaryExpr) flow {
	label := f.label
	lScalar := n.LHS.Type() == parser.ValueTypeScalar
	rScalar := n.RHS.Type() == parser.ValueTypeScalar
	switch {
	case lScalar && rScalar:
		return flowDropped
	case rScalar:
		return f.carries(n.LHS)
	case lScalar:
		return f.carries(n.RHS)
	}
	m := n.VectorMatching
	switch n.Op {
	case parser.LAND, parser.LUNLESS:
		return f.carries(n.LHS)
	case parser.LOR:
		// The union: every series needs the label, from either side.
		return both(f.carries(n.LHS), f.carries(n.RHS))
	}
	if m == nil {
		return flowUnknown
	}
	// resultMetric: the "many" side's labels; one-to-one keeps only the
	// `on` labels or drops the `ignoring` ones; group_x(include) copies the
	// included labels from the "one" side.
	many, one := n.LHS, n.RHS
	if m.Card == parser.CardOneToMany {
		many, one = n.RHS, n.LHS
	}
	if slices.Contains(m.Include, label) {
		return f.carries(one)
	}
	if m.Card == parser.CardOneToOne {
		if m.On != slices.Contains(m.MatchingLabels, label) {
			return flowDropped
		}
	}
	return f.carries(many)
}

// both: every series of a union keeps the label only if each part does.
func both(a, b flow) flow {
	switch {
	case a == flowDropped || b == flowDropped:
		return flowDropped
	case a == flowUnknown || b == flowUnknown:
		return flowUnknown
	}
	return flowKept
}

// selectorCarries: a selector of a recording rule yields what the rule's
// expression yields (every definition of it); one whose metric name is not
// an equality matcher could read a record unseen, so it proves nothing.
func (f *flowReader) selectorCarries(vs *parser.VectorSelector) flow {
	name := ""
	for _, m := range vs.LabelMatchers {
		if m.Name == model.MetricNameLabel {
			if m.Type != labels.MatchEqual {
				return f.anyRecordMatches(m)
			}
			name = m.Value
		}
	}
	if name == "" {
		return flowUnknown
	}
	defs, ok := f.records[name]
	if !ok {
		return flowKept
	}
	if f.visiting[name] {
		return flowUnknown
	}
	f.visiting[name] = true
	defer delete(f.visiting, name)
	out := flowKept
	for _, d := range defs {
		out = both(out, f.carries(d))
	}
	return out
}

func (f *flowReader) anyRecordMatches(m *labels.Matcher) flow {
	for name := range f.records {
		if m.Matches(name) {
			return flowUnknown
		}
	}
	return flowKept
}

// copiesWhole: `label_replace(v, dst, "$1", src, "(.*)")` (or `(.+)`, or
// `${1}`) sets dst to src's whole value. Anything else could empty it or
// leave it unset when the regex does not match.
func copiesWhole(n *parser.Call) (string, bool) {
	if n.Func.Name != "label_replace" || len(n.Args) != 5 {
		return "", false
	}
	lit := func(i int) (string, bool) {
		s, ok := n.Args[i].(*parser.StringLiteral)
		if !ok {
			return "", false
		}
		return s.Val, true
	}
	repl, ok1 := lit(2)
	src, ok2 := lit(3)
	re, ok3 := lit(4)
	if !ok1 || !ok2 || !ok3 || src == "" {
		return "", false
	}
	if (repl == "$1" || repl == "${1}") && (re == "(.*)" || re == "(.+)") {
		return src, true
	}
	return "", false
}

// forLabel: the same reader, judging another label (records and the cycle
// guard shared).
func (f *flowReader) forLabel(label string) *flowReader {
	return &flowReader{label: label, records: f.records, visiting: f.visiting}
}
