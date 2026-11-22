package main

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read promql-label-flow <label>`: whether the series each PromQL
// expression yields carry <label> (#4098: the custom-rule lint's
// preserved_labels), and for every `and` / `unless` of it that matches
// `on(… <label> …)`, whether the series its LEFT side yields can carry
// <label> — judged on Prometheus' own parse tree (#3678 F2). `x unless on(tenant) (m == 1)` removes a left series only when
// some right series has the same tenant; a left side that has already lost
// `tenant` (`sum without(tenant) (…)`, `sum(…)`, `vector(1)`) never matches,
// and the clause is there only in its spelling.
//
// Standard input is
//
//	{"expressions": [<expr>, …],
//	 "records": {<record name>: [{"expr": <its expr>, "labels": {…}}, …]},
//	 "aliases": [<label whose whole value is <label>'s value>, …],
//	 "metrics": {<raw metric name>: "without"|"by-job"|"unknown"},
//	 "jobs": [<scrape job every target of which carries <label>>, …]}
//
// "records" are the recording rules of the same rule files: a selector that
// names one is judged by that rule's expression (every definition of it)
// and its own `labels:` (which set, or with "", remove the label), so a
// record cannot hide a dropped label from the expression reading it.
// "aliases" name the labels a `label_replace` may copy into <label> whole
// (tenant from namespace in the kubernetes pack); a copy from any other
// label proves nothing — it would hold another label's values.
//
// "metrics" and "jobs" are what the scrape says about <label> (#4137): a raw
// metric named in "metrics" is "without" (no series of it carries <label>),
// "by-job" (series Prometheus writes per target — `up`, `scrape_*`, the
// client library's own families — carry its target's labels: <label> when
// the selector's `job` is an equality to one of "jobs", none when it names
// another job or no job, unknown when `job` is a regex or a negation) or
// "unknown". A raw metric not named is taken to carry <label> (the scrape
// contract: the tenant exporters' job writes it on every series). Whatever
// the metric, a selector whose own matcher on <label> does not match the
// empty string (`tenant="db-a"`, `tenant=~".+"`, `tenant!=""`) selects only
// series that carry it. stdout is
//
//	{"documents": [[{"result": "kept"|"dropped"|"unknown",
//	                 "set_matches": [{"op": "and"|"unless", "start": <byte offset>,
//	                                  "end": <byte offset, exclusive>,
//	                                  "lhs": "kept"|"dropped"|"unknown"}, …]}, …]]}
//
// one entry per expression, in input order: "result" is the whole
// expression's verdict, "lhs" each clause's left side's. "kept": EVERY
// series it yields carries the label, with the value of a series it was
// computed from. "dropped": some series it yields lack it. "unknown": this
// reading cannot tell (a function that writes the label, one it does not
// model, a value copied from a series matched on other labels, a record
// cycle, a record named but not given) — a caller that needs a proof
// refuses it.
//
// ⛔ What it cannot see (#4098): whether a raw metric carries the label at
// all is the scrape's business, not the expression's — it knows only what
// "metrics" and "jobs" tell it, and a raw metric they do not name is TAKEN to
// carry it. A metric name with a colon is a recording
// rule's (Prometheus' naming rule): one not among "records" is "unknown",
// never taken for a raw metric.
//
// An expression or record the parser refuses refuses the input (rc 2, the
// refusal document); an empty <label> or standard input that is not the
// object above is the caller's error (labelFlowRequestOf).
func readPromQLLabelFlow(label string, in *labelFlowRequest) ([]byte, error) {
	f := &flowReader{label: label, records: map[string][]recordDef{}, visiting: map[string]bool{},
		aliases: map[string]bool{}}
	for _, a := range in.Aliases {
		f.aliases[a] = true
	}
	f.metrics = in.Metrics
	f.jobs = map[string]bool{}
	for _, j := range in.Jobs {
		f.jobs[j] = true
	}
	for name, defs := range in.Records {
		for _, d := range defs {
			node, err := parseExpr(d.Expr)
			if err != nil {
				return nil, fmt.Errorf("record %s: %w", name, err)
			}
			f.records[name] = append(f.records[name], recordDef{expr: node, labels: d.Labels})
		}
	}
	out := make([]exprFlow, 0, len(in.Expressions))
	for i, e := range in.Expressions {
		node, err := parseExpr(e)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		out = append(out, exprFlow{Result: f.carries(node).String(), SetMatches: f.setMatchesOn(node)})
	}
	return json.Marshal(struct {
		Documents [][]exprFlow `json:"documents"`
	}{Documents: [][]exprFlow{out}})
}

// labelFlowRequest is promql-label-flow's standard input.
type labelFlowRequest struct {
	Expressions []string `json:"expressions"`
	Records     map[string][]struct {
		Expr   string            `json:"expr"`
		Labels map[string]string `json:"labels"`
	} `json:"records"`
	Aliases []string          `json:"aliases"`
	Metrics map[string]string `json:"metrics"`
	Jobs    []string          `json:"jobs"`
}

// The values of a promql-label-flow request's "metrics" (#4137).
const (
	metricWithout = "without"
	metricByJob   = "by-job"
	metricUnknown = "unknown"
)

// labelFlowRequestOf reads the caller's part of a promql-label-flow run: the
// <label> argument and the JSON framing of standard input. ⛔ Both are the
// caller's, not the input's (#3943): an error here is a caller error (exit
// 2, no refusal document), never an answer about the expressions.
func labelFlowRequestOf(label string, data []byte) (*labelFlowRequest, error) {
	if label == "" {
		return nil, fmt.Errorf("label is empty")
	}
	var in labelFlowRequest
	err := json.Unmarshal(data, &in)
	if err == nil {
		in.Expressions, err = labelFlowFraming(data)
	}
	if err != nil {
		return nil, fmt.Errorf(`standard input is not {"expressions": [...], "records": {...}, "aliases": [...], "metrics": {...}, "jobs": [...]}: %v`, err)
	}
	return &in, nil
}

// labelFlowFraming checks the strings the caller writes in a
// promql-label-flow request and returns "expressions" (jsonStrings). ⛔
// json.Unmarshal reads a null where a string goes as "" (#3943): a null
// "expr" of a record would reach the parser and refuse the input over the
// caller's framing, a null alias would pass unseen. So "expressions" must be
// an array of strings, every record's "expr" a string, and "aliases", when
// given, an array of strings, "metrics", when given, an object whose every
// value is one of "without" / "by-job" / "unknown", and "jobs", when given,
// an array of strings; anything else is the caller's error. A label
// value of a record's "labels" is left as json.Unmarshal reads it: null is
// "", which is what Prometheus reads `labels: {x: ~}` as (remove x).
func labelFlowFraming(data []byte) ([]string, error) {
	var framing struct {
		Expressions json.RawMessage `json:"expressions"`
		Records     map[string][]struct {
			Expr json.RawMessage `json:"expr"`
		} `json:"records"`
		Aliases json.RawMessage `json:"aliases"`
		Metrics json.RawMessage `json:"metrics"`
		Jobs    json.RawMessage `json:"jobs"`
	}
	if err := json.Unmarshal(data, &framing); err != nil {
		return nil, err
	}
	exprs, err := jsonStrings(framing.Expressions)
	if err != nil {
		return nil, fmt.Errorf("expressions: %w", err)
	}
	for name, defs := range framing.Records {
		for i, d := range defs {
			if _, err := jsonString(d.Expr); err != nil {
				return nil, fmt.Errorf("records[%q][%d].expr: %w", name, i, err)
			}
		}
	}
	if framing.Aliases != nil {
		if _, err := jsonStrings(framing.Aliases); err != nil {
			return nil, fmt.Errorf("aliases: %w", err)
		}
	}
	var metrics map[string]json.RawMessage
	if framing.Metrics != nil {
		// null decodes to a nil map: no facts, every raw metric taken to
		// carry the label — the caller's framing error, not an answer.
		if err := json.Unmarshal(framing.Metrics, &metrics); err != nil || metrics == nil {
			return nil, fmt.Errorf("metrics: not an object: %s", framing.Metrics)
		}
	}
	for name, raw := range metrics {
		v, err := jsonString(raw)
		if err == nil && v != metricWithout && v != metricByJob && v != metricUnknown {
			err = fmt.Errorf("%q is none of %q, %q, %q", v, metricWithout, metricByJob, metricUnknown)
		}
		if err != nil {
			return nil, fmt.Errorf("metrics[%q]: %w", name, err)
		}
	}
	if framing.Jobs != nil {
		if _, err := jsonStrings(framing.Jobs); err != nil {
			return nil, fmt.Errorf("jobs: %w", err)
		}
	}
	return exprs, nil
}

// flowReader judges label flow for one label, resolving recording rules.
type flowReader struct {
	label    string
	records  map[string][]recordDef
	visiting map[string]bool
	aliases  map[string]bool
	// metrics and jobs: what the scrape says about label (#4137). Only the
	// reader of the request's own label has them; one judging an alias
	// (forLabel) takes every raw metric to carry its label, as before.
	metrics map[string]string
	jobs    map[string]bool
}

// recordDef is one definition of a recording rule: its expression and the
// labels its `labels:` writes on every series.
type recordDef struct {
	expr   parser.Expr
	labels map[string]string
}

// exprFlow is one expression's entry.
type exprFlow struct {
	Result     string     `json:"result"`
	SetMatches []setMatch `json:"set_matches"`
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

// dropsLabel: functions that remove one label from what they otherwise pass
// through (the bucket boundary of a classic histogram).
var dropsLabel = map[string]string{"histogram_quantile": "le", "histogram_fraction": "le"}

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
		src, re, whole := copiesWhole(n)
		if !whole || !f.aliases[src] {
			// Not a whole copy of a label that IS this label's value: what it
			// writes is something else (job, instance…) — proves nothing.
			return flowUnknown
		}
		switch f.forLabel(src).carries(n.Args[0]) {
		case flowKept:
			return flowKept
		case flowDropped:
			if re == "(.+)" {
				// No source value: the regex does not match and the series
				// keeps the label it had.
				return f.carries(n.Args[0])
			}
			// `(.*)` matches the empty value and writes "" — the label is
			// removed.
			return flowDropped
		}
		return flowUnknown
	}
	if name == "absent" || name == "absent_over_time" {
		// The result's labels are the equality matchers of the argument when
		// it is a selector itself (promql createLabelsForAbsentFunction);
		// any other argument — an aggregation, a call — yields none.
		var vs *parser.VectorSelector
		switch a := n.Args[0].(type) {
		case *parser.VectorSelector:
			vs = a
		case *parser.MatrixSelector:
			vs, _ = a.VectorSelector.(*parser.VectorSelector)
		}
		if vs == nil {
			return flowDropped
		}
		// createLabelsForAbsentFunction: an equality matcher sets the label,
		// any other matcher of it — a second one, a negative one, before or
		// after — removes it (the engine, promql_label_flow_test.go), and an
		// empty value is no label (#4098 review). So: exactly one matcher of
		// the label, an equality with a value.
		var of []*labels.Matcher
		for _, m := range vs.LabelMatchers {
			if m.Name == label {
				of = append(of, m)
			}
		}
		if len(of) == 1 && of[0].Type == labels.MatchEqual && of[0].Value != "" {
			return flowKept
		}
		return flowDropped
	}
	if dropsLabel[name] == label {
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
		// The value is copied from the "one" side's series matched on the
		// matching labels: unless they decide the label, a series of
		// another tenant can be the match (#4098 review: `x * on()
		// group_left(tenant) y` rewrote t1's series to t2).
		if m.On != slices.Contains(m.MatchingLabels, label) {
			return flowUnknown
		}
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
// an equality matcher could read a record unseen — given or not (#4098
// review) — so it proves nothing.
func (f *flowReader) selectorCarries(vs *parser.VectorSelector) flow {
	name := ""
	for _, m := range vs.LabelMatchers {
		if m.Name == model.MetricNameLabel {
			if m.Type != labels.MatchEqual {
				return flowUnknown
			}
			name = m.Value
		}
	}
	if name == "" {
		return flowUnknown
	}
	defs, ok := f.records[name]
	if !ok {
		if strings.Contains(name, ":") {
			// A recording rule's name (Prometheus reserves the colon for
			// them) that the caller did not give: what it yields is unseen.
			return flowUnknown
		}
		return f.rawCarries(name, vs)
	}
	if f.visiting[name] {
		return flowUnknown
	}
	f.visiting[name] = true
	defer delete(f.visiting, name)
	out := flowKept
	for _, d := range defs {
		if v, set := d.labels[f.label]; set {
			// The rule's own labels overwrite (or with "", remove) it. A
			// recording rule's labels are no template (rules/recording.go):
			// any other value is the literal text on every series, not the
			// value of a series it read (#4098 review).
			if v == "" {
				out = both(out, flowDropped)
			} else {
				out = both(out, flowUnknown)
			}
			continue
		}
		out = both(out, f.carries(d.expr))
	}
	return out
}

// rawCarries: whether the series a selector of the raw metric *name* reads
// carry the label, by what the request says of it (#4137).
//
// The selector's own matchers on the label come first, whatever the metric
// (labels.Matcher: an absent label reads as ""): one that does not match ""
// selects only series that have it; `=""` / `=~""` select only series that
// do not. Only for a raw metric: a record's "kept" is a claim about where
// the value came from (a non-alias copy or a literal in its labels is not
// kept, #4098 review), which a non-empty matcher does not make (#4137 review).
func (f *flowReader) rawCarries(name string, vs *parser.VectorSelector) flow {
	for _, m := range vs.LabelMatchers {
		if m.Name == f.label && !m.Matches("") {
			return flowKept
		}
	}
	for _, m := range vs.LabelMatchers {
		if m.Name == f.label && m.Value == "" && (m.Type == labels.MatchEqual || m.Type == labels.MatchRegexp) {
			return flowDropped
		}
	}
	switch f.metrics[name] {
	case metricWithout:
		return flowDropped
	case metricUnknown:
		return flowUnknown
	case metricByJob:
		var job *labels.Matcher
		for _, m := range vs.LabelMatchers {
			if m.Name == model.JobLabel {
				if job != nil || m.Type != labels.MatchEqual {
					return flowUnknown
				}
				job = m
			}
		}
		if job != nil && f.jobs[job.Value] {
			return flowKept
		}
		// Another job, or every job: some target's series lack it.
		return flowDropped
	}
	return flowKept
}

// copiesWhole: `label_replace(v, dst, "$1", src, "(.*)")` (or `(.+)`, or
// `${1}`) sets dst to src's whole value. Anything else could empty it or
// leave it unset when the regex does not match.
func copiesWhole(n *parser.Call) (string, string, bool) {
	if n.Func.Name != "label_replace" || len(n.Args) != 5 {
		return "", "", false
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
		return "", "", false
	}
	if (repl == "$1" || repl == "${1}") && (re == "(.*)" || re == "(.+)") {
		return src, re, true
	}
	return "", "", false
}

// forLabel: the same reader, judging another label (records and the cycle
// guard shared).
func (f *flowReader) forLabel(label string) *flowReader {
	return &flowReader{label: label, records: f.records, visiting: f.visiting, aliases: f.aliases}
}
