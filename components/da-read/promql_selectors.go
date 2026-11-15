package main

import (
	"encoding/json"
	"fmt"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read promql-selectors <series>`: which vector selectors of each PromQL
// expression could select the series *series* — judged by Prometheus' own
// parser and label matchers, so a regex, a negative matcher, an escaped or
// single-quoted string, a `{__name__=~"…"}` selector or a selector with no
// metric name is read exactly as Prometheus reads it (#1200: a check that
// spells these out as text is always one spelling behind).
//
// *series* is a selector of equality matchers only, the series' identifying
// labels, e.g. `user_state_filter{filter="maintenance"}`. The series is taken
// to carry other labels too, of unknown value, so a matcher on a label
// *series* does not name could match it: `x{filter="maintenance",tenant="a"}`
// could select the series `x{filter="maintenance"}` of tenant a.
//
// Standard input is a JSON array of expressions; stdout is
//
//	{"documents": [[[{"selector": <its PromQL spelling, as Prometheus prints it>,
//	                  "start": <byte offset>, "end": <byte offset, exclusive>}, …], …]]}
//
// one list per expression, in input order, its selectors in the order they
// appear. An expression Prometheus' parser refuses refuses the input (rc 2,
// the refusal document); a bad <series> or standard input that is not a JSON
// array of strings is the caller's error (selectorsRequest).
func readPromQLSelectors(want map[string]string, exprs []string) ([]byte, error) {
	out := make([][]selectorHit, 0, len(exprs))
	for i, e := range exprs {
		hits, err := selectorsMatching(e, want)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		out = append(out, hits)
	}
	return json.Marshal(struct {
		Documents [][][]selectorHit `json:"documents"`
	}{Documents: [][][]selectorHit{out}})
}

// selectorsRequest reads the caller's part of a promql-selectors run: the
// <series> argument and the JSON framing of standard input. ⛔ Both are the
// caller's, not the input's (#3943): an error here is a caller error (exit
// 2, no refusal document), never an answer about the expressions.
func selectorsRequest(series string, data []byte) (map[string]string, []string, error) {
	want, err := seriesLabels(series)
	if err != nil {
		return nil, nil, err
	}
	exprs, err := jsonStrings(data)
	if err != nil {
		return nil, nil, fmt.Errorf("standard input is not a JSON array of strings: %w", err)
	}
	return want, exprs, nil
}

// jsonStrings decodes *raw* as a JSON array every element of which is a JSON
// string; `[]` is an empty list. ⛔ json.Unmarshal into a []string takes a
// top-level null as no list and a null element as "" (#3943): the first
// would answer about no expressions, the second hand the parser an empty
// expression and refuse the input over the caller's framing. Both, and any
// other non-string element, are an error here — the caller's, never a
// refusal of the input.
func jsonStrings(raw []byte) ([]string, error) {
	var elems []json.RawMessage
	if err := json.Unmarshal(raw, &elems); err != nil {
		return nil, err
	}
	if elems == nil {
		return nil, fmt.Errorf("null is not an array")
	}
	out := make([]string, len(elems))
	for i, e := range elems {
		s, err := jsonString(e)
		if err != nil {
			return nil, fmt.Errorf("element %d: %w", i, err)
		}
		out[i] = s
	}
	return out, nil
}

// jsonString decodes *raw* as one JSON string. A missing value (nil), null
// or any other JSON value is an error, never "" (see jsonStrings).
func jsonString(raw json.RawMessage) (string, error) {
	if len(raw) == 0 || raw[0] != '"' {
		if len(raw) == 0 {
			return "", fmt.Errorf("missing, not a string")
		}
		return "", fmt.Errorf("%s is not a string", raw)
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", err
	}
	return s, nil
}

type selectorHit struct {
	Selector string `json:"selector"`
	Start    int    `json:"start"`
	End      int    `json:"end"`
}

// seriesLabels reads *series* as a selector of equality matchers.
func seriesLabels(series string) (map[string]string, error) {
	ms, err := parseMetricSelector(series)
	if err != nil {
		return nil, fmt.Errorf("series %q: %w", series, err)
	}
	want := map[string]string{}
	for _, m := range ms {
		if m.Type != labels.MatchEqual {
			return nil, fmt.Errorf("series %q: %s is not an equality matcher", series, m)
		}
		want[m.Name] = m.Value
	}
	if want[model.MetricNameLabel] == "" {
		return nil, fmt.Errorf("series %q names no metric", series)
	}
	return want, nil
}

// selectorsMatching returns the vector selectors of *expr* that could select
// a series with the labels *want* (and others, of unknown value).
func selectorsMatching(expr string, want map[string]string) ([]selectorHit, error) {
	node, err := parseExpr(expr)
	if err != nil {
		return nil, err
	}
	hits := []selectorHit{}
	parser.Inspect(node, func(n parser.Node, _ []parser.Node) error {
		vs, ok := n.(*parser.VectorSelector)
		if !ok || !couldMatch(vs.LabelMatchers, want) {
			return nil
		}
		pr := vs.PositionRange()
		hits = append(hits, selectorHit{Selector: vs.String(), Start: int(pr.Start), End: int(pr.End)})
		return nil
	})
	return hits, nil
}

// couldMatch: every matcher on a label *want* names accepts its value; a
// matcher on another label might accept the series' unknown value.
func couldMatch(ms []*labels.Matcher, want map[string]string) bool {
	for _, m := range ms {
		v, known := want[m.Name]
		if known && !m.Matches(v) {
			return false
		}
	}
	return true
}

// parseExpr and parseMetricSelector are the PromQL parser's, with its panics
// recovered (loaderPanic): a panic of the parser on an expression refuses
// the input — Prometheus' rule loader fails on it the same way. Only the
// consumer library is guarded: a panic of da-read's own code around it is a
// crash (exit 2, no refusal document; #3943).
func parseExpr(e string) (node parser.Expr, err error) {
	defer loaderPanic("Prometheus' PromQL parser", &err)
	return ruleParser.ParseExpr(e)
}

func parseMetricSelector(s string) (ms []*labels.Matcher, err error) {
	defer loaderPanic("Prometheus' PromQL parser", &err)
	return ruleParser.ParseMetricSelector(s)
}
