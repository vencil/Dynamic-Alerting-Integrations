package batchpr

// PR-2 — file allocation helper.
//
// C-9 PR-3 EmitProposals returns a flat `path → bytes` map for the
// whole proposal set. C-10 PR-2 Apply() needs that map split per
// PlanItem. AllocateFiles is the canonical splitter the CLI / UI
// will call between Emit and Apply.
//
// Allocation rules (Profile-as-Directory-Default — ADR-018 §1):
//
//   1. Any path the exporter would merge into the inheritance
//      chain — `_defaults.yaml` or `_defaults.yml`, in any case —
//      → Base PR. The Base Infrastructure PR carries every
//      cascading defaults change in the Plan (PR-1 §C-10 chunking
//      strategy).
//
//   2. Any path the exporter would read as `<tenant-id>`'s carrier
//      — a `.yaml` / `.yml` basename in any case, neither
//      `_`-reserved nor `.`-hidden — where <tenant-id> appears in
//      some Plan.Items[i].TenantIDs (i.e. a tenant chunk) → that
//      chunk's PR. ⛔ The extension is folded; the tenant id keeps
//      the basename's case, because the exporter does.
//
//   3. PROPOSAL.md → Base PR (mirrors the convention C-9 PR-2's
//      emit_translated already implies — the proposal-level
//      summary lives alongside the shared defaults).
//
//   4. Anything else → Warning + skipped. Common cases that hit
//      this branch: stale files left over in the EmissionOutput
//      from a partial / debug run, a tenant ID present in a file
//      path but absent from any Plan chunk (caller bug), or a name
//      the exporter's walker classifies as neither a defaults nor a
//      tenant carrier (reserved `_` prefix, hidden `.` prefix, or
//      no YAML extension) — committing one of those proposes
//      configuration production will never read.
//
// ⛔ Rules 1, 2 and 4 are pinned against the shared conf.d name
// matrix by confd_name_classification_parity_test.go (#1605); see
// that file's header for why this write-plane component is held to
// the exporter's reading of a name.
//
// Empty-input contract:
//   - `plan == nil` or `len(plan.Items) == 0` → returns (nil, [
//     "AllocateFiles: empty plan; nothing to allocate"]).
//   - Empty `files` map → returns ({}, []) — no warning, no work.

import (
	"fmt"
	"path"

	"github.com/vencil/threshold-exporter/internal/confdname"
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
//
// ⛔ The classification below is pinned against the shared conf.d name matrix
// (`tests/shared/confd_name_classification_matrix.json`) by
// confd_name_classification_parity_test.go, the write-plane half of the
// cross-language pin the exporter and tenant-api already carry (#1605, the
// #1339 family). The files routed here are PROPOSED CONF.D CARRIERS, so every
// branch has to answer the way the exporter's walker would answer for the same
// name — a carrier it merges must reach a PR, and one it never reads must
// reach none. Measured before that pin existed: eleven of the matrix's
// twenty-three names disagreed, four of them silently.
//
// Branch order is load-bearing: the defaults carriers are `_`-prefixed, so
// they have to be recognised before the reserved-prefix drop.
func bucketForPath(p string, baseIdx int, tenantToIdx map[string]int) (int, string) {
	base := path.Base(p)

	// PROPOSAL.md is not a conf.d carrier at all — it is the human-readable
	// summary that rides along in the Base PR (rule 3 above). Deliberately
	// still an exact match: it is this pipeline's own artifact name, not
	// something the exporter classifies, so it is out of the matrix's scope.
	if base == "PROPOSAL.md" {
		if baseIdx < 0 {
			return -1, "no Base PR in plan to absorb PROPOSAL.md"
		}
		return baseIdx, ""
	}

	stem, isCarrier := confdname.SplitCarrier(base)
	if !isCarrier {
		return -1, fmt.Sprintf(
			"unrecognised file shape %q (only PROPOSAL.md and conf.d carriers "+
				"ending .yaml / .yml, any case, are routable)", base)
	}

	// The inheritance-chain carrier. The exporter compares the LOWERCASED
	// name against these two literals exactly, so `_defaults-multidb.yaml`
	// is deliberately NOT one of them — it falls through to the reserved
	// drop below, exactly as the exporter refuses to merge it into a chain.
	if confdname.IsDefaults(base) {
		if baseIdx < 0 {
			return -1, "no Base PR in plan"
		}
		return baseIdx, ""
	}

	// Dot-prefixed names: the exporter's walker skips them outright, so no
	// PR may carry one. ⛔ This drop is not reachable through `tenantToIdx`
	// misses — a plan's tenant ids come from Prometheus label VALUES
	// (ProposalRef.MemberTenantIDs), which may legally be `.hidden`, and
	// before this branch existed such an id routed the file into a chunk
	// with no warning at all.
	if confdname.IsHidden(base) {
		return -1, fmt.Sprintf(
			"%q is hidden (dot-prefixed); the exporter's walker skips it, so "+
				"no PR may carry it as a tenant carrier", base)
	}

	// Reserved-prefix names that are not the defaults carrier: the exporter
	// hashes them for change detection but derives no tenant from them.
	if confdname.IsReserved(base) {
		return -1, fmt.Sprintf(
			"%q uses the reserved `_` prefix but is not the defaults carrier; "+
				"the exporter derives no tenant from it", base)
	}

	if stem == "" {
		return -1, "empty filename before the YAML extension"
	}
	idx, ok := tenantToIdx[stem]
	if !ok {
		return -1, fmt.Sprintf("tenant ID %q not in any Plan chunk", stem)
	}
	return idx, ""
}
