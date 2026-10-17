package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"gopkg.in/yaml.v3"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/tenantorg"
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
func ListGroups(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := rbac.RequestPrincipal(r)

		list := d.Groups.ListGroups()
		resp := make([]GroupResponse, 0, len(list))
		for _, g := range list {
			// v2.5.0: Skip groups where user has no accessible members
			if !groupVisible(d, p, g.Members) {
				continue
			}
			resp = append(resp, GroupResponse{
				ID:          g.ID,
				Label:       g.Label,
				Description: g.Description,
				Filters:     g.Filters,
				Members:     filterAccessibleMembers(d.RBAC, d.TenantOrg, p, g.Members),
			})
		}
		writeJSON(w, http.StatusOK, resp)
	}
}

// hasAccessibleMember returns true if the user can access at least one member.
// Org-aware read (ADR-027 / LD-6 P4c): each member tenant is checked through
// OrgAllowedRead, so a group with only other-org members is hidden from an
// org-scoped caller once the org flag flips (records on axis="org"). tenantOrg
// may be nil (nil-receiver-safe → unlabeled).
func hasAccessibleMember(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, members []string) bool {
	for _, m := range members {
		if OrgAllowedRead(rbacMgr, tenantOrg, p, m, rbac.PermRead) {
			return true
		}
	}
	return false
}

// groupVisible is the ONE predicate deciding whether a group exists for this
// caller. GET, LIST, DELETE and the batch endpoint all answer an invisible
// group exactly as they answer a missing one (#1530 / #1531): an endpoint that
// tells the two apart — a 403 here, a 400 there — confirms a group the read
// plane says is not there, and the denial can name the members the read plane
// hid.
//
// A group with NO members (#3123) is visible only to a platform admin
// restricted on no axis (rbac.PlatformAdminUnrestricted — NOT
// PlatformAdminNonOrgScoped, which ignores environments / domains). It has no
// member to authorize against, so it used to be visible to — and overwritable
// and deletable by — every caller; a PUT can no longer create one (members:
// min=1), and the admin is who can still see and remove one left from before.
//
// Open mode (no RBAC rules) sees everything, as before.
func groupVisible(d *Deps, p *rbac.VerifiedPrincipal, members []string) bool {
	if len(d.RBAC.Get().Groups) == 0 {
		return true
	}
	if len(members) == 0 {
		return d.RBAC.PlatformAdminUnrestricted(p)
	}
	return hasAccessibleMember(d.RBAC, d.TenantOrg, p, members)
}

// writeGroupNotFound is the one 404 body for a missing OR invisible group, so
// the two stay byte-identical.
func writeGroupNotFound(w http.ResponseWriter, r *http.Request, groupID string) {
	WriteJSONError(w, r, http.StatusNotFound, "group not found: "+groupID)
}

// msgGroupIDUnavailable is the text of writeGroupIDUnavailable's 409.
const msgGroupIDUnavailable = "group id is not available; choose another id"

// writeGroupIDUnavailable answers a PUT /groups/{id} whose id is held by a
// group the caller cannot see (groupVisible false) — the same body whatever
// that group's members are, naming none of them (#3123).
//
// ⚠️ Accepted disclosure: the answer differs from a free id's (which a PUT
// creates), so a writer learns ONE bit — "this id is taken". Group ids are one
// namespace shared by every caller, so a create on a taken id cannot succeed
// silently; Kubernetes object names and S3 bucket names make the same trade.
// What it no longer tells is anything about the group: its members, whether
// the caller could see some of them, or that it is empty.
func writeGroupIDUnavailable(w http.ResponseWriter, r *http.Request) {
	WriteJSONErrorWithCode(w, r, http.StatusConflict, CodeGroupIDUnavailable, msgGroupIDUnavailable)
}

// errGroupIDUnavailable is PutGroup's transform error for writeGroupIDUnavailable.
var errGroupIDUnavailable = errors.New("group id is held by a group the caller cannot see")

// filterAccessibleMembers returns only the members the user has read
// access to. Members are tenant IDs themselves (so the identity
// extractor `tenantIDFromString` is just `s -> s`). Open-mode RBAC
// is handled inside filterByRBAC via OrgAllowedRead's open-mode
// short-circuit.
func filterAccessibleMembers(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, members []string) []string {
	return filterByRBAC(rbacMgr, tenantOrg, p, members, tenantIDFromString, rbac.PermRead)
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
// @Failure     400  {object} ErrorResponse
// @Failure     404  {object} ErrorResponse
// @Router      /api/v1/groups/{id} [get]
func GetGroup(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		g, ok := d.Groups.GetGroup(groupID)
		if !ok {
			writeGroupNotFound(w, r, groupID)
			return
		}

		// LD-6 P4c follow-up (#962): mirror ListGroups' RBAC member
		// filtering. Without it, GetGroup returned the FULL member tenant-id
		// list to any reader who cleared the route gate — the
		// read-by-id twin of the group-member enumeration oracle P4c closed for
		// the list view. Hide the group entirely (an identical 404 to a missing
		// group, so existence leaks nothing) when the caller can read none of
		// its members, mirroring ListGroups' skip; otherwise return only the
		// members they can read. Byte-identical for deployments with no
		// org-scope rules and in open mode (filterAccessibleMembers is the
		// identity transform there). Where org-scope rules label member
		// tenants, this filters them on the org axis exactly as ListGroups
		// already does — labeled cross-org members drop in both shadow and
		// enforce; the enforce flag only governs unlabeled-tenant leniency.
		p := rbac.RequestPrincipal(r)
		if !groupVisible(d, p, g.Members) {
			writeGroupNotFound(w, r, groupID)
			return
		}

		writeJSON(w, http.StatusOK, GroupResponse{
			ID:          groupID,
			Label:       g.Label,
			Description: g.Description,
			Filters:     g.Filters,
			Members:     filterAccessibleMembers(d.RBAC, d.TenantOrg, p, g.Members),
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
	Filters     map[string]string `json:"filters" validate:"max=20"`
	// At least one member (#3123): an empty group has nothing to authorize a
	// reader or writer against. Emptying a group is a DELETE.
	Members []string `json:"members" validate:"min=1,max=1000,dive,min=1,max=256"`
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
// #1529: an UPDATE also requires PermWrite on every member the group
// already has, because replacing the list removes them — otherwise PUT
// does what DELETE refuses. Stored members the caller cannot read are
// not named in the 403 (writeGroupMemberForbidden).
//
// #3123: an id held by a group the caller cannot see (groupVisible) is a 409
// GROUP_ID_UNAVAILABLE that says nothing about that group
// (writeGroupIDUnavailable — it discloses only that the id is taken). A
// visible group the caller may not write keeps the 403. `members` must be
// non-empty.
//
// @Summary     Create or update a group
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id   path     string          true "Group ID"
// @Param       body body     PutGroupRequest true "Group definition"
// @Success     200  {object} map[string]string
// @Failure     400  {object} ErrorResponse "Invalid id or body; members must list at least one tenant (an empty group is removed with DELETE)"
// @Failure     403  {object} ErrorResponse "The caller may not write a member tenant (of the request, or of the visible stored group)"
// @Failure     409  {object} ErrorResponse "Code GROUP_ID_UNAVAILABLE: the id is held by a group the caller cannot see — choose another id; do not retry. The body is the same whatever that group holds; it discloses only that the id is taken. Otherwise a state write lost a race (CONFLICT; retry)"
// @Failure     413  {object} ErrorResponse
// @Failure     500  {object} ErrorResponse
// @Failure     503  {object} ErrorResponse
// @Failure     507  {object} ErrorResponse
// @Router      /api/v1/groups/{id} [put]
func PutGroup(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		p := rbac.RequestPrincipal(r)

		body, ok := readLimitedBody(w, r, d)
		if !ok {
			return
		}

		var req PutGroupRequest
		if err := json.Unmarshal(body, &req); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}

		// v2.8.0 issue #134 — body-content range validation. Struct-tag
		// rules cover Label / Description / Members; per-pair Filters
		// length checks need the imperative path because validator's
		// `dive` doesn't render the offending key in the field path.
		violations := ValidateStructTags(&req)
		violations = append(violations, validateFilterMap(req.Filters, "filters")...)
		if len(violations) > 0 {
			WriteValidationErrors(w, r, violations)
			return
		}

		// v2.8.0 B-6 PR-2: tenant-scoped authz on members.
		// Caller must have PermWrite on every member tenant; reject
		// if any member is forbidden. List ALL forbidden ids in the
		// error so the operator can fix in one round-trip rather
		// than discovering them one-at-a-time. These ids come from the
		// request, so naming them tells the caller nothing they did not send.
		if forbidden := tenantsLackingPermission(d.RBAC, d.TenantOrg, p, req.Members, rbac.PermWrite, WriteScopeMeta(d.ConfigDir)); len(forbidden) > 0 {
			writeGroupMemberForbidden(w, r, d, p, "write", forbidden, nil)
			return
		}

		// Rebuild the whole record from the STORED copy, inside the state
		// store's read-modify-write (#2856). The groups record is a single
		// shared object, so this write necessarily rewrites every OTHER group
		// too — computing it from the in-memory snapshot silently reverts
		// anything the snapshot has not seen: an operator's out-of-band edit
		// of the state ConfigMap before the next reload, or a concurrent edit
		// to a different group. Reading the base inside the store's
		// compare-and-swap removes both — the same fix WriteMerged made for
		// the tenant batch path (#1097).
		//
		// #1529: a PUT REPLACES the stored member list, so it removes every
		// stored member the request leaves out — the same outcome DELETE
		// refuses unless the caller may write every stored member. The check
		// above only covers the members the request brings; this one covers
		// the members it would overwrite, and it runs on the stored copy, in
		// the lock, for the reason DeleteGroup's in-lock check gives. There is
		// deliberately no snapshot pre-check: DeleteGroup's comment records
		// how a stale snapshot makes that one refuse callers who are entitled.
		// `members: []` takes the same path — it is the shortest way to strip
		// a group, and the request-side check has nothing to check for it.
		var forbiddenOnDisk []string
		if err := d.mutateState(r.Context(), statestore.KeyGroups, "groups", email,
			func(current []byte) ([]byte, error) {
				cfg, perr := parseGroupsFile(current)
				if perr != nil {
					return nil, perr
				}
				if stored, present := cfg.Groups[groupID]; present {
					// #3123: a group the caller cannot see is answered before
					// its members are looked at, so nothing about them leaks.
					if !groupVisible(d, p, stored.Members) {
						return nil, errGroupIDUnavailable
					}
					if f := tenantsLackingPermission(d.RBAC, d.TenantOrg, p, stored.Members, rbac.PermWrite, WriteScopeMeta(d.ConfigDir)); len(f) > 0 {
						forbiddenOnDisk = f
						return nil, errGroupMemberForbidden
					}
				}
				cfg.Groups[groupID] = groups.Group{
					Label:       req.Label,
					Description: req.Description,
					Filters:     req.Filters,
					Members:     req.Members,
				}
				return groups.MarshalConfig(cfg)
			}); err != nil {
			if errors.Is(err, errGroupIDUnavailable) {
				writeGroupIDUnavailable(w, r)
				return
			}
			if errors.Is(err, errGroupMemberForbidden) {
				writeGroupMemberForbidden(w, r, d, p, "write", nil, forbiddenOnDisk)
				return
			}
			writeConfigFileError(w, r, err)
			return
		}

		writeJSON(w, http.StatusOK, map[string]string{
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
// @Failure     400 {object} ErrorResponse
// @Failure     403 {object} ErrorResponse
// @Failure     404 {object} ErrorResponse
// @Failure     409 {object} ErrorResponse
// @Failure     500 {object} ErrorResponse
// @Failure     503 {object} ErrorResponse
// @Failure     507 {object} ErrorResponse
// @Router      /api/v1/groups/{id} [delete]
func DeleteGroup(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		p := rbac.RequestPrincipal(r)

		cfg := d.Groups.Get()
		existing, ok := cfg.Groups[groupID]
		if !ok || !groupVisible(d, p, existing.Members) {
			writeGroupNotFound(w, r, groupID)
			return
		}

		// v2.8.0 B-6 PR-2: tenant-scoped authz. Caller must have
		// PermWrite on every member of the to-be-deleted group.
		// Without this, a malicious operator could destroy a
		// group whose members they don't own — a denial-of-
		// service surface against teams who depend on dashboards
		// keyed off that group.
		//
		// This one runs on the snapshot. It exists to turn away an
		// obviously-forbidden caller before they consume a write admission
		// slot, and the identical call inside the transform below is what
		// makes ALLOWING correct — a snapshot that has gone stale can no
		// longer let a forbidden delete through.
		//
		// ⚠️ It is NOT merely an optimisation: on the DENY side it is final,
		// because this branch returns and the in-lock check never runs. A
		// snapshot that is stale in the other direction (the file has since
		// lost the member the caller cannot write — an admin edited the state
		// record directly) therefore 403s a caller who is in fact entitled to
		// delete, until the next Reload or WatchLoop tick refreshes the snapshot. Closing the fail-closed half means dropping this
		// pre-check entirely and paying an admission slot per forbidden call;
		// that trade is deliberately not made here — it is recorded so the
		// next reader does not mistake the current shape for full coverage.
		if forbidden := tenantsLackingPermission(d.RBAC, d.TenantOrg, p, existing.Members, rbac.PermWrite, WriteScopeMeta(d.ConfigDir)); len(forbidden) > 0 {
			writeGroupMemberForbidden(w, r, d, p, "delete", nil, forbidden)
			return
		}

		// Same in-lock rebuild as PutGroup — see the comment there for why the
		// stored copy, not the snapshot, has to be the base. A target that is
		// already absent from the file returns nil (no-op success): the 404
		// above answered "does it exist" from the snapshot, and re-answering it
		// here would turn a concurrent delete of the same group into an error
		// for the caller who lost the race, on a request whose desired end
		// state was reached anyway.
		//
		// The member gate is re-evaluated HERE, against the group as stored,
		// because this is a DELETE: the object being destroyed is the
		// stored one, not one the request supplies. Checking the snapshot alone
		// would authorize against a member list that no longer describes what
		// gets removed — and the snapshot stays wrong until the next Reload or
		// WatchLoop tick. Under org-scoped RBAC (ADR-027) that gap is a real
		// authorization bypass: the route gate only proves write on SOME tenant
		// (#2520), while OrgAllowed can still deny individual member tenants.
		var forbiddenOnDisk []string
		if err := d.mutateState(r.Context(), statestore.KeyGroups, "groups", email,
			func(current []byte) ([]byte, error) {
				cfg, perr := parseGroupsFile(current)
				if perr != nil {
					return nil, perr
				}
				stored, present := cfg.Groups[groupID]
				if !present {
					return nil, nil
				}
				// The snapshot said visible; the file may since have changed
				// members. Re-judged here so the answer matches what a GET
				// would now say about the group being destroyed.
				if !groupVisible(d, p, stored.Members) {
					return nil, errGroupNotVisible
				}
				if f := tenantsLackingPermission(d.RBAC, d.TenantOrg, p, stored.Members, rbac.PermWrite, WriteScopeMeta(d.ConfigDir)); len(f) > 0 {
					forbiddenOnDisk = f
					return nil, errGroupMemberForbidden
				}
				delete(cfg.Groups, groupID)
				return groups.MarshalConfig(cfg)
			}); err != nil {
			if errors.Is(err, errGroupNotVisible) {
				writeGroupNotFound(w, r, groupID)
				return
			}
			if errors.Is(err, errGroupMemberForbidden) {
				writeGroupMemberForbidden(w, r, d, p, "delete", nil, forbiddenOnDisk)
				return
			}
			writeConfigFileError(w, r, err)
			return
		}

		writeJSON(w, http.StatusOK, map[string]string{
			"status":   "ok",
			"group_id": groupID,
		})
	}
}

// GroupIDFromPath extracts the group ID from the URL for RBAC middleware.
var GroupIDFromPath = func(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// parseGroupsFile decodes _groups.yaml bytes as read inside the state store's read-modify-write.
// A missing file (current == nil, the first-ever write) starts from an empty
// config; an existing but unparseable file is an ERROR, never an empty start —
// silently treating a malformed file as "no groups" would let one bad edit wipe
// every group on the next unrelated write.
//
// "Unparseable" has to mean more than "yaml.Unmarshal returned an error".
// yaml.v3 is not strict by default, so a document that is valid YAML but the
// wrong SHAPE — a typo'd top-level key, someone's ConfigMap wrapper, another
// file entirely — decodes cleanly into a zero GroupsConfig. That is
// indistinguishable from "no groups" to the code above, and rebuilding the
// file from it is precisely the one-bad-edit wipe this function exists to
// refuse. So the top-level key has to be probed for PRESENCE, not read for its
// value. An explicit `groups:` with a null or empty value stays legal: the
// shape is recognised and the file is genuinely declaring zero groups.
func parseGroupsFile(current []byte) (*groups.GroupsConfig, error) {
	if len(current) == 0 {
		return &groups.GroupsConfig{Groups: make(map[string]groups.Group)}, nil
	}
	if err := requireTopLevelKey(current, "groups"); err != nil {
		return nil, &configFileParseError{fmt.Errorf("read current _groups.yaml: %w", err)}
	}
	cfg, err := groups.ParseConfig(current)
	if err != nil {
		return nil, &configFileParseError{fmt.Errorf("read current _groups.yaml: %w", err)}
	}
	if cfg.Groups == nil {
		cfg.Groups = make(map[string]groups.Group)
	}
	return cfg, nil
}

// requireTopLevelKey fails unless data is a YAML mapping carrying key at its
// top level. Shared by parseGroupsFile / parseViewsFile: both rebuild a whole
// shared config file from what they read, so both need the shape of what they
// read to be the shape they think it is.
func requireTopLevelKey(data []byte, key string) error {
	var doc map[string]yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return err
	}
	if _, present := doc[key]; !present {
		return fmt.Errorf("no top-level %q key — refusing to rebuild the file from a document "+
			"whose shape is not recognised (a wipe of every existing entry would look identical "+
			"to a legitimate write)", key)
	}
	return nil
}

// errGroupMemberForbidden is returned by the PutGroup / DeleteGroup transforms
// when the group AS STORED has member tenants the caller may not write. The state
// store returns a transform error unchanged, so it arrives here as-is, and is matched with
// errors.Is so the handler can answer 403 instead of the generic 500 a
// transform failure would otherwise produce. The offending ids ride alongside
// in a captured variable rather than in the error, because the transform runs
// inside the store's read-modify-write and the handler renders the response after it returns.
var errGroupMemberForbidden = errors.New("group has member tenants the caller may not write")

// errGroupNotVisible is returned by DeleteGroup's transform when the group as
// stored is one the caller cannot see (groupVisible); the handler answers it
// with the same 404 as a missing group.
var errGroupNotVisible = errors.New("group is not visible to the caller")

// writeGroupMemberForbidden renders the 403 for both the pre-check and the
// in-lock check, so a caller cannot tell which of the two rejected them.
//
// The ids it names depend on where they came from (#1531). fromRequest are ids
// the caller sent, and are always named: the caller knows them already, and
// naming every one is what authz.go's "fix it in one round-trip" contract asks
// for. fromStored are ids read off the stored group; only those the caller
// can READ are named. The rest would hand out exactly what GET /groups/{id}
// filters away, so they are only acknowledged, without a count: the 403 itself
// already says something was refused, and a count would tell how many members
// the read plane hid.
func writeGroupMemberForbidden(w http.ResponseWriter, r *http.Request, d *Deps, p *rbac.VerifiedPrincipal, verb string, fromRequest, fromStored []string) {
	named := append([]string(nil), fromRequest...)
	hidden := false
	for _, id := range fromStored {
		if OrgAllowedRead(d.RBAC, d.TenantOrg, p, id, rbac.PermRead) {
			named = append(named, id)
		} else {
			hidden = true
		}
	}
	msg := "insufficient permission to " + verb + " group"
	switch {
	case len(named) > 0 && hidden:
		msg += " with forbidden member tenants: " + strings.Join(named, ", ") +
			"; it also has member tenants you cannot view"
	case len(named) > 0:
		msg += " with forbidden member tenants: " + strings.Join(named, ", ")
	default:
		msg += ": it has member tenants you cannot view"
	}
	WriteJSONError(w, r, http.StatusForbidden, msg)
}
