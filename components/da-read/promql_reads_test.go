package main

import (
	"bytes"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/prometheus/promql"
	"github.com/prometheus/prometheus/promql/parser"
)

// singleSelector are expressions with one vector selector, in every form
// that moves or widens the window it reads (#4080).
var singleSelector = []string{
	`x`,
	`rate(x[2h])`,
	`rate(x[1w])`,
	`rate(x[1y])`,
	`rate(x[1h30m])`,
	`rate(x[7200000ms])`,
	`rate(x[ 2h ])`,
	`rate(x[(30d)])`,
	`rate(x[2h*100])`,
	`rate(x[5m] offset 3h)`,
	`rate(x[30m] offset -2h)`,
	`rate(x[5m] @ 100)`,
	`rate(x[5m] @ start())`,
	`increase(x[1h] anchored)`,
	`rate(x[1h] smoothed)`,
	`x smoothed`,
	`max_over_time(rate(x[5m])[3h:1m])`,
	`max_over_time(rate(x[5m])[3h:])`,
	`max_over_time(rate(x[5m])[30m:2h])`,
	`max_over_time(rate(x[5m])[1h*24:])`,
	`max_over_time(x[1h:])`,
	`max_over_time(max_over_time(rate(x[5m])[50m:1m])[50m:1m])`,
	`max_over_time(max_over_time(rate(x[5m])[50m:1m] offset 1d)[50m:1m] offset 1w)`,
	`max_over_time((x @ end())[30d:1m])`,
	`max_over_time(max_over_time(x[1h:1m] @ 100)[30d:1m])`,
	`max_over_time(max_over_time(rate(x[5m] @ 100)[1h:1m])[30d:1m])`,
	`sum by (a) (rate(x{a="[2h]"}[5m])) > 0`,
}

// TestWindowIsWhatPrometheusReads holds the width to Prometheus' own:
// promql.FindMinMaxTime of the expression as a rule's instant query
// prepares it, with the lookback delta at its 1ms minimum. ⛔ This is the
// test that turns red when Prometheus reads a window differently from
// selectorWindowOf (a new modifier, a changed subquery rule).
//
// What FindMinMaxTime counts and the width leaves out, by design: a range
// selector reads (start, end] — one millisecond less than its range from the
// start Prometheus computes; `anchored` / `smoothed` add the lookback delta
// (1ms here) at the start, `smoothed` at the end too. Offsets move start and
// end together, so they fall out of end − start.
func TestWindowIsWhatPrometheusReads(t *testing.T) {
	t.Parallel()
	for _, e := range singleSelector {
		r, err := promqlReads(e)
		if err != nil || r.Problem != nil {
			t.Fatalf("%s: %v %v", e, err, r.Problem)
		}
		if len(r.Selectors) != 1 {
			t.Fatalf("%s: %d selectors, want 1", e, len(r.Selectors))
		}
		node, err := parseExpr(e)
		if err != nil {
			t.Fatal(err)
		}
		// Prometheus' own preparation, not preprocessExpr: the oracle
		// shares no code with what it checks.
		prepared, err := promql.PreprocessExpr(node, evalTime, evalTime, 0)
		if err != nil {
			t.Fatal(err)
		}
		var vs *parser.VectorSelector
		var ranged bool
		parser.Inspect(prepared, func(n parser.Node, _ []parser.Node) error {
			switch x := n.(type) {
			case *parser.VectorSelector:
				vs = x
			case *parser.MatrixSelector:
				ranged = true
			}
			return nil
		})
		mint, maxt := promql.FindMinMaxTime(&parser.EvalStmt{
			Expr: prepared, Start: evalTime, End: evalTime, LookbackDelta: time.Millisecond,
		})
		want := maxt - mint
		switch {
		case vs.Smoothed:
			want -= 1 // the lookback delta at the end
		case ranged && !vs.Anchored:
			want += 1 // (start, end]
		}
		if got := r.Selectors[0].WindowMs; got != want {
			t.Errorf("%s: window %dms, Prometheus reads %dms", e, got, want)
		}
	}
}

// TestWindowsOfTheSpellingsATextCheckMisses pins the widths #4080 measured
// a regex not to see, and that what only moves the window is not counted.
func TestWindowsOfTheSpellingsATextCheckMisses(t *testing.T) {
	t.Parallel()
	cases := []struct {
		expr, window string
		subqueries   []int64
	}{
		{`rate(x[1w])`, "1w", []int64{}},
		{`rate(x[1h30m])`, "1h30m", []int64{}},
		{`rate(x[(30d)])`, "30d", []int64{}},
		{`rate(x[2h*100])`, "8d8h", []int64{}},
		{`max_over_time(rate(x[5m])[3h:1m])`, "3h5m", []int64{3 * 3600000}},
		{`max_over_time(rate(x[5m])[1h*24:])`, "1d5m", []int64{24 * 3600000}},
		{`max_over_time(max_over_time(rate(x[5m])[50m:1m])[50m:1m])`, "1h45m", []int64{3000000, 3000000}},
		{`rate(x[5m] offset 1w)`, "5m", []int64{}},
		{`max_over_time((x @ end())[30d:1m])`, "0s", []int64{}},
		{`max_over_time(max_over_time(x[1h:1m] @ 100)[30d:1m])`, "1h", []int64{3600000}},
		{`increase(x[1h] anchored)`, "1h", []int64{}},
	}
	for _, c := range cases {
		r, err := promqlReads(c.expr)
		if err != nil || len(r.Selectors) != 1 {
			t.Fatalf("%s: %v %+v", c.expr, err, r)
		}
		s := r.Selectors[0]
		if s.Window != c.window || !slices.Equal(s.SubqueriesMs, c.subqueries) {
			t.Errorf("%s: window %s, subqueries %v; want %s, %v", c.expr, s.Window, s.SubqueriesMs, c.window, c.subqueries)
		}
	}
}

// TestSelectorSpanIsTheTextItReads: start/end name the selector with its
// range, so a caller quotes `x[2h]`, never a label value that looks like one.
func TestSelectorSpanIsTheTextItReads(t *testing.T) {
	t.Parallel()
	e := `sum(rate(x{a="[2h]"}[5m])) + y`
	r, err := promqlReads(e)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, s := range r.Selectors {
		got = append(got, e[s.Start:s.End]+"="+s.Window)
	}
	if want := []string{`x{a="[2h]"}[5m]=5m`, `y=0s`}; !slices.Equal(got, want) {
		t.Fatalf("selectors %v, want %v", got, want)
	}
}

// TestCallsAreFunctionsAndAggregationsByName: an aggregation is read the
// same whether its grouping is before or after it (`topk by (a) (…)`), and a
// name in a label value, a metric name or a grouping label is no call.
func TestCallsAreFunctionsAndAggregationsByName(t *testing.T) {
	t.Parallel()
	cases := []struct {
		expr  string
		calls []string
	}{
		{`topk(3, x)`, []string{"aggregation:topk"}},
		{`topk by (a) (3, x)`, []string{"aggregation:topk"}},
		{`sum(x) without (a) and quantile by (a) (0.9, y)`, []string{"aggregation:sum", "aggregation:quantile"}},
		{"predict_linear\n(x[5m], 60)", []string{"function:predict_linear"}},
		{`x{a="predict_linear("} # predict_linear(`, nil},
		{`predict_linear_total + sum by (predict_linear) (y)`, []string{"aggregation:sum"}},
		{`rate(x[step()])`, []string{"function:rate"}},
	}
	for _, c := range cases {
		r, err := promqlReads(c.expr)
		if err != nil {
			t.Fatalf("%s: %v", c.expr, err)
		}
		var got []string
		for _, h := range r.Calls {
			got = append(got, h.Kind+":"+h.Name)
		}
		if !slices.Equal(got, c.calls) {
			t.Errorf("%s: calls %v, want %v", c.expr, got, c.calls)
		}
	}
}

// TestAnExpressionPrometheusCannotEvaluateIsAProblemNotARefusal: the parser
// takes `[step()]`, the rule fails every evaluation (a duration that is not
// positive). That is an answer about the expression, not a refused input.
func TestAnExpressionPrometheusCannotEvaluateIsAProblemNotARefusal(t *testing.T) {
	t.Parallel()
	var out, errOut bytes.Buffer
	rc := run([]string{"promql-reads", "-"}, strings.NewReader(`["rate(x[step()])", "up"]`), &out, &errOut)
	if rc != exitOK {
		t.Fatalf("rc %d, stderr %s", rc, errOut.String())
	}
	var doc struct {
		Documents [][]exprReads `json:"documents"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	got := doc.Documents[0]
	if len(got) != 2 || got[0].Problem == nil || !strings.Contains(*got[0].Problem, "greater than 0") ||
		len(got[0].Selectors) != 0 || got[1].Problem != nil || len(got[1].Selectors) != 1 {
		t.Fatalf("answer %s", out.String())
	}
}

// TestAnExpressionTheParserRefusesRefusesTheInput: as promql-selectors.
func TestAnExpressionTheParserRefusesRefusesTheInput(t *testing.T) {
	t.Parallel()
	var out, errOut bytes.Buffer
	rc := run([]string{"promql-reads", "-"}, strings.NewReader(`["up", "rate(x[5m]"]`), &out, &errOut)
	if rc != exitCallerErr || strings.TrimSpace(out.String()) != `{"answer":"refused"}` ||
		!strings.Contains(errOut.String(), "expression 1") {
		t.Fatalf("rc %d, stdout %q, stderr %q", rc, out.String(), errOut.String())
	}
}

// TestPromQLReadsFramingIsTheCallers: not a JSON array of strings, or an
// operand it does not take, is a usage error, no refusal document.
func TestPromQLReadsFramingIsTheCallers(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		args  []string
		stdin string
	}{
		{[]string{"promql-reads", "-"}, `null`},
		{[]string{"promql-reads", "-"}, `[null]`},
		{[]string{"promql-reads", "-"}, `{"expressions": ["up"]}`},
		{[]string{"promql-reads", "file.json"}, `["up"]`},
		{[]string{"promql-reads", "-", "-"}, `["up"]`},
	} {
		var out, errOut bytes.Buffer
		if rc := run(c.args, strings.NewReader(c.stdin), &out, &errOut); rc != exitCallerErr || out.Len() != 0 {
			t.Errorf("%v %s: rc %d, stdout %q", c.args, c.stdin, rc, out.String())
		}
	}
}
