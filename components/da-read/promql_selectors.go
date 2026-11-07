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
// appear. An expression Prometheus' parser refuses refuses the input (rc 2).
func readPromQLSelectors(series string, data []byte) ([]byte, error) {
	want, err := seriesLabels(series)
	if err != nil {
		return nil, err
	}
	var exprs []string
	if err := json.Unmarshal(data, &exprs); err != nil {
		return nil, fmt.Errorf("standard input is not a JSON array of strings: %w", err)
	}
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

type selectorHit struct {
	Selector string `json:"selector"`
	Start    int    `json:"start"`
	End      int    `json:"end"`
}

// seriesLabels reads *series* as a selector of equality matchers.
func seriesLabels(series string) (map[string]string, error) {
	ms, err := ruleParser.ParseMetricSelector(series)
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
	node, err := ruleParser.ParseExpr(expr)
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
