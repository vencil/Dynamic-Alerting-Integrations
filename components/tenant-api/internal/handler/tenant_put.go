package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/textproto"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
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
	// closes. Never populated on error responses.
	Warnings []string `json:"warnings,omitempty"`
}

// PutTenant handles PUT /api/v1/tenants/{id}
//
// Accepts a tenant-only YAML document — the conf.d/{id}.yaml shape, a
// `tenants:` block containing the {id} section (NOT the platform defaults).
//
// #3064: only an API-managed tenant is written — one whose conf.d file is
// API-shaped (confd.TenantFileManagement: one document declaring only {id},
// under its own name, without YAML indirection where tenants are declared).
// Such a file is the tenant's alone, so the body replaces it whole and
// X-DA-Base-Hash (its source_hash) covers exactly this tenant. A git-managed
// tenant (any other shape: a file shared with other tenants, a stem that is
// not the declared id, several documents, top-level anchors) is refused,
// 409 TENANT_GIT_MANAGED, in both write modes and in the previews. The body
// must itself be API-shaped (400), so a write never turns the tenant's file
// into one tenant-api does not manage.
//
// The body is committed verbatim, so any top-level key other than `tenants`
// (e.g. a stray `defaults:` / `state_filters:` / `profiles:`, or a typo) is
// rejected with 400 — mirroring tenant-config.schema.json's
// additionalProperties:false (#705). Validation merges the on-disk
// _defaults.yaml so a tenant-only body's metric keys resolve against the
// inherited defaults — identical to GET /{id} and POST /{id}/validate
// (ADR-024 PR4 / #704). Writes to configDir/{id}.yaml and commits to git.
//
// #2903 (ADR-036 §4): routing and domain policy are judged by the route
// generator container alone — the tree with this body written is judged
// (`generate-routes --validate --strict`), and a refusal is a 422
// GENERATOR_FINDINGS. tenant-api no longer judges receivers, matcher values,
// group_by or the domain policy itself.
//
// #1560: a value the body writes that is the masked-credential placeholder
// is a 400 INVALID_BODY with violations (gitops.PlaceholderPreflight — the
// check POST /{id}/validate runs too), in both write modes.
//
// v2.6.0 Phase C: PR-based write-back (ADR-011) — when writeMode is PR,
// creates a feature branch and PR/MR instead of direct commit.
// Supports both GitHub PRs and GitLab MRs via platform.Client interface.
//
// @Summary     Update tenant config
// @Description Validates, writes, and commits a tenant's YAML configuration. The body replaces the tenant's conf.d file whole.
// @Description Only an API-managed tenant is written (#3064): one whose file (located by its name) is API-shaped — one YAML
// @Description document declaring only {id}, with no anchor, alias, merge key (<<) or tagged key at the top level or
// @Description directly under tenants: (anchors and aliases inside the tenant's own section are fine). Any other tenant is
// @Description git-managed (GET marks it managed_by: git) and every write is refused with 409 TENANT_GIT_MANAGED, in both write
// @Description modes; change it in git. The body must be API-shaped too (400), so it declares exactly tenants.{id}.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Param       X-DA-Write-Source header string false "Attribute the PR to a non-UI write source. Allowlisted: threshold-governance (#656). Omit for tenant-manager UI."
// @Param       X-DA-Base-Hash header string false "Optimistic concurrency: the source_hash GET /tenants/{id} returned for the file this body was derived from. 409 if the file changed since. 16 lowercase hex chars; a malformed value is a 400, never ignored. Direct write-back mode only (501 in PR mode)."
// @Success     200   {object} PutTenantResponse
// @Failure     400   {object} ErrorResponse "Bad request. A value the body writes that is the masked-credential placeholder is code INVALID_BODY with one violations[] entry per value (#1560; send the real value; nothing written); The flat _routing_receiver_type is a 400 BAD_REQUEST from the tenant key validation (#2903; the receiver type is _routing.receiver.type). Routing (receivers, matcher values, group_by, the domain policy) is judged by the route generator container: a refusal is a 422 GENERATOR_FINDINGS (#2903). Also 400 when the tenant id is not a DNS-1123 label: 1-63 lowercase letters, digits and -, starting and ending with a letter or digit (ADR-035; the route generator refuses a tree that declares any other id; nothing written). Also 400 when parse-tree refuses the body, the error naming its kind (#3206, ADR-036): more than one YAML document (an empty trailing --- included), a tag outside !!str !!int !!float !!bool !!null !!timestamp !!map !!seq (!!binary, !!set and local or non-specific ! tags included), a null or non-scalar key, a repeated key, a byte order mark past the start, or a syntax or decode error (nothing written). Also 400 when the body is not API-shaped (#3064): it declares a tenant besides {id}, several documents, or an anchor, alias, merge key or tagged key at the top level or directly under tenants: (nothing written). Also 400 when the current tenant file's tenants: block cannot be decoded for the end-of-life check and the caller lacks write permission on all tenants (#2405; nothing written)"
// @Failure     403   {object} ErrorResponse "Forbidden: insufficient permissions. In PR write-back mode the write scope is judged again on the latest base branch, which this server's local copy may lag: a refusal there is the same 403 with tenant_id, and no PR/MR or branch is left"
// @Failure     409   {object} ErrorResponse "Conflict: code TENANT_GIT_MANAGED (#3064) when the tenant is git-managed — its conf.d file (on the latest base branch in PR write-back mode) is not API-shaped; tenant_id and git_managed_reason (not_tenant_document, several_documents, yaml_indirection, tenant_count, id_mismatch) in the envelope, naming no other tenant; judged before the base hash; nothing written. Or base hash mismatch (current_source_hash only when the current file is API-shaped: the hash GET /tenants/{id} shows), pending PR, ambiguous tenant file, the tenant is also declared by another conf.d file (code TENANT_DECLARED_ELSEWHERE, a fixed text; nothing written), the tenant's conf.d file is not a regular file (code TENANT_CONFIG_NOT_LOADABLE, config_error not_regular_file; nothing written), or code TENANT_TREE_UNREADABLE when conf.d holds an unresolvable symlink (dangling, looping, or with a non-directory in its path) other than the file this write replaces (#2939, ADR-036 §4: the tree after the write is judged; repair it in git; nothing written; unreadable_count always, the entries' names and reasons in the text and in unreadable only for a caller with write permission on all tenants). The tenant's own file being unreadable in any other way (a failed read, a FIFO) is TENANT_CONFIG_NOT_LOADABLE (config_error unreadable or not_regular_file) for every caller: it is never replaced. A tenant file that is itself an unresolvable symlink (dangling, looping, or with a non-directory in its path) does not count: the write replaces it; or code TENANT_FILE_DIRTY (#1535, direct mode) when the tenant's conf.d file in tenant-api's copy differs from the commit the write lands on (modified, staged, deleted, untracked or marked skip-worktree / assume-unchanged outside tenant-api, or a link or the target on a symlinked tenant file's way inside the repository changed; a target outside the repository, in a nested repository or a submodule is not judged): fixed text, no path; nothing written; commit or discard the change in the repository, then retry"
// @Failure     413   {object} ErrorResponse "Payload too large: the body is over TA_MAX_BODY_BYTES (code PAYLOAD_TOO_LARGE; nothing written). The body is refused whole, never written truncated (#2778)"
// @Failure     422   {object} ErrorResponse "Refused by the route generator container (#2903, ADR-036 §4): code GENERATOR_FINDINGS — the conf.d tree with this write applied does not pass `generate-routes --validate --strict`; findings[] lists the generator's findings, the ones naming this write's file or tenant first (related: true), pre_existing set when findings_compared_with_base is true (the tree before the write was judged too); a finding unrelated to this write is shown only to a caller with write permission on all tenants (others get unrelated_findings_withheld). Or code GENERATOR_REJECTED with reason memory / incomplete / too_large (the files the write produces are too large to judge in one request) / invalid_request: the generator gave no verdict; or normalized: git attributes (.gitattributes eol, text or a filter) would commit other bytes than the ones the generator judged. Nothing written"
// @Failure     429   {object} ErrorResponse "Too many requests: code GENERATOR_REJECTED (#2903) — the route generator container gave no verdict this time, retry (Retry-After): reason busy, deadline, git_read, mismatch (what it judged is still not what would be written after one re-judgement) or no_time (too little of the request's time left to judge). Nothing written. Also code RATE_LIMITED (per-caller rate limit)"
// @Failure     500   {object} ErrorResponse "Internal error: code INTERNAL_ERROR, among others when the commit or the file write fails (#1535: a fault of the server, not of the body; the detail is in the server log under request_id); or code TENANT_STORE_FAULT (#2939): tenant-api's own copy of conf.d holds an entry the server cannot read and git cannot hold — a FIFO, socket or device named like a config file, a file whose read fails, a sub-directory that cannot be listed, a failed stat that is not an unresolvable link — other than the written tenant file; it is fixed on the server or its volume (or by resetting the copy from git), not by a write; wins over TENANT_TREE_UNREADABLE; nothing written; entry names disclosed as for TENANT_TREE_UNREADABLE"
// @Failure     501   {object} ErrorResponse
// @Failure     503   {object} ErrorResponse "Service unavailable: the write plane is busy (WRITE_OVERLOADED), the forge is degraded (FORGE_UNAVAILABLE) or the config worktree is not on the base branch (TREE_NOT_ON_BASE). Also, for every write that changes a conf.d file (#2903, ADR-036 §5): code VALIDATOR_UNAVAILABLE when the route generator container cannot judge (reason unreachable, bad_response, amtool_missing, generator_too_old, internal_error), or POLICY_UNAVAILABLE when its deployment policy file is unusable (reason policy_unusable, or policy_unreadable for tenant-api's own copy); Retry-After; nothing written. In PR/MR write-back mode, code GIT_PUSH_FAILED (#1724) when pushing the feature branch to the config repository failed, a timeout included: no PR/MR opened, Retry-After (the branch may be left on origin without a PR/MR). Also code UPSTREAM_ERROR (#1535, #3173) when the write was stopped before anything of it was committed: the request was cancelled or reached its deadline, or a git command hit its own deadline (TENANT_API_GIT_TIMEOUT) and the server read the branch back unmoved; nothing written, safe to retry. And code UPSTREAM_ERROR with another message when updating the branch failed and the server could not read the branch back: unlike the cases above it may already be committed, so refresh to see whether it was before retrying"
// @Router      /api/v1/tenants/{id} [put]
func PutTenant(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		// Optimistic concurrency (opt-in). Parsed before the body is read so
		// a malformed precondition costs nothing and can never be mistaken
		// for "no precondition was asked for" — the whole point of the
		// header is that the caller wanted the write gated, so anything that
		// stops the gate from running has to be an error, not a silent
		// unconditional overwrite.
		var baseHash string
		readBody := func(rw http.ResponseWriter, r *http.Request) (string, bool) {
			var ok bool
			if baseHash, ok = readBaseHashHeader(rw, r); !ok {
				return "", false
			}
			body, ok := readLimitedBody(rw, r, d)
			return string(body), ok
		}
		// #3062: the refusal sequence PUT shares with /diff and /validate
		// (tenant_put_plan.go): the gates, the declared tenants, the
		// placeholder. The rest runs in the Writer, on the file it replaces.
		plan, ok := planPut(rw, rw, r, d, endpointPut, readBody)
		if !ok {
			return
		}
		tenantID, body := plan.tenantID, plan.body
		r = plan.r
		email := rbac.RequestEmail(r)

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
			putTenantPRMode(d, rw, r, plan, email)
			return
		}

		// Default: direct commit-on-write (ADR-009)
		// #3054: one write attempt, counted by the answer it gets.
		attempt := d.startWriteAttempt(rw, r)
		defer attempt.end()
		rw = attempt
		var notices []string
		var err error
		if baseHash != "" {
			notices, err = d.Writer.WriteIfUnchanged(r.Context(), tenantID, email, body, baseHash)
		} else {
			notices, err = d.Writer.Write(r.Context(), tenantID, email, body)
		}
		if err != nil {
			if writePutDirectError(rw, r, d, tenantID, err) {
				attempt.fault = true
			}
			return
		}

		writeJSON(rw, http.StatusOK, PutTenantResponse{
			Status:   "ok",
			TenantID: tenantID,
			Warnings: notices,
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
func putTenantPRMode(d *Deps, rw http.ResponseWriter, r *http.Request, plan *putPlan, email string) {
	tenantID, yamlContent := plan.tenantID, plan.body
	gatePre, gatePost := plan.gatePre, plan.gatePost
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
		// #3123: the PR/MR named here must be one the caller could see on
		// GET /prs. A batch PR/MR covering tenantID also covers others; for a
		// caller who cannot read all of them the 409 stands but names no PR —
		// the same body as the in-flight-claim case, where there is none yet.
		if !prVisibleTo(d, rbac.RequestPrincipal(r), existingPR) {
			existingPR = platform.PRInfo{}
		}
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

	// #3054: from here on, one write attempt (the branch and the PR/MR),
	// counted by the answer it gets.
	attempt := d.startWriteAttempt(rw, r)
	defer attempt.end()
	rw = attempt

	// Create feature branch + commit. The checks above read the pod's local
	// tree, which may lag the base the branch is cut from, so they can only
	// refuse early (#2857). The authoritative verdict is taken there, under
	// the writer lock, before anything is written, in the same order as above:
	// the metadata write scope before and after the write (#1597). The
	// generator judges the branch point with the body written (#2903). A
	// refusal drops the branch.
	// gatePre/gatePost are the pairs the local gates judged. The reverse — a
	// tenant the local tree refuses and the base would allow — was already
	// refused by the local check: it can only refuse early. A tenant
	// git-managed on the base is refused before this check runs (#3064).
	p := rbac.RequestPrincipal(r)
	check := func(configDir string) error {
		// The pre-state check counts the org axis; when the base's tenant
		// file is unreadable it is skipped, so the post-state check does.
		pre, judged := freshWriteScope(configDir, tenantID)
		if judged {
			if ok, err := freshScopeAllowed(d, p, configDir, tenantID, gatePre, pre, true); err != nil || !ok {
				return orFreshScopeError(err, &freshBaseScopeError{TenantID: tenantID, Put: true, PreState: true})
			}
		}
		post := scopePairOf(proposedScopeMeta(configDir, yamlContent), tenantID)
		if ok, err := freshScopeAllowed(d, p, configDir, tenantID, gatePost, post, !judged); err != nil || !ok {
			return orFreshScopeError(err, &freshBaseScopeError{TenantID: tenantID, Put: true})
		}
		return nil
	}
	result, err := d.Writer.WritePRChecked(r.Context(), tenantID, email, yamlContent, check)
	if err != nil {
		if writePRPutError(rw, r, d, err) {
			return
		}
		// The body matched the branch base, so no commit — and therefore no
		// branch and no PR/MR. That is a success, not a forge error; mirrors
		// the all-no-op batch's clean 200 (#1102). Carries the deprecation
		// notices for the same reason the batch path does.
		var warnings []string
		if result != nil {
			warnings = result.Notices
		}
		writeJSON(rw, http.StatusOK, PutTenantResponse{
			Status:   "no_changes",
			TenantID: tenantID,
			Message:  "No changes to apply; no PR/MR created.",
			Warnings: warnings,
		})
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
		Warnings: result.Notices,
	})
}

// writePRPutError answers a PR-mode PUT whose WritePRChecked failed, and
// reports whether it wrote the answer. It reports false for exactly one
// error, gitops.ErrNoChanges: not a failure — the body matched the branch
// base — so the caller answers it 200 no_changes with the write's notices.
// The order of the checks is the answer's: the fresh-base scope refusals
// first (#2857), ErrNoChanges before every failure mapping.
func writePRPutError(rw http.ResponseWriter, r *http.Request, d *Deps, err error) (written bool) {
	// #2857: the write scope refuses it on the fresh base, or the base's org
	// file cannot be read; then ErrNoChanges; then the write-flow refusals,
	// #1724's push failure (503 GIT_PUSH_FAILED, no PR/MR; the deferred
	// ReleaseClaim frees the tenant for the retry), #795 F1's malformed body
	// (a CLIENT error → 400, as the direct path), #2070's base restore, and
	// #1724's cancellation (the 503 the direct path answers, #1535). No
	// ErrMergeFailed: WritePRChecked writes the body whole and never merges.
	// Anything else is an unexpected git failure → generic 500 (its text
	// stays in the log — WriteErrorEnvelope, #1700).
	written, _ = writeErr(rw, r, d, err, errCtx{
		stages: []kindSet{
			kindsFreshBase,
			kindsOf(errkind.NoChanges),
			kindsWriteFlow.union(kindsOutcome).with(errkind.GitPushFailed, errkind.Validation, errkind.BaseRestoreFailed),
		},
		fallback: fallbackInternal,
		prefix:   "PR write failed: ",
	})
	return written
}

// BaseHashHeader is the request header carrying the optimistic-concurrency
// base hash for a raw-YAML tenant write. The value is the `source_hash` that
// GET /tenants/{id} reports for the file the body was derived from.
//
// It is a header rather than a body field because this endpoint's body is the
// tenant YAML document itself (its sections written as sent) — there is nowhere in it to
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

// writePlaceholderError answers gitops.PlaceholderPreflight's refusal of the
// body (#1560) with the canonical 400 INVALID_BODY + violations, and reports
// whether err was one.
func writePlaceholderError(rw http.ResponseWriter, r *http.Request, err error) bool {
	var rse *gitops.PlaceholderError
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
