package federation

// Tenant log visibility handlers (ADR-021 §租戶自選的可見度子集) — the tenant
// tier of the two-tier log visibility:
//
//   - GET /api/v1/tenants/{id}/federation/log-visibility — the tenant's
//     selection and the fields its log copy keeps under it.
//   - PUT /api/v1/tenants/{id}/federation/log-visibility — replace the
//     selection (admin on the tenant; every field must be platform-curated).
//
// tenant-api stores and validates the selection; Vector's tenant projection
// enforces it at write time once the operator copies it into the tenant's
// `tenantProjections[].visibleFields` (see package logvis).

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/federation/logvis"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"gopkg.in/yaml.v3"
)

// LogVisibilityResponse is a tenant's log visibility as GET and PUT return it.
type LogVisibilityResponse struct {
	TenantID string `json:"tenant_id"`
	// Fields is the tenant's selection; null means no selection — the
	// platform default, every curated field (including ones added later).
	Fields *[]string `json:"fields" extensions:"x-nullable"`
	// SelectedVisibleFields is every field a projected row would keep under
	// Fields once the platform applies this selection to Vector's tenant
	// projection. tenant-api does not track whether it has been applied, so
	// this is the selection's meaning, not what the tenant's rows carry now.
	SelectedVisibleFields []string `json:"selected_visible_fields"`
	// AlwaysVisibleFields are kept whatever the selection.
	AlwaysVisibleFields []string `json:"always_visible_fields"`
	// SelectableFields are the fields a selection may name.
	SelectableFields []string `json:"selectable_fields"`
}

func logVisibilityResponse(tenantID string, s *logvis.Selection) LogVisibilityResponse {
	eff := logvis.Effective(s)
	return LogVisibilityResponse{
		TenantID:              tenantID,
		Fields:                eff.Fields,
		SelectedVisibleFields: logvis.VisibleFields(eff),
		AlwaysVisibleFields:   logvis.AlwaysVisibleFields,
		SelectableFields:      logvis.SelectableFields,
	}
}

func toLogVisViolations(lv []logvis.Violation) []handler.Violation {
	out := make([]handler.Violation, len(lv))
	for i, v := range lv {
		out[i] = handler.Violation{Field: v.Field, Reason: v.Reason}
	}
	return out
}

// GetTenantLogVisibility handles GET /api/v1/tenants/{id}/federation/log-visibility.
// A tenant with no record gets the platform default (fields: null).
//
// @Summary     Get a tenant's log visibility selection
// @Description The selection is applied by Vector's tenant log projection, at
// @Description write time, once the platform copies it into the tenant's
// @Description projection entry; it only ever narrows the platform-curated fields.
// @Tags        federation
// @Produce     json
// @Param       id path string true "Tenant ID"
// @Success     200 {object} LogVisibilityResponse
// @Failure     400 {object} handler.ErrorResponse
// @Failure     500 {object} handler.ErrorResponse
// @Failure     503 {object} handler.ErrorResponse
// @Router      /api/v1/tenants/{id}/federation/log-visibility [get]
func GetTenantLogVisibility(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := handler.ValidateTenantID(tenantID); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		sel, err := readLogVisibility(r.Context(), d, tenantID)
		if err != nil {
			if handler.WriteStateError(w, r, err) {
				return
			}
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "read log visibility: "+err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(logVisibilityResponse(tenantID, sel))
	}
}

// PutTenantLogVisibility handles PUT /api/v1/tenants/{id}/federation/log-visibility.
//
// Requires admin on the tenant, the bar of the federation metric subset and of
// token issuance. Every field must be one the platform curates as selectable
// (the containment rule); `{"fields": null}` returns to the platform default.
// The body must be exactly one JSON object whose only key is `fields`, spelt
// exactly, present even when null: a missing, misspelt or differently-cased
// key, or trailing data, would otherwise read as null and silently widen the
// selection back to every curated field.
//
// @Summary     Replace a tenant's log visibility selection
// @Tags        federation
// @Accept      json
// @Produce     json
// @Param       id   path string           true "Tenant ID"
// @Param       body body logvis.Selection true "Fields to keep; null returns to the platform default"
// @Success     200  {object} LogVisibilityResponse
// @Failure     400  {object} handler.ErrorResponse
// @Failure     403  {object} handler.ErrorResponse
// @Failure     409  {object} handler.ErrorResponse
// @Failure     500  {object} handler.ErrorResponse
// @Failure     503  {object} handler.ErrorResponse
// @Failure     507  {object} handler.ErrorResponse
// @Router      /api/v1/tenants/{id}/federation/log-visibility [put]
func PutTenantLogVisibility(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := handler.ValidateWritableTenantID(tenantID); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		if !handler.OrgAllowed(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r), tenantID, rbac.PermAdmin, handler.WriteScopeMeta(d.ConfigDir)) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, errcode.Forbidden,
				"admin permission required on tenant "+tenantID+" to edit its log visibility")
			return
		}
		email := rbac.RequestEmail(r)

		// Not io.LimitReader: it stops at the cap without an error, so a body
		// padded to the cap would be parsed from its truncated prefix.
		body, ok := handler.ReadLimitedBody(w, r, d)
		if !ok {
			return
		}
		sel, err := decodeLogVisibilityBody(body)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		if pv := logvis.Validate(sel); len(pv) > 0 {
			handler.WriteValidationErrors(w, r, toLogVisViolations(pv))
			return
		}

		yamlBytes, err := yaml.Marshal(sel)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "marshal log visibility: "+err.Error())
			return
		}
		if err := d.MutateState(r.Context(), statestore.LogVisibilityKey(tenantID), statestore.LogVisibilityEntity(tenantID), email,
			func([]byte) ([]byte, error) { return yamlBytes, nil }); err != nil {
			if handler.WriteStateError(w, r, err) {
				return
			}
			handler.WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(logVisibilityResponse(tenantID, sel))
	}
}

// decodeLogVisibilityBody reads a PUT body strictly. Go's json decoding
// matches keys case-insensitively, ignores a missing key and stops after the
// first value, and each of those would turn a malformed body into
// `fields: null` — the widest selection — so the body is read as a raw object
// first and each of them refused.
func decodeLogVisibilityBody(body []byte) (*logvis.Selection, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	var raw map[string]json.RawMessage
	if err := dec.Decode(&raw); err != nil {
		return nil, fmt.Errorf("invalid JSON: %v", err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("invalid JSON: trailing data after the request object")
	}
	if raw == nil {
		return nil, errors.New(`request body must be a JSON object with a "fields" key`)
	}
	for k := range raw {
		if k != "fields" {
			return nil, fmt.Errorf(`unknown key %q: the only key is "fields"`, k)
		}
	}
	fv, ok := raw["fields"]
	if !ok {
		return nil, errors.New(`"fields" is required; send {"fields": null} to return to the platform default`)
	}
	if bytes.Equal(bytes.TrimSpace(fv), []byte("null")) {
		return &logvis.Selection{}, nil
	}
	var fields []string
	if err := json.Unmarshal(fv, &fields); err != nil {
		return nil, fmt.Errorf(`"fields" must be null or a list of field names: %v`, err)
	}
	if fields == nil {
		fields = []string{}
	}
	return &logvis.Selection{Fields: &fields}, nil
}

// readLogVisibility loads tenantID's selection; no record is the default.
// The addressability predicate runs first, as for the federation subset
// (readFederationSubset): an id that cannot name a record never reaches the
// store.
func readLogVisibility(ctx context.Context, d *handler.Deps, tenantID string) (*logvis.Selection, error) {
	if !confd.IsAddressableTenantID(tenantID) {
		return nil, fmt.Errorf("%w: %q", ErrUnaddressableTenantID, tenantID)
	}
	if d.State == nil {
		return nil, errors.New("tenant-api state store is not configured")
	}
	data, ok, err := d.State.Get(ctx, statestore.LogVisibilityKey(tenantID))
	if err != nil {
		return nil, err
	}
	if !ok {
		return &logvis.Selection{}, nil
	}
	return logvis.Parse(data)
}
