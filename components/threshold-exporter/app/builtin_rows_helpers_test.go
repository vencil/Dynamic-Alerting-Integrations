package main

// builtin_rows_helpers_test.go — the built-in layer (#3832) gives every
// tenant a default row for each default-armed key its tree does not write.
// Tests whose fixtures are about other keys count around those rows with
// these helpers instead of a fixed number: the table keeps growing.

import (
	"math"
	"reflect"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// builtinKeysNotIn is the number of built-in keys the fixture does not write
// (written: the keys its files do write, any layer).
func builtinKeysNotIn(written ...string) int {
	w := make(map[string]bool, len(written))
	for _, k := range written {
		w[k] = true
	}
	n := 0
	for k := range config.BuiltinDefaults() {
		if !w[k] {
			n++
		}
	}
	return n
}

// layerKeysNotIn is the number of keys of the whole built-in layer — the
// table of values and the off table (#4163) — the fixture's root does not
// write (written: the keys it writes): what the layer adds to Defaults. An
// off key adds no resolved row, so count rows with builtinKeysNotIn.
func layerKeysNotIn(written ...string) int {
	w := make(map[string]bool, len(written))
	for _, k := range written {
		w[k] = true
	}
	n := builtinKeysNotIn(written...)
	for _, k := range config.BuiltinOffKeys() {
		if !w[k] {
			n++
		}
	}
	return n
}

// dropBuiltinRows is resolved without the rows of the built-in keys the
// fixture does not write (written as for builtinKeysNotIn). A row's key is
// `<component>_<metric>`, as the exporter splits it.
func dropBuiltinRows(resolved []ResolvedThreshold, written ...string) []ResolvedThreshold {
	w := make(map[string]bool, len(written))
	for _, k := range written {
		w[k] = true
	}
	out := make([]ResolvedThreshold, 0, len(resolved))
	for _, r := range resolved {
		key := r.Component + "_" + r.Metric
		if _, builtin := config.BuiltinDefault(key); builtin && !w[key] {
			continue
		}
		out = append(out, r)
	}
	return out
}

// withBuiltinDefaults is want with every built-in key it does not hold laid
// under it at the platform's value — a root `defaults:` map as the merge now
// holds it.
func withBuiltinDefaults(want map[string]float64) map[string]float64 {
	out := config.BuiltinDefaults()
	// #4163: the off table, its value the one SeedBuiltinDefaults lays.
	off := map[string]float64{}
	config.SeedBuiltinDefaults(off)
	for _, k := range config.BuiltinOffKeys() {
		out[k] = off[k]
	}
	for k, v := range want {
		out[k] = v
	}
	return out
}

// sameDefaults is a Defaults map compared by bits: a NaN (a refused root
// value, a root disable, the built-in off value of #4163) is never == itself,
// and the bits are the verdict.
func sameDefaults(a, b map[string]float64) bool {
	if len(a) != len(b) {
		return false
	}
	for k, av := range a {
		bv, ok := b[k]
		if !ok || math.Float64bits(av) != math.Float64bits(bv) {
			return false
		}
	}
	return true
}

// sameThresholdConfig is reflect.DeepEqual with Defaults compared by
// sameDefaults.
func sameThresholdConfig(a, b *ThresholdConfig) bool {
	if a == nil || b == nil {
		return a == b
	}
	if !sameDefaults(a.Defaults, b.Defaults) {
		return false
	}
	ac, bc := *a, *b
	ac.Defaults, bc.Defaults = nil, nil
	return reflect.DeepEqual(ac, bc)
}
