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
// value: the exporter's reader ignores it.
type ValueFinding struct {
	Tenant string
	Key    string // canonical spelling
	// Value is the value as written; Ratchet compares the exporter's reading
	// of it (readAlike), never the ScheduledValue itself.
	Value ScheduledValue
	// Problem is the reader's tenant-free reason (CheckReservedValue).
	Problem string
	// Message is the Errors entry, word for word.
	Message string
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

// notice is the advisory Ratchet puts in the finding's place.
func (f ValueFinding) notice() string {
	return fmt.Sprintf("NOTICE: tenant=%s: %s %q is not a value the exporter reads (%s) — %s. "+
		"It is already in the tenant file, so it does not block a write that leaves it as it is; "+
		"fix it the next time %s is changed",
		f.Tenant, f.Key, strings.TrimSpace(f.Value.Default), f.Problem, valueEffect(f.Key), f.Key)
}

// Ratchet moves each value finding whose key prev had with a value the
// exporter reads the same way (readAlike — the reader's own normalized
// reading, so a re-quoted or re-cased spelling counts as unchanged) from
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
		if !ok || !readAlike(f.Key, pv, f.Value) {
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
