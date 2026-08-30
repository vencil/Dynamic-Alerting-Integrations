package profile

// emit_carrier_roundtrip_test.go — the emitter half of #1605.
//
// The allocator half (internal/batchpr) pins what happens to a NAME. This half
// pins the step before it: the emitter INVENTS those names, via
// `safeFilename(tenantID)+".yaml"`, and the allocator recovers the tenant id
// back out of the filename. Those two are a round trip nobody had checked.
//
// ⛔ Tenant ids here are Prometheus label VALUES — `tenantIDForRule` reads them
// straight off `ParsedRule.Labels`, and `ProposalRef.MemberTenantIDs` is
// documented as "computed from the underlying ParsedRule.Labels". Nothing
// upstream constrains a label value to be a legal, unique, round-trippable
// filename. `safeFilename` is neither the identity (it maps `/` and `\` to `-`
// and strips leading dots) nor injective, and its range overlaps this
// emitter's own fixed filenames.
//
// Measured on cefb565, before the guard these tests pin:
//
//	tenants `a/b` + `a-b`      -> ONE file `a-b.yaml`, one proposal lost,
//	                              warnings: []
//	tenant  `_defaults`        -> the shared inheritance-chain document is
//	                              REPLACED by that tenant's `tenants:` block,
//	                              warnings: []
//
// ⛔ Every case below runs the exported EmitProposals, not safeFilename
// directly. A test that called the helper would have been green for the whole
// life of this defect: the helper does exactly what it says, and the damage is
// entirely in what its callers do with the answer.

import (
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// twoTenantProposal builds a one-proposal set whose two member rules carry the
// two given tenant label values. Everything else is held constant so each
// outcome is attributable to the ids.
func twoTenantProposal(idA, idB string) (*ProposalSet, []parser.ParsedRule) {
	mk := func(id, rid, thr string) parser.ParsedRule {
		return parser.ParsedRule{
			SourceRuleID: rid,
			Alert:        "HighCPU",
			Expr:         `avg(rate(node_cpu_seconds_total{tenant="` + id + `"}[5m])) > ` + thr,
			For:          "5m",
			Labels:       map[string]string{"tenant": id, "severity": "warning"},
			Dialect:      parser.DialectProm,
		}
	}
	rules := []parser.ParsedRule{
		mk(idA, "s.yaml#g[0].r[0]", "0.11"),
		mk(idB, "s.yaml#g[0].r[1]", "0.99"),
	}
	ps := &ProposalSet{Proposals: []ExtractionProposal{{
		MemberRuleIDs:      []string{"s.yaml#g[0].r[0]", "s.yaml#g[0].r[1]"},
		SharedExprTemplate: `avg(rate(node_cpu_seconds_total{tenant="<STR>"}[<NUM>m]))><NUM>`,
		SharedFor:          "5m",
		SharedLabels:       map[string]string{"severity": "warning"},
		VaryingLabelKeys:   []string{"tenant"},
		Dialect:            string(parser.DialectProm),
		Confidence:         ConfidenceHigh,
		Reason:             "two members sharing one template",
	}}}
	return ps, rules
}

func emitOneProposalFor(t *testing.T, ps *ProposalSet, rules []parser.ParsedRule) *EmissionOutput {
	t.Helper()
	out, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"dom"}, RootPrefix: "conf.d/"},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	return out
}

func sortedCarrierKeys(files map[string][]byte) []string {
	out := make([]string, 0, len(files))
	for k := range files {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// warningNaming returns the first warning mentioning every needle, or "".
func warningNaming(warnings []string, needles ...string) string {
	for _, w := range warnings {
		all := true
		for _, n := range needles {
			if !strings.Contains(w, n) {
				all = false
				break
			}
		}
		if all {
			return w
		}
	}
	return ""
}

// TestEmit_TwoTenantsCollidingOnOneCarrierNameKeepsBothOrSaysSo is the
// injectivity case. `a/b` and `a-b` are two different tenants that
// `safeFilename` maps onto one name.
//
// ⛔ THIS TEST ASSERTS ON THE EMITTED DOCUMENT, NOT ON WARNING PROSE, and the
// reason is a measured near-miss: the first version of it asked only "does some
// warning mention `a/b`", which a blind reviewer showed was satisfied by the
// UNRELATED round-trip warning — that one fires for `a/b` whether or not the
// collision guard exists. With the guard deleted the test stayed GREEN while
// last-write-wins silently dropped a tenant, i.e. the test named for this bug
// did not detect this bug. Worse, its own failure message said "no warning names
// the tenant that LOST its file" while asserting on `a/b`, the tenant that KEPT
// its file — the report and the action disagreeing, which is the very shape this
// family exists to kill.
//
// The emitter walks `prop.MemberRuleIDs` in slice order, so `a/b` is written
// first and `a-b` is the one that must be refused. Pinning WHICH document
// survives is what makes the assertion sensitive to keep-first.
func TestEmit_TwoTenantsCollidingOnOneCarrierNameKeepsBothOrSaysSo(t *testing.T) {
	t.Parallel()
	const first, second = "a/b", "a-b"
	ps, rules := twoTenantProposal(first, second)
	out := emitOneProposalFor(t, ps, rules)

	carriers := map[string]bool{}
	for k := range out.Files {
		if strings.HasSuffix(k, ".yaml") && !strings.HasSuffix(k, "_defaults.yaml") {
			carriers[k] = true
		}
	}
	if len(carriers) == 2 {
		return // both tenants got their own carrier; nothing was lost
	}

	// One file for two tenants. The surviving document must be the FIRST
	// writer's — anything else means a later write silently replaced an
	// earlier tenant's proposal.
	body := string(out.Files["conf.d/dom/a-b.yaml"])
	if !strings.Contains(body, first+":") {
		t.Errorf("tenants %q and %q collapse onto one carrier and the surviving "+
			"document belongs to %q, not to the tenant written first (%q).\n"+
			"  last-write-wins here means one tenant's proposed thresholds are "+
			"simply gone while the report says the proposal was emitted.\n"+
			"  conf.d/dom/a-b.yaml was:\n%s", first, second, second, first, body)
	}

	// And the refusal must be named — specifically the refusal, not the
	// round-trip warning that fires for `a/b` regardless.
	if w := warningNaming(out.Warnings, second, "overwrite"); w == "" {
		t.Errorf("tenant %q lost its carrier to a name collision and NO warning "+
			"names both that tenant and the overwrite it was refused.\n"+
			"  ⛔ a warning that merely mentions %q does not count: the round-trip "+
			"warning says that whether or not this collision was handled.\n"+
			"  emitted: %v\n  warnings: %v",
			second, first, sortedCarrierKeys(out.Files), out.Warnings)
	}
}

// TestEmit_ATenantNamedDefaultsDoesNotDestroyTheChainCarrier is the
// range-overlap case, and the worse of the two: the collision is not with
// another tenant but with this emitter's own shared-structure document.
func TestEmit_ATenantNamedDefaultsDoesNotDestroyTheChainCarrier(t *testing.T) {
	t.Parallel()
	ps, rules := twoTenantProposal("_defaults", "ordinary")
	out := emitOneProposalFor(t, ps, rules)

	defaults := string(out.Files["conf.d/dom/_defaults.yaml"])
	if defaults == "" {
		t.Fatalf("no chain carrier emitted at all; files: %v", sortedCarrierKeys(out.Files))
	}
	if strings.Contains(defaults, "tenants:") {
		t.Errorf("the inheritance-chain carrier conf.d/dom/_defaults.yaml holds a "+
			"per-tenant override block — tenant %q's file overwrote the shared "+
			"structure document.\n"+
			"  the exporter merges this file into EVERY tenant's chain, so the "+
			"proposal's shared structure is gone and one tenant's overrides are "+
			"now cascading to all of them.\n  content was:\n%s", "_defaults", defaults)
	}
	if w := warningNaming(out.Warnings, "_defaults"); w == "" {
		t.Errorf("tenant %q collides with the emitter's own chain-carrier filename "+
			"and NO warning names it.\n  warnings: %v", "_defaults", out.Warnings)
	}
}

// TestEmit_ACarrierNameThatDoesNotReadBackAsItsTenantSaysSo is the identity
// case, without any collision: `a/b` alone still emits `a-b.yaml`, and the
// allocator will attribute that carrier to a tenant called `a-b`.
func TestEmit_ACarrierNameThatDoesNotReadBackAsItsTenantSaysSo(t *testing.T) {
	t.Parallel()
	ps, rules := twoTenantProposal("a/b", "plain")
	out := emitOneProposalFor(t, ps, rules)

	if _, ok := out.Files["conf.d/dom/a-b.yaml"]; !ok {
		t.Fatalf("expected the mangled carrier name to still be emitted; files: %v",
			sortedCarrierKeys(out.Files))
	}
	if w := warningNaming(out.Warnings, "a/b", "a-b.yaml"); w == "" {
		t.Errorf("tenant %q emits carrier %q, which reads back as tenant %q — the "+
			"batch-PR allocator recovers the id from the filename, so this carrier "+
			"reaches no PR for that tenant — and NO warning names both.\n  warnings: %v",
			"a/b", "a-b.yaml", "a-b", out.Warnings)
	}
	// The ordinary tenant must be untouched by the guard.
	if _, ok := out.Files["conf.d/dom/plain.yaml"]; !ok {
		t.Errorf("the well-named tenant lost its carrier; files: %v", sortedCarrierKeys(out.Files))
	}
	if w := warningNaming(out.Warnings, "plain"); w != "" {
		t.Errorf("a well-named tenant drew a warning it should not have: %q", w)
	}
}
