package config

// #3269: a reserved key has no schedule. Its readers — the silent-mode,
// state-filter, severity-dedup and profile readers, the route generator —
// read the value's `default` alone; `overrides:` (a time window) and
// `expires:` / `reason:` are honoured on thresholds only. Yet
// ScheduledValue.UnmarshalYAML cannot see the key its value sits under, so
// `_silent_mode: {default: disable, overrides: [{window: 03:00-04:00, value:
// all}]}` decodes into a schedule like any threshold: the exporter never
// silenced anything at 03:00, while da-guard's ScheduleCuts / AtMinuteOfDay
// cut the day at the window and served a silenced copy, and the write gate
// said nothing.
//
// One judgement, scheduledReserved, and the value every reader reads,
// servedValue — the `default` alone, so what is served does not change:
//
//   - the typed decode (parseConfigFile) keeps only the default of every
//     such value it decodes and records where it did (ScheduledReservedEntries;
//     the exporter WARNs at commit), so no ThresholdConfig built from a file
//     carries the window: not /metrics, not da-guard's served-values;
//   - the planes that do not decode through it take servedValue too: the
//     subtree defaults overlay (applySubtreeDefaults), tenant-api's merge
//     (mergeTenantConfig's display copy, which keeps the tenant layer as
//     written for the write gate), and the walker's reported config
//     (effectiveView, rawServedValue);
//   - the write gate (validateOverrideKeys) refuses the form with a
//     ValueFinding of its own kind, which Ratchet lets through only when the
//     value is exactly as it was, schedule included.

import (
	"path/filepath"
	"sort"
	"strings"
)

// scheduledReserved reports whether sv, the value of key k, is a reserved
// key written with a schedule: a time window (`overrides:`) or an
// `expires:` / `reason:`. k is a reserved key by the registry
// (IsReservedKey: a listed key, or one under a reserved prefix such as
// `_state_`), and not one of the mapping keys whose mapping is kept whole
// (mappingReservedKeys, keepReservedMappings): their `default`, `overrides`
// or `expires` is a field of the mapping, not a schedule.
//
// The structure is the judge, not "the mapping has a `default` key":
// `{default: x}` alone (the `_profile` election form, #2433) is no
// schedule, and a mapping key's `default` field is none either.
func scheduledReserved(k string, sv ScheduledValue) bool {
	if len(sv.Overrides) == 0 && sv.Expiry == nil {
		return false
	}
	return IsReservedKey(k) && !mappingReservedKeys[k]
}

// servedValue is sv as the readers of key k read it: sv itself, or — when
// it is a scheduledReserved value — its `default` alone.
func servedValue(k string, sv ScheduledValue) ScheduledValue {
	if scheduledReserved(k, sv) {
		return ScheduledValue{Default: sv.Default}
	}
	return sv
}

// stripScheduledReserved replaces, in place, every scheduledReserved value
// of bodies with its servedValue and returns where it did, as
// "<section>.<name>.<key>", sorted (nil when nowhere).
func stripScheduledReserved(section string, bodies map[string]map[string]ScheduledValue) []string {
	var out []string
	for name, body := range bodies {
		for k, sv := range body {
			if scheduledReserved(k, sv) {
				body[k] = ScheduledValue{Default: sv.Default}
				out = append(out, section+"."+name+"."+k)
			}
		}
	}
	sort.Strings(out)
	return out
}

// ScheduledReservedEntries is the file's `tenants:` / `profiles:` entries
// whose reserved key was written with a schedule, as
// "tenants.<id>.<key>" / "profiles.<name>.<key>": the decode kept only
// each one's `default` (#3269). Set by ParseConfigFile only; nil for a
// merged config.
func (c *ThresholdConfig) ScheduledReservedEntries() []string {
	if c.notes == nil {
		return nil
	}
	return c.notes.scheduledReserved
}

// rawServedValue is servedValue for a value of key k as the walker holds it
// (a generic decode, effectiveView): the `default` it wrote, and true, when
// the value is a scheduledReserved one; (nil, false) otherwise. The value is
// judged by the same decode a tenant file's value gets
// (scheduledValueFromRaw), and only a mapping can be one.
func rawServedValue(k string, v any) (any, bool) {
	m, ok := v.(map[string]any)
	if !ok || !IsReservedKey(k) || mappingReservedKeys[k] {
		return nil, false
	}
	sv, ok := scheduledValueFromRaw(m)
	if !ok || !scheduledReserved(k, sv) {
		return nil, false
	}
	return m["default"], true
}

// withRawServedValues replaces, in place, every top-level value of m that
// rawServedValue judges with the default it wrote, and returns m.
func withRawServedValues(m map[string]any) map[string]any {
	for k, v := range m {
		if d, ok := rawServedValue(k, v); ok {
			m[k] = d
		}
	}
	return m
}

// scheduleFields names the schedule fields sv writes, for a message.
func scheduleFields(sv ScheduledValue) string {
	var f []string
	if len(sv.Overrides) > 0 {
		f = append(f, "`overrides:`")
	}
	if sv.Expiry != nil {
		if sv.Expiry.Expires != "" {
			f = append(f, "`expires:`")
		}
		if sv.Expiry.Reason != "" {
			f = append(f, "`reason:`")
		}
	}
	return strings.Join(f, " and ")
}

// scheduleHint is where the time-boxing a reserved key k cannot carry
// belongs, for a message ("" when nowhere).
func scheduleHint(k string) string {
	switch {
	case k == "_silent_mode":
		return "; for a one-time end write `_silent_mode: {target: …, expires: …}`, " +
			"for a recurring window use `_state_maintenance` with `recurring:`"
	case k == "_state_maintenance":
		return "; for a one-time end write `{target: enable, expires: …}`, for a recurring window `recurring:`"
	case strings.HasPrefix(k, "_state_"):
		return "; for a recurring maintenance window use `_state_maintenance` with `recurring:`"
	}
	return ""
}

// sameSchedule reports whether a and b are the same value as written:
// default, every window and the expiry, field for field.
func sameSchedule(a, b ScheduledValue) bool {
	if a.Default != b.Default || len(a.Overrides) != len(b.Overrides) {
		return false
	}
	for i := range a.Overrides {
		if a.Overrides[i] != b.Overrides[i] {
			return false
		}
	}
	if (a.Expiry == nil) != (b.Expiry == nil) {
		return false
	}
	return a.Expiry == nil || *a.Expiry == *b.Expiry
}

// SubtreeScheduledReserved is ScheduledReservedEntries for the subtree
// defaults files of a tree (#3269): every reserved key a conf.d subtree
// `_defaults.yaml` writes with a schedule, as "<root-relative path>:
// defaults.<key>", sorted, split by what the overlay does with it — the
// same verdict applySubtreeDefaults acts on (subtreeLevelValue, over cfg,
// the config being committed):
//
//   - defaultOnly: applied to the tenants that do not set the key, as its
//     `default` alone (servedValue);
//   - notApplied: not applied at all, schedule or not — a value the overlay
//     refuses (not threshold-shaped: `_silent_mode: {default: warning, …}`)
//     or no resolver would read from the tenant's map (a windowed `_state_`
//     key, #1569).
//
// parsed is the defaults-chain cache (absolute path → the file's defaults
// level, as applySubtreeDefaults reads it); the conf.d root's own
// `_defaults.yaml` is skipped by path, as there. Which values are schedules
// is rawServedValue's judgement — scheduledReserved.
func SubtreeScheduledReserved(root string, parsed map[string]map[string]any, cfg *ThresholdConfig) (defaultOnly, notApplied []string) {
	rootDir := AbsScanRoot(root)
	for path, level := range parsed {
		if filepath.Dir(filepath.Clean(path)) == rootDir {
			continue
		}
		for k, v := range level {
			if _, ok := rawServedValue(k, v); !ok {
				continue
			}
			e := subtreeRelPath(rootDir, path) + ": defaults." + k
			if _, verdict := subtreeLevelValue(cfg, k, v); verdict == subtreeApplied {
				defaultOnly = append(defaultOnly, e)
			} else {
				notApplied = append(notApplied, e)
			}
		}
	}
	sort.Strings(defaultOnly)
	sort.Strings(notApplied)
	return defaultOnly, notApplied
}
