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
// PR-5 (fuzzy matching) adds **duration canonicalisation** as an
// opt-in pass that runs BEFORE numericLiteral. With it enabled,
// `rate(foo[5m])` and `rate(foo[300s])` produce the same signature
// even though their raw text differs — because both durations canonicalise
// to "300 seconds" before numeric stripping. The strict pass remains
// unchanged (PR-1 contract) so existing callers see identical behaviour
// until they opt in via `WithCanonicalDurations()`.
//
// Other future improvements (NOT shipped here): comment stripping,
// AST-level structural matching, fuzzy Levenshtein label matching,
// cross-dialect collapsing. The honest scope of PR-5 is the
// duration-equivalence case that planning §C-9 PR-1 opening comments
// flagged as "PR-2 likely adds".

import (
	"regexp"
	"strconv"
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

	// rangeDurationToken matches a PromQL range-vector duration
	// embedded in `[...]` — the only place ordinary expressions allow
	// duration literals (e.g. `rate(foo[5m])`, `quantile_over_time(0.9, foo[1h30m])`).
	// We deliberately do NOT try to canonicalise durations after
	// `offset` or `@` modifiers in PR-5: those forms are rarer in
	// customer corpora and bring more grammar edge cases for marginal
	// signature gain.
	//
	// The pattern accepts multi-unit forms (`1h30m`, `2h45m30s`) and
	// the standard unit set {y, w, d, h, m, s, ms}. Negative durations
	// are not matched (none in real corpora and would corrupt the
	// to-seconds parser).
	rangeDurationToken = regexp.MustCompile(`\[(\d+(?:ms|[smhdwy]))+\]`)
)

// canonicaliseDurations rewrites every `[<duration>]` literal in `expr`
// into `[<DUR_<base26-encoded-millis>>]`. Two raw expressions whose
// only difference is duration syntax (`[5m]` vs `[300s]` vs `[300000ms]`)
// produce byte-identical output, letting the fuzzy cluster pass treat
// them as equivalent. Unparseable forms are left verbatim — a fuzzy
// cluster shouldn't form on a malformed duration anyway.
//
// The encoding uses base-26 lowercase letters specifically because
// the strict pass (numericLiteral / quotedString) runs AFTER this
// step. Encoding to digits would let numericLiteral mangle the
// canonicalised value into `<NUM>` (losing the equivalence signal).
// Letters survive every existing pass.
//
// Why milliseconds: PromQL allows `100ms` and we want it to canonicalise
// distinctly from `[1s]` (= 1000ms). Going through milliseconds
// preserves sub-second resolution; second-only would collide them.
func canonicaliseDurations(expr string) string {
	return rangeDurationToken.ReplaceAllStringFunc(expr, func(match string) string {
		// match is like "[5m]" or "[1h30m]" — strip the brackets,
		// parse, replace.
		inner := match[1 : len(match)-1]
		ms, ok := durationToMillis(inner)
		if !ok {
			// Leave verbatim — better to under-merge than to silently
			// collapse a token we can't fully parse.
			return match
		}
		return "[<DUR_" + base26Encode(ms) + ">]"
	})
}

// durationToMillis parses a PromQL duration string (multi-unit allowed)
// to total milliseconds. Returns false on malformed input.
//
// Supported units (PromQL grammar):
//   - ms = 1
//   - s  = 1_000
//   - m  = 60_000
//   - h  = 3_600_000
//   - d  = 86_400_000
//   - w  = 604_800_000
//   - y  = 31_536_000_000
//
// Multi-unit forms accumulate (`1h30m` = 3_600_000 + 1_800_000).
func durationToMillis(s string) (int64, bool) {
	if s == "" {
		return 0, false
	}
	var total int64
	i := 0
	for i < len(s) {
		// Read digit run.
		j := i
		for j < len(s) && s[j] >= '0' && s[j] <= '9' {
			j++
		}
		if j == i {
			return 0, false
		}
		n, err := strconv.ParseInt(s[i:j], 10, 64)
		if err != nil {
			return 0, false
		}
		// Read unit.
		if j >= len(s) {
			return 0, false
		}
		var unitMs int64
		switch s[j] {
		case 'm':
			// Distinguish "ms" (millisecond) from "m" (minute).
			if j+1 < len(s) && s[j+1] == 's' {
				unitMs = 1
				j += 2
			} else {
				unitMs = 60_000
				j++
			}
		case 's':
			unitMs = 1_000
			j++
		case 'h':
			unitMs = 3_600_000
			j++
		case 'd':
			unitMs = 86_400_000
			j++
		case 'w':
			unitMs = 604_800_000
			j++
		case 'y':
			unitMs = 31_536_000_000
			j++
		default:
			return 0, false
		}
		total += n * unitMs
		i = j
	}
	return total, true
}

// base26Encode returns a stable lowercase-letter representation of a
// non-negative integer. Used to produce a digit-free placeholder for
// canonicalised durations (so the strict numericLiteral pass leaves
// the placeholder alone). The empty string never appears — n=0 emits
// "a", which round-trips correctly under the same encoding.
func base26Encode(n int64) string {
	if n == 0 {
		return "a"
	}
	if n < 0 {
		// Canonicalisation should never produce negatives (parser
		// rejects negative durations). Defensive: prefix with 'n'.
		return "n" + base26Encode(-n)
	}
	var b []byte
	for n > 0 {
		b = append(b, byte('a'+(n%26)))
		n /= 26
	}
	// Reverse for stable big-endian-ish output (cosmetic; either order
	// is collision-free).
	for i, j := 0, len(b)-1; i < j; i, j = i+1, j-1 {
		b[i], b[j] = b[j], b[i]
	}
	return string(b)
}

// normaliseConfig carries optional toggles for normaliseExpr. Zero
// value preserves PR-1 strict behaviour (existing callers pass no
// options).
type normaliseConfig struct {
	canonicaliseDurations bool
}

// NormaliseOption mutates a normaliseConfig. Variadic functional
// options keep the public surface compatible — adding new toggles
// in future PRs doesn't break existing callers.
type NormaliseOption func(*normaliseConfig)

// WithCanonicalDurations enables the duration-canonicalisation pass
// before numeric/string stripping. Used by the fuzzy cluster path
// (PR-5) to make `[5m]`, `[300s]`, and `[300000ms]` produce identical
// signatures. Strict callers (PR-1 default) leave it off to preserve
// the conservative "different syntax = different cluster" guarantee.
func WithCanonicalDurations() NormaliseOption {
	return func(c *normaliseConfig) { c.canonicaliseDurations = true }
}

// normaliseExpr produces the cluster signature for one expression.
// Two rules with identical normaliseExpr output cluster together
// (provided their other axes also match — see signatureFor).
//
// Returns "" for empty input. The caller treats empty-signature
// rules as Unclustered.
//
// PR-1 callers invoke as `normaliseExpr(expr)` (no options) and get
// the original strict behaviour. PR-5 (fuzzy) callers add
// `WithCanonicalDurations()` to enable duration-equivalence merging
// (`[5m]` ≡ `[300s]`).
func normaliseExpr(expr string, opts ...NormaliseOption) string {
	if expr == "" {
		return ""
	}
	cfg := normaliseConfig{}
	for _, o := range opts {
		o(&cfg)
	}
	out := expr

	// Pass 0 (fuzzy-only): canonicalise `[<duration>]` tokens to a
	// digit-free placeholder so subsequent passes leave them intact.
	// Must run BEFORE numericLiteral (otherwise `5m` becomes `m` and
	// the duration value is lost).
	if cfg.canonicaliseDurations {
		out = canonicaliseDurations(out)
	}

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
