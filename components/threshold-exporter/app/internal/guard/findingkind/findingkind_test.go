package findingkind

// What the closed type does not hold by itself (see the package comment):
// register is the one constructor, the registrations are whole, and no file of
// the package is left out of some build.

import (
	"encoding/json"
	"go/ast"
	"go/build"
	"go/build/constraint"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// packageFiles parses every .go file in this directory — all of them,
// whatever their name or build constraint, which is the point.
func packageFiles(t *testing.T, withTests bool) (*token.FileSet, map[string]*ast.File) {
	t.Helper()
	names, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	out := map[string]*ast.File{}
	for _, name := range names {
		if !withTests && strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, name, nil, parser.ParseComments)
		if err != nil {
			t.Fatal(err)
		}
		out[name] = f
	}
	if len(out) == 0 {
		t.Fatal("no .go file found; the test is not looking at the package")
	}
	return fset, out
}

func mustPanic(t *testing.T, what string, f func()) {
	t.Helper()
	defer func() {
		if recover() == nil {
			t.Errorf("%s: register did not panic", what)
		}
	}()
	f()
}

func TestRegisterRefusesBadRegistrations(t *testing.T) {
	before := len(registry)
	mustPanic(t, "duplicate name", func() { register(FindingMissingRequired.String(), PlaneThreshold, "again") })
	mustPanic(t, "empty name", func() { register("", PlaneThreshold, "r") })
	mustPanic(t, "unknown plane", func() { register("zz_probe_plane", Plane("policy"), "r") })
	mustPanic(t, "empty plane", func() { register("zz_probe_plane", "", "r") })
	mustPanic(t, "no reason", func() { register("zz_probe_reason", PlaneRouting, "") })
	if len(registry) != before {
		t.Errorf("a refused registration was kept: %d kinds, had %d", len(registry), before)
	}
}

func TestAllIsWhole(t *testing.T) {
	all := All()
	if len(all) == 0 {
		t.Fatal("no kind registered")
	}
	if !sort.SliceIsSorted(all, func(i, j int) bool { return all[i].Kind.String() < all[j].Kind.String() }) {
		t.Error("All() is not sorted by name")
	}
	planes := map[Plane]int{}
	for _, e := range all {
		k := e.Kind
		switch {
		case k.IsZero():
			t.Errorf("All() holds the zero Kind: %+v", e)
		case e.Plane != PlaneRouting && e.Plane != PlaneThreshold:
			t.Errorf("%s: plane %q", k, e.Plane)
		case e.Reason == "":
			t.Errorf("%s: no reason", k)
		case k.Plane() != e.Plane:
			t.Errorf("%s: Plane() %q, registered %q", k, k.Plane(), e.Plane)
		}
		planes[e.Plane]++
	}
	if planes[PlaneRouting] == 0 || planes[PlaneThreshold] == 0 {
		t.Errorf("a plane is empty: %v", planes)
	}
	var zero Kind
	if !zero.IsZero() || zero.String() != "" || zero.Plane() != "" {
		t.Errorf("zero Kind: IsZero %v, String %q, Plane %q", zero.IsZero(), zero.String(), zero.Plane())
	}
}

// The bytes are the ones the string type produced; the zero Kind is refused.
func TestMarshalJSON(t *testing.T) {
	for _, e := range All() {
		got, err := json.Marshal(struct {
			K Kind `json:"kind"`
		}{e.Kind})
		if err != nil {
			t.Fatal(err)
		}
		want, _ := json.Marshal(struct {
			K string `json:"kind"`
		}{e.Kind.String()})
		if string(got) != string(want) {
			t.Errorf("%s: %s, want %s", e.Kind, got, want)
		}
	}
	if _, err := json.Marshal(struct{ K Kind }{}); err == nil {
		t.Error("the zero Kind marshalled without error")
	}
}

// TestNoBuildConstraints: every file of the package is in every build. A
// file left out on some GOOS / GOARCH (a `_arm64.go` name, a `//go:build`
// line) could hold a registration — or a constructor — that a test run on
// another platform never compiles (round 4 of #2904: da-tools ships arm64).
func TestNoBuildConstraints(t *testing.T) {
	_, files := packageFiles(t, true)
	// Two contexts with no GOOS or GOARCH in common: a file name suffix
	// matches at most one of them.
	contexts := []build.Context{build.Default, build.Default}
	contexts[0].GOOS, contexts[0].GOARCH = "linux", "amd64"
	contexts[1].GOOS, contexts[1].GOARCH = "windows", "arm64"
	for name, f := range files {
		for _, cg := range f.Comments {
			if cg.Pos() > f.Package {
				break
			}
			for _, c := range cg.List {
				if constraint.IsGoBuild(c.Text) || constraint.IsPlusBuild(c.Text) {
					t.Errorf("%s: build constraint %q", name, c.Text)
				}
			}
		}
		for _, ctx := range contexts {
			ctx.CgoEnabled = false
			if ok, err := ctx.MatchFile(".", name); err != nil || !ok {
				t.Errorf("%s: not built for %s/%s (err %v); this package takes no file-name constraint",
					name, ctx.GOOS, ctx.GOARCH, err)
			}
		}
		for _, imp := range f.Imports {
			if path, _ := strconv.Unquote(imp.Path.Value); path == "unsafe" || path == "C" {
				t.Errorf("%s: import %q", name, path)
			}
		}
	}
}

// TestOnlyRegisterMakesAKind: a Kind composite literal appears in register
// alone, so register's checks see every Kind there is.
func TestOnlyRegisterMakesAKind(t *testing.T) {
	fset, files := packageFiles(t, false)
	inRegister := 0
	for _, f := range files {
		for _, decl := range f.Decls {
			fd, isFunc := decl.(*ast.FuncDecl)
			allowed := isFunc && fd.Recv == nil && fd.Name.Name == "register"
			ast.Inspect(decl, func(n ast.Node) bool {
				lit, ok := n.(*ast.CompositeLit)
				if !ok {
					return true
				}
				if id, ok := lit.Type.(*ast.Ident); ok && id.Name == "Kind" {
					if !allowed {
						t.Errorf("%s: Kind literal outside register", fset.Position(lit.Pos()))
					} else {
						inRegister++
					}
				}
				return true
			})
		}
	}
	if inRegister != 1 {
		t.Errorf("register holds %d Kind literals, want 1", inRegister)
	}
}

// staticRegistrations reads `var X = register("name", PlaneY, ...)` from the
// package's non-test files: {X: name}, {name: plane}. Any other call of
// register, or any other package-level var initialised with a Kind, is an
// error, so the names a reader finds in the source are the registry.
func staticRegistrations(t *testing.T) (byVar map[string]string, planes map[string]Plane) {
	fset, files := packageFiles(t, false)
	byVar, planes = map[string]string{}, map[string]Plane{}
	seen := map[*ast.CallExpr]bool{}
	for _, f := range files {
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.VAR {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				if len(vs.Values) == 0 {
					continue
				}
				call := registerCall(vs.Values[0])
				if call == nil {
					continue
				}
				pos := fset.Position(vs.Pos())
				if len(vs.Names) != 1 || len(vs.Values) != 1 || vs.Type != nil || len(call.Args) != 3 {
					t.Errorf("%s: write `var FindingXxx = register(\"name\", PlaneY, \"reason\")`", pos)
					continue
				}
				name, err := strconv.Unquote(literal(call.Args[0]))
				plane := map[string]Plane{"PlaneRouting": PlaneRouting, "PlaneThreshold": PlaneThreshold}[identName(call.Args[1])]
				if err != nil || plane == "" {
					t.Errorf("%s: register's name must be a string literal and its plane PlaneRouting or PlaneThreshold", pos)
					continue
				}
				seen[call] = true
				byVar[vs.Names[0].Name] = name
				planes[name] = plane
			}
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if e, ok := n.(ast.Expr); ok {
				if call := registerCall(e); call != nil && !seen[call] {
					t.Errorf("%s: register called outside a package-level `var X = register(...)`",
						fset.Position(call.Pos()))
				}
			}
			return true
		})
	}
	return byVar, planes
}

// registerCall is e when e is a call of register, else nil.
func registerCall(e ast.Expr) *ast.CallExpr {
	call, ok := e.(*ast.CallExpr)
	if !ok {
		return nil
	}
	if id, ok := call.Fun.(*ast.Ident); ok && id.Name == "register" {
		return call
	}
	return nil
}

func literal(e ast.Expr) string {
	if bl, ok := e.(*ast.BasicLit); ok && bl.Kind == token.STRING {
		return bl.Value
	}
	return ""
}

func identName(e ast.Expr) string {
	if id, ok := e.(*ast.Ident); ok {
		return id.Name
	}
	return ""
}

// TestRegistrationsAreTheSource: what the source says is what All() holds.
func TestRegistrationsAreTheSource(t *testing.T) {
	byVar, planes := staticRegistrations(t)
	all := All()
	if len(all) != len(planes) || len(byVar) != len(planes) {
		t.Errorf("All() has %d kinds, the source registers %d names through %d vars",
			len(all), len(planes), len(byVar))
	}
	for _, e := range all {
		if planes[e.Kind.String()] != e.Plane {
			t.Errorf("%s: All() says plane %q, the source %q", e.Kind, e.Plane, planes[e.Kind.String()])
		}
	}
	for v := range byVar {
		if !strings.HasPrefix(v, "Finding") || !ast.IsExported(v) {
			t.Errorf("var %s: a kind's var is an exported FindingXxx", v)
		}
	}
}

// The directory holds no other kind of source (assembly, cgo helpers).
func TestOnlyGoFiles(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".go" {
			t.Errorf("%s: this package holds .go files only", e.Name())
		}
	}
}
