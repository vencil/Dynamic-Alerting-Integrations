package routingpolicy

import "regexp"

// tenantIDRe is the Go copy of _lib_validation._TENANT_ID_RE (#2341 R8).
var tenantIDRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// IsValidTenantID reports whether id is a tenant id the routing plane
// renders (#2341 R8). The route generator renders nothing for any other id —
// an empty one became `tenant=""`, which Alertmanager matches on alerts that
// carry NO tenant label, so platform alerts were routed to `tenant-`.
//
// ⛔ Not a new rule: the union of what the repo's three existing tenant-name
// rules accept (scripts/tools/ops/operator_generate.py and
// migrate_to_operator.py: a DNS-1123 label; alert_quality.py:
// `^[a-zA-Z0-9_-]+$`); the first set is inside the second. An id all three
// refuse (the empty one included) is refused; one any of them accepts
// (`UPPER`) is not, so a later single source of truth can only tighten
// this. Python twin: _lib_validation.is_valid_tenant_id; both are pinned by
// the `tenant_ids` table of tests/shared/routing_policy_parity_matrix.json.
func IsValidTenantID(id string) bool {
	return tenantIDRe.MatchString(id)
}
