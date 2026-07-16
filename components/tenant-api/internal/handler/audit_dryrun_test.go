package handler

// POST /api/v1/audit/tenants/{id}/access-report/dry-run handler tests
// (ADR-027 / LD-6 P7):
//
//   - LOCKED bar parity: the 403 is constant across ids AND byte-identical
//     to the GET endpoint's (shared constant — the dry-run must not become a
//     softer side door to the same audit surface), and an org-scoped
//     wildcard admin is still a non-qualifying caller.
//   - authorized-caller 400 matrix: id/query/body-shell/candidate/limit
//     failures fire only AFTER the bar (grounding the check order from the
//     authorized side, mirroring the GET tests).
//   - diff goldens: the unlabeled-tenant shadow/enforce divergent pair
//     (pass_unlabeled vs fail_unlabeled — the ONLY outcome pair where the
//     two fail-modes flip), the labeled-tenant conditional convergence, the
//     unsatisfiable-only delta, rename→removed+added, duplicate-name→coarse.
//   - redact-then-diff property: the redacted response serializes with ZERO
//     rule/group names, claim keys or org values while the pre-redaction
//     pairing (indexes + outcome deltas) survives.
//   - meta-audit: hashes and diff counts are logged, candidate content never.
//
// Candidate-evaluation fidelity (parse pipeline, fail-closed bit, report
// equivalence) is pinned at the rbac layer (candidate_test.go); these tests
// pin what the HTTP surface adds.

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

const (
	dryrunOrgClaimHeader  = "X-Dryrun-Org"
	dryrunTenantLabeled   = "db-dryrun-labeled"   // labeled with the covered org
	dryrunTenantUnlabeled = "db-dryrun-unlabeled" // onboarded, zero orgs
	dryrunTenantGhost     = "db-dryrun-ghost"     // never onboarded
	dryrunOrgCovered      = "ORG-DR-COVERED"      // tenant org ∩ live pinned rule
	dryrunOrgElsewhere    = "ORG-DR-ELSEWHERE"    // candidate re-pin target
)

// dryrunLiveRBACYAML (config order pins the live_index expectations):
//
//	0 dryrun-platform-admins — passes the bar
//	1 dryrun-org-admins      — org-scoped wildcard admin (fails the bar)
//	2 dryrun-readers         — passes route middleware, fails the bar
//	3 dryrun-ops             — NO org-scope (the candidate adds it → changed)
//	4 dryrun-pinned          — org-scoped, claims pin the covered org
//	5 dryrun-old-reader      — renamed in the candidate → removed
const dryrunLiveRBACYAML = `groups:
  - name: dryrun-platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: dryrun-org-admins
    tenants: ["*"]
    permissions: [admin]
    org-scope: org
  - name: dryrun-readers
    tenants: ["*"]
    permissions: [read]
  - name: dryrun-ops
    tenants: ["db-dryrun-*"]
    permissions: [read, write]
  - name: dryrun-pinned
    match:
      groups: [dryrun-team]
      claims:
        org: [` + dryrunOrgCovered + `]
    tenants: ["db-dryrun-*"]
    permissions: [read]
    org-scope: org
  - name: dryrun-old-reader
    tenants: ["db-dryrun-*"]
    permissions: [read]
`

// dryrunCandidateRBACYAML edits the live config on exactly three axes:
// dryrun-ops gains org-scope (outcome flip), dryrun-pinned re-pins its claim
// to a value the labeled tenant does not carry (unsatisfiable flip), and
// dryrun-old-reader is renamed to dryrun-new-reader with an identical body
// (the rename → removed+added case; candidate_index 5).
const dryrunCandidateRBACYAML = `groups:
  - name: dryrun-platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: dryrun-org-admins
    tenants: ["*"]
    permissions: [admin]
    org-scope: org
  - name: dryrun-readers
    tenants: ["*"]
    permissions: [read]
  - name: dryrun-ops
    tenants: ["db-dryrun-*"]
    permissions: [read, write]
    org-scope: org
  - name: dryrun-pinned
    match:
      groups: [dryrun-team]
      claims:
        org: [` + dryrunOrgElsewhere + `]
    tenants: ["db-dryrun-*"]
    permissions: [read]
    org-scope: org
  - name: dryrun-new-reader
    tenants: ["db-dryrun-*"]
    permissions: [read]
`

// newDryRunFixture mirrors newAuditReverseFixture: production RBAC
// constructor with the org claim header declared, tenantorg labeling one
// tenant and onboarding a second with zero orgs (the unlabeled state the
// shadow/enforce divergence keys on).
func newDryRunFixture(t *testing.T) *Deps {
	t.Helper()
	claimHeaders := map[string]string{"org": dryrunOrgClaimHeader}
	mgr := newRBACManagerWithClaims(t, dryrunLiveRBACYAML, claimHeaders)
	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		dryrunTenantLabeled:   {dryrunOrgCovered},
		dryrunTenantUnlabeled: {},
	}})
	// ClaimHeaders is the SAME map the manager was constructed with (the
	// main.go wiring shape) — candidates validate against the axes the
	// deployment actually runs.
	return &Deps{RBAC: mgr, TenantOrg: torg, ClaimHeaders: claimHeaders}
}

// dryRunBody wraps a candidate YAML in the request envelope.
func dryRunBody(t *testing.T, candidateYAML string) string {
	t.Helper()
	b, err := json.Marshal(DryRunRequest{Candidate: &DryRunCandidate{RbacYAML: &candidateYAML}})
	if err != nil {
		t.Fatalf("marshal dry-run body: %v", err)
	}
	return string(b)
}

// serveDryRun runs one POST through the SAME middleware shape the route
// mounts (rbac.Middleware(PermRead, nil) — authenticated only; the bar is in
// the handler).
func serveDryRun(t *testing.T, d *Deps, id, query, groups, orgClaim, body string) *httptest.ResponseRecorder {
	t.Helper()
	target := "/api/v1/audit/tenants/" + id + "/access-report/dry-run" + query
	req := newRequestWithChiParam("POST", target, "id", id, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "dryrun-auditor@example.com")
	req.Header.Set("X-Forwarded-Groups", groups)
	if orgClaim != "" {
		req.Header.Set(dryrunOrgClaimHeader, orgClaim)
	}
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(DryRunTenantAccessReport(d), d.RBAC, rbac.PermRead, nil).ServeHTTP(w, req)
	return w
}

// decodeDryRun asserts a 200 and decodes the envelope.
func decodeDryRun(t *testing.T, w *httptest.ResponseRecorder) DryRunResponse {
	t.Helper()
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp DryRunResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode dry-run response: %v", err)
	}
	return resp
}

// TestDryRun_BarConstant403 pins the enumeration-oracle defense on the POST
// surface AND its parity with the GET endpoint: a caller failing the bar
// gets a 403 body byte-identical across existing / ghost / malformed ids —
// and byte-identical to the GET endpoint's constant 403 (shared constant;
// the dry-run must not be a distinguishable side door).
func TestDryRun_BarConstant403(t *testing.T) {
	t.Parallel()
	d := newDryRunFixture(t)
	body := dryRunBody(t, dryrunCandidateRBACYAML)

	ids := []string{dryrunTenantLabeled, dryrunTenantGhost, "bad..id"}
	bodies := make([]string, len(ids))
	for i, id := range ids {
		w := serveDryRun(t, d, id, "", "dryrun-readers", "", body)
		if w.Code != http.StatusForbidden {
			t.Fatalf("id %q: status = %d, want constant 403 (bar before everything); body=%s",
				id, w.Code, w.Body.String())
		}
		bodies[i] = w.Body.String()
	}
	for i := 1; i < len(bodies); i++ {
		if bodies[i] != bodies[0] {
			t.Errorf("403 body for id %q differs from id %q — enumeration oracle:\n%s\nvs\n%s",
				ids[i], ids[0], bodies[i], bodies[0])
		}
	}

	// Cross-endpoint parity: the SAME caller on the GET endpoint.
	req := newRequestWithChiParam("GET",
		"/api/v1/audit/tenants/"+dryrunTenantLabeled+"/access-report", "id", dryrunTenantLabeled, nil)
	req.Header.Set("X-Forwarded-Email", "dryrun-auditor@example.com")
	req.Header.Set("X-Forwarded-Groups", "dryrun-readers")
	get := httptest.NewRecorder()
	wrapWithRBACMiddleware(GetTenantAccessReport(d), d.RBAC, rbac.PermRead, nil).ServeHTTP(get, req)
	if get.Code != http.StatusForbidden {
		t.Fatalf("GET reference status = %d, want 403; body=%s", get.Code, get.Body.String())
	}
	if bodies[0] != get.Body.String() {
		t.Errorf("POST 403 body differs from the GET endpoint's constant 403:\n%s\nvs\n%s",
			bodies[0], get.Body.String())
	}
}

// TestDryRun_OrgScopedWildcardAdmin403 pins the LOCKED bar semantics: an
// ORG-SCOPED wildcard admin — who passes the route middleware — gets the
// SAME constant 403 as any other non-qualifying caller.
func TestDryRun_OrgScopedWildcardAdmin403(t *testing.T) {
	t.Parallel()
	d := newDryRunFixture(t)
	body := dryRunBody(t, dryrunCandidateRBACYAML)

	w := serveDryRun(t, d, dryrunTenantLabeled, "", "dryrun-org-admins", dryrunOrgCovered, body)
	if w.Code != http.StatusForbidden {
		t.Fatalf("org-scoped wildcard admin status = %d, want 403 (tightened bar); body=%s",
			w.Code, w.Body.String())
	}
	ref := serveDryRun(t, d, dryrunTenantLabeled, "", "dryrun-readers", "", body)
	if w.Body.String() != ref.Body.String() {
		t.Errorf("org-scoped admin 403 body differs from the generic bar 403 (constant-shape violation):\n%s\nvs\n%s",
			w.Body.String(), ref.Body.String())
	}
}

// TestDryRun_AuthorizedCaller400s grounds the check order from the
// authorized side: every id/query/body/candidate validation fires as a 400
// only for a caller passing the bar (so the constant 403 above is genuinely
// the bar, not a shared error path).
func TestDryRun_AuthorizedCaller400s(t *testing.T) {
	t.Parallel()
	d := newDryRunFixture(t)
	valid := dryRunBody(t, dryrunCandidateRBACYAML)

	// Candidate exceeding the rule ceiling (parses fine, bounces on count).
	var bulk strings.Builder
	bulk.WriteString("groups:\n")
	for i := 0; i <= maxCandidateRules; i++ {
		fmt.Fprintf(&bulk, "  - name: dryrun-bulk-%d\n    tenants: [\"*\"]\n    permissions: [read]\n", i)
	}

	cases := []struct {
		name     string
		id       string
		query    string
		body     string
		wantCode string // machine-readable code, "" = don't care
		wantMsg  string // substring of the error message
	}{
		{"malformed id", "bad..id", "", valid, "", ""},
		{"unknown view value", dryrunTenantLabeled, "?view=redcated", valid, "", "view value"},
		{"unknown include value", dryrunTenantLabeled, "?include=everything", valid, "", "include value"},
		{"broken JSON shell", dryrunTenantLabeled, "", "{not json", "", "invalid JSON body"},
		// Strict body: an unknown field fails loud rather than being silently
		// dropped — this is the tenant_orgs_yaml extension slot's guarantee.
		{"unknown top-level field", dryrunTenantLabeled, "",
			`{"candidate":{"rbac_yaml":"groups: []\n"},"tenant_orgs_yaml":"x"}`, "", "invalid JSON body"},
		{"unknown candidate field", dryrunTenantLabeled, "",
			`{"candidate":{"rbac_yaml":"groups: []\n","tenant_orgs_yaml":"x"}}`, "", "invalid JSON body"},
		{"missing candidate", dryrunTenantLabeled, "", "{}", "", "candidate.rbac_yaml is required"},
		{"missing rbac_yaml", dryrunTenantLabeled, "", `{"candidate":{}}`, "", "candidate.rbac_yaml is required"},
		{"candidate invalid — undeclared org-scope key", dryrunTenantLabeled, "",
			dryRunBody(t, "groups:\n  - name: dryrun-x\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: team\n"),
			CodeCandidateInvalid, "not declared in --identity-claim-headers"},
		{"candidate invalid — unknown field", dryrunTenantLabeled, "",
			dryRunBody(t, "groups:\n  - name: dryrun-x\n    tenants: [\"*\"]\n    permissions: [read]\n    typo-field: x\n"),
			CodeCandidateInvalid, "candidate _rbac.yaml rejected"},
		{"rules over the ceiling", dryrunTenantLabeled, "", dryRunBody(t, bulk.String()), "", "dry-run limit"},
	}
	for _, tc := range cases {
		w := serveDryRun(t, d, tc.id, tc.query, "dryrun-platform-admins", "", tc.body)
		if w.Code != http.StatusBadRequest {
			t.Errorf("%s: status = %d, want 400; body=%s", tc.name, w.Code, w.Body.String())
			continue
		}
		if tc.wantCode != "" && !strings.Contains(w.Body.String(), tc.wantCode) {
			t.Errorf("%s: body missing code %q: %s", tc.name, tc.wantCode, w.Body.String())
		}
		if tc.wantMsg != "" && !strings.Contains(w.Body.String(), tc.wantMsg) {
			t.Errorf("%s: body missing %q: %s", tc.name, tc.wantMsg, w.Body.String())
		}
	}

	// Oversized body: a tight MaxBody truncates the read and the JSON shell
	// rejects the torn document (tenant_validate.go LimitReader precedent).
	small := &Deps{RBAC: d.RBAC, TenantOrg: d.TenantOrg, ClaimHeaders: d.ClaimHeaders, MaxBodyBytes: 64}
	w := serveDryRun(t, small, dryrunTenantLabeled, "", "dryrun-platform-admins", "", valid)
	if w.Code != http.StatusBadRequest {
		t.Errorf("oversized body: status = %d, want 400; body=%s", w.Code, w.Body.String())
	}
}

// TestDryRun_DiffGolden_UnlabeledFlip pins the diff on the UNLABELED tenant:
// adding org-scope to a matched rule is the ONLY combination where the two
// fail-modes diverge — shadow flips to pass_unlabeled while enforce flips to
// fail_unlabeled — plus the rename rendered as removed+added, exact
// alignment, and the envelope basics (versions, hash, anchors, caveats).
func TestDryRun_DiffGolden_UnlabeledFlip(t *testing.T) {
	t.Parallel()
	d := newDryRunFixture(t)

	w := serveDryRun(t, d, dryrunTenantUnlabeled, "", "dryrun-platform-admins", "",
		dryRunBody(t, dryrunCandidateRBACYAML))
	resp := decodeDryRun(t, w)

	if resp.SchemaVersion != 1 {
		t.Errorf("schema_version = %d, want 1", resp.SchemaVersion)
	}
	sum := sha256.Sum256([]byte(dryrunCandidateRBACYAML))
	if want := hex.EncodeToString(sum[:]); resp.CandidateSHA256 != want {
		t.Errorf("candidate_sha256 = %q, want %q", resp.CandidateSHA256, want)
	}
	if len(resp.Caveats) != 4 {
		t.Errorf("caveats = %d entries, want 4: %v", len(resp.Caveats), resp.Caveats)
	}
	if got := resp.Baseline.ConfigAnchor.RBACSHA256.Value; got == rbac.AnchorUnanchored || got == "" {
		t.Errorf("baseline rbac anchor = %q, want a real file hash", got)
	}
	if got := resp.Candidate.ConfigAnchor.RBACSHA256.Value; got != rbac.AnchorUnanchored {
		t.Errorf("candidate rbac anchor = %q, want %q (never a fabricated hash)", got, rbac.AnchorUnanchored)
	}

	if resp.Diff.Alignment != DryRunAlignmentExact {
		t.Errorf("alignment = %q, want exact", resp.Diff.Alignment)
	}
	if len(resp.Diff.Changed) != 1 {
		t.Fatalf("changed = %d entries, want 1 (dryrun-ops only): %+v", len(resp.Diff.Changed), resp.Diff.Changed)
	}
	c := resp.Diff.Changed[0]
	if c.Rule != "dryrun-ops" || c.LiveIndex != 3 || c.CandidateIndex != 3 {
		t.Errorf("changed entry = %+v, want dryrun-ops live_index=3 candidate_index=3", c)
	}
	if c.OutcomeShadow == nil || c.OutcomeShadow.From != rbac.OrgOutcomeNotRequired ||
		c.OutcomeShadow.To != rbac.OrgOutcomePassUnlabeled {
		t.Errorf("outcome_shadow delta = %+v, want not_required → pass_unlabeled", c.OutcomeShadow)
	}
	if c.OutcomeEnforce == nil || c.OutcomeEnforce.From != rbac.OrgOutcomeNotRequired ||
		c.OutcomeEnforce.To != rbac.OrgOutcomeFailUnlabeled {
		t.Errorf("outcome_enforce delta = %+v, want not_required → fail_unlabeled", c.OutcomeEnforce)
	}
	if c.Unsatisfiable != nil {
		t.Errorf("unsatisfiable delta = %+v, want absent (false on both sides)", c.Unsatisfiable)
	}

	if len(resp.Diff.Removed) != 1 || resp.Diff.Removed[0].Rule != "dryrun-old-reader" ||
		resp.Diff.Removed[0].LiveIndex != 5 {
		t.Errorf("removed = %+v, want [dryrun-old-reader live_index=5]", resp.Diff.Removed)
	}
	if len(resp.Diff.Added) != 1 || resp.Diff.Added[0].Rule != "dryrun-new-reader" ||
		resp.Diff.Added[0].CandidateIndex != 5 {
		t.Errorf("added = %+v, want [dryrun-new-reader candidate_index=5]", resp.Diff.Added)
	}
}

// TestDryRun_DiffGolden_LabeledConditional pins the diff on the LABELED
// tenant: the same org-scope addition converges to conditional_on_caller_org
// in BOTH fail-modes (the divergent unlabeled pair must not appear), and the
// re-pinned rule surfaces as an unsatisfiable-ONLY delta — proving the third
// axis alone marks a pair changed.
func TestDryRun_DiffGolden_LabeledConditional(t *testing.T) {
	t.Parallel()
	d := newDryRunFixture(t)

	w := serveDryRun(t, d, dryrunTenantLabeled, "", "dryrun-platform-admins", "",
		dryRunBody(t, dryrunCandidateRBACYAML))
	resp := decodeDryRun(t, w)

	if resp.Diff.Alignment != DryRunAlignmentExact {
		t.Errorf("alignment = %q, want exact", resp.Diff.Alignment)
	}
	if len(resp.Diff.Changed) != 2 {
		t.Fatalf("changed = %d entries, want 2 (ops + pinned, live order): %+v",
			len(resp.Diff.Changed), resp.Diff.Changed)
	}

	ops := resp.Diff.Changed[0]
	if ops.Rule != "dryrun-ops" {
		t.Fatalf("changed[0] = %+v, want dryrun-ops (live config order)", ops)
	}
	for name, delta := range map[string]*StringDelta{"outcome_shadow": ops.OutcomeShadow, "outcome_enforce": ops.OutcomeEnforce} {
		if delta == nil || delta.From != rbac.OrgOutcomeNotRequired || delta.To != rbac.OrgOutcomeConditional {
			t.Errorf("ops %s delta = %+v, want not_required → conditional_on_caller_org (both modes converge on a labeled tenant)",
				name, delta)
		}
	}

	pinned := resp.Diff.Changed[1]
	if pinned.Rule != "dryrun-pinned" || pinned.LiveIndex != 4 || pinned.CandidateIndex != 4 {
		t.Fatalf("changed[1] = %+v, want dryrun-pinned live_index=4 candidate_index=4", pinned)
	}
	if pinned.OutcomeShadow != nil || pinned.OutcomeEnforce != nil {
		t.Errorf("pinned outcome deltas = %+v / %+v, want both absent (conditional on both sides)",
			pinned.OutcomeShadow, pinned.OutcomeEnforce)
	}
	if pinned.Unsatisfiable == nil || pinned.Unsatisfiable.From || !pinned.Unsatisfiable.To {
		t.Errorf("pinned unsatisfiable delta = %+v, want false → true (re-pinned to an org the tenant lacks)",
			pinned.Unsatisfiable)
	}
}

// TestDryRun_SameNameCoarse pins the duplicate-name degrade: a name granted
// on both sides with >1 entry on either cannot be paired faithfully, so ALL
// its entries land in added+removed and the alignment reports coarse —
// while unambiguous names keep pairing exactly.
func TestDryRun_SameNameCoarse(t *testing.T) {
	t.Parallel()
	const liveYAML = `groups:
  - name: dryrun-platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: dryrun-dup
    tenants: ["db-dryrun-*"]
    permissions: [read]
`
	const candYAML = `groups:
  - name: dryrun-platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: dryrun-dup
    tenants: ["db-dryrun-*"]
    permissions: [read]
  - name: dryrun-dup
    tenants: ["db-dryrun-*"]
    permissions: [write]
`
	d := &Deps{
		RBAC: newRBACManagerWithClaims(t, liveYAML, map[string]string{"org": dryrunOrgClaimHeader}),
		TenantOrg: tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
			dryrunTenantUnlabeled: {},
		}}),
	}

	w := serveDryRun(t, d, dryrunTenantUnlabeled, "", "dryrun-platform-admins", "",
		dryRunBody(t, candYAML))
	resp := decodeDryRun(t, w)

	if resp.Diff.Alignment != DryRunAlignmentCoarse {
		t.Errorf("alignment = %q, want coarse (duplicated rule name)", resp.Diff.Alignment)
	}
	if len(resp.Diff.Changed) != 0 {
		t.Errorf("changed = %+v, want empty (the degraded name never pairs)", resp.Diff.Changed)
	}
	if len(resp.Diff.Removed) != 1 || resp.Diff.Removed[0].Rule != "dryrun-dup" ||
		resp.Diff.Removed[0].LiveIndex != 1 {
		t.Errorf("removed = %+v, want [dryrun-dup live_index=1]", resp.Diff.Removed)
	}
	if len(resp.Diff.Added) != 2 ||
		resp.Diff.Added[0].Rule != "dryrun-dup" || resp.Diff.Added[0].CandidateIndex != 1 ||
		resp.Diff.Added[1].Rule != "dryrun-dup" || resp.Diff.Added[1].CandidateIndex != 2 {
		t.Errorf("added = %+v, want both dryrun-dup candidate entries (indexes 1,2)", resp.Diff.Added)
	}
}

// TestDryRun_CandidateOnlyDupStaysExact pins the mirror of the removed-only
// case: a duplicate name that exists ONLY on the candidate side (absent from
// live) has no live counterpart to mis-pair, so alignment stays exact even
// though the name is added twice. "coarse" means ambiguous pairing, not
// "a name appears more than once".
func TestDryRun_CandidateOnlyDupStaysExact(t *testing.T) {
	t.Parallel()
	const liveYAML = `groups:
  - name: dryrun-platform-admins
    tenants: ["*"]
    permissions: [admin]
`
	const candYAML = `groups:
  - name: dryrun-platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: dryrun-new
    tenants: ["db-dryrun-*"]
    permissions: [read]
  - name: dryrun-new
    tenants: ["db-dryrun-*"]
    permissions: [write]
`
	d := &Deps{
		RBAC: newRBACManagerWithClaims(t, liveYAML, map[string]string{"org": dryrunOrgClaimHeader}),
		TenantOrg: tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
			dryrunTenantUnlabeled: {},
		}}),
	}

	w := serveDryRun(t, d, dryrunTenantUnlabeled, "", "dryrun-platform-admins", "",
		dryRunBody(t, candYAML))
	resp := decodeDryRun(t, w)

	if resp.Diff.Alignment != DryRunAlignmentExact {
		t.Errorf("alignment = %q, want exact (candidate-only dup has no pairing to get wrong)", resp.Diff.Alignment)
	}
	if len(resp.Diff.Removed) != 0 {
		t.Errorf("removed = %+v, want empty", resp.Diff.Removed)
	}
	if len(resp.Diff.Added) != 2 {
		t.Errorf("added = %+v, want both dryrun-new candidate entries", resp.Diff.Added)
	}
}

// TestDryRun_RedactedNoIdentifiers is the redact-then-diff property at the
// HTTP surface: with view=redacted (even alongside the org-values opt-in)
// the WHOLE serialized envelope — both reports AND the diff — carries no
// rule/group names, no claim keys, no org values, while the pre-redaction
// pairing survives as indexes and outcome deltas.
func TestDryRun_RedactedNoIdentifiers(t *testing.T) {
	t.Parallel()
	d := newDryRunFixture(t)

	w := serveDryRun(t, d, dryrunTenantLabeled, "?view=redacted&include=org_values",
		"dryrun-platform-admins", "", dryRunBody(t, dryrunCandidateRBACYAML))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	body := w.Body.String()

	// Class 1 — rule / group identifiers, from BOTH configs (the tenant id
	// deliberately survives: the caller asked about it).
	for _, tok := range []string{
		"dryrun-platform-admins", "dryrun-org-admins", "dryrun-readers", "dryrun-ops",
		"dryrun-pinned", "dryrun-old-reader", "dryrun-new-reader", "dryrun-team",
	} {
		if strings.Contains(body, tok) {
			t.Errorf("redacted body leaks rule/group identifier %q: %s", tok, body)
		}
	}
	// The diff's rule key must vanish entirely (omitempty on empty).
	if strings.Contains(body, `"rule"`) {
		t.Errorf("redacted body still carries a rule name field: %s", body)
	}
	// Class 2 — claim keys.
	if strings.Contains(body, `"org":`) || strings.Contains(body, `"claim_key"`) {
		t.Errorf("redacted body leaks claim identifiers: %s", body)
	}
	// Class 3 — org values (fixture prefix covers labels AND pins).
	if strings.Contains(body, "ORG-DR-") {
		t.Errorf("redacted body leaks org values: %s", body)
	}
	// The verbatim tenant pattern from either config.
	if strings.Contains(body, "db-dryrun-*") {
		t.Errorf("redacted body leaks the verbatim tenant pattern: %s", body)
	}

	// The structural pairing survives redaction.
	var resp DryRunResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode redacted response: %v", err)
	}
	if len(resp.Diff.Changed) != 2 || len(resp.Diff.Added) != 1 || len(resp.Diff.Removed) != 1 {
		t.Fatalf("redacted diff lost the pre-redaction pairing: %+v", resp.Diff)
	}
	if resp.Diff.Changed[0].LiveIndex != 3 || resp.Diff.Changed[0].OutcomeShadow == nil {
		t.Errorf("redacted changed entry lost indexes/outcomes: %+v", resp.Diff.Changed[0])
	}
	if resp.CandidateSHA256 == "" {
		t.Error("redacted envelope lost candidate_sha256 (a hash is an anchor, not an identifier)")
	}
}

// TestDryRun_MetaAudit pins the meta-audit line: hashes, projection and diff
// COUNTS are recorded; candidate content, claim values and org values never
// are. Deliberately NOT parallel — it swaps the process-global default slog
// logger (TestSlogRequestLogger precedent).
func TestDryRun_MetaAudit(t *testing.T) {
	d := newDryRunFixture(t)

	orig := slog.Default()
	defer slog.SetDefault(orig)
	var buf bytes.Buffer
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))

	w := serveDryRun(t, d, dryrunTenantLabeled, "", "dryrun-platform-admins", "",
		dryRunBody(t, dryrunCandidateRBACYAML))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	logged := buf.String()

	var entry map[string]any
	dec := json.NewDecoder(strings.NewReader(logged))
	for dec.More() {
		var e map[string]any
		if err := dec.Decode(&e); err != nil {
			break
		}
		if e["msg"] == "access-report dry-run served" {
			entry = e
			break
		}
	}
	if entry == nil {
		t.Fatalf("meta-audit line not emitted; log=%s", logged)
	}

	sum := sha256.Sum256([]byte(dryrunCandidateRBACYAML))
	wantFields := map[string]any{
		"caller":           "dryrun-auditor@example.com",
		"tenant":           dryrunTenantLabeled,
		"candidate_sha256": hex.EncodeToString(sum[:]),
		"view":             "full",
		"alignment":        DryRunAlignmentExact,
		"changed":          float64(2),
		"added":            float64(1),
		"removed":          float64(1),
	}
	for k, want := range wantFields {
		if got := entry[k]; got != want {
			t.Errorf("meta-audit %s = %v, want %v", k, got, want)
		}
	}
	for _, k := range []string{"rbac_sha256", "tenant_orgs_sha256", "include_org_values"} {
		if _, ok := entry[k]; !ok {
			t.Errorf("meta-audit missing field %q; entry=%v", k, entry)
		}
	}

	// Never candidate content: no rule names from the submitted config, no
	// org values — the log line travels further than the report.
	for _, tok := range []string{"dryrun-new-reader", "dryrun-ops", "ORG-DR-", "rbac_yaml"} {
		if strings.Contains(logged, tok) {
			t.Errorf("meta-audit leaks candidate content %q: %s", tok, logged)
		}
	}
}
