package gitops

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

// #1988 D1 PR-3 (review P2/P3): every section that may change the conf.d tree
// must end with onTreeRelease, success or error. That holds by construction
// only while each such section takes the lock through lockTree/unlockTree —
// a new `w.mu.Lock()` elsewhere would change the tree and tell no reader. So
// the only direct w.mu.Lock() calls allowed are the two helpers themselves.
func TestTreeLock_OnlyTheHelpersLockDirectly(t *testing.T) {
	allowed := map[string]bool{"lockTree": true, "TryWithTreeLock": true}
	matches, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range matches {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(token.NewFileSet(), name, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				sel, ok := call.Fun.(*ast.SelectorExpr)
				if !ok || (sel.Sel.Name != "Lock" && sel.Sel.Name != "TryLock") {
					return true
				}
				inner, ok := sel.X.(*ast.SelectorExpr)
				if ok && inner.Sel.Name == "mu" && !allowed[fn.Name.Name] {
					t.Errorf("%s: %s takes w.mu directly; use lockTree/unlockTree so readers are told the tree may have changed", name, fn.Name.Name)
				}
				return true
			})
		}
	}
}

// A failed write still releases through the hook: WritePR into a missing
// config dir fails after the feature branch was cut.
func TestTreeLock_ReleaseHookFiresOnFailure(t *testing.T) {
	repo := initRepoOnMain(t)
	w := NewWriter(filepath.Join(repo, "missing"), repo)
	var fired atomic.Int32
	w.SetOnTreeRelease(func() { fired.Add(1) })
	if _, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML); err == nil {
		t.Fatal("expected WritePR to fail")
	}
	if fired.Load() != 1 {
		t.Errorf("onTreeRelease fired %d times after a failed WritePR, want 1", fired.Load())
	}
	_ = os.Remove(filepath.Join(repo, "missing"))
}
