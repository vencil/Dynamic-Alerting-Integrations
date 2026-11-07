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
// Which tree: the one the write rewrites. The handler's check reads the
// serving tree — the tree a direct-mode write lands on, and in PR mode an
// early refusal only. In PR mode the write lands on the fresh base the branch
// is cut from, which the serving tree may lag, so the verdict is taken again
// there, under the writer lock, before anything is written (the same place as
// the #2857 scope re-judgement): a label changed on the base since the pod
// synced cannot be quietly reverted, nor a write that changes nothing locally
// pass for one that changes nothing.
//
// Reading the labels before the write:
//   - a tenant with no file yet: the labels the root platform layer alone
//     gives it (a tenant declared with an empty block) — creating a tenant
//     with labels is a change, creating one that keeps what the platform
//     layer already says is not;
//   - a file whose labels cannot be read (ambiguous, not a regular file,
//     unparseable, not declaring the tenant, undecodable `_metadata`): on the
//     serving tree, unlabeled — the write scope gates' own fail-soft
//     (WriteScopeMeta), which here fails closed: such a file cannot be given
//     labels by a non-administrator; on the fresh base, the labels the
//     serving tree's check judged, so a base file that is broken is refused
//     for what it is further on (the write's own validation) and not
//     reported as a label change it cannot be shown to be.

import (
	"errors"
	"net/http"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/rbac"
)

// labelChangeDeniedText is the refusal of a label change by a caller who is
// not a platform administrator. It names the tenant the caller addressed and
// neither label value (the base's may not be the caller's to learn).
func labelChangeDeniedText(tenantID string) string {
	return "only a platform administrator may change the environment or domain labels (_metadata) of tenant " +
		tenantID + ": they decide which RBAC rules' environments/domains cover it, so this write, " +
		"which would change them, is refused (ADR-027)"
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

// labelChangeAllowed is the gate: a write leaving the labels as they were
// passes whoever writes it; one changing them passes a platform administrator
// only.
func labelChangeAllowed(rbacMgr *rbac.Manager, p *rbac.VerifiedPrincipal, before, after scopePair) bool {
	return before == after || mayChangeScopeLabels(rbacMgr, p)
}

// scopeLabelsOf reads tenantID's labels from data, a tenant document on the
// tree whose root platform layer src reads; nil data is a tenant with no file
// (see the file comment). known is false when data's labels cannot be read.
func scopeLabelsOf(src *platformMetadataSource, data []byte, tenantID string) (pair scopePair, known bool) {
	if data == nil {
		data = []byte("tenants:\n  " + tenantID + ": {}\n")
	}
	e, dm, ok, decoded := src.declaredIn(data, tenantID)
	if !ok || !decoded {
		return scopePair{}, false
	}
	return scopePair{e, dm}, true
}

// scopeLabelsOnTree reads tenantID's labels from its file in configDir
// (confd.ResolveTenantFile, as every write gate locates it). known is false
// when the file is there but its labels cannot be read.
func scopeLabelsOnTree(configDir, tenantID string) (pair scopePair, known bool) {
	src := &platformMetadataSource{configDir: configDir}
	path, err := confd.ResolveTenantFile(configDir, tenantID)
	if errors.Is(err, confd.ErrTenantFileNotFound) {
		return scopeLabelsOf(src, nil, tenantID)
	}
	if err != nil {
		return scopePair{}, false
	}
	data, problem := confd.ReadTenantFile(filepath.Dir(path), filepath.Base(path))
	if problem != confd.ProblemNone {
		return scopePair{}, false
	}
	return scopeLabelsOf(src, data, tenantID)
}

// servingLabelsBefore is the labels before the write on the serving tree:
// unreadable reads as unlabeled (see the file comment).
func servingLabelsBefore(configDir, tenantID string) scopePair {
	pair, _ := scopeLabelsOnTree(configDir, tenantID)
	return pair
}

// requireLabelsUnchangedAt is the PUT check on the serving tree (planPut):
// before is the tenant's labels there, after the ones the body proposes. It
// writes the 403 to w and returns false when the caller may not make the
// change.
func requireLabelsUnchangedAt(w http.ResponseWriter, r *http.Request, d *Deps, tenantID string, before, after scopePair) bool {
	if labelChangeAllowed(d.RBAC, rbac.RequestPrincipal(r), before, after) {
		return true
	}
	WriteJSONErrorWithCode(w, r, http.StatusForbidden, errcode.Forbidden, labelChangeDeniedText(tenantID))
	return false
}

// freshLabelsBefore is the labels before the write on the fresh base a
// PR-mode write lands on: the tenant's file there as data (nil: no file),
// else — labels that cannot be read there — local, the ones the serving
// tree's check judged.
func freshLabelsBefore(configDir string, data []byte, tenantID string, local scopePair) scopePair {
	if pair, known := scopeLabelsOf(&platformMetadataSource{configDir: configDir}, data, tenantID); known {
		return pair
	}
	return local
}

// freshLabelsBeforeOnTree is freshLabelsBefore reading the tenant's file in
// configDir itself (a PUT's check gets the tree, not the bytes).
func freshLabelsBeforeOnTree(configDir, tenantID string, local scopePair) scopePair {
	if pair, known := scopeLabelsOnTree(configDir, tenantID); known {
		return pair
	}
	return local
}

// gateBatchOpLabels is a batch op's label check on the serving tree, after
// its scope gates: an op that sets a scopeMetadataKeys key is refused when
// the labels it leaves differ from the ones before it — the tenant's block on
// disk with prior (the tenant's earlier ops of the request) laid over it,
// read as a proposed body is (stackedScopeMeta).
func gateBatchOpLabels(rbacMgr *rbac.Manager, configDir string, prior []BatchOperation, op BatchOperation, p *rbac.VerifiedPrincipal) (res BatchResult, refused bool) {
	if !touchesScopeMetadata(op) {
		return BatchResult{}, false
	}
	before := scopePairOf(stackedScopeMeta(configDir, op.TenantID, prior), op.TenantID)
	after := scopePairOf(postStateScopeMeta(configDir, prior, op), op.TenantID)
	return gateBatchOpLabelsAt(rbacMgr, p, op, before, after)
}

// gateBatchOpLabelsAt is gateBatchOpLabels over pairs the caller computed.
func gateBatchOpLabelsAt(rbacMgr *rbac.Manager, p *rbac.VerifiedPrincipal, op BatchOperation, before, after scopePair) (BatchResult, bool) {
	if !touchesScopeMetadata(op) || labelChangeAllowed(rbacMgr, p, before, after) {
		return BatchResult{}, false
	}
	return gateRefusal(op.TenantID, errkind.Forbidden, labelChangeDeniedText(op.TenantID)), true
}
