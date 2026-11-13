package gitops

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// #3535: every conf.d file tenant-api writes is cfg.ConfDFileMode — the one
// mode da-guard writes conf.d in too (pkg/config). This gate reads the
// package's source:
//
//   - a call of a conf.d writer (writeFileAtomic, writeTenant,
//     writeTenantFile) passes cfg.ConfDFileMode as its mode; only the
//     writers themselves pass their own `perm` parameter through;
//   - a writer is only ever called, never taken as a value (a function value
//     would carry a mode the gate cannot see);
//   - a direct file write (os.WriteFile, os.OpenFile, os.Chmod, os.Create)
//     sits only in a function listed below, with why it is not a conf.d
//     write — so a new one is a visible edit here, not a quiet literal.
//
// Writes git makes (checkout, restore, reset) take the process umask; that
// is a documented limit (#3535), not this gate's.
var confDWriters = map[string]bool{"writeFileAtomic": true, "writeTenant": true, "writeTenantFile": true}

// notConfDModeHomes: functions that call a conf.d writer for a file that is
// not in conf.d, with why.
var notConfDModeHomes = map[string]string{
	"writePublishMarker": "the publish marker, beside the batch worktrees — not a conf.d file",
}

var directWriteHomes = map[string]string{
	"writeFileAtomic": "the conf.d writer itself: chmods the temporary file to the mode it is given",
	"DiffTexts":       "a scratch diff directory under the OS temp dir, not conf.d",
	"mirrorDir":       "the temporary staging worktree, not conf.d (serving files are re-synced with cfg.ConfDFileMode)",
}

var directWriteCalls = map[string]bool{"WriteFile": true, "OpenFile": true, "Chmod": true, "Create": true}

func TestConfDWritesUseConfDFileMode(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	var bad []string
	calls := 0
	seenHomes := map[string]bool{}
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
			where := func(n ast.Node) string { return fset.Position(n.Pos()).String() + " in " + fn.Name.Name }
			callees := map[ast.Expr]bool{}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				callees[call.Fun] = true
				name := ""
				switch f := call.Fun.(type) {
				case *ast.Ident:
					name = f.Name
				case *ast.SelectorExpr:
					name = f.Sel.Name
					if pkg, ok := f.X.(*ast.Ident); ok && pkg.Name == "os" && directWriteCalls[name] {
						if _, ok := directWriteHomes[fn.Name.Name]; !ok {
							bad = append(bad, where(call)+": os."+name+" outside the listed homes")
						}
						seenHomes[fn.Name.Name] = true
						return true
					}
				}
				if !confDWriters[name] || len(call.Args) != 3 {
					return true
				}
				calls++
				mode := call.Args[2]
				if sel, ok := mode.(*ast.SelectorExpr); ok && sel.Sel.Name == "ConfDFileMode" {
					if pkg, ok := sel.X.(*ast.Ident); ok && pkg.Name == "cfg" {
						return true
					}
				}
				if id, ok := mode.(*ast.Ident); ok && id.Name == "perm" && confDWriters[fn.Name.Name] {
					return true // a writer passing its own parameter on
				}
				if _, ok := notConfDModeHomes[fn.Name.Name]; ok {
					return true
				}
				bad = append(bad, where(call)+": mode is not cfg.ConfDFileMode")
				return true
			})
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				var id string
				switch e := n.(type) {
				case *ast.Ident:
					id = e.Name
				case *ast.SelectorExpr:
					if callees[e] {
						return false // a called w.writeTenant: its Sel is the callee, not a value
					}
					id = e.Sel.Name
				default:
					return true
				}
				if expr, ok := n.(ast.Expr); ok && callees[expr] {
					return true
				}
				if confDWriters[id] && !confDWriters[fn.Name.Name] { // a writer may test its own seam
					bad = append(bad, where(n)+": "+id+" taken as a value")
				}
				return true
			})
		}
	}
	if calls == 0 {
		t.Fatal("no conf.d write call found: the gate reads nothing (renamed writers?)")
	}
	var stale []string
	for home := range directWriteHomes {
		if !seenHomes[home] {
			stale = append(stale, home)
		}
	}
	sort.Strings(stale)
	if len(stale) > 0 {
		t.Errorf("directWriteHomes lists functions with no direct write any more: %v", stale)
	}
	if len(bad) > 0 {
		t.Fatalf("conf.d writes outside cfg.ConfDFileMode (#3535): %v", bad)
	}
}
