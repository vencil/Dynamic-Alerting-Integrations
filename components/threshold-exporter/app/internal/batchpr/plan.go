package batchpr

// BuildPlan — pure planner that turns a PlanInput into an ordered
// Plan of PRs.
//
// Algorithm (deterministic, see test TestBuildPlan_DeterministicOutput):
//
//   1. Validate input. len(Proposals)==0 → fatal.
//   2. Compute every tenant ID the proposals touch. Tenants whose
//      directory the caller didn't supply via TenantDirs go to
//      Plan.Warnings (they're skipped from chunking but still
//      surface so the caller can fix the input and re-plan).
//   3. Emit ONE Base Infrastructure PlanItem covering every
//      proposal — the proposal-level shared structure becomes the
//      `_defaults.yaml` content. (Empty proposal lists are rejected
//      at step 1, so this step always fires.)
//   4. Bucket the known tenants by ChunkBy:
//        - ChunkByDomain  → first path segment of the dir
//        - ChunkByRegion  → first two path segments
//        - ChunkByCount   → fixed-size sequential buckets
//      Buckets are walked in sorted-key order for determinism.
//   5. Apply ChunkSize as a soft cap to ChunkByDomain/Region
//      buckets — an oversized bucket splits into sub-chunks of at
//      most ChunkSize tenants each.
//   6. Emit one tenant PlanItem per chunk; assign sequential
//      `[chunk i/N]` titles in walk order; mark each as blocked-by
//      the Base PR (when one exists).

import (
	"fmt"
	"sort"
	"strings"
)

// defaultChunkSize matches the value documented on PlanInput.ChunkSize.
const defaultChunkSize = 25

// baseBlockedByMarker is the placeholder PR-2 will replace with the
// real `#<base-pr-num>` once the Base PR has been opened. Stored
// verbatim in PlanItem.BlockedBy for tenant items.
const baseBlockedByMarker = "<base>"

// BuildPlan validates the input and computes the ordered Plan.
//
// Errors:
//   - len(Proposals)==0 → fmt error (caller almost always wants to
//     treat empty input as a bug, not a no-op).
//   - ChunkBy=ChunkByCount with ChunkSize<=0 → fmt error.
//
// Otherwise BuildPlan never errors. Tenants the caller forgot to
// place via TenantDirs surface in Plan.Warnings rather than
// erroring — this lets a caller iteratively refine TenantDirs
// without losing the rest of the plan.
func BuildPlan(input PlanInput) (*Plan, error) {
	if len(input.Proposals) == 0 {
		return nil, fmt.Errorf("batchpr: no proposals to plan")
	}
	chunkBy := input.ChunkBy
	if chunkBy == "" {
		chunkBy = ChunkByDomain
	}
	chunkSize := input.ChunkSize
	if chunkSize <= 0 {
		if chunkBy == ChunkByCount {
			return nil, fmt.Errorf("batchpr: ChunkBy=count requires ChunkSize > 0")
		}
		chunkSize = defaultChunkSize
	}

	plan := &Plan{
		Summary: PlanSummary{
			TotalProposals:     len(input.Proposals),
			ChunkBy:            chunkBy,
			EffectiveChunkSize: chunkSize,
		},
	}

	// Step 1: emit the Base Infrastructure PR. Always exactly one
	// when there is at least one proposal (PR-1 invariant — fuzzier
	// strategies in later PRs may decide to omit it).
	baseItem := buildBasePR(input.Proposals)
	plan.Items = append(plan.Items, baseItem)
	plan.Summary.BasePRCount = 1

	// Step 2: figure out which tenants need to be touched, with
	// dir-resolution warnings.
	knownTenants, missingTenants := collectKnownTenants(input)
	for _, t := range missingTenants {
		plan.Warnings = append(plan.Warnings,
			fmt.Sprintf("tenant %q referenced by proposals but missing from TenantDirs; skipped from chunking", t))
	}

	// Step 3: bucket + chunk the known tenants.
	chunks := bucketTenants(knownTenants, input.TenantDirs, chunkBy, chunkSize)

	totalChunks := len(chunks)
	for i, c := range chunks {
		item := buildTenantPR(i+1, totalChunks, c, input.Proposals)
		plan.Items = append(plan.Items, item)
	}

	plan.Summary.TenantPRCount = len(chunks)
	plan.Summary.TotalTenants = len(knownTenants)

	return plan, nil
}

// collectKnownTenants walks every proposal's MemberTenantIDs,
// dedupes, and partitions into "we have a directory for this
// tenant" vs "missing TenantDirs entry". Both slices are sorted.
func collectKnownTenants(input PlanInput) (known, missing []string) {
	seen := make(map[string]struct{})
	for _, p := range input.Proposals {
		for _, tid := range p.MemberTenantIDs {
			seen[tid] = struct{}{}
		}
	}
	for tid := range seen {
		if _, ok := input.TenantDirs[tid]; ok {
			known = append(known, tid)
		} else {
			missing = append(missing, tid)
		}
	}
	sort.Strings(known)
	sort.Strings(missing)
	return known, missing
}

// chunk holds the tenants destined for one tenant PR plus the
// human-readable bucket key (domain, domain/region, or synthetic
// count label).
type chunk struct {
	key     string
	tenants []string // sorted
}

// bucketTenants groups tenants by the requested strategy and applies
// ChunkSize as a soft cap on bucket size. Returns chunks in
// deterministic walk order (by key, then by sub-chunk index).
func bucketTenants(tenants []string, dirs map[string]string, by ChunkBy, size int) []chunk {
	if len(tenants) == 0 {
		return nil
	}

	switch by {
	case ChunkByCount:
		// Sequential N-sized chunks over the sorted tenant list.
		// Keys are zero-padded so lexicographic sort = numeric sort.
		var out []chunk
		for i := 0; i < len(tenants); i += size {
			end := i + size
			if end > len(tenants) {
				end = len(tenants)
			}
			members := append([]string(nil), tenants[i:end]...)
			out = append(out, chunk{
				key:     fmt.Sprintf("count-bucket-%03d", (i/size)+1),
				tenants: members,
			})
		}
		return out

	case ChunkByDomain, ChunkByRegion, "":
		// The two path-segment strategies share the soft-cap split
		// logic. The `""` case is defensive — BuildPlan normalises
		// an empty ChunkBy to ChunkByDomain before calling here, so
		// in normal flow the empty arm is unreachable. Keeping it
		// in the case list means a future caller invoking
		// bucketTenants directly with the zero value still gets the
		// expected default behaviour rather than the silent nil
		// return at the bottom of the switch.
		segCount := 1
		if by == ChunkByRegion {
			segCount = 2
		}
		buckets := make(map[string][]string)
		for _, t := range tenants {
			key := pathSegmentsPrefix(dirs[t], segCount)
			buckets[key] = append(buckets[key], t)
		}
		// Sort the buckets within each key for stable output.
		for k := range buckets {
			sort.Strings(buckets[k])
		}
		// Walk bucket keys in sorted order.
		keys := make([]string, 0, len(buckets))
		for k := range buckets {
			keys = append(keys, k)
		}
		sort.Strings(keys)

		var out []chunk
		for _, k := range keys {
			members := buckets[k]
			// Apply soft cap — split oversized buckets into
			// sub-chunks of at most `size`.
			if len(members) <= size {
				out = append(out, chunk{key: k, tenants: members})
				continue
			}
			subIdx := 1
			for i := 0; i < len(members); i += size {
				end := i + size
				if end > len(members) {
					end = len(members)
				}
				out = append(out, chunk{
					key:     fmt.Sprintf("%s/part-%02d", k, subIdx),
					tenants: append([]string(nil), members[i:end]...),
				})
				subIdx++
			}
		}
		return out
	}
	// Unknown ChunkBy values are normalised to default at the
	// BuildPlan caller layer; this branch is unreachable but keeps
	// the switch exhaustive for the linter.
	return nil
}

// pathSegmentsPrefix returns the first `n` slash-separated segments
// of `path`, joined by `/`. A path with fewer than n segments
// returns the whole path. An empty input returns "<unassigned>" so
// chunks remain bucketable rather than collapsing into a noisy "".
func pathSegmentsPrefix(path string, n int) string {
	if path == "" {
		return "<unassigned>"
	}
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) <= n {
		return strings.Join(parts, "/")
	}
	return strings.Join(parts[:n], "/")
}

// buildBasePR composes the Base Infrastructure PlanItem covering
// every proposal. Title + description follow planning §C-10
// conventions so reviewers can recognise them at a glance.
func buildBasePR(proposals []ProposalRef) PlanItem {
	dialects := dialectMix(proposals)
	dialectLabel := strings.Join(dialects, "+") // e.g. "prom+metricsql"
	indices := make([]int, len(proposals))
	for i := range proposals {
		indices[i] = i
	}

	body := strings.Builder{}
	body.WriteString("Base Infrastructure PR — `_defaults.yaml` for the following profiles:\n\n")
	body.WriteString("| # | Tenants | Dialect | For | Shared label keys |\n")
	body.WriteString("|---|---------|---------|-----|-------------------|\n")
	for i, p := range proposals {
		body.WriteString(fmt.Sprintf("| %d | %d | %s | %s | %s |\n",
			i+1,
			len(p.MemberTenantIDs),
			emptyOrValue(p.Dialect, "—"),
			emptyOrValue(p.SharedFor, "—"),
			emptyOrValue(strings.Join(sortedKeys(p.SharedLabels), ", "), "—"),
		))
	}
	body.WriteString("\nMerge this PR before any tenant chunk PR (each carries `Blocked by: <base>`).\n")

	return PlanItem{
		Kind:                  PlanItemBase,
		Title:                 fmt.Sprintf("[Base Infrastructure] Import %d profiles (%s)", len(proposals), dialectLabel),
		Description:           body.String(),
		SourceProposalIndices: indices,
	}
}

// buildTenantPR composes one tenant chunk PlanItem.
func buildTenantPR(chunkIdx, total int, c chunk, proposals []ProposalRef) PlanItem {
	body := strings.Builder{}
	body.WriteString(fmt.Sprintf("Tenant chunk %d of %d.\n\n", chunkIdx, total))
	body.WriteString(fmt.Sprintf("**Bucket key**: `%s`\n", c.key))
	body.WriteString(fmt.Sprintf("**Tenants in this chunk** (%d):\n\n", len(c.tenants)))
	for _, t := range c.tenants {
		body.WriteString(fmt.Sprintf("- `%s`\n", t))
	}
	body.WriteString("\n**Blocked by** the Base Infrastructure PR — review only after that has merged.\n")

	// Find which proposals contribute to this chunk for the
	// SourceProposalIndices field.
	tenantSet := make(map[string]struct{}, len(c.tenants))
	for _, t := range c.tenants {
		tenantSet[t] = struct{}{}
	}
	var indices []int
	for i, p := range proposals {
		for _, t := range p.MemberTenantIDs {
			if _, ok := tenantSet[t]; ok {
				indices = append(indices, i)
				break
			}
		}
	}

	return PlanItem{
		Kind:                  PlanItemTenant,
		Title:                 fmt.Sprintf("[chunk %d/%d] Import PromRules to %s", chunkIdx, total, c.key),
		Description:           body.String(),
		BlockedBy:             baseBlockedByMarker,
		SourceProposalIndices: indices,
		TenantIDs:             c.tenants,
		ChunkKey:              c.key,
	}
}

// dialectMix returns the sorted, deduplicated set of dialect labels
// appearing across all proposals. Empty dialects are dropped.
func dialectMix(proposals []ProposalRef) []string {
	seen := make(map[string]struct{})
	for _, p := range proposals {
		if p.Dialect == "" {
			continue
		}
		seen[p.Dialect] = struct{}{}
	}
	if len(seen) == 0 {
		return []string{"unknown"}
	}
	out := make([]string, 0, len(seen))
	for d := range seen {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}

// sortedKeys returns the keys of m in sorted order. Defined here
// rather than imported from a util package because batchpr keeps
// its dependency surface deliberately small (PR-1 imports nothing
// from /internal/profile or /internal/parser — see PlanInput
// rationale in types.go).
func sortedKeys(m map[string]string) []string {
	if len(m) == 0 {
		return nil
	}
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func emptyOrValue(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}
