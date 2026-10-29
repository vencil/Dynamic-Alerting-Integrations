package config

// row_count.go — how many user_threshold rows the resolver behind /metrics
// produces per tenant, at its busiest minute of the UTC day (#3380).
//
// da-guard's cardinality check compares this count with the per-tenant cap.
// It is the resolver's own count (ResolveStats.PerTenantRowCount), not a
// count of config keys: a key the build does not deliver produces no row, a
// `_critical` key, a dimensional key, a #1231 legacy twin or a custom-alert
// recipe produces the rows the resolver gives it, and a "disable" none.

import (
	"sort"
	"time"
)

// PeakTenantRowCounts is, per tenant of c, the most user_threshold rows the
// resolver produces for it at any minute of the UTC day, counted BEFORE the
// max_metrics_per_tenant cut (ResolveStats.PerTenantRowCount): the cut
// truncates at the busiest minute, so that minute is the one to compare with
// the cap. `expires:` is read at now — the caller's instant (#3368: pkg/config
// reads no wall clock). Silent; c is not modified. Every tenant of c has an
// entry.
//
// The day is read as ValuesNotServed reads it: c resolved once at now, then
// each tenant with a schedule resolved alone (a one-tenant copy) at the start
// of every segment of its own day (addScheduleCuts) but the one now falls in.
// A tenant's rows depend only on its own map and the config-wide settings, so
// the one-tenant copy produces the rows c produces for that tenant.
func (c *ThresholdConfig) PeakTenantRowCounts(now time.Time) map[string]int {
	_, stats := c.resolveAtWithStats(now, nil, nil, nil)
	peak := stats.PerTenantRowCount
	utc := now.UTC()
	nowMinute := utc.Hour()*60 + utc.Minute()
	var one ThresholdConfig
	for tenant, overrides := range c.Tenants {
		seen := addScheduleCuts(nil, overrides)
		if seen == nil {
			continue
		}
		cuts := sortedCuts(seen)
		current := sort.SearchInts(cuts, nowMinute+1) - 1
		one = *c
		one.Tenants = map[string]map[string]ScheduledValue{tenant: overrides}
		for i, m := range cuts {
			if i == current {
				continue
			}
			_, s := one.AtMinuteOfDay(m).resolveAtWithStats(now, nil, nil, nil)
			if n := s.PerTenantRowCount[tenant]; n > peak[tenant] {
				peak[tenant] = n
			}
		}
	}
	return peak
}
