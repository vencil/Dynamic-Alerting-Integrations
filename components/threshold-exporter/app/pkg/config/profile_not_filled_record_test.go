package config

// #3353: ThresholdConfig.ProfileNotFilled — ApplyProfiles' own decision
// (#1189), per tenant, for the exporter's keys-unread audit. Tenant ids are
// fixture names (CLAUDE.md #9).

import (
	"reflect"
	"testing"
)

func TestProfileNotFilled_PerTenantAndRecomputedByEveryExpansion(t *testing.T) {
	t.Parallel()
	c := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: []string{"redis_evicted_keys"},
		Profiles: map[string]map[string]ScheduledValue{
			"gold": {"redis_evicted_keys": {Default: "11"}, "mysql_connections": {Default: "90"}},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"tx": {"_profile": {Default: "gold"}},
			"ty": {"_profile": {Default: "gold"}, "redis_evicted_keys": {Default: "5"}}, // sets it itself
			"tz": {},
		},
	}
	c.applyProfiles(nil)
	want := map[string][]string{"tx": {"redis_evicted_keys"}}
	if got := c.ProfileNotFilled(); !reflect.DeepEqual(got, want) {
		t.Fatalf("ProfileNotFilled = %v, want %v", got, want)
	}
	// A second expansion of the same config (the incremental reload runs it
	// again) states the same set, not twice it.
	c.applyProfiles(nil)
	if got := c.ProfileNotFilled(); !reflect.DeepEqual(got, want) {
		t.Errorf("after a second expansion ProfileNotFilled = %v, want %v", got, want)
	}
	// The tenant leaves the profile: gone from the set.
	delete(c.Tenants["tx"], "_profile")
	c.applyProfiles(nil)
	if got := c.ProfileNotFilled(); len(got) != 0 {
		t.Errorf("after tx left the profile ProfileNotFilled = %v, want none", got)
	}
}
