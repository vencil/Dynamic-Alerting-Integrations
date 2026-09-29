package routingpolicy

import "sort"

// Constraint names, as written in `_domain_policy.yaml`.
const (
	ConstraintForbidden = "forbidden_receiver_types"
	ConstraintAllowed   = "allowed_receiver_types"
	// ConstraintRequireCriticalEscalation (#2325): severity=critical alerts
	// must reach an EscalationTypes receiver (CheckCriticalEscalation).
	ConstraintRequireCriticalEscalation = "require_critical_escalation"
)

// Policy is the receiver-type part of one domain policy.
type Policy struct {
	Domain                 string
	Tenants                []string
	ForbiddenReceiverTypes []string
	AllowedReceiverTypes   []string
	// AllowedListNonEmpty is true when `allowed_receiver_types` has any
	// entry, string or not. The Python check restricts on a non-empty set,
	// so a list of only non-string entries (`[true]`, `[~]`, `[1]`) allows
	// NO receiver type; AllowedReceiverTypes keeps the strings only, and
	// would read that list as "unconstrained" on its own.
	AllowedListNonEmpty bool
	// RequireCriticalEscalation is `require_critical_escalation: true`
	// (#2325), booleans read as PyYAML reads them (DecodePyYAML: a plain
	// `yes` / `on` is true). null / false / absent leave it off; any other value is not a
	// boolean, is reported as a Problem, and leaves it off too (the Python
	// check enforces only `is True`).
	RequireCriticalEscalation bool
	// Scope is the directory level (root-relative, slash-separated) of the
	// `_domain_policy.yaml` below the conf.d root this policy came from
	// (#2326); "" for a root policy. LoadTree has already dropped the
	// `tenants:` entries outside that subtree, so Tenants is what applies.
	Scope string
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
				if (p.AllowedListNonEmpty || len(p.AllowedReceiverTypes) > 0) && !contains(p.AllowedReceiverTypes, rt) {
					out = append(out, Violation{Domain: p.Domain, Constraint: ConstraintAllowed, Target: tg.Ref, ReceiverType: rt})
				}
			}
		}
	}
	return out
}
