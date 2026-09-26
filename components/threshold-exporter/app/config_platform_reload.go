package main

// Reload membership of the platform per-tenant layer (#2019).
//
// A root platform file's `tenants:` block feeds a tenant's merged_hash
// (config.ComputeMergedHash's overlay) without being in that tenant's
// defaults CHAIN — `_profiles.yaml` never is, and `_defaults.yaml` is only
// because it is also the L0 carrier. classifyTenant's chain comparison
// (#1964) therefore cannot see an edit to it; these helpers are the second
// input set it compares, per tenant, so that editing only a platform
// file's entry for tx recomputes tx's merged_hash and attributes the
// reload, and an edit to ty's entry leaves tx's cached hash alone.

import (
	"os"
	"reflect"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// readTreeFile is a reload tick's bytes for a scan file: the scan's own when
// it read them (the hash moved), else a read of the file — the same
// fallback recomputeMergedHash always makes.
func readTreeFile(f *config.TreeFile) ([]byte, error) {
	if f.Data != nil {
		return f.Data, nil
	}
	return os.ReadFile(f.AbsPath)
}

// platformFilesMoved reports whether the root platform files of two ticks
// differ in membership or bytes. False on the common tick, which then skips
// the per-tenant comparison entirely.
func platformFilesMoved(prior, next []config.PlatformTenants) bool {
	if len(prior) != len(next) {
		return true
	}
	for i := range prior {
		if prior[i].File != next[i].File || prior[i].Hash != next[i].Hash {
			return true
		}
	}
	return false
}

// platformOverlayDelta compares tenant tid's root-platform-file entries
// between two ticks. paths are the absolute paths of the files whose entry
// for tid was added, removed or changed (the scope attribution); keys are
// the top-level keys whose platform-supplied value changed once the files
// are applied in merge order (classifyDefaultsNoOpEffect's shadow test).
// Both nil when no entry for tid moved.
func platformOverlayDelta(prior, next []config.PlatformTenants, tid string) (paths, keys []string) {
	priorBlocks, abs := platformBlocksOf(prior, tid, nil)
	nextBlocks, abs := platformBlocksOf(next, tid, abs)
	if len(priorBlocks) == 0 && len(nextBlocks) == 0 {
		return nil, nil
	}
	for file, a := range abs {
		pb, inPrior := priorBlocks[file]
		nb, inNext := nextBlocks[file]
		if inPrior != inNext || !reflect.DeepEqual(pb, nb) {
			paths = append(paths, a)
		}
	}
	if len(paths) == 0 {
		return nil, nil
	}
	sort.Strings(paths)
	pu, nu := platformUnion(prior, tid), platformUnion(next, tid)
	for k, pv := range pu {
		if nv, ok := nu[k]; !ok || !reflect.DeepEqual(pv, nv) {
			keys = append(keys, k)
		}
	}
	for k := range nu {
		if _, ok := pu[k]; !ok {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return paths, keys
}

// platformBlocksOf is tid's entry per file name, recording each file's
// absolute path into abs (allocated on first use).
func platformBlocksOf(files []config.PlatformTenants, tid string, abs map[string]string) (map[string]map[string]any, map[string]string) {
	var out map[string]map[string]any
	for _, pt := range files {
		b, ok := pt.Block(tid)
		if !ok {
			continue
		}
		if out == nil {
			out = make(map[string]map[string]any)
		}
		if abs == nil {
			abs = make(map[string]string)
		}
		out[pt.File] = b
		abs[pt.File] = pt.AbsPath
	}
	return out, abs
}

// platformUnion is tid's platform values as the merge sees them: every
// file's entry applied in merge order, a later file over an earlier one.
func platformUnion(files []config.PlatformTenants, tid string) map[string]any {
	out := make(map[string]any)
	for _, pb := range config.PlatformOverlayFor(files, tid) {
		for k, v := range pb.Block {
			out[k] = v
		}
	}
	return out
}
