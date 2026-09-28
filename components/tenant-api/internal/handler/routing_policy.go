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
		if p.Kind == routingpolicy.ProblemDomainPolicyUnusable && p.File != "" {
			continue
		}
		slog.Warn("routing layer not applied to the domain-policy check",
			"kind", p.Kind, "file", p.File, "field", p.Field, "detail", p.Message)
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

// batchRoutingViolations judges a batch op's effect on the tenant's routing:
// the tenant's block on disk with the patch's scalars laid over its top
// level, resolved and checked. An op that touches neither `_routing_profile`
// nor `_routing` is not judged — an unrelated write is not refused for the
// routing already on disk (documented asymmetry with PUT, which always
// judges the whole body).
func batchRoutingViolations(configDir string, mgr *policy.Manager, tenantID string, patch map[string]string) []policy.Violation {
	if mgr == nil || !touchesRouting(patch) {
		return nil
	}
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
		// A new tenant: the patch is the whole block.
	default:
		slog.Warn("batch routing policy check: tenant file not resolved, judging the patch alone",
			"tenant", tenantID, "error", err)
	}
	for k, v := range patch {
		block[k] = v
	}
	return mgr.CheckTenantRouting(tenantID, block, loadRoutingLayers(configDir))
}
