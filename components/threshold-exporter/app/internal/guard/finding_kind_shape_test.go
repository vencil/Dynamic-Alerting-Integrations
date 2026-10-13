package guard

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestFindingKindsAreNamedTypedConsts holds the one shape every finding kind
// is written in: `FindingXxx FindingKind = "xxx"`. The routing/threshold
// classification gate (tests/shared/test_routing_policy_parity.py,
// go_finding_kinds) finds kinds by reading that shape out of this package's
// source; a kind written any other way — a conversion `FindingKind("x")`, a
// bare literal or an untyped const in `Kind:` — would be invisible to it, and
// a routing check could then be deleted from Go with no generator twin and
// nothing going red (#2904, ADR-036 §9). So this test refuses those shapes
// instead of the gate trying to recognise them.
func TestFindingKindsAreNamedTypedConsts(t *testing.T) {
	fset := token.NewFileSet()
	paths, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, path := range paths {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		src, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		file, err := parser.ParseFile(fset, path, src, 0)
		if err != nil {
			t.Fatal(err)
		}
		checked++
		ast.Inspect(file, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.CallExpr:
				if id, ok := n.Fun.(*ast.Ident); ok && id.Name == "FindingKind" {
					t.Errorf("%s: FindingKind(...) conversion; declare a `FindingXxx FindingKind = \"...\"` const instead", fset.Position(n.Pos()))
				}
			case *ast.ValueSpec:
				if n.Type == nil {
					// An untyped `FindingXxx = "xxx"` passes the Kind check
					// below by name but is not the shape the gate reads.
					for i, name := range n.Names {
						if i < len(n.Values) && strings.HasPrefix(name.Name, "Finding") {
							if lit, ok := n.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
								t.Errorf("%s: untyped string const %s; declare it as FindingKind", fset.Position(name.Pos()), name.Name)
							}
						}
					}
					return true
				}
				if id, ok := n.Type.(*ast.Ident); !ok || id.Name != "FindingKind" {
					return true
				}
				for _, name := range n.Names {
					if !strings.HasPrefix(name.Name, "Finding") {
						t.Errorf("%s: FindingKind const %s must be named Finding<Something>", fset.Position(name.Pos()), name.Name)
					}
				}
			case *ast.CompositeLit:
				if id, ok := n.Type.(*ast.Ident); !ok || id.Name != "Finding" {
					return true
				}
				for _, elt := range n.Elts {
					kv, ok := elt.(*ast.KeyValueExpr)
					if !ok {
						t.Errorf("%s: positional Finding literal; name the fields so Kind is visible", fset.Position(elt.Pos()))
						continue
					}
					if key, ok := kv.Key.(*ast.Ident); !ok || key.Name != "Kind" {
						continue
					}
					if _, ok := kv.Value.(*ast.BasicLit); ok {
						t.Errorf("%s: Kind set from a literal; use a `FindingXxx FindingKind` const", fset.Position(kv.Value.Pos()))
					}
					if id, ok := kv.Value.(*ast.Ident); ok && !strings.HasPrefix(id.Name, "Finding") {
						t.Errorf("%s: Kind set from %s, not a Finding<Something> const", fset.Position(kv.Value.Pos()), id.Name)
					}
				}
			}
			return true
		})
	}
	if checked == 0 {
		t.Fatal("no non-test .go files found; the test is not looking at the guard package")
	}
}
