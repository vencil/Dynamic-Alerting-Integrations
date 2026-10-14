package guard

// routing_kind_mapping_test.go — the two places routing.go turns another
// package's problem kind into a finding kind map every such kind (#2904 N1).
// A kind left out maps to the zero findingkind.Kind, which a report refuses;
// these tests find it before a report does, from the other package's own
// declarations rather than from a list kept here.

import (
	"go/constant"
	"go/importer"
	"go/token"
	"go/types"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// stringConsts type-checks the package at path from source and returns the
// value of every package-level string constant keep accepts.
func stringConsts(t *testing.T, path string, keep func(*types.Const) bool) []string {
	t.Helper()
	pkg, err := importer.ForCompiler(token.NewFileSet(), "source", nil).Import(path)
	if err != nil {
		t.Fatalf("type-check %s: %v", path, err)
	}
	var out []string
	for _, name := range pkg.Scope().Names() {
		c, ok := pkg.Scope().Lookup(name).(*types.Const)
		if !ok || c.Val().Kind() != constant.String || !keep(c) {
			continue
		}
		out = append(out, constant.StringVal(c.Val()))
	}
	sort.Strings(out)
	if len(out) == 0 {
		t.Fatalf("%s: no constant found; the test is not looking at the package", path)
	}
	return out
}

func TestReceiverFindingKindCoversEveryReceiverspecKind(t *testing.T) {
	kinds := stringConsts(t, "github.com/vencil/threshold-exporter/pkg/receiverspec", func(c *types.Const) bool {
		named, ok := c.Type().(*types.Named)
		return ok && named.Obj().Name() == "Kind" && named.Obj().Pkg() == c.Pkg()
	})
	// By the key's text: importing receiverspec here would add a user to
	// tests/shared/phase2_deletion_scope.json (ADR-036 §11).
	mapped := map[string]findingkind.Kind{}
	for k, fk := range receiverFindingKind {
		mapped[string(k)] = fk
	}
	for _, k := range kinds {
		if fk := mapped[k]; fk.IsZero() {
			t.Errorf("receiverspec kind %q has no finding kind in receiverFindingKind", k)
		}
	}
}

func TestPlatformProblemFindingsCoverEveryProblemKind(t *testing.T) {
	kinds := stringConsts(t, "github.com/vencil/threshold-exporter/pkg/routingpolicy", func(c *types.Const) bool {
		return c.Exported() && strings.HasPrefix(c.Name(), "Problem")
	})
	for _, k := range kinds {
		got := platformProblemFindings([]routingpolicy.Problem{{Kind: k, File: "f.yaml", Message: "m"}})
		// Each finding kind is named after the Problem kind it reports (#2326).
		if len(got) != 1 || got[0].Kind.String() != k {
			t.Errorf("routingpolicy problem %q: findings %+v, want one of kind %q", k, got, k)
		}
	}
	// A kind no case names is not reported as another kind's finding.
	got := platformProblemFindings([]routingpolicy.Problem{{Kind: "zz_unknown_problem", Message: "m"}})
	if len(got) != 1 || !got[0].Kind.IsZero() || got[0].Severity != SeverityError {
		t.Errorf("unknown problem kind: findings %+v, want one error with the zero Kind", got)
	}
}
