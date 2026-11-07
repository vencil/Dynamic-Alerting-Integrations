package handler

// The authoritative verdict on a tenant write's scope and labels (ADR-027 §7),
// taken under the writer lock on the tree the write lands on — the fresh base
// in PR write-back mode, the serving tree in direct mode. The gates a handler
// runs before it calls the Writer read a tree that another write, queued
// ahead of this one, may still change: the PR base the pod has not synced, or
// the serving tree an earlier direct write is about to rewrite. They can only
// refuse early. This is the one copy of the verdict both modes take in the
// lock: a single-tenant PUT through gitops.TreeCheck (putTreeCheck), a batch
// op through its in-lock merge (batchOpMerge).
//
// What is judged, in this order:
//
//  1. the write scope on the tenant's state before the write (skipped when its
//     file there cannot be read for its metadata: the post-state check then
//     counts the org axis);
//  2. the write scope on the state the write leaves;
//  3. the label gate (label_guard.go): a change of the environment/domain
//     labels — or a write over labels that cannot be read — passes a
//     platform administrator only.
//
// A scope denial is counted only for a value that differs from what the gate
// outside the lock judged (writeGate), so each write is counted once per
// value.

import (
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// treeJudge is one request's in-lock verdict: who writes, and where the
// tenant's organizations are read.
type treeJudge struct {
	rbac *rbac.Manager
	p    *rbac.VerifiedPrincipal
	// orgs reads tenantID's organizations for the tree at configDir.
	orgs func(configDir, tenantID string) ([]string, error)
	// direct: the serving tree, not a PR's fresh base — the refusals say
	// what the gates outside the lock say (freshBaseScopeError.Direct).
	direct bool
}

// prTreeJudge reads the organizations from the fresh base's own
// `_tenant_orgs.yaml` (not the hot-reloaded copy, which follows the local
// tree, #2857); a file that does not parse there refuses the write.
func prTreeJudge(d *Deps, p *rbac.VerifiedPrincipal) treeJudge {
	return treeJudge{rbac: d.RBAC, p: p, orgs: tenantorg.OrgsInTree}
}

// directTreeJudge reads the organizations the gates outside the lock read,
// the hot-reloaded copy: no write through the API changes
// `_tenant_orgs.yaml`, so no queued write can make it stale.
func directTreeJudge(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal) treeJudge {
	return treeJudge{rbac: rbacMgr, p: p, direct: true, orgs: func(_, tenantID string) ([]string, error) {
		orgs, _ := tenantOrg.OrgsForTenant(tenantID)
		return orgs, nil
	}}
}

// scopeAllowed is the write scope on the tree at configDir with now as the
// tenant's (environment, domain) there. gate is what the gate outside the
// lock judged; countOrg is set on one of a write's calls only, as the
// organizations are the same for all of them.
func (j treeJudge) scopeAllowed(configDir, tenantID string, gate writeGate, now scopePair, countOrg bool) (bool, error) {
	if j.rbac == nil {
		return true, nil // test-only state, see requireOrgWriteWithMeta
	}
	orgs, err := j.orgs(configDir, tenantID)
	if err != nil {
		return false, &freshBaseOrgsError{Err: err}
	}
	return j.rbac.AllowedInOrgRejudge(j.p, tenantID, rbac.PermWrite, orgs, now.environment, now.domain,
		now != gate.pair, countOrg && orgsKey(orgs) != gate.orgs), nil
}

// putTreeCheck is a PUT's in-lock verdict (see the file comment): body
// replaces tenantID's file whole; gatePre/gatePost are what planPut judged.
func (j treeJudge) putTreeCheck(tenantID, body string, gatePre, gatePost writeGate) gitops.TreeCheck {
	return func(configDir string) error {
		refusal := freshBaseScopeError{TenantID: tenantID, Put: true, Direct: j.direct}
		pre, judged := freshWriteScope(configDir, tenantID)
		if judged {
			if ok, err := j.scopeAllowed(configDir, tenantID, gatePre, pre, true); err != nil || !ok {
				refusal.PreState = true
				return orFreshScopeError(err, &refusal)
			}
		}
		post := scopePairOf(proposedScopeMeta(configDir, body), tenantID)
		if ok, err := j.scopeAllowed(configDir, tenantID, gatePost, post, !judged); err != nil || !ok {
			return orFreshScopeError(err, &refusal)
		}
		if before := scopeLabelsOnTree(configDir, tenantID); !labelChangeAllowed(j.rbac, j.p, before, post) {
			refusal.Labels, refusal.LabelsUnknown = true, !before.known
			return &refusal
		}
		return nil
	}
}

// batchOp is what a batch's gates outside the lock judged one op on, for its
// in-lock merge.
type batchOp struct {
	index int // in the request (for a group batch, in the expansion)
	op    BatchOperation
	// comparePre: the pre-state the gate judged — the tenant's file for its
	// first op, the post-state of the op before it for a later one (the
	// in-lock pre-state of a later op is the file with the earlier ops of
	// the request laid over it).
	comparePre, gatePost writeGate
	firstOp              bool // the tenant's first op of the request: the org axis is counted here
	merge                gitops.MergeFunc
	absent               *bool // set when the op's tenant is absent and the op skips it (#2882)
}

// batchOpMerge is a batch op's in-lock merge (see the file comment):
// configDir is the tree the batch is merged in, existing the tenant's file
// there with this request's earlier ops for it applied (nil: no file).
func (j treeJudge) batchOpMerge(o batchOp) func(configDir string, existing []byte) (string, error) {
	op := o.op
	return func(configDir string, existing []byte) (string, error) {
		// #2882: an absent group member is skipped before any verdict:
		// nothing is written for it, so there is nothing to judge.
		*o.absent = op.skipAbsent && tenantAbsentIn(existing, op.TenantID)
		if *o.absent {
			return "", gitops.ErrMergeNoOp
		}
		refusal := freshBaseScopeError{TenantID: op.TenantID, Op: o.index, Direct: j.direct}
		countOrg := o.firstOp
		pre, judged := freshWriteScope(configDir, op.TenantID)
		if judged {
			if ok, err := j.scopeAllowed(configDir, op.TenantID, o.comparePre, pre, countOrg); err != nil || !ok {
				refusal.PreState = true
				return "", orFreshScopeError(err, &refusal)
			}
			countOrg = false
		}
		merged, err := o.merge(existing)
		if err != nil {
			return "", err
		}
		if !touchesScopeMetadata(op) && !countOrg {
			return merged, nil
		}
		post := scopePairOf(proposedScopeMeta(configDir, merged), op.TenantID)
		if ok, err := j.scopeAllowed(configDir, op.TenantID, o.gatePost, post, countOrg); err != nil || !ok {
			return "", orFreshScopeError(err, &refusal)
		}
		if !touchesScopeMetadata(op) {
			return merged, nil
		}
		before := scopeLabelsOf(&platformMetadataSource{configDir: configDir}, existing, op.TenantID)
		if !labelChangeAllowed(j.rbac, j.p, before, post) {
			refusal.Labels, refusal.LabelsUnknown = true, !before.known
			return "", &refusal
		}
		return merged, nil
	}
}
