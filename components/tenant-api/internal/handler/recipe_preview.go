package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/recipepreview"
)

// RecipePreviewer is the recipe-preview container's client (ADR-027 §5) as
// the handler uses it; *recipepreview.Client in production.
type RecipePreviewer interface {
	Preview(ctx context.Context, req recipepreview.Request) (json.RawMessage, error)
}

// previewAnswerMargin is how much of the request's time the handler keeps
// for itself: the container's call ends this long before the request's
// deadline, so the handler still answers with the container's outcome and
// not the timeout middleware's 504.
const previewAnswerMargin = time.Second

// RecipePreviewRequest is the body of POST /api/v1/tenants/{id}/recipe-preview.
// The tenant is the path's: the one the caller was authorized to read.
type RecipePreviewRequest struct {
	// Recipe is one ADR-024 recipe, as the portal's recipe form writes it.
	Recipe map[string]any `json:"recipe" swaggertype:"object"`
	// Scenario is the synthetic input, e.g. {"value": 1500}.
	Scenario map[string]any `json:"scenario,omitempty" swaggertype:"object"`
}

// RecipePreviewState is one severity's verdict.
type RecipePreviewState struct {
	Severity string `json:"severity,omitempty"`
	Mode     string `json:"mode,omitempty"`
	// State is firing, inactive or error.
	State  string `json:"state"`
	Reason string `json:"reason"`
}

// RecipePreviewResponse is the would-fire verdict, as the recipe-preview
// container writes it (passed through unchanged).
type RecipePreviewResponse struct {
	Alertname *string              `json:"alertname"`
	Supported bool                 `json:"supported"`
	States    []RecipePreviewState `json:"states"`
	Warnings  []string             `json:"warnings"`
}

// PreviewRecipe handles POST /api/v1/tenants/{id}/recipe-preview.
//
// ADR-027 §5: the browser asks tenant-api, the route's read middleware
// authorizes the caller on {id}, and only then is the recipe-preview
// container asked, over the pod-internal socket, with the request's time
// left less previewAnswerMargin. The container reads no identity.
//
// @Summary     Would-fire preview of one recipe for a tenant
// @Description Answers whether one ADR-024 recipe would fire for a scenario value, through the platform's own compiler and promtool, in the recipe-preview container of tenant-api's pod. Needs read permission on the tenant. 429 PREVIEW_BUSY (reason busy, deadline or no_time) and 503 PREVIEW_UNAVAILABLE carry Retry-After; 503 PREVIEW_NOT_CONFIGURED means the deployment has no recipe-preview container.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id   path     string               true "Tenant ID"
// @Param       body body     RecipePreviewRequest true "Recipe and scenario"
// @Success     200  {object} RecipePreviewResponse
// @Failure     400  {object} ErrorResponse
// @Failure     401  {object} ErrorResponse
// @Failure     403  {object} ErrorResponse
// @Failure     429  {object} ErrorResponse
// @Failure     503  {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/recipe-preview [post]
func PreviewRecipe(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := chi.URLParam(r, "id")
		// The "*"-grant RBAC path also authorizes an EMPTY id, which is not a
		// tenant: refuse it before asking anybody.
		if err := ValidateTenantID(id); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		if d == nil || d.RecipePreview == nil {
			WriteKind(w, r, errkind.PreviewNotConfigured,
				"recipe preview is not configured on this deployment")
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}
		var in struct {
			Recipe   json.RawMessage `json:"recipe"`
			Scenario json.RawMessage `json:"scenario"`
		}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&in); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if !nonEmptyObject(in.Recipe) {
			WriteJSONError(w, r, http.StatusBadRequest, "`recipe` (a non-empty object) is required")
			return
		}
		if len(in.Scenario) > 0 && !isObject(in.Scenario) {
			WriteJSONError(w, r, http.StatusBadRequest, "`scenario` must be an object")
			return
		}

		ctx := r.Context()
		if dl, ok := ctx.Deadline(); ok {
			var cancel context.CancelFunc
			ctx, cancel = context.WithDeadline(ctx, dl.Add(-previewAnswerMargin))
			defer cancel()
		}
		verdict, err := d.RecipePreview.Preview(ctx, recipepreview.Request{
			Recipe: in.Recipe, Tenant: id, Scenario: in.Scenario,
		})
		if err != nil {
			if !WriteKindError(w, r, err, "recipe preview: ",
				errkind.PreviewBusy, errkind.PreviewRateLimited, errkind.PreviewRejected, errkind.PreviewUnavailable) {
				WriteJSONError(w, r, http.StatusInternalServerError, "recipe preview failed")
			}
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(verdict)
		_, _ = w.Write([]byte("\n"))
	}
}

func isObject(raw json.RawMessage) bool {
	var m map[string]json.RawMessage
	return json.Unmarshal(raw, &m) == nil && m != nil
}

func nonEmptyObject(raw json.RawMessage) bool {
	var m map[string]json.RawMessage
	return json.Unmarshal(raw, &m) == nil && len(m) > 0
}
