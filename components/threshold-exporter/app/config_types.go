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
	ResolvedThresholdExpiry   = config.ResolvedThresholdExpiry
	RecurringSchedule         = config.RecurringSchedule
	TenantMetadata            = config.TenantMetadata
	ResolvedMetadata          = config.ResolvedMetadata
	TimeWindowOverride        = config.TimeWindowOverride
	ScheduledValue            = config.ScheduledValue
	ExpiryMeta                = config.ExpiryMeta
	ThresholdConfig           = config.ThresholdConfig
	ResolvedThreshold         = config.ResolvedThreshold
	ResolvedSloObjective      = config.ResolvedSloObjective
	ResolvedSeverityDedup     = config.ResolvedSeverityDedup
	RoutingConfig             = config.RoutingConfig
	ConfigInfo                = config.ConfigInfo

	// InheritanceGraph was promoted to pkg/config in v2.8.0 PR-8 so cmd/da-guard
	// and tenant-api can construct it without importing package main.
	// The disk walker (config.ScanDirTree, #1941) lives there too; this
	// package reaches it through config_tree_scan.go.
	InheritanceGraph = config.InheritanceGraph

	// DuplicateTenantError is the cross-file duplicate-tenant error, lowered
	// into pkg/config (C6-A, #127 library-side gap) so tenant-api /
	// cmd/da-guard / simulate can errors.As it.
	// config.ScanDirTree records it on the scan; the callers here that
	// errors.As it keep compiling unchanged through this alias.
	DuplicateTenantError = config.DuplicateTenantError
)

// NewInheritanceGraph re-exports the pkg/config constructor under its
// historical name so existing app/test callers compile unchanged.
var NewInheritanceGraph = config.NewInheritanceGraph
