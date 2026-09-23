package main

// go/ast pin for the end state of the conf.d enumeration family (#1911): in
// the exporter module's production code, the only function that references a
// stdlib DIRECT directory-listing entry point is walkDirTree in
// pkg/config/tree_scan.go (behind config.ScanDirTree, the conf.d walker), and
// it does so exactly once. walkFilesDir in cmd/da-batchpr/main.go is the one
// explicit exclusion — it walks a PR payload directory, not conf.d (#1911
// owner ruling ①(a)).
//
// The population is derived, not listed: every non-_test.go Go file the
// module holds (the directories `./...` would load — see
// moduleProductionGoFiles), parsed with go/parser. A file that does not parse
// is a failure naming it, never a silent "no sites". Build constraints are
// ignored on purpose: a file behind a tag is still code somebody ships, so
// over-inclusion is the chosen direction.
//
// A site is a reference to a directory-listing entry point
// (dirEnumerationSites), keyed by (file, enclosing function). The key is the
// function, not the file: a second lister added next to the walker, or a
// conf.d read added elsewhere in an excluded file, must not ride on the
// allowance. The assertion is an exact multiset equality in both directions
// against {the walker} ∪ {exclusions}, counts included:
//   - a new site (new function, or one more in an allowed function) is red —
//     route it through config.ScanDirTree, or add the function to
//     confdWalkerExclusions with the reason it is not a conf.d reader;
//   - an allowed function whose sites are gone is red too, so a stale entry
//     cannot keep a hole open for whatever lands there next.
//
// ⛔ Anti-vacuity: the walker itself is IN the expected multiset, so exact
// equality already fails if the detector finds nothing at all — a detector
// broken into silence cannot pass. The classification and the
// enclosing-function resolution have their own counterexamples in
// TestDirEnumerationDetectorClassifiesEntryPoints.
//
// ⚠️ NOT GUARDED — directory listing this pin cannot see:
//   - transitive stdlib listers: functions that list a directory on the
//     caller's behalf, e.g. net/http.Dir / http.FileServer,
//     text/template and html/template ParseGlob, go/parser.ParseDir,
//     go/build.ImportDir, os.RemoveAll, os.CopyFS (with a non-DirFS source),
//     syscall.ReadDirent. Only the DIRECT entry points below are matched;
//     chasing the transitive closure is deliberately not attempted.
//   - subprocess-based listing (exec.Command "ls", "find", "git …"). The
//     module has one today, internal/batchpr/git_shell.go
//     (`git status --porcelain` in collectRebaseConflicts), which walks a PR
//     work tree, not the exporter's conf.d.
//   - reflection that looks a method up by string (reflect.Value.MethodByName
//     ("Readdirnames")…): no selector names the entry point in the source.
//   - cgo and raw syscalls.
//   - third-party dependencies (enumeration inside a module this one imports).
//   - a conf.d reader that opens files by a name it already knows without
//     listing the directory (not enumeration; out of #1911's scope).
//   - deleting this file.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// packageScope is the enclosing-function marker for a site outside any
// FuncDecl (a package-level var initializer, for instance).
const packageScope = "<package scope>"

// confdWalkerSites is the one production function allowed to reference a
// directory-listing entry point because it IS the conf.d walker, keyed
// "file · function", with its exact site count.
var confdWalkerSites = map[string]int{
	"pkg/config/tree_scan.go · walkDirTree": 1,
}

// confdWalkerExclusions are production functions that list a directory that
// is not conf.d, keyed like confdWalkerSites, with their exact site count and
// the reason. An entry whose function stops listing turns the pin red (stale
// exclusion); so does one more site in it.
var confdWalkerExclusions = map[string]struct {
	sites  int
	reason string
}{
	"cmd/da-batchpr/main.go · walkFilesDir": {1, "walks the PR payload directory, not conf.d — #1911 ruling ①(a)"},
}

// dirEnumeratingFuncs are the stdlib package-level entry points that DIRECTLY
// list the entries of a directory, keyed by import path. ⛔ This enumeration
// is legitimate only because the stdlib is the authority on which of its
// functions are directory-listing primitives — it is not a list of callers,
// and it does not grow when the repo does. It is NOT the set of stdlib
// functions that list a directory somewhere underneath (see NOT GUARDED in
// the file header). os.DirFS does not read by itself; it is included because
// an fs.FS over a directory exists here only to be listed or walked through
// the io/fs entries.
var dirEnumeratingFuncs = map[string]map[string]bool{
	"path/filepath": {"Walk": true, "WalkDir": true, "Glob": true},
	"io/fs":         {"WalkDir": true, "ReadDir": true, "Glob": true},
	"os":            {"ReadDir": true, "DirFS": true},
	"io/ioutil":     {"ReadDir": true}, // deprecated since Go 1.16, still compiles
}

// dirEnumeratingMethods are method names that list a directory on
// *os.File (Readdir, Readdirnames, ReadDir) and on fs.ReadDirFS / fs.ReadDirFile
// (ReadDir). Without type-checking the receiver is unknown, so ANY selector
// with one of these names matches — possibly over-inclusive (a user type's
// unrelated ReadDir method would be flagged). Over-inclusion turns red and
// gets reviewed, which is the safe direction.
var dirEnumeratingMethods = map[string]bool{
	"ReadDir":      true,
	"Readdir":      true,
	"Readdirnames": true,
}

// dirSite is one reference to a directory-listing entry point.
type dirSite struct {
	File string // module-relative
	Func string // enclosing FuncDecl ("Recv.Name" for methods) or packageScope
	Line int
	Expr string
}

func (s dirSite) key() string { return s.File + " · " + s.Func }

func (s dirSite) String() string {
	return fmt.Sprintf("%s:%d: %s (in %s)", s.File, s.Line, s.Expr, s.Func)
}

func TestConfdWalkerHoldsTheOnlyDirectDirListingSite(t *testing.T) {
	t.Parallel()

	root := exporterModuleRoot(t)
	files, err := moduleProductionGoFiles(root)
	if err != nil {
		t.Fatalf("listing the module's Go files: %v", err)
	}

	got := map[string][]dirSite{} // "file · function" -> sites
	for _, rel := range files {
		src, rerr := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
		if rerr != nil {
			t.Errorf("%s cannot be read, so whether it lists a directory is unknown: %v", rel, rerr)
			continue
		}
		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, rel, src, 0) // rel as the name: sites print module-relative
		if perr != nil {
			t.Errorf("%s does not parse, so whether it lists a directory is unknown: %v", rel, perr)
			continue
		}
		sites, derr := dirEnumerationSites(fset, file)
		if derr != nil {
			t.Errorf("%s: %v", rel, derr)
			continue
		}
		for _, s := range sites {
			got[s.key()] = append(got[s.key()], s)
		}
	}
	t.Logf("parsed %d production Go files; sites: %v", len(files), got)

	want := map[string]int{}
	for k, n := range confdWalkerSites {
		want[k] = n
	}
	for k, ex := range confdWalkerExclusions {
		if _, clash := want[k]; clash {
			t.Fatalf("%s is the walker; it cannot also be an exclusion", k)
		}
		want[k] = ex.sites
	}

	var unexpected, missing []string
	for k, sites := range got {
		allowed, ok := want[k]
		switch {
		case !ok:
			for _, s := range sites {
				unexpected = append(unexpected, s.String())
			}
		case len(sites) > allowed:
			unexpected = append(unexpected, fmt.Sprintf("%s: %d sites, %d allowed:\n      %s",
				k, len(sites), allowed, joinSites(sites)))
		}
	}
	for k, allowed := range want {
		n := len(got[k])
		if n >= allowed {
			continue
		}
		what := "the conf.d walker — either it moved or the detector went blind"
		if ex, ok := confdWalkerExclusions[k]; ok {
			what = "stale exclusion (\"" + ex.reason + "\"); update or delete it"
		}
		missing = append(missing, fmt.Sprintf("%s: %d sites, want %d — %s", k, n, allowed, what))
	}
	sort.Strings(unexpected)
	sort.Strings(missing)
	if len(unexpected) > 0 {
		t.Errorf("production code references a stdlib directory-listing entry point outside the "+
			"allowed functions; route it through config.ScanDirTree, or add the function to "+
			"confdWalkerExclusions with the reason it is not a conf.d reader (#1911):\n    %s",
			strings.Join(unexpected, "\n    "))
	}
	if len(missing) > 0 {
		t.Errorf("expected directory-listing site not found:\n    %s", strings.Join(missing, "\n    "))
	}
}

func joinSites(sites []dirSite) string {
	out := make([]string, len(sites))
	for i, s := range sites {
		out[i] = s.String()
	}
	return strings.Join(out, "\n      ")
}

// moduleProductionGoFiles returns the module-relative, slash-separated paths
// of every non-_test.go Go file in the packages `./...` loads from root:
// testdata, `.`/`_`-prefixed directories, vendor subdirectories and nested
// modules are skipped (same conventions as scanDeniedImports).
func moduleProductionGoFiles(root string) ([]string, error) {
	var out []string
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path == root {
				return nil
			}
			name := d.Name()
			if name == "testdata" || strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") ||
				filepath.Base(filepath.Dir(path)) == "vendor" {
				return filepath.SkipDir
			}
			if _, serr := os.Stat(filepath.Join(path, "go.mod")); serr == nil {
				return filepath.SkipDir // a nested module is not part of ./...
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		rel, rerr := filepath.Rel(root, path)
		if rerr != nil {
			return rerr
		}
		out = append(out, filepath.ToSlash(rel))
		return nil
	})
	sort.Strings(out)
	return out, err
}

// dirEnumerationSites returns every reference in file to a directory-listing
// entry point, each attributed to its enclosing top-level function (a
// closure counts toward the FuncDecl it is written in). References are
// matched, not only calls: passing os.ReadDir as a value lists a directory
// just as calling it does. Package identifiers are resolved through the
// file's imports, so aliases count; a dot-import of a watched package is an
// error because its members would be referenced unqualified and this
// detector does not resolve those. A local variable shadowing an import name
// is not modelled — it can only add a false match, never hide one.
func dirEnumerationSites(fset *token.FileSet, file *ast.File) ([]dirSite, error) {
	pkgByName := map[string]string{} // local name -> import path, watched packages only
	for _, spec := range file.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			return nil, err
		}
		if _, watched := dirEnumeratingFuncs[path]; !watched {
			continue
		}
		name := path[strings.LastIndex(path, "/")+1:]
		if spec.Name != nil {
			name = spec.Name.Name
		}
		switch name {
		case ".":
			return nil, fmt.Errorf("%s: dot-import of %q is unsupported by the directory-listing "+
				"detector (its members would be unqualified); import it by name",
				fset.Position(spec.Pos()), path)
		case "_":
			continue
		}
		pkgByName[name] = path
	}

	var sites []dirSite
	for _, decl := range file.Decls {
		scope := packageScope
		if fd, ok := decl.(*ast.FuncDecl); ok {
			scope = funcDeclName(fd)
		}
		ast.Inspect(decl, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			hit := dirEnumeratingMethods[sel.Sel.Name]
			if id, ok := sel.X.(*ast.Ident); ok && !hit {
				if path, ok := pkgByName[id.Name]; ok && dirEnumeratingFuncs[path][sel.Sel.Name] {
					hit = true
				}
			}
			if hit {
				pos := fset.Position(sel.Pos())
				sites = append(sites, dirSite{File: pos.Filename, Func: scope, Line: pos.Line, Expr: exprString(sel)})
			}
			return true
		})
	}
	return sites, nil
}

// funcDeclName is "Name" for a function and "Recv.Name" for a method, with
// the receiver's pointer and type parameters stripped.
func funcDeclName(fd *ast.FuncDecl) string {
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return fd.Name.Name
	}
	typ := fd.Recv.List[0].Type
	for {
		switch x := typ.(type) {
		case *ast.StarExpr:
			typ = x.X
			continue
		case *ast.IndexExpr:
			typ = x.X
			continue
		case *ast.IndexListExpr:
			typ = x.X
			continue
		case *ast.ParenExpr:
			typ = x.X
			continue
		}
		break
	}
	if id, ok := typ.(*ast.Ident); ok {
		return id.Name + "." + fd.Name.Name
	}
	return "?." + fd.Name.Name
}

func exprString(sel *ast.SelectorExpr) string {
	if id, ok := sel.X.(*ast.Ident); ok {
		return id.Name + "." + sel.Sel.Name
	}
	return "<expr>." + sel.Sel.Name
}

// The detector against synthetic sources, both directions: a detector that
// reports nothing would pass the pin's "unexpected" half just as well as a
// correct one.
func TestDirEnumerationDetectorClassifiesEntryPoints(t *testing.T) {
	t.Parallel()

	parse := func(t *testing.T, name, body string) ([]dirSite, error) {
		t.Helper()
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, "synthetic.go", "package p\n"+body+"\n", 0)
		if err != nil {
			t.Fatalf("%s: synthetic source does not parse: %v", name, err)
		}
		return dirEnumerationSites(fset, file)
	}

	cases := []struct {
		name string
		src  string // file body after the package clause
		want int    // sites
	}{
		{"filepath.Walk", `import "path/filepath"; var _ = filepath.Walk`, 1},
		{"filepath.WalkDir", `import "path/filepath"; var _ = filepath.WalkDir`, 1},
		{"filepath.Glob", `import "path/filepath"; func f() { filepath.Glob("*") }`, 1},
		{"fs.WalkDir", `import "io/fs"; var _ = fs.WalkDir`, 1},
		{"fs.ReadDir", `import "io/fs"; var _ = fs.ReadDir`, 1},
		{"fs.Glob", `import "io/fs"; var _ = fs.Glob`, 1},
		{"os.ReadDir", `import "os"; func f() { os.ReadDir(".") }`, 1},
		{"os.DirFS", `import "os"; var _ = os.DirFS(".")`, 1},
		{"ioutil.ReadDir", `import "io/ioutil"; var _ = ioutil.ReadDir`, 1},
		{"aliased import", `import fp "path/filepath"; var _ = fp.WalkDir`, 1},
		{"aliased os", `import xos "os"; var _ = xos.ReadDir`, 1},
		{"method Readdirnames", `func f(d interface{ Readdirnames(int) ([]string, error) }) { d.Readdirnames(-1) }`, 1},
		{"method Readdir on call result", `import "os"; func f() { g, _ := os.Open("."); g.Readdir(0) }`, 1},
		{"method ReadDir on any receiver", `func f(x T) { x.ReadDir("a") }`, 1},
		{"two sites", `import ("os"; "path/filepath"); var _, _ = os.ReadDir, filepath.Glob`, 2},

		{"near miss: filepath.Join", `import "path/filepath"; var _ = filepath.Join("a")`, 0},
		{"near miss: os.ReadFile", `import "os"; var _ = os.ReadFile`, 0},
		{"near miss: os.Open", `import "os"; var _ = os.Open`, 0},
		{"near miss: fs.SkipDir / fs.DirEntry", `import "io/fs"; var _ = fs.SkipDir; var _ fs.DirEntry`, 0},
		{"near miss: local func Walk", `func Walk() {}; var _ = Walk`, 0},
		{"near miss: unwatched package's Walk", `import "go/ast"; var _ = ast.Walk`, 0},
		{"near miss: unimported alias name", `var _ = filepath.WalkDir`, 0},
		{"near miss: blank import", `import _ "os"`, 0},
	}
	for _, tc := range cases {
		got, err := parse(t, tc.name, tc.src)
		if err != nil {
			t.Errorf("%s: %v", tc.name, err)
			continue
		}
		if len(got) != tc.want {
			t.Errorf("%s: %d sites, want %d: %v", tc.name, len(got), tc.want, got)
		}
	}

	// Enclosing-function resolution: the pin keys on it, so a wrong
	// attribution would let a second lister ride on an allowed function.
	scopeCases := []struct {
		name string
		src  string
		want []string // Func of each site, in source order
	}{
		{"plain func", `import "os"; func list() { os.ReadDir(".") }`, []string{"list"}},
		{"pointer-receiver method", `import "os"; type T struct{}; func (t *T) scan() { os.ReadDir(".") }`, []string{"T.scan"}},
		{"value-receiver generic method", `import "os"; type G[K any] struct{}; func (G[K]) scan() { os.ReadDir(".") }`, []string{"G.scan"}},
		{"closure counts toward its FuncDecl", `import "os"; func outer() { f := func() { os.ReadDir(".") }; f() }`, []string{"outer"}},
		{"package-level var", `import "os"; var lister = os.ReadDir`, []string{packageScope}},
		{"two functions, two keys", `import "os"; func a() { os.ReadDir(".") }; func b() { os.ReadDir(".") }`, []string{"a", "b"}},
	}
	for _, tc := range scopeCases {
		got, err := parse(t, tc.name, tc.src)
		if err != nil {
			t.Errorf("%s: %v", tc.name, err)
			continue
		}
		var funcs []string
		for _, s := range got {
			funcs = append(funcs, s.Func)
		}
		if strings.Join(funcs, ",") != strings.Join(tc.want, ",") {
			t.Errorf("%s: enclosing functions %q, want %q", tc.name, funcs, tc.want)
		}
	}

	// A dot-import of a watched package must be a loud error, not a zero.
	if _, err := parse(t, "dot-import", "import . \"os\"\nvar _ = ReadDir"); err == nil ||
		!strings.Contains(err.Error(), "dot-import") {
		t.Errorf("dot-import of os: err = %v, want an unsupported dot-import error", err)
	}
}
