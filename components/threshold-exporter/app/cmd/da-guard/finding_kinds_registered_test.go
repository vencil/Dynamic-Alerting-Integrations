package main

// finding_kinds_registered_test.go — every finding da-guard reports on the
// fixture trees carries a registered kind (#2904).
//
// findingkind.Kind is closed: a finding's kind is one of findingkind.All()
// or the zero Kind, which is what a finding gets when its check chose no
// kind. Writing the report as JSON refuses the zero Kind; this test finds it
// in process, over the trees the other tests already hold da-guard to: every
// tree of the routing-policy parity matrix and every tests/golden fixture,
// with --required-fields and a cardinality limit set so those checks run too.

import (
	"path/filepath"
	"sort"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestDaGuard_FixtureFindingsHaveRegisteredKinds(t *testing.T) {
	t.Parallel()
	registered := map[findingkind.Kind]bool{}
	for _, e := range findingkind.All() {
		registered[e.Kind] = true
	}

	trees := map[string]string{} // name -> conf.d
	for _, tree := range loadRoutingPolicyMatrix(t) {
		trees["matrix/"+tree.Name] = writeParityTree(t, tree.Files)
	}
	golden, err := filepath.Glob(filepath.Join(goldenDir(t), "fixtures", "*", "conf.d"))
	if err != nil {
		t.Fatal(err)
	}
	for _, dir := range golden {
		trees["golden/"+filepath.Base(filepath.Dir(dir))] = dir
	}
	names := make([]string, 0, len(trees))
	for name := range trees {
		names = append(names, name)
	}
	sort.Strings(names)

	seen := map[findingkind.Kind]bool{}
	judged := 0
	for _, name := range names {
		dir := trees[name]
		scoped, err := config.ScopeEffective(dir, dir)
		if err != nil {
			// A tree the exporter refuses before any check (a duplicate
			// tenant id, a decode stop) produces no findings to look at.
			t.Logf("%s: not judged: %v", name, err)
			continue
		}
		input := buildCheckInput(scoped, &flags{
			configDir:            dir,
			requiredFields:       "mysql_connections",
			cardinalityLimit:     1,
			cardinalityWarnRatio: 0.5,
		})
		report, err := guard.CheckDefaultsImpact(input)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		judged++
		for _, f := range report.Findings {
			switch {
			case f.Kind.IsZero():
				t.Errorf("%s: finding with the zero Kind (no kind chosen): %+v", name, f)
			case !registered[f.Kind]:
				t.Errorf("%s: finding kind %q is not in findingkind.All(): %+v", name, f.Kind, f)
			}
			seen[f.Kind] = true
		}
	}
	// Not vacuous: the trees were judged and produced findings.
	if judged == 0 || len(seen) == 0 {
		t.Errorf("judged %d trees, %d distinct kinds seen: the fixtures no longer exercise the checks", judged, len(seen))
	}
	t.Logf("judged %d of %d trees; %d distinct kinds", judged, len(names), len(seen))
}
