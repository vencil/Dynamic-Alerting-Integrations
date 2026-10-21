package main

// The direct answers (#3202): the codes a handler or middleware writes itself,
// without the kind table — WriteJSONErrorWithCode, an ErrorResponse literal
// handed to WriteErrorEnvelope, WriteJSONError (its code is codeFromStatus's
// for the status), and the RBAC middleware's writeEnvelope / writeError. The
// kind table lists its own statuses; these have no table, so the source is
// read for them.
//
// ⛔ Fail-closed: a call of one of those writers whose status or code is not
// a literal (http.StatusX, an integer, errcode.X) is an error naming its
// place, and so is every errcode.X in the scanned code that is neither one
// of the recognized shapes nor a listed exemption — a new way of writing a
// code cannot be missed silently. A function that only forwards its own
// parameters (forwarders) is listed by name, and a listed name that no
// longer exists is an error too.

import (
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"net/http"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"github.com/vencil/tenant-api/internal/errcode"
)

const errcodePath = "github.com/vencil/tenant-api/internal/errcode"

// directUse is one (status, code) a direct answer writes, and where.
type directUse struct {
	Status int
	Code   errcode.Code
	Site   string // file:line
}

// siteFunc names a function: its file (slash path from the module root) and
// its name.
type siteFunc struct{ file, fn string }

// forwarders write an answer from their own parameters; their callers are
// what is read.
var forwarders = []siteFunc{
	{"internal/handler/errors.go", "WriteJSONError"},
	{"internal/handler/errors.go", "WriteJSONErrorWithCode"},
	{"internal/handler/error_render.go", "writeKindAnswer"}, // the kind table's answers
	{"internal/rbac/middleware.go", "writeError"},
}

// statusMaps are the functions that map a status to a code with a switch:
// their case clauses are read as status → code.
var statusMaps = []siteFunc{
	{"internal/handler/errors.go", "codeFromStatus"}, // WriteJSONError
	{"internal/rbac/middleware.go", "writeError"},    // the RBAC middleware's writeError
}

// exemption is an errcode.X the scanner does not read as a direct answer:
// Status > 0 adds that (status, code) to the contract, 0 adds nothing.
type exemption struct {
	siteFunc
	code   string // the constant's Go name
	status int
	reason string
}

var exemptions = []exemption{
	{siteFunc{"internal/handler/errors.go", "writeJSON"}, "Internal", http.StatusInternalServerError,
		"writeJSON's own 500 when a body cannot be encoded"},
	{siteFunc{"internal/generator/errors.go", ""}, "ValidatorUnavailable", 0,
		"the generator service's protocol code; tenant-api's answer is the kind table's"},
	{siteFunc{"internal/generator/errors.go", ""}, "PolicyUnavailable", 0,
		"the generator service's protocol code; tenant-api's answer is the kind table's"},
	{siteFunc{"internal/handler/tenant_validate.go", "ValidateTenant"}, "TenantGitManaged", 0,
		"a field of the 200 /validate verdict, not an error answer"},
	{siteFunc{"internal/handler/tenant_validate.go", "ValidateTenant"}, "TenantFileDirty", 0,
		"a field of the 200 /validate verdict, not an error answer"},
}

// httpStatus is net/http's error statuses by constant name. A status name
// not here (a 2xx or 3xx in an error writer, a typo) is an error.
var httpStatus = map[string]int{
	"StatusBadRequest":                    http.StatusBadRequest,
	"StatusUnauthorized":                  http.StatusUnauthorized,
	"StatusPaymentRequired":               http.StatusPaymentRequired,
	"StatusForbidden":                     http.StatusForbidden,
	"StatusNotFound":                      http.StatusNotFound,
	"StatusMethodNotAllowed":              http.StatusMethodNotAllowed,
	"StatusNotAcceptable":                 http.StatusNotAcceptable,
	"StatusProxyAuthRequired":             http.StatusProxyAuthRequired,
	"StatusRequestTimeout":                http.StatusRequestTimeout,
	"StatusConflict":                      http.StatusConflict,
	"StatusGone":                          http.StatusGone,
	"StatusLengthRequired":                http.StatusLengthRequired,
	"StatusPreconditionFailed":            http.StatusPreconditionFailed,
	"StatusRequestEntityTooLarge":         http.StatusRequestEntityTooLarge,
	"StatusRequestURITooLong":             http.StatusRequestURITooLong,
	"StatusUnsupportedMediaType":          http.StatusUnsupportedMediaType,
	"StatusRequestedRangeNotSatisfiable":  http.StatusRequestedRangeNotSatisfiable,
	"StatusExpectationFailed":             http.StatusExpectationFailed,
	"StatusTeapot":                        http.StatusTeapot,
	"StatusMisdirectedRequest":            http.StatusMisdirectedRequest,
	"StatusUnprocessableEntity":           http.StatusUnprocessableEntity,
	"StatusLocked":                        http.StatusLocked,
	"StatusFailedDependency":              http.StatusFailedDependency,
	"StatusTooEarly":                      http.StatusTooEarly,
	"StatusUpgradeRequired":               http.StatusUpgradeRequired,
	"StatusPreconditionRequired":          http.StatusPreconditionRequired,
	"StatusTooManyRequests":               http.StatusTooManyRequests,
	"StatusRequestHeaderFieldsTooLarge":   http.StatusRequestHeaderFieldsTooLarge,
	"StatusUnavailableForLegalReasons":    http.StatusUnavailableForLegalReasons,
	"StatusInternalServerError":           http.StatusInternalServerError,
	"StatusNotImplemented":                http.StatusNotImplemented,
	"StatusBadGateway":                    http.StatusBadGateway,
	"StatusServiceUnavailable":            http.StatusServiceUnavailable,
	"StatusGatewayTimeout":                http.StatusGatewayTimeout,
	"StatusHTTPVersionNotSupported":       http.StatusHTTPVersionNotSupported,
	"StatusVariantAlsoNegotiates":         http.StatusVariantAlsoNegotiates,
	"StatusInsufficientStorage":           http.StatusInsufficientStorage,
	"StatusLoopDetected":                  http.StatusLoopDetected,
	"StatusNotExtended":                   http.StatusNotExtended,
	"StatusNetworkAuthenticationRequired": http.StatusNetworkAuthenticationRequired,
}

// errcodeNames reads internal/errcode's constants: Go name → code.
func errcodeNames(modRoot string) (map[string]errcode.Code, error) {
	files, err := filepath.Glob(filepath.Join(modRoot, "internal", "errcode", "*.go"))
	if err != nil {
		return nil, err
	}
	out := map[string]errcode.Code{}
	fset := token.NewFileSet()
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		af, err := parser.ParseFile(fset, f, nil, 0)
		if err != nil {
			return nil, err
		}
		for _, d := range af.Decls {
			gd, ok := d.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, sp := range gd.Specs {
				vs := sp.(*ast.ValueSpec)
				if id, ok := vs.Type.(*ast.Ident); !ok || id.Name != "Code" {
					continue
				}
				for i, n := range vs.Names {
					lit, ok := vs.Values[i].(*ast.BasicLit)
					if !ok {
						return nil, fmt.Errorf("errcode.%s is not a string literal", n.Name)
					}
					s, err := strconv.Unquote(lit.Value)
					if err != nil {
						return nil, err
					}
					out[n.Name] = errcode.Code(s)
				}
			}
		}
	}
	if len(out) == 0 {
		return nil, errors.New("read no errcode constants")
	}
	return out, nil
}

type scanner struct {
	fset    *token.FileSet
	names   map[string]errcode.Code
	file    string // slash path from the module root
	local   string // the errcode import's local name in file
	maps    map[siteFunc]map[int]errcode.Code
	uses    []directUse
	used    map[string]bool // forwarders, maps, exemptions met
	errs    []string
	pending []pendingCall // calls that need a status map read later
}

type pendingCall struct {
	mapFn  siteFunc
	status int
	site   string
}

func (s *scanner) pos(p token.Pos) string {
	ps := s.fset.Position(p)
	return fmt.Sprintf("%s:%d", s.file, ps.Line)
}

func (s *scanner) fail(p token.Pos, format string, a ...any) {
	s.errs = append(s.errs, s.pos(p)+": "+fmt.Sprintf(format, a...))
}

// codeOf is e as errcode.X, if it is one.
func (s *scanner) codeOf(e ast.Expr) (string, errcode.Code, bool) {
	sel, ok := e.(*ast.SelectorExpr)
	if !ok {
		return "", "", false
	}
	id, ok := sel.X.(*ast.Ident)
	if !ok || s.local == "" || id.Name != s.local {
		return "", "", false
	}
	c, ok := s.names[sel.Sel.Name]
	return sel.Sel.Name, c, ok
}

// statusOf is e as http.StatusX or an integer literal.
func statusOf(e ast.Expr) (int, bool) {
	switch x := e.(type) {
	case *ast.SelectorExpr:
		if id, ok := x.X.(*ast.Ident); ok && id.Name == "http" {
			st, ok := httpStatus[x.Sel.Name]
			return st, ok
		}
	case *ast.BasicLit:
		if x.Kind == token.INT {
			n, err := strconv.Atoi(x.Value)
			return n, err == nil && n >= 400 && n <= 599
		}
	}
	return 0, false
}

func calleeName(c *ast.CallExpr) string {
	switch f := c.Fun.(type) {
	case *ast.Ident:
		return f.Name
	case *ast.SelectorExpr:
		return f.Sel.Name
	}
	return ""
}

func isEnvelopeType(e ast.Expr) bool {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name == "ErrorResponse" || t.Name == "errorEnvelope"
	case *ast.SelectorExpr:
		return t.Sel.Name == "ErrorResponse"
	}
	return false
}

// codeField is lit's Code: value.
func codeField(lit *ast.CompositeLit) (ast.Expr, bool) {
	for _, el := range lit.Elts {
		if kv, ok := el.(*ast.KeyValueExpr); ok {
			if k, ok := kv.Key.(*ast.Ident); ok && k.Name == "Code" {
				return kv.Value, true
			}
		}
	}
	return nil, false
}

func (s *scanner) inRBAC() bool { return strings.HasPrefix(s.file, "internal/rbac/") }

// scanFile reads one file: its status maps, its writer calls and every
// errcode.X in it.
func (s *scanner) scanFile(af *ast.File) {
	s.local = ""
	for _, im := range af.Imports {
		if p, _ := strconv.Unquote(im.Path.Value); p == errcodePath {
			s.local = "errcode"
			if im.Name != nil {
				s.local = im.Name.Name
			}
		}
	}
	// accounted: the errcode.X selectors a recognized shape read.
	accounted := map[ast.Expr]bool{}
	for _, d := range af.Decls {
		fn := ""
		if fd, ok := d.(*ast.FuncDecl); ok {
			fn = fd.Name.Name
		}
		here := siteFunc{s.file, fn}
		forwarder := slices.Contains(forwarders, here)
		if forwarder {
			s.used["fwd "+s.file+" "+fn] = true
		}
		if slices.Contains(statusMaps, here) {
			s.used["map "+s.file+" "+fn] = true
			s.readStatusMap(d.(*ast.FuncDecl), here, accounted)
		}
		ast.Inspect(d, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.CallExpr:
				s.readCall(x, forwarder, accounted)
			case *ast.CompositeLit:
				s.readComposite(x, forwarder, accounted)
			case *ast.BinaryExpr:
				// A comparison reads a code; it never answers one.
				if x.Op == token.EQL || x.Op == token.NEQ {
					accounted[x.X], accounted[x.Y] = true, true
				}
			}
			return true
		})
		// Every errcode.X left is unrecognized unless exempted.
		ast.Inspect(d, func(n ast.Node) bool {
			sel, ok := n.(ast.Expr)
			if !ok || accounted[sel] {
				return true
			}
			name, code, ok := s.codeOf(sel)
			if !ok {
				return true
			}
			for _, ex := range exemptions {
				if ex.siteFunc == here && ex.code == name {
					s.used["ex "+ex.file+" "+ex.fn+" "+ex.code] = true
					if ex.status > 0 {
						s.uses = append(s.uses, directUse{ex.status, code, s.pos(sel.Pos())})
					}
					return true
				}
			}
			s.fail(sel.Pos(), "errcode.%s is used in a way the contract generator does not read (in %s); "+
				"write it through WriteJSONErrorWithCode / an ErrorResponse literal to WriteErrorEnvelope / "+
				"the kind table, or list it in cmd/gen-error-contract's exemptions with the reason", name, orTop(fn))
			return true
		})
	}
}

func orTop(fn string) string {
	if fn == "" {
		return "a top-level declaration"
	}
	return fn
}

// readStatusMap reads `case http.StatusX: return errcode.Y` (or
// `code = errcode.Y`) clauses.
func (s *scanner) readStatusMap(fd *ast.FuncDecl, here siteFunc, accounted map[ast.Expr]bool) {
	m := map[int]errcode.Code{}
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		cc, ok := n.(*ast.CaseClause)
		if !ok || cc.List == nil {
			return true
		}
		var codeExpr ast.Expr
		for _, st := range cc.Body {
			switch x := st.(type) {
			case *ast.ReturnStmt:
				if len(x.Results) == 1 {
					codeExpr = x.Results[0]
				}
			case *ast.AssignStmt:
				if len(x.Rhs) == 1 {
					codeExpr = x.Rhs[0]
				}
			}
		}
		_, code, ok := s.codeOf(codeExpr)
		if codeExpr == nil || !ok {
			s.fail(cc.Pos(), "%s: a case that does not map to an errcode constant", here.fn)
			return true
		}
		accounted[codeExpr] = true
		for _, e := range cc.List {
			st, ok := statusOf(e)
			if !ok {
				s.fail(e.Pos(), "%s: a case that is not a literal status", here.fn)
				continue
			}
			m[st] = code
		}
		return true
	})
	s.maps[here] = m
}

func (s *scanner) readCall(c *ast.CallExpr, forwarder bool, accounted map[ast.Expr]bool) {
	name := calleeName(c)
	var mapFn siteFunc
	switch name {
	case "WriteJSONError":
		mapFn = siteFunc{"internal/handler/errors.go", "codeFromStatus"}
	case "WriteJSONErrorWithCode", "WriteErrorEnvelope":
	case "writeEnvelope":
		if !s.inRBAC() {
			return
		}
	case "writeError":
		if !s.inRBAC() {
			return
		}
		mapFn = siteFunc{"internal/rbac/middleware.go", "writeError"}
	default:
		return
	}
	if forwarder {
		return
	}
	if len(c.Args) < 4 {
		s.fail(c.Pos(), "%s: %d arguments, want (w, r, status, …)", name, len(c.Args))
		return
	}
	status, ok := statusOf(c.Args[2])
	if !ok {
		s.fail(c.Args[2].Pos(), "%s: the status is not a literal (http.StatusX or an integer)", name)
		return
	}
	switch name {
	case "WriteJSONError", "writeError":
		s.pending = append(s.pending, pendingCall{mapFn, status, s.pos(c.Pos())})
	case "WriteJSONErrorWithCode":
		_, code, ok := s.codeOf(c.Args[3])
		if !ok {
			s.fail(c.Args[3].Pos(), "%s: the code is not an errcode constant", name)
			return
		}
		accounted[c.Args[3]] = true
		s.uses = append(s.uses, directUse{status, code, s.pos(c.Pos())})
	default: // WriteErrorEnvelope, writeEnvelope
		lit, ok := c.Args[3].(*ast.CompositeLit)
		if !ok {
			s.fail(c.Args[3].Pos(), "%s: the envelope is not a literal, so its code cannot be read", name)
			return
		}
		v, ok := codeField(lit)
		if !ok {
			s.fail(lit.Pos(), "%s: the envelope literal sets no Code", name)
			return
		}
		_, code, ok := s.codeOf(v)
		if !ok {
			s.fail(v.Pos(), "%s: the envelope's Code is not an errcode constant", name)
			return
		}
		accounted[v] = true
		s.uses = append(s.uses, directUse{status, code, s.pos(c.Pos())})
	}
}

// readComposite: an envelope literal anywhere else that sets Code must set
// an errcode constant (its errcode.X is then judged as unrecognized unless
// a call read it); a KindSpec row's Code is the kind table's.
func (s *scanner) readComposite(lit *ast.CompositeLit, forwarder bool, accounted map[ast.Expr]bool) {
	if at, ok := lit.Type.(*ast.ArrayType); ok {
		if id, ok := at.Elt.(*ast.Ident); ok && id.Name == "KindSpec" {
			for _, el := range lit.Elts {
				if row, ok := el.(*ast.CompositeLit); ok {
					if v, ok := codeField(row); ok {
						accounted[v] = true
					}
				}
			}
		}
		return
	}
	if lit.Type == nil || !isEnvelopeType(lit.Type) {
		return
	}
	v, ok := codeField(lit)
	if !ok || forwarder {
		return
	}
	if _, _, ok := s.codeOf(v); !ok {
		s.fail(v.Pos(), "an error envelope literal whose Code is not an errcode constant")
	}
}

// scanDirect reads every non-test .go file under internal/ and cmd/ of the
// module at modRoot but internal/errcode and this command, and returns the
// direct answers, sorted by code, status and site.
func scanDirect(modRoot string) ([]directUse, error) {
	names, err := errcodeNames(modRoot)
	if err != nil {
		return nil, err
	}
	s := &scanner{fset: token.NewFileSet(), names: names, maps: map[siteFunc]map[int]errcode.Code{}, used: map[string]bool{}}
	for _, dir := range []string{"internal", "cmd"} {
		err := filepath.WalkDir(filepath.Join(modRoot, dir), func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			rel, _ := filepath.Rel(modRoot, p)
			rel = filepath.ToSlash(rel)
			if d.IsDir() {
				if rel == "internal/errcode" || rel == "cmd/gen-error-contract" || d.Name() == "testdata" {
					return filepath.SkipDir
				}
				return nil
			}
			if path.Ext(rel) != ".go" || strings.HasSuffix(rel, "_test.go") {
				return nil
			}
			af, err := parser.ParseFile(s.fset, p, nil, 0)
			if err != nil {
				return err
			}
			s.file = rel
			s.scanFile(af)
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	for _, pc := range s.pending {
		code, ok := s.maps[pc.mapFn][pc.status]
		if !ok {
			s.errs = append(s.errs, fmt.Sprintf("%s: %s has no code for status %d", pc.site, pc.mapFn.fn, pc.status))
			continue
		}
		s.uses = append(s.uses, directUse{pc.status, code, pc.site})
	}
	for _, f := range forwarders {
		if !s.used["fwd "+f.file+" "+f.fn] {
			s.errs = append(s.errs, fmt.Sprintf("forwarder %s in %s no longer exists; drop it from the list", f.fn, f.file))
		}
	}
	for _, f := range statusMaps {
		if !s.used["map "+f.file+" "+f.fn] {
			s.errs = append(s.errs, fmt.Sprintf("status map %s in %s no longer exists; drop it from the list", f.fn, f.file))
		}
	}
	for _, ex := range exemptions {
		if !s.used["ex "+ex.file+" "+ex.fn+" "+ex.code] {
			s.errs = append(s.errs, fmt.Sprintf("exemption errcode.%s in %s (%s) matches nothing; drop it", ex.code, ex.file, orTop(ex.fn)))
		}
	}
	if len(s.errs) > 0 {
		slices.Sort(s.errs)
		return nil, errors.New("the direct error answers cannot all be read:\n  " + strings.Join(s.errs, "\n  "))
	}
	slices.SortFunc(s.uses, func(a, b directUse) int {
		if c := strings.Compare(string(a.Code), string(b.Code)); c != 0 {
			return c
		}
		if a.Status != b.Status {
			return a.Status - b.Status
		}
		return strings.Compare(a.Site, b.Site)
	})
	return s.uses, nil
}
