package main

import (
	"crypto/sha256"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// StateFilter defines a state-based monitoring filter (Scenario C).
// Each filter maps to kube_pod_container_status_waiting_reason or similar K8s state metrics.
// Per-tenant enable/disable is controlled via _state_<filter_name> in the tenants map.
type StateFilter struct {
	Reasons      []string `yaml:"reasons"`       // K8s waiting/terminated reasons to match
	Severity     string   `yaml:"severity"`      // Alert severity (default: "warning")
	DefaultState string   `yaml:"default_state"` // "enable" (default) or "disable" — 控制未設定 _state_ 時的預設行為
}

// ResolvedStateFilter is the resolved state for one tenant+filter pair.
// Exposed as user_state_filter{tenant, filter, severity} = 1.0 (flag gauge).
// Disabled filters produce no metric (same "absent = disabled" pattern as numeric thresholds).
type ResolvedStateFilter struct {
	Tenant     string
	FilterName string
	Severity   string
}

// ResolvedSilentMode is the resolved silent mode for one tenant.
// Exposed as user_silent_mode{tenant, target_severity} = 1.0 (flag gauge).
// Silent mode: alerts fire (TSDB records exist) but Alertmanager notifications are suppressed.
// This is distinct from maintenance mode which suppresses alerts at PromQL level (no TSDB records).
//
// Tenant config (scalar, backward compatible):
//
//	_silent_mode: "warning" | "critical" | "all" | "disable"
//
// Tenant config (structured, v1.7.0+):
//
//	_silent_mode:
//	  target: "warning" | "critical" | "all"
//	  expires: "2026-04-01T00:00:00Z"  # ISO 8601, optional
//	  reason: "Planned DB migration"    # optional
//
// Default (absent): Normal — no silent mode, all notifications delivered.
// When expires is set and past, the silent mode is treated as expired — sentinel metric stops emitting.
type ResolvedSilentMode struct {
	Tenant         string
	TargetSeverity string    // "warning" or "critical" — one struct per severity
	Expires        time.Time // zero value = no expiry
	Reason         string    // human-readable reason (optional)
	Expired        bool      // true if expires is set and past
}

// ResolvedMaintenanceExpiry tracks maintenance mode expiry state for a tenant.
// Used to emit da_config_event metric when maintenance mode expires.
type ResolvedMaintenanceExpiry struct {
	Tenant  string
	Expires time.Time
	Reason  string
	Expired bool
}

// silentModeStructured is the intermediate struct for parsing structured _silent_mode YAML.
type silentModeStructured struct {
	Target  string `yaml:"target"`
	Expires string `yaml:"expires"`
	Reason  string `yaml:"reason"`
}

// maintenanceModeStructured is the intermediate struct for parsing structured _state_maintenance YAML.
type maintenanceModeStructured struct {
	Target    string              `yaml:"target"`    // "enable" (default if omitted)
	Expires   string              `yaml:"expires"`   // ISO 8601
	Reason    string              `yaml:"reason"`
	Recurring []RecurringSchedule `yaml:"recurring"` // v1.11.0: periodic maintenance windows
}

// RecurringSchedule defines a periodic maintenance window (v1.11.0).
// Used by da-tools maintenance-scheduler CronJob to create Alertmanager silences.
// The Go exporter stores this data but does not act on it — the CronJob reads
// conf.d/ and evaluates cron expressions at runtime.
type RecurringSchedule struct {
	Cron     string `yaml:"cron"`     // Standard 5-field cron expression
	Duration string `yaml:"duration"` // Go-style duration (e.g., "4h", "30m")
	Reason   string `yaml:"reason"`   // Human-readable reason (optional)
}

// TenantMetadata holds optional metadata labels for a tenant.
// Exposed as tenant_metadata_info{tenant, runbook_url, owner, tier} = 1 (info metric).
// Unconditionally emitted for ALL tenants — unset fields default to empty string.
// This guarantees PromQL group_left joins always succeed (no False Negatives).
type TenantMetadata struct {
	RunbookURL string `yaml:"runbook_url"`
	Owner      string `yaml:"owner"`
	Tier       string `yaml:"tier"`
}

// ResolvedMetadata is the resolved metadata for one tenant.
type ResolvedMetadata struct {
	Tenant     string
	RunbookURL string
	Owner      string
	Tier       string
}

// TimeWindowOverride defines a UTC time window with an override value.
// Window format: "HH:MM-HH:MM" (UTC-only, cross-midnight supported).
//
// Example:
//
//	overrides:
//	  - window: "01:00-09:00"
//	    value: "1000"
type TimeWindowOverride struct {
	Window string `yaml:"window"` // "HH:MM-HH:MM" (UTC)
	Value  string `yaml:"value"`  // same value syntax as existing ("70", "disable", "500:critical")
}

// ScheduledValue supports both simple scalar strings (backward compatible)
// and structured values with time-window overrides (Phase 11 — B4).
//
// Scalar format (existing):
//
//	mysql_connections: "70"
//
// Structured format (new):
//
//	mysql_connections:
//	  default: "70"
//	  overrides:
//	    - window: "01:00-09:00"
//	      value: "1000"
type ScheduledValue struct {
	Default   string
	Overrides []TimeWindowOverride
}

// UnmarshalYAML implements custom YAML unmarshalling for ScheduledValue.
// Supports three forms:
//  1. Scalar string (backward compatible): "80"
//  2. Structured mapping with default+overrides: {default: "80", overrides: [...]}
//  3. Arbitrary mapping (e.g., _routing): {receiver: "...", group_wait: "30s"}
//     → serialized back to YAML string and stored in Default for downstream parsing
func (sv *ScheduledValue) UnmarshalYAML(value *yaml.Node) error {
	if value.Kind == yaml.ScalarNode {
		sv.Default = value.Value
		return nil
	}
	if value.Kind == yaml.MappingNode {
		// Check if this mapping has a "default" key (structured ScheduledValue)
		hasDefault := false
		for i := 0; i < len(value.Content)-1; i += 2 {
			if value.Content[i].Value == "default" {
				hasDefault = true
				break
			}
		}
		if hasDefault {
			var structured struct {
				Default   string              `yaml:"default"`
				Overrides []TimeWindowOverride `yaml:"overrides"`
			}
			if err := value.Decode(&structured); err != nil {
				return err
			}
			sv.Default = structured.Default
			sv.Overrides = structured.Overrides
			return nil
		}
		// Arbitrary mapping (e.g., _routing): serialize back to YAML string
		var raw interface{}
		if err := value.Decode(&raw); err != nil {
			return err
		}
		out, err := yaml.Marshal(raw)
		if err != nil {
			return fmt.Errorf("ScheduledValue: failed to re-serialize mapping: %w", err)
		}
		sv.Default = string(out)
		return nil
	}
	return fmt.Errorf("ScheduledValue: unsupported YAML node kind %d", value.Kind)
}

// String returns the default value for backward-compatible string access.
func (sv ScheduledValue) String() string {
	return sv.Default
}

// ResolveValue returns the effective value at the given time.
// If a time-window override matches, its value is returned; otherwise the default.
func (sv ScheduledValue) ResolveValue(now time.Time) string {
	for _, o := range sv.Overrides {
		if matchTimeWindow(o.Window, now) {
			return o.Value
		}
	}
	return sv.Default
}

// matchTimeWindow checks if the given time falls within a UTC "HH:MM-HH:MM" window.
// Supports cross-midnight windows (e.g., "22:00-06:00").
func matchTimeWindow(window string, now time.Time) bool {
	parts := strings.SplitN(window, "-", 2)
	if len(parts) != 2 {
		log.Printf("WARN: invalid time window format %q", window)
		return false
	}
	startH, startM, err1 := parseHHMM(parts[0])
	endH, endM, err2 := parseHHMM(parts[1])
	if err1 != nil || err2 != nil {
		log.Printf("WARN: invalid time window %q: start=%v end=%v", window, err1, err2)
		return false
	}

	utcNow := now.UTC()
	nowMinutes := utcNow.Hour()*60 + utcNow.Minute()
	startMinutes := startH*60 + startM
	endMinutes := endH*60 + endM

	if startMinutes <= endMinutes {
		// Same day: e.g., 01:00-09:00
		return nowMinutes >= startMinutes && nowMinutes < endMinutes
	}
	// Cross midnight: e.g., 22:00-06:00
	return nowMinutes >= startMinutes || nowMinutes < endMinutes
}

// parseHHMM parses "HH:MM" into hour and minute.
func parseHHMM(s string) (int, int, error) {
	s = strings.TrimSpace(s)
	parts := strings.SplitN(s, ":", 2)
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("invalid HH:MM format: %q", s)
	}
	h, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil || h < 0 || h > 23 {
		return 0, 0, fmt.Errorf("invalid hour in %q", s)
	}
	m, err := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err != nil || m < 0 || m > 59 {
		return 0, 0, fmt.Errorf("invalid minute in %q", s)
	}
	return h, m, nil
}

// ThresholdConfig represents the YAML config structure.
//
// Example config:
//
//	defaults:
//	  mysql_connections: 80
//	  mysql_cpu: 80
//	state_filters:
//	  container_crashloop:
//	    reasons: ["CrashLoopBackOff"]
//	    severity: "critical"
//	tenants:
//	  db-a:
//	    mysql_connections: "70"
//	    mysql_connections_backup:             # B4: scheduled override
//	      default: "70"
//	      overrides:
//	        - window: "01:00-09:00"
//	          value: "1000"
//	  db-b:
//	    mysql_connections: "disable"
//	    _state_container_crashloop: "disable"
// DefaultMaxMetricsPerTenant is the default cardinality limit per tenant.
// 0 means no limit (backward compatible).
const DefaultMaxMetricsPerTenant = 500

type ThresholdConfig struct {
	Defaults            map[string]float64                   `yaml:"defaults"`
	StateFilters        map[string]StateFilter               `yaml:"state_filters"`
	Tenants             map[string]map[string]ScheduledValue `yaml:"tenants"`
	Profiles            map[string]map[string]ScheduledValue `yaml:"profiles"`
	MaxMetricsPerTenant int                                  `yaml:"max_metrics_per_tenant"`
}

// ResolvedThreshold is the final resolved state for one tenant+metric pair.
// Phase 2B: CustomLabels supports dimensional metrics (e.g., queue="tasks").
// Phase 11 B1: RegexLabels supports regex dimensional metrics (e.g., tablespace=~"SYS.*").
type ResolvedThreshold struct {
	Tenant       string
	Metric       string
	Value        float64
	Severity     string
	Component    string
	CustomLabels map[string]string // dimensional labels from {key="value"} syntax
	RegexLabels  map[string]string // regex labels from {key=~"pattern"} syntax
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

// ResolvedSeverityDedup represents a tenant's severity deduplication preference.
// When mode="enable", Alertmanager inhibit rules suppress warning notifications
// when critical fires for the same tenant+metric_group.
// When mode="disable" or absent, both warning and critical notifications are sent.
//
// Default (absent): "enable" — backward compatible, suppress warning when critical fires.
// Tenant config: _severity_dedup: "enable" | "disable"
type ResolvedSeverityDedup struct {
	Tenant string
	Mode   string // "enable" or "disable"
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

// validReservedKeys lists tenant config keys with special meaning.
// Any key not matching these patterns AND not in defaults is suspicious.
// Source of truth (Python): scripts/tools/_lib_python.py — keep in sync.
var validReservedKeys = map[string]bool{
	"_silent_mode":      true,
	"_severity_dedup":   true,
	"_namespaces":       true,  // v1.8.0: metadata for N:1 tenant mapping tooling
	"_metadata":         true,  // v1.11.0: tenant metadata (runbook_url, owner, tier) → tenant_metadata_info metric
	"_profile":          true,  // v1.12.0: tenant profile reference for four-layer inheritance
	"_routing_profile":  true,  // v2.1.0 ADR-007: cross-domain routing profile reference
}

// validReservedPrefixes lists prefixes for tenant config keys with special meaning.
var validReservedPrefixes = []string{
	"_state_",
	"_routing",
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

			// _critical suffix → check base exists
			if strings.HasSuffix(key, "_critical") {
				baseKey := strings.TrimSuffix(key, "_critical")
				if _, exists := c.Defaults[baseKey]; exists {
					continue
				}
				// Already warned by ResolveAt, skip here
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

// applyProfiles expands profile values into tenant overrides (fill-in, not overwrite).
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
func (c *ThresholdConfig) applyProfiles() {
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

// RoutingConfig represents a tenant's alert routing preferences.
// Used by generate_alertmanager_routes.py to produce Alertmanager route/receiver fragments.
//
// NOTE: ResolveRouting() is currently not called by the exporter (routing config is
// consumed by the Python tooling, not by Prometheus). It is retained as:
//   - Guardrail reference implementation (must stay consistent with Python's GUARDRAILS)
//   - Foundation for potential future routing metrics (e.g., user_routing_configured{tenant})
//   - Validation test coverage (TestResolveRouting_* tests verify YAML round-trip fidelity)
//
// Tenant config (v1.3.0 structured receiver):
//
//	_routing:
//	  receiver:
//	    type: "webhook"
//	    url: "https://webhook.example.com/alerts"
//	  group_by: ["alertname", "severity"]
//	  group_wait: "30s"
//	  group_interval: "1m"
//	  repeat_interval: "4h"
type RoutingConfig struct {
	Tenant         string
	ReceiverType   string                 // "webhook" | "email" | "slack" | "teams"
	ReceiverConfig map[string]interface{} // type-specific config fields
	GroupBy        []string               // optional, platform default if absent
	GroupWait      string                 // optional, guardrail 5s–5m
	GroupInterval  string                 // optional, guardrail 5s–5m
	RepeatInterval string                 // optional, guardrail 1m–72h
}

// validReceiverTypes lists supported receiver types (must match Python RECEIVER_TYPES).
var validReceiverTypes = map[string]bool{
	"webhook":    true,
	"email":      true,
	"slack":      true,
	"teams":      true,
	"rocketchat": true,
	"pagerduty":  true,
}

// Timing guardrail bounds for routing config.
var routingGuardrails = map[string][2]time.Duration{
	"group_wait":      {5 * time.Second, 5 * time.Minute},
	"group_interval":  {5 * time.Second, 5 * time.Minute},
	"repeat_interval": {1 * time.Minute, 72 * time.Hour},
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

// clampDuration validates a duration string against guardrails.
// Returns the original string if within bounds, or the clamped value with a warning.
func clampDuration(value, param, tenant string) string {
	bounds, ok := routingGuardrails[param]
	if !ok {
		return value
	}

	d, err := time.ParseDuration(value)
	if err != nil {
		// Try Prometheus-style duration (e.g., "30s", "5m", "4h")
		d, err = parsePromDuration(value)
		if err != nil {
			log.Printf("WARN: invalid %s %q for tenant=%s, ignoring", param, value, tenant)
			return ""
		}
	}

	if d < bounds[0] {
		clamped := formatDuration(bounds[0])
		log.Printf("WARN: %s %q for tenant=%s below minimum %s, clamping to %s", param, value, tenant, formatDuration(bounds[0]), clamped)
		return clamped
	}
	if d > bounds[1] {
		clamped := formatDuration(bounds[1])
		log.Printf("WARN: %s %q for tenant=%s above maximum %s, clamping to %s", param, value, tenant, formatDuration(bounds[1]), clamped)
		return clamped
	}

	return value
}

// parsePromDuration parses Prometheus-style duration strings like "30s", "5m", "4h".
func parsePromDuration(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if len(s) < 2 {
		return 0, fmt.Errorf("duration too short: %q", s)
	}

	unit := s[len(s)-1]
	numStr := s[:len(s)-1]
	num, err := strconv.ParseFloat(numStr, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid number in duration %q: %w", s, err)
	}

	switch unit {
	case 's':
		return time.Duration(num * float64(time.Second)), nil
	case 'm':
		return time.Duration(num * float64(time.Minute)), nil
	case 'h':
		return time.Duration(num * float64(time.Hour)), nil
	case 'd':
		return time.Duration(num * 24 * float64(time.Hour)), nil
	default:
		return 0, fmt.Errorf("unknown duration unit %q in %q", string(unit), s)
	}
}

// formatDuration formats a time.Duration as a human-readable Prometheus-style string.
func formatDuration(d time.Duration) string {
	// NOTE: Prometheus/Alertmanager duration format only supports s/m/h (not d/w/y).
	// Do NOT convert to days even if evenly divisible.
	if d >= time.Hour && d%time.Hour == 0 {
		return fmt.Sprintf("%dh", int(d/time.Hour))
	}
	if d >= time.Minute && d%time.Minute == 0 {
		return fmt.Sprintf("%dm", int(d/time.Minute))
	}
	return fmt.Sprintf("%ds", int(d/time.Second))
}

// isDisabled checks if a value string means "disabled".
func isDisabled(lower string) bool {
	return lower == "disable" || lower == "disabled" || lower == "off" || lower == "false"
}

// parseMetricKey splits "mysql_connections" into ("mysql", "connections").
// If no underscore, component defaults to "default".
func parseMetricKey(key string) (component, metric string) {
	idx := strings.Index(key, "_")
	if idx < 0 {
		return "default", key
	}
	return key[:idx], key[idx+1:]
}

// keyWithLabelsRe matches "metric_name{label1=\"val1\", label2=\"val2\"}"
var keyWithLabelsRe = regexp.MustCompile(`^([a-zA-Z0-9_]+)\{(.+)\}$`)

// parseKeyWithLabels splits a metric key that may contain dimensional labels.
// Returns base key, exact-match labels (=), and regex-match labels (=~).
//
// Examples:
//
//	"redis_queue_length"                                         → ("redis_queue_length", nil, nil)
//	"redis_queue_length{queue=\"tasks\", priority=\"high\"}"     → ("redis_queue_length", {"queue":"tasks","priority":"high"}, nil)
//	"oracle_tablespace{tablespace=~\"SYS.*\"}"                  → ("oracle_tablespace", nil, {"tablespace":"SYS.*"})
//	"oracle_ts{env=\"prod\", tablespace=~\"SYS.*\"}"            → ("oracle_ts", {"env":"prod"}, {"tablespace":"SYS.*"})
func parseKeyWithLabels(key string) (string, map[string]string, map[string]string) {
	m := keyWithLabelsRe.FindStringSubmatch(key)
	if m == nil {
		return key, nil, nil
	}
	baseKey := m[1]
	exact, regex := parseLabelsStringWithOp(m[2])
	if len(exact) == 0 {
		exact = nil
	}
	if len(regex) == 0 {
		regex = nil
	}
	if exact == nil && regex == nil {
		return baseKey, nil, nil
	}
	return baseKey, exact, regex
}

// parseLabelsStringWithOp parses a comma-separated label string into exact and regex maps.
// Supports both = (exact match) and =~ (regex match) operators.
//
// Input: `queue="tasks", tablespace=~"SYS.*"`
// Returns: exact={"queue":"tasks"}, regex={"tablespace":"SYS.*"}
func parseLabelsStringWithOp(s string) (exact map[string]string, regex map[string]string) {
	exact = make(map[string]string)
	regex = make(map[string]string)
	pairs := strings.Split(s, ",")
	for _, pair := range pairs {
		pair = strings.TrimSpace(pair)
		// Check for =~ first (must check before = to avoid partial match)
		if idx := strings.Index(pair, "=~"); idx >= 0 {
			k := strings.TrimSpace(pair[:idx])
			v := strings.TrimSpace(pair[idx+2:])
			v = strings.Trim(v, `"'`)
			if k != "" {
				regex[k] = v
			}
			continue
		}
		// Regular = operator
		eqIdx := strings.Index(pair, "=")
		if eqIdx < 0 {
			continue
		}
		k := strings.TrimSpace(pair[:eqIdx])
		v := strings.TrimSpace(pair[eqIdx+1:])
		// Strip surrounding quotes (single or double)
		v = strings.Trim(v, `"'`)
		if k != "" {
			exact[k] = v
		}
	}
	return
}

// ============================================================
// ConfigManager — supports both single-file and directory mode
// ============================================================

// ConfigManager handles loading and hot-reloading the config.
// Supports two modes:
//   - Single-file mode (legacy): reads one YAML file
//   - Directory mode: scans all *.yaml files in a directory and deep-merges
//
// In directory mode, ConfigManager supports incremental hot-reload (v2.1.0):
// per-file SHA-256 tracking + parsed config cache → only changed files are
// re-parsed on each reload cycle, then all cached partials are merged.
type ConfigManager struct {
	path     string // file path or directory path
	isDir    bool   // true = directory mode
	mu       sync.RWMutex
	config   *ThresholdConfig
	loaded   bool
	lastReload time.Time
	lastHash   string // SHA-256 composite hash for change detection

	// Incremental reload state (directory mode only, v2.1.0)
	fileHashes  map[string]string          // filename → SHA-256
	fileConfigs map[string]ThresholdConfig // filename → parsed partial config
	fileMtimes  map[string]fileStat        // filename → mtime+size for quick skip (v2.1.0)
}

// fileStat stores lightweight file metadata for the mtime guard.
// If both ModTime and Size match the previous scan, the file's SHA-256
// is reused without re-reading the file contents.
type fileStat struct {
	ModTime int64 // UnixNano
	Size    int64
}

func NewConfigManager(path string) *ConfigManager {
	info, err := os.Stat(path)
	isDir := err == nil && info.IsDir()

	return &ConfigManager{
		path:  path,
		isDir: isDir,
	}
}

// Mode returns "directory" or "single-file" for diagnostics.
func (m *ConfigManager) Mode() string {
	if m.isDir {
		return "directory"
	}
	return "single-file"
}

// Load loads config from either a single file or a directory.
func (m *ConfigManager) Load() error {
	var cfg ThresholdConfig
	var hash string
	var err error

	if m.isDir {
		cfg, hash, err = loadDir(m.path)
	} else {
		cfg, hash, err = loadFile(m.path)
	}
	if err != nil {
		return err
	}

	// Ensure maps are initialized
	if cfg.Defaults == nil {
		cfg.Defaults = make(map[string]float64)
	}
	if cfg.Tenants == nil {
		cfg.Tenants = make(map[string]map[string]ScheduledValue)
	}
	if cfg.StateFilters == nil {
		cfg.StateFilters = make(map[string]StateFilter)
	}
	if cfg.Profiles == nil {
		cfg.Profiles = make(map[string]map[string]ScheduledValue)
	}

	// Expand profile values into tenant overrides (v1.12.0)
	cfg.applyProfiles()

	m.mu.Lock()
	m.config = &cfg
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = hash
	m.mu.Unlock()

	logConfigStats(&cfg, fmt.Sprintf("Config loaded (%s)", m.Mode()))

	return nil
}

// loadFile reads a single YAML config file and returns the parsed config + content hash.
func loadFile(path string) (ThresholdConfig, string, error) {
	var cfg ThresholdConfig

	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, "", fmt.Errorf("read config %s: %w", path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))

	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return cfg, "", fmt.Errorf("parse config %s: %w", path, err)
	}

	return cfg, hash, nil
}

// loadDir scans a directory for *.yaml files, parses and deep-merges them.
//
// File naming convention:
//   - _defaults.yaml: contains 'defaults' and 'state_filters' (loaded first due to underscore prefix)
//   - <tenant-name>.yaml: contains tenant-specific overrides under 'tenants' key
//
// Merge rules:
//   - Files are processed in sorted order (underscore prefix sorts first)
//   - defaults: later values overwrite earlier ones for the same key
//   - state_filters: later values overwrite earlier ones for the same filter name
//   - tenants: deep merge per tenant (later key-values overwrite)
//
// Boundary rule: state_filters should only be defined in _defaults.yaml.
// Tenant files should only contain a 'tenants' block. This is enforced with warnings.
func loadDir(dir string) (ThresholdConfig, string, error) {
	merged := ThresholdConfig{
		Defaults:     make(map[string]float64),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return merged, "", fmt.Errorf("read config dir %s: %w", dir, err)
	}

	// Collect *.yaml files, sorted (underscore prefix sorts first)
	var files []string
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || strings.HasPrefix(name, ".") {
			continue
		}
		if strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".yml") {
			files = append(files, name)
		}
	}
	sort.Strings(files)

	if len(files) == 0 {
		return merged, "", fmt.Errorf("no .yaml files found in %s", dir)
	}

	// Hash all file contents for change detection
	hasher := sha256.New()

	for _, name := range files {
		path := filepath.Join(dir, name)
		data, err := os.ReadFile(path)
		if err != nil {
			log.Printf("WARN: skip unreadable file %s: %v", path, err)
			continue
		}
		hasher.Write(data)

		var partial ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			log.Printf("WARN: skip unparseable file %s: %v", path, err)
			continue
		}

		isDefaultsFile := strings.HasPrefix(name, "_")
		isProfilesFile := name == "_profiles.yaml" || name == "_profiles.yml"

		// Boundary enforcement: warn if tenant file contains state_filters, defaults, or profiles
		if !isDefaultsFile {
			if len(partial.StateFilters) > 0 {
				log.Printf("WARN: state_filters found in %s — should only be in _defaults.yaml, ignoring", name)
				partial.StateFilters = nil
			}
			if len(partial.Defaults) > 0 {
				log.Printf("WARN: defaults found in %s — should only be in _defaults.yaml, ignoring", name)
				partial.Defaults = nil
			}
		}
		if !isProfilesFile && !isDefaultsFile {
			if len(partial.Profiles) > 0 {
				log.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
				partial.Profiles = nil
			}
		}

		// Merge defaults
		for k, v := range partial.Defaults {
			merged.Defaults[k] = v
		}

		// Merge state_filters
		for k, v := range partial.StateFilters {
			merged.StateFilters[k] = v
		}

		// Merge profiles (v1.12.0)
		for profileName, profileValues := range partial.Profiles {
			if merged.Profiles[profileName] == nil {
				merged.Profiles[profileName] = make(map[string]ScheduledValue)
			}
			for k, v := range profileValues {
				merged.Profiles[profileName][k] = v
			}
		}

		// Merge tenants (deep merge per tenant)
		for tenant, overrides := range partial.Tenants {
			if merged.Tenants[tenant] == nil {
				merged.Tenants[tenant] = make(map[string]ScheduledValue)
			}
			for k, v := range overrides {
				merged.Tenants[tenant][k] = v
			}
		}
	}

	hash := fmt.Sprintf("%x", hasher.Sum(nil))
	return merged, hash, nil
}

// scanDirFileHashes scans a directory and returns per-file SHA-256 hashes,
// the composite hash, per-file mtime+size stats, and a byte cache of files
// that were actually read (for reuse by callers that need file contents,
// avoiding double disk reads in fullDirLoad/IncrementalLoad).
//
// Uses DirEntry.Info() to get mtime+size from the directory listing itself,
// avoiding separate os.Stat calls per file.
//
// When oldHashes and oldMtimes are provided (non-nil), the mtime guard kicks in:
// files whose ModTime and Size match the previous scan reuse the cached SHA-256
// without re-reading file contents. This reduces NoChange cost from O(N×read)
// to O(N×stat) — typically 4-5× faster at 1000 tenants.
func scanDirFileHashes(dir string, oldHashes map[string]string, oldMtimes map[string]fileStat) (map[string]string, string, map[string]fileStat, map[string][]byte, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, "", nil, nil, fmt.Errorf("read config dir %s: %w", dir, err)
	}

	type dirFile struct {
		name string
		info os.FileInfo // from DirEntry.Info(), avoids separate os.Stat
	}
	var files []dirFile
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || strings.HasPrefix(name, ".") {
			continue
		}
		if strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".yml") {
			info, ierr := entry.Info()
			if ierr != nil {
				log.Printf("WARN: skip unreadable entry %s: %v", name, ierr)
				continue
			}
			files = append(files, dirFile{name: name, info: info})
		}
	}
	sort.Slice(files, func(i, j int) bool { return files[i].name < files[j].name })

	perFile := make(map[string]string, len(files))
	mtimes := make(map[string]fileStat, len(files))
	dataCache := make(map[string][]byte)
	compositeHasher := sha256.New()

	for _, f := range files {
		cur := fileStat{ModTime: f.info.ModTime().UnixNano(), Size: f.info.Size()}
		fullPath := filepath.Join(dir, f.name)

		// Mtime guard: reuse cached hash if mtime+size unchanged and file
		// is older than 2 seconds (safety window for coarse-mtime filesystems).
		if oldHashes != nil && oldMtimes != nil {
			age := time.Since(f.info.ModTime())
			if prev, ok := oldMtimes[f.name]; ok && age > 2*time.Second {
				if oldHash, hok := oldHashes[f.name]; hok && cur == prev {
					perFile[f.name] = oldHash
					mtimes[f.name] = cur
					compositeHasher.Write([]byte(oldHash))
					continue
				}
			}
		}

		data, rerr := os.ReadFile(fullPath)
		if rerr != nil {
			log.Printf("WARN: skip unreadable file %s: %v", f.name, rerr)
			continue
		}
		h := fmt.Sprintf("%x", sha256.Sum256(data))
		perFile[f.name] = h
		mtimes[f.name] = cur
		compositeHasher.Write([]byte(h))
		// Only cache bytes for files whose hash changed or is new (saves memory
		// in incremental path where 999/1000 files are unchanged).
		if oldHashes == nil {
			// First load: cache everything (fullDirLoad needs all bytes)
			dataCache[f.name] = data
		} else if oldH, ok := oldHashes[f.name]; !ok || oldH != h {
			// Changed or added file: cache for Phase 3 re-parse
			dataCache[f.name] = data
		}
	}

	return perFile, fmt.Sprintf("%x", compositeHasher.Sum(nil)), mtimes, dataCache, nil
}

// IncrementalLoad performs an incremental reload in directory mode.
// It compares per-file hashes with the cached state, re-parses only
// changed/added files, removes deleted files from cache, then rebuilds
// the merged config from cached partials.
//
// Falls back to full Load() for single-file mode or first-time load.
func (m *ConfigManager) IncrementalLoad() error {
	// Single-file mode or first load: fall back to full Load
	if !m.isDir {
		return m.Load()
	}

	m.mu.RLock()
	hasCache := m.fileHashes != nil && len(m.fileHashes) > 0
	m.mu.RUnlock()

	if !hasCache {
		return m.fullDirLoad()
	}

	// Phase 1: scan per-file hashes with mtime guard (cheap — stat + skip unchanged)
	m.mu.RLock()
	oldH := m.fileHashes
	oldM := m.fileMtimes
	prevHash := m.lastHash
	m.mu.RUnlock()

	newHashes, compositeHash, newMtimes, dataCache, err := scanDirFileHashes(m.path, oldH, oldM)
	if err != nil {
		return err
	}

	// Quick check: composite hash unchanged → no work needed
	unchanged := compositeHash == prevHash
	if unchanged {
		return nil
	}

	// Phase 2: diff per-file hashes → identify changed/added/removed
	m.mu.RLock()
	oldHashes := m.fileHashes
	oldConfigs := m.fileConfigs
	m.mu.RUnlock()

	var changed, added, removed []string

	// Detect changed and added files
	for name, newHash := range newHashes {
		oldHash, exists := oldHashes[name]
		if !exists {
			added = append(added, name)
		} else if newHash != oldHash {
			changed = append(changed, name)
		}
	}

	// Detect removed files
	for name := range oldHashes {
		if _, exists := newHashes[name]; !exists {
			removed = append(removed, name)
		}
	}

	// Copy cache for mutation — deferred until after diff to avoid
	// unnecessary allocation when the per-file diff shows no changes
	// (composite hash collision or race condition edge case).
	newConfigs := make(map[string]ThresholdConfig, len(oldConfigs))
	for k, v := range oldConfigs {
		newConfigs[k] = v
	}

	// Phase 3: re-parse only changed + added files.
	// Reuse file bytes from scan phase (dataCache) to avoid double disk read.
	reparse := append(changed, added...)
	sort.Strings(reparse)
	for _, name := range reparse {
		fullPath := filepath.Join(m.path, name)
		data, ok := dataCache[name]
		if !ok {
			// Fallback: file not in cache (shouldn't happen, but be safe)
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				log.Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
				delete(newConfigs, name)
				continue
			}
		}
		var partial ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			log.Printf("WARN: skip unparseable file %s: %v", fullPath, err)
			delete(newConfigs, name)
			continue
		}
		// Apply boundary enforcement (same rules as loadDir)
		applyBoundaryRules(name, &partial)
		newConfigs[name] = partial
	}

	// Remove deleted files from cache
	for _, name := range removed {
		delete(newConfigs, name)
	}

	// Phase 4: merge — use incremental patch when only tenant files changed,
	// full rebuild when _defaults.yaml, _profiles.yaml, or _state_filters changed.
	tenantOnly := true
	for _, name := range append(changed, added...) {
		if name == "_defaults.yaml" || name == "_profiles.yaml" || strings.HasPrefix(name, "_") {
			tenantOnly = false
			break
		}
	}
	for _, name := range removed {
		if strings.HasPrefix(name, "_") {
			tenantOnly = false
			break
		}
	}
	var merged ThresholdConfig
	if tenantOnly && m.config != nil {
		// Incremental patch: copy existing merged config, patch only affected tenants.
		// This avoids O(N) merge for the common "1 tenant file changed" case.
		m.mu.RLock()
		prev := m.config
		m.mu.RUnlock()

		merged = ThresholdConfig{
			Defaults:     prev.Defaults,     // shared (immutable between patches)
			StateFilters: prev.StateFilters,  // shared
			Profiles:     prev.Profiles,      // shared
			Tenants:      make(map[string]map[string]ScheduledValue, len(prev.Tenants)),
		}
		// Shallow-copy tenants map (keys only, values are immutable per-tenant maps)
		for k, v := range prev.Tenants {
			merged.Tenants[k] = v
		}
		// Apply changes: overwrite tenants from re-parsed files
		for _, name := range append(changed, added...) {
			if partial, ok := newConfigs[name]; ok {
				for tenant, overrides := range partial.Tenants {
					merged.Tenants[tenant] = overrides
				}
			}
		}
		// Remove tenants from deleted files
		for _, name := range removed {
			if partial, ok := oldConfigs[name]; ok {
				for tenant := range partial.Tenants {
					delete(merged.Tenants, tenant)
				}
			}
		}
		// Profiles unchanged → no need to re-apply
	} else {
		// Full rebuild: _defaults or _profiles changed, must re-merge everything
		merged = mergePartialConfigs(newConfigs)
		merged.applyProfiles()
	}

	// Atomic swap
	m.mu.Lock()
	m.config = &merged
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = compositeHash
	m.fileHashes = newHashes
	m.fileConfigs = newConfigs
	m.fileMtimes = newMtimes
	m.mu.Unlock()

	logConfigStats(&merged, fmt.Sprintf("Config reloaded (incremental, %d changed, %d added, %d removed)", len(changed), len(added), len(removed)))

	return nil
}

// fullDirLoad performs a full directory load and initializes the per-file cache.
// Used for the initial load and as fallback for IncrementalLoad.
func (m *ConfigManager) fullDirLoad() error {
	// Compute per-file hashes (no mtime guard on first load)
	perFileHashes, compositeHash, perFileMtimes, dataCache, err := scanDirFileHashes(m.path, nil, nil)
	if err != nil {
		return err
	}

	if len(perFileHashes) == 0 {
		return fmt.Errorf("no .yaml files found in %s", m.path)
	}

	// Parse all files using cached bytes from scan (avoids double disk read).
	fileConfigs := make(map[string]ThresholdConfig, len(perFileHashes))
	var fileNames []string
	for name := range perFileHashes {
		fileNames = append(fileNames, name)
	}
	sort.Strings(fileNames)

	for _, name := range fileNames {
		fullPath := filepath.Join(m.path, name)
		data, ok := dataCache[name]
		if !ok {
			// Fallback: read from disk (shouldn't happen on first load)
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				log.Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
				continue
			}
		}
		var partial ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			log.Printf("WARN: skip unparseable file %s: %v", fullPath, err)
			continue
		}
		applyBoundaryRules(name, &partial)
		fileConfigs[name] = partial
	}

	// Merge all partials
	merged := mergePartialConfigs(fileConfigs)
	merged.applyProfiles()

	m.mu.Lock()
	m.config = &merged
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = compositeHash
	m.fileHashes = perFileHashes
	m.fileConfigs = fileConfigs
	m.fileMtimes = perFileMtimes
	m.mu.Unlock()

	logConfigStats(&merged, fmt.Sprintf("Config loaded (%s)", m.Mode()))

	return nil
}

// logConfigStats logs config summary with cheap counts instead of calling
// the expensive Resolve()/ResolveStateFilters()/ResolveSilentModes().
// At 1000 tenants, this saves ~4ms per reload (Resolve alone costs ~2-5ms).
// The "resolved thresholds" count is estimated from tenant override counts
// rather than running the full resolution pipeline.
func logConfigStats(cfg *ThresholdConfig, prefix string) {
	// Cheap estimate: count total tenant overrides (each becomes ~1 resolved threshold)
	overrideCount := 0
	silentCount := 0
	stateCount := 0
	for _, overrides := range cfg.Tenants {
		for key := range overrides {
			switch {
			case key == "_silent_mode":
				silentCount++
			case strings.HasPrefix(key, "_state_"):
				stateCount++
			case !strings.HasPrefix(key, "_"):
				overrideCount++
			}
		}
	}

	log.Printf("%s: %d defaults, %d profiles, %d state_filters, %d tenants, ~%d threshold overrides, %d state entries, %d silent modes",
		prefix, len(cfg.Defaults), len(cfg.Profiles), len(cfg.StateFilters), len(cfg.Tenants),
		overrideCount, stateCount, silentCount)

	if warnings := cfg.ValidateTenantKeys(); len(warnings) > 0 {
		for _, w := range warnings {
			log.Printf("%s", w)
		}
	}
}

// applyBoundaryRules enforces the boundary convention: state_filters and
// defaults only in _defaults.yaml, profiles only in _profiles.yaml.
func applyBoundaryRules(name string, partial *ThresholdConfig) {
	isDefaultsFile := strings.HasPrefix(name, "_")
	isProfilesFile := name == "_profiles.yaml" || name == "_profiles.yml"

	if !isDefaultsFile {
		if len(partial.StateFilters) > 0 {
			log.Printf("WARN: state_filters found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.StateFilters = nil
		}
		if len(partial.Defaults) > 0 {
			log.Printf("WARN: defaults found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.Defaults = nil
		}
	}
	if !isProfilesFile && !isDefaultsFile {
		if len(partial.Profiles) > 0 {
			log.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
			partial.Profiles = nil
		}
	}
}

// mergePartialConfigs merges all cached partial configs in sorted filename order.
// Same merge semantics as loadDir: defaults/state_filters overwrite, tenants/profiles deep merge.
func mergePartialConfigs(configs map[string]ThresholdConfig) ThresholdConfig {
	// Pre-scan to estimate map capacities, avoiding rehash during merge.
	// In directory mode each tenant file has exactly 1 tenant, so
	// len(configs) is a reasonable upper bound for the Tenants map.
	tenantCap := 0
	defaultCap := 0
	for _, partial := range configs {
		tenantCap += len(partial.Tenants)
		if len(partial.Defaults) > defaultCap {
			defaultCap = len(partial.Defaults)
		}
	}

	merged := ThresholdConfig{
		Defaults:     make(map[string]float64, defaultCap),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue, tenantCap),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}

	// Sort filenames for deterministic merge order
	names := make([]string, 0, len(configs))
	for name := range configs {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		partial := configs[name]

		for k, v := range partial.Defaults {
			merged.Defaults[k] = v
		}
		for k, v := range partial.StateFilters {
			merged.StateFilters[k] = v
		}
		for profileName, profileValues := range partial.Profiles {
			if merged.Profiles[profileName] == nil {
				merged.Profiles[profileName] = make(map[string]ScheduledValue)
			}
			for k, v := range profileValues {
				merged.Profiles[profileName][k] = v
			}
		}
		for tenant, overrides := range partial.Tenants {
			if merged.Tenants[tenant] == nil {
				merged.Tenants[tenant] = make(map[string]ScheduledValue, len(overrides))
			}
			for k, v := range overrides {
				merged.Tenants[tenant][k] = v
			}
		}
	}

	return merged
}

// WatchLoop periodically checks for config changes and reloads.
// Uses content hash comparison for reliable change detection.
// K8s ConfigMap volumes update via symlink rotation (..data), so hash-based
// detection is more reliable than ModTime for both modes.
// The stopCh parameter allows graceful shutdown — close it to stop the loop.
//
// In directory mode, uses incremental reload (v2.1.0): per-file hash tracking
// means only changed files are re-parsed, reducing reload latency for large
// multi-tenant deployments.
func (m *ConfigManager) WatchLoop(interval time.Duration, stopCh <-chan struct{}) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-stopCh:
			log.Println("WatchLoop stopped")
			return
		case <-ticker.C:
		}

		if m.isDir {
			// Incremental reload path (v2.1.0): per-file hash check with mtime guard.
			// scanDirFileHashes uses mtime+size to skip unchanged files (stat-only).
			m.mu.RLock()
			oldH := m.fileHashes
			oldM := m.fileMtimes
			prevHash := m.lastHash
			m.mu.RUnlock()

			_, compositeHash, _, _, err := scanDirFileHashes(m.path, oldH, oldM)
			if err != nil {
				log.Printf("WARN: cannot check config %s: %v", m.path, err)
				continue
			}

			if compositeHash != prevHash {
				log.Printf("Config changed, incremental reloading...")
				if err := m.IncrementalLoad(); err != nil {
					log.Printf("ERROR: failed to reload config: %v", err)
				}
			}
		} else {
			// Single-file mode: full reload (no incremental benefit)
			_, hash, err := loadFile(m.path)
			if err != nil {
				log.Printf("WARN: cannot check config %s: %v", m.path, err)
				continue
			}

			m.mu.RLock()
			changed := hash != m.lastHash
			m.mu.RUnlock()

			if changed {
				log.Printf("Config changed, reloading...")
				if err := m.Load(); err != nil {
					log.Printf("ERROR: failed to reload config: %v", err)
				}
			}
		}
	}
}

func (m *ConfigManager) GetConfig() *ThresholdConfig {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.config
}

func (m *ConfigManager) IsLoaded() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.loaded
}

func (m *ConfigManager) LastReload() time.Time {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.lastReload
}
