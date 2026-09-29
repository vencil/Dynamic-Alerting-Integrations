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
// nil when the document does not parse or does not declare the tenant.
func extractTenantBlock(body []byte, tenantID string) map[string]any {
	var doc struct {
		Tenants map[string]map[string]any `yaml:"tenants"`
	}
	if err := yaml.Unmarshal(body, &doc); err != nil {
		return nil
	}
	return doc.Tenants[tenantID]
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

// touchesRouting reports whether a flat batch patch changes what the
// tenant's routing resolves to.
func touchesRouting(patch map[string]string) bool {
	_, profile := patch["_routing_profile"]
	_, routing := patch["_routing"]
	return profile || routing
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
// the tenant's block ON DISK, then each patch in prior, then this op's patch,
// laid over its top level in that order, resolved and checked. An op that
// touches neither `_routing_profile` nor `_routing` is not judged — an
// unrelated write is not refused for the routing already on disk (documented
// asymmetry with PUT, which always judges the whole body).
//
// The disk block is load-bearing: a patch judged alone would let
// `_routing: "on"` re-enable a tenant whose file already names a violating
// profile.
//
// prior is the SAME tenant's earlier ops of this request that will be
// applied before this one without being written first — PR mode, where
// WritePRBatch merges every op onto the same base in order (#2280 review:
// `_routing_profile: <violating>` then `_routing: "on"` on a disabled tenant
// each passed alone and stacked into a violation on the PR branch). Only ops
// that were taken into the batch belong there. Direct mode writes each op
// before judging the next, which reads the file back, so it passes nil.
//
// advisories are the non-blocking `require_critical_escalation` leak
// messages (#2325) for the same resolved routing; the caller adds them to
// the op's warnings when the op goes through.
func batchRoutingViolations(configDir string, mgr *policy.Manager, tenantID string, prior []map[string]string, patch map[string]string) (violations []policy.Violation, advisories []string) {
	if mgr == nil || !touchesRouting(patch) {
		return nil, nil
	}
	block := tenantBlockOnDisk(configDir, tenantID)
	for _, p := range prior {
		for k, v := range p {
			block[k] = v
		}
	}
	for k, v := range patch {
		block[k] = v
	}
	return mgr.JudgeTenantRouting(tenantID, block, loadRoutingLayers(configDir))
}
