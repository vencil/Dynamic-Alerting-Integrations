package rbac

// Legacy groups-slice evaluation signatures — TEST-ONLY since the
// principal-based evaluation core landed (ADR-027 / LD-6 P3).
//
// Why this file exists: Allowed / MetadataAllowed / AccessibleEnvironmentsFor /
// AccessibleDomainsFor replaced these four as the only production API. Moving
// the old names into an _test.go file makes any production call of the old
// shape — from ANY package, including handler and federation — a COMPILE
// error: a stronger, zero-maintenance version of a lint tripwire. The many
// pre-existing rbac test call sites keep compiling unchanged, and because
// each delegate is exactly one line into the production entry point, that
// historical test matrix doubles as the byte-identical oracle for the
// refactor (see allowed_equiv_test.go for the explicit equivalence pin).
//
// ⛔ Do NOT add logic here (no branching, no default-filling, no
// normalisation): a delegate that diverges from the production entry point
// would let tests pass against semantics production never runs.

// HasPermission is the legacy groups-slice form of Allowed.
func (m *Manager) HasPermission(idpGroups []string, tenantID string, want Permission) bool {
	return m.Allowed(&VerifiedPrincipal{Groups: idpGroups}, tenantID, want)
}

// HasMetadataAccess is the legacy groups-slice form of MetadataAllowed.
func (m *Manager) HasMetadataAccess(idpGroups []string, tenantID, environment, domain string) bool {
	return m.MetadataAllowed(&VerifiedPrincipal{Groups: idpGroups}, tenantID, environment, domain)
}

// AccessibleEnvironments is the legacy groups-slice form of AccessibleEnvironmentsFor.
func (m *Manager) AccessibleEnvironments(idpGroups []string) []string {
	return m.AccessibleEnvironmentsFor(&VerifiedPrincipal{Groups: idpGroups})
}

// AccessibleDomains is the legacy groups-slice form of AccessibleDomainsFor.
func (m *Manager) AccessibleDomains(idpGroups []string) []string {
	return m.AccessibleDomainsFor(&VerifiedPrincipal{Groups: idpGroups})
}
