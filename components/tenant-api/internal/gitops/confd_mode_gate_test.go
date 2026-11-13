package gitops

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #3535: every conf.d file tenant-api writes is cfg.ConfDFileMode — the one
// mode da-guard writes conf.d in too (pkg/config). A conf.d write goes
// through writeTenant or writeFileAtomic; each call's mode must be that
// constant, never a literal (a literal drifts from the rule unseen). The
// publish marker is not a conf.d file (it sits beside the batch worktrees).
var nonConfDWriters = map[string]string{
	"writePublishMarker": "the publish marker, beside the batch worktrees — not a conf.d file",
}

func TestConfDWritesUseConfDFileMode(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	var bad []string
	calls := 0
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		file, err := parser.ParseFile(fset, name, src, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			if _, skip := nonConfDWriters[fn.Name.Name]; skip {
				continue
			}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || len(call.Args) != 3 {
					return true
				}
				var callee string
				switch f := call.Fun.(type) {
				case *ast.Ident:
					callee = f.Name
				case *ast.SelectorExpr:
					callee = f.Sel.Name
				}
				if callee != "writeFileAtomic" && callee != "writeTenant" && callee != "writeTenantFile" {
					return true
				}
				calls++
				if sel, ok := call.Args[2].(*ast.SelectorExpr); ok && sel.Sel.Name == "ConfDFileMode" {
					return true
				}
				if id, ok := call.Args[2].(*ast.Ident); ok && id.Name == "perm" {
					return true // the writers' own pass-through of their parameter
				}
				bad = append(bad, fset.Position(call.Pos()).String()+" in "+fn.Name.Name)
				return true
			})
		}
	}
	if calls == 0 {
		t.Fatal("no conf.d write call found: the gate reads nothing (renamed writers?)")
	}
	if len(bad) > 0 {
		t.Fatalf("conf.d writes not in cfg.ConfDFileMode (#3535): %v", bad)
	}
}
