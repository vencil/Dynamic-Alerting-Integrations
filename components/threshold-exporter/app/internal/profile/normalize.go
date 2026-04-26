package profile

// Expression normalisation for cluster signatures.
//
// PR-1 cluster engine groups rules by EXACT signature match — two
// rules belong to the same proposal iff their normalised form is
// byte-identical. Normalisation is therefore the heart of "what
// counts as similar".
//
// The normaliser must be:
//   1. Stable: same input → same output across runs / Go versions.
//   2. Conservative: never collapse two rules that mean different
//      things. False merges in the cluster proposal would write
//      bogus `_defaults.yaml` entries that customers act on.
//   3. Liberal enough to catch the obvious "same alert per tenant"
//      pattern that motivates Phase .c.
//
// PR-1 strategy — token-level rewrite of the *raw expression
// string*, deliberately avoiding metricsql.Parse round-trip:
//
//   a. Numeric literals (integers + floats) → "<NUM>".
//      Threshold values are the most common per-tenant variation;
//      stripping them first is the highest-yield collapse.
//
//   b. Quoted-string label values inside `{...}` → "<STR>".
//      Catches the `{tenant="tenant-a"}` vs `{tenant="tenant-b"}`
//      shape that defines per-tenant variants. We do NOT touch
//      label *keys* (the `tenant` part) — those are real schema.
//
//   c. ALL whitespace removed.
//      The signature is a comparison key, not a human-readable
//      template; stripping rather than collapsing whitespace makes
//      the comparison robust against `> 1` vs `>1` formatting drift
//      without an operator-aware tokeniser. The display-friendly
//      template in ExtractionProposal.SharedExprTemplate is taken
//      from the first rule's normalisation, so callers still see a
//      reasonable shape — they just shouldn't expect re-spacing.
//
// Anything we don't normalise is intentional: function names,
// operators, label keys, modifiers. Two rules with different
// function names absolutely should not cluster — `rate()` and
// `irate()` are different signals.
//
// PR-2 will likely add: unit-aware threshold collapsing
// (`5m` ≡ `300s`), comment stripping, AST-level structural matching
// for cases where token-rewrite isn't enough. For PR-1 the cheap
// regex pass handles the common-case "same alert per tenant"
// pattern that drives the bulk of customer rule corpora.

import (
	"regexp"
	"strings"
)

var (
	// numericLiteral matches integer + decimal numbers preceded by
	// either start-of-string or a single punctuator/whitespace
	// character. The leading-char gate (NOT a `\b` word boundary)
	// is what protects identifiers with embedded digits like
	// `http_requests_total_5xx`: the `5` follows `_`, which isn't
	// in the leading set, so the regex declines to fire there.
	// Doesn't try to match scientific notation or hex — neither
	// appears in PromQL/MetricsQL rule corpora we've seen.
	numericLiteral = regexp.MustCompile(`(?:^|(?P<lead>[\s\(\[\,\=\!\<\>\+\-\*\/\%]))-?\d+(?:\.\d+)?`)

	// quotedString matches a double- or single-quoted string. Used
	// to strip per-tenant label values inside `{...}` matchers.
	// Greedy by design — we don't model escape sequences because
	// PromQL label-value strings rarely contain quotes; the worst
	// case is over-collapsing two strings that differ only in
	// escapes, which is acceptable for cluster signatures.
	quotedString = regexp.MustCompile(`"[^"]*"|'[^']*'`)

	// anyWhitespace matches any whitespace run for full removal.
	anyWhitespace = regexp.MustCompile(`\s+`)
)

// normaliseExpr produces the cluster signature for one expression.
// Two rules with identical normaliseExpr output cluster together
// (provided their other axes also match — see signatureFor).
//
// Returns "" for empty input. The caller treats empty-signature
// rules as Unclustered.
func normaliseExpr(expr string) string {
	if expr == "" {
		return ""
	}
	out := expr

	// Strip quoted-string label values first — otherwise the
	// numericLiteral regex can pick up digits inside string values
	// and mis-replace them.
	out = quotedString.ReplaceAllString(out, `"<STR>"`)

	// Replace numeric literals. The capture-group preserves the
	// leading delimiter so `> 0.9` becomes `> <NUM>` not `<NUM>9`.
	out = numericLiteral.ReplaceAllStringFunc(out, func(match string) string {
		// Find the leading delimiter character (if any) and preserve it.
		// numericLiteral's leading char (when present) is exactly one
		// byte: whitespace or a single ASCII punctuator.
		if len(match) == 0 {
			return "<NUM>"
		}
		first := match[0]
		if first == '-' || (first >= '0' && first <= '9') {
			// No leading delimiter captured (start-of-string match).
			return "<NUM>"
		}
		return string(first) + "<NUM>"
	})

	// Strip whitespace last so the placeholder substitutions can't
	// leave behind any. Removing all whitespace (rather than
	// collapsing to single spaces) is what makes `> 1` and `>1`
	// produce identical signatures.
	out = anyWhitespace.ReplaceAllString(out, "")
	return strings.TrimSpace(out)
}
