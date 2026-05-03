package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
)

// ViewResponse is the response body for a single saved view.
type ViewResponse struct {
	ID          string            `json:"id"`
	Label       string            `json:"label"`
	Description string            `json:"description,omitempty"`
	CreatedBy   string            `json:"created_by,omitempty"`
	Filters     map[string]string `json:"filters"`
}

// ListViews handles GET /api/v1/views
//
// @Summary     List all saved views
// @Description Returns all saved filter views defined in _views.yaml.
// @Tags        views
// @Produce     json
// @Success     200 {array}  ViewResponse
// @Router      /api/v1/views [get]
func (d *Deps) ListViews() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
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
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}

// GetView handles GET /api/v1/views/{id}
//
// @Summary     Get a single saved view
// @Tags        views
// @Produce     json
// @Param       id path string true "View ID"
// @Success     200 {object} ViewResponse
// @Failure     400 {object} map[string]string
// @Failure     404 {object} map[string]string
// @Router      /api/v1/views/{id} [get]
func (d *Deps) GetView() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		viewID := chi.URLParam(r, "id")
		if err := views.ValidateViewID(viewID); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		v, ok := d.Views.GetView(viewID)
		if !ok {
			writeJSONError(w, r,http.StatusNotFound, "view not found: "+viewID)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ViewResponse{
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
// @Failure     400  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Router      /api/v1/views/{id} [put]
func (d *Deps) PutView() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		viewID := chi.URLParam(r, "id")
		if err := views.ValidateViewID(viewID); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, r,http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		var req PutViewRequest
		if err := json.Unmarshal(body, &req); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}

		// v2.8.0 issue #134 — body-content range validation.
		// Struct-tag rules (above) cover Label / Description /
		// Filters element-count; per-pair Filters value length goes
		// through validateFilterMap because validator's `dive` doesn't
		// surface the offending key in the violation field path.
		violations := validateStructTags(&req)
		violations = append(violations, validateFilterMap(req.Filters, "filters")...)
		if len(violations) > 0 {
			writeValidationErrors(w, r,violations)
			return
		}

		cfg := d.Views.Get()
		newCfg := &views.ViewsConfig{
			Views: make(map[string]views.View, len(cfg.Views)+1),
		}
		for k, v := range cfg.Views {
			newCfg.Views[k] = v
		}
		newCfg.Views[viewID] = views.View{
			Label:       req.Label,
			Description: req.Description,
			CreatedBy:   email,
			Filters:     req.Filters,
		}

		yamlBytes, err := views.MarshalConfig(newCfg)
		if err != nil {
			writeJSONError(w, r,http.StatusInternalServerError, "marshal views: "+err.Error())
			return
		}

		if err := d.Writer.WriteViewsFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, r,http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, r,http.StatusInternalServerError, err.Error())
			return
		}

		_ = d.Views.Reload()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
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
// @Failure     400 {object} map[string]string
// @Failure     404 {object} map[string]string
// @Failure     409 {object} map[string]string
// @Router      /api/v1/views/{id} [delete]
func (d *Deps) DeleteView() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		viewID := chi.URLParam(r, "id")
		if err := views.ValidateViewID(viewID); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)

		cfg := d.Views.Get()
		if _, ok := cfg.Views[viewID]; !ok {
			writeJSONError(w, r,http.StatusNotFound, "view not found: "+viewID)
			return
		}

		newCfg := &views.ViewsConfig{
			Views: make(map[string]views.View, len(cfg.Views)),
		}
		for k, v := range cfg.Views {
			if k != viewID {
				newCfg.Views[k] = v
			}
		}

		yamlBytes, err := views.MarshalConfig(newCfg)
		if err != nil {
			writeJSONError(w, r,http.StatusInternalServerError, "marshal views: "+err.Error())
			return
		}

		if err := d.Writer.WriteViewsFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, r,http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, r,http.StatusInternalServerError, err.Error())
			return
		}

		_ = d.Views.Reload()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":  "ok",
			"view_id": viewID,
		})
	}
}

// ViewIDFromPath extracts the view ID from the URL for middleware.
var ViewIDFromPath = func(r *http.Request) string {
	return chi.URLParam(r, "id")
}
