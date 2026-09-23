package main

// go/ast pin for the end state of the conf.d enumeration family (#1911): the
// exporter module's production code lists directory entries in exactly one
// place, pkg/config/tree_scan.go (config.ScanDirTree, the conf.d walker).
// cmd/da-batchpr/main.go is the one explicit exclusion — it walks a PR
// payload directory, not conf.d (#1911 owner ruling ①(a)).
//
// The population is derived, not listed: every non-_test.go Go file the
// module holds (the directories `./...` would load — see
// moduleProductionGoFiles), parsed with go/parser. A file that does not parse
// is a failure naming it, never a silent "not an enumerator". Build
// constraints are ignored on purpose: a file behind a tag is still code
// somebody ships, so over-inclusion is the chosen direction.
//
// A file is a directory enumerator when it references a directory-listing
// entry point (dirEnumerationSites). The assertion is exact set equality in
// both directions against {the walker} ∪ {exclusions}:
//   - a new enumerator is red — route it through config.ScanDirTree, or add
//     it to confdWalkerExclusions with the reason it is not a conf.d reader;
//   - an exclusion (or the walker) that no longer enumerates is red too, so a
//     stale entry cannot keep a hole open for whatever lands there next.
//
// ⛔ Anti-vacuity: the walker itself is IN the expected set, so exact equality
// already fails if the detector finds nothing at all — a detector broken into
// silence cannot pass. The classification step has its own counterexamples
// in TestDirEnumerationDetectorClassifiesEntryPoints.
//
// ⚠️ NOT GUARDED: deleting this file; enumeration through a third-party
// package outside the stdlib (the module has no such dependency today); and
// a conf.d reader that opens files by a name it already knows without
// listing the directory (that is not enumeration, and is out of #1911's
// scope).

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

// confdWalkerFile is the one production file allowed to enumerate a directory
// because it IS the conf.d walker.
const confdWalkerFile = "pkg/config/tree_scan.go"

// confdWalkerExclusions are production files that enumerate a directory that
// is not conf.d. Each value is the reason; an entry whose file stops
// enumerating turns the pin red (stale exclusion).
var confdWalkerExclusions = map[string]string{
	"cmd/da-batchpr/main.go": "walks the PR payload directory (walkFilesDir), not conf.d — #1911 ruling ①(a)",
}

// dirEnumeratingFuncs are the stdlib package-level entry points that list the
// entries of a directory, keyed by import path. ⛔ This enumeration is
// legitimate only because the stdlib is the authority on which of its
// functions read a directory — it is not a list of callers, and it does not
// grow when the repo does. os.DirFS does not read by itself; it is included
// because the only thing an fs.FS over a directory is for here is listing or
// walking it through the io/fs entries below.
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

func TestConfdWalkerIsTheOnlyProductionDirEnumerator(t *testing.T) {
	t.Parallel()

	root := exporterModuleRoot(t)
	files, err := moduleProductionGoFiles(root)
	if err != nil {
		t.Fatalf("listing the module's Go files: %v", err)
	}

	got := map[string][]string{} // module-relative file -> call sites
	for _, rel := range files {
		src, rerr := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
		if rerr != nil {
			t.Errorf("%s cannot be read, so whether it enumerates a directory is unknown: %v", rel, rerr)
			continue
		}
		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, rel, src, 0) // rel as the name: sites print module-relative
		if perr != nil {
			t.Errorf("%s does not parse, so whether it enumerates a directory is unknown: %v", rel, perr)
			continue
		}
		sites, derr := dirEnumerationSites(fset, file)
		if derr != nil {
			t.Errorf("%s: %v", rel, derr)
			continue
		}
		if len(sites) > 0 {
			got[rel] = sites
		}
	}

	t.Logf("parsed %d production Go files; enumerators: %v", len(files), got)

	if _, clash := confdWalkerExclusions[confdWalkerFile]; clash {
		t.Fatalf("%s is the walker; it cannot also be an exclusion", confdWalkerFile)
	}
	want := map[string]bool{confdWalkerFile: true}
	for rel := range confdWalkerExclusions {
		want[rel] = true
	}

	var unexpected, missing []string
	for rel, sites := range got {
		if !want[rel] {
			unexpected = append(unexpected, strings.Join(sites, "\n    "))
		}
	}
	for rel := range want {
		if _, ok := got[rel]; ok {
			continue
		}
		if rel == confdWalkerFile {
			missing = append(missing, rel+" (the conf.d walker) — either it moved or the detector went blind")
		} else {
			missing = append(missing, rel+" — stale exclusion (\""+confdWalkerExclusions[rel]+"\"); delete it")
		}
	}
	sort.Strings(unexpected)
	sort.Strings(missing)
	if len(unexpected) > 0 {
		t.Errorf("production code enumerates a directory outside %s; route it through "+
			"config.ScanDirTree, or add it to confdWalkerExclusions with the reason it is "+
			"not a conf.d reader (#1911):\n    %s", confdWalkerFile, strings.Join(unexpected, "\n    "))
	}
	if len(missing) > 0 {
		t.Errorf("expected directory enumerator not found:\n    %s", strings.Join(missing, "\n    "))
	}
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

// dirEnumerationSites returns "file:line: expr" for every reference in file
// to a directory-listing entry point. References are matched, not only calls:
// passing os.ReadDir as a value lists a directory just as calling it does.
// Package identifiers are resolved through the file's imports, so aliases
// count; a dot-import of a watched package is an error because its members
// would be referenced unqualified and this detector does not resolve those.
// A local variable shadowing an import name is not modelled — it can only
// add a false match, never hide one.
func dirEnumerationSites(fset *token.FileSet, file *ast.File) ([]string, error) {
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
			return nil, fmt.Errorf("%s: dot-import of %q is unsupported by the directory-enumeration "+
				"detector (its members would be unqualified); import it by name",
				fset.Position(spec.Pos()), path)
		case "_":
			continue
		}
		pkgByName[name] = path
	}

	var sites []string
	ast.Inspect(file, func(n ast.Node) bool {
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
			sites = append(sites, fmt.Sprintf("%s:%d: %s", pos.Filename, pos.Line, exprString(sel)))
		}
		return true
	})
	return sites, nil
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

	cases := []struct {
		name string
		src  string // file body after the package clause
		want int    // enumeration sites
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
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, "synthetic.go", "package p\n"+tc.src+"\n", 0)
		if err != nil {
			t.Fatalf("%s: synthetic source does not parse: %v", tc.name, err)
		}
		got, err := dirEnumerationSites(fset, file)
		if err != nil {
			t.Errorf("%s: %v", tc.name, err)
			continue
		}
		if len(got) != tc.want {
			t.Errorf("%s: %d sites, want %d: %v", tc.name, len(got), tc.want, got)
		}
	}

	// A dot-import of a watched package must be a loud error, not a zero.
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "dot.go", "package p\nimport . \"os\"\nvar _ = ReadDir\n", 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := dirEnumerationSites(fset, file); err == nil || !strings.Contains(err.Error(), "dot-import") {
		t.Errorf("dot-import of os: err = %v, want an unsupported dot-import error", err)
	}
}
