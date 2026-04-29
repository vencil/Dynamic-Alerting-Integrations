package parser

// Dialect detection for one PromQL/MetricsQL expression.
//
// Strategy (single-parser, per planning §C-8 R7 糾錯一):
//
//  1. Try `metricsql.Parse(expr)`. metricsql is a strict superset of
//     PromQL, so a parse failure here means the expression is broken
//     in both languages. We label it `ambiguous` and surface the
//     parse error verbatim — the caller (ParsedRule.AnalyzeError)
//     uses it to print actionable feedback for the human reviewer.
//
//  2. Walk the AST collecting every function-call name. If any name
//     is reported by IsVMOnlyFunction (loaded from
//     vm_only_functions.yaml in PR-2) the expression is
//     `metricsql`-dialect; otherwise `prom`.
//
//     The `prom_compatible: bool` field on ParsedRule (which DOES
//     require a separate PromQL parser pass) is populated by the
//     strict-mode pipeline in promrule.go::buildParsedRule when
//     ParseOptions.StrictPromQL is set; see strict_prom.go for the
//     prometheus/promql/parser-based validator added in PR-2.
//
// Walker design: metricsql exposes only the `Expr` interface (single
// `AppendString` method). There is no public Visit; we type-switch on
// the concrete Expr structs. Coverage assertion lives in
// dialect_test.go — adding a new metricsql Expr type without
// teaching this walker would be silent under-coverage.

import (
	"fmt"
	"sort"

	"github.com/VictoriaMetrics/metricsql"
)

// AnalyzeExpr classifies a PromQL/MetricsQL expression and reports
// every VM-only function it uses.
//
// Return values:
//   - dialect — see Dialect consts.
//   - vmOnly  — sorted, deduplicated list of VM-only fn names found.
//     Empty when dialect != DialectMetricsQL. May still be populated
//     for DialectAmbiguous if the parse error happened mid-walk
//     (currently never — Parse is atomic — but documented for
//     forward compatibility).
//   - parseErr — the metricsql parse error, exposed for caller
//     diagnostics. nil for prom / metricsql dialects.
func AnalyzeExpr(expr string) (dialect Dialect, vmOnly []string, parseErr error) {
	if expr == "" {
		return DialectAmbiguous, nil, fmt.Errorf("empty expression")
	}

	parsed, err := metricsql.Parse(expr)
	if err != nil {
		return DialectAmbiguous, nil, err
	}

	seen := make(map[string]struct{})
	visitFuncNames(parsed, func(name string) {
		if IsVMOnlyFunction(name) {
			seen[lowerASCII(name)] = struct{}{}
		}
	})

	if len(seen) == 0 {
		return DialectProm, nil, nil
	}

	vmOnly = make([]string, 0, len(seen))
	for n := range seen {
		vmOnly = append(vmOnly, n)
	}
	sort.Strings(vmOnly)
	return DialectMetricsQL, vmOnly, nil
}

// visitFuncNames walks a parsed metricsql.Expr tree and invokes `fn`
// once per function-call site (FuncExpr / AggrFuncExpr).
//
// Coverage rationale for each switch arm:
//   - *MetricExpr   — leaf, label filters only, no nested fn calls.
//   - *NumberExpr   — leaf scalar.
//   - *StringExpr   — leaf string literal.
//   - *DurationExpr — leaf duration literal (`5m`, `1h`).
//   - *ModifierExpr — leaf modifier (`offset 5m`, `@start()`); any
//     modifier args metricsql exposes are themselves Exprs surfaced
//     via the parent FuncExpr/RollupExpr — verified by the coverage
//     test in dialect_test.go.
//   - *FuncExpr     — emits `Name`; recurses into Args.
//   - *AggrFuncExpr — emits `Name`; recurses into Args.
//   - *BinaryOpExpr — recurses into Left + Right (no fn name of its
//     own; binary operators like `+` `>` `==` are not function calls).
//   - *RollupExpr   — recurses into the wrapped Expr (rate/increase
//     wrappers attach a rollup window to a sub-expression).
//
// If metricsql adds a new Expr type and we don't extend this switch,
// TestVisitFuncNames_AllExprTypesCovered (in dialect_test.go) will
// flag it via reflect-based asserting.
func visitFuncNames(e metricsql.Expr, fn func(name string)) {
	if e == nil {
		return
	}
	switch x := e.(type) {
	case *metricsql.MetricExpr, *metricsql.NumberExpr,
		*metricsql.StringExpr, *metricsql.DurationExpr,
		*metricsql.ModifierExpr:
		// leaf — no nested Exprs to walk
	case *metricsql.FuncExpr:
		fn(x.Name)
		for _, a := range x.Args {
			visitFuncNames(a, fn)
		}
	case *metricsql.AggrFuncExpr:
		fn(x.Name)
		for _, a := range x.Args {
			visitFuncNames(a, fn)
		}
	case *metricsql.BinaryOpExpr:
		visitFuncNames(x.Left, fn)
		visitFuncNames(x.Right, fn)
	case *metricsql.RollupExpr:
		visitFuncNames(x.Expr, fn)
		// Window / Step / Offset on RollupExpr are *DurationExpr
		// values; visiting them is a no-op anyway, but keeping the
		// recursion in case future metricsql versions allow
		// expression-valued offsets.
		if x.Window != nil {
			visitFuncNames(x.Window, fn)
		}
		if x.Step != nil {
			visitFuncNames(x.Step, fn)
		}
		if x.Offset != nil {
			visitFuncNames(x.Offset, fn)
		}
	default:
		// Unknown Expr type — silently ignored for forward
		// compatibility, but the coverage test will fail loudly
		// when this branch is hit during normal parsing.
	}
}
