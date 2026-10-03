package routingpolicy

import "github.com/vencil/threshold-exporter/pkg/tenantid"

// IsValidTenantID reports whether id is a tenant id the routing plane
// renders (#2341 R8). The route generator refuses a tree that declares any
// other id, in every mode (ADR-035 D3); an empty one once became
// `tenant=""`, which Alertmanager matches on alerts that carry NO tenant
// label, so platform alerts were routed to `tenant-`.
//
// The rule is ADR-035's single source (pkg/tenantid, generated from the
// tenant-config schema's definitions.tenantId): a DNS-1123 label. Python
// twin: _lib_validation.is_valid_tenant_id; both are pinned by the
// `tenant_ids` table of tests/shared/routing_policy_parity_matrix.json.
func IsValidTenantID(id string) bool {
	return tenantid.Valid(id)
}
