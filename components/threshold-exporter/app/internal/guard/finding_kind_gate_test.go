package guard

// finding_kind_gate_test.go — the set of da-guard finding kinds, computed by
// the type checker, and its routing / threshold split (#2904, ADR-036 §9).
//
// tests/shared/test_routing_policy_parity.py is the gate for deleting
// da-guard's routing checks (#2903, #2905): every routing kind must be pinned
// against the route generator first. That gate is only as good as its list of
// kinds, and two earlier ways of producing the list were each beaten:
//
//   - a regex over this package's source (`FindingXxx FindingKind = "xxx"`):
//     `const A, B FindingKind = "a", "b"`, an alias type, a conversion or a
//     literal in `Kind:` were each invisible to it;
//   - an AST test restricting kinds to that one written shape
//     (finding_kind_shape_test.go, deleted here): syntax cannot see types, so
//     a kind written through an alias type, among nine ways found in review,
//     slipped past it.
//
// So the list now comes from go/types, which sees what the compiler sees:
//
//  1. Files are chosen by go/build (build tags honoured, _test.go excluded),
//     the way `go build` chooses them.
//  2. The kind set is every string constant whose type, alias-resolved, is
//     FindingKind, among the expressions the type checker recorded — however
//     it is written — with the files it occurs in.
//  3. Every such value must be the value of a package-level FindingKind
//     const. A literal kind (`Finding{Kind: "x"}`, `f.Kind = "x"`, an untyped
//     const) is red until it is declared, so every kind has a name.
//  4. A non-constant `FindingKind(...)` conversion is red: its value cannot be
//     known before run time, so the set would be unsound. reflect, unsafe and
//     cgo are refused in non-test files for the same reason.
//  5. findingKindPlanes (finding_kind_planes_test.go) must name exactly that
//     set, a kind referred to in routing.go must be a routing kind, and every
//     routing kind needs a reason.
//
// With DA_GUARD_EMIT_KINDS=<path> the test also writes
// {kind: {"plane": ..., "files": [...]}} to <path> as JSON once every check
// passed; the Python gate runs this test that way and reads that file.

import (
	"encoding/json"
	"go/ast"
	"go/build"
	"go/constant"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"testing"
)

// routingFindingsFile builds every routing and domain-policy finding; a kind
// it refers to is a routing kind.
const routingFindingsFile = "routing.go"

// emitKindsEnv names the file the kind table is written to.
const emitKindsEnv = "DA_GUARD_EMIT_KINDS"

// forbiddenImports would let a kind be produced at run time where the type
// checker cannot see it.
var forbiddenImports = map[string]bool{"reflect": true, "unsafe": true, "C": true}

type emittedKind struct {
	Plane kindPlane `json:"plane"`
	Files []string  `json:"files"`
}

func TestFindingKindPlanes(t *testing.T) {
	bp, err := build.Default.ImportDir(".", 0)
	if err != nil {
		t.Fatalf("go/build: %v", err)
	}
	for _, f := range bp.CgoFiles {
		t.Errorf("%s: cgo (import \"C\") in a non-test file", f)
	}
	names := append(append([]string{}, bp.GoFiles...), bp.CgoFiles...)
	if !slices.Contains(names, routingFindingsFile) {
		t.Fatalf("%s is not among the package's files %v", routingFindingsFile, names)
	}

	fset := token.NewFileSet()
	var files []*ast.File
	for _, name := range names {
		f, err := parser.ParseFile(fset, filepath.Join(bp.Dir, name), nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, imp := range f.Imports {
			if path, _ := strconv.Unquote(imp.Path.Value); forbiddenImports[path] {
				t.Errorf("%s: import %q in a non-test file", fset.Position(imp.Pos()), path)
			}
		}
		files = append(files, f)
	}

	info := &types.Info{Types: map[ast.Expr]types.TypeAndValue{}}
	conf := types.Config{Importer: importer.ForCompiler(fset, "source", nil)}
	pkg, err := conf.Check(bp.ImportPath, fset, files, info)
	if err != nil {
		t.Fatalf("type-check: %v", err)
	}
	tn, ok := pkg.Scope().Lookup("FindingKind").(*types.TypeName)
	if !ok {
		t.Fatal("no FindingKind type in the package")
	}
	kindType := tn.Type()
	isKind := func(typ types.Type) bool {
		return typ != nil && types.Identical(types.Unalias(typ), kindType)
	}

	// Package-level FindingKind consts, by value.
	named := map[string]bool{}
	for _, name := range pkg.Scope().Names() {
		if c, ok := pkg.Scope().Lookup(name).(*types.Const); ok && isKind(c.Type()) {
			named[constant.StringVal(c.Val())] = true
		}
	}

	// Every constant FindingKind value the checker recorded, with its files.
	kindFiles := map[string]map[string]bool{}
	for expr, tv := range info.Types {
		if tv.Value == nil || tv.Value.Kind() != constant.String || !isKind(tv.Type) {
			continue
		}
		kind := constant.StringVal(tv.Value)
		pos := fset.Position(expr.Pos())
		if !named[kind] {
			t.Errorf("%s: finding kind %q is no package-level FindingKind const's value; "+
				"declare `FindingXxx FindingKind = %q` and use it", pos, kind, kind)
		}
		if kindFiles[kind] == nil {
			kindFiles[kind] = map[string]bool{}
		}
		kindFiles[kind][filepath.Base(pos.Filename)] = true
	}
	if len(kindFiles) == 0 {
		t.Fatal("no finding kind found; the test is not looking at the guard package")
	}

	// A FindingKind made at run time: its value is unknowable here.
	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok || !info.Types[call.Fun].IsType() {
				return true
			}
			if tv := info.Types[call]; isKind(tv.Type) && tv.Value == nil {
				t.Errorf("%s: non-constant conversion to FindingKind; use a FindingXxx const",
					fset.Position(call.Pos()))
			}
			return true
		})
	}

	// The registry against the computed set.
	for kind, files := range kindFiles {
		plane, ok := findingKindPlanes[FindingKind(kind)]
		switch {
		case !ok:
			t.Errorf("finding kind %q (%s) is in no plane: add it to findingKindPlanes in "+
				"finding_kind_planes_test.go — routing if it judges `_routing*` keys, routing "+
				"profiles, domain policies, receivers / overrides / routes or which tenants the "+
				"route generator renders (then pin it in tests/shared/routing_policy_parity_matrix.json), "+
				"threshold if it judges threshold values, defaults files or cardinality",
				kind, slices.Sorted(maps.Keys(files)))
		case files[routingFindingsFile] && plane != planeRouting:
			t.Errorf("finding kind %q is referred to in %s but registered as %q, not %q",
				kind, routingFindingsFile, plane, planeRouting)
		}
	}
	for kind, plane := range findingKindPlanes {
		if kindFiles[string(kind)] == nil {
			t.Errorf("findingKindPlanes names %q, which no non-test source of the package has", kind)
		}
		if plane != planeRouting && plane != planeThreshold {
			t.Errorf("findingKindPlanes[%q] = %q, neither %q nor %q", kind, plane, planeRouting, planeThreshold)
		}
		if plane == planeRouting && findingKindPlaneReasons[kind] == "" {
			t.Errorf("routing kind %q has no reason in findingKindPlaneReasons", kind)
		}
	}
	for kind := range findingKindPlaneReasons {
		if _, ok := findingKindPlanes[kind]; !ok {
			t.Errorf("findingKindPlaneReasons names %q, which findingKindPlanes does not", kind)
		}
	}

	out := os.Getenv(emitKindsEnv)
	if out == "" || t.Failed() {
		return
	}
	table := map[string]emittedKind{}
	for kind, files := range kindFiles {
		table[kind] = emittedKind{Plane: findingKindPlanes[FindingKind(kind)], Files: slices.Sorted(maps.Keys(files))}
	}
	data, err := json.MarshalIndent(table, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(out, append(data, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
}
