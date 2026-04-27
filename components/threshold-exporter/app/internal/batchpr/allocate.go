package batchpr

// PR-2 — file allocation helper.
//
// C-9 PR-3 EmitProposals returns a flat `path → bytes` map for the
// whole proposal set. C-10 PR-2 Apply() needs that map split per
// PlanItem. AllocateFiles is the canonical splitter the CLI / UI
// will call between Emit and Apply.
//
// Allocation rules (Profile-as-Directory-Default — ADR-019 §1):
//
//   1. Any path matching `_defaults.yaml` (basename) → Base PR.
//      The Base Infrastructure PR carries every cascading defaults
//      change in the Plan (PR-1 §C-10 chunking strategy).
//
//   2. Any path matching `<tenant-id>.yaml` where <tenant-id>
//      appears in some Plan.Items[i].TenantIDs (i.e. a tenant
//      chunk) → that chunk's PR.
//
//   3. PROPOSAL.md → Base PR (mirrors the convention C-9 PR-2's
//      emit_translated already implies — the proposal-level
//      summary lives alongside the shared defaults).
//
//   4. Anything else → Warning + skipped. Common cases that hit
//      this branch: stale files left over in the EmissionOutput
//      from a partial / debug run, or a tenant ID present in a
//      file path but absent from any Plan chunk (caller bug).
//
// Empty-input contract:
//   - `plan == nil` or `len(plan.Items) == 0` → returns (nil, [
//     "AllocateFiles: empty plan; nothing to allocate"]).
//   - Empty `files` map → returns ({}, []) — no warning, no work.

import (
	"fmt"
	"path"
	"strings"
)

// AllocateFiles distributes the global emit Files map into per-
// PlanItem buckets. Returns:
//   - itemFiles: map[planItemIdx] map[path][]byte ready for
//     ApplyInput.ItemFiles.
//   - warnings: human-readable notes for files that didn't fit
//     any bucket (callers append these to ApplyResult.Warnings).
func AllocateFiles(plan *Plan, files map[string][]byte) (map[int]map[string][]byte, []string) {
	if plan == nil || len(plan.Items) == 0 {
		return nil, []string{"AllocateFiles: empty plan; nothing to allocate"}
	}
	if len(files) == 0 {
		return map[int]map[string][]byte{}, nil
	}

	// Find the Base PR index (0 or 1; the planner always emits
	// the Base PR first when present).
	baseIdx := -1
	for i, it := range plan.Items {
		if it.Kind == PlanItemBase {
			baseIdx = i
			break
		}
	}

	// Build a tenant ID → PlanItem index lookup. A tenant should
	// only appear in one chunk (PR-1 contract), but we coalesce
	// duplicates defensively into the first chunk.
	tenantToIdx := make(map[string]int, 64)
	for i, it := range plan.Items {
		if it.Kind != PlanItemTenant {
			continue
		}
		for _, tid := range it.TenantIDs {
			if _, exists := tenantToIdx[tid]; !exists {
				tenantToIdx[tid] = i
			}
		}
	}

	out := make(map[int]map[string][]byte, len(plan.Items))
	var warnings []string

	for p, body := range files {
		bucket, reason := bucketForPath(p, baseIdx, tenantToIdx)
		if bucket < 0 {
			warnings = append(warnings, fmt.Sprintf(
				"AllocateFiles: file %q has no plan bucket (%s); skipped",
				p, reason))
			continue
		}
		if out[bucket] == nil {
			out[bucket] = make(map[string][]byte)
		}
		out[bucket][p] = body
	}

	return out, warnings
}

// bucketForPath picks the right Plan.Items index for `p`, or
// returns (-1, reason) when the path doesn't fit any bucket.
//
// Single source of truth for the allocation rules; AllocateFiles
// loops over files and calls this per path.
func bucketForPath(p string, baseIdx int, tenantToIdx map[string]int) (int, string) {
	base := path.Base(p)
	switch {
	case base == "_defaults.yaml":
		if baseIdx < 0 {
			return -1, "no Base PR in plan"
		}
		return baseIdx, ""
	case base == "PROPOSAL.md":
		if baseIdx < 0 {
			return -1, "no Base PR in plan to absorb PROPOSAL.md"
		}
		return baseIdx, ""
	case strings.HasSuffix(base, ".yaml"):
		// Strip `.yaml` to recover the tenant ID candidate.
		tid := strings.TrimSuffix(base, ".yaml")
		if tid == "" {
			return -1, "empty filename before .yaml"
		}
		idx, ok := tenantToIdx[tid]
		if !ok {
			return -1, fmt.Sprintf("tenant ID %q not in any Plan chunk", tid)
		}
		return idx, ""
	default:
		return -1, fmt.Sprintf("unrecognised file shape %q (only _defaults.yaml / PROPOSAL.md / <tenant>.yaml supported)", base)
	}
}
