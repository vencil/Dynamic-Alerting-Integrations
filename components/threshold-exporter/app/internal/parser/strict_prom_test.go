package parser

import (
	"strings"
	"testing"
)

func TestValidateStrictPromQL_ValidExpressions(t *testing.T) {
	cases := []struct{ name, expr string }{
		{"simple gauge", `up == 1`},
		{"rate sum", `sum(rate(http_requests_total[5m]))`},
		{"label matchers", `kube_pod_status_phase{phase=~"Pending|Failed"} > 0`},
		{"histogram_quantile", `histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))`},
		{"binary scalar", `node_load1 / on(instance) node_cpu_count > 4`},
		{"offset modifier", `rate(http_requests_total[5m] offset 1h)`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateStrictPromQL(tc.expr); err != nil {
				t.Errorf("ValidateStrictPromQL(%q) = %v, want nil", tc.expr, err)
			}
			if !IsStrictPromQLCompatible(tc.expr) {
				t.Errorf("IsStrictPromQLCompatible(%q) = false, want true", tc.expr)
			}
		})
	}
}

func TestValidateStrictPromQL_RejectsVMOnlyFunctions(t *testing.T) {
	// VM-only function names are syntactically valid PromQL function
	// calls (the parser sees them as unknown function-name tokens),
	// so promql/parser will reject them at parse time. This pins the
	// "anti-vendor-lock-in" promise: rules using rollup_rate /
	// quantiles_over_time / etc. cannot pass strict validation.
	cases := []struct{ name, expr string }{
		{"rollup_rate", `rollup_rate(redis_db_keys[5m]) > 100`},
		{"quantiles_over_time", `quantiles_over_time("0.99,0.999", foo[5m])`},
		{"interpolate", `interpolate(some_gauge)`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateStrictPromQL(tc.expr); err == nil {
				t.Errorf("ValidateStrictPromQL(%q) = nil, want non-nil — VM-only fns must fail strict PromQL", tc.expr)
			}
			if IsStrictPromQLCompatible(tc.expr) {
				t.Errorf("IsStrictPromQLCompatible(%q) = true, want false", tc.expr)
			}
		})
	}
}

func TestValidateStrictPromQL_RejectsSyntaxErrors(t *testing.T) {
	cases := []string{
		`sum(rate(foo[5m]`, // missing close paren
		`foo[`,             // truncated range selector
		`{job="x"`,          // unmatched brace
	}
	for _, expr := range cases {
		t.Run(expr, func(t *testing.T) {
			err := ValidateStrictPromQL(expr)
			if err == nil {
				t.Errorf("ValidateStrictPromQL(%q) = nil, want syntax error", expr)
			}
			// Diagnostic should be non-empty and prefixed for caller filtering.
			if err != nil && !strings.Contains(err.Error(), "promql/parser") {
				t.Errorf("error %q does not contain 'promql/parser' prefix", err.Error())
			}
		})
	}
}

func TestValidateStrictPromQL_EmptyExpression(t *testing.T) {
	err := ValidateStrictPromQL("")
	if err == nil {
		t.Fatal("ValidateStrictPromQL(\"\") = nil, want empty-expression error")
	}
	if !strings.Contains(err.Error(), "empty") {
		t.Errorf("error %q does not mention 'empty'", err.Error())
	}
	if IsStrictPromQLCompatible("") {
		t.Error("IsStrictPromQLCompatible(\"\") = true, want false")
	}
}

func TestParsePromRulesWithOptions_PromCompatibleOnPureProm(t *testing.T) {
	src := mustReadTestdata(t, "promrule_basic.yaml")
	res, err := ParsePromRulesWithOptions(src, "promrule_basic.yaml", testGeneratedBy, ParseOptions{StrictPromQL: true})
	if err != nil {
		t.Fatalf("ParsePromRulesWithOptions: %v", err)
	}
	for i, r := range res.Rules {
		if r.Dialect != DialectProm {
			t.Fatalf("rule[%d] dialect = %q, want prom", i, r.Dialect)
		}
		if !r.PromCompatible {
			t.Errorf("rule[%d] PromCompatible = false, want true (expr=%q, err=%q)", i, r.Expr, r.StrictPromError)
		}
		if r.StrictPromError != "" {
			t.Errorf("rule[%d] StrictPromError = %q, want empty", i, r.StrictPromError)
		}
	}
}

func TestParsePromRulesWithOptions_VMOnlyMarkedIncompatible(t *testing.T) {
	src := mustReadTestdata(t, "promrule_metricsql.yaml")
	res, err := ParsePromRulesWithOptions(src, "promrule_metricsql.yaml", testGeneratedBy, ParseOptions{StrictPromQL: true})
	if err != nil {
		t.Fatalf("ParsePromRulesWithOptions: %v", err)
	}
	if len(res.Rules) == 0 {
		t.Fatal("expected at least one VM-only rule in fixture")
	}
	for i, r := range res.Rules {
		if r.Dialect != DialectMetricsQL {
			continue
		}
		if r.PromCompatible {
			t.Errorf("rule[%d] (vm-only) PromCompatible = true, want false (expr=%q)", i, r.Expr)
		}
		if r.StrictPromError == "" {
			t.Errorf("rule[%d] StrictPromError empty, want diagnostic from upstream parser", i)
		}
	}
}

func TestParsePromRulesWithOptions_AmbiguousMarkedIncompatibleWithoutDoublePass(t *testing.T) {
	// Pin behavior: ambiguous rules (metricsql parse failed) are
	// marked PromCompatible=false but StrictPromError is left empty —
	// running the strict parser too just produces a second confusing
	// error message. The reviewer already has AnalyzeError.
	src := mustReadTestdata(t, "promrule_ambiguous.yaml")
	res, err := ParsePromRulesWithOptions(src, "promrule_ambiguous.yaml", testGeneratedBy, ParseOptions{StrictPromQL: true})
	if err != nil {
		t.Fatalf("ParsePromRulesWithOptions: %v", err)
	}
	sawAmbiguous := false
	for _, r := range res.Rules {
		if r.Dialect != DialectAmbiguous {
			continue
		}
		sawAmbiguous = true
		if r.PromCompatible {
			t.Errorf("ambiguous rule has PromCompatible = true, want false (expr=%q)", r.Expr)
		}
		if r.StrictPromError != "" {
			t.Errorf("ambiguous rule has StrictPromError = %q, want empty (AnalyzeError carries the signal)", r.StrictPromError)
		}
		if r.AnalyzeError == "" {
			t.Errorf("ambiguous rule has empty AnalyzeError, fixture broken?")
		}
	}
	if !sawAmbiguous {
		t.Skip("fixture has no ambiguous rules — skipping (assert is conditional)")
	}
}

func TestParsePromRulesWithOptions_StrictOffLeavesFieldsZero(t *testing.T) {
	// Default options (StrictPromQL=false) MUST NOT touch
	// PromCompatible / StrictPromError. PR-1 callers depend on this
	// to avoid the upstream parser cost.
	src := mustReadTestdata(t, "promrule_basic.yaml")
	res, err := ParsePromRulesWithOptions(src, "promrule_basic.yaml", testGeneratedBy, ParseOptions{})
	if err != nil {
		t.Fatalf("ParsePromRulesWithOptions: %v", err)
	}
	for i, r := range res.Rules {
		if r.PromCompatible {
			t.Errorf("rule[%d] PromCompatible = true with StrictPromQL=false; want zero value", i)
		}
		if r.StrictPromError != "" {
			t.Errorf("rule[%d] StrictPromError = %q with StrictPromQL=false; want empty", i, r.StrictPromError)
		}
	}
}

func TestParsePromRules_BackwardsCompatPreservesPR1Behavior(t *testing.T) {
	// ParsePromRules is the PR-1 contract. PR-2 made it a thin
	// wrapper around ParsePromRulesWithOptions(opts={}); this test
	// pins that the wrapper doesn't accidentally turn StrictPromQL
	// on (would force every legacy caller to take a perf hit + a
	// new dependency surface).
	src := mustReadTestdata(t, "promrule_basic.yaml")
	res, err := ParsePromRules(src, "promrule_basic.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}
	for i, r := range res.Rules {
		if r.PromCompatible {
			t.Errorf("rule[%d] PromCompatible = true via PR-1 API; want zero value", i)
		}
		if r.StrictPromError != "" {
			t.Errorf("rule[%d] StrictPromError populated via PR-1 API; want empty", i)
		}
	}
}
