package profile

// Cluster engine — group ParsedRules by signature, emit
// ExtractionProposal per cluster.
//
// PR-1 algorithm (deterministic, single pass):
//
//   1. For each input rule, compute a `signature` = the normalised
//      expression + the rule's `for:` + the rule's dialect.
//      Rules with empty expr are dropped to Unclustered (no usable
//      signature).
//   2. Bucket rules by signature.
//   3. For each bucket whose size ≥ MinClusterSize, build an
//      ExtractionProposal:
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
//   4. Buckets below MinClusterSize go to Unclustered.
//
// The signature deliberately includes Dialect — a `prom`-dialect
// rule and an `metricsql`-dialect rule with the same normalised expr
// MUST NOT cluster, because their portability properties differ.
// The C-9 emission step (PR-2) needs to honour that boundary in
// `_defaults.yaml` placement decisions.
//
// Determinism guarantees:
//   - Bucket iteration is sorted by signature (stable).
//   - Within a bucket, MemberRuleIDs is sorted (stable).
//   - VaryingLabelKeys is sorted (stable).
//   - Proposal output order is sorted by MemberRuleIDs[0] (stable).
//   - Two runs over the same input produce byte-identical JSON
//     under encoding/json with sort_keys equivalent. Tested.

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

	// Bucket by signature.
	type bucket struct {
		members []parser.ParsedRule
	}
	buckets := make(map[string]*bucket)
	var unclusteredAmbig []string

	for _, r := range work {
		sig := signatureFor(r)
		if sig == "" {
			// Ambiguous / empty-expr rule survived SkipAmbiguous=false.
			// Surface in Unclustered so the caller sees it.
			unclusteredAmbig = append(unclusteredAmbig, r.SourceRuleID)
			continue
		}
		b, ok := buckets[sig]
		if !ok {
			b = &bucket{}
			buckets[sig] = b
		}
		b.members = append(b.members, r)
	}

	// Walk buckets in deterministic signature order so the eventual
	// proposal output is reproducible across runs.
	signatures := make([]string, 0, len(buckets))
	for s := range buckets {
		signatures = append(signatures, s)
	}
	sort.Strings(signatures)

	out := &ProposalSet{
		Unclustered: unclusteredAmbig,
		Stats: ProposalStats{
			TotalRulesIn: len(rules),
		},
	}

	for _, sig := range signatures {
		b := buckets[sig]
		if len(b.members) < opts.MinClusterSize {
			for _, m := range b.members {
				out.Unclustered = append(out.Unclustered, m.SourceRuleID)
			}
			continue
		}
		prop := buildProposalFromBucket(b.members)
		out.Proposals = append(out.Proposals, prop)
	}

	// Final sort: proposals by first member id; unclustered
	// alphabetical. Both stable.
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

// signatureFor builds the bucket key for one rule. The key encodes
// every dimension that MUST match across cluster members:
//   - normalised expression
//   - `for:` duration
//   - dialect
//
// Returns "" when the rule has no usable expression (empty or
// ambiguous-with-no-normalisable-form), signalling "send to
// Unclustered".
func signatureFor(r parser.ParsedRule) string {
	expr := normaliseExpr(r.Expr)
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
// savings estimate.
//
// Precondition: len(members) ≥ 1. Caller checks the size minimum.
func buildProposalFromBucket(members []parser.ParsedRule) ExtractionProposal {
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

	template := normaliseExpr(members[0].Expr)
	dialect := string(members[0].Dialect)

	return ExtractionProposal{
		MemberRuleIDs:            ids,
		SharedExprTemplate:       template,
		SharedFor:                members[0].For,
		SharedLabels:             shared,
		VaryingLabelKeys:         varying,
		Dialect:                  dialect,
		EstimatedYAMLLineSavings: savings,
		Confidence:               ConfidenceHigh,
		Reason: fmt.Sprintf(
			"%d rules share the same expression template, dialect=%s, for=%q",
			len(members), dialect, members[0].For),
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
