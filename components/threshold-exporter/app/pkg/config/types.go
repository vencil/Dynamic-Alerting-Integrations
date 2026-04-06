package config

import "time"

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
// Prometheus metric: tenant_metadata_info{tenant, runbook_url, owner, tier} = 1 (info metric).
// Unconditionally emitted for ALL tenants — unset fields default to empty string.
// This guarantees PromQL group_left joins always succeed (no False Negatives).
//
// v2.5.0: Added environment, region, domain, db_type, tags, groups for API/UI grouping.
// These new fields are NOT emitted as Prometheus labels (cardinality concern) —
// they are consumed by tenant-api and generate_tenant_metadata.py only.
type TenantMetadata struct {
	RunbookURL  string   `yaml:"runbook_url"`
	Owner       string   `yaml:"owner"`
	Tier        string   `yaml:"tier"`
	Environment string   `yaml:"environment"` // v2.5.0: production | staging | development
	Region      string   `yaml:"region"`      // v2.5.0: cloud region (e.g., ap-northeast-1)
	Domain      string   `yaml:"domain"`      // v2.5.0: business domain (e.g., finance, cache)
	DBType      string   `yaml:"db_type"`     // v2.5.0: database type (e.g., mariadb, postgresql)
	Tags        []string `yaml:"tags"`        // v2.5.0: free-form tags for ad-hoc filtering
	Groups      []string `yaml:"groups"`      // v2.5.0: group memberships (references _groups.yaml)
}

// ResolvedMetadata is the resolved metadata for one tenant.
// v2.5.0: Extended with grouping fields for API/UI consumption.
type ResolvedMetadata struct {
	Tenant      string
	RunbookURL  string
	Owner       string
	Tier        string
	Environment string
	Region      string
	Domain      string
	DBType      string
	Tags        []string
	Groups      []string
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

// DefaultMaxMetricsPerTenant is the default cardinality limit per tenant.
// 0 means no limit (backward compatible).
const DefaultMaxMetricsPerTenant = 500

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

// validReservedKeys lists tenant config keys with special meaning.
// Any key not matching these patterns AND not in defaults is suspicious.
// Source of truth (Python): scripts/tools/_lib_python.py — keep in sync.
var validReservedKeys = map[string]bool{
	"_silent_mode":     true,
	"_severity_dedup":  true,
	"_namespaces":      true, // v1.8.0: metadata for N:1 tenant mapping tooling
	"_metadata":        true, // v1.11.0: tenant metadata (runbook_url, owner, tier) → tenant_metadata_info metric
	"_profile":         true, // v1.12.0: tenant profile reference for four-layer inheritance
	"_routing_profile": true, // v2.1.0 ADR-007: cross-domain routing profile reference
}

// validReservedPrefixes lists prefixes for tenant config keys with special meaning.
var validReservedPrefixes = []string{
	"_state_",
	"_routing",
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

// ConfigInfo holds config source metadata for the threshold_exporter_config_info metric.
type ConfigInfo struct {
	ConfigSource string
	GitCommit    string
}
