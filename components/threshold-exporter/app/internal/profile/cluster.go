package profile

// Cluster engine — group ParsedRules by signature, emit
// ExtractionProposal per cluster.
//
// PR-1 algorithm (deterministic, two passes when fuzzy enabled):
//
//   PASS 1 (strict — always runs):
//   1. For each input rule, compute a `signature` = the normalised
//      expression + the rule's `for:` + the rule's dialect.
//      Rules with empty expr are dropped to Unclustered (no usable
//      signature).
//   2. Bucket rules by signature.
//   3. For each bucket whose size ≥ MinClusterSize, build an
//      ExtractionProposal with Confidence=high:
//        - SharedLabels  = labels with identical values across all
//                          members of the bucket.
//        - VaryingLabels = label keys that appear in any member but
//                          have different values (or are absent in
//                          at least one member).
//        - SharedExprTemplate = the normalised expression (already
//                          a template after numeric/string strip).
//        - SharedFor    = the bucket's `for:` (always identical
//                          within a signature bucket).
//        - EstimatedYAMLLineSavings = (N - 1) × shared_field_lines,
//                          where shared_field_lines counts SharedFor
//                          (1 line) + each SharedLabel (1 line).
//   4. Buckets below MinClusterSize feed into Pass 2 if EnableFuzzy.
//
//   PASS 2 (fuzzy — runs only when ClusterOptions.EnableFuzzy=true,
//   PR-5 addition):
//   5. Among the strict-pass residue (rules in sub-MinClusterSize
//      buckets), recompute a *fuzzy* signature using
//      WithCanonicalDurations() — equivalent durations like `[5m]`
//      and `[300s]` collapse to the same key.
//   6. Re-bucket the residue by the fuzzy signature.
//   7. For each fuzzy bucket whose size ≥ MinClusterSize, build a
//      ConfidenceMedium proposal with the same shape as Pass 1,
//      annotated with `Reason: "duration-equivalent ..."`.
//   8. Anything still below MinClusterSize after Pass 2 → Unclustered.
//
// The signature deliberately includes Dialect — a `prom`-dialect
// rule and a `metricsql`-dialect rule with the same normalised expr
// MUST NOT cluster, because their portability properties differ.
// Pass 2 PRESERVES this boundary; fuzzy never crosses dialects.
//
// Why the strict pass always wins: a rule that fits both strict and
// fuzzy buckets stays in its strict (high-confidence) cluster.
// Fuzzy only operates on rules the strict pass left as singletons or
// sub-MinClusterSize buckets — protecting the high-confidence
// proposals from contamination by lower-evidence merges.
//
// Determinism guarantees:
//   - Bucket iteration is sorted by signature (stable) in both passes.
//   - Within a bucket, MemberRuleIDs is sorted (stable).
//   - VaryingLabelKeys is sorted (stable).
//   - Proposal output order is sorted by MemberRuleIDs[0] (stable),
//     mixing high and medium proposals together by member-id key
//     (NOT by confidence tier — keeps run-to-run output identical
//     even if one rule's tier changes between fuzzy on/off).
//   - Two runs over the same input + same options produce
//     byte-identical JSON. Tested.

import (
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// BuildProposals runs the cluster engine over a slice of ParsedRule
// records and returns the ProposalSet.
//
// Inputs come from a parser.ParseResult.Rules slice; the function
// does not mutate them.
//
// Errors:
//   - len(rules) == 0  → fmt error (caller almost always wants to
//     treat empty input as a bug rather than an empty proposal set).
//
// Otherwise BuildProposals never errors — even a corpus where
// nothing clusters returns a valid (empty Proposals) ProposalSet.
func BuildProposals(rules []parser.ParsedRule, opts ClusterOptions) (*ProposalSet, error) {
	if len(rules) == 0 {
		return nil, fmt.Errorf("profile: no rules to cluster")
	}
	if opts.MinClusterSize < 2 {
		opts.MinClusterSize = 2
	}

	// Drop ambiguous rules upfront if asked. Otherwise they'll fall
	// through to Unclustered (their signature is built from the
	// normalised expr, which is empty → bucket key empty → caught
	// by the empty-signature drop below).
	work := make([]parser.ParsedRule, 0, len(rules))
	for _, r := range rules {
		if opts.SkipAmbiguous && r.Dialect == parser.DialectAmbiguous {
			continue
		}
		work = append(work, r)
	}

	out := &ProposalSet{
		Stats: ProposalStats{
			TotalRulesIn: len(rules),
		},
	}

	// PASS 1: strict signature clustering. Returns the strict-cluster
	// proposals AND a residue slice of rules that fell below
	// MinClusterSize plus the ambiguous-no-signature stragglers.
	strictProposals, residue, ambigUnclustered := strictPassClusters(work, opts)
	out.Proposals = append(out.Proposals, strictProposals...)
	out.Unclustered = append(out.Unclustered, ambigUnclustered...)

	// PASS 2: fuzzy signature clustering on the strict residue (PR-5).
	// Off by default — preserves PR-1 behaviour for callers that don't
	// opt in. The fuzzy pass uses duration-canonicalised signatures so
	// `rate(foo[5m])` and `rate(foo[300s])` collapse together.
	if opts.EnableFuzzy {
		fuzzyProposals, fuzzyResidue := fuzzyPassClusters(residue, opts)
		out.Proposals = append(out.Proposals, fuzzyProposals...)
		residue = fuzzyResidue
	}

	// Anything still in residue is genuinely Unclustered — neither
	// pass found enough peers.
	for _, r := range residue {
		out.Unclustered = append(out.Unclustered, r.SourceRuleID)
	}

	// Final sort: proposals by first member id (NOT by confidence
	// tier — keeps run-to-run order identical when one rule's tier
	// changes); unclustered alphabetical. Both stable.
	sort.Slice(out.Proposals, func(i, j int) bool {
		return out.Proposals[i].MemberRuleIDs[0] < out.Proposals[j].MemberRuleIDs[0]
	})
	sort.Strings(out.Unclustered)

	out.Stats.ProposalsEmitted = len(out.Proposals)
	for _, p := range out.Proposals {
		out.Stats.RulesClustered += len(p.MemberRuleIDs)
		out.Stats.TotalLineSavings += p.EstimatedYAMLLineSavings
	}
	out.Stats.RulesUnclustered = len(out.Unclustered)
	return out, nil
}

// strictPassClusters runs the original strict-signature clustering
// (PR-1 behaviour) and returns:
//   - proposals: ConfidenceHigh proposals, one per bucket ≥
//     MinClusterSize.
//   - residue: rules from sub-MinClusterSize buckets, available for
//     the optional fuzzy pass. Sorted by SourceRuleID for stable
//     downstream behaviour.
//   - ambigUnclustered: SourceRuleIDs of rules with no usable
//     signature (DialectAmbiguous + no normalised expr). These
//     skip the fuzzy pass entirely — fuzzy can't help an empty
//     signature.
func strictPassClusters(work []parser.ParsedRule, opts ClusterOptions) (proposals []ExtractionProposal, residue []parser.ParsedRule, ambigUnclustered []string) {
	type bucket struct {
		members []parser.ParsedRule
	}
	buckets := make(map[string]*bucket)

	for _, r := range work {
		sig := signatureFor(r)
		if sig == "" {
			ambigUnclustered = append(ambigUnclustered, r.SourceRuleID)
			continue
		}
		b, ok := buckets[sig]
		if !ok {
			b = &bucket{}
			buckets[sig] = b
		}
		b.members = append(b.members, r)
	}

	// Deterministic walk by signature.
	signatures := make([]string, 0, len(buckets))
	for s := range buckets {
		signatures = append(signatures, s)
	}
	sort.Strings(signatures)

	for _, sig := range signatures {
		b := buckets[sig]
		if len(b.members) < opts.MinClusterSize {
			residue = append(residue, b.members...)
			continue
		}
		proposals = append(proposals, buildProposalFromBucket(b.members, ConfidenceHigh, ""))
	}

	// Stable residue order — caller (fuzzy pass) iterates it.
	sort.Slice(residue, func(i, j int) bool {
		return residue[i].SourceRuleID < residue[j].SourceRuleID
	})
	return proposals, residue, ambigUnclustered
}

// fuzzyPassClusters runs duration-canonicalised clustering on the
// strict-pass residue. Returns:
//   - proposals: ConfidenceMedium proposals (one per fuzzy bucket
//     ≥ MinClusterSize).
//   - leftover: rules that didn't reach MinClusterSize even under
//     the looser key. Caller adds their SourceRuleIDs to Unclustered.
//
// PR-5 design notes:
//   - Dialect remains in the fuzzy signature: cross-dialect merges
//     are an explicit non-goal (vendor-lock-in risk).
//   - `for:` remains in the fuzzy signature: rules that differ only
//     in `for:` duration are usually intentional alert-tier
//     separations (warning at 5m, critical at 15m). Fuzzy doesn't
//     auto-merge them.
//   - The ONLY axis fuzzy loosens is the **range-vector duration**
//     INSIDE `[]` of the expression itself (e.g. `rate(foo[5m])`
//     vs `rate(foo[300s])`).
func fuzzyPassClusters(residue []parser.ParsedRule, opts ClusterOptions) (proposals []ExtractionProposal, leftover []parser.ParsedRule) {
	type bucket struct {
		members []parser.ParsedRule
	}
	buckets := make(map[string]*bucket)

	for _, r := range residue {
		sig := signatureForFuzzy(r)
		if sig == "" {
			leftover = append(leftover, r)
			continue
		}
		b, ok := buckets[sig]
		if !ok {
			b = &bucket{}
			buckets[sig] = b
		}
		b.members = append(b.members, r)
	}

	signatures := make([]string, 0, len(buckets))
	for s := range buckets {
		signatures = append(signatures, s)
	}
	sort.Strings(signatures)

	for _, sig := range signatures {
		b := buckets[sig]
		if len(b.members) < opts.MinClusterSize {
			leftover = append(leftover, b.members...)
			continue
		}
		// Detect what was loosened to inform the human-readable Reason.
		// PR-5 only loosens duration tokens; we surface that
		// explicitly so reviewers know why this is medium-confidence.
		reason := fuzzyReason(b.members)
		proposals = append(proposals, buildProposalFromBucket(b.members, ConfidenceMedium, reason))
	}

	// Stable leftover order.
	sort.Slice(leftover, func(i, j int) bool {
		return leftover[i].SourceRuleID < leftover[j].SourceRuleID
	})
	return proposals, leftover
}

// fuzzyReason composes a human-readable explanation for a
// medium-confidence cluster. PR-5 ships only the duration-equivalence
// case; future fuzzy axes extend the switch with named diagnostics.
//
// We DON'T list the literal duration tokens — for a 50-rule cluster
// that would be a 50-line cell in the rendered Markdown. The summary
// just notes how many distinct raw forms collapsed into one.
func fuzzyReason(members []parser.ParsedRule) string {
	rawDurations := make(map[string]struct{})
	for _, m := range members {
		// Track raw durations encountered so the reviewer sees the
		// breadth of the merge (e.g. "5 distinct raw range durations
		// canonicalised to 1").
		matches := rangeDurationToken.FindAllString(m.Expr, -1)
		for _, d := range matches {
			rawDurations[d] = struct{}{}
		}
	}
	dialect := string(members[0].Dialect)
	forVal := members[0].For
	if len(rawDurations) > 1 {
		return fmt.Sprintf(
			"%d rules cluster under duration-equivalence (%d distinct raw range durations collapsed); dialect=%s, for=%q",
			len(members), len(rawDurations), dialect, forVal)
	}
	return fmt.Sprintf(
		"%d rules cluster under fuzzy signature; dialect=%s, for=%q",
		len(members), dialect, forVal)
}

// signatureFor builds the bucket key for one rule. The key encodes
// every dimension that MUST match across cluster members:
//   - normalised expression (strict — no duration canonicalisation)
//   - `for:` duration
//   - dialect
//
// Returns "" when the rule has no usable expression (empty or
// ambiguous-with-no-normalisable-form), signalling "send to
// Unclustered".
func signatureFor(r parser.ParsedRule) string {
	return buildSignature(r, false)
}

// signatureForFuzzy builds the *fuzzy* bucket key for one rule —
// same axes as the strict signature, but with duration canonicalisation
// applied to the expression. `[5m]` and `[300s]` collapse to the same
// signature here, while the strict variant keeps them apart.
//
// Returns "" under the same conditions as the strict variant.
func signatureForFuzzy(r parser.ParsedRule) string {
	return buildSignature(r, true)
}

// buildSignature is the shared helper. The bool isolates the only
// degree of freedom between strict and fuzzy keys (which prevents
// drift if we add more axes later — e.g. a "loose for:" pass).
func buildSignature(r parser.ParsedRule, fuzzy bool) string {
	var expr string
	if fuzzy {
		expr = normaliseExpr(r.Expr, WithCanonicalDurations())
	} else {
		expr = normaliseExpr(r.Expr)
	}
	if expr == "" {
		return ""
	}
	// Null-byte separator: cannot appear in YAML-derived strings
	// (the parser would have rejected them upstream) so cannot
	// produce accidental signature collisions from punctuation in
	// any of the joined fields.
	return strings.Join([]string{
		"expr=" + expr,
		"for=" + r.For,
		"dialect=" + string(r.Dialect),
	}, "\x00")
}

// buildProposalFromBucket is called once per bucket that meets the
// minimum size; it derives shared/varying labels and computes the
// savings estimate. Confidence + Reason are caller-supplied so the
// strict (PR-1) and fuzzy (PR-5) paths can stamp different values
// on otherwise-identical proposal shapes.
//
// `reason` may be empty; if so, buildProposalFromBucket synthesises
// the PR-1 default text. Fuzzy callers pass a tier-specific Reason
// via fuzzyReason() so reviewers see WHY the cluster is medium-
// confidence in the rendered Markdown.
//
// Precondition: len(members) ≥ 1. Caller checks the size minimum.
func buildProposalFromBucket(members []parser.ParsedRule, confidence Confidence, reason string) ExtractionProposal {
	ids := make([]string, 0, len(members))
	for _, m := range members {
		ids = append(ids, m.SourceRuleID)
	}
	sort.Strings(ids)

	shared, varying := partitionLabels(members)

	// EstimatedYAMLLineSavings: each shared field, written once in
	// `_defaults.yaml`, replaces N copies in the per-tenant files.
	// Net saving per shared field is therefore (N - 1) lines.
	// SharedFor, when present, counts as one shared field.
	sharedFieldLines := len(shared)
	if members[0].For != "" {
		sharedFieldLines++
	}
	savings := (len(members) - 1) * sharedFieldLines

	// SharedExprTemplate uses the STRICT normalisation regardless of
	// pass — fuzzy clustering proves the rules are equivalent under
	// duration canonicalisation, but the displayed template should
	// remain a faithful render of one member's actual expression
	// (the human reviewing the proposal needs to see what real rules
	// look like, not a synthetic placeholder string).
	template := normaliseExpr(members[0].Expr)
	dialect := string(members[0].Dialect)

	if reason == "" {
		reason = fmt.Sprintf(
			"%d rules share the same expression template, dialect=%s, for=%q",
			len(members), dialect, members[0].For)
	}

	return ExtractionProposal{
		MemberRuleIDs:            ids,
		SharedExprTemplate:       template,
		SharedFor:                members[0].For,
		SharedLabels:             shared,
		VaryingLabelKeys:         varying,
		Dialect:                  dialect,
		EstimatedYAMLLineSavings: savings,
		Confidence:               confidence,
		Reason:                   reason,
	}
}

// partitionLabels splits the union of all member labels into:
//   - shared: keys present in EVERY member with the SAME value
//   - varying: keys present in any member that don't qualify as
//     shared (either value differs, or key is missing in at least
//     one member)
//
// Both outputs are sorted (shared by key, varying by key).
func partitionLabels(members []parser.ParsedRule) (shared map[string]string, varying []string) {
	if len(members) == 0 {
		return nil, nil
	}
	// Build the union of all label keys.
	keyUnion := make(map[string]struct{})
	for _, m := range members {
		for k := range m.Labels {
			keyUnion[k] = struct{}{}
		}
	}

	shared = make(map[string]string)
	varyingSet := make(map[string]struct{})
	for k := range keyUnion {
		first, ok := members[0].Labels[k]
		if !ok {
			// Missing in member 0 → can't be shared.
			varyingSet[k] = struct{}{}
			continue
		}
		consistent := true
		for _, m := range members[1:] {
			if v, present := m.Labels[k]; !present || v != first {
				consistent = false
				break
			}
		}
		if consistent {
			shared[k] = first
		} else {
			varyingSet[k] = struct{}{}
		}
	}

	for k := range varyingSet {
		varying = append(varying, k)
	}
	sort.Strings(varying)
	return shared, varying
}
