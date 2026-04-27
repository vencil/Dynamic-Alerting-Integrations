package profile

// PR-3 — PromRule → threshold-exporter conf.d translator.
//
// PROBLEM
// -------
// PR-2's emission ships an *intermediate* artifact tree:
// `_defaults.yaml` carries `shared_expr_template`, `dialect`, etc.,
// but NOT the structured scalar fields (`metric_key: numeric_value`)
// that the threshold-exporter runtime actually consumes via
// ADR-018 deepMerge + config_resolve.go::ResolveAt.
//
// The gap is real because PrometheusRule expressions look like
//   `mysql_global_status_threads_connected > 800`
// while the runtime expects
//   `mysql_connections: 800`
// at the conf.d/ leaf level. Bridging the two needs an AST-aware
// translator that:
//
//   1. Pulls the threshold scalar out of the comparison expression.
//   2. Picks (or derives) a stable scalar `metric_key` for the
//      `_defaults.yaml` / tenant.yaml entry.
//   3. Surfaces severity from rule labels.
//   4. Reports actionable warnings when any of the above is
//      ambiguous so the human reviewer can intervene before the
//      proposal lands as real conf.d/.
//
// SCOPE — see ADR-019 for the full design rationale and the
// non-goals (which expressions we deliberately don't translate).
//
// CONTRACT
// --------
// `TranslateRule` operates on a single ParsedRule and returns a
// RuleTranslation. Pure function; never mutates the input.
//
// `TranslateProposal` aggregates per-rule translations across a
// cluster and decides:
//
//   - The proposal-level metric_key (must be unanimous across
//     members; mismatch is a translation failure surfaced via
//     Warnings, not a panic).
//   - The cluster's default threshold (median of member values
//     for stability against outliers; falls back to first member
//     when len == 1).
//   - Per-tenant override entries (only when their value differs
//     from the cluster default — keeps tenant.yaml minimal).
//
// PR-3 INTENTIONAL NON-GOALS:
//
//   - Multi-comparison expressions (`a > 1 and b > 2`) — too
//     ambiguous; leave for human-driven custom translation.
//   - Histogram quantile bucketing translation.
//   - Auto-rewriting the source PrometheusRule expression to use
//     `user_threshold{}` lookup. Translator surfaces the threshold
//     scalar; rule rewrite is a separate concern out of scope.

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode"

	"github.com/VictoriaMetrics/metricsql"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// MetricKeyLabel is the conventional label customers add to a
// PrometheusRule when they want explicit control over the
// translator's metric_key choice. ADR-019 §metric-key-resolution
// defines the resolution order: explicit label wins; alert/record
// snake-case fallback; opaque-rule fallback last.
const MetricKeyLabel = "metric_key"

// TranslationStatus enumerates how confidently the translator
// could derive a conf.d-ready entry from one rule.
type TranslationStatus string

const (
	// TranslationOK — clean translation: explicit threshold scalar +
	// derivable metric_key + non-ambiguous severity.
	TranslationOK TranslationStatus = "ok"

	// TranslationPartial — translation succeeded but at least one
	// component was derived heuristically (e.g. metric_key from
	// alert name rather than explicit label). Still emit-able.
	TranslationPartial TranslationStatus = "partial"

	// TranslationSkipped — the rule expression doesn't match the
	// PR-3 supported shape (single top-level numeric comparison).
	// The rule passes through to per-tenant raw expr and SHOULD
	// be reviewed by a human before commit. Not an error — many
	// real-world rules legitimately need custom translation.
	TranslationSkipped TranslationStatus = "skipped"
)

// RuleTranslation is the per-rule translator output.
type RuleTranslation struct {
	// SourceRuleID is the original ParsedRule's identity, copied
	// verbatim so cluster aggregation can correlate translations
	// back to their source.
	SourceRuleID string `json:"source_rule_id"`

	// Status — see TranslationStatus consts.
	Status TranslationStatus `json:"status"`

	// MetricKey is the conf.d/ scalar key to publish under
	// `_defaults.yaml` / tenant.yaml. Empty when Status ==
	// TranslationSkipped.
	MetricKey string `json:"metric_key,omitempty"`

	// Threshold is the numeric comparison value the rule asserts.
	// Always populated when Status != TranslationSkipped.
	Threshold float64 `json:"threshold,omitempty"`

	// Operator is the comparison operator from the rule's expression
	// (`>`, `>=`, `<`, `<=`). Surfaced so future emission layers can
	// preserve direction semantics if/when ADR-019 §1 grows from
	// "value-only" to "value+direction".
	Operator string `json:"operator,omitempty"`

	// Severity is the alert severity, derived from rule.Labels.
	// "warning" (default), "critical", or whatever the rule labels.
	// Empty for recording rules.
	Severity string `json:"severity,omitempty"`

	// Warnings collects per-rule heuristic-derivation notes, e.g.
	// "metric_key derived from alert name, not explicit label".
	// Surfaces in PROPOSAL.md so reviewers see the soft spots.
	Warnings []string `json:"warnings,omitempty"`

	// SkipReason populated only when Status == TranslationSkipped,
	// explaining why translation didn't apply (e.g. "expression
	// uses two comparisons", "right-hand side is not a literal
	// number"). Mirrors AnalyzeError's role in the parser package.
	SkipReason string `json:"skip_reason,omitempty"`
}

// TranslateRule converts one ParsedRule into a RuleTranslation.
// Pure; never mutates `rule`.
//
// Errors: only for empty rule.Expr (caller bug). All other failure
// modes are surfaced via Status==TranslationSkipped + SkipReason.
func TranslateRule(rule parser.ParsedRule) (RuleTranslation, error) {
	if rule.Expr == "" {
		return RuleTranslation{}, errors.New("translate: rule.Expr is empty")
	}

	out := RuleTranslation{SourceRuleID: rule.SourceRuleID}

	parsed, err := metricsql.Parse(rule.Expr)
	if err != nil {
		out.Status = TranslationSkipped
		out.SkipReason = fmt.Sprintf("metricsql parse error: %s", err.Error())
		return out, nil
	}

	cmp, found := findTopLevelComparison(parsed)
	if !found {
		out.Status = TranslationSkipped
		out.SkipReason = "no top-level numeric comparison found (translator only handles `<expr> {>|>=|<|<=} <number>` shapes)"
		return out, nil
	}

	out.Threshold = cmp.threshold
	out.Operator = cmp.op

	// metric_key resolution — ADR-019 §metric-key-resolution order.
	out.MetricKey, out.Status, out.Warnings = resolveMetricKey(rule, cmp.metricExpr)

	// Severity from rule labels (default warning).
	out.Severity = severityFromLabels(rule.Labels)

	return out, nil
}

// comparison captures the parts of a top-level binary-op comparison
// the translator cares about.
type comparison struct {
	op         string
	threshold  float64
	metricExpr *metricsql.MetricExpr // the *non*-number side; may be nil for very degenerate cases
}

// findTopLevelComparison walks down the AST root to find the first
// BinaryOpExpr with a comparison operator + a NumberExpr child.
// Returns (zero, false) when no such shape is found.
//
// Strategy: descend through unary parens / `keep_metric_names` style
// wrappers if the AST exposes them (metricsql parser collapses many
// of these; the descent is a defensive measure for forward-compat).
// The first BinaryOpExpr we hit in pre-order traversal is the
// "top-level" comparison.
func findTopLevelComparison(e metricsql.Expr) (comparison, bool) {
	switch x := e.(type) {
	case *metricsql.BinaryOpExpr:
		if isComparisonOp(x.Op) {
			if cmp, ok := comparisonFromBinaryOp(x); ok {
				return cmp, true
			}
		}
		// Not a numeric comparison at this level — keep walking
		// deeper. Real-world examples: `(rate(foo[5m]) > 0.5) and
		// (count(bar) > 0)` — the outer `and` isn't a comparison
		// but its left arm is. We honor the left-first descent so
		// deterministic output for the common nested shape.
		if cmp, ok := findTopLevelComparison(x.Left); ok {
			return cmp, true
		}
		if cmp, ok := findTopLevelComparison(x.Right); ok {
			return cmp, true
		}
	case *metricsql.RollupExpr:
		// `rate(metric[5m]) offset 1m` — descend into the wrapped
		// expr; comparison can't appear inside RollupExpr's window
		// anyway.
		return findTopLevelComparison(x.Expr)
	case *metricsql.FuncExpr:
		// fn(args). Comparisons don't appear directly inside fn args
		// in the shapes we support, but descend into Args[0] so a
		// future fn wrapping a comparison doesn't silently skip.
		for _, a := range x.Args {
			if cmp, ok := findTopLevelComparison(a); ok {
				return cmp, true
			}
		}
	case *metricsql.AggrFuncExpr:
		for _, a := range x.Args {
			if cmp, ok := findTopLevelComparison(a); ok {
				return cmp, true
			}
		}
	}
	return comparison{}, false
}

// comparisonFromBinaryOp inspects a BinaryOpExpr that's already
// known to use a comparison operator and tries to identify the
// `<expr> op <number>` (or `<number> op <expr>`) shape. Returns
// (zero, false) when neither side is a NumberExpr (e.g. the expr
// is `metric > on(tenant) other_metric` — a vector comparison
// that the translator doesn't handle).
func comparisonFromBinaryOp(x *metricsql.BinaryOpExpr) (comparison, bool) {
	if num, ok := x.Right.(*metricsql.NumberExpr); ok {
		return comparison{
			op:         x.Op,
			threshold:  num.N,
			metricExpr: extractFirstMetricExpr(x.Left),
		}, true
	}
	if num, ok := x.Left.(*metricsql.NumberExpr); ok {
		// Inverted form — e.g. `0.5 < rate(foo[5m])`. Flip the
		// operator so downstream consumers see a consistent
		// "metric op threshold" shape.
		return comparison{
			op:         flipComparisonOp(x.Op),
			threshold:  num.N,
			metricExpr: extractFirstMetricExpr(x.Right),
		}, true
	}
	return comparison{}, false
}

// extractFirstMetricExpr finds the first MetricExpr inside an Expr
// tree (for metric-name extraction when deriving fallback metric_key).
// Returns nil when none found (e.g. `vector(0) > 0`).
func extractFirstMetricExpr(e metricsql.Expr) *metricsql.MetricExpr {
	switch x := e.(type) {
	case *metricsql.MetricExpr:
		return x
	case *metricsql.RollupExpr:
		return extractFirstMetricExpr(x.Expr)
	case *metricsql.FuncExpr:
		for _, a := range x.Args {
			if m := extractFirstMetricExpr(a); m != nil {
				return m
			}
		}
	case *metricsql.AggrFuncExpr:
		for _, a := range x.Args {
			if m := extractFirstMetricExpr(a); m != nil {
				return m
			}
		}
	case *metricsql.BinaryOpExpr:
		if m := extractFirstMetricExpr(x.Left); m != nil {
			return m
		}
		return extractFirstMetricExpr(x.Right)
	}
	return nil
}

// isComparisonOp returns true for operators the translator
// recognises as numeric thresholds. `==`/`!=` are deliberately
// excluded — equality on a numeric metric vs scalar is rarely a
// "threshold" semantics; ADR-019 lists this as an explicit non-goal.
func isComparisonOp(op string) bool {
	switch op {
	case ">", ">=", "<", "<=":
		return true
	}
	return false
}

// flipComparisonOp swaps the operator when the threshold appears on
// the LHS of the comparison, e.g. `0.5 < x` is semantically `x >
// 0.5`. Keeping consumers oblivious to which side the literal sits
// on simplifies emission.
func flipComparisonOp(op string) string {
	switch op {
	case ">":
		return "<"
	case ">=":
		return "<="
	case "<":
		return ">"
	case "<=":
		return ">="
	}
	return op
}

// resolveMetricKey applies the ADR-019 §metric-key-resolution order:
//
//  1. Explicit label `metric_key: <value>` on the rule wins. No
//     warning needed — the customer chose this on purpose.
//  2. Snake-case of rule.Alert (or rule.Record). Surface a warning
//     so reviewers know it was derived (cosmetic drift in the alert
//     name → silent metric_key drift).
//  3. Inner metric name from the expression's first MetricExpr.
//     Last resort; warning level escalates.
//  4. Empty string → Status == TranslationSkipped. The translator
//     refuses to make up a key with no semantic anchoring.
//
// Returns the resolved key, the resulting Status (OK if explicit,
// Partial if heuristic), and warnings to attach to the
// RuleTranslation.
func resolveMetricKey(rule parser.ParsedRule, metricExpr *metricsql.MetricExpr) (string, TranslationStatus, []string) {
	if v, ok := rule.Labels[MetricKeyLabel]; ok && v != "" {
		return v, TranslationOK, nil
	}
	var warnings []string
	if rule.Alert != "" {
		key := snakeCaseIdentifier(rule.Alert)
		if key != "" {
			warnings = append(warnings, fmt.Sprintf(
				"metric_key %q derived from alert name (no explicit `%s` label)",
				key, MetricKeyLabel))
			return key, TranslationPartial, warnings
		}
	}
	if rule.Record != "" {
		key := snakeCaseIdentifier(rule.Record)
		if key != "" {
			warnings = append(warnings, fmt.Sprintf(
				"metric_key %q derived from record name (no explicit `%s` label)",
				key, MetricKeyLabel))
			return key, TranslationPartial, warnings
		}
	}
	if metricExpr != nil {
		if name := metricNameOf(metricExpr); name != "" {
			warnings = append(warnings, fmt.Sprintf(
				"metric_key %q derived from inner metric name (no explicit `%s` label, no alert/record name)",
				name, MetricKeyLabel))
			return name, TranslationPartial, warnings
		}
	}
	return "", TranslationSkipped, []string{
		"could not derive metric_key: rule has no `metric_key` label, no alert/record name, and no recognisable inner metric",
	}
}

// metricNameOf reads the `__name__` filter out of a MetricExpr's
// label-filter set. metricsql exposes this as the FIRST entry of
// the inner-most LabelFilter slice when present.
func metricNameOf(m *metricsql.MetricExpr) string {
	if m == nil || len(m.LabelFilterss) == 0 {
		return ""
	}
	first := m.LabelFilterss[0]
	if len(first) == 0 {
		return ""
	}
	if first[0].Label == "__name__" {
		return first[0].Value
	}
	return ""
}

// snakeCaseIdentifier converts a rule name like `MySQLHighConnections`
// into a stable conf.d-shape key like `my_sql_high_connections`. Best
// effort; preserves embedded digits and accepts existing snake_case
// input as a no-op.
//
// The acronym boundary is the tricky case: in `MySQLHigh`, the `H`
// is the start of a new word even though it's preceded by another
// uppercase. Detect this with one-char lookahead: insert `_` before
// an upper rune when prev is upper AND next is lower. This catches
// `MySQL→High`, `JSONParser→Output`, etc.
func snakeCaseIdentifier(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	runes := []rune(s)
	var b strings.Builder
	b.Grow(len(s) + 4)
	for i, r := range runes {
		switch {
		case r == '-' || r == ' ' || r == '/' || r == '.':
			if b.Len() > 0 && b.String()[b.Len()-1] != '_' {
				b.WriteByte('_')
			}
		case unicode.IsUpper(r):
			if i > 0 {
				prev := runes[i-1]
				var next rune
				if i+1 < len(runes) {
					next = runes[i+1]
				}
				switch {
				// CamelCase boundary: lowercase or digit → upper.
				case unicode.IsLower(prev) || unicode.IsDigit(prev):
					b.WriteByte('_')
				// Acronym→Word boundary: UPPER UPPER lower (like
				// `SQL` followed by `High` in `MySQLHigh`). Insert
				// `_` before the LAST upper of the run so the new
				// word starts cleanly.
				case unicode.IsUpper(prev) && unicode.IsLower(next):
					b.WriteByte('_')
				}
			}
			b.WriteRune(unicode.ToLower(r))
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(r)
		case r == '_':
			b.WriteByte('_')
		default:
			// Drop anything else (parens, quotes, symbols).
		}
	}
	out := b.String()
	out = strings.Trim(out, "_")
	for strings.Contains(out, "__") {
		out = strings.ReplaceAll(out, "__", "_")
	}
	return out
}

// severityFromLabels picks the alert severity. Convention:
// labels.severity wins; absence defaults to "warning" for alert
// rules, empty for recording rules (the latter have no severity
// concept).
func severityFromLabels(labels map[string]string) string {
	if v, ok := labels["severity"]; ok && v != "" {
		return v
	}
	return ""
}

// ---------------------------------------------------------------
// Cluster-level translation
// ---------------------------------------------------------------

// ProposalTranslation aggregates RuleTranslation per cluster
// member into a single conf.d-ready emission shape:
//
//   - MetricKey is the unanimous metric_key across translatable
//     members. Mismatch produces Warnings + Status drops to
//     TranslationPartial; the proposal still emits using the
//     majority key but reviewers see the dissent.
//   - DefaultThreshold is the median (or single value when len==1)
//     of member thresholds. Resilient to one-off outliers.
//   - PerTenantOverrides maps tenant-id → threshold for tenants
//     whose value diverges from DefaultThreshold. Tenants matching
//     the default are absent from the map (deepMerge falls through
//     to _defaults.yaml).
//   - Severity is the unanimous severity across members; mismatch
//     downgrades to Partial with a warning.
type ProposalTranslation struct {
	Status             TranslationStatus  `json:"status"`
	MetricKey          string             `json:"metric_key,omitempty"`
	DefaultThreshold   float64            `json:"default_threshold,omitempty"`
	Operator           string             `json:"operator,omitempty"`
	Severity           string             `json:"severity,omitempty"`
	PerTenantOverrides map[string]float64 `json:"per_tenant_overrides,omitempty"`
	MemberStatuses     []RuleTranslation  `json:"member_statuses,omitempty"`
	Warnings           []string           `json:"warnings,omitempty"`
}

// TranslateProposal applies TranslateRule to every member of a
// proposal and produces the cluster-level summary. tenantKey is
// the label pickTenantLabelKey resolved (so tenant-id is read off
// rule.Labels[tenantKey] consistently with the per-tenant
// emission path).
//
// Returns Status TranslationSkipped when ZERO members translated
// successfully — there's no honest way to emit a default in that
// case, and the proposal should fall back to PR-2 intermediate
// emission.
func TranslateProposal(prop ExtractionProposal, members []parser.ParsedRule, tenantKey string) (*ProposalTranslation, error) {
	if len(members) == 0 {
		return nil, errors.New("translate: proposal has zero members")
	}

	out := &ProposalTranslation{
		PerTenantOverrides: make(map[string]float64),
	}
	out.MemberStatuses = make([]RuleTranslation, 0, len(members))

	// Per-member translation.
	keyVotes := make(map[string]int)
	severityVotes := make(map[string]int)
	opVotes := make(map[string]int)
	var thresholds []float64
	type tenantValue struct {
		tenant string
		value  float64
	}
	var tenantValues []tenantValue

	translatedCount := 0
	for _, m := range members {
		t, err := TranslateRule(m)
		if err != nil {
			// Translator only errors on empty Expr — propagate so
			// the caller sees a contract violation, rather than
			// silently treating it like a TranslationSkipped.
			return nil, fmt.Errorf("translate member %q: %w", m.SourceRuleID, err)
		}
		out.MemberStatuses = append(out.MemberStatuses, t)
		if t.Status == TranslationSkipped {
			continue
		}
		translatedCount++
		keyVotes[t.MetricKey]++
		opVotes[t.Operator]++
		if t.Severity != "" {
			severityVotes[t.Severity]++
		}
		thresholds = append(thresholds, t.Threshold)

		tenantID := ""
		if tenantKey != "" {
			tenantID = m.Labels[tenantKey]
		}
		if tenantID != "" {
			tenantValues = append(tenantValues, tenantValue{tenant: tenantID, value: t.Threshold})
		}
	}

	if translatedCount == 0 {
		out.Status = TranslationSkipped
		out.Warnings = append(out.Warnings,
			"no member rule translated successfully; proposal falls back to intermediate emission")
		return out, nil
	}

	// Pick winner (and detect dissent) for each axis.
	out.MetricKey = pickMajority(keyVotes)
	out.Operator = pickMajority(opVotes)
	out.Severity = pickMajority(severityVotes)

	if len(keyVotes) > 1 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"metric_key not unanimous across %d translated members: %s; using majority %q",
			translatedCount, formatVotes(keyVotes), out.MetricKey))
	}
	if len(opVotes) > 1 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"comparison operator not unanimous: %s; using majority %q",
			formatVotes(opVotes), out.Operator))
	}
	if len(severityVotes) > 1 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"severity not unanimous: %s; using majority %q",
			formatVotes(severityVotes), out.Severity))
	}

	// Default threshold = median (stable against single-tenant
	// extreme values).
	out.DefaultThreshold = median(thresholds)

	// Per-tenant overrides — only when value differs from default.
	for _, tv := range tenantValues {
		if tv.value != out.DefaultThreshold {
			out.PerTenantOverrides[tv.tenant] = tv.value
		}
	}

	// Final status: OK if every translated member returned OK and
	// no axis required majority resolution; Partial otherwise.
	out.Status = TranslationOK
	if len(keyVotes) > 1 || len(opVotes) > 1 || len(severityVotes) > 1 {
		out.Status = TranslationPartial
	}
	for _, m := range out.MemberStatuses {
		if m.Status == TranslationPartial {
			out.Status = TranslationPartial
			break
		}
	}
	if translatedCount < len(members) {
		out.Status = TranslationPartial
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"%d of %d members were skipped (see MemberStatuses for individual SkipReason)",
			len(members)-translatedCount, len(members)))
	}

	return out, nil
}

// pickMajority returns the key with the highest count. Ties broken
// alphabetically for stable output. Empty map → "".
func pickMajority(votes map[string]int) string {
	if len(votes) == 0 {
		return ""
	}
	type kv struct {
		k string
		v int
	}
	pairs := make([]kv, 0, len(votes))
	for k, v := range votes {
		pairs = append(pairs, kv{k, v})
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].v != pairs[j].v {
			return pairs[i].v > pairs[j].v
		}
		return pairs[i].k < pairs[j].k
	})
	return pairs[0].k
}

// formatVotes renders a sorted "key=count, key=count" string for
// stable warning messages.
func formatVotes(votes map[string]int) string {
	keys := make([]string, 0, len(votes))
	for k := range votes {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%q=%d", k, votes[k])
	}
	return strings.Join(parts, ", ")
}

// median returns the middle value of a slice. For even-length
// slices, returns the lower-of-two-middles (deterministic, no
// floating-point averaging surprises like 79.5 vs 80).
func median(xs []float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	cp := append([]float64(nil), xs...)
	sort.Float64s(cp)
	return cp[(len(cp)-1)/2]
}
