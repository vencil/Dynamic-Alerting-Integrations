package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/prometheus/prometheus/promql/parser"
)

// TestNamesAreWhatTheParserLetsARuleCall holds every verdict to the parser
// itself (#4099): a name is accepted exactly when calling it does not fail
// for being unknown or not enabled — over every function Prometheus defines
// and every aggregation operator, so a Prometheus that adds, renames or
// enables one cannot leave the verdicts behind.
func TestNamesAreWhatTheParserLetsARuleCall(t *testing.T) {
	t.Parallel()
	names := []string{"holt_winters", "predict_lineer", "and", "+", "atan2", "SUM", ""}
	for n := range parser.Functions {
		names = append(names, n)
	}
	for it := parser.ItemType(0); it < 1<<16; it++ {
		if it.IsAggregator() {
			names = append(names, it.String())
		}
	}
	out, err := readPromQLNames(names)
	if err != nil {
		t.Fatal(err)
	}
	var got struct{ Documents [][]bool }
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	accepted := 0
	for i, n := range names {
		// the call in each argument shape: refused when any shape is refused
		// for the name itself (an argument error can come first)
		var perr error
		refused := false
		for _, args := range []string{"()", "(x)", "(1, x)", `("v", x)`} {
			_, perr = ruleParser.ParseExpr(n + args)
			if perr != nil && (strings.Contains(perr.Error(), "unknown function") ||
				strings.Contains(perr.Error(), "not enabled") ||
				strings.Contains(perr.Error(), "is experimental")) {
				refused = true
				break
			}
		}
		refused = refused || !isCallable(n)
		if got.Documents[0][i] == refused {
			t.Errorf("%q: verdict %v, but parsing %q(): %v", n, got.Documents[0][i], n, perr)
		}
		if got.Documents[0][i] {
			accepted++
		}
	}
	if accepted < 50 {
		t.Fatalf("only %d names accepted: the registry was not read", accepted)
	}
}

// isCallable: a name that can head a call at all (an identifier or an
// aggregation operator), not an operator symbol or a keyword spelt in
// another case.
func isCallable(n string) bool {
	if _, ok := parser.Functions[n]; ok {
		return true
	}
	for it := parser.ItemType(0); it < 1<<16; it++ {
		if it.IsAggregator() && it.String() == n {
			return true
		}
	}
	return false
}

func TestPromQLNamesCommand(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	if rc := run([]string{"promql-names", "-"}, strings.NewReader(`["rate", "topk", "holt_winters"]`), &stdout, &stderr); rc != exitOK ||
		stdout.String() != `{"documents":[[true,true,false]]}`+"\n" {
		t.Fatalf("rc %d stdout %q stderr %s", rc, stdout.String(), stderr.String())
	}
	for _, bad := range []string{`{}`, `[1]`, `[null]`, `not json`} {
		stdout.Reset()
		if rc := run([]string{"promql-names", "-"}, strings.NewReader(bad), &stdout, &stderr); rc != exitCallerErr || stdout.Len() != 0 {
			t.Errorf("%s: rc %d stdout %q", bad, rc, stdout.String())
		}
	}
}
