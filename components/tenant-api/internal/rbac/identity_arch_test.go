package rbac

// ADR-027 §9 item 2, as an architecture test: identity is built from headers
// only on the UDS listener's path, and the rate limiter, the request log and
// the dev bypass read the identity object, never a header.
//
// go/ast, not grep, for the reason guards_ast_test.go gives: the questions are
// about call shapes ("a one-argument .Resolve call", "a request's .Header
// field"), which a text pattern cannot tell from a comment or a method call on
// a response writer. The UDS-only half — that the TCP router mounts no
// resolver — is behavioral and lives in cmd/server/tcp_router_test.go.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// prodFiles parses every non-test .go file of the module, keyed by the path
// relative to the module root (slash-separated).
func prodFiles(t *testing.T) (map[string]*ast.File, *token.FileSet) {
	t.Helper()
	root := moduleRootForGuard(t)
	fset := token.NewFileSet()
	files := map[string]*ast.File{}
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if name := d.Name(); name == "testdata" || name == "vendor" || strings.HasPrefix(name, ".") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			return perr
		}
		rel, _ := filepath.Rel(root, path)
		files[filepath.ToSlash(rel)] = f
		return nil
	})
	if err != nil {
		t.Fatalf("walk module: %v", err)
	}
	if len(files) == 0 {
		t.Fatal("found no production .go files: the guard would pass vacuously")
	}
	return files, fset
}

// Only ResolveIdentity turns headers into a principal: a one-argument
// .Resolve(…) call (HeaderResolver.Resolve's shape) anywhere else is a second
// place identity is built, and nothing would keep it off the TCP plane.
// adr-check: ADR-027/9.2a
func TestHeaderResolutionOnlyInResolveIdentity(t *testing.T) {
	t.Parallel()
	files, fset := prodFiles(t)
	const allowed = "internal/rbac/identity.go"
	var violations []string
	sawAllowed := false
	for rel, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok || len(call.Args) != 1 {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != "Resolve" {
				return true
			}
			if rel == allowed {
				sawAllowed = true
				return true
			}
			violations = append(violations, fset.Position(call.Pos()).String())
			return true
		})
	}
	if !sawAllowed {
		t.Fatalf("%s no longer calls .Resolve(r): the guard's anchor moved — update it", allowed)
	}
	sort.Strings(violations)
	if len(violations) > 0 {
		t.Errorf("identity is resolved from headers outside rbac.ResolveIdentity (ADR-027 §9 item 2):\n  %s",
			strings.Join(violations, "\n  "))
	}
}

// The conventional identity header names may appear only where they are
// defined (proxyconfig), as the resolver's fallback (principal.go) and in the
// TCP plane's refusal (tcp_router.go). Anywhere else a literal or a reference
// to them is a read of identity that skips the resolver.
// adr-check: ADR-027/9.2a
func TestIdentityHeaderNamesOnlyInTheirOwners(t *testing.T) {
	t.Parallel()
	files, fset := prodFiles(t)
	literalOwners := map[string]bool{"internal/proxyconfig/proxyconfig.go": true}
	constOwners := map[string]bool{
		"internal/rbac/principal.go": true,
		"cmd/server/tcp_router.go":   true,
	}
	identityNames := map[string]bool{"x-forwarded-email": true, "x-forwarded-groups": true}
	var violations []string
	for rel, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.BasicLit:
				if x.Kind != token.STRING || literalOwners[rel] {
					return true
				}
				if v, err := strconv.Unquote(x.Value); err == nil && identityNames[strings.ToLower(strings.TrimSpace(v))] {
					violations = append(violations, fset.Position(x.Pos()).String()+" literal "+x.Value)
				}
			case *ast.SelectorExpr:
				if id, ok := x.X.(*ast.Ident); ok && id.Name == "proxyconfig" &&
					(x.Sel.Name == "DefaultEmailHeader" || x.Sel.Name == "DefaultGroupsHeader") && !constOwners[rel] {
					violations = append(violations, fset.Position(x.Pos()).String()+" proxyconfig."+x.Sel.Name)
				}
			}
			return true
		})
	}
	sort.Strings(violations)
	if len(violations) > 0 {
		t.Errorf("identity header names used outside their owners (ADR-027 §9 item 2):\n  %s",
			strings.Join(violations, "\n  "))
	}
}

// The rate limiter's caller key, the request log and the dev bypass do not
// touch a request's headers at all: a `<x>.Header` field read (a method call
// such as w.Header() on the response writer is not one) in their bodies fails.
// adr-check: ADR-027/9.2b
func TestRateLimitLogAndBypassReadNoRequestHeader(t *testing.T) {
	t.Parallel()
	files, fset := prodFiles(t)
	targets := map[string]string{
		"rateLimitCaller":     "internal/handler/rate_limiter.go",
		"SlogRequestLogger":   "internal/handler/middleware.go",
		"DevBypassMiddleware": "internal/rbac/devbypass.go",
	}
	found := map[string]bool{}
	var violations []string
	for name, rel := range targets {
		f := files[rel]
		if f == nil {
			t.Fatalf("%s not found: the guard's anchor moved — update it", rel)
		}
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Name.Name != name || fn.Body == nil {
				continue
			}
			found[name] = true
			called := map[*ast.SelectorExpr]bool{}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				if call, ok := n.(*ast.CallExpr); ok {
					if sel, ok := call.Fun.(*ast.SelectorExpr); ok {
						called[sel] = true
					}
				}
				return true
			})
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "Header" && !called[sel] {
					violations = append(violations, name+" at "+fset.Position(sel.Pos()).String())
				}
				return true
			})
		}
	}
	for name, rel := range targets {
		if !found[name] {
			t.Fatalf("func %s not found in %s: the guard's anchor moved — update it", name, rel)
		}
	}
	sort.Strings(violations)
	if len(violations) > 0 {
		t.Errorf("rate limiter / request log / dev bypass read a request header (ADR-027 §9 item 2):\n  %s",
			strings.Join(violations, "\n  "))
	}
}
