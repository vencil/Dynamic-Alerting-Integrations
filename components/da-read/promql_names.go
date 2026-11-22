package main

import (
	"encoding/json"

	"github.com/prometheus/prometheus/promql/parser"
	"github.com/prometheus/prometheus/util/features"
)

// `da-read promql-names -`: whether each name is one a rule's expression can
// call — a function or an aggregation operator the parser Prometheus loads
// rule files with accepts (#4099: the custom-rule lint's denied_functions).
// A name the parser does not know (`holt_winters`, a typo), or knows but
// has not enabled (an experimental function, unless deployedFeatures enables
// them: prometheus_features.go), is never called, so a deny of
// it denies nothing.
//
// Standard input is a JSON array of names; stdout is
//
//	{"documents": [[true|false, …]]}
//
// one verdict per name, in input order. The names are spelt as
// promql-reads reports a call: a function's name, an aggregation's
// lower-case operator. Standard input that is not a JSON array of strings
// is the caller's error.
//
// The verdicts are the parser's own registry (RegisterFeatures, which
// Prometheus serves as /api/v1/features), never a list of ours: a
// Prometheus that adds, renames or enables a function changes them.
func readPromQLNames(names []string) ([]byte, error) {
	known := callableNames()
	out := make([]bool, 0, len(names))
	for _, n := range names {
		out = append(out, known[n])
	}
	return json.Marshal(struct {
		Documents [][]bool `json:"documents"`
	}{Documents: [][]bool{out}})
}

// callableNames: the functions and aggregation operators ruleParser
// accepts, by the spelling a call is reported in.
func callableNames() map[string]bool {
	r := features.NewRegistry()
	ruleParser.RegisterFeatures(r)
	set := r.Get()
	aggregations := map[string]bool{}
	for t := parser.ItemType(0); t < 1<<16; t++ {
		if t.IsAggregator() {
			aggregations[t.String()] = true
		}
	}
	out := map[string]bool{}
	for name, enabled := range set[features.PromQLFunctions] {
		out[name] = enabled
	}
	// PromQLOperators holds the binary operators beside the aggregations.
	for name, enabled := range set[features.PromQLOperators] {
		if aggregations[name] {
			out[name] = enabled
		}
	}
	return out
}
