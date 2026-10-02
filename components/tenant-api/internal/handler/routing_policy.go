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
// the policy watcher (`_domain_policy.yaml`), so LoadRoot's policy problems
// are not repeated here.
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
// is logged and read as empty (fail-open: the patches are judged alone).
func tenantBlockOnDisk(configDir, tenantID string) map[string]any {
	block := map[string]any{}
	path, err := confd.ResolveTenantFile(configDir, tenantID)
	switch {
	case err == nil:
		data, problem := confd.ReadTenantFile(filepath.Dir(path), filepath.Base(path))
		if problem != confd.ProblemNone {
			slog.Warn("batch routing policy check: tenant file unreadable, judging the patch alone",
				"tenant", tenantID, "problem", problem)
		}
		for k, v := range extractTenantBlock(data, tenantID) {
			block[k] = v
		}
	case errors.Is(err, confd.ErrTenantFileNotFound):
		// A new tenant: the patches are the whole block.
	default:
		slog.Warn("batch routing policy check: tenant file not resolved, judging the patch alone",
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
	if !touchesRouting(BatchOperation{Patch: op.Patch}) && !removesAny(block, op.Unset) {
		return nil, nil, false
	}
	applyBatchEdit(block, op)
	violations, advisories = mgr.JudgeTenantRouting(op.TenantID, block, loadRoutingLayers(configDir))
	return violations, advisories, true
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
