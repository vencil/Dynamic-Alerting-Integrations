package batchpr

// PR-2 — branch name derivation.
//
// Branch names must be deterministic from the Plan + PlanItem so
// re-running Apply() against the same input produces the same
// branch names — that's the foundation for Apply()'s idempotency
// (a re-run sees the existing branch + open PR and skips the work).
//
// Naming scheme:
//
//   <prefix>/base-<plan-hash>                       (Base PR)
//   <prefix>/tenant-<safe-chunk-key>-<plan-hash>    (tenant chunk PR)
//
// where:
//   - `<prefix>` is ApplyInput.BranchPrefix, defaulting to
//     `da-tools/c10` (no trailing slash).
//   - `<plan-hash>` is the first 8 hex chars of SHA-256 over a
//     canonical JSON encoding of Plan.Items[*].SourceProposalIndices
//     plus chunk-keys + tenant-id sets. Two Plans with the same
//     proposal contents produce the same hash; structural drift
//     (re-clustering, scope change) produces a fresh hash.
//   - `<safe-chunk-key>` is the chunk key with `/` and other
//     non-branch-safe chars replaced with `-`. Domain-default
//     chunks like `domain/region` flatten to `domain-region`.
//
// Why a hash and not a version number / timestamp:
//   - Version number requires central state. The Plan doesn't carry
//     one and we don't want a hidden counter file.
//   - Timestamp would defeat idempotency (re-run picks new branch
//     name → new PR → duplication).
//   - Content hash gives the right semantics: same plan → same
//     branches → idempotent apply; different plan → different
//     branches → fresh PRs. Customers who legitimately want
//     fresh PRs after editing the Plan get them automatically.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
)

// defaultBranchPrefix is the prefix used when ApplyInput.BranchPrefix
// is empty. Convention: `da-tools/c10` so customer repos can grep
// branches that batchpr-apply created.
const defaultBranchPrefix = "da-tools/c10"

// computePlanHash returns an 8-hex-char fingerprint of the Plan.
//
// Hash key: every PlanItem's (Kind, SourceProposalIndices, ChunkKey,
// TenantIDs). Title / Description are deliberately excluded because
// they're derived (rendering changes shouldn't change the branch
// name; only structural changes should).
func computePlanHash(plan *Plan) string {
	if plan == nil || len(plan.Items) == 0 {
		return "00000000"
	}
	key := make([]map[string]any, 0, len(plan.Items))
	for _, it := range plan.Items {
		key = append(key, map[string]any{
			"kind":             string(it.Kind),
			"source_proposals": it.SourceProposalIndices,
			"chunk_key":        it.ChunkKey,
			"tenant_ids":       it.TenantIDs,
		})
	}
	// Use json.Marshal — yaml.v3 does NOT guarantee map-key
	// ordering for map[string]any, but encoding/json's encoder
	// sorts struct keys (and we only nest concrete strings + sorted
	// slices, so no map-iter randomness slips in).
	b, err := json.Marshal(key)
	if err != nil {
		// Encoding can't fail for the shape above (no maps with
		// non-string keys, no chans / funcs). Defensive fallback
		// keeps the code total.
		return "ffffffff"
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])[:8]
}

// branchNameFor derives the deterministic branch name for one
// PlanItem. prefix should be the user-supplied (or defaulted)
// BranchPrefix WITHOUT trailing slash; planHash is the 8-char
// fingerprint from computePlanHash.
//
// Defensive normalisation:
//   - Trailing `/` stripped (so `da-tools/c10/` and `da-tools/c10`
//     produce identical names).
//   - Leading `-` stripped to defend `git checkout -B <name>`
//     against a prefix that would parse as a flag (e.g. a
//     hostile / typo'd `--force/...` prefix). PR-2 self-review
//     caught this attack surface; sanitisation is cheap.
//   - Empty result falls back to defaultBranchPrefix.
func branchNameFor(prefix, planHash string, item PlanItem) string {
	prefix = strings.TrimRight(prefix, "/")
	prefix = strings.TrimLeft(prefix, "-")
	if prefix == "" {
		prefix = defaultBranchPrefix
	}
	switch item.Kind {
	case PlanItemBase:
		return fmt.Sprintf("%s/base-%s", prefix, planHash)
	case PlanItemTenant:
		safe := safeBranchSegment(item.ChunkKey)
		if safe == "" {
			safe = "unassigned"
		}
		return fmt.Sprintf("%s/tenant-%s-%s", prefix, safe, planHash)
	default:
		return fmt.Sprintf("%s/unknown-%s", prefix, planHash)
	}
}

// safeBranchSegment normalises a chunk key into a git-branch-safe
// segment.
//
// Git refnames can't contain `..`, `~`, `^`, `:`, `?`, `*`, `[`,
// `\`, sequences of `/`, lone `@{`, etc. (see git-check-ref-format).
// PR-2 conservatively maps anything outside `[A-Za-z0-9-]` to `-`
// and collapses repeated dashes. ASCII-only output keeps shell-out
// friendly (no quoting surprises).
func safeBranchSegment(s string) string {
	if s == "" {
		return ""
	}
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r >= 'A' && r <= 'Z',
			r >= 'a' && r <= 'z',
			r >= '0' && r <= '9',
			r == '-':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	out := b.String()
	for strings.Contains(out, "--") {
		out = strings.ReplaceAll(out, "--", "-")
	}
	out = strings.Trim(out, "-")
	return out
}
