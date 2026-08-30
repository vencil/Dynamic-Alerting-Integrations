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

// TestEmit_TwoTenantsCollidingOnOneCarrierNameSayWhoWasDisplaced is the
// injectivity case. `a/b` and `a-b` are two different tenants that
// `safeFilename` maps onto one name.
//
// ⛔ THE SPEC HERE IS "SAY SO", NOT "KEEP THE FIRST ONE", and that is a
// measured decision rather than a default. An earlier version of this change
// made tenant-vs-tenant collisions keep-first; a blind reviewer measured that
// against `cefb565` and found it changed the ALERTING THRESHOLDS emitted for a
// wholly ordinary corpus (`db-a`/`db-b` across two regions, no exotic ids:
// 0.85 -> 0.70 and 0.90 -> 0.60), because two member rules for one tenant in
// one proposal collide too and base kept the LAST. Neither survivor is more
// correct, so the defect worth fixing is the silence.
//
// ⛔ AND THE ASSERTION IS ON THE DISPLACED DOCUMENT BEING NAMED, not on any
// warning merely mentioning a tenant. The first version of this test asked only
// "does some warning mention `a/b`", which a blind reviewer showed was satisfied
// by the UNRELATED round-trip warning — that one fires for `a/b` whether or not
// the collision is reported at all, so the test named for this bug did not
// detect this bug.
func TestEmit_TwoTenantsCollidingOnOneCarrierNameSayWhoWasDisplaced(t *testing.T) {
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
		return // both tenants got their own carrier; nothing was displaced
	}

	// One file for two tenants. The warning must name the tenant that was
	// displaced AND the rule its document came from — without the source
	// rule id the operator cannot tell which threshold vanished.
	//
	// ⛔ `first` alone is not enough to satisfy this: the round-trip warning
	// already names it. Requiring the SOURCE RULE ID of the displaced
	// document is what makes this assertion specific to the collision.
	displacedRule := "s.yaml#g[0].r[0]" // `first` is written first, so it loses
	if w := warningNaming(out.Warnings, second, displacedRule); w == "" {
		t.Errorf("tenants %q and %q collapse onto one carrier and NO single warning "+
			"names both the surviving tenant %q and the rule %s whose document it "+
			"displaced.\n"+
			"  ⛔ a warning that merely mentions %q does not count: the round-trip "+
			"warning says that whether or not the collision was reported.\n"+
			"  a proposal that drops one of two tenants' thresholds while the report "+
			"says it was emitted is exactly the #1339 shape.\n"+
			"  emitted: %v\n  warnings: %v",
			first, second, second, displacedRule, first,
			sortedCarrierKeys(out.Files), out.Warnings)
	}
}

// TestEmit_AReservedTenantIdIsNamedEvenWithoutACollision closes a blind spot a
// mutation reviewer measured: with `_defaults` as the only reserved-id case,
// deleting the reserved-prefix rule from `confdname.TenantNamedBy` left the
// whole suite GREEN, because the `_defaults` fixture's OVERWRITE warning also
// contains the string `_defaults` and the assertion could not tell the two
// warnings apart. `_rbac` is reserved, is a legal Prometheus label value, and
// collides with nothing — so only the round-trip warning can satisfy it.
func TestEmit_AReservedTenantIdIsNamedEvenWithoutACollision(t *testing.T) {
	t.Parallel()
	ps, rules := twoTenantProposal("_rbac", "plain")
	out := emitOneProposalFor(t, ps, rules)

	if _, ok := out.Files["conf.d/dom/_rbac.yaml"]; !ok {
		t.Fatalf("expected the reserved-name carrier to still be emitted; files: %v",
			sortedCarrierKeys(out.Files))
	}
	if w := warningNaming(out.Warnings, "_rbac", "will not reach"); w == "" {
		t.Errorf("tenant %q emits carrier %q, which the exporter never reads as a "+
			"tenant carrier at all (reserved `_` prefix) — so the batch-PR allocator "+
			"drops it and that tenant gets no PR — and NO warning says so.\n"+
			"  ⛔ this case must not collide with anything: a collision warning that "+
			"happens to contain the same substring would mask the round-trip rule "+
			"going missing.\n  warnings: %v",
			"_rbac", "_rbac.yaml", out.Warnings)
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
	if w := warningNaming(out.Warnings, "_defaults", "refused"); w == "" {
		t.Errorf("tenant %q is written under the chain-carrier name and NO warning "+
			"says the write was refused.\n  warnings: %v", "_defaults", out.Warnings)
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

// TestEmit_ACaseVariantOfTheChainCarrierNameIsRefusedToo closes a hole a blind
// reviewer measured in the FIRST version of the refusal above.
//
// ⛔ That version asked "is this path already occupied, and if so is it the
// defaults name" — a byte-exact map lookup guarding a case-FOLDING predicate.
// Tenant `_DEFAULTS` emits `_DEFAULTS.yaml`, which collides with
// `_defaults.yaml` on no byte at all, so the refusal never fired: the file was
// written, with a `tenants:` block, and `confdname.IsDefaults` says true — so
// the allocator routes it into the Base PR as a defaults carrier and the
// exporter, which lowercases before comparing, merges it into EVERY tenant's
// chain in that directory.
//
// One reader comparing bytes beside another comparing folded case is this
// family's entire shape, and the guard against it had it inside itself.
func TestEmit_ACaseVariantOfTheChainCarrierNameIsRefusedToo(t *testing.T) {
	t.Parallel()
	ps, rules := twoTenantProposal("_DEFAULTS", "ordinary")
	out := emitOneProposalFor(t, ps, rules)

	if body, ok := out.Files["conf.d/dom/_DEFAULTS.yaml"]; ok {
		t.Errorf("tenant %q was written to %q, a name every reader of this tree "+
			"classifies as the inheritance-chain carrier (it differs from "+
			"`_defaults.yaml` in case only, and every classifier here folds case).\n"+
			"  ⛔ it collides with nothing byte-for-byte, so an occupancy check "+
			"cannot see it — the refusal has to be on the NAME.\n"+
			"  the exporter would merge this into every tenant's chain in the "+
			"directory. Content was:\n%s", "_DEFAULTS", "_DEFAULTS.yaml", string(body))
	}
	if w := warningNaming(out.Warnings, "_DEFAULTS", "refused"); w == "" {
		t.Errorf("tenant %q was not written under the chain-carrier name and NO "+
			"warning says why.\n  warnings: %v", "_DEFAULTS", out.Warnings)
	}
	// The ordinary tenant must be untouched.
	if _, ok := out.Files["conf.d/dom/ordinary.yaml"]; !ok {
		t.Errorf("the well-named tenant lost its carrier; files: %v",
			sortedCarrierKeys(out.Files))
	}
}

// TestEmit_OneTenantWithTwoRulesIsNotReportedAsTwoTenants pins that the
// collision warning states the RIGHT CAUSE.
//
// ⛔ A blind reviewer measured the first version saying "two tenants share one
// carrier name" for the case the change's own A/B had used as its headline
// example: ONE tenant with two member rules in a proposal (clustering keys on
// expr+for+dialect, so duplicates of one tenant land together). An operator
// reading that goes looking for a second tenant that does not exist. The
// emitter has both ids in hand and no excuse for the wrong sentence.
func TestEmit_OneTenantWithTwoRulesIsNotReportedAsTwoTenants(t *testing.T) {
	t.Parallel()
	ps, rules := twoTenantProposal("db-a", "db-a")
	out := emitOneProposalFor(t, ps, rules)

	w := warningNaming(out.Warnings, "db-a", "s.yaml#g[0].r[0]", "s.yaml#g[0].r[1]")
	if w == "" {
		t.Fatalf("one tenant's two member rules collided and no warning names both "+
			"rules.\n  warnings: %v", out.Warnings)
	}
	if strings.Contains(w, "tenants ") || strings.Contains(w, "two tenants") {
		t.Errorf("the collision warning blames two tenants, but this fixture has "+
			"ONE tenant with two member rules:\n  %s\n"+
			"  an operator reading that goes hunting for a second tenant that does "+
			"not exist — and the emitter has both ids in hand.", w)
	}
}
