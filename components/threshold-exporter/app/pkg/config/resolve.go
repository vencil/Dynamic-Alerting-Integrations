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
func (c *ThresholdConfig) ResolveAt(now time.Time) []ResolvedThreshold {
	var result []ResolvedThreshold

	// Cardinality limit per tenant (0 = no limit)
	limit := c.MaxMetricsPerTenant
	if limit == 0 {
		limit = DefaultMaxMetricsPerTenant
	}
	tenantCount := make(map[string]int)

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
		if limit > 0 && count > limit {
			log.Printf("ERROR: tenant=%s produced %d metrics (limit=%d), truncating to limit", tenant, count, limit)
			result = result[:startIdx+limit]
		}
	}

	return result
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
