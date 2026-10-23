package handler

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/views"
)

// viewsEntity is the state-record entity of a saved-views write: the change
// event's tenant_id, which eventVisible keys the views read bar off.
const viewsEntity = "views"

// requireViewsAccess is the saved-views bar (#3160), checked first in every
// views handler, in the handler like the federation PUT / backfill bars. A
// view belongs to no tenant and no org — the handlers return and overwrite
// every view as stored — so the bar is a NON-org-scoped "*" rule
// (rbac.PlatformNonOrgScoped) at want: read for GET, write for PUT/DELETE.
// Reads also pass in open mode (readsViews); a write never does, as open mode
// grants read only. An org-scoped "*" rule fails it in enforce and shadow mode
// alike. A Deps without an RBAC manager is a test-only state (see
// requireOrgWriteWithMeta) and is let through. Returns false after writing the
// 403.
func requireViewsAccess(w http.ResponseWriter, r *http.Request, d *Deps, want rbac.Permission) bool {
	if d.RBAC == nil {
		return true
	}
	p := rbac.RequestPrincipal(r)
	if want == rbac.PermRead {
		if readsViews(d.RBAC, p) {
			return true
		}
		WriteJSONErrorWithCode(w, r, http.StatusForbidden, errcode.Forbidden,
			"platform read permission (non-org-scoped) required to read saved views")
		return false
	}
	if d.RBAC.PlatformNonOrgScoped(p, want) {
		return true
	}
	WriteJSONErrorWithCode(w, r, http.StatusForbidden, errcode.Forbidden,
		"platform write permission (non-org-scoped) required to edit saved views")
	return false
}

// ViewResponse is the response body for a single saved view.
//
// `id`, `label` and `filters` have no omitempty, so every view carries them;
// binding:"required" (read by swag only — nothing binds a response at run
// time) declares that in the OpenAPI schema, so a portal fixture without the
// id it keys apply / delete on fails apiFixture (#3231).
type ViewResponse struct {
	ID          string            `json:"id" binding:"required"`
	Label       string            `json:"label" binding:"required"`
	Description string            `json:"description,omitempty"`
	CreatedBy   string            `json:"created_by,omitempty"`
	Filters     map[string]string `json:"filters" binding:"required"`
}

// ListViews handles GET /api/v1/views
//
// @Summary     List all saved views
// @Description Returns all saved filter views defined in _views.yaml.
// @Description Requires read on "*" from a rule with no org-scope (open mode
// @Description reads them too).
// @Tags        views
// @Produce     json
// @Success     200 {array}  ViewResponse
// @Failure     403 {object} ErrorResponse
// @Router      /api/v1/views [get]
func ListViews(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !requireViewsAccess(w, r, d, rbac.PermRead) {
			return
		}
		list := d.Views.ListViews()
		resp := make([]ViewResponse, 0, len(list))
		for _, v := range list {
			resp = append(resp, ViewResponse{
				ID:          v.ID,
				Label:       v.Label,
				Description: v.Description,
				CreatedBy:   v.CreatedBy,
				Filters:     v.Filters,
			})
		}
		writeJSON(w, http.StatusOK, resp)
	}
}

// GetView handles GET /api/v1/views/{id}
//
// @Summary     Get a single saved view
// @Tags        views
// @Produce     json
// @Param       id path string true "View ID"
// @Success     200 {object} ViewResponse
// @Failure     400 {object} ErrorResponse
// @Failure     403 {object} ErrorResponse
// @Failure     404 {object} ErrorResponse
// @Router      /api/v1/views/{id} [get]
func GetView(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !requireViewsAccess(w, r, d, rbac.PermRead) {
			return
		}
		viewID := chi.URLParam(r, "id")
		if err := views.ValidateViewID(viewID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		v, ok := d.Views.GetView(viewID)
		if !ok {
			WriteJSONError(w, r, http.StatusNotFound, "view not found: "+viewID)
			return
		}

		writeJSON(w, http.StatusOK, ViewResponse{
			ID:          viewID,
			Label:       v.Label,
			Description: v.Description,
			CreatedBy:   v.CreatedBy,
			Filters:     v.Filters,
		})
	}
}

// PutViewRequest is the body for PUT /api/v1/views/{id}.
//
// `Filters` per-key value-length checks live in
// `body_validator.go::validateFilterMap` (struct tags can't render
// the offending key in the violation `field` path).
type PutViewRequest struct {
	Label       string            `json:"label" validate:"required,min=1,max=256"`
	Description string            `json:"description" validate:"max=1024"`
	Filters     map[string]string `json:"filters" validate:"required,min=1,max=20"`
}

// PutView handles PUT /api/v1/views/{id}
//
// Creates or updates a saved view. The creator's email is recorded.
//
// @Summary     Create or update a saved view
// @Tags        views
// @Accept      json
// @Produce     json
// @Param       id   path     string         true "View ID"
// @Param       body body     PutViewRequest true "View definition"
// @Success     200  {object} map[string]string
// @Failure     400  {object} ErrorResponse
// @Failure     403  {object} ErrorResponse
// @Failure     409  {object} ErrorResponse
// @Failure     413  {object} ErrorResponse
// @Failure     500  {object} ErrorResponse
// @Failure     503  {object} ErrorResponse
// @Failure     507  {object} ErrorResponse
// @Router      /api/v1/views/{id} [put]
func PutView(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !requireViewsAccess(w, r, d, rbac.PermWrite) {
			return
		}
		viewID := chi.URLParam(r, "id")
		if err := views.ValidateViewID(viewID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)

		body, ok := readLimitedBody(w, r, d)
		if !ok {
			return
		}

		var req PutViewRequest
		if err := json.Unmarshal(body, &req); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}

		// v2.8.0 issue #134 — body-content range validation.
		// Struct-tag rules (above) cover Label / Description /
		// Filters element-count; per-pair Filters value length goes
		// through validateFilterMap because validator's `dive` doesn't
		// surface the offending key in the violation field path.
		violations := ValidateStructTags(&req)
		violations = append(violations, validateFilterMap(req.Filters, "filters")...)
		if len(violations) > 0 {
			WriteValidationErrors(w, r, violations)
			return
		}

		// Rebuild from the STORED copy, inside the state store's read-modify-write (#2856) — see the
		// comment in PutGroup for why the in-memory snapshot cannot be the
		// base of a whole-file rewrite. _views.yaml has exactly the same
		// shape: one shared object, one snapshot that refreshes only on a
		// Reload (mutateState's, after a write that succeeded or may have
		// — #1535) or a WatchLoop tick.
		if err := d.mutateState(r.Context(), statestore.KeyViews, viewsEntity, email,
			func(current []byte) ([]byte, error) {
				cfg, perr := parseViewsFile(current)
				if perr != nil {
					return nil, perr
				}
				cfg.Views[viewID] = views.View{
					Label:       req.Label,
					Description: req.Description,
					CreatedBy:   email,
					Filters:     req.Filters,
				}
				return views.MarshalConfig(cfg)
			}); err != nil {
			writeConfigFileError(w, r, err)
			return
		}

		writeJSON(w, http.StatusOK, map[string]string{
			"status":  "ok",
			"view_id": viewID,
		})
	}
}

// DeleteView handles DELETE /api/v1/views/{id}
//
// @Summary     Delete a saved view
// @Tags        views
// @Produce     json
// @Param       id path string true "View ID"
// @Success     200 {object} map[string]string
// @Failure     400 {object} ErrorResponse
// @Failure     403 {object} ErrorResponse
// @Failure     404 {object} ErrorResponse
// @Failure     409 {object} ErrorResponse
// @Failure     500 {object} ErrorResponse
// @Failure     503 {object} ErrorResponse
// @Failure     507 {object} ErrorResponse
// @Router      /api/v1/views/{id} [delete]
func DeleteView(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !requireViewsAccess(w, r, d, rbac.PermWrite) {
			return
		}
		viewID := chi.URLParam(r, "id")
		if err := views.ValidateViewID(viewID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)

		cfg := d.Views.Get()
		if _, ok := cfg.Views[viewID]; !ok {
			WriteJSONError(w, r, http.StatusNotFound, "view not found: "+viewID)
			return
		}

		// In-lock rebuild, and an already-absent target is a no-op success —
		// same reasoning as DeleteGroup.
		if err := d.mutateState(r.Context(), statestore.KeyViews, viewsEntity, email,
			func(current []byte) ([]byte, error) {
				vcfg, perr := parseViewsFile(current)
				if perr != nil {
					return nil, perr
				}
				if _, present := vcfg.Views[viewID]; !present {
					return nil, nil
				}
				delete(vcfg.Views, viewID)
				return views.MarshalConfig(vcfg)
			}); err != nil {
			writeConfigFileError(w, r, err)
			return
		}

		writeJSON(w, http.StatusOK, map[string]string{
			"status":  "ok",
			"view_id": viewID,
		})
	}
}

// ViewIDFromPath extracts the view ID from the URL for middleware.
var ViewIDFromPath = func(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// parseViewsFile decodes _views.yaml bytes as read inside the state store's read-modify-write.
// Missing file → empty start; unparseable existing file → error, never an
// empty start (see parseGroupsFile for why that distinction matters).
func parseViewsFile(current []byte) (*views.ViewsConfig, error) {
	if len(current) == 0 {
		return &views.ViewsConfig{Views: make(map[string]views.View)}, nil
	}
	if err := requireTopLevelKey(current, "views"); err != nil {
		return nil, &configFileParseError{fmt.Errorf("read current _views.yaml: %w", err)}
	}
	cfg, err := views.ParseConfig(current)
	if err != nil {
		return nil, &configFileParseError{fmt.Errorf("read current _views.yaml: %w", err)}
	}
	if cfg.Views == nil {
		cfg.Views = make(map[string]views.View)
	}
	return cfg, nil
}
