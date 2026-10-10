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
// the flat merge stacks layers through overlayTenantLayer) and the
// tenant-api metadata readers (RootPlatform.PlatformMetadata + MergeMetadata).
// The walker plane (/effective, da-guard effective) carries no `_metadata`
// at all (mergeDroppedKeys); that is unchanged.

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

// MergeMetadata applies one more layer over the metadata of the layers below
// it (nil = none): the per-key rule of this file. own is the layer's decoded
// `_metadata` value and writes reports whether the layer writes the key at
// all — a layer that does not write `_metadata` leaves below unchanged, one
// that writes a non-mapping clears it.
func MergeMetadata(below map[string]any, own any, writes bool) map[string]any {
	if !writes {
		return below
	}
	return overlayMetadata(below, own)
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

// PlatformMetadata returns the `_metadata` the root platform files'
// `tenants.<tenantID>` entries resolve to — merged per key in merge order —
// or nil when none writes it (or the last one to write it wrote a
// non-mapping). A file the flat decode rejects contributes nothing.
// Apply the tenant's own layer with MergeMetadata. The result is a new map.
func (root RootPlatform) PlatformMetadata(tenantID string) map[string]any {
	var acc map[string]any
	for i := range root.r.files {
		pf := root.r.files[i].parsed
		if pf.err != nil {
			continue
		}
		sv, writes := pf.cfg.Tenants[tenantID][metadataKey]
		if !writes {
			continue
		}
		acc = overlayMetadata(acc, decodeMetadataValue(sv))
	}
	return acc
}
