package handler

// ============================================================
// PUT /api/v1/tenants/{id}/custom-alerts — v2.9.0 (ADR-024 §S6b-2, #741)
// ============================================================
//
// Server-side, comment-preserving write of a tenant's `_custom_alerts`
// list. The portal RecipeBuilder modal sends the desired full recipe
// array as JSON; the server merges it into the (human-authored)
// tenant.yaml via yaml.Node AST surgery (preserving comments — Reef 1),
// validates with the same S5 Go validator as every other write boundary,
// and commits via the existing gitops.Writer.
//
// Why a sub-resource endpoint (not the full PUT /{id}): PUT is full-
// overlay and the portal has no YAML serializer, so the client would
// have to reconstruct + re-serialise the whole tenant doc (the blocker).
// Here the client sends only a JSON array; the server owns the robust
// YAML round-trip. See ADR-024 §S6b-2.

import (
	"encoding/json"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/customalerts"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// PutCustomAlertsRequest is the body of PUT /tenants/{id}/custom-alerts.
type PutCustomAlertsRequest struct {
	// CustomAlerts is the desired FULL recipe list (collection-replace —
	// the client owns the array, having loaded it via GET). A POINTER so an
	// ABSENT field (nil) is rejected — a truncated/buggy request must NOT
	// be read as "delete every recipe" (self-review F1, a data-loss
	// footgun). An explicit empty array `[]` IS the intentional delete-all
	// (Reef 2).
	CustomAlerts *[]map[string]any `json:"custom_alerts"`
	// BaseHash is the source_hash the client got from GET. REQUIRED —
	// optimistic concurrency is the safe default, not opt-in: an empty
	// base_hash would silently disable clobber protection (self-review F2).
	BaseHash string `json:"base_hash"`
}

// PutCustomAlertsResponse is the success body.
type PutCustomAlertsResponse struct {
	Status     string `json:"status"`
	TenantID   string `json:"tenant_id"`
	SourceHash string `json:"source_hash"` // new hash post-write (for the next edit)
	// Warnings carries non-blocking advisories for a write that SUCCEEDED —
	// the #1231 deprecated-key alias notices from the shared write-boundary
	// validation. They concern the tenant file's THRESHOLD keys (the write
	// re-validates the whole merged document), not the recipes being saved:
	// this endpoint is the portal's only tenant-file write path, so it is
	// where a mysql_cpu-era file gets its author-facing migration signal.
	Warnings []string `json:"warnings,omitempty"`
}

// PutTenantCustomAlerts handles PUT /api/v1/tenants/{id}/custom-alerts.
//
// @Summary     Replace a tenant's custom-alert recipes
// @Description Merges the supplied recipe array into the tenant's
// @Description `_custom_alerts` (comment-preserving AST edit), validates
// @Description (S5 Go validator), and commits. Optimistic concurrency via
// @Description base_hash (409 on drift). Empty array deletes the key.
// @Description 409 TENANT_CONFIG_NOT_LOADABLE config_error not_round_trippable when re-encoding the tenant file would change content other than _custom_alerts (#2903; the message names the first difference inside the tenant's own block; nothing written).
// @Description 409 TENANT_CONFIG_NOT_LOADABLE (with tenant_id, config_error) when the tenant's file cannot be loaded as a
// @Description tenant config (malformed_yaml | invalid_config, as on GET): repair the tenant file itself first, e.g. with a
// @Description whole-file PUT /api/v1/tenants/{id}, which replaces it (see GET /api/v1/tenants/{id} for the limits of that
// @Description repair, e.g. a broken file shared with other tenants). Also 409 TENANT_CONFIG_NOT_LOADABLE with config_error
// @Description not_regular_file when the tenant's conf.d file is not a regular file: nothing is read or written, and the
// @Description whole-file PUT refuses it too, so replace it with a regular file in git.
// @Description 409 TENANT_TREE_UNREADABLE as on PUT /api/v1/tenants/{id} when conf.d holds an unresolvable symlink (dangling, looping, or with a non-directory in its path) other
// @Description than the tenant's file, 500 TENANT_STORE_FAULT when tenant-api's copy holds an entry the server cannot
// @Description read (#2939; nothing written); 409 TENANT_CONFIG_NOT_LOADABLE config_error unreadable when the tenant's
// @Description own file cannot be read.
// @Description 422 TENANT_FILE_WITHHELD when the tenant's file also declares a tenant the caller may not read, or its
// @Description tenants cannot be listed with certainty and the caller lacks read permission on all tenants (#3050): GET
// @Description withholds that file's custom_alerts and source_hash (raw_yaml_withheld), so no edit can be based on them;
// @Description nothing written. A 409 CONFLICT carries current_source_hash only for a file GET would show the caller.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id    path     string                 true "Tenant ID"
// @Param       body  body     PutCustomAlertsRequest true "Desired recipe list + base_hash"
// @Success     200   {object} PutCustomAlertsResponse
// @Failure     400   {object} ErrorResponse
// @Failure     403   {object} ErrorResponse
// @Failure     404   {object} ErrorResponse "Not found: no tenant file, or the tenant file is a symlink that cannot be resolved (dangling, looping — a self-loop included — or through a non-directory): it has no content to merge recipes into, so there is no tenant here (a whole-file PUT /api/v1/tenants/{id} replaces such a link)"
// @Failure     409   {object} ErrorResponse "Conflict: base_hash drift (CONFLICT), the tenant file cannot be loaded or is not a regular file (TENANT_CONFIG_NOT_LOADABLE), the tenant is declared by another conf.d file (TENANT_DECLARED_ELSEWHERE), or code TENANT_TREE_UNREADABLE when conf.d holds an unresolvable symlink (dangling, looping, or with a non-directory in its path) other than the file this write replaces (#2939, ADR-036 §4: the tree after the write is judged; repair it in git; nothing written; unreadable_count always, the entries' names and reasons in the text and in unreadable only for a caller with write permission on all tenants). The tenant's own file being unreadable in any other way (a failed read, a FIFO) is TENANT_CONFIG_NOT_LOADABLE (config_error unreadable or not_regular_file) for every caller: it is never replaced"
// @Failure     413   {object} ErrorResponse
// @Failure     422   {object} ErrorResponse "TENANT_FILE_WITHHELD (#3050: the tenant file declares a tenant the caller may not read; nothing written). Or refused by the route generator container (#2903, ADR-036 §4): code GENERATOR_FINDINGS — the conf.d tree with this write applied does not pass `generate-routes --validate --strict`; findings[] lists the generator's findings, the ones naming this write's file or tenant first (related: true), pre_existing set when findings_compared_with_base is true (the tree before the write was judged too); a finding unrelated to this write is shown only to a caller with write permission on all tenants (others get unrelated_findings_withheld). Or code GENERATOR_REJECTED with reason memory / incomplete / too_large (the files the write produces are too large to judge in one request) / invalid_request: the generator gave no verdict; or normalized: git attributes (.gitattributes eol, text or a filter) would commit other bytes than the ones the generator judged. Nothing written"
// @Failure     429   {object} ErrorResponse "Too many requests: code GENERATOR_REJECTED (#2903) — the route generator container gave no verdict this time, retry (Retry-After): reason busy, deadline, git_read, mismatch (what it judged is still not what would be written after one re-judgement) or no_time (too little of the request's time left to judge). Nothing written. Also code RATE_LIMITED (per-caller rate limit)"
// @Failure     500   {object} ErrorResponse "Internal error; or code TENANT_STORE_FAULT (#2939): tenant-api's own copy of conf.d holds an entry the server cannot read and git cannot hold — a FIFO, socket or device named like a config file, a file whose read fails, a sub-directory that cannot be listed, a failed stat that is not an unresolvable link — other than the written tenant file; it is fixed on the server or its volume (or by resetting the copy from git), not by a write; wins over TENANT_TREE_UNREADABLE; nothing written; entry names disclosed as for TENANT_TREE_UNREADABLE"
// @Failure     501   {object} ErrorResponse
// @Failure     503   {object} ErrorResponse "Service unavailable: the write plane is busy (WRITE_OVERLOADED), the forge is degraded (FORGE_UNAVAILABLE) or the config worktree is not on the base branch (TREE_NOT_ON_BASE). Also, for every write that changes a conf.d file (#2903, ADR-036 §5): code VALIDATOR_UNAVAILABLE when the route generator container cannot judge (reason unreachable, bad_response, amtool_missing, generator_too_old, internal_error), or POLICY_UNAVAILABLE when its deployment policy file is unusable (reason policy_unusable, or policy_unreadable for tenant-api's own copy); Retry-After; nothing written"
// @Router      /api/v1/tenants/{id}/custom-alerts [put]
func PutTenantCustomAlerts(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateWritableTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// ADR-027 / LD-6 P4b: org-scope-aware write gate — authorization
		// comes BEFORE feature availability (the PR-mode 501 below), so an
		// unauthorized caller cannot probe the server's write-mode.
		if !RequireOrgWrite(w, r, d, tenantID, rbac.PermWrite) {
			return
		}

		// MVP scope (ADR-024 §S6b-2): the in-UI write targets DIRECT
		// write-back mode. In PR/MR mode the recipe author uses the
		// standalone recipe-builder + their normal PR flow (S6b-1).
		if d.WriteMode.IsPRMode() {
			WriteJSONError(w, r, http.StatusNotImplemented,
				"custom-alert editing via the UI is not yet supported in PR write-back mode; "+
					"use the standalone recipe builder and your GitOps PR flow")
			return
		}

		body, ok := readLimitedBody(w, r, d)
		if !ok {
			return
		}
		var req PutCustomAlertsRequest
		if err := json.Unmarshal(body, &req); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		// F1: an absent/null `custom_alerts` must not be read as delete-all.
		if req.CustomAlerts == nil {
			WriteJSONError(w, r, http.StatusBadRequest,
				"custom_alerts is required (send an explicit array; [] deletes all)")
			return
		}
		// F2: base_hash is required — optimistic concurrency is the safe
		// default. Clients GET the tenant (which returns source_hash) first.
		if req.BaseHash == "" {
			WriteJSONError(w, r, http.StatusBadRequest,
				"base_hash is required; GET the tenant first and echo its source_hash")
			return
		}
		// #1017: quantile must arrive as a JSON STRING. A number has already
		// lost its authored text and would be merged into conf.d as a bare
		// YAML scalar, which the Go exporter and the Python compiler can read
		// differently (recipe_id parity → silently muted alert). Fail loud
		// before merging — symmetric with the conf.d schema CI gate, which
		// would reject the committed file anyway (schema type: string).
		if viol := customalerts.QuantileStringViolations(*req.CustomAlerts); len(viol) > 0 {
			violations := make([]Violation, 0, len(viol))
			for _, v := range viol {
				violations = append(violations, Violation{Field: "_custom_alerts", Reason: v})
			}
			WriteValidationErrors(w, r, violations)
			return
		}

		// Load the current tenant file (must exist — recipes are authored
		// against an existing tenant). #1673: resolve the tenant's actual
		// file instead of assuming the `.yaml` spelling.
		filePath, err := confd.ResolveTenantFile(d.ConfigDir, tenantID)
		switch {
		case errors.Is(err, confd.ErrTenantFileNotFound):
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		case errors.Is(err, confd.ErrAmbiguousTenantFile):
			WriteJSONError(w, r, http.StatusConflict, err.Error())
			return
		case err != nil:
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		// #2477: a tenant file that is not a regular file is refused at once
		// (409, config_error not_regular_file) instead of being read — before
		// the tree is judged, exactly as PUT /tenants/{id} refuses it.
		if errors.Is(confd.CheckRegularFile(filePath), confd.ErrNotRegularFile) {
			writeTenantFileNotRegular(w, r, tenantID)
			return
		}
		// #2939 R4-N3: the Writer's verdict on the tree BEFORE this handler
		// reads the file, so PUT, /diff and this endpoint answer one tree the
		// same way — a store fault elsewhere before the file's own
		// unreadability (TENANT_CONFIG_NOT_LOADABLE), an unresolvable link
		// elsewhere, a declaration elsewhere. The write is judged again
		// under the lock.
		if d.Writer != nil {
			if err := d.Writer.JudgeWrite(tenantID); err != nil {
				if !writeTenantPlacementError(w, r, d, err) {
					WriteJSONError(w, r, http.StatusInternalServerError, writeErrorText(r, err))
				}
				return
			}
		}
		// gitops.ReadTenantFile: an unresolvable symlink (dangling, looping)
		// reads as missing — there is no tenant to merge recipes into.
		raw, err := gitops.ReadTenantFile(filePath)
		if errors.Is(err, fs.ErrNotExist) {
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if errors.Is(err, confd.ErrNotRegularFile) {
			writeTenantFileNotRegular(w, r, tenantID)
			return
		}
		if err != nil {
			// The judgement above refuses a tenant file that cannot be read
			// (TargetUnreadableError), so this is a file that broke after it
			// — a server-side failure, not a classification of the file.
			WriteJSONError(w, r, http.StatusInternalServerError, writeErrorText(r, err))
			return
		}

		// #2373 review F1: a file threshold-exporter rejects is answered by
		// GET without custom_alerts, so a client's list was not built from
		// the file's real recipes — merging it in would replace them. Refused
		// before the base_hash check: a matching hash proves only that the
		// client read THIS file, not that it saw its recipes. The in-lock
		// WriteIfUnchanged compares the same bytes, so `raw` is the base.
		var notLoadable *tenantFileNotLoadableError
		if err := checkPartialWriteBase(tenantID, raw); errors.As(err, &notLoadable) {
			writeTenantFileNotLoadable(w, r, notLoadable)
			return
		}
		// #3050: GET withholds this file's custom_alerts and source_hash
		// from the caller (it declares a tenant the caller may not read), so
		// no edit of the caller's can be based on them — and the conflict
		// answer below would hand out the hash GET withholds.
		if !fileShown(r, d, raw, tenantID) {
			WriteJSONErrorWithCode(w, r, http.StatusUnprocessableEntity, CodeTenantFileWithheld,
				"the tenant file also declares a tenant you may not read (or its tenants cannot be listed with "+
					"certainty), so GET /api/v1/tenants/{id} withholds its custom_alerts and source_hash and no "+
					"edit can be based on them; nothing was written. Edit the file in git")
			return
		}

		// Optimistic concurrency (Reef 3): reject if the file moved under us.
		// This is the CHEAP check — it runs outside the writer's lock, so it
		// can only catch a base that was already stale when the request
		// arrived. It exists to reject those before they consume a write
		// admission slot; the authoritative comparison happens inside the lock
		// in Writer.WriteIfUnchanged below, which is what closes the read→write
		// window this comment previously recorded as a remaining TOCTOU (F4).
		//
		// The two checks share one base: `raw` is both what this hash is
		// computed over and what `merged` is built from, so the in-lock check
		// passing proves the merge base was still current at commit time.
		currentHash := cfg.ComputeSourceHash(raw)
		if req.BaseHash != currentHash {
			WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
				Error: "the tenant configuration was updated by someone else; refresh and retry",
				Code:  CodeConflict,
				Extra: map[string]any{"current_source_hash": currentHash},
			})
			return
		}

		// Comment-preserving AST merge (Reef 1 / Reef 2). A merge error means
		// the EXISTING tenant file is malformed (not a document / no
		// `tenants.<id>` mapping) — a server-state issue, not the client's
		// (valid JSON) input. Surface 500, not a misleading 400 (self-review
		// G4, consistent with the unparseable-merged 500 below).
		merged, err := customalerts.MergeCustomAlerts(string(raw), tenantID, *req.CustomAlerts)
		if err != nil {
			// The text describes the tenant file's content, not the
			// server (#1700): its own code, so it is not withheld.
			// #2903: the decode error's text may quote another tenant's
			// value (the file can be shared); it goes to the log only.
			slog.Warn("custom alerts: cannot merge into the tenant file", "tenant", tenantID, "error", err)
			WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode,
				"internal error: cannot merge into the tenant file (the details are in the server log)")
			return
		}
		// #2903: the re-encode must change nothing but `_custom_alerts`.
		edit := partialEdit{key: "_custom_alerts", set: len(*req.CustomAlerts) > 0}
		r = withTenantNames(r, d) // #3039: which other tenants the refusals below may name
		if err := verifyPartialWrite(raw, []byte(merged), tenantID, []partialEdit{edit}, gitops.TenantNamesOf(r.Context())); errors.As(err, &notLoadable) {
			writeTenantFileNotLoadable(w, r, notLoadable)
			return
		}

		// Pre-validate for STRUCTURED violations (Reef 4): the S5 validator
		// runs over the whole array, so a pre-existing bad recipe also
		// surfaces here — return all of them by name/index so the UI can
		// point at the offending rule, not just reject opaquely.
		var mcfg cfg.ThresholdConfig
		if err := yaml.Unmarshal([]byte(merged), &mcfg); err != nil {
			// The merge produces YAML via the encoder, so this should be
			// unreachable — if it fires, our merge logic is at fault (a
			// server bug), not the client's input. Fail-fast as 500, not a
			// misleading 400.
			// The text describes the tenant file's content, not the
			// server (#1700): its own code, so it is not withheld.
			slog.Warn("custom alerts: merged config is not parseable", "tenant", tenantID, "error", err)
			WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode,
				"internal error: merged config is not parseable (the details are in the server log)")
			return
		}
		if viol := cfg.ValidateTenantCustomAlerts(tenantID, mcfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault); len(viol) > 0 {
			violations := make([]Violation, 0, len(viol))
			for _, v := range viol {
				violations = append(violations, Violation{Field: "_custom_alerts", Reason: v})
			}
			WriteValidationErrors(w, r, violations)
			return
		}

		// B2-wide eol-expansion guard (ADR-024 §8): block writes that GROW usage
		// of an end-of-life recipe, without collateral-blocking edits to existing
		// or unrelated recipes (the outage-hostage failure mode). This handler-level
		// check returns STRUCTURED Violations (portal-friendly) and runs first; the
		// shared gitops.Writer.validate enforces the same guard for PutTenant +
		// batch full-config writes (which never pass through here), so every
		// tenant-api write path is covered.
		currentAlerts, err := customalerts.Extract(string(raw), tenantID)
		if err != nil {
			// The text describes the tenant file's content, not the
			// server (#1700): its own code, so it is not withheld.
			slog.Warn("custom alerts: cannot read current custom alerts", "tenant", tenantID, "error", err)
			WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode,
				"internal error: cannot read current custom alerts (the details are in the server log)")
			return
		}
		if viol := customalerts.EolExpansionViolations(currentAlerts, *req.CustomAlerts); len(viol) > 0 {
			violations := make([]Violation, 0, len(viol))
			for _, v := range viol {
				violations = append(violations, Violation{Field: "_custom_alerts", Reason: v})
			}
			WriteValidationErrors(w, r, violations)
			return
		}

		// Commit via the shared writer (re-validates schema + custom alerts,
		// attributes the commit to the operator).
		email := rbac.RequestEmail(r)
		// #3054: one write attempt, counted by the answer it gets.
		attempt := d.startWriteAttempt(w, r)
		defer attempt.end()
		w = attempt
		notices, err := d.Writer.WriteIfUnchanged(r.Context(), tenantID, email, merged, req.BaseHash)
		if err != nil {
			// #2903: the generator container refused it (422 / 429 / 503).
			if writeGeneratorError(w, r, d, err) {
				return
			}
			if errors.Is(err, gitops.ErrWriteOverloaded) {
				WriteOverloaded(w, r)
				return
			}
			// #1723: the tree was on a PR branch and nothing was written —
			// retryable, not the 400 fallback below.
			if errors.Is(err, gitops.ErrTreeNotOnBase) {
				WriteTreeNotOnBase(w, r, err)
				return
			}
			// The base moved between the pre-check above and the lock. Same
			// envelope as that pre-check so a client cannot tell which of the
			// two rejected it — only that it must re-read and retry.
			var pre *gitops.PreconditionError
			if errors.As(err, &pre) {
				extra := map[string]any{}
				// #3050: the file changed under the lock; its new version
				// may declare a tenant the caller may not read.
				if pre.Current != "" && tenantFileShown(r, d, tenantID, pre.Current) {
					extra["current_source_hash"] = pre.Current
				}
				WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
					Error: "the tenant configuration was updated by someone else; refresh and retry",
					Code:  CodeConflict,
					Extra: extra,
				})
				return
			}
			if errors.Is(err, gitops.ErrConflict) {
				WriteJSONError(w, r, http.StatusConflict, err.Error())
				return
			}
			// Forge degradation (TRK-318): the in-lock base fetch timed out;
			// the write never touched a stale base, so a retry is safe →
			// surface a retry-hinting 503, not a 400 (matches PutTenant).
			if errors.Is(err, gitops.ErrForgeDegraded) {
				writeForgeDegraded(w, r)
				return
			}
			// #2078: the id is also declared by another conf.d file (the
			// exporter already rejects that tree) → 409, fixed message.
			if writeTenantPlacementError(w, r, d, err) {
				return
			}
			// #1700: as PutTenant's fallback — the error's own text only
			// when it is about the request. #3054: otherwise the failure is
			// the server's, whatever the status.
			if !writeErrorIsForClient(err) {
				attempt.fault = true
			}
			WriteJSONError(w, r, http.StatusBadRequest, writeErrorText(r, err))
			return
		}

		writeJSON(w, http.StatusOK, PutCustomAlertsResponse{
			Status:     "success",
			TenantID:   tenantID,
			SourceHash: cfg.ComputeSourceHash([]byte(merged)),
			Warnings:   notices,
		})
	}
}
