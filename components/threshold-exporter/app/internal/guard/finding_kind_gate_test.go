package guard

// finding_kind_gate_test.go — every registered da-guard finding kind is one
// the guard package reports (#2904).
//
// The set of kinds is internal/guard/findingkind's: a closed type whose only
// constructor registers the kind with its reason (see that package's
// comment). First, finding_kind_isolation_test.go over the whole module:
// findingkind is imported by name, so every use of a kind is a
// `findingkind.FindingXxx` selector this test sees, and no kind var is
// written to. Then, over every non-test .go file of this package whatever
// its name or build constraint (go/build's default context would skip an
// `_arm64.go` file): every registered kind is used by some file, else it is
// stale.
//
// Until #2905 this test also split the kinds into a routing and a threshold
// plane and wrote that table out for tests/shared/test_routing_policy_parity.py,
// the gate for deleting da-guard's routing checks. Those checks and their
// kinds are deleted (ADR-036 §7 step 3), so the split and the table went
// with them.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

const findingKindImport = "github.com/vencil/threshold-exporter/internal/guard/findingkind"

// nonTestFiles parses every non-test .go file in dir, ignoring build
// constraints.
func nonTestFiles(t *testing.T, dir string) (*token.FileSet, map[string]*ast.File) {
	t.Helper()
	names, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	out := map[string]*ast.File{}
	for _, path := range names {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		out[filepath.Base(path)] = f
	}
	if len(out) == 0 {
		t.Fatalf("no non-test .go file in %s", dir)
	}
	return fset, out
}

// kindVars maps each findingkind var to the kind it registers, read from
// `var FindingXxx = register("name", ...)` (findingkind's own tests hold
// every Kind var to that form) and held equal to All().
func kindVars(t *testing.T) map[string]findingkind.Kind {
	t.Helper()
	_, files := nonTestFiles(t, "findingkind")
	byName := map[string]findingkind.Kind{}
	for _, e := range findingkind.All() {
		byName[e.Kind.String()] = e.Kind
	}
	out := map[string]findingkind.Kind{}
	for _, f := range files {
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.VAR {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				if len(vs.Names) != 1 || len(vs.Values) != 1 {
					continue
				}
				call, ok := vs.Values[0].(*ast.CallExpr)
				if !ok || len(call.Args) == 0 {
					continue
				}
				if id, ok := call.Fun.(*ast.Ident); !ok || id.Name != "register" {
					continue
				}
				lit, ok := call.Args[0].(*ast.BasicLit)
				if !ok {
					t.Fatalf("findingkind var %s: register's name is not a literal", vs.Names[0].Name)
				}
				name, _ := strconv.Unquote(lit.Value)
				k, ok := byName[name]
				if !ok {
					t.Fatalf("findingkind var %s registers %q, which All() lacks", vs.Names[0].Name, name)
				}
				out[vs.Names[0].Name] = k
			}
		}
	}
	if len(out) != len(byName) {
		t.Fatalf("findingkind: %d vars registered in the source, %d kinds in All()", len(out), len(byName))
	}
	return out
}

func TestFindingKindsAreReported(t *testing.T) {
	requireKindIsolation(t) // finding_kind_isolation_test.go: rules a–e, module-wide
	vars := kindVars(t)
	_, files := nonTestFiles(t, ".")

	kindFiles := map[findingkind.Kind]map[string]bool{}
	for name, f := range files {
		local := map[string]bool{} // names findingkind is imported under
		for _, imp := range f.Imports {
			if path, _ := strconv.Unquote(imp.Path.Value); path == findingKindImport {
				if imp.Name == nil {
					local["findingkind"] = true
				} else {
					local[imp.Name.Name] = true
				}
			}
		}
		if len(local) == 0 {
			continue
		}
		kindOf := func(e ast.Expr) (findingkind.Kind, bool) {
			sel, ok := ast.Unparen(e).(*ast.SelectorExpr)
			if !ok {
				return findingkind.Kind{}, false
			}
			if x, ok := sel.X.(*ast.Ident); !ok || !local[x.Name] {
				return findingkind.Kind{}, false
			}
			k, ok := vars[sel.Sel.Name]
			return k, ok
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if sel, ok := n.(*ast.SelectorExpr); ok {
				if k, ok := kindOf(sel); ok {
					if kindFiles[k] == nil {
						kindFiles[k] = map[string]bool{}
					}
					kindFiles[k][name] = true
				}
			}
			return true
		})
	}

	for _, e := range findingkind.All() {
		if len(kindFiles[e.Kind]) == 0 {
			t.Errorf("finding kind %q is registered but no non-test source of the guard package uses it; "+
				"remove it from findingkind or report it", e.Kind)
		}
	}
}
