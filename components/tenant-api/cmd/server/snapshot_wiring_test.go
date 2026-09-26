package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"testing"
)

// #1988 D1 PR-3 (review P5a): the tenant list / search snapshot cache is only
// correct when it is the one handler.WireTenantSnapshots tied to THE writer —
// loads under the writer's lock, invalidated on every write section. The
// behaviour is pinned in package handler (TestWireTenantSnapshots_*); this
// pins that main actually serves that cache: Deps.SearchCache must be a
// variable assigned from handler.WireTenantSnapshots(writer). A plain
// NewTenantSnapshotCache() here would compile, pass every handler test and
// serve unmerged PR branches.
func TestMainServesTheWriterWiredSnapshotCache(t *testing.T) {
	f, err := parser.ParseFile(token.NewFileSet(), "main.go", nil, 0)
	if err != nil {
		t.Fatalf("parse main.go: %v", err)
	}
	wired := map[string]bool{} // identifiers assigned from handler.WireTenantSnapshots(writer)
	var searchCache ast.Expr
	ast.Inspect(f, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.AssignStmt:
			if len(n.Lhs) != 1 || len(n.Rhs) != 1 {
				return true
			}
			call, ok := n.Rhs[0].(*ast.CallExpr)
			if !ok || len(call.Args) == 0 {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != "WireTenantSnapshots" {
				return true
			}
			if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "handler" {
				return true
			}
			if arg, ok := call.Args[0].(*ast.Ident); !ok || arg.Name != "writer" {
				return true
			}
			if id, ok := n.Lhs[0].(*ast.Ident); ok {
				wired[id.Name] = true
			}
		case *ast.KeyValueExpr:
			if k, ok := n.Key.(*ast.Ident); ok && k.Name == "SearchCache" {
				searchCache = n.Value
			}
		}
		return true
	})
	if searchCache == nil {
		t.Fatal("main.go sets no Deps.SearchCache")
	}
	id, ok := searchCache.(*ast.Ident)
	if !ok || !wired[id.Name] {
		t.Errorf("Deps.SearchCache is not the result of handler.WireTenantSnapshots(writer)")
	}
}
