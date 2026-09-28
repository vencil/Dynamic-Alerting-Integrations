package routingpolicy

import "sort"

// Constraint names, as written in `_domain_policy.yaml`.
const (
	ConstraintForbidden = "forbidden_receiver_types"
	ConstraintAllowed   = "allowed_receiver_types"
)

// Policy is the receiver-type part of one domain policy.
type Policy struct {
	Domain                 string
	Tenants                []string
	ForbiddenReceiverTypes []string
	AllowedReceiverTypes   []string
}

// Violation is one receiver type a domain policy rejects.
type Violation struct {
	Domain       string
	Constraint   string // ConstraintForbidden / ConstraintAllowed
	Target       string // Target.Ref: "receiver", "overrides[i]", "routes[i]"
	ReceiverType string
}

// CheckReceiverTypes judges every Target of a resolved routing against the
// receiver-type constraints of each policy that lists tenantID, as
// _grar_validate.check_domain_policies does: domains in name order, targets
// in Targets order, and `forbidden_receiver_types` and
// `allowed_receiver_types` as two INDEPENDENT tests — a type can break both.
// A receiver whose type is not a non-empty string is not judged here (its
// shape is someone else's finding).
//
// Call it only for a tenant whose routing resolved (Resolve ok=true); the
// Python check skips every other tenant.
func CheckReceiverTypes(tenantID string, resolved map[string]any, policies []Policy) []Violation {
	sorted := make([]Policy, len(policies))
	copy(sorted, policies)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].Domain < sorted[j].Domain })

	targets := Targets(resolved)
	var out []Violation
	for _, p := range sorted {
		for _, t := range p.Tenants {
			if t != tenantID {
				continue
			}
			for _, tg := range targets {
				rt := ReceiverType(tg.Receiver)
				if rt == "" {
					continue
				}
				if len(p.ForbiddenReceiverTypes) > 0 && contains(p.ForbiddenReceiverTypes, rt) {
					out = append(out, Violation{Domain: p.Domain, Constraint: ConstraintForbidden, Target: tg.Ref, ReceiverType: rt})
				}
				if len(p.AllowedReceiverTypes) > 0 && !contains(p.AllowedReceiverTypes, rt) {
					out = append(out, Violation{Domain: p.Domain, Constraint: ConstraintAllowed, Target: tg.Ref, ReceiverType: rt})
				}
			}
		}
	}
	return out
}
