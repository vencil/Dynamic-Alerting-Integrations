package profile

import "testing"

func TestNormaliseExpr_StripsNumericLiterals(t *testing.T) {
	// Whitespace is fully stripped at the end of normalisation, so
	// expected values collapse spaces between tokens.
	cases := []struct{ in, want string }{
		{"x > 0.9", "x><NUM>"},
		{"x > 5", "x><NUM>"},
		{"x > 0.85 and y < 100", "x><NUM>andy<<NUM>"},
		{"foo[5m]", "foo[<NUM>m]"},
		// Decimal threshold + integer threshold normalise to the
		// same template so per-tenant variants collapse.
		{"rate(http[1m]) > 1.5", "rate(http[<NUM>m])><NUM>"},
		{"rate(http[1m]) > 2", "rate(http[<NUM>m])><NUM>"},
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := normaliseExpr(tc.in); got != tc.want {
				t.Errorf("normaliseExpr(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestNormaliseExpr_StripsLabelStringValues(t *testing.T) {
	cases := []struct{ in, want string }{
		{`x{tenant="tenant-a"}`, `x{tenant="<STR>"}`},
		{`x{tenant="tenant-b"}`, `x{tenant="<STR>"}`},
		// Both should normalise to the same string so they cluster.
		{`x{tenant='single-quoted'}`, `x{tenant="<STR>"}`},
		{`x{a="foo",b="bar"}`, `x{a="<STR>",b="<STR>"}`},
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := normaliseExpr(tc.in); got != tc.want {
				t.Errorf("normaliseExpr(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestNormaliseExpr_PerTenantVariantsCollapse(t *testing.T) {
	// The motivating use case: same alert per tenant, only label
	// value + threshold differ. Both must normalise to the same
	// signature so they cluster.
	a := `avg(rate(node_cpu_seconds_total{tenant="tenant-a"}[5m])) > 0.85`
	b := `avg(rate(node_cpu_seconds_total{tenant="tenant-b"}[5m])) > 0.95`
	if normaliseExpr(a) != normaliseExpr(b) {
		t.Errorf("expected per-tenant variants to collapse:\n  a: %q\n  b: %q", normaliseExpr(a), normaliseExpr(b))
	}
}

func TestNormaliseExpr_DifferentFunctionsStayDistinct(t *testing.T) {
	// rate() and irate() are different signals — they must NOT
	// collapse, even with identical surroundings.
	r := `rate(foo[5m]) > 1`
	ir := `irate(foo[5m]) > 1`
	if normaliseExpr(r) == normaliseExpr(ir) {
		t.Errorf("rate vs irate should not collapse:\n  rate:  %q\n  irate: %q", normaliseExpr(r), normaliseExpr(ir))
	}
}

func TestNormaliseExpr_CollapsesWhitespace(t *testing.T) {
	// Two formattings of the same expression normalise identically.
	tight := `rate(foo[5m])>1`
	loose := `rate(foo[5m])  >   1`
	if normaliseExpr(tight) != normaliseExpr(loose) {
		t.Errorf("whitespace formatting differences leaked into signature:\n  tight: %q\n  loose: %q",
			normaliseExpr(tight), normaliseExpr(loose))
	}
}

func TestNormaliseExpr_EmptyReturnsEmpty(t *testing.T) {
	if got := normaliseExpr(""); got != "" {
		t.Errorf("normaliseExpr(\"\") = %q, want empty", got)
	}
}

func TestNormaliseExpr_DigitsInIdentifiersPreserved(t *testing.T) {
	// `node_cpu_seconds_total` contains no digits but other metric
	// names like `http_requests_total_5xx` do. The normaliser must
	// not strip digits that are part of an identifier.
	in := `http_requests_total_5xx > 0`
	got := normaliseExpr(in)
	// We expect the trailing `> 0` to become `><NUM>` but the
	// `_5xx` inside the identifier to remain (whitespace stripped).
	want := `http_requests_total_5xx><NUM>`
	if got != want {
		t.Errorf("normaliseExpr(%q) = %q, want %q (digits inside identifier should survive)", in, got, want)
	}
}
