package config

import (
	"strings"
	"time"
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

// ResolvedThresholdExpiry tracks a time-boxed threshold override's expiry state
// for a tenant+metric (PREVENT #656). Used by the collector to emit
// da_config_event{event="threshold_expired"} when a loosened threshold's TTL
// lapses (the value itself fail-safes back to the platform default in
// resolveBaseRows). v1: base standard metrics only (those in _defaults.yaml).
type ResolvedThresholdExpiry struct {
	Tenant    string
	MetricKey string // full conf.d key, e.g. "mysql_connections"
	Expires   time.Time
	Reason    string
	Expired   bool
}

// silentModeStructured is the intermediate struct for parsing structured _silent_mode YAML.
type silentModeStructured struct {
	Target  string `yaml:"target"`
	Expires string `yaml:"expires"`
	Reason  string `yaml:"reason"`
}

// maintenanceModeStructured is the intermediate struct for parsing structured _state_maintenance YAML.
type maintenanceModeStructured struct {
	Target    string              `yaml:"target"`  // "enable" (default if omitted)
	Expires   string              `yaml:"expires"` // ISO 8601
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
	// Expiry (PREVENT #656): non-nil makes this a *time-boxed* override that
	// auto-reverts to the platform default after Expiry.Expires (fail-safe: more
	// protection, never silent) and emits da_config_event{event="threshold_expired"}.
	// nil = permanent override. Behind a POINTER so the common (no-expiry)
	// ScheduledValue stays small — it is held per-override in
	// map[tenant]map[metric]ScheduledValue and copied on every config merge /
	// scrape, so two inline strings measurably regressed ResolveSilentModes /
	// MergePartialConfigs (bench-gate #871). Honored only on base standard metrics
	// (those in _defaults.yaml); ignored elsewhere (v1).
	Expiry *ExpiryMeta
	// deleted marks a reserved (`_`) key a `tenants:` / `profiles:` body
	// wrote as null: "delete the inherited value" (#3341,
	// reservedNullMarker). Its Default is "", so a reader that does not ask
	// reads what it always read; the readers for which "" and "no key" part
	// ask through tenantReserved.
	deleted bool
}

// ExpiryMeta carries a ScheduledValue's optional time-box (PREVENT #656).
// Allocated only when `expires:` / `reason:` are present (rare — an incident
// loosening), keeping the hot ScheduledValue struct small in the common case.
type ExpiryMeta struct {
	Expires string // RFC3339 instant; empty = no time-box
	Reason  string // human-readable reason (surfaced on expiry)
}

// DefaultMaxMetricsPerTenant is the per-tenant cardinality limit used when
// ThresholdConfig.MaxMetricsPerTenant is 0 (unset). A NEGATIVE value there
// disables truncation entirely (resolve.go only truncates when limit > 0).
// In directory mode only the ROOT `_defaults.yaml` may set it (#2028).
const DefaultMaxMetricsPerTenant = 500

// ThresholdConfig represents the YAML config structure.
//
// Example config:
//
//	defaults:
//	  mysql_connections: 80
//	  mysql_threads_running: 30              # threads_running saturation, NOT host CPU% (#944; renamed from mysql_cpu, #1231)
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
	Defaults map[string]float64 `yaml:"defaults"`
	// OptionalOverrides names keys the platform RECOGNISES but asserts no value
	// for — the runtime half of the registry's `tier: optional_overrides`
	// (docs/schemas/threshold-registry.schema.json calls tier "a load-bearing
	// field, not decoration" and the tier itself "documented-but-dormant until
	// wired"; this is that wiring).
	//
	// The config model used to have exactly two expressible states: a key is in
	// Defaults (⇒ resolveBaseRows emits a platform value for EVERY tenant), or
	// it is not (⇒ nothing is emitted AND any tenant that tries to set it is
	// refused on write — today by the shared key verdict, ADR-038 §11).
	// There was no slot for "declared, tenant-enablable, platform asserts
	// nothing", so "dormant, waiting for the tenant" was not a state such a key
	// could be in: it was dead, and the tenant could not help themselves.
	//
	// This field IS that slot, and it is now wired end to end —
	// resolveDeclaredRows emits a row
	// for it, State 1 only: a row exists iff the tenant supplied a value, with
	// no platform fallback because there is no platform value to fall back to.
	//
	// ⚠️ A list, not a map: membership is the entire payload. Putting these in
	// Defaults with a null value would not work — a null there is no write
	// (ParseConfigFile, #2518; before it, a threshold of zero for every
	// tenant), so the key would not be declared at all.
	//
	// ⛔ This field is PLATFORM-scoped. applyBoundaryRules strips it from
	// tenant-owned files for the same reason it strips Defaults: a tenant that
	// could name its own keys here would be self-authorising past the write
	// gate via a direct GitOps push.
	OptionalOverrides   []string                             `yaml:"optional_overrides"`
	StateFilters        map[string]StateFilter               `yaml:"state_filters"`
	Tenants             map[string]map[string]ScheduledValue `yaml:"tenants"`
	Profiles            map[string]map[string]ScheduledValue `yaml:"profiles"`
	MaxMetricsPerTenant int                                  `yaml:"max_metrics_per_tenant"`

	// spelled is set by ParseConfigFile when the file writes a dimensional
	// key in another spelling than the canonical one, or a threshold under
	// two spellings in one map (normalizeKeys, #2031): the readers that give
	// keys back as written, or count the duplicates, resolve the effective
	// config of the tenants it bears on (spellingCandidates). Not merged:
	// only a file's own decode carries it.
	spelled bool

	// notes is set by ParseConfigFile when the file has something to say
	// about entries it decoded (fileNotes); nil otherwise, and for a merged
	// config. Behind a pointer so the common file pays one word, not a slice
	// header per note (#3269: ThresholdConfig is held per file and copied on
	// every merge).
	notes *fileNotes
}

// fileNotes is what one file's decode records, without changing any value
// the readers read, about its `tenants:` / `profiles:` entries.
type fileNotes struct {
	// stringMetadata: the entries that write `_metadata` as a string rather
	// than a mapping (stringMetadataEntries, #2859).
	stringMetadata []string
	// scheduledReserved: the entries whose reserved key the file wrote with
	// a schedule, which the decode kept only the `default` of
	// (stripScheduledReserved, #3269).
	scheduledReserved []string
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

	// legacyTwinOf carries the canonical base key when this row is the
	// transition-window legacy dual-emit twin of that key (#1231), and is
	// empty on every other row. Unexported by design: it is resolve-internal
	// ordering metadata — truncationSortKey sorts a twin immediately AFTER
	// its canonical row so the cardinality-cap cut can never keep the twin
	// while dropping the canonical — and it must never leak into the
	// tenant-api JSON (encoding/json skips unexported fields) or the
	// Prometheus label set (the collector reads exported fields only).
	legacyTwinOf string
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

// validReservedKeys, validReservedPrefixes, tenantRoutingKeys,
// platformRoutingKeys, topLevelReadElsewhere and retiredReservedKeys live in
// reserved_keys_gen.go, generated by scripts/tools/dx/sync_schema.py from
// reserved_keys.json — the one list of top-level `_` keys (ADR-038); the
// Python constants and the two schemas' `_` keys come from the same file.
// tenantRoutingKeys / platformRoutingKeys are the `_routing`-prefixed keys
// the route generator reads (#2983): a tenant's own, and those it takes from
// the top level of a conf.d `_` file. The `_routing` PREFIX keeps every
// other spelling reserved (not a metric), so `_routing_profle` passed every
// key check while nothing read it; the generator names such a key
// (_grar_validate, pinned by tests/shared/routing_unknown_keys_cases.json).

// keySet is the set of keys, for the generated tables.
func keySet(keys ...string) map[string]bool {
	out := make(map[string]bool, len(keys))
	for _, k := range keys {
		out[k] = true
	}
	return out
}

// retiredKey is a key nothing reads any more and the hint printed for it.
type retiredKey struct {
	key, hint string
}

// retiredHint is the hint for a retired key k, or "" when k is not one.
func retiredHint(k string) string {
	for _, r := range retiredReservedKeys {
		if r.key == k {
			return r.hint
		}
	}
	return ""
}

// RetiredHint is retiredHint, for da-guard.
func RetiredHint(k string) string { return retiredHint(k) }

// prefixReaders is one registered prefix and its readers.
type prefixReaders struct {
	prefix  string
	readers map[string][]string
}

// RegisteredReaders is what the reserved-key registry declares for the `_`
// key k: each reader → the layers it reads k in (ADR-038 decision 2), taken
// from the key's own entry, else from the first registered prefix k carries.
// registered is false for a key the registry does not list and for a
// retired key (RetiredHint). A copy: the caller may keep or change it.
func RegisteredReaders(k string) (readers map[string][]string, registered bool) {
	if retiredHint(k) != "" {
		return nil, false
	}
	src, ok := reservedKeyReaders[k]
	if !ok {
		for _, p := range reservedPrefixReaders {
			if strings.HasPrefix(k, p.prefix) {
				src, ok = p.readers, true
				break
			}
		}
	}
	if !ok {
		return nil, false
	}
	out := make(map[string][]string, len(src))
	for r, layers := range src {
		out[r] = append([]string(nil), layers...)
	}
	return out, true
}

// IsReservedKey reports whether k is a reserved tenant config key: one of
// validReservedKeys, or starting with one of validReservedPrefixes.
func IsReservedKey(k string) bool {
	if validReservedKeys[k] {
		return true
	}
	for _, prefix := range validReservedPrefixes {
		if strings.HasPrefix(k, prefix) {
			return true
		}
	}
	return false
}

// IsUnregisteredUnderscoreKey reports whether k starts with `_` and the
// reserved-key registry (reserved_keys.json) does not list it: not a key, a
// prefix or a retired key of it — `_myth`, `_myth_critical`,
// `_myth{x="a"}`, `_silent_bogus`. The `_` namespace is the registry's, as
// the JSON schema says: nothing reads such a key in any layer — no row
// phase reads ANY `_` key (baseRowsSkipKey; before #3045 a root
// `defaults:` entry made one a threshold) — so it is the not_served reason
// `unregistered`, which the shared key verdict refuses on write, even when a
// `defaults:` declares it (#3045, ADR-038 §11).
//
// The retired-key test cannot be told apart from `true` with today's
// registry: its one retired key, `_routing_receiver_type`, is under the
// `_routing` prefix IsReservedKey already accepts. It stays for a retired
// key outside every prefix.
func IsUnregisteredUnderscoreKey(k string) bool {
	if !strings.HasPrefix(k, "_") || IsReservedKey(k) {
		return false
	}
	// The registry's top-level keys that are no tenant key (`_policies`,
	// `_routing_defaults`, …) are listed too.
	if _, listed := reservedKeyReaders[k]; listed {
		return false
	}
	return retiredHint(k) == ""
}

// IsRoutingKey reports whether k is a routing key: `_routing` or
// `_routing_<…>` — the keys the route generator reads by name (#2291; its
// own predicate is _grar_parse._is_routing_key). Narrower than the
// `_routing` reserved PREFIX: `_routingProfile` / `_routings` are reserved
// keys (IsReservedKey) but not routing keys. The #2388 report
// (subtreeReservedKeys) calls it to leave routing keys to the generator.
func IsRoutingKey(k string) bool {
	return k == "_routing" || strings.HasPrefix(k, "_routing_")
}

// IsRoutingLayerKey reports whether k is a routing-layer key:
// `_severity_dedup`, or any key starting with `_routing` (Python:
// _grar_parse._is_routing_layer_key). The resolvers skip such a key when they
// build threshold rows (baseRowsSkipKey, resolveDimensionalRows,
// reservedShapeWins); profileFill does not fill one in from a threshold
// profile and ProfileRoutingKeys names each one a profile writes (#2914; why:
// routing_in_unread_location in docs/cli-reference.md#guard). Broader than
// IsRoutingKey, which is the routing keys alone (`_routing`, `_routing_<…>`).
// Every key the registry says the route generator reads must be one
// (TestIsRoutingLayerKey_CoversEveryKeyTheGeneratorReads): da-guard's exit 4
// names a tree "only the generator judges" by it.
func IsRoutingLayerKey(k string) bool {
	return k == "_severity_dedup" || strings.HasPrefix(k, "_routing")
}

// IsRecognisedReservedKey reports whether k is a reserved key something
// reads by name: one of validReservedKeys, a routing key (IsRoutingKey), or a
// `_state_<filter>` key (read for each declared filter). Narrower than
// IsReservedKey, which also accepts anything under a reserved PREFIX —
// `_routingProfile` and `_routings` pass IsReservedKey, yet no reader looks
// them up (#2388 r3).
//
// A bare `_state_` is the filter named "" — declarable like any other name
// (measured: a root `state_filters: {"": …}` is read) — so it is recognised
// like any `_state_<filter>` (#2388 r5).
func IsRecognisedReservedKey(k string) bool {
	return validReservedKeys[k] || IsRoutingKey(k) || strings.HasPrefix(k, "_state_")
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
