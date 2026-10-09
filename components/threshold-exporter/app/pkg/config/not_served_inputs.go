package config

// The inputs ValuesNotServed reads, for a caller that keeps its verdicts
// between commits and recomputes only the tenants whose inputs moved (#2065:
// the exporter's per-commit audit).
//
// ⛔ THE FINGERPRINTS ARE OF THE RESOLVER'S OWN INPUT, not of the files. A
// tenant's verdicts are a function of (its built map cfg.Tenants[id], the
// config-wide values the resolve phases read — Defaults and
// OptionalOverrides —, and now through `expires:`), because the record sites
// sit in resolveBaseRows / resolveCriticalRows / resolveDimensionalRows /
// resolveDeclaredRows, which read nothing else (MaxMetricsPerTenant cuts rows
// after them). The built map already carries what the tenant file, the root
// platform files' `tenants:` entries, the profiles and the subtree defaults
// overlay put there, so hashing it needs no account of which file feeds which
// key — a merged_hash or a file hash would (merged_hash is the untyped
// written merge, not the overlay's typed result, and flat mode has none).
// TestValuesNotServedCache_* (package main) drives random reload sequences
// against a recompute of everything.

import (
	"hash/fnv"
	"math"
	"sort"
	"time"
)

// TenantValuesInput is a fingerprint of one tenant's built map — every key
// and every field of its ScheduledValue (default, each window and value in
// order, expires, reason). Equal fingerprints of the same map content are
// guaranteed; distinct content colliding is a 64-bit FNV-1a collision.
func TenantValuesInput(overrides map[string]ScheduledValue) uint64 {
	keys := make([]string, 0, len(overrides))
	for k := range overrides {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := fnv.New64a()
	field := func(s string) {
		_, _ = h.Write([]byte(s))
		_, _ = h.Write([]byte{0})
	}
	for _, k := range keys {
		sv := overrides[k]
		field(k)
		field(sv.Default)
		for _, o := range sv.Overrides {
			field(o.Window)
			field(o.Value)
		}
		_, _ = h.Write([]byte{1})
		if sv.Expiry != nil {
			field(sv.Expiry.Expires)
			field(sv.Expiry.Reason)
		}
		_, _ = h.Write([]byte{2})
	}
	return h.Sum64()
}

// ValuesNotServedGlobalInput is a fingerprint of the config-wide values the
// resolve phases read: Defaults (key and value) and OptionalOverrides (in
// order). When it moves, every tenant's verdicts may move.
func (c *ThresholdConfig) ValuesNotServedGlobalInput() uint64 {
	keys := make([]string, 0, len(c.Defaults))
	for k := range c.Defaults {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := fnv.New64a()
	var b [8]byte
	for _, k := range keys {
		_, _ = h.Write([]byte(k))
		_, _ = h.Write([]byte{0})
		bits := math.Float64bits(c.Defaults[k])
		for i := range b {
			b[i] = byte(bits >> (8 * i))
		}
		_, _ = h.Write(b[:])
	}
	_, _ = h.Write([]byte{1})
	for _, k := range c.OptionalOverrides {
		_, _ = h.Write([]byte(k))
		_, _ = h.Write([]byte{0})
	}
	return h.Sum64()
}

// TenantValuesNotServed is ValuesNotServed for one tenant of c (its entry of
// the full table; nil when it records nothing or the tenant is absent).
func (c *ThresholdConfig) TenantValuesNotServed(tenant string, now time.Time) map[string]string {
	overrides, ok := c.Tenants[tenant]
	if !ok {
		return nil
	}
	one := *c
	one.Tenants = map[string]map[string]ScheduledValue{tenant: overrides}
	return one.ValuesNotServed(now)[tenant]
}

// ValuesNotServedExpiryEdge is the earliest `expires:` instant of overrides
// that now has not passed — the instant after which isThresholdExpired (the
// one time-dependent input of a tenant's verdicts besides the whole-day
// schedule reading) changes for one of its values. ok is false when there
// is none (no time-box, all passed, or unparseable — which the resolver
// treats as never expiring).
func ValuesNotServedExpiryEdge(overrides map[string]ScheduledValue, now time.Time) (edge time.Time, ok bool) {
	for _, sv := range overrides {
		if sv.Expiry == nil || sv.Expiry.Expires == "" {
			continue
		}
		t, err := time.Parse(time.RFC3339, sv.Expiry.Expires)
		if err != nil || now.After(t) {
			continue
		}
		if !ok || t.Before(edge) {
			edge, ok = t, true
		}
	}
	return edge, ok
}
