package handler

// Domain-policy checks on a tenant's RESOLVED routing (#2280): what the route
// generator renders from `_routing_defaults` → the referenced routing profile
// → the tenant's `_routing`, judged by pkg/routingpolicy — the package
// da-guard uses, pinned to the Python generator by
// tests/shared/routing_policy_parity_matrix.json.

import (
	"errors"
	"log/slog"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"gopkg.in/yaml.v3"
)

// extractTenantBlock returns `tenants.<tenantID>` of a tenant document, or
// nil when the document does not parse or does not declare the tenant. Its
// `_routing` carries the receivers, `routes[i].match` values and override
// `alertname` / `metric_group` as the route generator's PyYAML reads them
// (routingpolicy.WithPyYAMLRouting, #2431), so the policy judges the routes
// the generator renders; a value with no PyYAML reading is refused
// (Unmatched), never judged as yaml.v3 read it.
func extractTenantBlock(body []byte, tenantID string) map[string]any {
	var doc struct {
		Tenants map[string]map[string]any `yaml:"tenants"`
	}
	if err := yaml.Unmarshal(body, &doc); err != nil {
		return nil
	}
	block := doc.Tenants[tenantID]
	if r, has := block["_routing"]; has {
		block["_routing"] = routingpolicy.WithPyYAMLRouting(r, routingpolicy.PyYAMLRoutingByTenant(body)[tenantID])
	}
	return block
}

// loadRoutingLayers reads `_routing_defaults` and the routing profiles from
// the conf.d root, per request. A layer that cannot be read is logged by name
// and left out, and the check runs on what remains — fail-open, the same side
// the domain-policy watcher fails on. Domain policies themselves come from
// the caller's policy.Manager (the watcher, or freshBasePolicyCheck's
// snapshot of the PR base), so LoadRoot's policy problems are not repeated
// here.
func loadRoutingLayers(configDir string) routingpolicy.Layers {
	layers, _, problems := routingpolicy.LoadRoot(configDir, nil)
	for _, p := range problems {
		switch {
		case p.Kind == routingpolicy.ProblemDomainPolicyUnusable && p.File != "":
			continue
		case p.Kind == routingpolicy.ProblemRoutingDefaultsRoutes:
			// Not a layer left out: the rest of _routing_defaults applies;
			// only its `routes` are dropped, as the route generator drops them.
			slog.Warn("_routing_defaults.routes ignored by the domain-policy check; the rest of _routing_defaults applies",
				"file", p.File)
		default:
			slog.Warn("routing layer not applied to the domain-policy check",
				"kind", p.Kind, "file", p.File, "field", p.Field, "detail", p.Message)
		}
	}
	return layers
}

// touchesRouting reports whether a batch op changes what the tenant's
// routing resolves to: it sets `_routing_profile` or `_routing`, or it
// removes one of them (B2: unset ["_routing"] turns routing back on for a
// tenant a disabling `_routing` turned off — the very change the domain
// policy must see). `_routing_profile` is listed for unset too so that
// widening unsetAllowedKeys cannot leave a routing removal unjudged.
func touchesRouting(op BatchOperation) bool {
	_, profile := op.Patch["_routing_profile"]
	_, routing := op.Patch["_routing"]
	if profile || routing {
		return true
	}
	for _, k := range op.Unset {
		if k == "_routing" || k == "_routing_profile" {
			return true
		}
	}
	return false
}

// readsPolicy reports whether the domain policy judges batch op: it writes
// the flat `_routing_receiver_type` (CheckWrite) or touches routing
// (touchesRouting). Only such an op is refused while the policy is
// unavailable (hub #2486 Q7-2); an unrelated write goes through.
func readsPolicy(op BatchOperation) bool {
	_, flat := op.Patch["_routing_receiver_type"]
	return flat || touchesRouting(op)
}

// batchPolicyUnavailable is the direct-mode batch gate (hub #2486 Q7-2): the
// error policy.Manager.RefusesWrites returns when some op reads the policy
// and the policy is unavailable (and --policy-unavailable-open is off), else
// nil. It lets nothing through on its own account and counts nothing: under
// --policy-unavailable-open each op is let through, logged and counted once
// by executeBatchOps' per-op gate (batchOpPolicyUnavailable), so
// tenant_api_policy_unavailable_open_total counts writes, not requests.
func batchPolicyUnavailable(mgr *policy.Manager, ops []BatchOperation) error {
	if mgr == nil {
		return nil
	}
	for _, op := range ops {
		if readsPolicy(op) {
			return mgr.RefusesWrites()
		}
	}
	return nil
}

// applyBatchEdit lays one op over a tenant block as mergePatchYAML lays it
// over the file: its patch keys set, then its unset keys removed (the two
// are disjoint — validateBatchEdit).
func applyBatchEdit(block map[string]any, op BatchOperation) {
	for k, v := range op.Patch {
		block[k] = v
	}
	for _, k := range op.Unset {
		delete(block, k)
	}
}

// tenantBlockOnDisk is tenants.<tenantID> of the tenant's file in configDir,
// or an empty block for a new tenant. A file that cannot be resolved or read
// is logged and read as empty (fail-open: the patches are judged alone). Read
// by the routing check (batchRoutingViolations) and the metadata post-state
// check (postStateScopeMeta).
func tenantBlockOnDisk(configDir, tenantID string) map[string]any {
	block := map[string]any{}
	path, err := confd.ResolveTenantFile(configDir, tenantID)
	switch {
	case err == nil:
		data, problem := confd.ReadTenantFile(filepath.Dir(path), filepath.Base(path))
		if problem != confd.ProblemNone {
			slog.Warn("batch check: tenant file unreadable, judging the patch alone",
				"tenant", tenantID, "problem", problem)
		}
		for k, v := range extractTenantBlock(data, tenantID) {
			block[k] = v
		}
	case errors.Is(err, confd.ErrTenantFileNotFound):
		// A new tenant: the patches are the whole block.
	default:
		slog.Warn("batch check: tenant file not resolved, judging the patch alone",
			"tenant", tenantID, "error", err)
	}
	return block
}

// batchRoutingViolations judges a batch op's effect on the tenant's routing:
// the tenant's block ON DISK, then each op in prior, then op itself, laid
// over its top level in that order (applyBatchEdit: set, then remove),
// resolved and checked. An op that touches routing neither by its patch nor
// by its unset (touchesRouting) is not judged — an unrelated write is not
// refused for the routing already on disk (documented asymmetry with PUT,
// which always judges the whole body).
//
// prior is the SAME tenant's earlier ops of this request that will be
// applied before this one without being written first — PR mode, where
// WritePRBatch merges every op onto the same base in order. Only ops that
// were taken into the batch belong there. Each carries its unset as well as
// its patch: an earlier op that removes `_routing` re-enables routing under
// every later op, and one that sets `_routing: disable` disables it. Direct
// mode writes each op before judging the next, which reads the file back,
// so it passes nil.
//
// advisories are the non-blocking `require_critical_escalation` leak
// messages (#2325) for the same resolved routing; the caller adds them to
// the op's warnings when the op goes through.
//
// An op whose only routing change is an unset of a key the stacked block
// does not carry changes nothing (mergePatchYAML writes nothing for it,
// gitops.ErrMergeNoOp) and is not judged either: a no-op is not refused for
// the routing already there. judged reports whether the op was judged — the
// caller registers advisories only for an op that was.
func batchRoutingViolations(configDir string, mgr *policy.Manager, prior []BatchOperation, op BatchOperation) (violations []policy.Violation, advisories []string, judged bool) {
	if mgr == nil || !touchesRouting(op) {
		return nil, nil, false
	}
	block := tenantBlockOnDisk(configDir, op.TenantID)
	for _, p := range prior {
		applyBatchEdit(block, p)
	}
	if !changesRouting(block, op) {
		return nil, nil, false
	}
	applyBatchEdit(block, op)
	violations, advisories = judgeTenantBlock(configDir, mgr, op.TenantID, block)
	return violations, advisories, true
}

// changesRouting reports whether op, laid over block (the tenant block it
// will be merged into), changes the routing: its patch sets a routing key,
// or its unset removes one block carries. An unset of an absent key changes
// nothing (mergePatchYAML writes nothing for it) and is not judged.
func changesRouting(block map[string]any, op BatchOperation) bool {
	return touchesRouting(BatchOperation{Patch: op.Patch}) || removesAny(block, op.Unset)
}

// judgeTenantBlock is the judging half shared by every domain-policy check
// of a tenant's routing — PUT (judgePutBody), the batch pre-check
// (batchRoutingViolations: the block on disk with the prior ops stacked) and
// PR mode's in-lock checks (freshBasePolicyCheck, freshBasePutPolicyCheck:
// the block on the fresh base, judged by a policy snapshot read from it).
// block is the tenant file's block; as the route generator does
// (_lib_confd.overlay_platform_tenants), it is laid over the root platform
// files' `tenants.<id>` entries (Layers.TenantBlock: the tenant's own
// `_routing` / `_routing_profile` win whole), then resolved over the routing
// layers in configDir and judged by mgr. So a tenant whose own file carries
// no `_routing` is judged on the one the platform overlay gives it.
func judgeTenantBlock(configDir string, mgr *policy.Manager, tenantID string, block map[string]any) ([]policy.Violation, []string) {
	return mgr.JudgeTenantBlock(tenantID, block, loadRoutingLayers(configDir))
}

// judgePutBody is PUT's domain-policy check of the whole body (a PUT
// replaces the whole file): CheckWrite on its flat keys and the routing its
// tenant block resolves to (judgeTenantBlock). mgr and configDir are the
// pod's (the pre-check) or the fresh base's (freshBasePutPolicyCheck).
func judgePutBody(configDir string, mgr *policy.Manager, tenantID string, body []byte) ([]policy.Violation, []string) {
	violations := mgr.CheckWrite(tenantID, extractPatchKeys(body, tenantID))
	rv, advisories := judgeTenantBlock(configDir, mgr, tenantID, extractTenantBlock(body, tenantID))
	return append(violations, rv...), advisories
}

// freshBasePutPolicyCheck is PUT's authoritative domain-policy check in PR
// mode (#2486), the PUT twin of freshBasePolicyCheck: run by
// Writer.WritePRChecked under the writer lock, with configDir checked out at
// the feature branch cut from the FRESH origin base, before the body is
// written. The pre-check judged the pod's local tree and the policy
// watcher's copy, which may lag the base; here the routing layers, the
// platform overlay and the domain policy (policy.LoadSnapshot) are the
// base's. The whole body is judged, as by the pre-check. loadErr is non-nil
// when the base's policy file cannot be read or parsed: the caller refuses
// the write.
func freshBasePutPolicyCheck(configDir, tenantID string, body []byte) (violations []policy.Violation, loadErr error) {
	snap, err := policy.LoadSnapshot(configDir)
	if err != nil {
		return nil, err
	}
	violations, _ = judgePutBody(configDir, snap, tenantID, body)
	return violations, nil
}

// freshBasePolicyCheck is PR mode's authoritative domain-policy check (B2
// F1), run inside the op's merge closure, under the writer lock, after the
// feature branch is cut from the FRESH origin base. The pre-check judged the
// pod's LOCAL tree (and the policy watcher's copy of its
// `_domain_policy.yaml`), which is synced only at startup and may lag the
// base, so it can pass an op that breaks the policy on the base. Here EVERY
// input comes from the branch: existing is the tenant's file there (with the
// request's earlier ops already committed on it), merged is what this op
// makes of it, and configDir is the working tree checked out at the branch —
// so the routing layers AND the domain policy (policy.LoadSnapshot, the file
// the watcher reads) are the base's. The same two checks as the pre-check:
// CheckWrite on the flat `_routing_receiver_type`, and the resolved routing
// of the merged block for an op that changes it. An op neither concerns is
// not judged, and does not read the policy file. loadErr is non-nil when the
// base's policy file cannot be read or parsed: the caller refuses the batch.
func freshBasePolicyCheck(configDir string, op BatchOperation, existing []byte, merged string) (violations []policy.Violation, loadErr error) {
	_, receiverType := op.Patch["_routing_receiver_type"]
	routing := touchesRouting(op) && changesRouting(extractTenantBlock(existing, op.TenantID), op)
	if !receiverType && !routing {
		return nil, nil
	}
	snap, err := policy.LoadSnapshot(configDir)
	if err != nil {
		return nil, err
	}
	if receiverType {
		violations = append(violations, snap.CheckWrite(op.TenantID, op.Patch)...)
	}
	if routing {
		block := extractTenantBlock([]byte(merged), op.TenantID)
		if block == nil {
			block = map[string]any{}
		}
		rv, _ := judgeTenantBlock(configDir, snap, op.TenantID, block)
		violations = append(violations, rv...)
	}
	return violations, nil
}

// removesAny reports whether block carries any of keys.
func removesAny(block map[string]any, keys []string) bool {
	for _, k := range keys {
		if _, has := block[k]; has {
			return true
		}
	}
	return false
}
