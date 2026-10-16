package testutil

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

// RoutingPolicyProblemKinds returns every `Problem*` string constant
// declared in pkg/routingpolicy's non-test sources: name → value. It reads
// the source, so a test that ranges over it covers a new Problem kind
// without being edited (internal/guard's severity check, da-guard's
// zero-tenant check, #2991).
func RoutingPolicyProblemKinds(t testing.TB) map[string]string {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	dir := filepath.Join(filepath.Dir(thisFile), "..", "..", "pkg", "routingpolicy")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read %s: %v", dir, err)
	}
	fset := token.NewFileSet()
	var files []*ast.File
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, e.Name()), nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", e.Name(), err)
		}
		files = append(files, f)
	}
	kinds := map[string]string{}
	for _, f := range files {
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, name := range vs.Names {
					if !strings.HasPrefix(name.Name, "Problem") {
						continue
					}
					// Fail rather than skip: a kind declared any other way
					// (iota, an implicit repeat, "a"+"b", an alias of another
					// constant) would otherwise drop out of the callers' checks unseen.
					if i >= len(vs.Values) {
						t.Fatalf("%s has no value of its own (iota or an implicit repeat?): "+
							"declare every Problem kind as a string literal so this helper reads it", name.Name)
					}
					lit, ok := vs.Values[i].(*ast.BasicLit)
					if !ok || lit.Kind != token.STRING {
						t.Fatalf("%s is not a plain string literal: declare every Problem kind as one "+
							"so this helper reads it", name.Name)
					}
					v, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatalf("%s: %v", name.Name, err)
					}
					kinds[name.Name] = v
				}
			}
		}
	}
	return kinds
}
