package handler

// What-if dry-run endpoint (ADR-027 / LD-6 P7):
//
//	POST /api/v1/audit/tenants/{id}/access-report/dry-run
//
// Answers "what would change for tenant {id} if this candidate _rbac.yaml
// were deployed" by evaluating the candidate through the SAME pipeline the
// live config runs — rbac.ParseCandidateConfig (strict parse + validation)
// and rbac.NewCandidate over the SAME reverse-report core — WITHOUT ever
// installing it. The response carries the live baseline report, the
// candidate report, and a structural diff. POST because it carries a config
// body; it computes and commits NOTHING (see the write-route manifest's
// gateWriteOpPlatformAdmin entry).
//
// Authorization is the GET endpoint's LOCKED bar, byte-identical: the same
// PlatformAdminNonOrgScoped check, the same constant 403 (shared
// auditReverseForbiddenMsg), before anything id/query/body-derived — a
// dry-run accepts arbitrary candidate configs, so a lower bar would hand
// non-admins a claim-key/org-value enumeration oracle via the validation
// error echo (candidate_invalid deliberately includes parse detail, which
// names declared claim keys).
//
// Handler check order (mirrors audit_reverse.go; the bar stays first):
//
//	 1. PlatformAdminNonOrgScoped(bar)   → constant 403 (byte-identical P6)
//	 2. ValidateTenantID                 → 400
//	 3. query-param validation (strict)  → 400
//	 4. bounded body read (d.MaxBody)
//	 5. JSON shell / candidate.rbac_yaml presence → 400
//	 6. rbac.ParseCandidateConfig        → 400 CANDIDATE_INVALID + detail
//	 7. rule-count bound (maxCandidateRules) → 400
//	 8. baseline + candidate reports (same org inputs, same options)
//	 9. structural diff (BEFORE any redaction)
//	10. view=redacted → redact both reports + strip diff rule names
//	11. meta-audit INFO log → 200
//
// Diff semantics (owner decisions, P7 spec):
//
//   - structure-then-redact: grants are paired BY RULE NAME on the two FULL
//     reports first; redaction is applied to the already-paired structure.
//     Cross-report indexes are each config's own order, so pairing redacted
//     reports by index would misalign the moment a rule is inserted.
//   - "changed" compares exactly three axes: outcome_shadow, outcome_enforce,
//     unsatisfiable — the flag-agnostic surface. generated_at and flags are
//     excluded by the P6 contract; config_anchor is presented side by side,
//     never diffed. Permission/WHO changes are visible in the two embedded
//     reports, not classified here.
//   - a rename is removed+added by definition (the pairing key is the name).
//   - duplicated rule names (legal in config) make that name's pairing
//     ambiguous: ALL its entries degrade to added+removed and the alignment
//     reports "coarse".

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/rbac"
)

// dryRunSchemaVersion pins the dry-run envelope's JSON schema generation,
// independent of the embedded reports' own schema_version.
const dryRunSchemaVersion = 1

// maxCandidateRules bounds the candidate's rule count. Report generation is
// O(rules × orgs) per report and the endpoint accepts arbitrary configs, so
// an explicit cheap ceiling keeps a pathological candidate from becoming a
// CPU oracle. 1024 is ~10× the largest known deployment's rule count.
const maxCandidateRules = 1024

// Dry-run diff alignment values: exact = every rule name paired
// unambiguously; coarse = at least one duplicated name degraded to
// added+removed.
const (
	DryRunAlignmentExact  = "exact"
	DryRunAlignmentCoarse = "coarse"
)

// DryRunRequest is the POST body envelope. The candidate documents nest
// under "candidate" so a future tenant_orgs_yaml axis extends the same
// object; until then org labeling always comes from the LIVE
// _tenant_orgs.yaml (a fixed response caveat says so).
type DryRunRequest struct {
	Candidate *DryRunCandidate `json:"candidate"`
}

// DryRunCandidate carries the candidate config documents. RbacYAML is the
// verbatim _rbac.yaml text; a pointer so an ABSENT field is a 400 while a
// present-but-empty document stays a valid what-if (the empty-config
// verdict is mode-dependent and worth asking about).
type DryRunCandidate struct {
	RbacYAML *string `json:"rbac_yaml"`
}

// StringDelta is one changed string axis (from → to).
type StringDelta struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// BoolDelta is one changed boolean axis (from → to).
type BoolDelta struct {
	From bool `json:"from"`
	To   bool `json:"to"`
}

// DryRunChanged is one rule granted in BOTH reports whose diffable axes
// differ. Only the axes that actually changed are present; an absent axis is
// unchanged. Indexes are each report's own config order and survive
// redaction (identity is index, mirroring ReverseGrant).
type DryRunChanged struct {
	Rule           string       `json:"rule,omitempty"` // stripped in the redacted view
	LiveIndex      int          `json:"live_index"`
	CandidateIndex int          `json:"candidate_index"`
	OutcomeShadow  *StringDelta `json:"outcome_shadow,omitempty"`
	OutcomeEnforce *StringDelta `json:"outcome_enforce,omitempty"`
	Unsatisfiable  *BoolDelta   `json:"unsatisfiable,omitempty"`
}

// DryRunAdded is one grant present only in the candidate report.
type DryRunAdded struct {
	Rule           string `json:"rule,omitempty"` // stripped in the redacted view
	CandidateIndex int    `json:"candidate_index"`
}

// DryRunRemoved is one grant present only in the live report.
type DryRunRemoved struct {
	Rule      string `json:"rule,omitempty"` // stripped in the redacted view
	LiveIndex int    `json:"live_index"`
}

// DryRunDiff is the structural diff between the baseline and candidate
// reports' grants for the queried tenant.
type DryRunDiff struct {
	Alignment string          `json:"alignment"`
	Changed   []DryRunChanged `json:"changed"`
	Added     []DryRunAdded   `json:"added"`
	Removed   []DryRunRemoved `json:"removed"`
}

// DryRunResponse is the dry-run envelope (the endpoint's @Success schema).
// CandidateSHA256 lives HERE, not inside the candidate report's
// config_anchor: a candidate report is honestly unanchored (no file, no
// hot-reload hash) and mutating it would fake an anchor the rbac core never
// produced. The envelope hash is the handler's own digest of the submitted
// bytes, so an operator can still pin "which candidate produced this diff".
type DryRunResponse struct {
	SchemaVersion   int                `json:"schema_version"`
	GeneratedAt     string             `json:"generated_at"`
	Baseline        rbac.ReverseReport `json:"baseline"`
	Candidate       rbac.ReverseReport `json:"candidate"`
	CandidateSHA256 string             `json:"candidate_sha256"`
	Diff            DryRunDiff         `json:"diff"`
	Caveats         []string           `json:"caveats"`
}

// dryRunCaveats is the fixed evaluation-context banner every dry-run
// response carries — the assumptions a reader must hold before acting on
// the diff.
var dryRunCaveats = []string{
	"candidate evaluated under THIS deployment's --identity-claim-headers declaration",
	"org labeling taken from the LIVE _tenant_orgs.yaml (candidate tenant-org input not supported)",
	"a renamed rule appears as removed+added (grants pair by rule name)",
	"presence-implies-membership applies: a grant entry's existence is itself weakly identifying",
}

// DryRunTenantAccessReport handles
// POST /api/v1/audit/tenants/{id}/access-report/dry-run.
//
// Query parameters mirror the GET endpoint (include=org_values opt-in,
// view=full|redacted strict); both embedded reports and the diff follow the
// selected projection together.
//
// @Summary     What-if dry-run: diff a candidate _rbac.yaml's access report (audit-only)
// @Description Evaluates an operator-supplied candidate _rbac.yaml through the
// @Description SAME strict parse+validation pipeline and reverse-report core
// @Description the live config runs, WITHOUT installing it, and reports what
// @Description would change for tenant {id}: the live baseline report, the
// @Description candidate report (config_anchor honestly unanchored; the
// @Description envelope's candidate_sha256 pins the submitted bytes), and a
// @Description structural diff paired by rule name before any redaction.
// @Description Mutates nothing. Requires the same NON-org-scoped platform
// @Description admin grant as the GET endpoint; all other callers receive the
// @Description byte-identical constant 403 (no tenant-enumeration oracle).
// @Tags        audit
// @Accept      json
// @Produce     json
// @Param       id      path  string        true  "Tenant ID"
// @Param       include query string        false "Opt-in expansions"  Enums(org_values)
// @Param       view    query string        false "Report projection"  Enums(full, redacted) default(full)
// @Param       body    body  DryRunRequest true  "Candidate config envelope"
// @Success     200 {object} DryRunResponse
// @Failure     400 {object} map[string]string
// @Failure     401 {object} map[string]string
// @Failure     403 {object} map[string]string
// @Router      /api/v1/audit/tenants/{id}/access-report/dry-run [post]
func DryRunTenantAccessReport(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// 1. LOCKED bar, before anything request-derived — the SAME constant
		// 403 as the GET endpoint, byte for byte.
		if !d.RBAC.PlatformAdminNonOrgScoped(rbac.RequestPrincipal(r)) {
			WriteJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden, auditReverseForbiddenMsg)
			return
		}

		// 2. Tenant id shape (no existence check / no 404 masking — same
		// offboarded-tenant rationale as the GET endpoint).
		id := chi.URLParam(r, "id")
		if err := ValidateTenantID(id); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// 3. Query params — strict, mirroring the GET endpoint: a typo'd
		// ?view=redcated silently answering FULL would be a fail-open
		// projection choice.
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

		// 4. Bounded body read (tenant_validate.go precedent). An oversized
		// body truncates at the limit and fails the JSON shell below.
		body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		// 5. JSON shell, strict — unknown fields are rejected to match the
		// strict-query handling above and, more importantly, to keep the
		// tenant_orgs_yaml extension slot fail-LOUD: a client that sends a
		// candidate field this build does not yet evaluate gets a 400, not a
		// silent drop that would make the diff lie about what took effect.
		// rbac_yaml uses pointer-presence so an absent field is rejected while
		// an explicit empty document remains a valid what-if (custom-alerts F1
		// precedent: absent must not be misread).
		var req DryRunRequest
		dec := json.NewDecoder(bytes.NewReader(body))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		if req.Candidate == nil || req.Candidate.RbacYAML == nil {
			WriteJSONError(w, r, http.StatusBadRequest, "candidate.rbac_yaml is required")
			return
		}
		candBytes := []byte(*req.Candidate.RbacYAML)

		// 6. Candidate parse through the LIVE pipeline, validated against the
		// claim keys this deployment declares. The parse detail is echoed:
		// the caller already passed the platform-admin bar.
		cfg, err := rbac.ParseCandidateConfig(candBytes, d.ClaimHeaders)
		if err != nil {
			WriteJSONErrorWithCode(w, r, http.StatusBadRequest, CodeCandidateInvalid,
				"candidate _rbac.yaml rejected: "+err.Error())
			return
		}

		// 7. Rule-count ceiling (cheap DoS bound; see maxCandidateRules).
		if len(cfg.Groups) > maxCandidateRules {
			WriteJSONError(w, r, http.StatusBadRequest, fmt.Sprintf(
				"candidate has %d rules, exceeding the dry-run limit of %d", len(cfg.Groups), maxCandidateRules))
			return
		}

		// 8. Both reports over the SAME inputs: org labeling comes from the
		// live tenantorg manager for candidate and baseline alike (the
		// candidate axis is _rbac.yaml only), and the candidate inherits the
		// live fail-closed bit so an empty candidate reports the verdict the
		// same bytes would produce once deployed.
		orgs, known := d.TenantOrg.OrgsForTenant(id)
		tenantOrgsHash := ""
		if d.TenantOrg != nil {
			tenantOrgsHash = d.TenantOrg.LastHash()
		}
		opts := rbac.ReverseReportOptions{
			IncludeOrgValues: includeOrgValues,
			DevBypassActive:  devBypassActive.Load(),
		}
		baseline := d.RBAC.ReverseAccessReport(id, orgs, known, tenantOrgsHash, opts)
		candidate := rbac.NewCandidate(cfg, d.RBAC.EvaluationMode()).
			ReverseAccessReport(id, orgs, known, tenantOrgsHash, opts)

		// 9-10. Diff the FULL structures first, then project.
		diff := diffReverseReports(baseline, candidate)
		if redacted {
			baseline = rbac.RedactReverseReport(baseline)
			candidate = rbac.RedactReverseReport(candidate)
			diff = redactDryRunDiff(diff)
		}

		candSum := sha256.Sum256(candBytes)
		resp := DryRunResponse{
			SchemaVersion:   dryRunSchemaVersion,
			GeneratedAt:     time.Now().UTC().Format(time.RFC3339),
			Baseline:        baseline,
			Candidate:       candidate,
			CandidateSHA256: hex.EncodeToString(candSum[:]),
			Diff:            diff,
			Caveats:         dryRunCaveats,
		}

		// 11. Meta-audit (mirrors the GET endpoint's): who dry-ran WHAT
		// candidate against whose access map, anchored by hashes and diff
		// COUNTS only — never candidate content, claim values or org values;
		// this log line travels further than the report itself.
		slog.Info("access-report dry-run served",
			"caller", rbac.RequestEmail(r),
			"tenant", id,
			"rbac_sha256", baseline.ConfigAnchor.RBACSHA256.Value,
			"tenant_orgs_sha256", baseline.ConfigAnchor.TenantOrgsSHA256.Value,
			"candidate_sha256", resp.CandidateSHA256,
			"view", map[bool]string{true: "redacted", false: "full"}[redacted],
			"include_org_values", includeOrgValues,
			"alignment", diff.Alignment,
			"changed", len(diff.Changed),
			"added", len(diff.Added),
			"removed", len(diff.Removed),
		)

		writeJSON(w, http.StatusOK, resp)
	}
}

// diffReverseReports pairs the two reports' grants BY RULE NAME on the FULL
// (pre-redaction) structures — structure-then-redact. A name granted exactly
// once on each side pairs; the pair is "changed" iff any diffable axis
// differs (changedEntry). A name granted on one side only is added/removed —
// which covers both a rule edited out of the config AND a rule whose tenant
// patterns stopped hitting the queried tenant (the same thing, seen from
// this tenant's access map). A duplicated name on both sides cannot be
// paired faithfully: all its entries degrade to added+removed and the
// alignment drops to coarse. Emission order is deterministic: removed/
// changed in live config order, added in candidate config order.
func diffReverseReports(live, cand rbac.ReverseReport) DryRunDiff {
	liveByName := grantsByRule(live.Grants)
	candByName := grantsByRule(cand.Grants)

	diff := DryRunDiff{
		Alignment: DryRunAlignmentExact,
		Changed:   make([]DryRunChanged, 0),
		Added:     make([]DryRunAdded, 0),
		Removed:   make([]DryRunRemoved, 0),
	}
	for i := range live.Grants {
		g := &live.Grants[i]
		lg, cg := liveByName[g.Rule], candByName[g.Rule]
		switch {
		case len(cg) == 0:
			// Removed×N is exact even for a duplicated live name: with no
			// candidate counterpart there is no pairing to get wrong.
			diff.Removed = append(diff.Removed, DryRunRemoved{Rule: g.Rule, LiveIndex: g.Index})
		case len(lg) == 1 && len(cg) == 1:
			if c := changedEntry(lg[0], cg[0]); c != nil {
				diff.Changed = append(diff.Changed, *c)
			}
		default:
			// Name on both sides with >1 grant on either: ambiguous pairing.
			diff.Alignment = DryRunAlignmentCoarse
			diff.Removed = append(diff.Removed, DryRunRemoved{Rule: g.Rule, LiveIndex: g.Index})
		}
	}
	for i := range cand.Grants {
		g := &cand.Grants[i]
		lg, cg := liveByName[g.Rule], candByName[g.Rule]
		if len(lg) == 1 && len(cg) == 1 {
			continue // paired (changed or identical) above
		}
		// Coarse only when the name also exists on the live side (>1 either
		// side = ambiguous pairing). Added×N for a candidate-only duplicate
		// name stays exact — the mirror of the removed-only case above: with
		// no live counterpart there is no pairing to get wrong.
		if len(lg) > 0 {
			diff.Alignment = DryRunAlignmentCoarse
		}
		diff.Added = append(diff.Added, DryRunAdded{Rule: g.Rule, CandidateIndex: g.Index})
	}
	return diff
}

// grantsByRule indexes a report's grants by rule name (multi-valued: rules
// may share a name).
func grantsByRule(grants []rbac.ReverseGrant) map[string][]*rbac.ReverseGrant {
	byName := make(map[string][]*rbac.ReverseGrant, len(grants))
	for i := range grants {
		byName[grants[i].Rule] = append(byName[grants[i].Rule], &grants[i])
	}
	return byName
}

// changedEntry compares one exactly-paired grant across the three diffable
// axes — outcome_shadow / outcome_enforce / unsatisfiable, the flag-agnostic
// surface — and returns nil when the pair is identical on all three.
func changedEntry(live, cand *rbac.ReverseGrant) *DryRunChanged {
	c := DryRunChanged{Rule: live.Rule, LiveIndex: live.Index, CandidateIndex: cand.Index}
	hit := false
	if live.OrgGate.OutcomeShadow != cand.OrgGate.OutcomeShadow {
		c.OutcomeShadow = &StringDelta{From: live.OrgGate.OutcomeShadow, To: cand.OrgGate.OutcomeShadow}
		hit = true
	}
	if live.OrgGate.OutcomeEnforce != cand.OrgGate.OutcomeEnforce {
		c.OutcomeEnforce = &StringDelta{From: live.OrgGate.OutcomeEnforce, To: cand.OrgGate.OutcomeEnforce}
		hit = true
	}
	if live.OrgGate.Unsatisfiable != cand.OrgGate.Unsatisfiable {
		c.Unsatisfiable = &BoolDelta{From: live.OrgGate.Unsatisfiable, To: cand.OrgGate.Unsatisfiable}
		hit = true
	}
	if !hit {
		return nil
	}
	return &c
}

// redactDryRunDiff rebuilds the diff from an ALLOWLIST of redaction-safe
// fields, mirroring rbac.RedactReverseReport. It deliberately does NOT
// strip-in-place: a denylist that clears known identifiers would silently pass
// through any identifier field a future edit adds to these structs (the
// denylist-after-merge fail-open class this repo has been burned by). Only the
// index/outcome-delta fields — which carry no identifiers — are copied
// forward; Rule is dropped by construction.
func redactDryRunDiff(diff DryRunDiff) DryRunDiff {
	out := DryRunDiff{
		Alignment: diff.Alignment,
		Changed:   make([]DryRunChanged, len(diff.Changed)),
		Added:     make([]DryRunAdded, len(diff.Added)),
		Removed:   make([]DryRunRemoved, len(diff.Removed)),
	}
	for i, c := range diff.Changed {
		out.Changed[i] = DryRunChanged{
			LiveIndex:      c.LiveIndex,
			CandidateIndex: c.CandidateIndex,
			OutcomeShadow:  c.OutcomeShadow,
			OutcomeEnforce: c.OutcomeEnforce,
			Unsatisfiable:  c.Unsatisfiable,
		}
	}
	for i, a := range diff.Added {
		out.Added[i] = DryRunAdded{CandidateIndex: a.CandidateIndex}
	}
	for i, rm := range diff.Removed {
		out.Removed[i] = DryRunRemoved{LiveIndex: rm.LiveIndex}
	}
	return out
}
