package config

// The collector's reading of silent mode and maintenance, in one place (#1988).
//
// ⛔ ONE READING, TWO CONSUMERS. The exporter's collector turns these into
// user_silent_mode / da_config_event{event="silence_expired"} /
// user_state_filter, and tenant-api answers "is this tenant silenced / in
// maintenance" from the same values. Before this file the Expired filter lived
// only in collector.go, and tenant-api's only other option was
// IsMaintenanceActive — which disagreed with the collector on `""` / `~` (it
// said inactive; ResolveStateFiltersAt treats any non-disable value as enabled)
// and ignored the platform's `state_filters.maintenance` entirely (no filter →
// the collector emits nothing; `default_state` unset → it emits for every
// tenant without an override). A second reader re-deriving any of this is the
// drift #1988 was opened for.

import (
	"sort"
	"time"
)

// maintenanceFilterName is the state filter whose rows mean "in maintenance".
const maintenanceFilterName = "maintenance"

// OperationalStates is what the collector emits for silent mode and state
// filters at one instant.
type OperationalStates struct {
	// Silences are the silent-mode entries in effect: one per (tenant,
	// severity), each emitted as user_silent_mode = 1.
	Silences []ResolvedSilentMode
	// ExpiredSilences are entries whose `expires` has passed: they emit
	// da_config_event{event="silence_expired"} instead, so Alertmanager's
	// inhibit stops and notifications resume.
	ExpiredSilences []ResolvedSilentMode
	// StateFilters are the enabled state-filter rows, each emitted as
	// user_state_filter = 1. The `maintenance` rows are what "in maintenance"
	// means.
	StateFilters []ResolvedStateFilter
}

// OperationalStatesAt resolves silent modes and state filters at `now` and
// splits the silent entries by expiry — the split the collector used to make
// inline in collectSilentModes.
func (c *ThresholdConfig) OperationalStatesAt(now time.Time) OperationalStates {
	var out OperationalStates
	// The collector calls this once per scrape, so the split must not cost
	// what the inline version did not: it partitions the resolved slice in
	// place (it is ours) and returns two views of it, allocating nothing.
	// Swapping reorders entries, which carries no meaning — the resolver
	// walks a map, so its order was never stable to begin with.
	all := c.ResolveSilentModesAt(now)
	k := 0
	for i := range all {
		if !all[i].Expired {
			all[k], all[i] = all[i], all[k]
			k++
		}
	}
	out.Silences, out.ExpiredSilences = all[:k:k], all[k:]
	out.StateFilters = c.ResolveStateFiltersAt(now)
	return out
}

// TenantOperationalState is one tenant's view of OperationalStates.
type TenantOperationalState struct {
	// SilentTargets are the severities whose notifications are silenced
	// ("critical" / "warning"), sorted; empty (never nil) when none is.
	SilentTargets []string
	// MaintenanceActive reports whether the tenant has a `maintenance`
	// state-filter row, i.e. whether user_state_filter{filter="maintenance"}
	// is emitted for it.
	MaintenanceActive bool
}

// ByTenant projects the states onto every tenant of c. A tenant with no
// silence and no maintenance row is present with the zero reading, so a
// caller can tell "not silenced" from "not a tenant".
func (s OperationalStates) ByTenant(c *ThresholdConfig) map[string]TenantOperationalState {
	out := make(map[string]TenantOperationalState, len(c.Tenants))
	for tenant := range c.Tenants {
		out[tenant] = TenantOperationalState{SilentTargets: []string{}}
	}
	for _, sm := range s.Silences {
		st := out[sm.Tenant]
		st.SilentTargets = append(st.SilentTargets, sm.TargetSeverity)
		out[sm.Tenant] = st
	}
	for _, sf := range s.StateFilters {
		if sf.FilterName != maintenanceFilterName {
			continue
		}
		st := out[sf.Tenant]
		st.MaintenanceActive = true
		out[sf.Tenant] = st
	}
	for tenant, st := range out {
		if st.SilentTargets == nil {
			st.SilentTargets = []string{}
		}
		sort.Strings(st.SilentTargets)
		out[tenant] = st
	}
	return out
}
