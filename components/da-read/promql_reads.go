package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/promql"
	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read promql-reads -`: what each PromQL expression reads, as Prometheus
// evaluates it in a rule (#4080) — the width of the time window each vector
// selector reads, and the functions and aggregations it calls. A check that
// spells these out as text (`\[(\d+[smhd])\]`, `\bfunc\s*\(`) is one spelling
// behind: it does not see `[1w]`, `[1h30m]`, `[(30d)]`, a subquery or
// `topk by (a) (…)`, and it sees `[2h]` inside a label value.
//
// Standard input is a JSON array of expressions; stdout is
//
//	{"documents": [[{"selectors": [{"start", "end", "range_ms", "subqueries_ms": [...],
//	                                "window_ms", "window"}, …],
//	                 "calls": [{"name", "kind": "function"|"aggregation", "start", "end"}, …],
//	                 "problem": null or <why Prometheus cannot evaluate it>}, …]]}
//
// one entry per expression, in input order; start / end are byte offsets
// into the expression's UTF-8 encoding (of the matrix selector when the
// selector has a range, else of the selector). An expression Prometheus'
// parser refuses refuses the input (rc 2, the refusal document), as in
// promql-selectors; standard input that is not a JSON array of strings is
// the caller's error.
//
// The window is read off the expression as Prometheus prepares it for
// evaluation (promql.PreprocessExpr, as a rule's instant query does): a
// duration expression (`[(30d)]`, `[2h*100]`, `[1h*24:]`) is its value and
// `@ start()` / `@ end()` are timestamps. Its width is what
// getTimeRangesForSelector (promql/engine.go) reads, less what only shifts
// it or is the server's own setting:
//
//   - the selector's own range plus the range of every subquery around it;
//     a subquery with `@` drops the subqueries outside it (subqueryTimes),
//     and a selector with `@` reads at that time whatever surrounds it, so
//     only its own range;
//   - not `offset`, which moves the window without widening it;
//   - not the lookback delta (an instant selector's, or the one `anchored` /
//     `smoothed` add): the server's flag, not the expression's.
//
// promql_reads_test.go holds the width to promql.FindMinMaxTime on
// expressions with one selector, so a Prometheus that reads windows
// differently turns it red.
//
// An expression the parser takes but Prometheus cannot evaluate (a duration
// expression that is not positive, `rate(x[step()])` in a rule) is no
// refusal: its `problem` says why, and it has no selectors (its calls are
// read off the parse) — a rule with it fails every evaluation, which is an
// answer about it.
func readPromQLReads(exprs []string) ([]byte, error) {
	out := make([]exprReads, 0, len(exprs))
	for i, e := range exprs {
		r, err := promqlReads(e)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		out = append(out, r)
	}
	return json.Marshal(struct {
		Documents [][]exprReads `json:"documents"`
	}{Documents: [][]exprReads{out}})
}

type exprReads struct {
	Selectors []selectorWindow `json:"selectors"`
	Calls     []callHit        `json:"calls"`
	Problem   *string          `json:"problem"`
}

type selectorWindow struct {
	// Selector is how Prometheus prints the selector, its range and
	// modifiers with it (MatrixSelector / VectorSelector String, as
	// written: before `@ start()` / `end()` and duration expressions are
	// resolved) — one line, label values quoted, never the text as typed
	// (#4080: a multi-line selector would break a finding into lines).
	Selector     string  `json:"selector"`
	Start        int     `json:"start"`
	End          int     `json:"end"`
	RangeMs      int64   `json:"range_ms"`
	SubqueriesMs []int64 `json:"subqueries_ms"`
	WindowMs     int64   `json:"window_ms"`
	// Range, Subqueries and Window are the *Ms as Prometheus spells a
	// duration (model.Duration): the one spelling, for messages.
	Range      string   `json:"range"`
	Subqueries []string `json:"subqueries"`
	Window     string   `json:"window"`
}

type callHit struct {
	Name  string `json:"name"`
	Kind  string `json:"kind"`
	Start int    `json:"start"`
	End   int    `json:"end"`
}

// The kinds of callHit.
const (
	callFunction    = "function"
	callAggregation = "aggregation"
)

// promqlReads is one expression's entry.
func promqlReads(e string) (exprReads, error) {
	node, err := parseExpr(e)
	if err != nil {
		return exprReads{}, err
	}
	r := exprReads{Selectors: []selectorWindow{}, Calls: []callHit{}}
	// Calls and spellings before PreprocessExpr: it folds the query-context
	// functions of duration expressions and rewrites nodes in place.
	spelling := map[*parser.VectorSelector]string{}
	parser.Inspect(node, func(n parser.Node, path []parser.Node) error {
		switch x := n.(type) {
		case *parser.VectorSelector:
			spelling[x] = x.String()
			if len(path) > 0 {
				if ms, ok := path[len(path)-1].(*parser.MatrixSelector); ok {
					spelling[x] = ms.String()
				}
			}
		case *parser.Call:
			pr := x.PositionRange()
			r.Calls = append(r.Calls, callHit{x.Func.Name, callFunction, int(pr.Start), int(pr.End)})
		case *parser.AggregateExpr:
			pr := x.PositionRange()
			r.Calls = append(r.Calls, callHit{x.Op.String(), callAggregation, int(pr.Start), int(pr.End)})
		}
		return nil
	})
	prepared, err := preprocessExpr(node)
	if err != nil {
		var pe *panicError
		if errors.As(err, &pe) {
			return exprReads{}, err // the engine's panic on the input: a refusal
		}
		msg := err.Error()
		r.Problem = &msg
		return r, nil
	}
	parser.Inspect(prepared, func(n parser.Node, path []parser.Node) error {
		vs, ok := n.(*parser.VectorSelector)
		if !ok {
			return nil
		}
		w := selectorWindowOf(vs, path)
		w.Selector = spelling[vs]
		r.Selectors = append(r.Selectors, w)
		return nil
	})
	return r, nil
}

// evalTime is the time the expression is prepared for. Only `@ start()` /
// `@ end()` read it, and a selector at a fixed time reads its own range
// whatever the time is.
var evalTime = time.Unix(0, 0)

func preprocessExpr(node parser.Expr) (prepared parser.Expr, err error) {
	defer loaderPanic("Prometheus' PromQL engine", &err)
	return promql.PreprocessExpr(node, evalTime, evalTime, 0)
}

// selectorWindowOf is the window *vs* reads, *path* the nodes above it.
func selectorWindowOf(vs *parser.VectorSelector, path []parser.Node) selectorWindow {
	var own time.Duration
	pr := vs.PositionRange()
	if len(path) > 0 {
		if ms, ok := path[len(path)-1].(*parser.MatrixSelector); ok {
			own = ms.Range
			pr = ms.PositionRange()
		}
	}
	subqueries := []int64{}
	if vs.Timestamp == nil {
		// subqueryTimes: a subquery with `@` drops the ones outside it.
		for _, p := range path {
			sq, ok := p.(*parser.SubqueryExpr)
			if !ok {
				continue
			}
			if sq.Timestamp != nil {
				subqueries = subqueries[:0]
			}
			subqueries = append(subqueries, sq.Range.Milliseconds())
		}
	}
	width := own.Milliseconds()
	texts := make([]string, 0, len(subqueries))
	for _, s := range subqueries {
		width += s
		texts = append(texts, spelled(s))
	}
	return selectorWindow{
		Start: int(pr.Start), End: int(pr.End),
		RangeMs: own.Milliseconds(), SubqueriesMs: subqueries, WindowMs: width,
		Range: spelled(own.Milliseconds()), Subqueries: texts, Window: spelled(width),
	}
}

// spelled is *ms* milliseconds as Prometheus spells a duration.
func spelled(ms int64) string {
	return model.Duration(time.Duration(ms) * time.Millisecond).String()
}
