package config

// unset_thresholds.go — which operator-supplied threshold keys serve a tenant
// no threshold (#1200 D-A A3, #1175).
//
// The platform ships a value only for the keys the threshold registry marks
// `arming: default`. Every other key a rule pack reads is supplied by the
// operator; until a threshold is served for it, the alert that compares
// against it has no right-hand side and never fires — silently. This file
// answers, per subscribed tenant, which of those keys serve nothing, so the
// exporter can serve `user_threshold_unset` and each rule pack can raise an
// info alert for a tenant whose exporter of that kind is answering.
//
// Optional, both ways (owner decision 2026-11-15): the state filter
// `unset_thresholds` (root `state_filters:`) is the switch. The shipped
// default_state is `disable`; a platform sets `enable` to notify every
// tenant, and a tenant overrides either way with `_state_unset_thresholds:
// enable|disable`. Only subscribed tenants are judged and served.
//
// The keys and the pack each belongs to are GENERATED from the registry into
// unset_threshold_keys_gen.go by `check_threshold_registry.py --regen`;
// `--ci` fails while it is stale.
//
// A key is configured for a tenant — no notice — when:
//   - the resolver serves a row for it (its base or a dimensional spelling,
//     any severity — an inline `5:critical` arms the critical alert) at some
//     part of the tenant's UTC day, judged before the max_metrics_per_tenant
//     cut — a `<key>_critical` row included, which arms the critical alert
//     (#4163: the built-in layer values every key of the table, so a
//     tenant's `_critical` is served even with no base value). A value the
//     resolver refuses ("abc", `disable:critical`) or a value for a key the
//     root does not declare serves nothing and is NOT configured; or
//   - the tenant writes `disable` for it (in any window): the tenant does not
//     want that alert, and the notice for it ends; or
//   - the root `defaults:` writes it `disable` (RootDisabledDefault, legal
//     for every key of the built-in layer since #4163): the platform switched
//     it off for every tenant that gives it no value.
//
// Cost: only the generated keys are resolved — the defaults and the tenant
// keys whose base is one of them — so a subscribed tenant costs its own
// operator keys, at now and at each window start of those keys, not a
// second resolve of everything it writes (BenchmarkUnsetThresholdsAt).

import (
	"sort"
	"strings"
	"time"
)

// UnsetThresholdsFilter is the state filter that switches the notice on.
const UnsetThresholdsFilter = "unset_thresholds"

// unsetThresholdKey is one line of the generated table: a key the platform
// does not ship a value for, and the pack whose rules read it.
type unsetThresholdKey struct {
	key  string
	pack string
}

// UnsetThreshold is one (tenant, key) that serves no threshold.
type UnsetThreshold struct {
	Tenant string
	Pack   string
	Key    string
}

// UnsetThresholdKeys is a copy of the generated table as key → pack.
func UnsetThresholdKeys() map[string]string {
	out := make(map[string]string, len(unsetThresholdKeys))
	for _, e := range unsetThresholdKeys {
		out[e.key] = e.pack
	}
	return out
}

// UnsetThresholdsSubscribed is the tenants whose state filter
// `unset_thresholds` is on, from a state-filter reading at the same instant.
func UnsetThresholdsSubscribed(filters []ResolvedStateFilter) map[string]bool {
	out := map[string]bool{}
	for _, f := range filters {
		if f.FilterName == UnsetThresholdsFilter {
			out[f.Tenant] = true
		}
	}
	return out
}

// UnsetThresholdsAt lists, for every subscribed tenant, the generated keys
// that serve it no threshold at now's UTC day and that it does not switch
// off, sorted by tenant then key.
func (c *ThresholdConfig) UnsetThresholdsAt(now time.Time, subscribed map[string]bool) []UnsetThreshold {
	if c == nil || len(subscribed) == 0 || len(unsetThresholdKeys) == 0 {
		return nil
	}
	quiet := func(string, ...any) {}
	inTable := make(map[string]bool, len(unsetThresholdKeys))
	for _, e := range unsetThresholdKeys {
		inTable[e.key] = true
	}
	canonOptional := canonicalizeOptionalOverrides(c.OptionalOverrides)
	// Only the generated keys: the phases read nothing else for them.
	defaults := map[string]float64{}
	for k, v := range canonicalizeDefaults(c.Defaults) {
		if inTable[keyBase(k)] {
			defaults[k] = v
		}
	}

	tenants := make([]string, 0, len(subscribed))
	for t := range subscribed {
		if _, ok := c.Tenants[t]; ok {
			tenants = append(tenants, t)
		}
	}
	sort.Strings(tenants)

	out := make([]UnsetThreshold, 0, len(tenants)*len(unsetThresholdKeys))
	for _, t := range tenants {
		all, _ := canonicalizeOverrides(c.Tenants[t])
		overrides := map[string]ScheduledValue{}
		for k, sv := range all {
			if inTable[unsetFamilyBase(k)] {
				overrides[k] = sv
			}
		}
		configured := map[string]bool{}
		sink := func(_ string, metricKey string, _ bool, _ ResolvedThreshold) {
			configured[keyBase(metricKey)] = true
		}
		for _, at := range dayInstants(now, overrides) {
			c.resolveBaseRows(t, defaults, overrides, at, sink, quiet, nil)
			c.resolveDimensionalRows(t, defaults, overrides, at, sink, quiet, nil)
			c.resolveDeclaredRows(t, defaults, canonOptional, overrides, at, sink, quiet, nil)
			// #4163: a `<key>_critical` row arms the critical alert — the
			// sink is handed its base key, so it counts as configured.
			c.resolveCriticalRows(t, defaults, overrides, at, sink, quiet, nil)
		}
		for _, e := range unsetThresholdKeys {
			if configured[e.key] || switchedOffKey(overrides, e.key) || RootDisabledDefault(defaults[e.key]) {
				continue
			}
			out = append(out, UnsetThreshold{Tenant: t, Pack: e.pack, Key: e.key})
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// dayInstants is now and, on now's UTC day, the start of every segment of a
// scheduled value in overrides: the instants at which what the tenant serves
// can differ, so a key served only in a window counts as configured all day
// and the notice does not come and go with the window.
func dayInstants(now time.Time, overrides map[string]ScheduledValue) []time.Time {
	day := now.UTC().Truncate(24 * time.Hour)
	seen := map[int]bool{}
	out := []time.Time{now}
	for _, sv := range overrides {
		if len(sv.Overrides) == 0 {
			continue
		}
		for _, seg := range sv.DaySegments() {
			if !seen[seg.From] {
				seen[seg.From] = true
				out = append(out, day.Add(time.Duration(seg.From)*time.Minute))
			}
		}
	}
	return out
}

// unsetFamilyBase is the base key of key's family: its head before `{`,
// without a `_critical` suffix — so a tenant's `<key>_critical` reaches the
// critical phase beside its base (#4163).
func unsetFamilyBase(key string) string {
	head := keyBase(key)
	if base, critical := criticalRowBase(head); critical {
		return base
	}
	return head
}

// keyBase is the key a config key serves: its head before `{`.
func keyBase(key string) string {
	head, _ := splitKeyHead(key)
	return head
}

// switchedOffKey reports whether the tenant's own value for key (any window)
// is a disable.
func switchedOffKey(overrides map[string]ScheduledValue, key string) bool {
	sv, ok := overrides[key]
	if !ok {
		return false
	}
	for _, seg := range sv.DaySegments() {
		if IsDisabled(strings.ToLower(strings.TrimSpace(seg.Value))) {
			return true
		}
	}
	return false
}
