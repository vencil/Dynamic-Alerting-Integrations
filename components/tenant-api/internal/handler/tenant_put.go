package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/textproto"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"gopkg.in/yaml.v3"
)

// PutTenantResponse is the response body for PUT /api/v1/tenants/{id}.
type PutTenantResponse struct {
	Status   string `json:"status"`
	TenantID string `json:"tenant_id"`
	PRURL    string `json:"pr_url,omitempty"`
	PRNumber int    `json:"pr_number,omitempty"`
	Message  string `json:"message,omitempty"`
	// Warnings carries NON-BLOCKING advisories for a write that SUCCEEDED —
	// currently the #1231 deprecated-key alias notices (e.g. a body still
	// spelling mysql_threads_running as mysql_cpu). The write went through;
	// these tell the author what to migrate before the transition window
	// closes. Also the #2325 domain-policy advisories: a non-pagerduty
	// destination that still catches severity=critical alerts under
	// `require_critical_escalation`. Never populated on error responses.
	Warnings []string `json:"warnings,omitempty"`
}

// PutTenant handles PUT /api/v1/tenants/{id}
//
// Accepts a tenant-only YAML document — the conf.d/{id}.yaml shape, a
// `tenants:` block containing the {id} section (NOT the platform defaults).
// The body is committed verbatim, so any top-level key other than `tenants`
// (e.g. a stray `defaults:` / `state_filters:` / `profiles:`, or a typo) is
// rejected with 400 — mirroring tenant-config.schema.json's
// additionalProperties:false (#705). Validation merges the on-disk
// _defaults.yaml so a tenant-only body's metric keys resolve against the
// inherited defaults — identical to GET /{id} and POST /{id}/validate
// (ADR-024 PR4 / #704). Writes to configDir/{id}.yaml and commits to git.
//
// v2.5.0 Phase C: Domain policy enforcement — writes that violate the
// tenant's domain policy return 403 with details.
//
// #2295: the receivers the body writes in `_routing` are checked against the
// receiver contract (pkg/receiverspec, gitops.ReceiverPreflight — the check
// POST /{id}/validate runs too), so a receiver Alertmanager could not load is
// a 400 INVALID_BODY with violations, in both write modes. It runs after the
// policy gate: a receiver type the tenant's domain may not use stays a 403
// whatever its fields. It is this handler's, not the Writer's, so writes that
// change only another part of the file (custom alerts, batch) are not refused
// over a receiver already on disk. #2431: the same check refuses a
// `routes[i].match` value or `overrides[i].alertname` / `metric_group` the
// body writes that the route generator's PyYAML does not read as a string
// (routingpolicy.ValuesNotString) — only what the body writes, never a value
// inherited from `_routing_defaults` or a routing profile. #2503: so is a
// `group_by` element the body writes that is not a string as PyYAML reads
// it, is empty, repeats a label or is `...` beside other labels
// (routingpolicy.GroupByInvalid).
//
// v2.6.0 Phase C: PR-based write-back (ADR-011) — when writeMode is PR,
// creates a feature branch and PR/MR instead of direct commit.
// Supports both GitHub PRs and GitLab MRs via platform.Client interface.
//
// @Summary     Update tenant config
// @Description Validates, writes, and commits a tenant's YAML configuration.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Param       X-DA-Write-Source header string false "Attribute the PR to a non-UI write source. Allowlisted: threshold-governance (#656). Omit for tenant-manager UI."
// @Param       X-DA-Base-Hash header string false "Optimistic concurrency: the source_hash GET /tenants/{id} returned for the file this body was derived from. 409 if the file changed since. 16 lowercase hex chars; a malformed value is a 400, never ignored. Direct write-back mode only (501 in PR mode)."
// @Success     200   {object} PutTenantResponse
// @Failure     400   {object} ErrorResponse "Bad request. A receiver the body writes in _routing (receiver, overrides[].receiver, routes[].receiver) that Alertmanager could not load or the route generator would skip is code INVALID_BODY with one violations[] entry per problem (#2295; nothing written); so is a routes[].match value or overrides[].alertname / metric_group the body writes that the route generator does not read as a string, e.g. unquoted yes, 1:30 or ~ (#2431; quote it), and a group_by entry the body writes (group_by, overrides[].group_by, routes[].group_by) that is not a string as the route generator reads it (unquoted 8, on), is empty, repeats a label or is ... beside other labels (#2503; quote or remove it), and a _routing the body writes that is neither a mapping nor a disabling string (disable / disabled / off / false), e.g. "slack", a list, null or an unquoted false / off, which the route generator reads as a boolean (#2341; to turn routing off quote it, 'off'). Also 400 when the tenant id is not a DNS-1123 label: 1-63 lowercase letters, digits and -, starting and ending with a letter or digit (ADR-035; the route generator refuses a tree that declares any other id; nothing written). Also 400 when the current tenant file cannot be parsed and the caller lacks write permission on all tenants (#2405; nothing written)"
// @Failure     403   {object} ErrorResponse "Forbidden: insufficient permissions, or the body breaks the domain policy (code POLICY_VIOLATION; nothing written). In PR write-back mode the body is judged again on the latest base branch, which this server's local copy may lag: a violation there, or a base whose _domain_policy.yaml / .yml cannot be loaded, is the same 403 with tenant_id, and no PR/MR or branch is left"
// @Failure     409   {object} ErrorResponse "Conflict: base hash mismatch, pending PR, ambiguous tenant file, the tenant is already declared by another conf.d file (code TENANT_DECLARED_ELSEWHERE; nothing written), or the tenant's conf.d file is not a regular file (code TENANT_CONFIG_NOT_LOADABLE, config_error not_regular_file; nothing written)"
// @Failure     500   {object} ErrorResponse
// @Failure     501   {object} ErrorResponse
// @Failure     503   {object} ErrorResponse
// @Router      /api/v1/tenants/{id} [put]
func PutTenant(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateWritableTenantID(tenantID); err != nil {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}
		// ADR-027 / LD-6 P4b: org-scope-aware write gate, top of handler —
		// BEFORE the body is read, so a denied caller triggers no side
		// effect on either write path (direct commit or PR mode) and learns
		// nothing from policy-violation details.
		if !RequireOrgWrite(rw, r, d, tenantID, rbac.PermWrite) {
			return
		}
		// #2477: a tenant file that is not a regular file is refused here,
		// at once (409, config_error not_regular_file), before any write step
		// touches it. A new tenant (no file yet) and every other resolve
		// outcome go on to the Writer exactly as before.
		if existing, err := confd.ResolveTenantFile(d.ConfigDir, tenantID); err == nil {
			if errors.Is(confd.CheckRegularFile(existing), confd.ErrNotRegularFile) {
				writeTenantFileNotRegular(rw, r, tenantID)
				return
			}
		}
		email := rbac.RequestEmail(r)

		// Optimistic concurrency (opt-in). Parsed before the body is read so a
		// malformed precondition costs nothing and can never be mistaken for
		// "no precondition was asked for" — the whole point of the header is
		// that the caller wanted the write gated, so anything that stops the
		// gate from running has to be an error, not a silent unconditional
		// overwrite.
		baseHash, ok := readBaseHashHeader(rw, r)
		if !ok {
			return
		}

		body, ok := readLimitedBody(rw, r, d)
		if !ok {
			return
		}

		// #1597 follow-up: the gate above authorized against the tenant's
		// metadata AS IT IS ON DISK. This is a whole-file replace, and
		// `_metadata` lives inside the file being replaced, so the body can
		// move the tenant into an environment/domain the caller does not
		// administer. Authorize the PROPOSED state too — same predicate, same
		// flag, so shadow mode is unaffected and the gap closes exactly when
		// the axis is enforced.
		if !RequireOrgWriteProposed(rw, r, d, tenantID, rbac.PermWrite, string(body)) {
			return
		}

		// v2.5.0: Domain policy enforcement before write. #2280: the body's
		// routing is judged as the generator renders it — resolved over the
		// root's `_routing_defaults` and the referenced routing profile, every
		// receiver (main, overrides, routes) — not just a nested main-receiver
		// key. A PUT replaces the whole file, so the whole body is judged.
		// #2325: a `require_critical_escalation` leak does not block; it is
		// returned with the successful write's warnings (advisories).
		// #2486: the tenant block is laid over the root platform overlay
		// first (judgeTenantBlock), and PR mode judges the body again on the
		// fresh base (putTenantPRMode).
		var advisories []string
		if d.Policy != nil {
			violations, adv := judgePutBody(d.ConfigDir, d.Policy, tenantID, body)
			advisories = adv
			if len(violations) > 0 {
				writePolicyViolation(rw, r, violations)
				return
			}
		}

		// #2295: the receivers the body writes, after the policy gate above.
		if err := gitops.ReceiverPreflight(tenantID, string(body)); err != nil {
			writeReceiverShapeError(rw, r, err)
			return
		}

		// #2405: replacing a current file that cannot be parsed needs write
		// permission on all tenants; the Writer checks the bit under its lock,
		// on the file the write lands on (both modes).
		r = withReplaceUnparseable(r, d)

		// v2.6.0: PR-based write-back mode (ADR-011) — supports GitHub + GitLab
		if d.prWritePath() {
			// A base hash cannot mean anything here. PR mode writes on a
			// feature branch and then restores the working tree to base, so
			// the file this handler could hash is the BASE version — a second
			// write quoting the same hash would pass the check while an
			// unmerged PR already carries a change to that tenant. Refuse
			// rather than answer 200 to a request whose guarantee we did not
			// provide: silently ignoring it is worse than not supporting it,
			// because the caller cannot tell the difference.
			if baseHash != "" {
				WriteJSONError(rw, r, http.StatusNotImplemented,
					"X-DA-Base-Hash is not supported in PR write-back mode: the on-disk file "+
						"tracks the base branch, so it cannot witness a pending PR's change")
				return
			}
			putTenantPRMode(d, rw, r, tenantID, email, string(body), advisories)
			return
		}

		// Default: direct commit-on-write (ADR-009)
		var notices []string
		var err error
		if baseHash != "" {
			notices, err = d.Writer.WriteIfUnchanged(r.Context(), tenantID, email, string(body), baseHash)
		} else {
			notices, err = d.Writer.Write(r.Context(), tenantID, email, string(body))
		}
		if err != nil {
			if errors.Is(err, gitops.ErrWriteOverloaded) {
				WriteOverloaded(rw, r)
				return
			}
			// #1723: the tree was on a PR branch and nothing was written —
			// retryable, not the 400 fallback below.
			if errors.Is(err, gitops.ErrTreeNotOnBase) {
				WriteTreeNotOnBase(rw, r, err)
				return
			}
			// Same 409 + CONFLICT code as the custom-alerts base_hash check:
			// from the caller's side both mean "the file moved under you,
			// re-read and retry", and the current hash tells them what to
			// re-read against.
			var pre *gitops.PreconditionError
			if errors.As(err, &pre) {
				extra := map[string]any{
					"message": "the tenant configuration was updated by someone else; refresh and retry",
				}
				if pre.Current != "" {
					extra["current_source_hash"] = pre.Current
				}
				WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
					Error: gitops.ErrPrecondition.Error(),
					Code:  CodeConflict,
					Extra: extra,
				})
				return
			}
			if errors.Is(err, gitops.ErrConflict) {
				WriteJSONError(rw, r, http.StatusConflict, err.Error())
				return
			}
			// #1673: two files claim this tenant. The request is well-formed;
			// the on-disk state is ambiguous — 409, not the 400 fallback.
			if errors.Is(err, confd.ErrAmbiguousTenantFile) {
				WriteJSONError(rw, r, http.StatusConflict, err.Error())
				return
			}
			// #2078: the id is declared by another conf.d file → 409
			// TENANT_DECLARED_ELSEWHERE (fixed message, path only in the log).
			if writeTenantPlacementError(rw, r, err) {
				return
			}
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}

		writeJSON(rw, http.StatusOK, PutTenantResponse{
			Status:   "ok",
			TenantID: tenantID,
			Warnings: append(notices, advisories...),
		})
	}
}

// putTenantPRMode handles a single-tenant PUT in PR write-back mode (ADR-011):
// atomically claim the tenant (409 on a pending/in-flight PR), write the config
// to a feature branch, then open the PR/MR and register it. Split out of
// PutTenant (Cycle 10 refactor) to keep the handler readable — behavior is
// unchanged. The caller must have verified IsPRMode && PRClient != nil &&
// PRTracker != nil. Always writes a response. The deferred ReleaseClaim fires on
// this function's return, which is immediately before the caller returns — same
// timing as when the defer lived in the handler.
//
// advisories (#2325, non-blocking domain-policy notes) are appended to the
// response's warnings on every success path.
func putTenantPRMode(d *Deps, rw http.ResponseWriter, r *http.Request, tenantID, email, yamlContent string, advisories []string) {
	// Atomically claim the tenant. Returns false if a PR/MR is
	// already pending OR another request is mid-creation — both map
	// to 409. The claim (not the async poll cache) is what makes two
	// concurrent same-tenant writes safe; see Tracker.ClaimTenant.
	//
	// #644: if the claim fails BECAUSE the byTenant cache says a PR is
	// open (HasPendingPR is true — vs the in-flight-claim case where
	// HasPendingPR is false), the cache may be up to ~30 s stale after a
	// merge → spurious 409. Force a single bounded refresh and retry the
	// claim ONCE. The 2 s ctx stops a degraded forge from extending the
	// 409 response latency (a slower refresh continues in background and
	// populates the cache for the next request).
	claimed := d.PRTracker.ClaimTenant(tenantID)
	if !claimed && d.PRTracker.HasPendingPR(tenantID) {
		// Detached from r.Context() on purpose (#644): if the client cancelled
		// (browser close / TCP RST) r.Context() is already Done by the time we
		// get here → WithTimeout(r.Context(), …) returns an immediately-Done
		// ctx → RefreshNow would skip Sync → we'd return the stale 409 the fix
		// is meant to kill. Request-cancel-protection is NOT load-bearing here
		// (the 2 s bound + the in-background Sync continuation already cover a
		// degraded forge). context.Background() ensures the refresh actually
		// happens for the live-client case.
		refreshCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		d.PRTracker.RefreshNow(refreshCtx)
		cancel()
		claimed = d.PRTracker.ClaimTenant(tenantID)
	}
	if !claimed {
		existingPR, _ := d.PRTracker.PendingPRForTenant(tenantID)
		WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
			Error: "pending_pr_exists",
			Code:  CodePendingPR,
			Extra: map[string]any{
				"existing_pr_url": existingPR.WebURL,
				"pr_number":       existingPR.Number,
				"message":         fmt.Sprintf("A pending PR/MR for %s already exists or is being created. Merge or close it first.", tenantID),
			},
		})
		return
	}
	// Release the claim on ANY exit that isn't a successful registration —
	// every failure return AND a recovered panic. On success RegisterPR
	// clears the in-flight claim and sets byTenant (the durable dedup), so
	// this deferred release then no-ops; on failure/panic it frees the
	// tenant for retry instead of leaving a zombie 409-until-pod-restart.
	defer d.PRTracker.ReleaseClaim(tenantID)

	// Create feature branch + commit. #2486: the domain-policy check above
	// read the pod's local tree, which may lag the base the branch is cut
	// from; the body is judged again there, under the writer lock, before it
	// is written (freshBasePutPolicyCheck). A refusal drops the branch.
	var check gitops.PRBaseCheck
	if d.Policy != nil {
		check = func(configDir string) error {
			v, loadErr := freshBasePutPolicyCheck(configDir, tenantID, []byte(yamlContent))
			if loadErr != nil || len(v) > 0 {
				return &freshBasePolicyError{TenantID: tenantID, Put: true, Violations: v, LoadErr: loadErr}
			}
			return nil
		}
	}
	result, err := d.Writer.WritePRChecked(r.Context(), tenantID, email, yamlContent, check)
	if err != nil {
		// #2486: the body breaks the domain policy on the fresh base (or
		// the base's policy file cannot be loaded) — 403, as the pre-check's.
		var freshPolicy *freshBasePolicyError
		if errors.As(err, &freshPolicy) {
			writeFreshBasePolicyViolation(rw, r, freshPolicy)
			return
		}
		// The body matched the branch base, so no commit — and therefore no
		// branch and no PR/MR. That is a success, not a forge error; mirrors
		// the all-no-op batch's clean 200 (#1102). Carries the deprecation
		// notices for the same reason the batch path does.
		if errors.Is(err, gitops.ErrNoChanges) {
			var warnings []string
			if result != nil {
				warnings = result.Notices
			}
			writeJSON(rw, http.StatusOK, PutTenantResponse{
				Status:   "no_changes",
				TenantID: tenantID,
				Message:  "No changes to apply; no PR/MR created.",
				Warnings: append(warnings, advisories...),
			})
			return
		}
		// TRK-320 ErrWriteOverloaded / TRK-318 ErrForgeDegraded → canonical
		// retry-hinting 503s (shared with the batch path).
		if writeWriteFlowError(rw, r, err) {
			return
		}
		// #795 F1: malformed body is a CLIENT error → 400 (matches the
		// direct-write path), not a server 500.
		if errors.Is(err, gitops.ErrValidation) {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}
		// Anything else is an unexpected git failure → generic 500.
		WriteJSONError(rw, r, http.StatusInternalServerError, "PR write failed: "+err.Error())
		return
	}

	// Create PR/MR via platform client + register in tracker.
	// PR-6/11: shared with BatchTenants via createPRAndRegister.
	// #656: attribute the PR to its declared write source (UI by default; an
	// allowlisted X-DA-Write-Source header routes automation writes like the
	// threshold governance loop onto their own label/title/Source channel).
	ws := resolveWriteSource(r)
	prTitle := ws.titleSingle(tenantID)
	prBody := fmt.Sprintf("**Operator:** %s\n**Source:** %s\n**Tenant:** %s", email, ws.sourceLine, tenantID)
	pr, err := createPRAndRegister(d,
		prTitle, prBody, result.BranchName,
		ws.labels(),
		[]string{tenantID},
	)
	if err != nil {
		// Claim is released by the deferred ReleaseClaim above. Forbidden →
		// clean 403 (never a 500, so da-portal shows a permission error),
		// circuit-open → sanitized 503, else generic 503. Shared with batch.
		writeForgeCreateError(rw, r, d.PRClient.ProviderName(), err)
		return
	}

	writeJSON(rw, http.StatusOK, PutTenantResponse{
		Status:   "pending_review",
		TenantID: tenantID,
		PRURL:    pr.WebURL,
		PRNumber: pr.Number,
		Message:  "PR/MR created. Configuration will take effect after merge.",
		Warnings: append(result.Notices, advisories...),
	})
}

// BaseHashHeader is the request header carrying the optimistic-concurrency
// base hash for a raw-YAML tenant write. The value is the `source_hash` that
// GET /tenants/{id} reports for the file the body was derived from.
//
// It is a header rather than a body field because this endpoint's body is the
// tenant YAML document itself, committed verbatim — there is nowhere in it to
// put a protocol field. The name follows the X-DA-* convention this endpoint
// already uses for X-DA-Write-Source, and deliberately not If-Match: honouring
// If-Match would imply the rest of HTTP conditional requests (ETag on GET,
// If-None-Match, 304, weak validators), none of which this API provides.
const BaseHashHeader = "X-DA-Base-Hash"

// readBaseHashHeader returns the base hash a request is asking to be gated on,
// or "" when it asked for no gate. It writes a 400 and returns ok=false for a
// present-but-malformed value.
//
// Shape is enforced rather than pattern-matched-and-shrugged-at: a typo'd or
// truncated hash can never equal a real one, so accepting it would turn every
// write into a 409 the caller cannot explain, and accepting-then-ignoring it
// would hand back an unconditional overwrite under the name of a precondition.
// 400 says exactly which of the two happened.
func readBaseHashHeader(rw http.ResponseWriter, r *http.Request) (string, bool) {
	// Absent and present-but-blank are NOT the same request. `Header.Get`
	// flattens both to "", which would send `X-DA-Base-Hash:` (or a value of
	// only spaces) down the unconditional-write path — a caller who asked for
	// the gate silently not getting one, the exact failure this header exists
	// to prevent. Only a header that is not there at all means "no gate".
	values, present := r.Header[textproto.CanonicalMIMEHeaderKey(BaseHashHeader)]
	if !present || len(values) == 0 {
		return "", true
	}
	raw := strings.TrimSpace(r.Header.Get(BaseHashHeader))
	if !isSourceHash(raw) {
		WriteJSONError(rw, r, http.StatusBadRequest,
			BaseHashHeader+" must be a source_hash: 16 lowercase hex characters, "+
				"as returned by GET /api/v1/tenants/{id}")
		return "", false
	}
	return raw, true
}

// isSourceHash reports whether s has the shape cfg.ComputeSourceHash produces:
// SHA-256 rendered with %x and truncated to 16 chars, so lowercase hex only.
func isSourceHash(s string) bool {
	if len(s) != 16 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

// extractPatchKeys extracts flat key-value pairs from a tenant YAML body.
// Converts nested structures like _routing.receiver.type into flat keys.
// Also preserves the original key names for flat-format patches (e.g.,
// "_routing_receiver_type" used by batch operations).
func extractPatchKeys(body []byte, tenantID string) map[string]string {
	result := make(map[string]string)
	var raw struct {
		Tenants map[string]map[string]interface{} `yaml:"tenants"`
	}
	if err := yaml.Unmarshal(body, &raw); err != nil {
		return result
	}
	tenant, ok := raw.Tenants[tenantID]
	if !ok {
		return result
	}
	for k, v := range tenant {
		switch val := v.(type) {
		case string:
			result[k] = val
		case map[string]interface{}:
			// Flatten nested maps (e.g., _routing.receiver.type)
			flattenMap(k, val, result)
		default:
			result[k] = fmt.Sprintf("%v", val)
		}
	}
	return result
}

// flattenMap recursively flattens a nested map into dot-separated keys.
// maxDepth prevents stack overflow from maliciously nested YAML payloads.
func flattenMap(prefix string, m map[string]interface{}, out map[string]string) {
	flattenMapDepth(prefix, m, out, 0)
}

func flattenMapDepth(prefix string, m map[string]interface{}, out map[string]string, depth int) {
	if depth > 100 {
		out[prefix] = fmt.Sprintf("<nested too deep: %d levels>", depth)
		return
	}
	for k, v := range m {
		key := prefix + "." + k
		switch val := v.(type) {
		case string:
			out[key] = val
		case map[string]interface{}:
			flattenMapDepth(key, val, out, depth+1)
		default:
			out[key] = fmt.Sprintf("%v", val)
		}
	}
}

// writePolicyViolation lives in errors.go (PR-9/11 unification).

// writeReceiverShapeError answers gitops.ReceiverPreflight's refusal of the
// body's receivers (#2295) with the canonical 400 INVALID_BODY + violations, and
// reports whether err was one.
func writeReceiverShapeError(rw http.ResponseWriter, r *http.Request, err error) bool {
	var rse *gitops.ReceiverShapeError
	if !errors.As(err, &rse) {
		return false
	}
	violations := make([]Violation, len(rse.Violations))
	for i, v := range rse.Violations {
		violations[i] = Violation{Field: v.Field, Reason: v.Reason}
	}
	WriteValidationErrors(rw, r, violations)
	return true
}
