package parser

// VictoriaMetrics-only function names (PR-1 seed allowlist).
//
// This is the list of MetricsQL function identifiers that have NO
// counterpart in vanilla PromQL. A rule using any of these is marked
// `dialect=metricsql` + `prom_portable=false` and cannot be safely
// migrated back to a stock Prometheus server.
//
// Source curation: cross-referenced from
// https://docs.victoriametrics.com/victoriametrics/metricsql/ as of
// MetricsQL v0.87.0 (the version pinned in go.mod). Categories:
//
//   - rollup_*           — VM rollup analogues
//   - range_*            — sliding-window aggregators VM added
//   - *_over_time exotic — VM extensions beyond the PromQL set
//   - histogram_* exotic — VM histogram analytics PromQL doesn't have
//   - keep_last_value /
//     interpolate /
//     remove_resets etc. — VM-specific gauge utilities
//   - label_set / label_copy / label_move — VM label rewriters
//   - bitmap_*           — VM bitmap operators
//   - at / step / start /
//     end / now           — VM time-context functions
//
// The seed is intentionally conservative: false positives (mark a fn
// as VM-only when it's actually portable) cost a customer one extra
// manual review per rule; false negatives (miss a real VM-only fn)
// cost the customer an Alertmanager outage when they roll back to
// Prometheus. Keep extending this list as we encounter rules in the
// wild.
//
// PR-2 promotes this to an external `vm_only_functions.yaml` with a
// CI freshness gate (`test_vm_only_functions_freshness.go`) that
// diffs against the metricsql release in go.mod and fails when an
// upstream version-bump introduces new function names not yet
// classified here. Until then this hardcoded set is the single
// source of truth — any change requires a code review.

// vmOnlyFuncs lists every MetricsQL function name that is NOT also a
// valid PromQL function. Membership is checked case-insensitively
// because metricsql's own parser lowercases function names before
// dispatch.
//
// **Conservative bias is deliberate.** Several entries below
// (`histogram_avg`, `histogram_stddev`, `histogram_stdvar`,
// `last_over_time`, `first_over_time`) became available in modern
// Prometheus releases (2.40+). We keep them here so that customers
// running older Prometheus servers — the realistic rollback target
// for our anti-vendor-lock-in promise — get a "manually review
// portability" signal rather than an outage. Cost: one extra review
// per affected rule. The PR-2 freshness gate will let us narrow this
// list per "minimum supported Prometheus version" if a customer
// commits to a newer floor.
//
// CAUTION when adding entries: list only function-call names that
// actually appear in `metricsql.FuncExpr.Name` or
// `metricsql.AggrFuncExpr.Name`. Keywords (`with`, modifier verbs
// like `offset`/`@`) are parsed as separate Expr shapes and never
// reach the function-name visitor; adding them here is dead code.
var vmOnlyFuncs = map[string]struct{}{
	// rollup_* family
	"rollup":                 {},
	"rollup_rate":            {},
	"rollup_deriv":           {},
	"rollup_increase":        {},
	"rollup_delta":           {},
	"rollup_candlestick":     {},
	"rollup_scrape_interval": {},

	// range_* (sliding-window over series)
	"range_first":             {},
	"range_last":              {},
	"range_avg":               {},
	"range_sum":               {},
	"range_min":               {},
	"range_max":               {},
	"range_median":            {},
	"range_quantile":          {},
	"range_stddev":            {},
	"range_stdvar":            {},
	"range_trim_outliers":     {},
	"range_trim_spikes":       {},
	"range_trim_zscore":       {},
	"range_zscore":            {},
	"range_linear_regression": {},
	"range_normalize":         {},
	"range_mad":               {},
	"range_over_time":         {},

	// *_over_time MetricsQL-only extensions
	"quantiles_over_time":           {},
	"histogram_quantiles_over_time": {},
	"geomean_over_time":             {},
	"mode_over_time":                {},
	"share_le_over_time":            {},
	"share_gt_over_time":            {},
	"share_eq_over_time":            {},
	"count_le_over_time":            {},
	"count_gt_over_time":            {},
	"count_eq_over_time":            {},
	"count_ne_over_time":            {},
	"sum_eq_over_time":              {},
	"sum_gt_over_time":              {},
	"sum_le_over_time":              {},
	"zscore_over_time":              {},
	"first_over_time":               {},
	"last_over_time":                {},
	"distinct_over_time":            {},
	"increases_over_time":           {},
	"decreases_over_time":           {},
	"duration_over_time":            {},
	"lag":                           {},
	"lifetime":                      {},
	"tlast_change_over_time":        {},
	"tfirst_over_time":              {},
	"tmin_over_time":                {},
	"tmax_over_time":                {},
	"mad_over_time":                 {},
	"median_over_time":              {},
	"outlier_iqr_over_time":         {},

	// VM histogram analytics (PromQL only has histogram_quantile)
	"histogram_share":  {},
	"histogram_avg":    {},
	"histogram_stddev": {},
	"histogram_stdvar": {},

	// VM gauge utilities
	"keep_last_value": {},
	"keep_next_value": {},
	"interpolate":     {},
	"remove_resets":   {},
	"running_sum":     {},
	"running_avg":     {},
	"running_min":     {},
	"running_max":     {},
	"default_rollup":  {},

	// VM label rewriters
	"label_set":            {},
	"label_del":            {},
	"label_keep":           {},
	"label_copy":           {},
	"label_move":           {},
	"label_lowercase":      {},
	"label_uppercase":      {},
	"label_replace_strict": {},
	"label_match":          {},
	"label_mismatch":       {},
	"label_value":          {},
	"label_transform":      {},
	"label_graphite_group": {},

	// VM bitmap operators
	"bitmap_and": {},
	"bitmap_or":  {},
	"bitmap_xor": {},

	// VM time-context (PromQL has no equivalents)
	"at":    {},
	"step":  {},
	"start": {},
	"end":   {},
	"now":   {},

	// Other VM-specific
	"alias":                      {},
	"limit_offset":               {},
	"prometheus_buckets":         {},
	"buckets_limit":              {},
	"sort_by_label":              {},
	"sort_by_label_desc":         {},
	"sort_by_label_numeric":      {},
	"sort_by_label_numeric_desc": {},
	"smooth_exponential":         {},
	"union":                      {},
}

// IsVMOnlyFunction reports whether the given function name is known
// to belong only to MetricsQL (not vanilla PromQL).
//
// The check is case-insensitive; metricsql normalises function names
// to lower-case before dispatch, so "Rollup_Rate" and "rollup_rate"
// behave identically and we match accordingly.
func IsVMOnlyFunction(name string) bool {
	_, ok := vmOnlyFuncs[lowerASCII(name)]
	return ok
}

// lowerASCII is a tiny ASCII-only lower-caser used to avoid pulling
// in unicode tables for function-name normalisation. PromQL /
// MetricsQL function names are by spec ASCII identifiers.
func lowerASCII(s string) string {
	out := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		out[i] = c
	}
	return string(out)
}
