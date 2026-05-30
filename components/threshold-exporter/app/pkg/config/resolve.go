package config

import (
	"fmt"
	"log"
	"sort"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

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

	for tenant, overrides := range c.Tenants {
		startIdx := len(result) // track where this tenant's metrics start

		for metricKey, defaultValue := range c.Defaults {
			// Skip _state_ prefixed keys — handled by ResolveStateFilters()
			// Skip _silent_ prefixed keys — handled by ResolveSilentModes()
			// Skip _severity_dedup — handled by ResolveSeverityDedup()
			// Skip _routing — handled by ResolveRouting() (Phase 4)
			if strings.HasPrefix(metricKey, "_state_") || strings.HasPrefix(metricKey, "_silent_") ||
				metricKey == "_severity_dedup" || strings.HasPrefix(metricKey, "_routing") {
				continue
			}

			// Parse metric key: "mysql_connections" → component="mysql", metric="connections"
			component, metric := parseMetricKey(metricKey)
			severity := "warning" // default severity

			// Check tenant override (skip _state_ overrides)
			if sv, exists := overrides[metricKey]; exists {
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
					result = append(result, ResolvedThreshold{
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
			result = append(result, ResolvedThreshold{
				Tenant:    tenant,
				Metric:    metric,
				Value:     defaultValue,
				Severity:  severity,
				Component: component,
			})
		}

		// Multi-tier severity: scan for <metricKey>_critical overrides.
		// These produce an additional threshold with severity=critical.
		for key, sv := range overrides {
			if !strings.HasSuffix(key, "_critical") || strings.HasPrefix(key, "_state_") || strings.HasPrefix(key, "_silent_") {
				continue
			}

			override := sv.ResolveValue(now)
			lower := strings.TrimSpace(strings.ToLower(override))
			if isDisabled(lower) {
				continue
			}

			// Derive the base metric key: "mysql_connections_critical" → "mysql_connections"
			baseKey := strings.TrimSuffix(key, "_critical")
			// Verify that the base metric exists in defaults (otherwise ignore)
			if _, exists := c.Defaults[baseKey]; !exists {
				log.Printf("WARN: _critical key %q has no matching default %q, skipping", key, baseKey)
				continue
			}

			component, metric := parseMetricKey(baseKey)
			if v, err := strconv.ParseFloat(strings.TrimSpace(override), 64); err == nil {
				result = append(result, ResolvedThreshold{
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

		// Phase 2B: dimensional keys — tenant overrides with {label="value"} syntax.
		// Phase 11 B1: also supports {label=~"pattern"} regex matchers.
		// These are tenant-only (no default inheritance) and don't support _critical suffix.
		// Severity override uses the "value:severity" syntax (e.g., "500:critical").
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

			result = append(result, ResolvedThreshold{
				Tenant:       tenant,
				Metric:       metric,
				Value:        v,
				Severity:     severity,
				Component:    component,
				CustomLabels: customLabels,
				RegexLabels:  regexLabels,
			})
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
	}

	return result, ResolveStats{PerTenantOverLimit: perTenantOverLimit}
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
func truncationSortKey(r ResolvedThreshold) string {
	version := r.CustomLabels["version"]
	if version == "" {
		version = r.RegexLabels["version"]
	}
	tier := "0"
	if version != "" && version != "default" {
		tier = "1"
	}

	var b strings.Builder
	b.Grow(64) // pre-size: tier+component+metric+severity+labels rarely exceeds this
	b.WriteString(tier)
	b.WriteByte(0)
	b.WriteString(r.Component)
	b.WriteByte(0)
	b.WriteString(r.Metric)
	b.WriteByte(0)
	b.WriteString(r.Severity)
	b.WriteByte(0)
	b.WriteString(canonicalLabelKey(r.CustomLabels, r.RegexLabels))
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

// ValidateTenantKeys checks all tenant config keys against known defaults and
// reserved patterns. Returns a list of warning messages for unknown keys.
// This helps catch typos like "_silence_mode" that would be silently ignored.
func (c *ThresholdConfig) ValidateTenantKeys() []string {
	var warnings []string

	for tenant, overrides := range c.Tenants {
		// Validate _profile reference (v1.12.0)
		// Note: applyProfiles() also warns on unknown profiles during merging.
		// This check ensures validation is complete even if applyProfiles is skipped.
		if sv, exists := overrides["_profile"]; exists {
			profileName := strings.TrimSpace(sv.Default)
			if profileName != "" {
				if _, found := c.Profiles[profileName]; !found {
					warnings = append(warnings, fmt.Sprintf(
						"WARN: tenant=%s: _profile references unknown profile %q", tenant, profileName))
				}
			}
		}

		for key := range overrides {
			// Known reserved key
			if validReservedKeys[key] {
				continue
			}

			// Known reserved prefix
			reserved := false
			for _, prefix := range validReservedPrefixes {
				if strings.HasPrefix(key, prefix) {
					reserved = true
					break
				}
			}
			if reserved {
				continue
			}

			// Dimensional key with {labels}
			if strings.Contains(key, "{") {
				baseKey, _, _ := parseKeyWithLabels(key)
				if _, exists := c.Defaults[baseKey]; exists {
					continue
				}
				// Unknown base key in dimensional key
				warnings = append(warnings, fmt.Sprintf(
					"WARN: tenant=%s: unknown base metric %q in dimensional key %q",
					tenant, baseKey, key))
				continue
			}

			// _critical suffix → skip (base validation done by ResolveAt)
			if strings.HasSuffix(key, "_critical") {
				continue
			}

			// Normal metric key → must exist in defaults
			if _, exists := c.Defaults[key]; exists {
				continue
			}

			// Underscore-prefixed but not reserved → likely typo
			if strings.HasPrefix(key, "_") {
				warnings = append(warnings, fmt.Sprintf(
					"WARN: tenant=%s: unknown reserved key %q (typo?)", tenant, key))
				continue
			}

			// Not in defaults, not reserved → unknown metric key
			warnings = append(warnings, fmt.Sprintf(
				"WARN: tenant=%s: unknown key %q not in defaults", tenant, key))
		}
	}

	return warnings
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

		// Fill-in: copy profile keys that the tenant has NOT overridden
		for key, profileValue := range profile {
			if _, tenantHas := overrides[key]; !tenantHas {
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
