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
	// problem judges a value: "" when the exporter reads it, otherwise the
	// reason it ignores it, in one tenant-free sentence.
	problem func(ScheduledValue) string
	// reading is the value as the exporter reads it, as a comparable value:
	// two values whose readings are == are read the same way — quoting, key
	// order, comments and every spelling the reader treats alike fall away.
	// KeyValidation.Ratchet compares these, never the ScheduledValue itself.
	reading func(ScheduledValue) any
	// effect is what happens while the reader ignores the value, for the
	// write gate's message ("nothing is silenced while it is set").
	effect string
}

// valueChecks binds each `value.check` name the registry may carry to the
// exporter-side reading that judges it. TestValueChecks_BoundBothWays keeps
// it equal to the names the registry uses, in both directions.
var valueChecks = map[string]valueCheck{
	"silent_mode": {
		problem: silentModeProblem,
		reading: func(sv ScheduledValue) any {
			r := silentModeOf(sv)
			return r.canonical()
		},
		effect: "nothing is silenced while it is set",
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
// registry's `batch.unset` keys and every retired key.
func BatchUnsettable(k string) bool {
	for _, u := range batchUnsettableKeys {
		if u == k {
			return true
		}
	}
	return false
}

// BatchUnsettableKeys are the keys BatchUnsettable admits, sorted.
func BatchUnsettableKeys() []string { return sortedCopy(batchUnsettableKeys) }

// ScopeMetadataKeys are the keys that decide a tenant's environment/domain
// through the entry they name (`_profile`: its profile's `_metadata`), sorted.
func ScopeMetadataKeys() []string { return sortedCopy(scopeMetadataKeys) }

// CheckReservedValue judges sv as the value of reserved key k with the
// function its registry `value.check` names: "" when k has no check, or the
// value is one the exporter reads; otherwise the problem.
func CheckReservedValue(k string, sv ScheduledValue) string {
	return checkReservedValue(k, sv, reservedValueChecks, valueChecks)
}

func checkReservedValue(k string, sv ScheduledValue, names map[string]string, checks map[string]valueCheck) string {
	c, ok := checks[names[k]]
	if !ok || c.problem == nil {
		return ""
	}
	return c.problem(sv)
}

// readAlike reports whether the exporter reads a and b, two values of
// reserved key k, the same way — by the reading k's registry `value.check`
// is bound to. A key with no check is never "alike": with nothing to say
// how the reader reads it, the caller must treat the value as changed.
func readAlike(k string, a, b ScheduledValue) bool {
	c, ok := valueChecks[reservedValueChecks[k]]
	if !ok || c.reading == nil {
		return false
	}
	return c.reading(a) == c.reading(b)
}

// valueEffect is what happens while the reader ignores a value of k ("" when
// k's check does not say).
func valueEffect(k string) string { return valueChecks[reservedValueChecks[k]].effect }

func sortedCopy(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}
