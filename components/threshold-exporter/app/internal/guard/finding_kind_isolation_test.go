package guard

// finding_kind_isolation_test.go — a kind var keeps the kind it was
// registered as, in every package of the module (#2904).
//
// The kinds are package-level vars, so `findingkind.FindingX =
// findingkind.FindingY` compiles in any package, and afterwards the name
// FindingX reports another kind; so does writing through
// `&findingkind.FindingX` or a pointer-receiver method. That is a slip the
// type cannot stop, so checkKindIsolation parses every .go file under the
// module root with go/parser (no go/build: no build constraint or
// `_GOOS_GOARCH` name hides a file) and reports:
//
//   - findingkind dot- or blank-imported (every use must be a selector);
//   - a findingkind package-level var as the target of an assignment,
//     ++/--, a range clause or &: outside findingkind matched by import path
//     (any alias, the package imported twice), inside it by the parser's
//     object resolution (a local of the same name is fine);
//   - a pointer-receiver method in findingkind.
//
// Test files are held to them too: a test that reassigns a kind changes what
// every other test of that run observes. Forging a kind on purpose is a
// non-goal (findingkind's package comment).

import (
	"bufio"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// moduleRoot is the threshold-exporter module, relative to this package.
const moduleRoot = "../.."

// findingKindRel is findingkind's directory relative to the module root
// (and, joined to the module path, its import path).
const findingKindRel = "internal/guard/findingkind"

type parsedFile struct {
	rel  string // slash-separated, relative to the module root
	file *ast.File
}

// checkKindIsolation checks the module at root and returns one line per
// violation, sorted. An error means the walk itself failed.
func checkKindIsolation(root string) ([]string, error) {
	modPath, err := modulePath(filepath.Join(root, "go.mod"))
	if err != nil {
		return nil, err
	}
	fkImport := modPath + "/" + findingKindRel

	fset := token.NewFileSet()
	var files []parsedFile
	var out []string
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || filepath.Ext(path) != ".go" {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		// Object resolution on: the write check inside findingkind needs it.
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return fmt.Errorf("parse %s: %w", rel, err)
		}
		files = append(files, parsedFile{rel: rel, file: f})
		return nil
	})
	if err != nil {
		return nil, err
	}

	// findingkind's package-level vars, and the specs that declare them.
	pkgVars := map[string]bool{}
	pkgSpecs := map[*ast.ValueSpec]bool{}
	inFindingKind := func(pf parsedFile) bool {
		return filepath.ToSlash(filepath.Dir(pf.rel)) == findingKindRel && pf.file.Name.Name == "findingkind"
	}
	for _, pf := range files {
		if !inFindingKind(pf) {
			continue
		}
		for _, decl := range pf.file.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.VAR {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				pkgSpecs[vs] = true
				for _, n := range vs.Names {
					pkgVars[n.Name] = true
				}
			}
		}
	}
	if len(pkgVars) == 0 {
		return nil, fmt.Errorf("no package-level var found in %s/%s; the check is not looking at findingkind", root, findingKindRel)
	}

	report := func(pos token.Pos, format string, args ...any) {
		p := fset.Position(pos)
		rel, err := filepath.Rel(root, p.Filename)
		if err != nil {
			rel = p.Filename
		}
		out = append(out, fmt.Sprintf("%s:%d: %s", filepath.ToSlash(rel), p.Line, fmt.Sprintf(format, args...)))
	}

	for _, pf := range files {
		f := pf.file
		local := map[string]bool{} // names findingkind is imported under in this file
		for _, imp := range f.Imports {
			if path, _ := strconv.Unquote(imp.Path.Value); path != fkImport {
				continue
			}
			name := "findingkind"
			if imp.Name != nil {
				name = imp.Name.Name
			}
			if name == "." || name == "_" {
				report(imp.Pos(), "findingkind imported as %q; import it by name so each kind is a selector", name)
				continue
			}
			local[name] = true
		}

		own := inFindingKind(pf)
		// isKindVar: e names a findingkind package-level var.
		isKindVar := func(e ast.Expr) (string, bool) {
			switch e := ast.Unparen(e).(type) {
			case *ast.SelectorExpr:
				if x, ok := e.X.(*ast.Ident); ok && local[x.Name] && pkgVars[e.Sel.Name] {
					return x.Name + "." + e.Sel.Name, true
				}
			case *ast.Ident:
				if !own || !pkgVars[e.Name] {
					return "", false
				}
				if e.Obj == nil { // declared in another file of the package
					return e.Name, true
				}
				if vs, ok := e.Obj.Decl.(*ast.ValueSpec); ok && pkgSpecs[vs] {
					return e.Name, true
				}
			}
			return "", false
		}
		written := func(e ast.Expr, how string) {
			if name, ok := isKindVar(e); ok {
				report(e.Pos(), "%s %s: a findingkind var must keep the kind it was registered as", how, name)
			}
		}
		ast.Inspect(f, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.AssignStmt:
				for _, lhs := range n.Lhs {
					written(lhs, "assignment to")
				}
			case *ast.IncDecStmt:
				written(n.X, "++/-- on")
			case *ast.RangeStmt:
				if n.Key != nil {
					written(n.Key, "range assigns to")
				}
				if n.Value != nil {
					written(n.Value, "range assigns to")
				}
			case *ast.UnaryExpr:
				if n.Op == token.AND {
					written(n.X, "address taken of")
				}
			case *ast.FuncDecl:
				if own && n.Recv != nil && len(n.Recv.List) == 1 {
					if _, ptr := ast.Unparen(n.Recv.List[0].Type).(*ast.StarExpr); ptr {
						report(n.Pos(), "pointer-receiver method %s in findingkind: calling it on a kind var takes the var's address", n.Name.Name)
					}
				}
			}
			return true
		})
	}
	sort.Strings(out)
	return out, nil
}

// modulePath reads the `module` line of a go.mod.
func modulePath(goMod string) (string, error) {
	fh, err := os.Open(goMod)
	if err != nil {
		return "", err
	}
	defer fh.Close()
	sc := bufio.NewScanner(fh)
	for sc.Scan() {
		if rest, ok := strings.CutPrefix(strings.TrimSpace(sc.Text()), "module "); ok {
			if p, err := strconv.Unquote(strings.TrimSpace(rest)); err == nil {
				return p, nil
			}
			return strings.TrimSpace(rest), nil
		}
	}
	if err := sc.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("%s: no module line", goMod)
}

// requireKindIsolation runs the check over this module; TestFindingKindsAreReported
// calls it first, so its source scan sees every use of a kind as a selector.
func requireKindIsolation(t *testing.T) {
	t.Helper()
	start := time.Now()
	violations, err := checkKindIsolation(moduleRoot)
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range violations {
		t.Error(v)
	}
	t.Logf("module-wide findingkind isolation check: %s", time.Since(start))
}

// --- the check against synthetic modules ---------------------------------

const syntheticFindingKind = `package findingkind

type Kind struct{ s string }

func register(name string) Kind { return Kind{s: name} }

func Unregistered() Kind { return Kind{s: "unregistered_finding_kind"} }

func (k Kind) String() string { return k.s }

var registry = map[string]Kind{}

var (
	FindingA = register("a")
	FindingB = register("b")
)
`

// syntheticModule writes a module example.com/m holding a findingkind
// package and one clean consumer, plus extra (relative path -> source).
func syntheticModule(t *testing.T, extra map[string]string) string {
	t.Helper()
	root := t.TempDir()
	files := map[string]string{
		"go.mod":                       "module example.com/m\n\ngo 1.24\n",
		findingKindRel + "/kinds.go":   syntheticFindingKind,
		"internal/guard/consumer.go":   "package guard\n\nimport \"example.com/m/" + findingKindRel + "\"\n\nvar k = findingkind.FindingA\n\nfunc use() string { x := findingkind.FindingB; return x.String() + k.String() }\n",
		"internal/guard/guard_test.go": "package guard\n\nimport fk \"example.com/m/" + findingKindRel + "\"\n\nvar _ = fk.FindingA\n",
	}
	for k, v := range extra {
		files[k] = v
	}
	for rel, src := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(src), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func runIsolation(t *testing.T, extra map[string]string) []string {
	t.Helper()
	v, err := checkKindIsolation(syntheticModule(t, extra))
	if err != nil {
		t.Fatal(err)
	}
	return v
}

const fkImportLine = `"example.com/m/` + findingKindRel + `"`

// The synthetic module with nothing added is clean: a positive control, so
// each case below is red for what it adds. It also pins what is allowed:
// locals and parameters named like a kind var inside findingkind, writes into
// the registry map, and the register-initialised vars themselves.
func TestFindingKindIsolation_SyntheticCleanModule(t *testing.T) {
	t.Parallel()
	if v := runIsolation(t, map[string]string{
		findingKindRel + "/shadow.go": "package findingkind\n\nfunc f(FindingB Kind) Kind {\n\tFindingA := FindingB\n\tFindingA = Kind{}\n\tp := &FindingA\n\t_ = p\n\tfor FindingA = range []Kind{} {\n\t}\n\tregistry[\"x\"] = FindingB\n\treturn FindingA\n}\n",
	}); len(v) != 0 {
		t.Errorf("clean module reported:\n%s", strings.Join(v, "\n"))
	}
}

func TestFindingKindIsolation_RulesFire(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		files map[string]string
		want  string // substring of the one expected violation
	}{
		{
			// A file no default build compiles is walked too.
			name: "write/assignment in an arm64-only file",
			files: map[string]string{"pkg/r/r_arm64.go": "//go:build arm64\n\npackage r\n\nimport " + fkImportLine +
				"\n\nfunc init() { findingkind.FindingA = findingkind.FindingB }\n"},
			want: "pkg/r/r_arm64.go:7: assignment to findingkind.FindingA",
		},
		{
			name:  "import/dot import",
			files: map[string]string{"pkg/d/d.go": "package d\n\nimport . " + fkImportLine + "\n\nvar _ = FindingA\n"},
			want:  "pkg/d/d.go:3: findingkind imported as \".\"",
		},
		{
			name:  "import/blank import",
			files: map[string]string{"pkg/d/d.go": "package d\n\nimport _ " + fkImportLine + "\n"},
			want:  "pkg/d/d.go:3: findingkind imported as \"_\"",
		},
		{
			name: "write/assignment from another package",
			files: map[string]string{"pkg/r/r.go": "package r\n\nimport " + fkImportLine +
				"\n\nfunc init() { findingkind.FindingA = findingkind.FindingB }\n"},
			want: "pkg/r/r.go:5: assignment to findingkind.FindingA",
		},
		{
			name: "write/assignment through an alias, parenthesised",
			files: map[string]string{"pkg/r/r.go": "package r\n\nimport kinds " + fkImportLine +
				"\n\nfunc init() { (kinds.FindingA) = kinds.FindingB }\n"},
			want: "pkg/r/r.go:5: assignment to kinds.FindingA",
		},
		{
			name: "write/address taken through the second of two imports",
			files: map[string]string{"pkg/r/r.go": "package r\n\nimport (\n\tone " + fkImportLine + "\n\ttwo " + fkImportLine +
				"\n)\n\nvar _ = one.FindingA\n\nvar P = &two.FindingB\n"},
			want: "pkg/r/r.go:10: address taken of two.FindingB",
		},
		{
			name: "write/increment",
			files: map[string]string{"pkg/r/r.go": "package r\n\nimport " + fkImportLine +
				"\n\nfunc init() { findingkind.FindingA++ }\n"},
			want: "pkg/r/r.go:5: ++/-- on findingkind.FindingA",
		},
		{
			name: "write/range target",
			files: map[string]string{"pkg/r/r.go": "package r\n\nimport " + fkImportLine +
				"\n\nfunc init() {\n\tfor _, findingkind.FindingA = range []findingkind.Kind{findingkind.FindingB} {\n\t}\n}\n"},
			want: "pkg/r/r.go:6: range assigns to findingkind.FindingA",
		},
		{
			name: "write/assignment in a test file",
			files: map[string]string{"pkg/r/r_test.go": "package r\n\nimport " + fkImportLine +
				"\n\nfunc init() { findingkind.FindingA = findingkind.FindingB }\n"},
			want: "pkg/r/r_test.go:5: assignment to findingkind.FindingA",
		},
		{
			name:  "write/assignment inside findingkind",
			files: map[string]string{findingKindRel + "/swap.go": "package findingkind\n\nfunc init() { FindingA = FindingB }\n"},
			want:  findingKindRel + "/swap.go:3: assignment to FindingA",
		},
		{
			name:  "write/address taken inside findingkind, same file as the var",
			files: map[string]string{findingKindRel + "/kinds.go": syntheticFindingKind + "\nvar p = &FindingB\n"},
			want:  findingKindRel + "/kinds.go:18: address taken of FindingB",
		},
		{
			name:  "pointer receiver/pointer-receiver method",
			files: map[string]string{findingKindRel + "/set.go": "package findingkind\n\nfunc (k *Kind) Set(s string) { k.s = s }\n"},
			want:  findingKindRel + "/set.go:3: pointer-receiver method Set",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			v := runIsolation(t, tc.files)
			if len(v) != 1 || !strings.Contains(v[0], tc.want) {
				t.Errorf("violations:\n%s\nwant exactly one containing %q", strings.Join(v, "\n"), tc.want)
			}
		})
	}
}
