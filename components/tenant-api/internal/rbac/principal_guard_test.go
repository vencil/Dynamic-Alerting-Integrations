package rbac

// Source-fidelity guard for the identity seam (ADR-027 / LD-6 P3).
//
// The largest silent-failure surface of the principal refactor is a handler
// hand-building a principal from parts — e.g.
// &rbac.VerifiedPrincipal{Groups: rbac.RequestGroups(r)} — which compiles,
// behaves identically today, and silently DROPS the verified claims the
// moment they start participating in authorization. Production code must
// obtain the principal only from a resolver (HeaderResolver.Resolve) or the
// request context (rbac.RequestPrincipal); it must never construct one.
//
// This test parses (go/ast — parse, not grep) every non-test .go file in the
// module outside this package and fails on any VerifiedPrincipal composite
// literal. The rbac package itself (the resolvers that legitimately construct
// principals) and *_test.go files (fixtures) are exempt.
//
// Why go/ast instead of golangci forbidigo (the hybrid-lint-policy default):
// verified empirically on golangci-lint 2.12.2 — forbidigo matches
// identifiers/selector expressions wherever they appear, so a pattern on
// `rbac\.VerifiedPrincipal` also flags the TYPE usage every converted
// signature needs (`p *rbac.VerifiedPrincipal` — false positives on the
// exact API this refactor introduces), and it cannot anchor on the
// composite-literal brace (its matched text is the selector, never `{`), so
// `rbac\.VerifiedPrincipal\{` matches nothing at all. The AST walk below
// flags exactly the construction site and nothing else.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVerifiedPrincipalConstructionConfinedToRBACAndTests(t *testing.T) {
	t.Parallel()

	root := moduleRootForGuard(t)
	rbacDir := filepath.Join(root, "internal", "rbac")

	var violations []string
	walkErr := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			name := d.Name()
			// Skip dependency/artifact trees and this package (the one
			// sanctioned construction site).
			if name == "vendor" || name == "node_modules" || strings.HasPrefix(name, ".") {
				return filepath.SkipDir
			}
			if filepath.Clean(path) == filepath.Clean(rbacDir) {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}

		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			return perr
		}
		ast.Inspect(file, func(n ast.Node) bool {
			lit, ok := n.(*ast.CompositeLit)
			if !ok {
				return true
			}
			// Match on the type name regardless of package qualifier so an
			// aliased import (x "…/rbac"; x.VerifiedPrincipal{…}) or a
			// dot-import cannot dodge the guard.
			switch typ := lit.Type.(type) {
			case *ast.SelectorExpr:
				if typ.Sel.Name == "VerifiedPrincipal" {
					violations = append(violations, fset.Position(lit.Pos()).String())
				}
			case *ast.Ident:
				if typ.Name == "VerifiedPrincipal" {
					violations = append(violations, fset.Position(lit.Pos()).String())
				}
			}
			return true
		})
		return nil
	})
	if walkErr != nil {
		t.Fatalf("walking module source tree: %v", walkErr)
	}

	if len(violations) > 0 {
		t.Errorf("VerifiedPrincipal composite literal in production code outside package rbac "+
			"(hand-built principals silently drop verified claims — use rbac.RequestPrincipal(r), "+
			"or a resolver; test fixtures belong in _test.go files):\n  %s",
			strings.Join(violations, "\n  "))
	}
}

// moduleRootForGuard walks up from the package directory (the test working
// directory) to the nearest go.mod — the tenant-api module root.
func moduleRootForGuard(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found above the rbac package directory")
		}
		dir = parent
	}
}
