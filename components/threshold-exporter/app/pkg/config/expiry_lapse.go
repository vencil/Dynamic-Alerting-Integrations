package config

// expiry_lapse.go — what a time-boxed threshold gives way to (#4263, #4254).
//
// ONE RULE. A value whose `expires:` has passed is as if it were not
// written: the tenant gets what the merge would have given it without that
// value — the next layer down (a root platform file's `tenants:` entry under
// the tenant's file, a profile, a subtree `_defaults.yaml`), else the root
// `defaults:`. It is the reading a deleted line gets, so the two cannot
// differ (TestExpiry_ExpiredLineServesAsDeleted pins it on /metrics).
//
// Except where that would go silent (PREVENT #656): when nothing beneath
// gives the key a value — no layer under it and no root value, or only the
// built-in layer's off (expiryHonoured) — the value is served past its
// `expires:`, and the write check refuses the `expires:` (ValidateTenantKeys).
// A `disable` beneath is a value: an explicit platform decision, not silence
// (#3832 decision 3).
//
// HOW THE MERGE RECORDS "BENEATH". Every layer of the tenant's merged map
// is filled top-down and skips a key a higher layer already holds
// (overlayTenantLayer, applyProfiles, applySubtreeDefaults, the tenant-api
// merge's platform supply). Where it skips a key whose held value carries an
// `expires:`, it stacks its own value beneath (stackLapse) instead of
// dropping it. The chain is copied on write: a held value may be shared with
// a cached partial (the incremental reload keeps them).

import "time"

// lapseLayer is the merge layer a lapse target came from, highest first.
// stackLapse keeps one value per layer, so re-running a fill (the
// incremental reload runs ApplyProfiles over an already merged config)
// stacks nothing twice.
type lapseLayer uint8

const (
	lapseLayerSubtree lapseLayer = 1 // a subtree `_defaults.yaml` (deepest level)
	lapseLayerProfile lapseLayer = 2 // the tenant's `_profile`
	lapseLayerFile    lapseLayer = 3 // an earlier file's `tenants:` entry
)

// lapseTarget is one value an expiring value gives way to, and its layer.
type lapseTarget struct {
	value ScheduledValue
	layer lapseLayer
}

// stackLapse returns held with under stacked beneath it as the value its
// expiry gives way to — at the end of held's chain, behind every value from
// a higher layer that also expires — and whether it stacked. Nothing is
// stacked under a value without `expires:` (it never gives way), nor where
// the chain already holds a value from layer or a lower one. held itself is
// never mutated: the changed links are copies.
func stackLapse(held, under ScheduledValue, layer lapseLayer) (ScheduledValue, bool) {
	if held.Expiry == nil {
		return held, false
	}
	e := *held.Expiry
	if e.lapse == nil {
		e.lapse = &lapseTarget{value: under, layer: layer}
	} else {
		if e.lapse.layer <= layer {
			return held, false
		}
		next, ok := stackLapse(e.lapse.value, under, layer)
		if !ok {
			return held, false
		}
		e.lapse = &lapseTarget{value: next, layer: e.lapse.layer}
	}
	held.Expiry = &e
	return held, true
}

// liveValue is the value the tenant's sv of key (canonical) is read as at
// at: sv itself until it expires, then what it gives way to — the next
// value of its chain, or, past the end of it, nothing (present false: the
// root `defaults:` value applies, as for a key the tenant does not write)
// when the root values key (lapsesToRoot). With neither, sv is served past
// its `expires:` (PREVENT #656). The one reading of `expires:` on a
// threshold: every row phase, familyHeadOff and the expiry event ask it.
func liveValue(key string, defaults map[string]float64, sv ScheduledValue, at time.Time) (ScheduledValue, bool) {
	for isThresholdExpired(sv, at) {
		if l := sv.Expiry.lapse; l != nil {
			sv = l.value
			continue
		}
		if lapsesToRoot(key, defaults) {
			return ScheduledValue{}, false
		}
		return sv, true
	}
	return sv, true
}

// canLapse reports whether sv's `expires:` can take effect at all: a value
// lies beneath it in the tenant's merge, or the root values key
// (lapsesToRoot). False is the case liveValue serves past `expires:` —
// the one the write check refuses and the expiry event leaves out.
func canLapse(key string, defaults map[string]float64, sv ScheduledValue) bool {
	if sv.Expiry != nil && sv.Expiry.lapse != nil {
		return true
	}
	return lapsesToRoot(key, defaults)
}

// lapsesToRoot reports whether a tenant value of key (canonical) with
// nothing beneath it in the merge may give way to the root `defaults:`: the
// root gives key a value a resolver serves. For a base key that is
// expiryHonoured (a refused root value of a key with a platform value falls
// to the built-in one, #3832). A `<base>_critical` or dimensional key has no
// such layer: only a finite root number is one (#4173, #2031) — a refused
// one (`disable` included: the root reads none for these) serves nothing.
func lapsesToRoot(key string, defaults map[string]float64) bool {
	if CriticalRowKey(key) || isDimensionalKey(key) {
		v, valued := defaults[key]
		return valued && finiteThreshold(v)
	}
	return expiryHonoured(key, defaults)
}

// heldAcrossSpellings is the key under which m holds key's threshold —
// key itself, else another #1231 spelling of it — and whether it does: the
// question hasAliasEquivalent answers, with the key it found.
func heldAcrossSpellings(m map[string]ScheduledValue, key string) (string, bool) {
	if _, ok := m[key]; ok {
		return key, true
	}
	var buf [2]string
	for _, s := range otherSpellings(key, &buf) {
		if _, ok := m[s]; ok {
			return s, true
		}
	}
	return "", false
}

// lapseChain calls f for sv and every value it gives way to, in order (#4263).
// Every reader of a value's time-dependent fields (its schedule, its
// `expires:`) reads the whole chain: liveValue may serve any link of it.
func lapseChain(sv ScheduledValue, f func(ScheduledValue)) {
	for {
		f(sv)
		if sv.Expiry == nil || sv.Expiry.lapse == nil {
			return
		}
		sv = sv.Expiry.lapse.value
	}
}

// fixedAtMinute is sv with its schedule, and the schedule of every value it
// gives way to, read at minute of the day (AtMinuteOfDay), and whether
// anything changed. The expiry chain itself is kept.
func fixedAtMinute(sv ScheduledValue, minute int) (ScheduledValue, bool) {
	changed := false
	if len(sv.Overrides) > 0 {
		sv = ScheduledValue{Default: sv.valueAtMinute(minute, nil), Expiry: sv.Expiry}
		changed = true
	}
	if sv.Expiry != nil && sv.Expiry.lapse != nil {
		if lv, ok := fixedAtMinute(sv.Expiry.lapse.value, minute); ok {
			e := *sv.Expiry
			e.lapse = &lapseTarget{value: lv, layer: sv.Expiry.lapse.layer}
			sv.Expiry = &e
			changed = true
		}
	}
	return sv, changed
}
