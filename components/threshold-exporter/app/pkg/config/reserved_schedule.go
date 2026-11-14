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
//     subtree defaults overlay (applySubtreeDefaults, which judges the
//     stripped value — subtreeLevelRead, #3295), tenant-api's merge
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
		return ScheduledValue{Default: sv.Default, structured: sv.structured}
	}
	return sv
}

// ScheduledReservedEntry is one `tenants:` / `profiles:` entry of a file
// whose reserved key the file wrote with a schedule (scheduledReserved): the
// decode kept only its `default` (#3269).
type ScheduledReservedEntry struct {
	// File: the scan key (root-relative slash path) of the file; "" in a
	// file's own record (ThresholdConfig.ScheduledReservedEntries), which
	// knows no path.
	File string
	// Section: "tenants" or "profiles".
	Section string
	// Owner: the tenant id, or the profile name.
	Owner string
	// Key: the reserved key, as the file spells it.
	Key string
}

// Entry is e as "<section>.<owner>.<key>" (the exporter's WARN names it so).
func (e ScheduledReservedEntry) Entry() string {
	return e.Section + "." + e.Owner + "." + e.Key
}

// stripScheduledReserved replaces, in place, every scheduledReserved value
// of bodies with its servedValue and returns where it did, sorted by Entry
// (nil when nowhere).
func stripScheduledReserved(section string, bodies map[string]map[string]ScheduledValue) []ScheduledReservedEntry {
	var out []ScheduledReservedEntry
	for name, body := range bodies {
		for k, sv := range body {
			if scheduledReserved(k, sv) {
				body[k] = ScheduledValue{Default: sv.Default, structured: sv.structured}
				out = append(out, ScheduledReservedEntry{Section: section, Owner: name, Key: k})
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Entry() < out[j].Entry() })
	return out
}

// ScheduledReservedEntries is the file's `tenants:` / `profiles:` entries
// whose reserved key was written with a schedule, as
// "tenants.<id>.<key>" / "profiles.<name>.<key>": the decode kept only
// each one's `default` (#3269). Set by ParseConfigFile only; nil for a
// merged config.
func (c *ThresholdConfig) ScheduledReservedEntries() []string {
	if c.notes == nil || c.notes.scheduledReserved == nil {
		return nil
	}
	out := make([]string, 0, len(c.notes.scheduledReserved))
	for _, e := range c.notes.scheduledReserved {
		out = append(out, e.Entry())
	}
	return out
}

// scheduledReservedOfFiles is, over the files the flat merge read
// (FlatBuild.FileConfigs), every ScheduledReservedEntry of each, File set,
// sorted by file then Entry (LoadReport.ScheduledReserved, #3621). nil when
// none.
func scheduledReservedOfFiles(files map[string]ThresholdConfig) []ScheduledReservedEntry {
	var out []ScheduledReservedEntry
	for file, fc := range files {
		if fc.notes == nil {
			continue
		}
		for _, e := range fc.notes.scheduledReserved {
			e.File = file
			out = append(out, e)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].File != out[j].File {
			return out[i].File < out[j].File
		}
		return out[i].Entry() < out[j].Entry()
	})
	return out
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
	// #3638: the same text written as a scalar and as a mapping is not the
	// same value — a block scalar `|` holding "target: critical\n" and the
	// mapping {target: critical} have one Default.
	if a.Default != b.Default || a.structured != b.structured || len(a.Overrides) != len(b.Overrides) {
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
//   - notApplied: not applied at all — exactly as its `default` written
//     alone would not be (#3295): every reserved key but a routing key,
//     which a subtree does not carry (subtreeReservedUnsupported, #3341),
//     and a routing key's default the overlay refuses or no resolver reads.
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
