package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

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
func ListGroups(mgr *groups.Manager, rbacMgr *rbac.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		idpGroups := rbac.RequestGroups(r)
		rbacCfg := rbacMgr.Get()

		list := mgr.ListGroups()
		resp := make([]GroupResponse, 0, len(list))
		for _, g := range list {
			// v2.5.0: Skip groups where user has no accessible members
			if len(rbacCfg.Groups) > 0 && !hasAccessibleMember(rbacMgr, idpGroups, g.Members) {
				continue
			}
			resp = append(resp, GroupResponse{
				ID:          g.ID,
				Label:       g.Label,
				Description: g.Description,
				Filters:     g.Filters,
				Members:     filterAccessibleMembers(rbacMgr, idpGroups, g.Members),
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

// filterAccessibleMembers returns only the members the user has read access to.
func filterAccessibleMembers(rbacMgr *rbac.Manager, idpGroups, members []string) []string {
	rbacCfg := rbacMgr.Get()
	if len(rbacCfg.Groups) == 0 {
		return members // open mode
	}
	filtered := make([]string, 0, len(members))
	for _, m := range members {
		if rbacMgr.HasPermission(idpGroups, m, rbac.PermRead) {
			filtered = append(filtered, m)
		}
	}
	return filtered
}

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
func GetGroup(mgr *groups.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		g, ok := mgr.GetGroup(groupID)
		if !ok {
			writeJSONError(w, http.StatusNotFound, "group not found: "+groupID)
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
type PutGroupRequest struct {
	Label       string            `json:"label"`
	Description string            `json:"description"`
	Filters     map[string]string `json:"filters"`
	Members     []string          `json:"members"`
}

// PutGroup handles PUT /api/v1/groups/{id}
//
// Creates or updates a group. Writes to _groups.yaml via gitops writer.
//
// @Summary     Create or update a group
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id   path     string          true "Group ID"
// @Param       body body     PutGroupRequest true "Group definition"
// @Success     200  {object} map[string]string
// @Failure     400  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Router      /api/v1/groups/{id} [put]
func PutGroup(mgr *groups.Manager, writer *gitops.Writer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		var req PutGroupRequest
		if err := json.Unmarshal(body, &req); err != nil {
			writeJSONError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}

		if req.Label == "" {
			writeJSONError(w, http.StatusBadRequest, "label is required")
			return
		}

		// Update the in-memory config and write to disk
		cfg := mgr.Get()
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
			writeJSONError(w, http.StatusInternalServerError, "marshal groups: "+err.Error())
			return
		}

		if err := writer.WriteGroupsFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, http.StatusInternalServerError, err.Error())
			return
		}

		// Reload manager to pick up the new file
		_ = mgr.Reload()

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
func DeleteGroup(mgr *groups.Manager, writer *gitops.Writer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)

		cfg := mgr.Get()
		if _, ok := cfg.Groups[groupID]; !ok {
			writeJSONError(w, http.StatusNotFound, "group not found: "+groupID)
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
			writeJSONError(w, http.StatusInternalServerError, "marshal groups: "+err.Error())
			return
		}

		if err := writer.WriteGroupsFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, http.StatusInternalServerError, err.Error())
			return
		}

		_ = mgr.Reload()

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
