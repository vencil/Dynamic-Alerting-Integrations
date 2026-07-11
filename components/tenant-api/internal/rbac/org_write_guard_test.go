package rbac

// Write-plane org-scope guard (ADR-027 / LD-6 P4b).
//
// AllowedInOrg (via handler.OrgAllowed) is the ONLY legitimate per-tenant
// write/admin authorization entry point after P4b: a handler that calls the
// org-blind `Allowed` for a write decision compiles, behaves identically
// today (shadow mode), and silently DROPS the org-scope axis the moment
// --rbac-org-scope-enforce flips — the exact silent-failure shape the
// principal guard pins for hand-built principals.
//
// This test parses (go/ast — parse, not grep) every non-test .go file in the
// module outside internal/rbac (the package that legitimately implements the
// check) and inspects every `Allowed` call:
//
//   - AUTO-EXEMPT: a call whose tenant argument (2nd arg) is the basic
//     literal "*" — the platform-scope gate. Org-scope deliberately does not
//     apply to platform scope (invariant I6: an org-scoped rule is not a
//     platform admin), so these sites stay on the org-blind Allowed by
//     design and need no allowlist entry.
//   - Everything else must hit the annotated allowlist below (read-plane
//     filtering only — org visibility is ScopeAllowed's job on the list
//     plane; read-by-id lands in P4c).
//
// A stale allowlist entry (no longer matching any call site) FAILS the test:
// an exemption must stay demonstrably necessary (#1067 discipline), or a
// converted call site would leave a hole a future write-plane caller could
// silently crawl through by reusing the entry's file+function.
//
// Same go/ast-over-forbidigo rationale as principal_guard_test.go: the guard
// must anchor on the CALL with a specific argument shape (tenant arg = "*"),
// which a selector-pattern linter cannot express.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// orgWriteAllowlist enumerates the sanctioned org-blind `Allowed` call sites
// outside internal/rbac (beyond the "*"-literal auto-exemption). Key is
// "<module-relative file path>::<enclosing function>"; value is why the site
// may stay org-blind. P4b end state: exactly the read-plane filters.
var orgWriteAllowlist = map[string]string{
	"internal/handler/authz.go::filterByRBAC": "read-plane generic filter (PermRead callers: task results, PR lists, " +
		"group members) — org visibility on the list plane is ScopeAllowed's job; read-by-id hardening lands in P4c",
	"internal/handler/group.go::hasAccessibleMember": "read-plane group visibility test (ListGroups skip) — " +
		"same P4c read-plane deferral as filterByRBAC",
	"internal/handler/pr.go::ListPRs": "read-plane PR list filter for ?tenant= — returns an empty list (never 403) " +
		"to avoid the tenant-existence oracle; read-plane org filtering lands in P4c",
}

func TestWritePlaneAllowedCallsConfinedToAllowlist(t *testing.T) {
	t.Parallel()

	root := moduleRootForGuard(t)
	rbacDir := filepath.Join(root, "internal", "rbac")

	var violations []string
	hits := make(map[string]bool, len(orgWriteAllowlist))

	walkErr := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			name := d.Name()
			if name == "vendor" || name == "node_modules" || strings.HasPrefix(name, ".") {
				return filepath.SkipDir
			}
			// internal/rbac is the package that IMPLEMENTS the permission
			// check (middleware, evaluation core) — package-level exemption.
			if filepath.Clean(path) == filepath.Clean(rbacDir) {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}

		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return relErr
		}
		rel = filepath.ToSlash(rel)

		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			return perr
		}

		// Walk per top-level declaration so every call can be attributed to
		// its enclosing function (closures attribute to the FuncDecl that
		// lexically contains them — e.g. the handler constructor ListPRs).
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				// Match on the method/function NAME regardless of receiver or
				// package qualifier (aliased import / dot-import cannot dodge).
				var callee string
				switch f := call.Fun.(type) {
				case *ast.SelectorExpr:
					callee = f.Sel.Name
				case *ast.Ident:
					callee = f.Name
				default:
					return true
				}
				if callee != "Allowed" {
					return true
				}
				// Auto-exemption: platform-scope gate — tenant arg (2nd of
				// Allowed(p, tenantID, want)) is the basic literal "*".
				if len(call.Args) >= 2 {
					if lit, ok := call.Args[1].(*ast.BasicLit); ok &&
						lit.Kind == token.STRING && lit.Value == `"*"` {
						return true
					}
				}
				entry := rel + "::" + fn.Name.Name
				if _, ok := orgWriteAllowlist[entry]; ok {
					hits[entry] = true
					return true
				}
				violations = append(violations,
					fmt.Sprintf("%s (entry %q)", fset.Position(call.Pos()), entry))
				return true
			})
		}
		return nil
	})
	if walkErr != nil {
		t.Fatalf("walking module source tree: %v", walkErr)
	}

	if len(violations) > 0 {
		t.Errorf("org-blind rbac Allowed call in production code outside the sanctioned sites "+
			"(ADR-027 / LD-6 P4b — the org-scope axis is invisible to Allowed, so a write gate here "+
			"silently ignores org restrictions once --rbac-org-scope-enforce flips):\n  %s\n"+
			"Fix: per-tenant WRITE/ADMIN decisions must use handler.OrgAllowed / rbac.AllowedInOrg; "+
			"platform-level gates must pass the literal \"*\" as the tenant argument; a genuine "+
			"read-plane filter may be added to orgWriteAllowlist with a reason.",
			strings.Join(violations, "\n  "))
	}

	var stale []string
	for entry := range orgWriteAllowlist {
		if !hits[entry] {
			stale = append(stale, entry)
		}
	}
	sort.Strings(stale)
	if len(stale) > 0 {
		t.Errorf("stale orgWriteAllowlist entries (no matching call site — an exemption must stay "+
			"demonstrably necessary; remove the entry or fix its file::function key):\n  %s",
			strings.Join(stale, "\n  "))
	}
}
