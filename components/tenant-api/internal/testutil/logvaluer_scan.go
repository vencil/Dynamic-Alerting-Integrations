package testutil

import (
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// ErrorTypesWithoutLogValuer type-checks the non-test Go files of the
// package in dir (go/types, its imports from source) and returns, sorted,
// the name of every type declared there — exported or not — whose method
// set (the type's or its pointer's, promoted methods included) implements
// error but not log/slog.LogValuer, less the names in exempt. It also
// returns every error type it saw, for a caller's positive control.
//
// #1560 ⑤-b: an auxiliary guard only — what keeps the generator's text out
// of tenant-api's log is the behaviour matrix; this scan makes a new error
// type decide, at review time, whether it carries such text.
func ErrorTypesWithoutLogValuer(t testing.TB, dir string, exempt map[string]string) (missing, all []string) {
	t.Helper()
	fset := token.NewFileSet()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var files []*ast.File
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, f)
	}
	conf := types.Config{Importer: importer.ForCompiler(fset, "source", nil)}
	pkg, err := conf.Check(filepath.Base(dir), fset, files, nil)
	if err != nil {
		t.Fatalf("type-check %s: %v", dir, err)
	}
	slogPkg, err := conf.Importer.Import("log/slog")
	if err != nil {
		t.Fatal(err)
	}
	logValuer := slogPkg.Scope().Lookup("LogValuer").Type().Underlying().(*types.Interface)
	errorIface := types.Universe.Lookup("error").Type().Underlying().(*types.Interface)
	implements := func(typ types.Type, iface *types.Interface) bool {
		return types.Implements(typ, iface) || types.Implements(types.NewPointer(typ), iface)
	}
	for _, name := range pkg.Scope().Names() {
		tn, ok := pkg.Scope().Lookup(name).(*types.TypeName)
		if !ok || tn.IsAlias() {
			continue
		}
		if _, isIface := tn.Type().Underlying().(*types.Interface); isIface {
			continue
		}
		if !implements(tn.Type(), errorIface) {
			continue
		}
		all = append(all, name)
		if _, ok := exempt[name]; ok {
			continue
		}
		if !implements(tn.Type(), logValuer) {
			missing = append(missing, name)
		}
	}
	sort.Strings(missing)
	sort.Strings(all)
	return missing, all
}
