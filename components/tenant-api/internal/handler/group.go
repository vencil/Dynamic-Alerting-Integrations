package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

// GroupResponse is the response body for a single group.
type GroupResponse struct {
	ID          string            `json:"id"`
	Label       string            `json:"label"`
	Description string            `json:"description,omitempty"`
	Filters     map[string]string `json:"filters,omitempty"`
	Members     []string          `json:"members"`
}

// ListGroups handles GET /api/v1/groups
//
// v2.5.0 Phase C: Permission-filtered — only returns groups where the user
// has access to at least one member tenant (based on RBAC tenant patterns).
//
// @Summary     List all custom groups
// @Description Returns tenant groups visible to the authenticated user, filtered by RBAC.
// @Tags        groups
// @Produce     json
// @Success     200 {array}  GroupResponse
// @Router      /api/v1/groups [get]
func (d *Deps) ListGroups() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		idpGroups := rbac.RequestGroups(r)
		rbacCfg := d.RBAC.Get()

		list := d.Groups.ListGroups()
		resp := make([]GroupResponse, 0, len(list))
		for _, g := range list {
			// v2.5.0: Skip groups where user has no accessible members
			if len(rbacCfg.Groups) > 0 && !hasAccessibleMember(d.RBAC, idpGroups, g.Members) {
				continue
			}
			resp = append(resp, GroupResponse{
				ID:          g.ID,
				Label:       g.Label,
				Description: g.Description,
				Filters:     g.Filters,
				Members:     filterAccessibleMembers(d.RBAC, idpGroups, g.Members),
			})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}

// hasAccessibleMember returns true if the user can access at least one member.
func hasAccessibleMember(rbacMgr *rbac.Manager, idpGroups, members []string) bool {
	for _, m := range members {
		if rbacMgr.HasPermission(idpGroups, m, rbac.PermRead) {
			return true
		}
	}
	return false
}

// filterAccessibleMembers returns only the members the user has read
// access to. Members are tenant IDs themselves (so the identity
// extractor `tenantIDFromString` is just `s -> s`). Open-mode RBAC
// is handled inside filterByRBAC via HasPermission's open-mode
// short-circuit.
func filterAccessibleMembers(rbacMgr *rbac.Manager, idpGroups, members []string) []string {
	return filterByRBAC(rbacMgr, idpGroups, members, tenantIDFromString, rbac.PermRead)
}

// tenantIDFromString is the identity extractor used when filtering a
// slice whose elements are themselves tenant IDs.
func tenantIDFromString(s string) string { return s }

// GetGroup handles GET /api/v1/groups/{id}
//
// @Summary     Get a single group
// @Description Returns a tenant group by ID.
// @Tags        groups
// @Produce     json
// @Param       id   path     string true "Group ID"
// @Success     200  {object} GroupResponse
// @Failure     400  {object} map[string]string
// @Failure     404  {object} map[string]string
// @Router      /api/v1/groups/{id} [get]
func (d *Deps) GetGroup() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		g, ok := d.Groups.GetGroup(groupID)
		if !ok {
			writeJSONError(w, r,http.StatusNotFound, "group not found: "+groupID)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(GroupResponse{
			ID:          groupID,
			Label:       g.Label,
			Description: g.Description,
			Filters:     g.Filters,
			Members:     g.Members,
		})
	}
}

// PutGroupRequest is the body for PUT /api/v1/groups/{id}.
//
// `Filters` per-key length checks live in
// `body_validator.go::validateFilterMap` (struct-tag `dive` could
// cover values but the per-pair field-path reporting needs custom
// rendering for the JSON `field` response value).
type PutGroupRequest struct {
	Label       string            `json:"label" validate:"required,min=1,max=256"`
	Description string            `json:"description" validate:"max=4096"`
	Filters     map[string]string `json:"filters"`
	Members     []string          `json:"members" validate:"max=1000,dive,min=1,max=256"`
}

// PutGroup handles PUT /api/v1/groups/{id}
//
// Creates or updates a group. Writes to _groups.yaml via gitops writer.
//
// **v2.8.0 B-6 PR-2 hardening**: requires PermWrite on **every member
// tenant**, not just route-level PermWrite. Without this check, any
// PermWrite user could rewrite any group's `members` field to point at
// tenants they cannot read — an info-disclosure escalation surface
// (e.g. group could later be referenced by a dashboard query that
// reveals the tenants' merged_hash). The check returns 403 with a
// list of forbidden tenant IDs so the operator knows exactly what to
// fix.
//
// @Summary     Create or update a group
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id   path     string          true "Group ID"
// @Param       body body     PutGroupRequest true "Group definition"
// @Success     200  {object} map[string]string
// @Failure     400  {object} map[string]string
// @Failure     403  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Router      /api/v1/groups/{id} [put]
func (d *Deps) PutGroup() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		idpGroups := rbac.RequestGroups(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, r,http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		var req PutGroupRequest
		if err := json.Unmarshal(body, &req); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}

		// v2.8.0 issue #134 — body-content range validation. Struct-tag
		// rules cover Label / Description / Members; per-pair Filters
		// length checks need the imperative path because validator's
		// `dive` doesn't render the offending key in the field path.
		violations := validateStructTags(&req)
		violations = append(violations, validateFilterMap(req.Filters, "filters")...)
		if len(violations) > 0 {
			writeValidationErrors(w, r,violations)
			return
		}

		// v2.8.0 B-6 PR-2: tenant-scoped authz on members.
		// Caller must have PermWrite on every member tenant; reject
		// if any member is forbidden. List ALL forbidden ids in the
		// error so the operator can fix in one round-trip rather
		// than discovering them one-at-a-time.
		if forbidden := tenantsLackingPermission(d.RBAC, idpGroups, req.Members, rbac.PermWrite); len(forbidden) > 0 {
			writeJSONError(w, r,http.StatusForbidden,
				"insufficient permission to write group with forbidden member tenants: "+
					strings.Join(forbidden, ", "))
			return
		}

		// Update the in-memory config and write to disk
		cfg := d.Groups.Get()
		newCfg := &groups.GroupsConfig{
			Groups: make(map[string]groups.Group, len(cfg.Groups)+1),
		}
		for k, v := range cfg.Groups {
			newCfg.Groups[k] = v
		}
		newCfg.Groups[groupID] = groups.Group{
			Label:       req.Label,
			Description: req.Description,
			Filters:     req.Filters,
			Members:     req.Members,
		}

		yamlBytes, err := groups.MarshalConfig(newCfg)
		if err != nil {
			writeJSONError(w, r,http.StatusInternalServerError, "marshal groups: "+err.Error())
			return
		}

		if err := d.Writer.WriteGroupsFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, r,http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, r,http.StatusInternalServerError, err.Error())
			return
		}

		// Reload manager to pick up the new file
		_ = d.Groups.Reload()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":   "ok",
			"group_id": groupID,
		})
	}
}

// DeleteGroup handles DELETE /api/v1/groups/{id}
//
// Removes a group from _groups.yaml.
//
// @Summary     Delete a group
// @Tags        groups
// @Produce     json
// @Param       id path string true "Group ID"
// @Success     200 {object} map[string]string
// @Failure     400 {object} map[string]string
// @Failure     404 {object} map[string]string
// @Failure     409 {object} map[string]string
// @Router      /api/v1/groups/{id} [delete]
func (d *Deps) DeleteGroup() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, r,http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		idpGroups := rbac.RequestGroups(r)

		cfg := d.Groups.Get()
		existing, ok := cfg.Groups[groupID]
		if !ok {
			writeJSONError(w, r,http.StatusNotFound, "group not found: "+groupID)
			return
		}

		// v2.8.0 B-6 PR-2: tenant-scoped authz. Caller must have
		// PermWrite on every member of the to-be-deleted group.
		// Without this, a malicious operator could destroy a
		// group whose members they don't own — a denial-of-
		// service surface against teams who depend on dashboards
		// keyed off that group.
		if forbidden := tenantsLackingPermission(d.RBAC, idpGroups, existing.Members, rbac.PermWrite); len(forbidden) > 0 {
			writeJSONError(w, r,http.StatusForbidden,
				"insufficient permission to delete group with forbidden member tenants: "+
					strings.Join(forbidden, ", "))
			return
		}

		newCfg := &groups.GroupsConfig{
			Groups: make(map[string]groups.Group, len(cfg.Groups)),
		}
		for k, v := range cfg.Groups {
			if k != groupID {
				newCfg.Groups[k] = v
			}
		}

		yamlBytes, err := groups.MarshalConfig(newCfg)
		if err != nil {
			writeJSONError(w, r,http.StatusInternalServerError, "marshal groups: "+err.Error())
			return
		}

		if err := d.Writer.WriteGroupsFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, r,http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, r,http.StatusInternalServerError, err.Error())
			return
		}

		_ = d.Groups.Reload()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":   "ok",
			"group_id": groupID,
		})
	}
}

// GroupIDFromPath extracts the group ID from the URL for RBAC middleware.
var GroupIDFromPath = func(r *http.Request) string {
	return chi.URLParam(r, "id")
}
