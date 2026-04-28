package parser

// StrictPromQLValidator wraps prometheus/promql/parser.ParseExpr for
// the C-8 PR-2 "anti-vendor-lock-in" promise.
//
// Why a second parser at all when metricsql already accepts every
// PromQL expression? Because metricsql is a strict superset — a rule
// that parses with metricsql AND uses no VM-only functions might
// still be rejected by vanilla Prometheus. Concrete drift sources we
// have to detect:
//
//   - VM-specific syntactic sugar that doesn't trigger a function-
//     name match (currently rare, but the metricsql changelog adds
//     them periodically).
//   - PromQL grammar quirks Prometheus rejects that metricsql tolerates
//     (e.g. modifier ordering, label-name quoting rules).
//   - Forward-compat surprises: a future metricsql version starts
//     parsing some VM-only DSL we haven't audited yet.
//
// The strict validator gives `prom_compatible: bool` an O(1) source
// of truth: parse with prometheus/promql/parser; failure → false,
// success → true. Combined with the existing VM-only allowlist we get
// the planning §C-8 spec's distinction:
//
//   - dialect: prom        → metricsql parses + no VM-only functions
//                            (always also prom_compatible = true)
//   - dialect: metricsql   → metricsql parses + uses VM-only functions
//                            (prom_compatible always = false)
//   - dialect: ambiguous   → metricsql parse failed
//                            (prom_compatible = false)
//
// And the new prom_compatible flag adds a finer signal: a rule could
// be `dialect=prom` but `prom_compatible=false` if metricsql happens
// to accept some quirky shape that real Prometheus doesn't. Customers
// running `--fail-on-non-portable` rely on this to gate migrations.

import (
	"fmt"
	"sync"

	promqlparser "github.com/prometheus/prometheus/promql/parser"
)

// strictParser is a process-wide singleton because NewParser is
// stateless beyond Options — sharing one instance saves the
// per-call construction cost when validating many rules in a tight
// loop (typical CLI workload: 10K rules in the customer corpus).
//
// Created on first use; concurrent first-use is safe via sync.Once.
var (
	strictParserOnce sync.Once
	strictParser     promqlparser.Parser
)

func getStrictParser() promqlparser.Parser {
	strictParserOnce.Do(func() {
		// Zero-value Options matches a vanilla Prometheus server
		// without experimental flags — the conservative target for
		// "anti-vendor-lock-in" portability claims.
		strictParser = promqlparser.NewParser(promqlparser.Options{})
	})
	return strictParser
}

// ValidateStrictPromQL reports whether `expr` parses with the
// upstream Prometheus PromQL parser. A nil error means the expression
// would be accepted by a vanilla Prometheus server.
//
// This function is called per-rule by the parser (when
// ParseOptions.StrictPromQL is true) and exposed as a stand-alone
// helper for the CLI's `--validate-strict-prom` flag — callers who
// only want a yes/no signal without parsing the whole rule.
//
// Empty string is rejected the same way the metricsql analyzer
// rejects it (mirrors AnalyzeExpr's contract).
func ValidateStrictPromQL(expr string) error {
	if expr == "" {
		return fmt.Errorf("empty expression")
	}
	if _, err := getStrictParser().ParseExpr(expr); err != nil {
		return fmt.Errorf("promql/parser: %w", err)
	}
	return nil
}

// IsStrictPromQLCompatible is a convenience boolean wrapper around
// ValidateStrictPromQL. Use this when you only care about the
// outcome, not the diagnostic message.
func IsStrictPromQLCompatible(expr string) bool {
	return ValidateStrictPromQL(expr) == nil
}
