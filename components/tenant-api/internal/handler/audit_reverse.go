package handler

// Reverse access-report endpoint (ADR-027 / LD-6 P6):
//
//	GET /api/v1/audit/tenants/{id}/access-report?include=org_values&view=redacted
//
// Answers "who can access tenant {id}, via which rule, under which conditions"
// by calling the rbac package's ReverseAccessReport core — the handler holds
// ZERO enumeration or match logic; it only authorizes, resolves the tenant's
// org inputs, and serializes.
//
// Mounted under the /audit top-level segment, deliberately OUTSIDE the
// /tenants/{id} tree, so it does not inherit the read-by-id org middleware:
// the route middleware only authenticates (rbac.Middleware(PermRead, nil) —
// the federation-policy precedent), and the real authorization bar lives HERE,
// tightened to PlatformAdminNonOrgScoped (owner decision §0.1): an org-scoped
// wildcard admin must NOT be able to read the platform-wide access map.
//
// Handler check order (invariant, round-1 C8) — the bar runs FIRST and its
// 403 is CONSTANT (status + body byte-identical whether {id} is valid,
// malformed, or nonexistent), so an unauthorized caller cannot use the
// endpoint as a tenant-enumeration oracle:
//
//	1. PlatformAdminNonOrgScoped(bar)  → constant 403
//	2. ValidateTenantID                → 400
//	3. query-param validation          → 400
//	4. OrgsForTenant (nil-receiver-safe; a nonexistent tenant is NOT a 404 —
//	   an auditor must be able to ask about an already-offboarded tenant)
//	5. build report → (view=redacted projection) → meta-audit INFO log →
//	   writeJSON
//
// The meta-audit log (round-1 C11) records WHO pulled WHOSE access map under
// WHICH config snapshot. It flows into the platform log-aggregation pipeline,
// whose reader audience is wider than platform admins — so it records the
// caller email (existing middleware log convention) and config hashes, but
// NEVER claim values or org values.

import (
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/rbac"
)

// auditReverseForbiddenMsg is the CONSTANT 403 message for callers failing the
// platform-admin bar. It must not vary with the requested tenant id (or its
// validity/existence) — the byte-identical-403 test pins this.
const auditReverseForbiddenMsg = "platform admin (non-org-scoped) permission required for access reports"

// GetTenantAccessReport handles GET /api/v1/audit/tenants/{id}/access-report.
//
// Query parameters:
//   - include=org_values — opt-in expansion of the org identifiers DERIVED
//     from _tenant_orgs (tenant.orgs + each grant's passing_org_values); those
//     are absent from the default view, which carries the org_status enum and
//     org-gate outcomes. Org values pinned VERBATIM in a rule's match.claims
//     still appear in the default full view via who.claims_all_of (the LOCKED
//     transcribe-verbatim WHO semantics — this opt-in does not gate them).
//   - view=redacted|full (default full) — redacted applies the rbac-core
//     allowlist-rebuild projection (RedactReverseReport): rule/group names,
//     claim keys+values and org values are stripped, replaced by counts and
//     pattern/shape enums. Caller is already a platform admin, so full is the
//     sensible default; the sensitive axis (org values) has its own opt-in.
//
// @Summary     Reverse access report: who can access this tenant (audit-only)
// @Description Enumerates the live RBAC config with the SAME predicates the
// @Description forward gates run and reports, per rule, who is granted what on
// @Description tenant {id} under which org-gate conditions (both shadow and
// @Description enforce outcomes). Audit-only: authorization decisions remain
// @Description with the forward gates. Requires a NON-org-scoped platform
// @Description admin grant; all other callers receive a constant 403
// @Description regardless of {id} (no tenant-enumeration oracle). Note the
// @Description redacted view cannot remove the org-membership inference carried
// @Description by a grant entry's mere existence: for a value-pinned org rule,
// @Description the grant entry itself is weakly identifying.
// @Tags        audit
// @Produce     json
// @Param       id      path  string true  "Tenant ID"
// @Param       include query string false "Opt-in expansions"           Enums(org_values)
// @Param       view    query string false "Report projection"           Enums(full, redacted) default(full)
// @Success     200 {object} rbac.ReverseReport
// @Failure     400 {object} map[string]string
// @Failure     401 {object} map[string]string
// @Failure     403 {object} map[string]string
// @Router      /api/v1/audit/tenants/{id}/access-report [get]
func GetTenantAccessReport(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// 1. LOCKED bar, before anything id-derived: constant 403 for every
		// non-(non-org-scoped-platform-admin) caller, byte-identical no matter
		// what {id} carries.
		if !d.RBAC.PlatformAdminNonOrgScoped(rbac.RequestPrincipal(r)) {
			WriteJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden, auditReverseForbiddenMsg)
			return
		}

		// 2. Tenant id shape (access.go precedent). Existence is deliberately
		// NOT checked — no 404 masking; an audit must be able to ask about an
		// offboarded tenant (the report then shows not_onboarded).
		id := chi.URLParam(r, "id")
		if err := ValidateTenantID(id); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// 3. Query params — strict: an unknown value is a 400, never silently
		// ignored. A typo'd ?view=redcated silently answering with the FULL
		// report would be a fail-open projection choice.
		includeOrgValues := false
		switch r.URL.Query().Get("include") {
		case "":
		case "org_values":
			includeOrgValues = true
		default:
			WriteJSONError(w, r, http.StatusBadRequest, "unsupported include value: only org_values is recognized")
			return
		}
		redacted := false
		switch r.URL.Query().Get("view") {
		case "", "full":
		case "redacted":
			redacted = true
		default:
			WriteJSONError(w, r, http.StatusBadRequest, "unsupported view value: full or redacted")
			return
		}

		// 4. Org inputs, injected here because rbac does not import tenantorg
		// (SetOrgResolver seam convention). OrgsForTenant is nil-receiver-safe;
		// LastHash is not, so the hash keeps its own guard (an unwired manager
		// reports "unanchored" via the empty string).
		orgs, known := d.TenantOrg.OrgsForTenant(id)
		tenantOrgsHash := ""
		if d.TenantOrg != nil {
			tenantOrgsHash = d.TenantOrg.LastHash()
		}

		// 5. Build → project → meta-audit → serialize. DevBypassActive is the
		// live ADR-022 runtime gauge (this package's atomic, set at startup),
		// injected per call so the report's completeness section stays
		// runtime-fresh (round-1 C7).
		rep := d.RBAC.ReverseAccessReport(id, orgs, known, tenantOrgsHash, rbac.ReverseReportOptions{
			IncludeOrgValues: includeOrgValues,
			DevBypassActive:  devBypassActive.Load(),
		})
		if redacted {
			rep = rbac.RedactReverseReport(rep)
		}

		// Meta-audit (round-1 C11): who pulled whose access map, anchored to
		// the exact config snapshot. No claim values, no org values — this log
		// line travels further than the report itself.
		slog.Info("reverse access report served",
			"caller", rbac.RequestEmail(r),
			"tenant", id,
			"rbac_sha256", rep.ConfigAnchor.RBACSHA256.Value,
			"tenant_orgs_sha256", rep.ConfigAnchor.TenantOrgsSHA256.Value,
			"view", map[bool]string{true: "redacted", false: "full"}[redacted],
			"include_org_values", includeOrgValues,
			"verdict", rep.Verdict,
			"grants", len(rep.Grants),
		)

		writeJSON(w, http.StatusOK, rep)
	}
}
