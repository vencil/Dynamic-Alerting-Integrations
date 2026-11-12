package handler

// ADR-027 §7 / §9 item 6 — the environment and domain labels a tenant carries
// (`_metadata.environment` / `_metadata.domain`, as the scope reading resolves
// them) decide which RBAC rules' `environments:` / `domains:` cover it, so
// only a platform administrator may change them. A write by anyone else that
// would change either label — set one where there was none, change one, or
// remove one; a value left as it was is not a change — is refused, 403
// FORBIDDEN, whatever the caller's write scope says.
//
// "Platform administrator" is rbac.Manager.PlatformAdminUnrestricted: a rule
// granting admin on the literal "*" tenant with no org-scope, environments or
// domains. A label decides which OTHER rules cover the tenant, so changing it
// changes who may see and write it — the same power as editing _rbac.yaml,
// which no API grants. An admin restricted to some environments could
// otherwise move a tenant between the environments it administers and so
// hand it to every rule scoped to the destination; the write scope gates
// (on the state before and after) do not stop that, since both states are
// inside its own scope. Not Allowed(p, "*", PermAdmin) either: an org-scoped
// "*" admin rule passes that (its org axis is not consulted on the
// platform-scope query).
//
// What a label is: the pair the write scope gates read (ScopeMetaFunc's
// reading, cfg.MetadataResolver): the tenant document's own `_metadata` merged
// per key over the root platform layer, else the elected profile's. So a
// batch op that changes `_profile` (a batch cannot set `_metadata`,
// cfg.PatchableAsString) changes the labels exactly when the profile it
// elects carries other ones, and a body that drops its own `_metadata` changes
// them unless the layer below says the same.
//
// Which tree: the one the write rewrites, under the writer lock (tree_judge.go):
// the serving tree in direct mode, the fresh base a PR-mode branch is cut from
// in PR mode. The check a handler runs before it calls the Writer reads a tree
// that a write queued ahead of this one may still change, so it can only
// refuse early: a label changed since — by a platform administrator's write
// that landed first, or on the base since the pod synced — cannot be quietly
// reverted, nor a write that changes nothing early pass for one that changes
// nothing where it lands.
//
// Reading the labels before the write:
//   - a tenant with no file yet: the labels the root platform layer alone
//     gives it (a tenant declared with an empty block) — creating a tenant
//     with labels is a change, creating one that keeps what the platform
//     layer already says is not;
//   - a file that is an unresolvable symlink: no file (scopeLabelsOnTree);
//   - a file that parses but does not declare the tenant (a `tenants: {}`
//     shell): no file — it holds no labels of the tenant;
//   - a file whose labels cannot be read (ambiguous, not a regular file,
//     unparseable, undecodable `_metadata`):
//     unknown. No write can be shown to leave unknown labels as they were —
//     a body without labels may be removing the ones the file holds — so
//     under the lock a write over such a file passes a platform
//     administrator only, as config check reports such a tenant
//     (metadata_unknown). The early check lets it through: most such files
//     are refused for what they are under the lock first (git-managed,
//     ambiguous, not a regular file), and that answer is the one the
//     caller can act on.

import (
	"errors"
	"io/fs"
	"net/http"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// labelChangeDeniedText is the refusal of a write a caller who is not a
// platform administrator may not make: one changing tenantID's labels, or —
// beforeKnown false — one over a file whose labels cannot be read. It names
// the tenant the caller addressed and no label value (the stored one may not
// be the caller's to learn).
func labelChangeDeniedText(tenantID string, beforeKnown bool) string {
	head := "only a platform administrator may change the environment or domain labels (_metadata) of tenant " +
		tenantID + ": they decide which RBAC rules' environments/domains cover it"
	if !beforeKnown {
		return head + ", and the labels it currently resolves to cannot be read — its file, a root platform entry " +
			"or its profile writes a _metadata that does not decode (e.g. the string form, #3638) — so this write " +
			"cannot be shown to leave them as they were and is refused (ADR-027)"
	}
	return head + ", so this write, which would change them, is refused (ADR-027)"
}

// mayChangeScopeLabels reports whether p may change a tenant's environment /
// domain labels: a platform administrator only. A nil manager (a Deps without
// one) is the test-only state requireOrgWriteWithMeta documents.
func mayChangeScopeLabels(rbacMgr *rbac.Manager, p *rbac.VerifiedPrincipal) bool {
	if rbacMgr == nil {
		return true
	}
	return rbacMgr.PlatformAdminUnrestricted(p)
}

// labelState is a tenant's labels before a write: pair as the write scope
// gates read them, known false when its file's labels cannot be read.
type labelState struct {
	pair  scopePair
	known bool
}

// labelChangeAllowed is the gate under the writer lock: a write leaving known
// labels as they were passes whoever writes it; one changing them, or one
// over labels that cannot be read, passes a platform administrator only.
func labelChangeAllowed(rbacMgr *rbac.Manager, p *rbac.VerifiedPrincipal, before labelState, after scopePair) bool {
	return (before.known && before.pair == after) || mayChangeScopeLabels(rbacMgr, p)
}

// labelChangeAllowedEarly is the gate outside the lock: labels that cannot be
// read are left to the verdict under the lock (see the file comment).
func labelChangeAllowedEarly(rbacMgr *rbac.Manager, p *rbac.VerifiedPrincipal, before labelState, after scopePair) bool {
	return !before.known || labelChangeAllowed(rbacMgr, p, before, after)
}

// scopeLabelsOf reads tenantID's labels from data, a tenant document on the
// tree whose root platform layer src reads; nil data is a tenant with no file
// (see the file comment).
func scopeLabelsOf(src *platformMetadataSource, data []byte, tenantID string) labelState {
	if data == nil {
		data = []byte("tenants:\n  " + tenantID + ": {}\n")
	}
	e, dm, ok, decoded := src.declaredIn(data, tenantID)
	if !ok {
		// A file that parses but does not declare the tenant (a
		// `tenants: {}` shell) holds no labels of it: the platform layer
		// alone gives them, as for a tenant with no file. Only a file that
		// does not parse is unknown.
		if _, err := cfg.ParseConfigFile(data); err == nil {
			return scopeLabelsOf(src, nil, tenantID)
		}
		return labelState{}
	}
	if !decoded {
		return labelState{}
	}
	return labelState{scopePair{e, dm}, true}
}

// scopeLabelsOnTree reads tenantID's labels from its file in configDir
// (confd.ResolveTenantFile, as every write gate locates it), read as the
// write reads the file it replaces (gitops.ReadTenantFile): a file that is an
// unresolvable symlink holds no labels anywhere — the write replaces it as it
// would create the tenant — so it reads as no file, not as unknown.
func scopeLabelsOnTree(configDir, tenantID string) labelState {
	src := &platformMetadataSource{configDir: configDir}
	path, err := confd.ResolveTenantFile(configDir, tenantID)
	if errors.Is(err, confd.ErrTenantFileNotFound) {
		return scopeLabelsOf(src, nil, tenantID)
	}
	if err != nil {
		return labelState{}
	}
	data, err := gitops.ReadTenantFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return scopeLabelsOf(src, nil, tenantID)
	}
	if err != nil {
		return labelState{}
	}
	return scopeLabelsOf(src, data, tenantID)
}

// requireLabelsUnchangedAt is the PUT check outside the lock (planPut):
// before is the tenant's labels on the serving tree, after the ones the body
// proposes. It writes the 403 to w and returns false when the caller may not
// make the change.
func requireLabelsUnchangedAt(w http.ResponseWriter, r *http.Request, d *Deps, tenantID string, before labelState, after scopePair) bool {
	if labelChangeAllowedEarly(d.RBAC, rbac.RequestPrincipal(r), before, after) {
		return true
	}
	WriteJSONErrorWithCode(w, r, http.StatusForbidden, errcode.Forbidden, labelChangeDeniedText(tenantID, true))
	return false
}

// batchLabelsBefore is the "before" of a batch op's label check outside the
// lock, after its scope gates: the tenant's block on disk with prior (the
// tenant's earlier ops of the request) laid over it, read as a proposed body
// is (stackedScopeMeta). Labels its file holds that cannot be read are left to
// the verdict under the lock (unknown here, which the early check lets
// through).
func batchLabelsBefore(configDir string, prior []BatchOperation, op BatchOperation) labelState {
	if !touchesScopeMetadata(op) || !scopeLabelsOnTree(configDir, op.TenantID).known {
		return labelState{}
	}
	return labelState{scopePairOf(stackedScopeMeta(configDir, op.TenantID, prior), op.TenantID), true}
}

// gateBatchOpLabelsAt is a batch op's label check outside the lock: an op that
// sets a scopeMetadataKeys key is refused when the labels it leaves (after)
// differ from the ones before it.
func gateBatchOpLabelsAt(rbacMgr *rbac.Manager, p *rbac.VerifiedPrincipal, op BatchOperation, before labelState, after scopePair) (BatchResult, bool) {
	if !touchesScopeMetadata(op) || labelChangeAllowedEarly(rbacMgr, p, before, after) {
		return BatchResult{}, false
	}
	return gateRefusal(op.TenantID, errkind.Forbidden, labelChangeDeniedText(op.TenantID, true)), true
}
