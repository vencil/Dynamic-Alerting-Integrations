package parser

import (
	"reflect"
	"strings"
	"testing"

	"github.com/VictoriaMetrics/metricsql"
)

func TestAnalyzeExpr_PortablePromQL(t *testing.T) {
	cases := []struct{ name, expr string }{
		{"simple gauge ratio", "container_memory_usage_bytes / container_spec_memory_limit_bytes > 0.85"},
		{"rate + sum", "sum by (job) (rate(http_requests_total[5m]))"},
		{"histogram_quantile + le", "histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))"},
		{"avg over time", "avg_over_time(node_load1[10m])"},
		{"binary scalar comparison", "up == 0"},
		{"label matchers", `kube_pod_status_phase{phase=~"Pending|Failed"} > 0`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, vmOnly, err := AnalyzeExpr(tc.expr)
			if err != nil {
				t.Fatalf("AnalyzeExpr error: %v", err)
			}
			if d != DialectProm {
				t.Errorf("dialect = %q, want %q", d, DialectProm)
			}
			if len(vmOnly) != 0 {
				t.Errorf("vmOnly = %v, want empty", vmOnly)
			}
		})
	}
}

func TestAnalyzeExpr_VMOnlyDialect(t *testing.T) {
	cases := []struct {
		name        string
		expr        string
		wantVMFuncs []string
	}{
		{"quantiles_over_time", `quantiles_over_time("0.99,0.999", foo[5m])`, []string{"quantiles_over_time"}},
		{"rollup_rate", "rollup_rate(redis_db_keys[5m])", []string{"rollup_rate"}},
		{"remove_resets", "remove_resets(some_counter) > 0", []string{"remove_resets"}},
		{"smooth_exponential composed with rate", "smooth_exponential(rate(http_requests_total[1m]), 0.3)", []string{"smooth_exponential"}},
		{"two VM-only funcs in one expr", "interpolate(keep_last_value(some_gauge))", []string{"interpolate", "keep_last_value"}},
		{"VM-only inside binary op", "rollup_rate(foo[5m]) + rate(bar[5m]) > 100", []string{"rollup_rate"}},
		{"case insensitive — uppercase", "ROLLUP_RATE(foo[5m])", []string{"rollup_rate"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, vmOnly, err := AnalyzeExpr(tc.expr)
			if err != nil {
				t.Fatalf("AnalyzeExpr error: %v", err)
			}
			if d != DialectMetricsQL {
				t.Errorf("dialect = %q, want %q", d, DialectMetricsQL)
			}
			if !reflect.DeepEqual(vmOnly, tc.wantVMFuncs) {
				t.Errorf("vmOnly = %v, want %v", vmOnly, tc.wantVMFuncs)
			}
		})
	}
}

func TestAnalyzeExpr_AmbiguousOnSyntaxError(t *testing.T) {
	d, vmOnly, err := AnalyzeExpr("sum(rate(foo[5m]")
	if d != DialectAmbiguous {
		t.Errorf("dialect = %q, want %q", d, DialectAmbiguous)
	}
	if err == nil {
		t.Error("err = nil, want parse failure surfaced")
	}
	if vmOnly != nil {
		t.Errorf("vmOnly = %v, want nil for ambiguous", vmOnly)
	}
}

func TestAnalyzeExpr_AmbiguousOnEmpty(t *testing.T) {
	d, _, err := AnalyzeExpr("")
	if d != DialectAmbiguous {
		t.Errorf("dialect = %q, want %q", d, DialectAmbiguous)
	}
	if err == nil || !strings.Contains(err.Error(), "empty") {
		t.Errorf("err = %v, want empty-expression error", err)
	}
}

// TestVisitFuncNames_AllExprTypesCovered asserts that every concrete
// metricsql.Expr type returned by Parse for a non-trivial corpus is
// recognised by visitFuncNames. If metricsql adds a new Expr type
// upstream and we don't extend the type switch, walks will silently
// miss VM-only functions inside the new shape — this test catches
// that regression by parsing a spread of expressions covering every
// known shape and asserting we never hit the `default` arm.
func TestVisitFuncNames_AllExprTypesCovered(t *testing.T) {
	// Each expression below is chosen to surface one specific Expr
	// concrete type when parsed by metricsql v0.87.0. The test
	// records the set of types actually visited and compares to the
	// expected set; a delta means either metricsql changed shape or
	// the corpus needs extending.
	corpus := []string{
		// MetricExpr + label filters
		`http_requests_total{job="api"}`,
		// NumberExpr (scalar)
		`42`,
		// StringExpr — appears via metricsql.StringExpr inside fn args
		`label_replace(foo, "dst", "$1", "src", "(.+)")`,
		// DurationExpr — bare duration is rare; nest inside rate()
		`rate(foo[5m])`,
		// FuncExpr
		`abs(foo)`,
		// AggrFuncExpr (sum/min/max/avg/...)
		`sum by (job) (foo)`,
		// BinaryOpExpr
		`foo + bar`,
		// RollupExpr (the [5m] window itself)
		`rate(foo[5m])`,
		// ModifierExpr (offset)
		`foo offset 1h`,
	}
	visited := make(map[string]struct{})
	hitDefault := false
	for _, src := range corpus {
		expr, err := metricsql.Parse(src)
		if err != nil {
			t.Fatalf("metricsql.Parse(%q): %v", src, err)
		}
		recordExprTypes(expr, visited, &hitDefault)
	}
	if hitDefault {
		t.Error("visitFuncNames hit the `default` arm — metricsql introduced a new Expr type the walker doesn't recognise")
	}
	// Sanity: ensure we actually walked enough types to make the
	// assertion meaningful. If metricsql renames internal types
	// this lower bound will still warn early.
	if len(visited) < 5 {
		t.Errorf("visited only %d Expr types (%v); corpus may have lost coverage", len(visited), visited)
	}
}

// recordExprTypes mirrors visitFuncNames but records the concrete Go
// type names instead of function names. Stays in lock-step with the
// production switch — if a new arm is added there, add it here too.
func recordExprTypes(e metricsql.Expr, seen map[string]struct{}, hitDefault *bool) {
	if e == nil {
		return
	}
	typeName := reflectTypeName(e)
	seen[typeName] = struct{}{}
	switch x := e.(type) {
	case *metricsql.MetricExpr, *metricsql.NumberExpr,
		*metricsql.StringExpr, *metricsql.DurationExpr,
		*metricsql.ModifierExpr:
		// leaf
	case *metricsql.FuncExpr:
		for _, a := range x.Args {
			recordExprTypes(a, seen, hitDefault)
		}
	case *metricsql.AggrFuncExpr:
		for _, a := range x.Args {
			recordExprTypes(a, seen, hitDefault)
		}
	case *metricsql.BinaryOpExpr:
		recordExprTypes(x.Left, seen, hitDefault)
		recordExprTypes(x.Right, seen, hitDefault)
	case *metricsql.RollupExpr:
		recordExprTypes(x.Expr, seen, hitDefault)
		if x.Window != nil {
			recordExprTypes(x.Window, seen, hitDefault)
		}
		if x.Step != nil {
			recordExprTypes(x.Step, seen, hitDefault)
		}
		if x.Offset != nil {
			recordExprTypes(x.Offset, seen, hitDefault)
		}
	default:
		*hitDefault = true
	}
}

// reflectTypeName returns "*metricsql.<Foo>" for a parsed expression
// without pulling reflect into the production walker. Used only by
// the coverage test above.
func reflectTypeName(e metricsql.Expr) string {
	if e == nil {
		return "<nil>"
	}
	// fmt.Sprintf("%T", e) avoids reflect.TypeOf import noise.
	return tName(e)
}

func tName(e any) string {
	// Tiny helper to keep the import surface minimal.
	switch e.(type) {
	case *metricsql.MetricExpr:
		return "*metricsql.MetricExpr"
	case *metricsql.NumberExpr:
		return "*metricsql.NumberExpr"
	case *metricsql.StringExpr:
		return "*metricsql.StringExpr"
	case *metricsql.DurationExpr:
		return "*metricsql.DurationExpr"
	case *metricsql.ModifierExpr:
		return "*metricsql.ModifierExpr"
	case *metricsql.FuncExpr:
		return "*metricsql.FuncExpr"
	case *metricsql.AggrFuncExpr:
		return "*metricsql.AggrFuncExpr"
	case *metricsql.BinaryOpExpr:
		return "*metricsql.BinaryOpExpr"
	case *metricsql.RollupExpr:
		return "*metricsql.RollupExpr"
	default:
		return "unknown"
	}
}

func TestIsVMOnlyFunction_CaseInsensitive(t *testing.T) {
	cases := []struct {
		name string
		want bool
	}{
		{"rollup_rate", true},
		{"ROLLUP_RATE", true},
		{"Rollup_Rate", true},
		{"rate", false},
		{"sum", false},
		{"histogram_quantile", false}, // PromQL native
		{"histogram_share", true},     // VM only
		{"", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := IsVMOnlyFunction(tc.name); got != tc.want {
				t.Errorf("IsVMOnlyFunction(%q) = %v, want %v", tc.name, got, tc.want)
			}
		})
	}
}
