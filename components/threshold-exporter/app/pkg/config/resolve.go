package config

import (
	"fmt"
	"log"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// --- ADR-024 Version-Aware Threshold: dimensional `version` label guard ---
//
// versionLabelPattern is the Phase-1 baseline charset for a `version`
// dimensional label value. It is INTENTIONALLY pilot-calibratable (ADR-024
// OQ-6): real app.kubernetes.io/version strings observed in the pilot may
// carry uppercase letters or long Git SHAs, in which case this is widened
// after observation. Kept lowercase-anchored for now to stay conservative.
const versionLabelPattern = `^[a-z0-9][a-z0-9._-]*$`

var versionLabelRe = regexp.MustCompile(versionLabelPattern)

// pilotVersionMetrics is the Phase-1 allow-list of "<component>/<metric>"
// identities on which a `version` dimensional label is in scope (ADR-024
// OQ-6 component scoping = rule-pack-kubernetes container cpu/memory). A
// version label on any other metric risks cross-pack double-count
// (a non-pilot pack's `sum by(tenant)` would fan across versions) and is
// flagged so the guard can reject it before it reaches a shared series.
var pilotVersionMetrics = map[string]bool{
	"container/cpu":    true,
	"container/memory": true,
}

// ResolveStats carries side-channel observability data for ResolveAtWithStats
// (issue #652). The runtime per-tenant cardinality cap at resolve.go silently
// truncates the result slice when a tenant exceeds max_metrics_per_tenant;
// PerTenantOverLimit surfaces the truncation magnitude (count - limit) so
// the collector can publish `da_tenant_metrics_over_limit{tenant}` for
// alerting on the silent-failure path.
//
// Every tenant present in the post-Resolve view appears as a key — value
// is 0 for compliant tenants (state-coded gauge semantics, see #652 design).
// A tenant that has been deleted from config simply will not appear; the
// collector must Reset() the GaugeVec before applying these values so
// removed tenants' previous gauge entries are evicted.
type ResolveStats struct {
	PerTenantOverLimit map[string]int
	// #741 S3a: per-tenant count of malformed _custom_alerts entries
	// (dropped). Drives the da_custom_alert_parse_errors gauge.
	PerTenantCustomAlertErrors map[string]int
	// #1231: per-tenant count of deprecated (aliased) key spellings still
	// present in the tenant's own config (e.g. mysql_cpu after the
	// mysql_threads_running rename). Drives the da_config_deprecated_keys
	// gauge — migration-progress observability only, deliberately NOT wired
	// to any alert. Tenants with zero deprecated keys have no entry (the
	// gauge simply doesn't emit for them, mirroring the ConstMetric
	// per-scrape pattern of da_custom_alert_parse_errors).
	PerTenantDeprecatedKeys map[string]int
	// ADR-031: raw slo_burn_rate objectives (percentage) carried out of resolve
	// time for the user_slo_objective{tenant, recipe_id} gauge. One entry per
	// valid slo declaration; objective:"disable" contributes none (data-plane
	// opt-out, matching its absent user_threshold rows).
	SloObjectives []ResolvedSloObjective
}

// Resolve applies three-state logic using the current time.
// Wraps ResolveAt(time.Now()) for backward compatibility.
func (c *ThresholdConfig) Resolve() []ResolvedThreshold {
	return c.ResolveAt(time.Now())
}

// ResolveAt applies three-state logic at a specific time.
// The time parameter enables deterministic testing of time-window overrides (B4).
//
//   - custom value → use it (with time-window resolution)
//   - omitted      → use default
//   - "disable"    → skip (no metric exposed)
//
// Multi-tier severity: tenants can set <metric>_critical in their overrides
// to expose a separate critical-severity threshold for the same metric.
// The base metric retains severity=warning; the _critical variant gets severity=critical.
// PromQL can then use `unless` to suppress warning when critical fires.
//
// Returns the list of thresholds to expose as Prometheus metrics.
//
// Equivalent to ResolveAtWithStats(now) with the stats return value
// discarded. Production callers that need the per-tenant cardinality view
// (the threshold-exporter Prometheus collector) should call the stats
// variant directly; tests / debug handlers that do not care about
// observability stats can keep using this signature.
func (c *ThresholdConfig) ResolveAt(now time.Time) []ResolvedThreshold {
	result, _ := c.ResolveAtWithStats(now)
	return result
}

// ResolveAtWithStats is identical to ResolveAt but additionally returns
// per-tenant cardinality observations (#652). See ResolveStats for shape.
//
// The threshold-exporter collector uses the stats return value to drive
// `da_tenant_metrics_over_limit{tenant}` (state-coded gauge); compliant
// tenants appear with value 0 so the collector's per-scrape Reset+Set
// loop correctly evicts vanished tenants and clears gauges for tenants
// that have just dropped back below the limit.
func (c *ThresholdConfig) ResolveAtWithStats(now time.Time) ([]ResolvedThreshold, ResolveStats) {
	var result []ResolvedThreshold

	// Cardinality limit per tenant (0 = no limit)
	limit := c.MaxMetricsPerTenant
	if limit == 0 {
		limit = DefaultMaxMetricsPerTenant
	}
	tenantCount := make(map[string]int)
	// #652: per-tenant over-limit magnitudes for the
	// da_tenant_metrics_over_limit gauge. Populated for every visited
	// tenant — compliant tenants get 0 so the collector's Reset+Set loop
	// clears stale gauges for tenants that just dropped back below the cap.
	perTenantOverLimit := make(map[string]int, len(c.Tenants))
	// #741 S3a: per-tenant malformed _custom_alerts count for the
	// da_custom_alert_parse_errors gauge (fail-loud — a bad declaration is
	// dropped but never silent: the gauge surfaces it for an operational alert).
	perTenantCustomAlertErrors := make(map[string]int, len(c.Tenants))
	// ADR-031: slo_burn_rate objectives for the user_slo_objective gauge.
	var sloObjectives []ResolvedSloObjective
	// #1231: per-tenant deprecated-spelling counts for the
	// da_config_deprecated_keys gauge (only tenants with >=1 entry appear).
	perTenantDeprecatedKeys := make(map[string]int)

	// #1231 alias canonicalization happens HERE, at the resolve boundary,
	// before any key→label derivation (parseMetricKey is untouched). Both
	// maps get a canonical VIEW — the customer-managed _defaults.yaml may
	// still carry the old spelling too — and the parsed config is never
	// mutated, so raw views (GET /{id}) keep showing the file verbatim.
	// canonicalizeDefaults/Overrides also dedup "both spellings present"
	// (canonical wins), the explicit guard against emitting two rows with
	// identical label sets, which would 500 the whole Prometheus Gather.
	canonDefaults := canonicalizeDefaults(c.Defaults)

	for tenant, overrides := range c.Tenants {
		startIdx := len(result) // track where this tenant's metrics start

		canonOverrides, deprecatedCount := canonicalizeOverrides(overrides)
		if deprecatedCount > 0 {
			perTenantDeprecatedKeys[tenant] = deprecatedCount
		}

		// Phase 2A: base thresholds (three-state + inline severity suffix),
		// Phase 2A-crit: <metric>_critical variants, Phase 2B: dimensional
		// {label="v"}/{label=~"re"} overrides. Each phase is a verbatim
		// extraction appended in the original order; intra-segment order is
		// otherwise governed by Go map iteration (non-deterministic, as before)
		// and the cardinality sort below.
		result = append(result, c.resolveBaseRows(tenant, canonDefaults, canonOverrides, now)...)
		result = append(result, c.resolveCriticalRows(tenant, canonDefaults, canonOverrides, now)...)
		result = append(result, c.resolveDimensionalRows(tenant, canonOverrides, now)...)

		// #741 S3a: tenant-authored custom alerts → user_threshold{component="custom",
		// recipe_id,name,mode}. Appended into this tenant's segment BEFORE the
		// cardinality guard so they count toward the cap and truncate
		// deterministically alongside regular thresholds (truncationSortKey folds
		// CustomLabels via canonicalLabelKey, so recipe_id/name/mode keep the
		// ordering total + stable). Malformed entries are dropped + counted.
		caRows, caObjs, caErrs := resolveTenantCustomAlerts(tenant, overrides)
		if len(caRows) > 0 || caErrs > 0 {
			result = append(result, caRows...)
			perTenantCustomAlertErrors[tenant] = caErrs
		}

		// Cardinality guard: enforce per-tenant metric limit (v1.5.0)
		count := len(result) - startIdx
		tenantCount[tenant] = count
		// #652: record over-limit magnitude (compliant tenants → 0) for
		// the gauge. Recorded BEFORE truncation so the magnitude reflects
		// what the tenant tried to emit, not the (already-truncated)
		// observed slice length. Effective limit (post-MaxMetricsPerTenant
		// fallback to DefaultMaxMetricsPerTenant) is used so the gauge
		// aligns with the actual runtime cap, never the unset-zero literal.
		overflow := 0
		if limit > 0 && count > limit {
			overflow = count - limit
			// ADR-024 AC-7: deterministic truncation. The slice order above
			// reflects Go map iteration over Defaults/overrides, which is
			// randomized per process — so without sorting, an over-cap tenant
			// would have a DIFFERENT subset truncated on every scrape, making
			// the surviving alert series flap in and out (Prometheus alert
			// flapping + PagerDuty repeat-fire). Sort this tenant's segment by
			// a stable identity key BEFORE truncating: unversioned / default
			// thresholds are protected (sort first, always kept); explicitly
			// versioned ones are dropped from the lexicographic tail, so the
			// dropped version is the same on every scrape (stable disappearance
			// → fires the over-limit gauge predictably, never flaps).
			seg := result[startIdx:]
			sort.SliceStable(seg, func(i, j int) bool {
				return truncationSortKey(seg[i]) < truncationSortKey(seg[j])
			})
			log.Printf("ERROR: tenant=%s produced %d metrics (limit=%d), truncating to limit", tenant, count, limit)
			result = result[:startIdx+limit]
		}
		perTenantOverLimit[tenant] = overflow

		// ADR-031: keep the user_slo_objective gauge aligned with the
		// truncated threshold rows. caObjs was collected BEFORE the
		// cardinality cut above, so for an over-cap tenant an slo shape whose
		// user_threshold rows were just truncated away would still publish
		// its objective gauge — a gauge for a rule that can never fire.
		// Filter objectives down to shapes with >=1 SURVIVING custom row
		// (recipe_id match within this tenant's post-truncation segment).
		if len(caObjs) > 0 {
			if overflow > 0 {
				alive := make(map[string]bool)
				for _, rt := range result[startIdx:] {
					if rt.Component == "custom" {
						alive[rt.CustomLabels["recipe_id"]] = true
					}
				}
				kept := caObjs[:0]
				for _, o := range caObjs {
					if alive[o.RecipeID] {
						kept = append(kept, o)
					}
				}
				caObjs = kept
			}
			sloObjectives = append(sloObjectives, caObjs...)
		}
	}

	return result, ResolveStats{
		PerTenantOverLimit:         perTenantOverLimit,
		PerTenantCustomAlertErrors: perTenantCustomAlertErrors,
		PerTenantDeprecatedKeys:    perTenantDeprecatedKeys,
		SloObjectives:              sloObjectives,
	}
}

// isThresholdExpired reports whether a time-boxed threshold override (PREVENT
// #656) has passed its `expires:` instant. A malformed expires fails OPEN (the
// override is kept — mirrors ResolveMaintenanceExpiriesAt; ValidateTenantKeys
// warns on it). Empty expires = a permanent override (never expires).
func isThresholdExpired(sv ScheduledValue, now time.Time) bool {
	if sv.Expiry == nil || sv.Expiry.Expires == "" {
		return false
	}
	t, err := time.Parse(time.RFC3339, sv.Expiry.Expires)
	if err != nil {
		return false // fail-open; validation surfaces the malformed value
	}
	return now.After(t)
}

// resolveBaseRows resolves a tenant's base thresholds from the given defaults
// view using the three-state contract (custom value / omitted→default /
// disable) plus the inline "value:severity" suffix. Extracted verbatim from
// the ResolveAtWithStats per-tenant loop (Phase 2A) — see that method for the
// full contract.
//
// #1231: both maps arrive pre-canonicalized (deprecated alias spellings folded
// onto their canonical key), and every append goes through appendWithLegacyTwin
// so alias targets dual-emit a legacy-identity row during the transition
// window. disable still suppresses BOTH rows (no canonical row → no twin).
func (c *ThresholdConfig) resolveBaseRows(tenant string, defaults map[string]float64, overrides map[string]ScheduledValue, now time.Time) []ResolvedThreshold {
	var rows []ResolvedThreshold
	for metricKey, defaultValue := range defaults {
		// Skip _state_ / _silent_ / _severity_dedup / _routing keys — handled
		// by ResolveStateFilters() / ResolveSilentModes() / ResolveSeverityDedup()
		// / ResolveRouting() respectively.
		if strings.HasPrefix(metricKey, "_state_") || strings.HasPrefix(metricKey, "_silent_") ||
			metricKey == "_severity_dedup" || strings.HasPrefix(metricKey, "_routing") {
			continue
		}

		// Parse metric key: "mysql_connections" → component="mysql", metric="connections"
		component, metric := parseMetricKey(metricKey)
		severity := "warning" // default severity

		// Check tenant override (skip _state_ overrides).
		// PREVENT #656: a time-boxed override past its expires: instant is treated
		// as absent → falls through to State 2 (platform default). The value still
		// emits (with the default) so this is fail-safe (more protection, never
		// silent) and leaves the cardinality count unchanged; collectThresholdExpiries
		// emits da_config_event so a cleanup PR removes the stale YAML.
		if sv, exists := overrides[metricKey]; exists && !isThresholdExpired(sv, now) {
			override := sv.ResolveValue(now)
			lower := strings.TrimSpace(strings.ToLower(override))

			// State 3: disable
			if isDisabled(lower) {
				continue
			}

			// Check if it has severity suffix: "70:critical"
			parts := strings.SplitN(override, ":", 2)
			valueStr := strings.TrimSpace(parts[0])
			if len(parts) == 2 {
				severity = strings.TrimSpace(parts[1])
			}

			// State 1: custom value
			if v, err := strconv.ParseFloat(valueStr, 64); err == nil {
				rows = appendWithLegacyTwin(rows, metricKey, ResolvedThreshold{
					Tenant:    tenant,
					Metric:    metric,
					Value:     v,
					Severity:  severity,
					Component: component,
				})
				continue
			}

			// Unknown value — log warning, use default
			log.Printf("WARN: unknown value %q for tenant=%s metric=%s, using default", override, tenant, metricKey)
		}

		// State 2: use default
		rows = appendWithLegacyTwin(rows, metricKey, ResolvedThreshold{
			Tenant:    tenant,
			Metric:    metric,
			Value:     defaultValue,
			Severity:  severity,
			Component: component,
		})
	}
	return rows
}

// resolveCriticalRows resolves a tenant's <metric>_critical override variants,
// each producing an additional severity=critical threshold for an existing
// default metric. Extracted verbatim from the ResolveAtWithStats per-tenant
// loop (multi-tier severity scan).
//
// #1231: both maps arrive pre-canonicalized, so an old-spelled
// `mysql_cpu_critical` is already `mysql_threads_running_critical` here and
// its base lookup hits the canonical defaults view; the emit goes through
// appendWithLegacyTwin for the transition-window legacy critical row.
func (c *ThresholdConfig) resolveCriticalRows(tenant string, defaults map[string]float64, overrides map[string]ScheduledValue, now time.Time) []ResolvedThreshold {
	var rows []ResolvedThreshold
	for key, sv := range overrides {
		if !strings.HasSuffix(key, "_critical") || strings.HasPrefix(key, "_state_") || strings.HasPrefix(key, "_silent_") {
			continue
		}

		// PREVENT #656 v1: `expires:` is intentionally NOT honored on _critical
		// overrides — they have no platform default to fail-safe back to (reverting
		// would go SILENT, the very thing PREVENT avoids). expires here is a no-op;
		// ValidateTenantKeys warns the author. Honored only in resolveBaseRows.
		override := sv.ResolveValue(now)
		lower := strings.TrimSpace(strings.ToLower(override))
		if isDisabled(lower) {
			continue
		}

		// Derive the base metric key: "mysql_connections_critical" → "mysql_connections"
		baseKey := strings.TrimSuffix(key, "_critical")
		// Verify that the base metric exists in defaults (otherwise ignore)
		if _, exists := defaults[baseKey]; !exists {
			log.Printf("WARN: _critical key %q has no matching default %q, skipping", key, baseKey)
			continue
		}

		component, metric := parseMetricKey(baseKey)
		if v, err := strconv.ParseFloat(strings.TrimSpace(override), 64); err == nil {
			rows = appendWithLegacyTwin(rows, baseKey, ResolvedThreshold{
				Tenant:    tenant,
				Metric:    metric,
				Value:     v,
				Severity:  "critical",
				Component: component,
			})
		} else {
			log.Printf("WARN: invalid critical threshold %q for tenant=%s key=%s", override, tenant, key)
		}
	}
	return rows
}

// resolveDimensionalRows resolves a tenant's dimensional overrides using the
// `metric{label="v"}` / `{label=~"re"}` syntax. These are tenant-only (no
// default inheritance) and use the "value:severity" suffix for severity.
// Extracted verbatim from the ResolveAtWithStats per-tenant loop
// (Phase 2B / Phase 11 B1).
//
// #1231: the overrides map arrives pre-canonicalized (an old-spelled
// dimensional key is already `mysql_threads_running{...}` here, deduped on the
// FULL canonical key including the label segment), and the emit goes through
// appendWithLegacyTwin keyed on the dimensional key's BASE — so a dimensional
// override on an alias target dual-emits its legacy twin with the identical
// label set, closing the transition-window gap the base/_critical shapes
// already covered (review F1: without this, upgrading a plain override to a
// dimensional one made the legacy-identity series vanish mid-window).
func (c *ThresholdConfig) resolveDimensionalRows(tenant string, overrides map[string]ScheduledValue, now time.Time) []ResolvedThreshold {
	var rows []ResolvedThreshold
	for key, sv := range overrides {
		if !strings.Contains(key, "{") {
			continue // not a dimensional key
		}
		if strings.HasPrefix(key, "_state_") || strings.HasPrefix(key, "_silent_") ||
			key == "_severity_dedup" || strings.HasPrefix(key, "_routing") {
			continue
		}

		baseKey, customLabels, regexLabels := parseKeyWithLabels(key)
		if len(customLabels) == 0 && len(regexLabels) == 0 {
			log.Printf("WARN: failed to parse dimensional key %q for tenant=%s, skipping", key, tenant)
			continue
		}

		// PREVENT #656 v1: `expires:` is intentionally NOT honored on dimensional
		// overrides — no platform default to fail-safe to (reverting would go
		// SILENT). expires here is a no-op; ValidateTenantKeys warns. See
		// resolveBaseRows for the honored path.
		valStr := sv.ResolveValue(now)
		lower := strings.TrimSpace(strings.ToLower(valStr))
		if isDisabled(lower) {
			continue
		}

		component, metric := parseMetricKey(baseKey)
		severity := "warning"

		parts := strings.SplitN(valStr, ":", 2)
		valueStr := strings.TrimSpace(parts[0])
		if len(parts) == 2 {
			severity = strings.TrimSpace(parts[1])
		}

		v, err := strconv.ParseFloat(valueStr, 64)
		if err != nil {
			log.Printf("WARN: invalid dimensional threshold %q for tenant=%s key=%s, skipping", valStr, tenant, key)
			continue
		}

		rows = appendWithLegacyTwin(rows, baseKey, ResolvedThreshold{
			Tenant:       tenant,
			Metric:       metric,
			Value:        v,
			Severity:     severity,
			Component:    component,
			CustomLabels: customLabels,
			RegexLabels:  regexLabels,
		})
	}
	return rows
}

// truncationSortKey produces a deterministic ordering key for one tenant's
// resolved thresholds, used to make the per-tenant cardinality-cap truncation
// in ResolveAtWithStats stable across scrapes (ADR-024 AC-7).
//
// Two-tier contract:
//   - Tier "0" (sorts first → protected, kept under the cap): thresholds with
//     no `version` dimensional label, or version="default" — the baseline that
//     must survive truncation so the tenant never loses its un-versioned alert.
//   - Tier "1" (sorts last → dropped from the lexicographic tail first):
//     explicitly versioned thresholds (e.g. {version="v2"}). Ordering by the
//     canonical identity below guarantees the SAME version is dropped on every
//     scrape when a tenant is over the cap.
//
// The remainder of the key (component, metric, severity, sorted dimensional
// labels) makes the order total and stable so the sort result is identical
// across processes regardless of map iteration order.
//
// #1231 twin sub-ordering (review F3, WITHIN the tier contract above, which is
// unchanged): a transition-window legacy twin sorts under its CANONICAL
// identity plus a trailing twin rank — i.e. immediately after the canonical
// row it shadows. A cardinality cut landing inside the pair therefore drops
// the twin first (the only row rule packs no longer consume); a cut above the
// pair drops both together. Without this, the twin's legacy identity (e.g.
// "cpu" < "threads_running") sorted AHEAD of its canonical within the same
// tier, so an over-cap boundary could keep the unconsumed shadow and kill the
// only row with a consumer. Tier itself needs no twin handling: the twin
// copies its canonical row's labels, so their version tier is identical by
// construction.
func truncationSortKey(r ResolvedThreshold) string {
	version := r.CustomLabels["version"]
	if version == "" {
		version = r.RegexLabels["version"]
	}
	tier := "0"
	if version != "" && version != "default" {
		tier = "1"
	}

	component, metric := r.Component, r.Metric
	twinRank := "0"
	if r.legacyTwinOf != "" {
		component, metric = parseMetricKey(r.legacyTwinOf)
		twinRank = "1"
	}

	var b strings.Builder
	b.Grow(64) // pre-size: tier+component+metric+severity+labels rarely exceeds this
	b.WriteString(tier)
	b.WriteByte(0)
	b.WriteString(component)
	b.WriteByte(0)
	b.WriteString(metric)
	b.WriteByte(0)
	b.WriteString(r.Severity)
	b.WriteByte(0)
	b.WriteString(canonicalLabelKey(r.CustomLabels, r.RegexLabels))
	b.WriteByte(0)
	b.WriteString(twinRank)
	return b.String()
}

// canonicalLabelKey renders dimensional labels as a deterministic, sorted
// string (exact labels as "k=v", regex labels as "k=~v") joined by commas.
func canonicalLabelKey(custom, regex map[string]string) string {
	if len(custom) == 0 && len(regex) == 0 {
		return ""
	}
	parts := make([]string, 0, len(custom)+len(regex))
	for k, v := range custom {
		parts = append(parts, k+"="+v)
	}
	for k, v := range regex {
		parts = append(parts, k+"=~"+v)
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

// ResolveStateFilters resolves state-based monitoring filters for all tenants.
// For each state filter defined in config, each tenant gets an enabled flag
// unless explicitly disabled via _state_<filter_name>: "disable" in tenants map.
//
// v1.7.0: _state_maintenance supports structured format with expires.
// When expires is past, the filter is treated as disabled (maintenance auto-deactivates).
//
// Returns the list of enabled state filters to expose as Prometheus metrics.
func (c *ThresholdConfig) ResolveStateFilters() []ResolvedStateFilter {
	return c.ResolveStateFiltersAt(time.Now())
}

// ResolveStateFiltersAt is the time-parameterized version for testability.
func (c *ThresholdConfig) ResolveStateFiltersAt(now time.Time) []ResolvedStateFilter {
	var result []ResolvedStateFilter

	if len(c.StateFilters) == 0 {
		return result
	}

	for filterName, filter := range c.StateFilters {
		severity := filter.Severity
		if severity == "" {
			severity = "warning"
		}

		// default_state: "disable" → 預設關閉，需明確 enable
		// default_state: "" 或 "enable" → 預設開啟 (向後相容)
		defaultEnabled := !isDisabled(strings.TrimSpace(strings.ToLower(filter.DefaultState)))

		for tenant, overrides := range c.Tenants {
			stateKey := "_state_" + filterName
			if sv, exists := overrides[stateKey]; exists {
				val := strings.TrimSpace(sv.Default)
				lower := strings.TrimSpace(strings.ToLower(val))

				if isDisabled(lower) {
					continue // 明確停用
				}

				// v1.7.0: Check for structured format with expires (maintenance mode)
				if filterName == "maintenance" && strings.Contains(val, "expires:") {
					parsed := maintenanceModeStructured{}
					if err := yaml.Unmarshal([]byte(val), &parsed); err != nil {
						log.Printf("WARN: failed to parse structured _state_maintenance for tenant=%s: %v", tenant, err)
						continue
					}
					if parsed.Expires != "" {
						t, err := time.Parse(time.RFC3339, parsed.Expires)
						if err != nil {
							log.Printf("WARN: invalid expires %q in _state_maintenance for tenant=%s: %v", parsed.Expires, tenant, err)
							// Can't parse → treat as no expiry → still active
						} else if now.After(t) {
							continue // Expired → maintenance auto-deactivated
						}
					}
				}

				// 明確啟用 (任何非 disable 的值，如 "enable" or structured object)
			} else if !defaultEnabled {
				continue // 無覆寫 + 預設關閉 = 跳過
			}

			result = append(result, ResolvedStateFilter{
				Tenant:     tenant,
				FilterName: filterName,
				Severity:   severity,
			})
		}
	}

	return result
}

// ResolveSilentModes resolves silent mode preferences for all tenants.
// Supports both scalar format ("warning"/"critical"/"all"/"disable") and
// structured format ({target, expires, reason}).
//
// When expires is set and in the past (relative to `now`), the entry is marked Expired=true
// and the sentinel metric should NOT be emitted (silent mode auto-deactivates).
// The caller (collector) uses Expired entries to emit da_config_event instead.
//
// Returns one ResolvedSilentMode per tenant+severity combination.
// "all" expands to two entries: one for "warning" and one for "critical".
func (c *ThresholdConfig) ResolveSilentModes() []ResolvedSilentMode {
	return c.ResolveSilentModesAt(time.Now())
}

// ResolveSilentModesAt is the time-parameterized version for testability.
func (c *ThresholdConfig) ResolveSilentModesAt(now time.Time) []ResolvedSilentMode {
	var result []ResolvedSilentMode

	for tenant, overrides := range c.Tenants {
		sv, exists := overrides["_silent_mode"]
		if !exists {
			continue // Normal mode (default) — no silent entries
		}

		val := strings.TrimSpace(sv.Default)

		// Try structured format: check if the value looks like YAML mapping
		// ScheduledValue.UnmarshalYAML serializes mappings back to YAML string
		if strings.Contains(val, "target:") {
			parsed := silentModeStructured{}
			if err := yaml.Unmarshal([]byte(val), &parsed); err != nil {
				log.Printf("WARN: failed to parse structured _silent_mode for tenant=%s: %v", tenant, err)
				continue
			}
			target := strings.TrimSpace(strings.ToLower(parsed.Target))
			if isDisabled(target) || target == "" {
				continue
			}

			var expires time.Time
			var expired bool
			if parsed.Expires != "" {
				t, err := time.Parse(time.RFC3339, parsed.Expires)
				if err != nil {
					log.Printf("WARN: invalid expires %q in _silent_mode for tenant=%s: %v (expected RFC3339/ISO8601)", parsed.Expires, tenant, err)
				} else {
					expires = t
					expired = now.After(t)
				}
			}

			entries := resolveSilentTarget(tenant, target, expires, parsed.Reason, expired)
			result = append(result, entries...)
			continue
		}

		// Scalar format (backward compatible)
		lower := strings.TrimSpace(strings.ToLower(val))
		if isDisabled(lower) || lower == "" {
			continue
		}

		entries := resolveSilentTarget(tenant, lower, time.Time{}, "", false)
		if len(entries) == 0 {
			log.Printf("WARN: unknown silent mode %q for tenant=%s, ignoring (valid: warning, critical, all, disable)", lower, tenant)
		}
		result = append(result, entries...)
	}

	return result
}

// resolveSilentTarget expands a target string into ResolvedSilentMode entries.
func resolveSilentTarget(tenant, target string, expires time.Time, reason string, expired bool) []ResolvedSilentMode {
	base := ResolvedSilentMode{
		Tenant:  tenant,
		Expires: expires,
		Reason:  reason,
		Expired: expired,
	}
	switch target {
	case "warning":
		e := base
		e.TargetSeverity = "warning"
		return []ResolvedSilentMode{e}
	case "critical":
		e := base
		e.TargetSeverity = "critical"
		return []ResolvedSilentMode{e}
	case "all":
		w := base
		w.TargetSeverity = "warning"
		c := base
		c.TargetSeverity = "critical"
		return []ResolvedSilentMode{w, c}
	default:
		return nil
	}
}

// ResolveMaintenanceExpiries resolves maintenance mode expiry state for all tenants.
// Only returns entries for tenants with structured _state_maintenance that have an expires field.
// Used by the collector to emit da_config_event when maintenance mode expires.
func (c *ThresholdConfig) ResolveMaintenanceExpiries() []ResolvedMaintenanceExpiry {
	return c.ResolveMaintenanceExpiriesAt(time.Now())
}

// ResolveMaintenanceExpiriesAt is the time-parameterized version for testability.
func (c *ThresholdConfig) ResolveMaintenanceExpiriesAt(now time.Time) []ResolvedMaintenanceExpiry {
	var result []ResolvedMaintenanceExpiry

	for tenant, overrides := range c.Tenants {
		// Check all _state_* keys for maintenance filter specifically
		sv, exists := overrides["_state_maintenance"]
		if !exists {
			continue
		}

		val := strings.TrimSpace(sv.Default)
		// Only structured format supports expires
		if !strings.Contains(val, "expires:") {
			continue
		}

		parsed := maintenanceModeStructured{}
		if err := yaml.Unmarshal([]byte(val), &parsed); err != nil {
			log.Printf("WARN: failed to parse structured _state_maintenance for tenant=%s: %v", tenant, err)
			continue
		}

		if parsed.Expires == "" {
			continue
		}

		t, err := time.Parse(time.RFC3339, parsed.Expires)
		if err != nil {
			log.Printf("WARN: invalid expires %q in _state_maintenance for tenant=%s: %v", parsed.Expires, tenant, err)
			continue
		}

		result = append(result, ResolvedMaintenanceExpiry{
			Tenant:  tenant,
			Expires: t,
			Reason:  parsed.Reason,
			Expired: now.After(t),
		})
	}

	return result
}

// ResolveThresholdExpiries resolves time-boxed threshold expiry state for all
// tenants (PREVENT #656). Used by the collector to emit da_config_event when a
// loosened threshold's TTL lapses. v1: base standard metrics only.
func (c *ThresholdConfig) ResolveThresholdExpiries() []ResolvedThresholdExpiry {
	return c.ResolveThresholdExpiriesAt(time.Now())
}

// ResolveThresholdExpiriesAt is the time-parameterized version for testability.
func (c *ThresholdConfig) ResolveThresholdExpiriesAt(now time.Time) []ResolvedThresholdExpiry {
	var result []ResolvedThresholdExpiry
	// #1231: membership goes through the same canonical view as
	// ResolveAtWithStats — an old-spelled override whose default has already
	// been renamed still HAS its expiry honored by resolveBaseRows, so the
	// da_config_event for it must keep emitting too (skipping here would be a
	// silent gap: value reverts, cleanup alert never fires).
	canonDefaults := canonicalizeDefaults(c.Defaults)
	for tenant, overrides := range c.Tenants {
		for metricKey, sv := range overrides {
			if sv.Expiry == nil || sv.Expiry.Expires == "" {
				continue
			}
			// v1 scope: expires is honored only on base standard metrics — those
			// with a platform default to fail-safe back to (resolveBaseRows is the
			// only resolution path with the expiry hook). Reserved keys, dimensional
			// ({...}), _critical variants and custom alerts are NOT in defaults →
			// skipped here so we never emit an expiry event for a threshold whose
			// value won't actually revert. The same promise covers the dedup-loser
			// case below: a deprecated-spelled entry whose canonical sibling is
			// ALSO set never governed the value at all. ValidateTenantKeys warns on
			// out-of-scope expires so it isn't a silent no-op.
			canonKey, wasAlias := canonicalKeyFor(metricKey)
			// #1231 review F2: same-map conflict — resolve's canonical-wins dedup
			// ignores this entry entirely, so its lapsed expires reverts nothing.
			// Emitting "expired and auto-reverted" here would page the tenant
			// about a value that was never in effect. Mirrors ValidateTenantKeys'
			// conflict notice, which already tells the author to delete the entry.
			if wasAlias {
				if _, canonAlsoSet := overrides[canonKey]; canonAlsoSet {
					continue
				}
			}
			if _, isDefault := canonDefaults[canonKey]; !isDefault {
				continue
			}
			t, err := time.Parse(time.RFC3339, sv.Expiry.Expires)
			if err != nil {
				log.Printf("WARN: invalid expires %q in threshold %q for tenant=%s: %v", sv.Expiry.Expires, metricKey, tenant, err)
				continue
			}
			result = append(result, ResolvedThresholdExpiry{
				Tenant:    tenant,
				MetricKey: metricKey,
				Expires:   t,
				Reason:    sv.Expiry.Reason,
				Expired:   now.After(t),
			})
		}
	}
	return result
}

// IsMaintenanceActive checks if a structured _state_maintenance is currently active (not expired).
// For scalar "enable" values (no expires), it always returns true.
// For structured values with expires in the past, it returns false.
func (c *ThresholdConfig) IsMaintenanceActive(tenant string, now time.Time) bool {
	overrides, exists := c.Tenants[tenant]
	if !exists {
		return false
	}
	sv, exists := overrides["_state_maintenance"]
	if !exists {
		return false
	}

	val := strings.TrimSpace(sv.Default)
	lower := strings.TrimSpace(strings.ToLower(val))

	// Scalar "disable" — not active
	if isDisabled(lower) || lower == "" {
		return false
	}

	// Structured format with expires
	if strings.Contains(val, "expires:") {
		parsed := maintenanceModeStructured{}
		if err := yaml.Unmarshal([]byte(val), &parsed); err != nil {
			return false
		}
		if parsed.Expires != "" {
			t, err := time.Parse(time.RFC3339, parsed.Expires)
			if err != nil {
				return true // can't parse → treat as no expiry → active
			}
			return !now.After(t)
		}
	}

	// Scalar "enable" or structured without expires — active
	return true
}

// ResolveSeverityDedup resolves severity deduplication preferences for all tenants.
// Default: "enable" (backward compatible — suppress warning notification when critical fires).
// Tenants can set _severity_dedup: "disable" to receive both notifications.
//
// Returns one ResolvedSeverityDedup per tenant where mode="enable".
// Tenants with "disable" produce no entry (sentinel alert won't fire → no inhibit).
func (c *ThresholdConfig) ResolveSeverityDedup() []ResolvedSeverityDedup {
	var result []ResolvedSeverityDedup

	for tenant, overrides := range c.Tenants {
		sv, exists := overrides["_severity_dedup"]
		if !exists {
			// Default: enable (backward compatible)
			result = append(result, ResolvedSeverityDedup{Tenant: tenant, Mode: "enable"})
			continue
		}

		val := strings.TrimSpace(strings.ToLower(sv.Default))
		switch val {
		case "enable", "enabled", "on", "true":
			result = append(result, ResolvedSeverityDedup{Tenant: tenant, Mode: "enable"})
		case "disable", "disabled", "off", "false":
			// No entry → sentinel won't fire → no inhibit → both notifications sent
			continue
		default:
			log.Printf("WARN: unknown severity_dedup value %q for tenant=%s, defaulting to enable (valid: enable, disable)", val, tenant)
			result = append(result, ResolvedSeverityDedup{Tenant: tenant, Mode: "enable"})
		}
	}

	return result
}

// ResolveMetadata returns metadata for ALL tenants unconditionally.
// Tenants without _metadata get empty strings — this guarantees PromQL
// group_left joins never fail (no False Negatives from missing info metric).
//
// _metadata is stored as a re-serialized YAML string in ScheduledValue.Default
// (arbitrary mapping path in UnmarshalYAML). We parse it back into TenantMetadata.
func (c *ThresholdConfig) ResolveMetadata() []ResolvedMetadata {
	var result []ResolvedMetadata

	for tenant, overrides := range c.Tenants {
		meta := ResolvedMetadata{Tenant: tenant}

		sv, exists := overrides["_metadata"]
		if exists && sv.Default != "" {
			var tm TenantMetadata
			if err := yaml.Unmarshal([]byte(sv.Default), &tm); err != nil {
				log.Printf("WARN: tenant=%s: failed to parse _metadata: %v", tenant, err)
			} else {
				meta.RunbookURL = tm.RunbookURL
				meta.Owner = tm.Owner
				meta.Tier = tm.Tier
				meta.Environment = tm.Environment
				meta.Region = tm.Region
				meta.Domain = tm.Domain
				meta.DBType = tm.DBType
				meta.Tags = tm.Tags
				meta.Groups = tm.Groups
			}
		}

		result = append(result, meta)
	}

	// Sort by tenant name for deterministic output
	sort.Slice(result, func(i, j int) bool {
		return result[i].Tenant < result[j].Tenant
	})

	return result
}

// KeyValidation is the two-channel result of ValidateTenantKeys (#1231 c2).
//
// Errors carries the pre-existing warning kinds — everything that gates a
// tenant-api write (gitops/writer.go turns any entry into ErrValidation) and
// that CI's Python validator mirrors. Notices carries advisory,
// never-blocking messages (currently: deprecated-key alias notices), so a
// tenant whose config still uses an old spelling keeps writing successfully
// during the transition window while being told to migrate.
//
// A named struct (not a bare ([]string, []string) pair) on purpose: a
// positional swap at a call site would compile clean and silently invert the
// gate semantics.
type KeyValidation struct {
	Errors  []string // blocking channel: unknown keys, bad expires, version-label violations, dangling _critical, ...
	Notices []string // advisory channel: #1231 deprecation notices; MUST never block a write
}

// ValidateTenantKeys checks all tenant config keys against known defaults and
// reserved patterns. Errors lists warning messages for unknown/invalid keys
// (this helps catch typos like "_silence_mode" that would be silently
// ignored); Notices lists non-blocking deprecation advisories (#1231).
//
// Alias handling mirrors the resolve boundary: each key is canonicalized
// first (exact-match table, never prefix-match) and validated under its
// canonical spelling against a canonical defaults view — so messages for
// aliased keys name the NEW key, and the paired notice ties old→new. When
// both spellings appear in the same map, the canonical entry wins and the
// deprecated one is reported as ignored (matching resolve's dedup).
func (c *ThresholdConfig) ValidateTenantKeys() KeyValidation {
	var v KeyValidation
	canonDefaults := canonicalizeDefaults(c.Defaults)
	// The platform surface is TWO sets, not one (#1189 / TRK-337): keys the
	// platform gives a value to (defaults), and keys it merely RECOGNISES
	// (optional_overrides — declared, tenant-settable, platform asserts
	// nothing). Both go through the same canonicalization, or a deprecated
	// spelling on the declared list would silently never match.
	canonOptional := canonicalizeOptionalOverrides(c.OptionalOverrides)

	// Defaults-side conflict: both spellings of the same threshold present.
	// Resolve lets the canonical entry win and ignores the deprecated one —
	// say so, or the losing value would just silently never apply.
	// (A defaults map carrying ONLY the old spelling is the expected
	// pre-migration state during the transition window — no notice for that;
	// the platform-side rename is its own tracked commit in #1231.)
	for legacy, canon := range deprecatedKeyAliases {
		if _, hasLegacy := c.Defaults[legacy]; !hasLegacy {
			continue
		}
		if _, hasCanon := c.Defaults[canon]; hasCanon {
			v.Notices = append(v.Notices, fmt.Sprintf(
				"NOTICE: defaults: deprecated key %q is ignored because its replacement %q is also set — remove %q (#1231 rename)",
				legacy, canon, legacy))
		}
	}

	for tenant, overrides := range c.Tenants {
		// Validate _profile reference (v1.12.0)
		// Note: applyProfiles() also warns on unknown profiles during merging.
		// This check ensures validation is complete even if applyProfiles is skipped.
		if sv, exists := overrides["_profile"]; exists {
			profileName := strings.TrimSpace(sv.Default)
			if profileName != "" {
				if _, found := c.Profiles[profileName]; !found {
					v.Errors = append(v.Errors, fmt.Sprintf(
						"WARN: tenant=%s: _profile references unknown profile %q", tenant, profileName))
				}
			}
		}

		for key, sv := range overrides {
			// #1231 alias canonicalization — BEFORE any other check, mirroring
			// the resolve boundary. Aliased keys get a notice (never an error);
			// validation below then runs on the canonical spelling.
			canonKey, wasAlias := canonicalKeyFor(key)
			if wasAlias {
				if _, canonAlsoSet := overrides[canonKey]; canonAlsoSet {
					// Same-map conflict: resolve ignores this entry entirely
					// (canonical wins), so don't validate it either — the
					// canonical sibling gets validated on its own turn.
					v.Notices = append(v.Notices, fmt.Sprintf(
						"NOTICE: tenant=%s: deprecated key %q is ignored because its replacement %q is also set — remove %q (#1231 rename)",
						tenant, key, canonKey, key))
					continue
				}
				v.Notices = append(v.Notices, fmt.Sprintf(
					"NOTICE: tenant=%s: key %q was renamed to %q (#1231) — the old name still resolves during the 2-release transition window; please update this override to %q",
					tenant, key, canonKey, canonKey))
			}

			// PREVENT #656 v1: `expires:` is honored only on base standard metrics
			// (resolveBaseRows is the only resolution path with the fail-safe hook).
			// Warn when it appears elsewhere (silent no-op) or is malformed (would
			// never auto-revert) — fail-loud per dev-rule #5.
			if sv.Expiry != nil && sv.Expiry.Expires != "" {
				// ⛔ Order matters, and it is defaults-first — matching the
				// _critical branch below. Nothing forbids a key from being in
				// BOTH sets (mergePartialInto only unions the list; no schema
				// or loader cross-checks it against defaults), and a key that
				// HAS a platform value has something to revert to no matter
				// what else names it. Checking declared first would refuse a
				// perfectly good time-boxed override while telling the author
				// "there is no default to revert to" about a key whose default
				// is right there — and would refuse it while
				// ResolveThresholdExpiriesAt (which gates on canonDefaults
				// alone) still emitted its expiry event, splitting the very
				// pair the contract test below exists to hold together.
				if _, isDefault := canonDefaults[canonKey]; isDefault {
					if _, err := time.Parse(time.RFC3339, sv.Expiry.Expires); err != nil {
						v.Errors = append(v.Errors, fmt.Sprintf(
							"WARN: tenant=%s: invalid `expires:` %q on %q (need RFC3339 e.g. 2026-07-01T00:00:00Z) — override will NOT auto-revert", tenant, sv.Expiry.Expires, canonKey))
					}
				} else if _, isDeclared := canonOptional[canonKey]; isDeclared {
					// ⛔ Declared keys are refused their OWN message rather than
					// folded into the generic out-of-scope one below, because
					// this is the single shape where `expires:` looks like it
					// ought to work: the platform names the key, so an author
					// reasonably expects the time-boxing base metrics get.
					//
					// It cannot work. `expires:` is a fail-SAFE hook — it
					// reverts an override TO the platform default
					// (resolveBaseRows is the only path carrying it). A
					// declared key has no platform value to revert to, so
					// lapsing leaves the threshold with no value at all —
					// whatever the tenant was getting stops, and the alert
					// goes quiet with nothing said. That is the silent revert
					// PREVENT #656 forbids, and it is verbatim why
					// resolveCriticalRows and resolveDimensionalRows refuse
					// expires too — declared keys are the fourth member of
					// that same "nothing to fall back to" family.
					v.Errors = append(v.Errors, fmt.Sprintf(
						"WARN: tenant=%s: `expires:` on %q is refused — %q is declared without a platform value (optional_overrides), so there is no default to revert to: on expiry the threshold would have NO value and the alert would go silent. Remove `expires:`; delete the override itself when you want it gone.",
						tenant, canonKey, canonKey))
				} else {
					v.Errors = append(v.Errors, fmt.Sprintf(
						"WARN: tenant=%s: `expires:` on %q is ignored — only base standard metrics (in _defaults.yaml) support time-boxed thresholds in v1", tenant, canonKey))
				}
			}

			// Known reserved key
			if validReservedKeys[canonKey] {
				continue
			}

			// Known reserved prefix
			reserved := false
			for _, prefix := range validReservedPrefixes {
				if strings.HasPrefix(canonKey, prefix) {
					reserved = true
					break
				}
			}
			if reserved {
				continue
			}

			// Dimensional key with {labels}
			if strings.Contains(canonKey, "{") {
				baseKey, customLabels, regexLabels := parseKeyWithLabels(canonKey)
				_, baseValued := canonDefaults[baseKey]
				_, baseDeclared := canonOptional[baseKey]
				// A declared base is accepted here with no reservation, unlike
				// the _critical shape below: resolveDimensionalRows is
				// tenant-only — it never consults defaults at all — so a
				// dimensional override on a declared base resolves and emits
				// the moment it is written. Accepting it does not promise
				// anything the resolver won't deliver.
				if baseValued || baseDeclared {
					// ADR-024 OQ-6: validate any `version` dimensional label.
					v.Errors = append(v.Errors,
						validateVersionLabel(tenant, canonKey, baseKey, customLabels, regexLabels)...)
					continue
				}
				// Unknown base key in dimensional key
				v.Errors = append(v.Errors, fmt.Sprintf(
					"WARN: tenant=%s: unknown base metric %q in dimensional key %q",
					tenant, baseKey, canonKey))
				continue
			}

			// `<metric>_critical` → the BASE metric must be in defaults.
			//
			// This used to skip unconditionally, deferring to "base validation
			// done by ResolveAt". ResolveAt does check — but it only
			// `log.Printf`s at scrape time (resolveCriticalRows), and nobody
			// authoring a config reads the exporter's log: the tenant author, CI
			// (`generate_alertmanager_routes.py --validate`) and the tenant-api
			// write path all read THIS function's output. So the critical tier
			// was the ONLY key shape with no author-visible signal — and it is
			// also the shape that fails hardest: a base key missing from defaults
			// still resolves to the platform default, whereas a dangling
			// `_critical` produces NO row at all. The tenant's critical alert
			// silently never materialises.
			//
			// The base key already warns a few lines below, so this is a
			// CONSISTENCY fix, not new strictness — and it restores parity with
			// the Python validator (`_grar_validate.validate_tenant_keys`), which
			// has always fallen through to a warning here.
			//
			// #1231: the lookup runs on canonical spellings, so an old-spelled
			// `mysql_cpu_critical` whose (renamed) base exists gets only the
			// rename notice above — no dangling error; a truly base-less
			// `_critical` still errors, naming the NEW key.
			if strings.HasSuffix(canonKey, "_critical") {
				baseKey := strings.TrimSuffix(canonKey, "_critical")
				if _, exists := canonDefaults[baseKey]; exists {
					continue
				}
				// ⛔ A DECLARED base is still refused — deliberately, and this
				// is the one place the two membership sets must NOT be unioned.
				// resolveCriticalRows keys off `defaults[baseKey]` and drops
				// the row with a scrape-time log line when it is absent; a
				// declared base is absent from defaults by definition. So
				// accepting this key would let the write succeed and then emit
				// nothing, with the only signal in the exporter's log — the
				// precise failure this validator was extended to end (see the
				// block comment above).
				//
				// ⚠️ Read alongside the flat branch below, which IS accepted
				// even though it emits nothing either. The two are not
				// inconsistent, but the difference is reachability, not
				// principle, so it has to be said out loud: this shape is
				// writable TODAY — the `_critical` twins of keys like
				// pg_connections have their base in the shipped defaults, so a
				// tenant can hit this branch on a live config right now. The
				// flat shape cannot be reached at all until some file declares
				// a key, and no supported producer can do that yet (the Helm
				// template renders only defaults/state_filters, and
				// scaffold_tenant.generate_defaults() emits the same two). A
				// tripwire test holds that shut rather than leaving it to
				// sequencing: tests/shared/test_optional_overrides_tripwire.py.
				//
				// The pairing flips together: when the emission loop learns to
				// resolve declared keys, resolveCriticalRows and this branch
				// change in the same commit, never one without the other.
				if _, declared := canonOptional[baseKey]; declared {
					v.Errors = append(v.Errors, fmt.Sprintf(
						"WARN: tenant=%s: %q cannot be set — its base %q is declared without a platform value (optional_overrides), and the critical tier needs a base VALUE in defaults: resolveCriticalRows drops the row when the base is absent, so this override would be accepted and then emit nothing. Set %q itself instead, or ask the platform to move %q into defaults.",
						tenant, canonKey, baseKey, baseKey, baseKey))
					continue
				}
				v.Errors = append(v.Errors, fmt.Sprintf(
					"WARN: tenant=%s: %q has no base metric %q in defaults — the critical "+
						"tier is silently DROPPED (not defaulted): resolveCriticalRows only "+
						"emits a critical row when the base key exists. Add %q to the platform "+
						"defaults, or remove this override.",
					tenant, canonKey, baseKey, baseKey))
				continue
			}

			// Normal metric key → must be somewhere on the platform surface:
			// either valued (defaults) or merely declared (optional_overrides).
			if _, exists := canonDefaults[canonKey]; exists {
				continue
			}
			// The third slot. Before this, a key absent from defaults was
			// rejected here and gitops/writer.go turned the rejection into
			// ErrValidation — so "dormant, waiting for the tenant to set it"
			// was not a state any key could be in: it was dead AND unwritable,
			// and the tenant could not help themselves. Accepting it is what
			// lets a tenant calibrate a threshold the platform declines to put
			// a number on.
			//
			// ⚠️ Accepting is not emitting, and this branch is the one place
			// that gap lives. resolveBaseRows still iterates defaults only, so
			// until the emission loop lands, a declared key a tenant sets is
			// recorded and produces no row — no error, no notice, not even a
			// scrape-time log line. That is a worse signal than the `_critical`
			// shape refused above, which at least logs.
			//
			// What makes it acceptable is not sequencing but reachability, and
			// reachability is held shut mechanically rather than promised:
			// nothing can put a key on this list yet (the Helm template renders
			// only defaults/state_filters; scaffold_tenant.generate_defaults()
			// emits the same two; no shipped _defaults.yaml declares one), and
			// tests/shared/test_optional_overrides_tripwire.py turns red the
			// moment that stops being true. It is deleted by the same commit
			// that adds the emission loop — and it also turns red if that
			// commit lands and leaves it behind.
			if _, declared := canonOptional[canonKey]; declared {
				continue
			}

			// Underscore-prefixed but not reserved → likely typo
			if strings.HasPrefix(canonKey, "_") {
				v.Errors = append(v.Errors, fmt.Sprintf(
					"WARN: tenant=%s: unknown reserved key %q (typo?)", tenant, canonKey))
				continue
			}

			// Not in defaults, not reserved → unknown metric key
			v.Errors = append(v.Errors, fmt.Sprintf(
				"WARN: tenant=%s: unknown key %q not in defaults", tenant, canonKey))
		}
	}

	return v
}

// validateVersionLabel enforces the ADR-024 OQ-6 rules on a dimensional
// `version` label found in a tenant key: a documented charset, no reserved
// values (empty / literal "default"), Phase-1 component scoping, and exact
// (non-regex) selectors. It returns warning strings that ride
// KeyValidation.Errors; the actual consumers are (1) the exporter's
// config-load log (logConfigStats), (2) the tenant-api write path —
// gitops/writer.go treats any Errors entry as ErrValidation, so a bad
// version label is hard-rejected at the write boundary — and (3) the CI
// Python validator parity path (generate_alertmanager_routes.py --validate).
// NOTE: da-guard does NOT consume or escalate these (an earlier version of
// this comment claimed it did).
//
// The Go side is intentionally observability-grade: the exporter logs these
// at config load so operators see violations even if a tenant bypasses CI.
func validateVersionLabel(tenant, key, baseKey string, custom, regex map[string]string) []string {
	exact, hasExact := custom["version"]
	pattern, hasRegex := regex["version"]
	if !hasExact && !hasRegex {
		return nil // no version label on this key — nothing to validate
	}

	var w []string

	// Component scope: a version label is only in scope for piloted metrics.
	component, metric := parseMetricKey(baseKey)
	if !pilotVersionMetrics[component+"/"+metric] {
		w = append(w, fmt.Sprintf(
			"WARN: tenant=%s: version label on non-pilot metric %q in key %q — "+
				"ADR-024 Phase 1 only permits %s; a version label here risks "+
				"cross-pack double-count", tenant, baseKey, key, pilotVersionMetricList()))
	}

	if hasRegex {
		// Phase 1 expects exact version="..." selectors; a regex matcher on
		// version is almost certainly a mistake and defeats per-version join.
		w = append(w, fmt.Sprintf(
			"WARN: tenant=%s: regex version matcher %q in key %q — ADR-024 "+
				"Phase 1 expects an exact version=\"...\" selector", tenant, pattern, key))
	}

	if hasExact {
		switch {
		case exact == "":
			w = append(w, fmt.Sprintf(
				"WARN: tenant=%s: empty version label in key %q (ADR-024 OQ-6 "+
					"forbids empty — it collides with the unversioned baseline)", tenant, key))
		case exact == "default":
			w = append(w, fmt.Sprintf(
				"WARN: tenant=%s: literal version=\"default\" in key %q is reserved "+
					"for the normalize-layer fallback (ADR-024 OQ-6)", tenant, key))
		case !versionLabelRe.MatchString(exact):
			w = append(w, fmt.Sprintf(
				"WARN: tenant=%s: version %q in key %q violates %s "+
					"(ADR-024 OQ-6; pilot-calibratable)", tenant, exact, key, versionLabelPattern))
		}
	}

	return w
}

// pilotVersionMetricList renders the Phase-1 allow-list deterministically for
// warning messages.
func pilotVersionMetricList() string {
	keys := make([]string, 0, len(pilotVersionMetrics))
	for k := range pilotVersionMetrics {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return strings.Join(keys, ", ")
}

// ApplyProfiles expands profile values into tenant overrides (fill-in, not overwrite).
// For each tenant with _profile: "<name>", profile keys that are NOT already set
// by the tenant are copied into the tenant's overrides map.
//
// Four-layer inheritance chain (v1.12.0):
//  1. Global Defaults (_defaults.yaml) — handled by Resolve fallback
//  2. Rule Pack Baseline — embedded in defaults
//  3. Profile Overlay (_profiles.yaml) — expanded HERE into tenant overrides
//  4. Tenant Override (tenant-*.yaml) — already in overrides, never overwritten
//
// This approach ensures all existing Resolve* functions work unchanged —
// they see a single merged overrides map without knowing about profiles.
func (c *ThresholdConfig) ApplyProfiles() {
	if len(c.Profiles) == 0 {
		return
	}

	for tenant, overrides := range c.Tenants {
		sv, exists := overrides["_profile"]
		if !exists {
			continue
		}

		profileName := strings.TrimSpace(sv.Default)
		if profileName == "" {
			continue
		}

		profile, found := c.Profiles[profileName]
		if !found {
			log.Printf("WARN: tenant=%s references unknown profile %q, ignoring", tenant, profileName)
			continue
		}

		// Fill-in: copy profile keys that the tenant has NOT overridden.
		// #1231: "overridden" is checked across alias spellings — a tenant's
		// old-spelled mysql_cpu override must still beat a profile's
		// mysql_threads_running value (and vice versa), otherwise resolve's
		// canonical-wins dedup would let the profile displace the tenant's
		// own setting (four-layer priority inversion).
		//
		// #1231 review F4: the PROFILE side is canonicalized FIRST (same
		// canonical-wins dedup as tenant overrides), for two reasons:
		// (a) a dual-spelled profile must not flip its effective value with
		//     Go's randomized map iteration — this was the only
		//     order-dependent layer in the four-layer chain;
		// (b) the fill-in lands under the CANONICAL spelling, so a clean
		//     tenant is never handed a deprecated key it did not write (no
		//     false tenant-facing rename notice, no phantom
		//     da_config_deprecated_keys count). The dropped deprecated-count
		//     signal belongs to the profile AUTHOR's surface, which has no
		//     notification channel today — deliberately not invented here.
		canonProfile, _ := canonicalizeOverrides(profile)
		for key, profileValue := range canonProfile {
			if !tenantHasAliasEquivalent(overrides, key) {
				overrides[key] = profileValue
			}
		}
	}
}

// ResolveRouting resolves alert routing configurations for all tenants.
// Tenants set _routing as a structured map in their config.
// Returns one RoutingConfig per tenant that has a valid _routing section.
//
// Guardrails:
//   - group_wait: 5s–5m (clamped with warning)
//   - group_interval: 5s–5m (clamped with warning)
//   - repeat_interval: 1m–72h (clamped with warning)
//   - receiver is required; skip tenant if missing
func (c *ThresholdConfig) ResolveRouting() []RoutingConfig {
	var result []RoutingConfig

	for tenant, overrides := range c.Tenants {
		sv, exists := overrides["_routing"]
		if !exists {
			continue
		}

		// _routing is stored as a YAML string in ScheduledValue.Default
		// but it's actually a structured map. We need to re-parse it.
		raw := sv.Default
		if raw == "" {
			continue
		}

		// Parse the routing config from the raw YAML value.
		// In directory mode, _routing is a nested map that gets serialized
		// as a ScheduledValue. We parse it from the original YAML structure.
		var routingMap map[string]interface{}
		if err := yaml.Unmarshal([]byte(raw), &routingMap); err != nil {
			// If it's not valid YAML, it might be a simple string — skip
			log.Printf("WARN: invalid _routing config for tenant=%s: %v", tenant, err)
			continue
		}

		rc := RoutingConfig{Tenant: tenant}

		// Extract receiver (required, must be a map with 'type')
		recvRaw, hasRecv := routingMap["receiver"]
		if !hasRecv {
			log.Printf("WARN: _routing for tenant=%s missing required 'receiver' field, skipping", tenant)
			continue
		}
		recvMap, ok := recvRaw.(map[interface{}]interface{})
		if !ok {
			// Try map[string]interface{} (depends on YAML parser)
			if rm, ok2 := recvRaw.(map[string]interface{}); ok2 {
				rc.ReceiverConfig = rm
			} else {
				log.Printf("WARN: _routing for tenant=%s: 'receiver' must be a map with 'type', skipping", tenant)
				continue
			}
		} else {
			rc.ReceiverConfig = make(map[string]interface{}, len(recvMap))
			for k, v := range recvMap {
				if ks, ok := k.(string); ok {
					rc.ReceiverConfig[ks] = v
				}
			}
		}
		if rtype, ok := rc.ReceiverConfig["type"].(string); ok && validReceiverTypes[rtype] {
			rc.ReceiverType = rtype
		} else {
			log.Printf("WARN: _routing for tenant=%s: invalid or missing receiver 'type', skipping", tenant)
			continue
		}

		// Extract group_by (optional)
		if gb, ok := routingMap["group_by"].([]interface{}); ok {
			for _, v := range gb {
				if s, ok := v.(string); ok {
					rc.GroupBy = append(rc.GroupBy, s)
				}
			}
		}

		// Extract and validate timing parameters with guardrails
		if gw, ok := routingMap["group_wait"].(string); ok && gw != "" {
			rc.GroupWait = clampDuration(gw, "group_wait", tenant)
		}
		if gi, ok := routingMap["group_interval"].(string); ok && gi != "" {
			rc.GroupInterval = clampDuration(gi, "group_interval", tenant)
		}
		if ri, ok := routingMap["repeat_interval"].(string); ok && ri != "" {
			rc.RepeatInterval = clampDuration(ri, "repeat_interval", tenant)
		}

		result = append(result, rc)
	}

	return result
}
