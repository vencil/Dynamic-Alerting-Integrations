// Package parser converts Prometheus / VictoriaMetrics rule artifacts
// (PrometheusRule CRDs and similar YAML shapes) into a canonical
// ParsedRule record consumed by the v2.8.0 Phase .c migration toolkit
// (C-9 Profile Builder, C-10 Batch PR Pipeline).
//
// PR-1 ships the parser core + dialect detection. Subsequent PRs add:
//   - PR-2: external `vm_only_functions.yaml` + freshness CI gate,
//     `prom_compatible: bool` (requires prometheus/promql/parser dep),
//     CLI `da-tools parser import`.
//
// Single source of truth for "what makes a rule MetricsQL-only" is
// vm_only_functions.go in this package. Keeping the allowlist tight is
// a hard requirement for C-8's "anti-vendor-lock-in" promise — any
// rule we mis-classify as `prom` may surprise customers when they try
// to roll back to vanilla Prometheus.
package parser

// Dialect labels which rule language a parsed expression most closely
// matches. The classification governs whether C-9 / C-10 will treat
// the rule as portable to vanilla Prometheus.
type Dialect string

const (
	// DialectProm — expression parses with metricsql AND uses no
	// VM-only functions. Safely portable to vanilla Prometheus.
	DialectProm Dialect = "prom"

	// DialectMetricsQL — expression uses one or more VM-only
	// functions (per vm_only_functions.go). NOT portable.
	DialectMetricsQL Dialect = "metricsql"

	// DialectAmbiguous — metricsql parser failed (syntax error,
	// unrecognised function name, etc.). Caller must surface for
	// human review; never auto-converted by C-9.
	DialectAmbiguous Dialect = "ambiguous"
)

// ParsedRule is the canonical output for one alerting or recording
// rule. Field naming follows the upstream PrometheusRule CRD
// (`Alert` / `Record` / `Expr` / `For` / `Labels` / `Annotations`)
// plus dialect classification metadata added by this parser.
//
// Exactly one of Alert / Record is populated for a well-formed rule;
// both empty signals a malformed rule (parser preserves it but emits
// a warning).
type ParsedRule struct {
	Alert       string            `json:"alert,omitempty"`
	Record      string            `json:"record,omitempty"`
	Expr        string            `json:"expr"`
	For         string            `json:"for,omitempty"`
	Labels      map[string]string `json:"labels,omitempty"`
	Annotations map[string]string `json:"annotations,omitempty"`

	// SourceRuleID is a stable pointer back to the source location,
	// shaped as `<file-or-doc>#groups[i].rules[j]`. C-10's
	// `batch-pr refresh --source-rule-ids` indexes against this so
	// data-layer hot-fixes can target the affected subset without
	// rerunning the full pipeline.
	SourceRuleID string `json:"source_rule_id"`

	// Dialect classification (see consts).
	Dialect Dialect `json:"dialect"`

	// VMOnlyFunctions lists every function in this rule's expression
	// that is not part of vanilla PromQL. Sorted for stable diffs.
	// Empty when Dialect == DialectProm. Populated even when
	// Dialect == DialectAmbiguous if any partial match was detected
	// (best-effort signal for the human reviewer).
	VMOnlyFunctions []string `json:"vm_only_functions,omitempty"`

	// PromPortable is true when the rule can be evaluated by a
	// vanilla Prometheus server unchanged: parses with metricsql AND
	// uses no VM-only functions. Convenience flag for C-9 / C-10
	// consumers that don't want to switch on Dialect themselves.
	PromPortable bool `json:"prom_portable"`

	// AnalyzeError, when non-empty, is the parser error message from
	// metricsql.Parse for a DialectAmbiguous rule. Surfaced so the
	// reviewer can see *why* the rule was rejected, not just that it
	// was. Empty for prom / metricsql dialects.
	AnalyzeError string `json:"analyze_error,omitempty"`
}

// Provenance stamps where a ParsedRule came from + when + with
// which parser version. C-10's `refresh --source-rule-ids` reads
// SourceRuleID; the rest is for human auditing during incidents.
//
// Stamped at ParseResult level (not per-rule) since every rule in a
// single Parse call shares the same provenance.
type Provenance struct {
	// GeneratedBy identifies the tool + version. Conventional
	// format: `da-tools@tools-vX.Y.Z parser@<git-sha>`. ParsePromRules
	// stores whatever the caller passes verbatim; the future CLI
	// subcommand populates it from build metadata, library callers
	// supply their own identifier.
	GeneratedBy string `json:"generated_by"`

	// SourceFile is the path / URI the rules came from. Stamped on
	// every Parse call even when ambiguous.
	SourceFile string `json:"source_file"`

	// ParsedAt is RFC 3339 in UTC.
	ParsedAt string `json:"parsed_at"`

	// SourceChecksum is the SHA-256 (full 64 hex) of the raw input
	// bytes the parser saw. Lets C-10 detect "the source moved
	// underneath us between the dry-run and the real apply".
	SourceChecksum string `json:"source_checksum"`
}

// ParseResult is the top-level return from ParsePromRules.
type ParseResult struct {
	Provenance Provenance   `json:"provenance"`
	Rules      []ParsedRule `json:"rules"`

	// Warnings collects non-fatal issues encountered during parse:
	// missing alert/record name, unknown rule shape, etc. Fatal
	// parse errors (malformed YAML, missing `spec.groups`) return
	// an error from ParsePromRules instead.
	Warnings []string `json:"warnings,omitempty"`
}
