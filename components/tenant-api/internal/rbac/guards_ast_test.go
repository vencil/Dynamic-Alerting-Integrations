package rbac

// go/ast architecture guards for the identity/authorization seams (ADR-027 /
// LD-6 P3 + P4b). Two tripwires, one file:
//
//  1. VerifiedPrincipal construction is confined to package rbac and tests.
//  2. Org-blind `Allowed` calls outside internal/rbac are confined to the
//     "*"-literal platform gates plus an annotated (currently EMPTY)
//     allowlist.
//
// Why go/ast instead of golangci forbidigo (the hybrid-lint-policy default):
// verified empirically on golangci-lint 2.12.2 — forbidigo matches
// identifiers/selector expressions wherever they appear, so a pattern on
// `rbac\.VerifiedPrincipal` also flags the TYPE usage every converted
// signature needs (`p *rbac.VerifiedPrincipal` — false positives on the
// exact API this refactor introduces), and it cannot anchor on the
// composite-literal brace (its matched text is the selector, never `{`), so
// `rbac\.VerifiedPrincipal\{` matches nothing at all. Likewise the org-write
// guard must anchor on the CALL with a specific argument shape (tenant arg =
// "*"), which a selector-pattern linter cannot express. The AST walks below
// flag exactly the construction/call site and nothing else.
//
// moduleRootForGuard lives in testhelpers_test.go.

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

// ── guard 1: principal construction (LD-6 P3) ────────────────────────────────
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

// ── guard 2: write-plane org-scope (LD-6 P4b) ────────────────────────────────
//
// AllowedInOrg (via handler.OrgAllowed) is the ONLY legitimate per-tenant
// write/admin authorization entry point after P4b: a handler that calls the
// org-blind `Allowed` for a write decision compiles, behaves identically
// today (shadow mode), and silently DROPS the org-scope axis the moment
// --rbac-org-scope-enforce flips — the exact silent-failure shape guard 1
// pins for hand-built principals.
//
// This test parses every non-test .go file in the module outside
// internal/rbac (the package that legitimately implements the check) and
// inspects every `Allowed` call:
//
//   - AUTO-EXEMPT: a call whose tenant argument (2nd arg) is the basic
//     literal "*" — the platform-scope gate. Org-scope deliberately does not
//     apply to platform scope (invariant I6: an org-scoped rule is not a
//     platform admin), so these sites stay on the org-blind Allowed by
//     design and need no allowlist entry.
//   - Everything else must hit the annotated allowlist below. P4c end state:
//     the allowlist is EMPTY. P4c converted the last three read-plane filters
//     (filterByRBAC / hasAccessibleMember / ListPRs) to the org-aware
//     handler.OrgAllowedRead, so outside internal/rbac the ONLY remaining
//     org-blind Allowed calls are the "*"-literal platform gates (auto-exempt).
//     A new non-"*" org-blind Allowed anywhere is now a hard violation.
//
// A stale allowlist entry (no longer matching any call site) FAILS the test:
// an exemption must stay demonstrably necessary (#1067 discipline), or a
// converted call site would leave a hole a future write-plane caller could
// silently crawl through by reusing the entry's file+function.

// orgWriteAllowlist enumerates the sanctioned org-blind `Allowed` call sites
// outside internal/rbac (beyond the "*"-literal auto-exemption). Key is
// "<module-relative file path>::<enclosing function>"; value is why the site
// may stay org-blind.
//
// P4c end state: EMPTY. The three P4b-era read-plane exemptions (filterByRBAC /
// hasAccessibleMember / ListPRs) were converted to handler.OrgAllowedRead, so no
// non-"*" org-blind Allowed call remains outside internal/rbac. The map stays
// declared (not deleted) so a future read-plane filter that genuinely needs the
// org-blind path can be added back with an explicit reason rather than silently.
var orgWriteAllowlist = map[string]string{}

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
