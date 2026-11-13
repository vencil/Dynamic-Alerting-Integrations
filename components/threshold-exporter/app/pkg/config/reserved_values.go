package config

// What the registry (reserved_keys.json, ADR-038 decision 2) says about a
// reserved key's VALUE: its shape, the exporter-side function that judges it,
// and whether a tenant-api batch op may remove the key. The tables are
// generated (reserved_keys_gen.go); this file is the lookup over them and the
// binding of each `value.check` name to the function that judges it.

import (
	"fmt"
	"sort"
)

// The value shapes a registry `value.shape` names.
const (
	ShapeScalar          = "scalar"
	ShapeMapping         = "mapping"
	ShapeList            = "list"
	ShapeScalarOrMapping = "scalar_or_mapping"
)

// valueCheck is what a registry `value.check` name is bound to: the
// exporter's own reading of the value, in the two forms the write gate needs.
type valueCheck struct {
	// problem judges a value of key k: "" when the exporter reads it,
	// otherwise the reason it ignores it, in one tenant-free sentence. k is
	// the key itself, so a check bound to a prefix (`_state_`) knows which
	// key under it it judges.
	problem func(k string, sv ScheduledValue) string
	// reading is the value as the exporter reads it, as a comparable value:
	// two values whose readings are == are read the same way — quoting, key
	// order, comments and every spelling the reader treats alike fall away.
	// KeyValidation.Ratchet compares these, never the ScheduledValue itself.
	// It must be a value == can compare (a string, or a struct of scalars):
	// == on an interface holding a slice panics.
	reading func(k string, sv ScheduledValue) any
	// effect is what happens while the reader ignores the value, for the
	// write gate's message ("nothing is silenced while it is set").
	effect string
}

// valueChecks binds each `value.check` name the registry may carry (on a
// key or on a prefix) to the exporter-side reading that judges it.
// TestValueChecks_BoundBothWays keeps it equal to the names the registry
// uses, in both directions.
var valueChecks = map[string]valueCheck{
	"silent_mode": {
		problem: func(_ string, sv ScheduledValue) string { return silentModeProblem(sv) },
		reading: func(_ string, sv ScheduledValue) any {
			r := silentModeOf(sv)
			return r.canonical()
		},
		effect: "nothing is silenced while it is set",
	},
	// #3620: every `_state_<filter>` (prefixes._state_), by the one reading
	// of such a value (stateValueOf).
	"state_filter": {
		problem: stateFilterValueProblem,
		reading: stateFilterValueReading,
		effect:  "the state filter is not switched on while it is set",
	},
	// #3591: `_metadata`, by the decode tenant_metadata_info is served from
	// (decodeTenantMetadata); a field TenantMetadata does not know is a part
	// the exporter ignores.
	"metadata": {
		problem: metadataProblem,
		reading: metadataReading,
		effect:  "what the exporter cannot read of it is not served",
	},
	// #3591: `_severity_dedup`, by the resolver's own words
	// (severityDedupModeOf).
	"severity_dedup": {
		problem: func(_ string, sv ScheduledValue) string { return severityDedupProblem(sv) },
		reading: func(_ string, sv ScheduledValue) any { return severityDedupReading(sv) },
		effect:  "severity dedup stays on (the default) while it is set",
	},
}

// metadataPatchReason is the refusal tenant-api has given for a batch patch
// of `_metadata` since #2859; kept word for word.
const metadataPatchReason = "_metadata is a mapping and a batch patch value is a string, so a batch cannot set it; " +
	"write it as a mapping with PUT /api/v1/tenants/{id} (#2859)"

// ValueShape is k's registry value shape (Shape*), or "" when k is not a
// registered key or declares none.
func ValueShape(k string) string { return reservedValueShapes[k] }

// PatchableAsString reports whether a string (a batch patch value) can set
// k. A mapping or a list cannot: reason says so and where to write it. Any
// other key — a scalar reserved key, a threshold key — can.
func PatchableAsString(k string) (ok bool, reason string) {
	shape := reservedValueShapes[k]
	if shape != ShapeMapping && shape != ShapeList {
		return true, ""
	}
	if k == metadataKey {
		return false, metadataPatchReason
	}
	return false, fmt.Sprintf("%s is a %s and a batch patch value is a string, so a batch cannot set it; "+
		"write it as a %s with PUT /api/v1/tenants/{id}", k, shape, shape)
}

// BatchUnsettable reports whether a batch op's `unset` may remove k: the
// registry's `batch.unset` keys and every retired key — the reserved
// registry's and the threshold registry's `retired_keys:` (#3829), with
// their `_critical` and dimensional `<key>{…}` spellings: while one is on
// disk the key verdict refuses it, so the tenant must be able to remove it
// through the API.
func BatchUnsettable(k string) bool {
	return batchUnsettableIn(batchUnsettableKeys, retiredThresholdKeys, k)
}

func batchUnsettableIn(reserved []string, retired []retiredThresholdKey, k string) bool {
	for _, u := range reserved {
		if u == k {
			return true
		}
	}
	_, ok := retiredThresholdIn(retired, k)
	return ok
}

// BatchUnsettableKeys are the keys BatchUnsettable admits, sorted (a retired
// threshold key by its base spelling).
func BatchUnsettableKeys() []string {
	return batchUnsettableKeysOf(batchUnsettableKeys, retiredThresholdKeys)
}

func batchUnsettableKeysOf(reserved []string, retired []retiredThresholdKey) []string {
	return sortedCopy(append(append([]string(nil), reserved...), retiredThresholdKeyNames(retired)...))
}

// ScopeMetadataKeys are the keys that decide a tenant's environment/domain
// through the entry they name (`_profile`: its profile's `_metadata`), sorted.
func ScopeMetadataKeys() []string { return sortedCopy(scopeMetadataKeys) }

// CheckReservedValue judges sv as the value of reserved key k with the
// function its registry `value.check` names — the key's own, or else the
// one of the prefix k is under (#3620: `_state_`): "" when k has no check,
// or the value is one the exporter reads; otherwise the problem.
func CheckReservedValue(k string, sv ScheduledValue) string {
	return checkReservedValue(k, sv, reservedValueChecks, reservedPrefixValueChecks, valueChecks)
}

func checkReservedValue(k string, sv ScheduledValue, names, prefixNames map[string]string, checks map[string]valueCheck) string {
	c, ok := checks[valueCheckName(k, names, prefixNames)]
	if !ok || c.problem == nil {
		return ""
	}
	return c.problem(k, sv)
}

// valueCheckName is the `value.check` that judges k: its own entry's, or
// else that of the registered prefix k is under ("" for none). A key's own
// entry wins: a prefix's check covers only the keys the registry does not
// list one by one.
func valueCheckName(k string, names, prefixNames map[string]string) string {
	if name, ok := names[k]; ok {
		return name
	}
	if _, listed := validReservedKeys[k]; listed {
		return ""
	}
	for p, name := range prefixNames {
		if len(k) > len(p) && k[:len(p)] == p {
			return name
		}
	}
	return ""
}

// reservedCheck is the check bound to k (the key's or its prefix's).
func reservedCheck(k string) (valueCheck, bool) {
	c, ok := valueChecks[valueCheckName(k, reservedValueChecks, reservedPrefixValueChecks)]
	return c, ok
}

// readAlike reports whether the exporter reads a and b, two values of key k,
// the same way: for a reserved key by the reading k's registry
// `value.check` is bound to — a reserved key with no check is never
// "alike": with nothing to say how the reader reads it, the caller must
// treat the value as changed; for a threshold key by its row resolver's
// reading (thresholdReadAlike, #3267). ValueFinding.unchanged's one
// comparison of a value as read.
func readAlike(k string, a, b ScheduledValue) bool {
	if !baseRowsSkipKey(k) {
		// A threshold key: its row resolver's reading (#3267).
		return thresholdReadAlike(k, a, b)
	}
	c, ok := reservedCheck(k)
	if !ok || c.reading == nil {
		return false
	}
	return c.reading(k, a) == c.reading(k, b)
}

// valueEffect is what happens while the reader ignores a value of k ("" when
// k's check does not say).
func valueEffect(k string) string {
	c, _ := reservedCheck(k)
	return c.effect
}

func sortedCopy(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}
