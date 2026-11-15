// Package aminhibit evaluates the repo's HAND-WRITTEN Alertmanager configs
// against Alertmanager's own matcher implementation.
//
// Why this exists (#1132): try-local's Severity Dedup inhibit rule was broken
// from the day it was written until #1132 — bidirectionally. Its `equal:` listed
// `metric_group`, but its source (the TenantSeverityDedupEnabled sentinel) never
// carries that label, so dedup could never fire; and because Alertmanager treats
// a label missing from BOTH sides as equal, the same rule silently suppressed
// warnings that also lacked it (TenantConfigEvent). `amtool check-config` is
// green on all of that — it only validates syntax. Nothing in the repo tested
// inhibit SEMANTICS, so the bug survived ~2 years.
//
// Scope: the two hand-written configs, and the generator's dedup rule.
//   - try-local/alertmanager.yml                     (hand-written)
//   - k8s/03-monitoring/configmap-alertmanager.yaml  (the SOURCE the generator
//     merges into: Silent Mode / Custom Alert rules, no dedup rule — the merge
//     refuses a source that holds one, #3899)
//   - testdata/generated_dedup_inhibit.yaml          (the generator's REAL
//     severity dedup rule for a fixed dedup_configs, written and drift-checked
//     by tests/ops/test_generated_dedup_inhibit_fixture.py)
//
// The dedup rule is emitted by scripts/tools/ops/_grar_routes.py
// (build_dedup_inhibit_rule): ONE rule for every tenant, with the tenants set
// to `_severity_dedup: disable` in its target's tenant!~ list. Reading its
// actual output here means Alertmanager's own matcher — not a re-derivation —
// judges what the generator writes.
package aminhibit

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/alertmanager/config/common"
	"github.com/prometheus/alertmanager/pkg/labels"
	"gopkg.in/yaml.v3"
)

// repoRoot resolves paths relative to this module (tests/alertmanager-inhibit).
func repoRoot(rel string) string { return filepath.Join("..", "..", rel) }

// lset is an alert's label set as Alertmanager sees it at match time.
//
// A Go map returns "" for an absent key, which is exactly Alertmanager's rule:
// "a missing label and a label with an empty value are the same thing"
// (prometheus/alertmanager#1727, #507). That equivalence is the whole reason the
// #1132 bug had a second, silent failure direction, so the fixtures below rely
// on it rather than modelling absence separately.
type lset map[string]string

// matches reports whether a label set satisfies every matcher, using
// Alertmanager's own labels.Matcher.Matches — so regex anchoring, `=~ ".+"`
// presence gating and empty-value handling are AM's, not ours.
func matches(ms common.Matchers, l lset) bool {
	for _, m := range ms {
		if !m.Matches(l[m.Name]) {
			return false
		}
	}
	return true
}

// equalHolds implements the `equal:` comparison: every listed label must have
// the same value on both alerts, with missing == "".
//
// This mirrors inhibit.go, which builds the equal-label subset of each alert and
// compares them. It is the one piece of semantics this harness states itself
// (three lines); everything hairy — matcher parsing, regex compilation, matching
// — comes from Alertmanager. TestHarnessReproducesTheHistoricalBug pins that
// this reproduces real AM behaviour on the one case we have ground truth for.
func equalHolds(eq []string, src, tgt lset) bool {
	for _, n := range eq {
		if src[n] != tgt[n] {
			return false
		}
	}
	return true
}

// inhibits reports whether one rule lets `src` suppress `tgt`.
//
// The middle clause mirrors Alertmanager's two-sided-match exclusion: when the
// TARGET also satisfies source_matchers, a source alert that itself satisfies
// target_matchers does NOT inhibit it (inhibit.go computes
// `excludeTwoSidedMatch := r.SourceMatchers.Matches(target)` and then skips any
// candidate source whose labels match TargetMatchers). Without it, a rule whose
// two sides overlap would look mutually-inhibiting here but not in production.
// Every rule in the repo today has disjoint sides (critical vs warning,
// severity=none sentinel vs the real alert), so this changes no assertion
// below — it is here so an overlapping rule added later is judged correctly
// rather than silently mis-modelled.
func inhibits(r common.InhibitRule, src, tgt lset) bool {
	if !matches(r.SourceMatchers, src) || !matches(r.TargetMatchers, tgt) {
		return false
	}
	if matches(r.SourceMatchers, tgt) && matches(r.TargetMatchers, src) {
		return false
	}
	return equalHolds(r.Equal, src, tgt)
}

// muted reports whether ANY rule in the config suppresses tgt given that src is
// also firing — Alertmanager's Inhibitor.Mutes, minus only the alert-lifecycle
// machinery (resolved-alert GC, the source-alert index/cache). Those are runtime
// bookkeeping over WHICH alerts are currently firing; they cannot change whether
// a CONFIG expresses the intended relation between two given alerts, which is
// what this suite asserts.
func muted(rules []common.InhibitRule, src, tgt lset) bool {
	for _, r := range rules {
		if inhibits(r, src, tgt) {
			return true
		}
	}
	return false
}

// rejectDeprecatedForms fails loudly if a rule uses the pre-0.22 map syntax.
// This harness only understands `source_matchers`/`target_matchers`; silently
// skipping a `source_match:` rule would make the gate fail OPEN — reporting
// green on a config it never actually evaluated.
func rejectDeprecatedForms(t *testing.T, path string, rules []common.InhibitRule) {
	t.Helper()
	for i, r := range rules {
		if len(r.SourceMatch) > 0 || len(r.TargetMatch) > 0 ||
			len(r.SourceMatchRE) > 0 || len(r.TargetMatchRE) > 0 {
			t.Fatalf("%s rule[%d]: deprecated source_match/target_match syntax is not "+
				"evaluated by this gate; migrate it to source_matchers/target_matchers "+
				"or teach the gate to read it (leaving it would silently fail open)", path, i)
		}
	}
}

type amConfig struct {
	InhibitRules []common.InhibitRule `yaml:"inhibit_rules"`
}

// parseAM unmarshals an alertmanager.yml body. common.InhibitRule's own
// UnmarshalYAML compiles the matcher expressions, so a malformed matcher fails
// here exactly as it would in Alertmanager.
func parseAM(t *testing.T, path, body string) []common.InhibitRule {
	t.Helper()
	var c amConfig
	if err := yaml.Unmarshal([]byte(body), &c); err != nil {
		t.Fatalf("%s: parse: %v", path, err)
	}
	if len(c.InhibitRules) == 0 {
		t.Fatalf("%s: no inhibit_rules found — the gate would vacuously pass", path)
	}
	rejectDeprecatedForms(t, path, c.InhibitRules)
	return c.InhibitRules
}

// loadPlainAM reads a bare alertmanager.yml (try-local).
func loadPlainAM(t *testing.T, rel string) []common.InhibitRule {
	t.Helper()
	p := repoRoot(rel)
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read %s: %v", p, err)
	}
	return parseAM(t, rel, string(b))
}

// loadConfigMapAM reads an alertmanager.yml embedded in a k8s ConfigMap.
func loadConfigMapAM(t *testing.T, rel, key string) []common.InhibitRule {
	t.Helper()
	p := repoRoot(rel)
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read %s: %v", p, err)
	}
	var cm struct {
		Data map[string]string `yaml:"data"`
	}
	if err := yaml.Unmarshal(b, &cm); err != nil {
		t.Fatalf("%s: parse configmap: %v", rel, err)
	}
	body, ok := cm.Data[key]
	if !ok {
		t.Fatalf("%s: configmap has no data[%q]", rel, key)
	}
	return parseAM(t, fmt.Sprintf("%s data[%s]", rel, key), body)
}

// sideGatesLabel reports whether this matcher set GUARANTEES `label` is present
// (non-empty). A missing label reads as "" in Alertmanager, so "guarantees
// present" == "some matcher on `label` does NOT match the empty string" —
// decided by Alertmanager's OWN matcher (m.Matches), not a re-derivation. This
// is the Go mirror of _grar_validate._matchers_gate_label_present (the generator
// invariant), so the repo gate and the generator agree by construction.
func sideGatesLabel(ms common.Matchers, label string) bool {
	for _, m := range ms {
		if m.Name == label && !m.Matches("") {
			return true
		}
	}
	return false
}

// pinnedTenant returns the tenant id a matcher set pins to a literal, if any.
func pinnedTenant(ms common.Matchers) (string, bool) {
	for _, m := range ms {
		if m.Name == "tenant" && m.Type == labels.MatchEqual {
			return m.Value, true
		}
	}
	return "", false
}

// check is one assertion: with src firing, is tgt suppressed?
type check struct {
	name string
	src  lset
	tgt  lset
	want bool
	why  string
}

func run(t *testing.T, rules []common.InhibitRule, cases []check) {
	t.Helper()
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := muted(rules, c.src, c.tgt)
			if got != c.want {
				t.Errorf("muted=%v want=%v\n  source: %v\n  target: %v\n  why: %s",
					got, c.want, c.src, c.tgt, c.why)
			}
		})
	}
}
