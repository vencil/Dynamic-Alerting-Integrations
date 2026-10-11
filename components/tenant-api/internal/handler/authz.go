package handler

// ============================================================
// v2.8.0 Phase B Track C (B-6) PR-2 — tenant-scoped authz helpers.
//
// The chi-route-level RBAC middleware grants PermRead/PermWrite on
// the route's primary tenant (extracted from `{id}` path param).
// But several endpoints accept a LIST of tenant IDs in the request
// body or operate on a stored list (groups.Members, view filters)
// — for those, route-level auth alone leaves an info-disclosure
// surface ("if you can hit `PUT /api/v1/groups/{id}`, you can
// rewrite that group to reference any tenants you can see in the
// platform"). These helpers close that gap.
//
// Design contract:
//   - Always check using the **request's** verified principal
//     (rbac.RequestPrincipal — attached by the auth middleware),
//     not the route handler's idea of who the user is. Single
//     source of truth; never hand-build a principal from parts.
//   - Return the FULL list of forbidden tenant IDs to the caller
//     (not just the first). Operators fixing their permission
//     misalignment shouldn't have to retry-and-discover.
//   - These are write-side checks — the read-side filtering helpers
//     in group.go (`hasAccessibleMember`, `filterAccessibleMembers`)
//     handle the symmetrical read concern.
// ============================================================

import (
	"errors"
	"net/http"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// ScopeMetaFunc resolves one tenant's (environment, domain) for a write-plane
// scope decision (#1597).
//
// Resolution happens at DECISION TIME, straight from the tenant's own file. A
// stale authorization input on the plane that MUTATES state is a real (if
// small) window, and the cost argument for caching does not hold here: the same
// request is about to run a git commit, which is orders of magnitude more
// expensive than one file read. (The read/collection filters deliberately do
// NOT take one of these — they are per-item loops, and binding the metadata
// axis there is a separate plane with its own migration, out of scope here.)
//
// #2370, #2830: the metadata read is the one the list reads
// (cfg.MetadataResolver), which is /metrics' reading: the root platform files'
// `tenants.<id>._metadata` with the tenant document's own `_metadata` merged
// over it per key, else the elected profile's. The root platform files are
// read once per resolver, on first use. When they cannot be read
// (platformMetadataOrNone) the tenant document's own `_metadata` is used
// alone, as before #2370.
//
// A tenant file that is absent or unusable fails SOFT to the empty pair — an
// unlabeled tenant — which is exactly what the pre-#1597 metadata-blind write
// plane behaved like. Resolution never invents a label it could not read.
type ScopeMetaFunc func(tenantID string) (environment, domain string)

// platformMetadataSource reads configDir's root platform layer once, on first
// use, for one resolver.
type platformMetadataSource struct {
	configDir string
	once      sync.Once
	metadata  cfg.MetadataResolver
}

// environmentDomainOf is a ScopeMetaFunc's answer for a tenant document data that
// declares tenantID (or that the caller proposes to write for it).
func (s *platformMetadataSource) environmentDomainOf(data []byte, tenantID string) (string, string) {
	var metadata cfg.MetadataResolver // no configDir: the document's own `_metadata` alone
	if s.configDir != "" {
		s.once.Do(func() {
			root, _ := platformMetadataOrNone(s.configDir, "write")
			s.metadata = root.MetadataResolver()
		})
		metadata = s.metadata
	}
	// A `_metadata` that does not decode reads as the empty pair, as an
	// unusable file does.
	meta, _, _ := metadata.ResolveFile(tenantID, data)
	return meta.Environment, meta.Domain
}

// WriteScopeMeta builds the write-plane resolver: one targeted read of the
// tenant's actual file (confd.ResolveTenantFile, so a `<id>.yml` tenant is not
// silently treated as unlabeled — #1673).
func WriteScopeMeta(configDir string) ScopeMetaFunc {
	src := &platformMetadataSource{configDir: configDir}
	return func(tenantID string) (string, string) {
		if configDir == "" {
			return "", ""
		}
		path, err := confd.ResolveTenantFile(configDir, tenantID)
		if err != nil {
			return "", "" // absent, ambiguous or unsafe id → unlabeled
		}
		// #2477: confd.ReadTenantFile, not os.ReadFile — it opens without
		// blocking and refuses a non-regular file on the opened fd, so the
		// write gate answers at once whatever kind of file is at the path.
		// Any problem → unlabeled, the same fail-soft as above.
		data, problem := confd.ReadTenantFile(filepath.Dir(path), filepath.Base(path))
		if problem != confd.ProblemNone {
			return "", ""
		}
		return src.environmentDomainOf(data, tenantID)
	}
}

// OrgAllowed is the single composition point of tenantorg.OrgsForTenant and
// rbac.AllowedInOrg (ADR-027 / LD-6 P4b): it resolves the target tenant's
// organization list AT DECISION TIME and feeds it to the org-scope-aware
// write-plane permission check. Every per-tenant write/admin authorization
// decision in the handler tree goes through here (or through the
// RequireOrgWrite wrapper below) — never through the org-blind rbac.Allowed;
// the org_write_guard tripwire pins that. A pure predicate so batch/member
// loops can call it per item and shape their own per-item error.
//
// tenantOrg may be nil (a Deps built without wiring the manager, e.g. a
// handler test literal): OrgsForTenant is nil-receiver-safe and reports the
// tenant as unlabeled, which with no org-scoped rule is byte-identical to
// the pre-P4b permission check.
func OrgAllowed(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager,
	p *rbac.VerifiedPrincipal, tenantID string, want rbac.Permission, meta ScopeMetaFunc) bool {
	orgs, _ := tenantOrg.OrgsForTenant(tenantID)
	var environment, domain string
	if meta != nil {
		environment, domain = meta(tenantID)
	}
	return rbacMgr.AllowedInOrg(p, tenantID, want, orgs, environment, domain)
}

// OrgAllowedRead is the READ/VISIBILITY-plane sibling of OrgAllowed (ADR-027 /
// LD-6 P4c): it resolves the tenant's org list and routes through
// rbac.AllowedInOrgRead, which records its would-deny on axis="org" (the same
// series the list-plane ScopeAllowed uses) rather than the write plane's
// axis="org_write". Used by the collection read filters (filterByRBAC over
// PR lists / task results, and hasAccessibleMember/filterAccessibleMembers for
// the ListGroups and GetGroup member lists) so those reads stop being a
// cross-org tenant-reference oracle once the org flag flips — the read plane
// closes in lockstep with read-by-id. Like OrgAllowed, tenantOrg may be nil
// (nil-receiver-safe → unlabeled).
func OrgAllowedRead(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager,
	p *rbac.VerifiedPrincipal, tenantID string, want rbac.Permission) bool {
	orgs, _ := tenantOrg.OrgsForTenant(tenantID)
	return rbacMgr.AllowedInOrgRead(p, tenantID, want, orgs)
}

// RequireOrgWrite is the top-of-handler convenience wrapper over OrgAllowed
// for single-tenant write handlers: it answers "may the request's verified
// principal perform `want` on tenantID?" and, when the answer is no, writes
// the canonical 403 envelope (same WriteJSONErrorWithCode shape as the
// federation handlers) and returns false so the caller can bail with a bare
// `return`. The message names the org axis so an operator denied by an
// org-scoped rule knows which knob to look at, but deliberately reveals
// neither the tenant's org list nor any principal claim value (principal.go
// logging discipline — the org names are an enumeration oracle).
func RequireOrgWrite(w http.ResponseWriter, r *http.Request, d *Deps, tenantID string, want rbac.Permission) bool {
	_, ok := requireOrgWriteAt(w, r, d, tenantID, want)
	return ok
}

// requireOrgWriteAt is RequireOrgWrite that also returns what it judged, for
// a PR-mode write to pass to its in-lock re-judgement (freshScopeAllowed).
func requireOrgWriteAt(w http.ResponseWriter, r *http.Request, d *Deps, tenantID string, want rbac.Permission) (writeGate, bool) {
	pair := scopePairOf(WriteScopeMeta(d.ConfigDir), tenantID)
	return writeGate{pair, gateOrgs(d, tenantID)}, requireOrgWriteWithMeta(w, r, d, tenantID, want, fixedScopeMeta(pair),
		"insufficient permissions for tenant "+tenantID+
			" (permission and organization-scope checks, ADR-027)")
}

// RequireOrgWriteProposed is the POST-STATE half of the write gate (#1597
// follow-up). RequireOrgWrite authorizes against the tenant's metadata as it is
// ON DISK; that is the right question to ask before reading the body, but it is
// not the only one, because `_metadata` lives INSIDE the file a whole-file PUT
// replaces. A caller authorized for environment=production could therefore
// submit a body setting environment=dev and have it committed — the write would
// be checked against production and land in dev, planting configuration in a
// scope the caller does not administer (and removing the tenant from their own
// reach).
//
// This is specific to the metadata axis. The org axis cannot be attacked this
// way: org membership lives in the admin-only `_tenant_orgs.yaml`, a separate
// file this path cannot write. The batch path reaches the metadata axis
// through scalar keys alone — `_profile`, and `_metadata` in its string form
// (#2830; it refuses to overwrite a `_metadata` mapping with a scalar,
// tenant_batch.go) — and runs the same post-state check per op
// (gateBatchOpPostState).
//
// The check runs the SAME predicate against the PROPOSED metadata, so it
// inherits the axis's flag: in shadow it changes nothing, and it closes the gap
// exactly when --rbac-metadata-write-scope-enforce is flipped. Call it AFTER
// the body is read and BEFORE any write; RequireOrgWrite still runs first so a
// denied caller learns nothing from a body it was never allowed to submit.
func RequireOrgWriteProposed(w http.ResponseWriter, r *http.Request, d *Deps,
	tenantID string, want rbac.Permission, proposedYAML string) bool {
	_, ok := requireOrgWriteProposedAt(w, r, d, tenantID, want, proposedYAML)
	return ok
}

// requireOrgWriteProposedAt is RequireOrgWriteProposed that also returns what
// it judged (see requireOrgWriteAt).
func requireOrgWriteProposedAt(w http.ResponseWriter, r *http.Request, d *Deps,
	tenantID string, want rbac.Permission, proposedYAML string) (writeGate, bool) {
	pair := scopePairOf(proposedScopeMeta(d.ConfigDir, proposedYAML), tenantID)
	return writeGate{pair, gateOrgs(d, tenantID)}, requireOrgWriteWithMeta(w, r, d, tenantID, want, fixedScopeMeta(pair),
		"insufficient permissions for the tenant metadata this write proposes"+
			" — the environment/domain in the body places tenant "+tenantID+
			" outside your scope (#1597)")
}

// proposedScopeMeta reads environment/domain from the content the caller is
// proposing to write, rather than from disk. Same extractor the list plane
// uses, so a body and a stored file are read identically — the body's
// `_metadata` merged per key over configDir's root platform layer (#2370),
// the value the tenant will have once the body is written.
func proposedScopeMeta(configDir, yamlContent string) ScopeMetaFunc {
	src := &platformMetadataSource{configDir: configDir}
	return func(tenantID string) (string, string) {
		return src.environmentDomainOf([]byte(yamlContent), tenantID)
	}
}

// scopePair is one tenant's (environment, domain) as a ScopeMetaFunc reads it.
type scopePair struct{ environment, domain string }

func scopePairOf(meta ScopeMetaFunc, tenantID string) scopePair {
	e, dm := meta(tenantID)
	return scopePair{e, dm}
}

// writeGate is what a gate on the local tree judged a tenant on: its
// (environment, domain) and its organizations (orgsKey). A PR-mode write
// passes it down to the in-lock re-judgement, which counts a would-deny only
// for a value that differs on the fresh base (freshScopeAllowed).
type writeGate struct {
	pair scopePair
	orgs string
}

// orgsKey is a comparable form of an organization list (order-insensitive).
func orgsKey(orgs []string) string {
	s := append([]string(nil), orgs...)
	sort.Strings(s)
	return strings.Join(s, "\n")
}

func gateOrgs(d *Deps, tenantID string) string {
	orgs, _ := d.TenantOrg.OrgsForTenant(tenantID)
	return orgsKey(orgs)
}

func fixedScopeMeta(pair scopePair) ScopeMetaFunc {
	return func(string) (string, string) { return pair.environment, pair.domain }
}

// freshWriteScope is WriteScopeMeta on the fresh base a PR-mode write lands
// on (#2857), with one difference: judged is false when the tenant's file
// there exists but cannot be read for its metadata (ambiguous, not a regular
// file, does not decode, does not declare the tenant). WriteScopeMeta reads
// such a file as unlabeled, which on the base would turn a broken file into a
// scope refusal; the write is refused for what it is further on (the merge
// or the writer's validation). A file absent on the base is unlabeled and
// judged, as a new tenant is.
func freshWriteScope(configDir, tenantID string) (pair scopePair, judged bool) {
	path, err := confd.ResolveTenantFile(configDir, tenantID)
	if errors.Is(err, confd.ErrTenantFileNotFound) {
		return scopePair{}, true
	}
	if err != nil {
		return scopePair{}, false
	}
	data, problem := confd.ReadTenantFile(filepath.Dir(path), filepath.Base(path))
	if problem != confd.ProblemNone {
		return scopePair{}, false
	}
	root, _ := platformMetadataOrNone(configDir, "write")
	meta, ok, decoded := root.MetadataResolver().ResolveFile(tenantID, data)
	if !ok || !decoded {
		return scopePair{}, false
	}
	return scopePair{meta.Environment, meta.Domain}, true
}

// freshScopeAllowed is the write scope judged on the fresh base a PR-mode
// write lands on (#2857), configDir being that tree. This verdict is the
// authoritative one: the gate on the pod's local tree can only refuse early,
// because that tree may lag the base. It is always taken, with the tenant's
// organizations read from the base's `_tenant_orgs.yaml` (not the
// hot-reloaded copy, which follows the local tree) and fresh as its
// (environment, domain) there.
//
// gate is what the gate on the local tree judged (passed down, not read
// again). A shadow would-deny is recorded only for a value that differs from
// it, so each write is counted once per value; countOrg is set on one of a
// write's calls only, as the organizations are the same for all of them.
// An org file that does not parse on the base refuses the write.
func freshScopeAllowed(d *Deps, p *rbac.VerifiedPrincipal, configDir, tenantID string, gate writeGate, fresh scopePair, countOrg bool) (bool, error) {
	if d.RBAC == nil {
		return true, nil // test-only state, see requireOrgWriteWithMeta
	}
	orgs, err := tenantorg.OrgsInTree(configDir, tenantID)
	if err != nil {
		return false, &freshBaseOrgsError{Err: err}
	}
	return d.RBAC.AllowedInOrgRejudge(p, tenantID, rbac.PermWrite, orgs, fresh.environment, fresh.domain,
		fresh != gate.pair, countOrg && orgsKey(orgs) != gate.orgs), nil
}

func requireOrgWriteWithMeta(w http.ResponseWriter, r *http.Request, d *Deps,
	tenantID string, want rbac.Permission, meta ScopeMetaFunc, denyMsg string) bool {
	// A Deps literal without an RBAC manager is a TEST-ONLY state (nil-safe
	// contract, mirroring TenantOrg above): main.go always wires a non-nil
	// manager — even open mode is a non-nil Manager — and the route-level
	// RBAC middleware dereferences the same manager, so a nil here can never
	// be reached by a routed production request. Treating it as "no RBAC
	// layer configured" preserves the pre-P4b behavior of the handlers these
	// wrappers guard, which performed no in-handler permission check.
	if d.RBAC == nil {
		return true
	}
	if OrgAllowed(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r), tenantID, want, meta) {
		return true
	}
	WriteJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden, denyMsg)
	return false
}

// withReplaceUnparseable returns r carrying gitops.WithReplaceUnparseable when
// the caller may replace a current tenant file that cannot be parsed (#2405):
// such a file's content — including which tenants it declares — cannot be
// read, so replacing it needs write permission on ALL tenants, not only on the
// URL's tenant.
//
// "All tenants" is rbac.PlatformUnrestricted(p, PermWrite): a matching rule
// whose tenant list holds the literal "*" (a prefix pattern such as "svc-*" is
// not all tenants) and that carries no org-scope, environments or domains. The
// scope-axis flags (orgScopeEnforce / metadata*ScopeEnforce) are deliberately
// NOT consulted: in shadow mode a scoped "*" rule still reaches unlabeled
// tenants, but that is migration leniency on tenants whose labels are known,
// and a file that cannot be parsed has no labels to check — so a scoped caller
// never passes, in either mode. RequireOrgWrite has already authorized the URL
// tenant; this only decides the extra bit. A Deps without an RBAC manager (a
// test-only state, see requireOrgWriteWithMeta) does not get the bit — the
// refusal is the fail-closed side.
func withReplaceUnparseable(r *http.Request, d *Deps) *http.Request {
	if d.RBAC == nil || !d.RBAC.PlatformUnrestricted(rbac.RequestPrincipal(r), rbac.PermWrite) {
		return r
	}
	return r.WithContext(gitops.WithReplaceUnparseable(r.Context()))
}

// tenantsLackingPermission returns the subset of `tenantIDs` for
// which the caller (identified by principal `p`) does NOT have
// `want` permission. An empty slice means "all good — caller may
// proceed". A nil principal is the anonymous caller (no groups) —
// it produces a forbidden list equal to `tenantIDs` (anonymous →
// cannot write anything).
//
// The check is org-scope-aware (ADR-027 / LD-6 P4b): each tenant is
// evaluated through OrgAllowed, so an org-scoped rule only grants a
// member tenant it covers. tenantOrg may be nil (test literals) —
// see OrgAllowed.
//
// Open mode (no `_rbac.yaml`): the permission check grants READ
// only — a write/admin check denies every tenant, so for
// PermWrite/PermAdmin this helper returns the full input list.
// In practice the route-level middleware's platform-scope gate
// (`Allowed(p, "*", PermWrite)`) already 403s an open-mode write
// request before any handler calling this helper runs; the
// per-tenant denial here is the defense-in-depth layer (see
// TestTenantsLackingPermission_OpenModeWriteRejectsAll).
//
// Edge cases:
//   - tenantIDs is nil/empty → empty result (nothing to check)
//   - duplicate ids in input → de-duplicated output (forbidden ids
//     aren't repeated; matches what an operator wants to see)
func tenantsLackingPermission(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, tenantIDs []string, want rbac.Permission, meta ScopeMetaFunc) []string {
	if len(tenantIDs) == 0 {
		return nil
	}
	seen := make(map[string]bool, len(tenantIDs))
	forbidden := make([]string, 0)
	for _, tid := range tenantIDs {
		if tid == "" || seen[tid] {
			continue
		}
		seen[tid] = true
		if !OrgAllowed(rbacMgr, tenantOrg, p, tid, want, meta) {
			forbidden = append(forbidden, tid)
		}
	}
	return forbidden
}

// filterByRBAC returns the subset of `items` whose tenant ID
// (extracted via `tenantID(item)`) the caller has the given
// permission on. Items with empty tenant IDs are passed through
// — that's how administrative entries (e.g. PRs not bound to a
// single tenant) end up surfacing to readers without false-403'ing
// on a missing tag.
//
// Empty / nil input → returned as-is. Open-read mode (no
// _rbac.yaml) → OrgAllowedRead returns true for every tenant, so
// this helper effectively becomes the identity transform — no
// restrictions, allocation cost only.
//
// Org-aware read filter (ADR-027 / LD-6 P4c): each item's tenant is checked
// through OrgAllowedRead, so a group-member / PR / task entry belonging to a
// tenant outside the caller's org is filtered out once --rbac-org-scope-enforce
// flips — the read plane closes in lockstep with read-by-id (P4c converted this
// from the pre-P4c org-blind Allowed, removing its org_write_guard exemption).
// The would-deny records on axis="org" (read/visibility plane). tenantOrg may be
// nil (test literals) — OrgsForTenant is nil-receiver-safe → unlabeled.
//
// Generic over the slice element type so it covers the four
// near-identical filter wrappers below (members []string,
// PRInfo, async.TaskResult, etc.) without copy-paste drift on
// the loop body.
func filterByRBAC[T any](
	rbacMgr *rbac.Manager,
	tenantOrg *tenantorg.Manager,
	p *rbac.VerifiedPrincipal,
	items []T,
	tenantID func(T) string,
	perm rbac.Permission,
) []T {
	if len(items) == 0 {
		return items
	}
	out := make([]T, 0, len(items))
	for _, item := range items {
		tid := tenantID(item)
		if tid == "" || OrgAllowedRead(rbacMgr, tenantOrg, p, tid, perm) {
			out = append(out, item)
		}
	}
	return out
}
