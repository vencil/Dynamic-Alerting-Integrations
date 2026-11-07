package rbac

// ADR-027 §7: which bar a handler sets for "platform administrator". The
// unrestricted bars (PlatformAdminUnrestricted / PlatformUnrestricted: a "*"
// rule with no org-scope, environments or domains) are the default. The
// non-org-scoped bars (PlatformAdminNonOrgScoped / PlatformNonOrgScoped)
// also pass a rule restricted to some environments or domains, so they fit
// only a decision those axes do not bear on. Each use of them — a call, or a
// method value passed on (readsPlatform(m, p, m.PlatformNonOrgScoped)) — is
// listed here with why; a new one fails this test until it is added — so a new gate
// picks the permissive bar on purpose, and one that does not is stricter,
// never looser. go/ast, not grep: a method call is not a comment or a name in
// a string (guards_ast_test.go).

import (
	"go/ast"
	"sort"
	"testing"
)

// nonOrgScopedCallSites maps each production file (module-relative) using a
// non-org-scoped bar to the number of uses and why that bar fits.
var nonOrgScopedCallSites = map[string]struct {
	calls int
	why   string
}{
	"internal/handler/audit_dryrun.go":        {1, "the what-if report lists grants and reveals no tenant content"},
	"internal/handler/audit_reverse.go":       {1, "the reverse report lists grants and reveals no tenant content"},
	"internal/handler/federation/policy.go":   {1, "the federation whitelist belongs to no tenant"},
	"internal/handler/federation/accounts.go": {1, "the federation account registry belongs to no tenant"},
	"internal/handler/view.go":                {1, "saved views belong to no tenant"},
	"internal/handler/events.go":              {1, "saved views belong to no tenant (the views change event)"},
}

func TestNonOrgScopedBarsOnlyWhereListed(t *testing.T) {
	t.Parallel()
	files, fset := prodFiles(t)
	got := map[string]int{}
	for rel, f := range files {
		if rel == "internal/rbac/reverse.go" { // the definitions
			continue
		}
		// Every selector naming a bar, not only a call's Fun: a method value
		// handed to a helper is a use too.
		ast.Inspect(f, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			switch sel.Sel.Name {
			case "PlatformAdminNonOrgScoped", "PlatformNonOrgScoped":
				got[rel]++
				if _, listed := nonOrgScopedCallSites[rel]; !listed {
					t.Errorf("%s: %s is not a listed call site of a non-org-scoped bar; "+
						"use PlatformAdminUnrestricted / PlatformUnrestricted, or add it to "+
						"nonOrgScopedCallSites with why environments and domains do not matter there",
						fset.Position(sel.Pos()), sel.Sel.Name)
				}
			}
			return true
		})
	}
	var listed []string
	for rel := range nonOrgScopedCallSites {
		listed = append(listed, rel)
	}
	sort.Strings(listed)
	for _, rel := range listed {
		if want := nonOrgScopedCallSites[rel].calls; got[rel] != want {
			t.Errorf("%s: %d use(s) of a non-org-scoped bar, the list says %d", rel, got[rel], want)
		}
	}
}
