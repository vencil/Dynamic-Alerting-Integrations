package main

// Type aliases re-export `pkg/config` types into `package main` so the
// rest of the binary (config.go, collector.go, handlers, simulate, all
// test files) can keep referring to `ThresholdConfig`, `ScheduledValue`,
// `RoutingConfig`, etc. without importing the package qualifier.
//
// v2.8.0 PR-4 collapsed the parallel `app/config_*.go` ↔ `pkg/config/*.go`
// trees that diverged during the half-completed v2.x extract. `pkg/config`
// is the canonical home — it has the v2.5.0 metadata fields
// (Environment / Region / Domain / DBType / Tags / Groups on
// TenantMetadata) and is already imported by `cmd/da-guard`.
//
// Behavior pin: type aliases (`type X = config.X`) propagate methods,
// struct layout, and JSON/YAML tags identically. Existing tests calling
// `cfg.Resolve()`, `cfg.ResolveAt(...)`, `cfg.ApplyProfiles()` still
// dispatch to `pkg/config/resolve.go` unchanged.

import "github.com/vencil/threshold-exporter/pkg/config"

type (
	StateFilter               = config.StateFilter
	ResolvedStateFilter       = config.ResolvedStateFilter
	ResolvedSilentMode        = config.ResolvedSilentMode
	ResolvedMaintenanceExpiry = config.ResolvedMaintenanceExpiry
	RecurringSchedule         = config.RecurringSchedule
	TenantMetadata            = config.TenantMetadata
	ResolvedMetadata          = config.ResolvedMetadata
	TimeWindowOverride        = config.TimeWindowOverride
	ScheduledValue            = config.ScheduledValue
	ThresholdConfig           = config.ThresholdConfig
	ResolvedThreshold         = config.ResolvedThreshold
	ResolvedSeverityDedup     = config.ResolvedSeverityDedup
	RoutingConfig             = config.RoutingConfig
	ConfigInfo                = config.ConfigInfo
)

// fileStat is app-only — pkg/config has no concept of file mtimes
// because it's the standalone resolver layer. Kept here next to the
// other manager-state types it pairs with.
type fileStat struct {
	ModTime int64 // UnixNano
	Size    int64
}
