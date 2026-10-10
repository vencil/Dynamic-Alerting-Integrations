package config

// `_metadata` across a tenant's layers (#2370).
//
// A tenant's `_metadata` can be written in two kinds of layer: a ROOT
// platform file's `tenants.<id>._metadata` (the platform's per-tenant
// default; several root platform files apply in merge order,
// sortFlatMergeOrder) and the tenant's own file. Every plane that reads
// tenant metadata resolves those layers with ONE rule:
//
//   - PER KEY. A later layer writes its keys over the earlier layers'; a
//     key it does not write keeps the earlier value. A tenant file writing
//     only `db_type` keeps the platform's `owner` / `environment` / `domain`.
//   - ORDER. Root platform files in merge order (a later file over an
//     earlier one), the tenant file over all of them.
//   - A VALUE IS REPLACED WHOLE. `tags: [a]` over `tags: [b, c]` is `[a]`;
//     the merge does not descend into a key's value.
//   - A LAYER WHOSE `_metadata` IS NOT A MAPPING (null, a scalar, a list)
//     replaces everything below it: no key survives from the earlier layers.
//     A tenant writing `_metadata: null` therefore has no metadata.
//
// The planes: /metrics (tenant_metadata_info, tenant_expected_exporter —
// the flat merge stacks layers through overlayTenantLayer, then
// ApplyProfiles fills in, then ResolveMetadata decodes) and the tenant-api
// metadata readers (MetadataResolver, which runs those same three steps
// for one tenant, #2830). The walker plane (/effective,
// da-guard effective) carries no `_metadata` at all (mergeDroppedKeys);
// that is unchanged.

import (
	"errors"
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

// metadataKey is the reserved key the rule above applies to.
const metadataKey = "_metadata"

// overlayMetadata returns base with layer's keys written over it. layer is
// a decoded YAML value; when it is not a mapping the result is nil (the
// layer replaces everything below it). base is never written to.
func overlayMetadata(base map[string]any, layer any) map[string]any {
	m, ok := layer.(map[string]any)
	if !ok {
		return nil
	}
	out := make(map[string]any, len(base)+len(m))
	for k, v := range base {
		out[k] = v
	}
	for k, v := range m {
		out[k] = v
	}
	return out
}

// decodeMetadataValue decodes a `_metadata` ScheduledValue back into the YAML
// value it was written as (UnmarshalYAML re-serializes a mapping into
// Default). An empty or undecodable Default decodes to nil — not a mapping.
func decodeMetadataValue(sv ScheduledValue) any {
	if sv.Default == "" {
		return nil
	}
	var v any
	if yaml.Unmarshal([]byte(sv.Default), &v) != nil {
		return nil
	}
	return v
}

// overlayMetadataValue is the per-key rule on the flat plane's
// representation: prev is what the earlier layers left for `_metadata`, next
// is what the later layer writes. When both decode to mappings the result is
// their per-key merge, re-serialized as UnmarshalYAML would have stored it;
// otherwise next replaces prev whole (a non-mapping layer clears what is
// below it, and a non-mapping below has no key to keep).
func overlayMetadataValue(prev, next ScheduledValue) ScheduledValue {
	nm, ok := decodeMetadataValue(next).(map[string]any)
	if !ok {
		return next
	}
	pm, ok := decodeMetadataValue(prev).(map[string]any)
	if !ok {
		return next
	}
	out, err := yaml.Marshal(overlayMetadata(pm, nm))
	if err != nil {
		return next
	}
	return ScheduledValue{Default: string(out)}
}

// overlayTenantLayer writes one layer of a tenant's overrides (src) over the
// layers below it (dst): overlayAcrossSpellings' per-key rule for every key,
// and for `_metadata` the per-key rule of this file instead of a whole-value
// replace. It is how every flat-plane path stacks a tenant's layers
// (mergePartialInto, and the incremental reload's OverlayTenantLayer).
//
// ⚠️ dst's `_metadata` is REPLACED by a new value, never edited in place, and
// only when both layers write it — the common shape (one layer writes it, or
// none) costs one map lookup.
func overlayTenantLayer(dst, src map[string]ScheduledValue) {
	prev, had := dst[metadataKey]
	overlayAcrossSpellings(dst, src)
	if !had {
		return
	}
	if next, writes := src[metadataKey]; writes {
		dst[metadataKey] = overlayMetadataValue(prev, next)
	}
}

// OverlayTenantLayer is overlayTenantLayer for package main's incremental
// reload (reclaimTenantFrom), which must stack a tenant's declaring files
// exactly as mergePartialInto does.
func OverlayTenantLayer(dst, src map[string]ScheduledValue) {
	overlayTenantLayer(dst, src)
}

// ErrRootPlatformUnread is wrapped by LoadRootPlatformChecked when the root
// platform layer could not be read in full.
var ErrRootPlatformUnread = errors.New("root platform files could not be read")

// LoadRootPlatformChecked is LoadRootPlatform for readers that must tell an
// empty platform layer from one that could not be read: it returns an error
// wrapping ErrRootPlatformUnread when the root cannot be walked or listed,
// or when a root `_`-prefixed config file was dropped by the walk because its
// stat or read failed. A file that was read but that the flat decode
// (ParseConfigFile) rejects is NOT an error: it contributes nothing, as on
// /metrics, which skips it.
func LoadRootPlatformChecked(configDir string) (RootPlatform, error) {
	scan, err := scanRootPlatform(configDir)
	if err != nil {
		return RootPlatform{}, fmt.Errorf("%w: %v", ErrRootPlatformUnread, err)
	}
	if scan.RootWalkErr != nil {
		return RootPlatform{}, fmt.Errorf("%w: %v", ErrRootPlatformUnread, scan.RootWalkErr)
	}
	var unread []string
	for _, u := range scan.Unreadable {
		unread = append(unread, fmt.Sprintf("%q (%s)", u.RelKey, u.Reason))
	}
	for _, k := range rootPlatformKeys(scan) {
		if f := scan.Files[k]; f == nil || f.Data == nil {
			unread = append(unread, fmt.Sprintf("%q (not read)", k))
		}
	}
	if len(unread) > 0 {
		return RootPlatform{}, fmt.Errorf("%w: %s", ErrRootPlatformUnread, strings.Join(unread, ", "))
	}
	return RootPlatform{r: rootPlatformFrom(scan)}, nil
}

// MetadataResolver reads tenants' `_metadata` over one RootPlatform the way
// /metrics reads it (#2830). Build it once per root read
// (RootPlatform.MetadataResolver) and resolve each tenant with Resolve or
// ResolveFile: the part every tenant shares — the root files' merged
// `profiles:` and the root carrier's `optional_overrides` — is computed once
// here, and each tenant pays only for its own layers.
//
// Per tenant, /metrics' own steps:
//
//   - LAYERS: the root platform files' `tenants.<id>` entries in merge
//     order, then the tenant file's, stacked with overlayTenantLayer (as
//     mergePartialInto does) — for `_metadata`, the per-key rule at the top
//     of this file; for `_profile`, the later layer's value. A layer written
//     as a string holding YAML (`_metadata: "owner: x\n"`) decodes to the
//     same mapping as the mapping form, as on /metrics, where both reach
//     ScheduledValue's Default as YAML text.
//   - PROFILE: applyProfiles, the fill-in ApplyProfiles runs, over the
//     profiles' `_metadata` alone: when no layer writes `_metadata`, the
//     elected profile's fills it in whole; a layer that writes it keeps the
//     profile's out entirely, not per key. (profileFill decides each key on
//     its own, so leaving the profiles' other keys out changes nothing for
//     `_metadata`.)
//   - DECODE: tenantMetadataOf, ResolveMetadata's per-tenant body — a
//     non-string scalar value is read as its text (`environment: 123` is
//     "123"), and a value that cannot decode into its TenantMetadata field's
//     type (a mapping for `environment`, a single string for `tags`) leaves
//     every field empty.
//
// Nothing is logged: not the decode WARN, not the profile WARNs, and not the
// root carrier's parse ERROR (/metrics and the tenant-api merge core log
// those). The zero value — and the resolver of the zero RootPlatform — has
// no platform files and no profiles: it reads the tenant file's own
// `_metadata` alone. Read-only after construction; safe for concurrent use.
type MetadataResolver struct {
	root rootPlatform
	// profiles is, per profile name, the merged profile's `_metadata` entry
	// (mergeProfilesInto's rule: a later file over an earlier one), or an
	// empty map when it writes none — the name is still a known profile.
	profiles map[string]map[string]ScheduledValue
	// declared is the root carrier's optional_overrides when they declare
	// `_metadata` (then no profile may fill it in), else nil.
	declared []string
}

// MetadataResolver builds the resolver for this root platform surface.
func (root RootPlatform) MetadataResolver() MetadataResolver {
	m := MetadataResolver{root: root.r}
	all := make(map[string]map[string]ScheduledValue)
	root.r.mergeProfilesInto(all)
	if len(all) > 0 {
		m.profiles = make(map[string]map[string]ScheduledValue, len(all))
		for name, values := range all {
			only := make(map[string]ScheduledValue, 1)
			if sv, ok := values[metadataKey]; ok {
				only[metadataKey] = sv
			}
			m.profiles[name] = only
		}
	}
	if c := root.r.carrier(); c != nil && c.parsed.err == nil {
		if _, declared := canonicalizeOptionalOverrides(c.parsed.cfg.OptionalOverrides)[metadataKey]; declared {
			m.declared = []string{metadataKey}
		}
	}
	return m
}

// Resolve reads tenantID's metadata, own being the tenant's entry in its
// tenant file as ParseConfigFile decodes it (nil = the file writes no key).
func (m MetadataResolver) Resolve(tenantID string, own map[string]ScheduledValue) ResolvedMetadata {
	layers := make(map[string]ScheduledValue, 2)
	stack := func(layer map[string]ScheduledValue) {
		for _, k := range [...]string{metadataKey, "_profile"} {
			if sv, writes := layer[k]; writes {
				overlayTenantLayer(layers, map[string]ScheduledValue{k: sv})
			}
		}
	}
	for i := range m.root.files {
		if pf := m.root.files[i].parsed; pf.err == nil {
			stack(pf.cfg.Tenants[tenantID])
		}
	}
	stack(own)
	c := ThresholdConfig{
		Tenants:           map[string]map[string]ScheduledValue{tenantID: layers},
		Profiles:          m.profiles,
		OptionalOverrides: m.declared,
	}
	c.applyProfiles(nil)
	return tenantMetadataOf(tenantID, layers, nil)
}

// ResolveFile is Resolve over a tenant file's bytes. ok is false when they
// do not decode (ParseConfigFile) or do not declare tenantID: no metadata is
// read for it, as /metrics reads none from a file it skips.
func (m MetadataResolver) ResolveFile(tenantID string, tenantData []byte) (meta ResolvedMetadata, ok bool) {
	tenantCfg, err := ParseConfigFile(tenantData)
	if err != nil {
		return ResolvedMetadata{Tenant: tenantID}, false
	}
	own, declared := tenantCfg.Tenants[tenantID]
	if !declared {
		return ResolvedMetadata{Tenant: tenantID}, false
	}
	return m.Resolve(tenantID, own), true
}
