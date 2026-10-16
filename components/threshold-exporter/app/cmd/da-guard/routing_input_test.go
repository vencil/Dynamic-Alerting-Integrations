package main

// routing_input_test.go — the routing-plane inputs of guard.CheckInput, as
// da-guard's CLI built them before #2904, kept for the Go half of
// tests/shared/routing_policy_parity_matrix.json only.
//
// ⛔ TEST-ONLY, AND TEMPORARY. Since #2904 (ADR-036 §9 step 2) the CLI no
// longer judges routing or domain policy; the route generator does. The
// routing checks of internal/guard and pkg/routingpolicy stay until step 3
// (#2905) deletes them, and until then the parity matrix still holds them to
// the generator — through this helper, which calls them the way the CLI
// did, instead of through the CLI. #2905 deletes this file with them.

import (
	"path/filepath"
	"testing"

	"github.com/vencil/threshold-exporter/internal/confdlayout"
	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"github.com/vencil/threshold-exporter/pkg/tenantid"
)

// pyyamlOwn is the tenant file's block Resolve reads for ec, with the
// receivers, `routes[i].match` values and override `alertname` /
// `metric_group` of its `_routing` as the route generator's PyYAML reads
// them (#2295, #2431). A `_routing` with no PyYAML counterpart is judged
// Unmatched (fail-closed).
func pyyamlOwn(ec *config.EffectiveConfig, pyRouting map[string]map[string]any) map[string]any {
	own := ec.TenantOverridesRaw
	r, has := own["_routing"]
	if !has {
		return own
	}
	py := pyRouting[ec.SourceFile][ec.TenantID]
	out := make(map[string]any, len(own))
	for k, v := range own {
		out[k] = v
	}
	out["_routing"] = routingpolicy.WithPyYAMLRouting(r, py)
	return out
}

// withRoutingInput fills in the routing-plane fields of in — the resolved
// routing per tenant over the whole tree at configDir, the domain policies,
// the routing tree's problems, the invalid / unloaded tenant ids and
// `_routing_enforced` — exactly as the CLI's buildCheckInput did before #2904.
func withRoutingInput(in guard.CheckInput, scoped *config.ScopedTenants, configDir string) guard.CheckInput {
	routing := make(map[string]map[string]any)
	provenance := make(map[string]routingpolicy.Provenance)
	unknownProfiles := make(map[string]string)
	disabled := make(map[string]bool)
	notMapping := make(map[string]any)
	ignoredKeys := make(map[string][]string)
	unknownKeys := make(map[string][]string)
	invalidIDs := make(map[string]string)
	unloaded := make(map[string]string)

	failed := make(map[string]bool, len(scoped.ParseFailed))
	for _, pf := range scoped.ParseFailed {
		failed[pf] = true
	}
	skip := func(rel string) bool { return failed[rel] }
	tree, policies, problems := routingpolicy.LoadTree(configDir, skip)
	problems = append(problems, routingpolicy.UnreadRouting(configDir, scoped.DefaultsFiles, skip)...)

	pyRouting := map[string]map[string]any{}
	pyNotMapping := map[string]map[string]bool{}
	for _, ec := range scoped.Tenants {
		if _, done := pyRouting[ec.SourceFile]; !done {
			data, err := config.ReadConfFile(filepath.Join(config.AbsScanRoot(configDir), filepath.FromSlash(ec.SourceFile)))
			if err == nil {
				pyRouting[ec.SourceFile] = routingpolicy.PyYAMLRoutingByTenant(data)
				pyNotMapping[ec.SourceFile] = routingpolicy.PyYAMLTenantBodyNotMapping(data)
			} else {
				pyRouting[ec.SourceFile] = nil
			}
		}
	}
	for _, ec := range scoped.Tenants {
		if pyNotMapping[ec.SourceFile][ec.TenantID] && !tree.Root.PlatformBodies[ec.TenantID] {
			unloaded[ec.TenantID] = ec.SourceFile
		}
		if !tenantid.Valid(ec.TenantID) {
			invalidIDs[ec.TenantID] = ec.SourceFile
			continue
		}
		layers := tree.LayersFor(confdlayout.LevelOf(ec.SourceFile))
		block := layers.TenantBlock(ec.TenantID, pyyamlOwn(ec, pyRouting))
		if routingpolicy.IsDisabled(block["_routing"]) {
			disabled[ec.TenantID] = true
		}
		if r, has := block["_routing"]; has && routingpolicy.RoutingNotMapping(r) {
			notMapping[ec.TenantID] = r
		}
		own := layers.TenantBlock(ec.TenantID, ec.TenantOverridesRaw)
		if f := routingpolicy.IgnoredKeys(own["_routing"], routingpolicy.BlockRouting); len(f) > 0 {
			ignoredKeys[ec.TenantID] = f
		}
		written := map[string]any{}
		for k := range ec.TenantOverridesRaw {
			written[k] = nil
		}
		for k := range layers.OverlayUnknownKeys[ec.TenantID] {
			written[k] = nil
		}
		if f := routingpolicy.UnknownRoutingKeys(written, routingpolicy.LayerTenant); len(f) > 0 {
			unknownKeys[ec.TenantID] = f
		}
		resolved, ok, prov, unknown := routingpolicy.Resolve(ec.TenantID, block, layers)
		if ok {
			routing[ec.TenantID] = resolved
			provenance[ec.TenantID] = prov
		}
		if unknown != nil {
			unknownProfiles[ec.TenantID] = *unknown
		}
	}

	in.RoutingByTenant = routing
	in.RoutingProvenance = provenance
	in.RoutingDisabled = disabled
	in.RoutingNotMapping = notMapping
	in.RoutingIgnoredKeys = ignoredKeys
	in.RoutingUnknownKeys = unknownKeys
	in.InvalidTenantIDs = invalidIDs
	in.UnloadedTenants = unloaded
	in.UnknownRoutingProfiles = unknownProfiles
	in.DomainPolicies = policies
	in.PlatformProblems = problems
	in.RoutingEnforced = tree.Enforced
	return in
}

// routingGuardFindings is the parity matrix's Go half for one tree: the
// guard over the exporter's resolution of configDir with the routing inputs
// above. stop is non-nil when the exporter refuses the tree before any check
// (a duplicate tenant id: ScopeEffective's error).
//
// ⛔ Test-only since #2904: the CLI no longer runs these checks. This helper
// and withRoutingInput go with the Go routing code (internal/guard routing,
// pkg/routingpolicy) in ADR-036 step 3 (#2905).
func routingGuardFindings(t *testing.T, configDir string) (findings []jsonFinding, parseFailed []string, stop error) {
	t.Helper()
	scoped, err := config.ScopeEffective(configDir, "")
	if err != nil {
		return nil, nil, err
	}
	// No withGeneratorDuplicates: since #2904 it also names a `_domain_policy`
	// / `_routing_profiles` file the generator refuses whole, which LoadTree
	// reports as a Problem here, as the matrix's platform rows expect; every
	// other matrix tree has none (the test requires parse_failed empty).
	in := withRoutingInput(buildCheckInput(scoped, &flags{configDir: configDir}), scoped, configDir)
	report, err := guard.CheckDefaultsImpact(in)
	if err != nil {
		t.Fatalf("guard: %v", err)
	}
	for _, f := range report.Findings {
		findings = append(findings, jsonFinding{Severity: string(f.Severity), Kind: f.Kind.String(), TenantID: f.TenantID, Field: f.Field})
	}
	return findings, scoped.ParseFailed, nil
}
