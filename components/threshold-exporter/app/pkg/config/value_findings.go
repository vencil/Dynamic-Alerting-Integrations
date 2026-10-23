package config

// #3227 (ADR-038 decision 12): a reserved key's VALUE is judged at the write
// gate by the exporter's own reading of it (CheckReservedValue), and a value
// already on disk that a write leaves as the exporter reads it does not
// block that write — it is reported as a notice instead (Ratchet).

import (
	"fmt"
	"strings"
)

// ValueFinding is one KeyValidation.Errors entry about a reserved key's
// value: the exporter's reader ignores it, or ignores part of it.
type ValueFinding struct {
	Tenant string
	Key    string // canonical spelling
	// Value is the value as written. Ratchet compares it with the value the
	// key had as the finding's kind says (unchanged): the exporter's reading
	// of it (readAlike) for a value the reader ignores, the value itself,
	// schedule included (sameSchedule), for a schedule.
	Value ScheduledValue
	// Problem is the tenant-free reason: the reader's (CheckReservedValue),
	// or scheduleProblem.
	Problem string
	// Message is the Errors entry, word for word.
	Message string
	// schedule marks the #3269 kind (newScheduleFinding): a reserved key
	// written with a schedule its readers ignore.
	schedule bool
}

func newValueFinding(tenant, key string, sv ScheduledValue, problem string) ValueFinding {
	return ValueFinding{
		Tenant:  tenant,
		Key:     key,
		Value:   sv,
		Problem: problem,
		Message: fmt.Sprintf("WARN: tenant=%s: %s %q is not a value the exporter reads (%s) — %s",
			tenant, key, strings.TrimSpace(sv.Default), problem, valueEffect(key)),
	}
}

// scheduleProblem is a schedule finding's tenant-free reason.
const scheduleProblem = "a reserved key does not support a time window or expires"

// newScheduleFinding is the finding for sv, the value of reserved key key
// written with a schedule (scheduledReserved, #3269).
func newScheduleFinding(tenant, key string, sv ScheduledValue) ValueFinding {
	return ValueFinding{
		Tenant:  tenant,
		Key:     key,
		Value:   sv,
		Problem: scheduleProblem,
		Message: fmt.Sprintf("WARN: tenant=%s: %s writes %s (%s) — only its default %q is read, the rest is ignored. "+
			"Remove %s%s",
			tenant, key, scheduleFields(sv), scheduleProblem,
			strings.TrimSpace(sv.Default), scheduleFields(sv), scheduleHint(key)),
		schedule: true,
	}
}

// unchanged reports whether prev, the value f.Key had before the write, is
// the value f is about as far as f's kind is concerned.
func (f ValueFinding) unchanged(prev ScheduledValue) bool {
	if f.schedule {
		// As written, not as read: the reading of a schedule is its
		// `default`, so comparing readings would let a write that moves
		// or adds a window through as "unchanged".
		return sameSchedule(prev, f.Value)
	}
	return readAlike(f.Key, prev, f.Value)
}

// notice is the advisory Ratchet puts in the finding's place.
func (f ValueFinding) notice() string {
	if f.schedule {
		return fmt.Sprintf("NOTICE: tenant=%s: %s writes %s (%s) — only its default %q is read, the rest is ignored. "+
			"It is already in the tenant file, so it does not block a write that leaves it exactly as it is; "+
			"remove %s the next time %s is changed%s",
			f.Tenant, f.Key, scheduleFields(f.Value), f.Problem,
			strings.TrimSpace(f.Value.Default), scheduleFields(f.Value), f.Key, scheduleHint(f.Key))
	}
	return fmt.Sprintf("NOTICE: tenant=%s: %s %q is not a value the exporter reads (%s) — %s. "+
		"It is already in the tenant file, so it does not block a write that leaves it as it is; "+
		"fix it the next time %s is changed",
		f.Tenant, f.Key, strings.TrimSpace(f.Value.Default), f.Problem, valueEffect(f.Key), f.Key)
}

// Ratchet moves each value finding whose key prev had with a value the
// finding counts as unchanged (ValueFinding.unchanged: for a value the
// reader ignores, one the exporter reads the same way — readAlike, the
// reader's own normalized reading, so a re-quoted or re-cased spelling
// counts as unchanged; for a schedule, the same value as written) from
// Errors to Notices. Every other entry of Errors stays where it is.
//
// prev is the value the key had before this write, in the canonical
// spelling: (sv, true) when it was set, (_, false) when it was not or nothing
// is known about it. A caller with no trustworthy base answers false for
// everything — or does not call Ratchet — and stays strict.
func (v KeyValidation) Ratchet(prev func(tenant, key string) (ScheduledValue, bool)) KeyValidation {
	if len(v.Values) == 0 || prev == nil {
		return v
	}
	out := KeyValidation{
		Errors:  append([]string(nil), v.Errors...),
		Notices: append([]string(nil), v.Notices...),
	}
	for _, f := range v.Values {
		pv, ok := prev(f.Tenant, f.Key)
		if !ok || !f.unchanged(pv) {
			out.Values = append(out.Values, f)
			continue
		}
		out.Errors = removeFirst(out.Errors, f.Message)
		out.Notices = append(out.Notices, f.notice())
	}
	return out
}

// Ratchet is KeyValidation.Ratchet for every tenant (a flat file declares
// several). The platform part names no tenant and carries no value finding.
func (kv KeyValidationByTenant) Ratchet(prev func(tenant, key string) (ScheduledValue, bool)) KeyValidationByTenant {
	out := KeyValidationByTenant{Platform: kv.Platform, Tenants: make(map[string]KeyValidation, len(kv.Tenants))}
	for tid, v := range kv.Tenants {
		out.Tenants[tid] = v.Ratchet(prev)
	}
	return out
}

// PriorValues is a Ratchet prev over tenants (a tenant file's `tenants:` as
// decoded), looked up in the canonical spelling: an aliased spelling stands
// for its canonical key, and when a map has both the canonical entry wins,
// as resolve does.
func PriorValues(tenants map[string]map[string]ScheduledValue) func(tenant, key string) (ScheduledValue, bool) {
	return func(tenant, key string) (ScheduledValue, bool) {
		keys := tenants[tenant]
		if sv, ok := keys[key]; ok {
			return sv, true
		}
		for k, sv := range keys {
			if canon, wasAlias := canonicalKeyFor(k); wasAlias && canon == key {
				return sv, true
			}
		}
		return ScheduledValue{}, false
	}
}

// SelfRatchet is Ratchet against the values the findings are about: every
// value finding becomes a notice. For a reader of what is on disk (GET),
// where no write is being judged and nothing a write would leave as it is
// should read as blocking.
func (kv KeyValidationByTenant) SelfRatchet() KeyValidationByTenant {
	return kv.Ratchet(func(tenant, key string) (ScheduledValue, bool) {
		for _, f := range kv.Tenants[tenant].Values {
			if f.Key == key {
				return f.Value, true
			}
		}
		return ScheduledValue{}, false
	})
}

func removeFirst(in []string, s string) []string {
	for i, e := range in {
		if e == s {
			return append(in[:i:i], in[i+1:]...)
		}
	}
	return in
}
