package rbac

// Offline scope-gap listing for `tenant-api config check --kind scope`
// (ADR-027 §7, §9 item 7): for each scope axis, the rules that restrict it ×
// the tenants they apply to that carry no label on it. Every scope axis is
// enforced, so each such pair is a tenant the rule stops granting — the list
// an operator backfills labels from before upgrading.
//
// The judgement lives here, beside the evaluation it predicts, not in the
// command: which tenants a rule applies to is tenantMatches (the one tenant
// pattern matcher every plane uses), and "unlabeled" is the same test the
// scope evaluation applies —
//
//   - environment / domain: the rule's allow-list is non-empty and the
//     tenant's value is "" (an empty allow-list places no restriction);
//   - org: the rule has an org-scope and the tenant has no organization in
//     _tenant_orgs.yaml (absent and listed-with-an-empty-list alike).
//
// It does not judge whether a LABELED tenant passes the restriction: a label
// outside every rule's allow-list is a policy choice, not a missing label.

import "sort"

// ScopeAxis names one scope axis in a ScopeGap.
type ScopeAxis string

const (
	ScopeAxisEnvironment ScopeAxis = "environment"
	ScopeAxisDomain      ScopeAxis = "domain"
	ScopeAxisOrg         ScopeAxis = "org"
)

// scopeAxisOrder is the order ScopeGaps are sorted in within one rule.
var scopeAxisOrder = map[ScopeAxis]int{ScopeAxisEnvironment: 0, ScopeAxisDomain: 1, ScopeAxisOrg: 2}

// TenantLabels is what the scope axes read of one tenant.
type TenantLabels struct {
	ID string
	// Environment and Domain are the tenant's resolved metadata values, as
	// the list plane reads them ("" = unlabeled).
	Environment string
	Domain      string
	// MetadataUnknown: the tenant's metadata could not be read (a broken
	// tenant file, an unreadable root platform layer, an id the file does
	// not declare). Its empty Environment/Domain are unknown, not unlabeled,
	// so the metadata axes are not judged for it; the caller reports it
	// separately. The org axis does not depend on the tenant file and is
	// judged as usual.
	MetadataUnknown bool
	// Orgs is the tenant's organization list from _tenant_orgs.yaml.
	Orgs []string
}

// ScopeGap is one rule × one tenant the rule applies to that is unlabeled on
// an axis the rule restricts.
type ScopeGap struct {
	Axis ScopeAxis `json:"axis"`
	// Rule is the rule's name; RuleIndex its position in groups: (names
	// need not be unique).
	Rule      string `json:"rule"`
	RuleIndex int    `json:"rule_index"`
	Tenant    string `json:"tenant"`
}

// UnlabeledScopeGaps returns every rule × tenant × axis where the rule
// applies to the tenant (its tenants: patterns match the id), restricts the
// axis, and the tenant carries no label on it. The result is sorted by rule
// index, then axis (environment, domain, org), then tenant id, so the same
// config always yields the same list. A nil config yields none.
func (c *RBACConfig) UnlabeledScopeGaps(tenants []TenantLabels) []ScopeGap {
	if c == nil {
		return nil
	}
	var gaps []ScopeGap
	for i := range c.Groups {
		rule := &c.Groups[i]
		for _, t := range tenants {
			if !tenantMatches(rule.Tenants, t.ID) {
				continue
			}
			add := func(axis ScopeAxis) {
				gaps = append(gaps, ScopeGap{Axis: axis, Rule: rule.Name, RuleIndex: i, Tenant: t.ID})
			}
			if !t.MetadataUnknown {
				if len(rule.Environments) > 0 && t.Environment == "" {
					add(ScopeAxisEnvironment)
				}
				if len(rule.Domains) > 0 && t.Domain == "" {
					add(ScopeAxisDomain)
				}
			}
			if rule.OrgScope != "" && len(t.Orgs) == 0 {
				add(ScopeAxisOrg)
			}
		}
	}
	sort.SliceStable(gaps, func(a, b int) bool {
		ga, gb := gaps[a], gaps[b]
		if ga.RuleIndex != gb.RuleIndex {
			return ga.RuleIndex < gb.RuleIndex
		}
		if ga.Axis != gb.Axis {
			return scopeAxisOrder[ga.Axis] < scopeAxisOrder[gb.Axis]
		}
		return ga.Tenant < gb.Tenant
	})
	return gaps
}
