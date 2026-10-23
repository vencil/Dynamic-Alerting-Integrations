package main

// The Extra fields the handlers set (#3272): every key an error envelope's
// Extra map can carry, the code it is sent under and the JSON type of its
// value, read from the type-checked source — so that checkExtra can hold
// every one of them to internal/errcode's declarations (name and type) before
// anything ships (ErrorResponse.MarshalJSON drops an undeclared field at run
// time; that drop is the backstop, not the check).
//
// Two shapes set Extra, and both are read:
//
//   - an envelope literal, ErrorResponse{…, Extra: E}: its code is the
//     literal's Code (an errcode constant) or — for the literal handed to
//     writeKindAnswer — the kind its spec argument was looked up for
//     (`spec := kindSpecOf(errkind.X)`); E is a map[string]any literal or a
//     local variable, whose keys are those of the map literal it is assigned
//     and of every `v["key"] = …` on it;
//   - renderKind, whose second result is a kind's Extra: per case clause of
//     its switch, the keys of the map[string]any literals it returns or
//     assigns to a variable, and of every `v["key"] = …` on such a variable;
//     the code is each case kind's in the kind table.
//
// writeErr hands renderKind's result on (`text, extra := renderKind(…)`):
// that Extra is renderKind's, read there.
//
// The JSON type of each value is its Go type's (go/types): a string, integer
// or boolean basic type, an array (a slice or array, with its element's
// type), an object (a map or a struct); a pointer is its element's. A type
// with its own MarshalJSON / MarshalText, an interface, a float or anything
// else is not judged — it stops the command at its place.
//
// ⛔ Fail-closed, as scan.go: a key that is not a string literal, an Extra
// value of another shape, an assignment to an Extra variable that is not a
// map literal, an Extra variable used any other way than being assigned a map
// literal, indexed, or handed over as the Extra (passed to a function — a
// helper that adds keys, maps.Copy —, copied to another variable, its address
// taken, ranged over), a renderKind return whose Extra is none of the above,
// a literal whose code cannot be read, an Extra set on an envelope after it
// was built (env.Extra = …, env.Extra[k] = …), a value whose JSON type cannot
// be told, and source that does not type-check each stop the command with
// their place. A new way of setting a field cannot be missed silently.

import (
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"golang.org/x/tools/go/packages"
)

const errkindPath = "github.com/vencil/tenant-api/internal/errkind"

// renderKindFunc is the function whose case clauses give each kind's Extra.
var renderKindFunc = siteFunc{"internal/handler/error_render.go", "renderKind"}

// extraUse is one Extra field a handler sets, the code it is sent under — or
// the kind, whose code is the kind table's —, the JSON type of its value
// (Items: an array's elements), its Go type, and where.
type extraUse struct {
	Code   errcode.Code
	Kind   errkind.Kind
	Field  string
	Type   errcode.ExtraType
	Items  errcode.ExtraType
	GoType string
	Site   string // file:line
}

// keyVal is one key an Extra map is given and the value it is given.
type keyVal struct {
	key string
	val ast.Expr
}

type extraScanner struct {
	fset      *token.FileSet
	info      *types.Info
	kinds     map[string]errkind.Kind // errkind Go name → kind
	codes     map[string]errcode.Code // errcode Go name → code
	file      string
	codeLocal string
	kindLocal string
	uses      []extraUse
	errs      []string
	sawRender bool
}

func (s *extraScanner) pos(p token.Pos) string {
	return fmt.Sprintf("%s:%d", s.file, s.fset.Position(p).Line)
}

func (s *extraScanner) fail(p token.Pos, format string, a ...any) {
	s.errs = append(s.errs, s.pos(p)+": "+fmt.Sprintf(format, a...))
}

// selectorOf is e as local.Name.
func selectorOf(e ast.Expr, local string) (string, bool) {
	sel, ok := e.(*ast.SelectorExpr)
	if !ok || local == "" {
		return "", false
	}
	id, ok := sel.X.(*ast.Ident)
	if !ok || id.Name != local {
		return "", false
	}
	return sel.Sel.Name, true
}

func (s *extraScanner) codeOf(e ast.Expr) (errcode.Code, bool) {
	n, ok := selectorOf(e, s.codeLocal)
	if !ok {
		return "", false
	}
	c, ok := s.codes[n]
	return c, ok
}

func (s *extraScanner) kindOf(e ast.Expr) (errkind.Kind, bool) {
	n, ok := selectorOf(e, s.kindLocal)
	if !ok {
		return "", false
	}
	k, ok := s.kinds[n]
	return k, ok
}

// isAnyMap reports whether t is map[string]any (or map[string]interface{}).
func isAnyMap(t ast.Expr) bool {
	m, ok := t.(*ast.MapType)
	if !ok {
		return false
	}
	if k, ok := m.Key.(*ast.Ident); !ok || k.Name != "string" {
		return false
	}
	switch v := m.Value.(type) {
	case *ast.Ident:
		return v.Name == "any"
	case *ast.InterfaceType:
		return v.Methods == nil || len(v.Methods.List) == 0
	}
	return false
}

// anyMapLit is e as a map[string]any literal.
func anyMapLit(e ast.Expr) (*ast.CompositeLit, bool) {
	lit, ok := e.(*ast.CompositeLit)
	if !ok || !isAnyMap(lit.Type) {
		return nil, false
	}
	return lit, true
}

// litKeys is lit's keys, each a string literal, with their values.
func (s *extraScanner) litKeys(lit *ast.CompositeLit) []keyVal {
	var out []keyVal
	for _, el := range lit.Elts {
		kv, ok := el.(*ast.KeyValueExpr)
		if !ok {
			s.fail(el.Pos(), "an Extra map literal element that is not key: value")
			continue
		}
		if k, ok := stringLit(kv.Key); ok {
			out = append(out, keyVal{k, kv.Value})
		} else {
			s.fail(kv.Key.Pos(), "an Extra key that is not a string literal")
		}
	}
	return out
}

func stringLit(e ast.Expr) (string, bool) {
	lit, ok := e.(*ast.BasicLit)
	if !ok || lit.Kind != token.STRING {
		return "", false
	}
	v, err := strconv.Unquote(lit.Value)
	return v, err == nil
}

// is reports whether id names the variable v.
func (s *extraScanner) is(id *ast.Ident, v types.Object) bool {
	return v != nil && s.info.ObjectOf(id) == v
}

// varKeys is the keys of the Extra variable v within body: the map literals
// assigned to it and every v["key"] = … . Any other assignment to it is an
// error, and so is any other use of it than those, an index read, or one of
// sinks (where it is handed over as an Extra): passed to a call, copied,
// address taken, ranged over — its keys could then change unseen. ok is
// false when nothing in body assigns it a map literal.
func (s *extraScanner) varKeys(body ast.Node, v types.Object, sinks []*ast.Ident) (keys []keyVal, ok bool) {
	allowed := map[*ast.Ident]bool{}
	for _, id := range sinks {
		allowed[id] = true
	}
	ast.Inspect(body, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.IndexExpr:
			if id, isID := x.X.(*ast.Ident); isID && s.is(id, v) {
				allowed[id] = true // a read, or the assignment below
			}
		case *ast.AssignStmt:
			for i, l := range x.Lhs {
				switch lx := l.(type) {
				case *ast.Ident:
					if !s.is(lx, v) {
						continue
					}
					allowed[lx] = true
					if len(x.Rhs) != len(x.Lhs) {
						s.fail(lx.Pos(), "Extra variable %s is assigned something other than a map[string]any literal", lx.Name)
						continue
					}
					lit, isLit := anyMapLit(x.Rhs[i])
					if !isLit {
						s.fail(lx.Pos(), "Extra variable %s is assigned something other than a map[string]any literal", lx.Name)
						continue
					}
					ok = true
					keys = append(keys, s.litKeys(lit)...)
				case *ast.IndexExpr:
					id, isID := lx.X.(*ast.Ident)
					if !isID || !s.is(id, v) {
						continue
					}
					k, isStr := stringLit(lx.Index)
					if !isStr {
						s.fail(lx.Index.Pos(), "an Extra key of %s that is not a string literal", id.Name)
						continue
					}
					if len(x.Rhs) != len(x.Lhs) {
						s.fail(lx.Pos(), "an Extra key of %s assigned from a multi-value expression", id.Name)
						continue
					}
					keys = append(keys, keyVal{k, x.Rhs[i]})
				}
			}
		}
		return true
	})
	ast.Inspect(body, func(n ast.Node) bool {
		if id, isID := n.(*ast.Ident); isID && s.is(id, v) && !allowed[id] {
			s.fail(id.Pos(), "Extra variable %s is used other than by assigning it a map[string]any literal, "+
				"an index or handing it over as the Extra (passed to a call, copied, address taken, ranged over): "+
				"the keys it ends up with cannot be read; set every key in the function that builds the envelope", id.Name)
		}
		return true
	})
	return keys, ok
}

// marshals reports whether t (or *t) has its own MarshalJSON or MarshalText:
// its JSON type is then not its Go type's.
func marshals(t types.Type) bool {
	for _, m := range []string{"MarshalJSON", "MarshalText"} {
		if obj, _, _ := types.LookupFieldOrMethod(t, true, nil, m); obj != nil {
			if _, isFunc := obj.(*types.Func); isFunc {
				return true
			}
		}
	}
	return false
}

// jsonTypeOf is the JSON type encoding/json gives a value of Go type t (and
// an array's element type); ok is false when it cannot be told here.
func jsonTypeOf(t types.Type) (typ, items errcode.ExtraType, ok bool) {
	if t == nil {
		return "", "", false
	}
	if _, isNamed := t.(*types.Named); isNamed && marshals(t) {
		return "", "", false
	}
	switch u := t.Underlying().(type) {
	case *types.Basic:
		switch info := u.Info(); {
		case info&types.IsString != 0:
			return errcode.ExtraString, "", true
		case info&types.IsInteger != 0:
			return errcode.ExtraInteger, "", true
		case info&types.IsBoolean != 0:
			return errcode.ExtraBoolean, "", true
		}
	case *types.Pointer:
		return jsonTypeOf(u.Elem())
	case *types.Slice:
		if b, isBasic := u.Elem().Underlying().(*types.Basic); isBasic && b.Kind() == types.Byte {
			return "", "", false // []byte is a base64 string
		}
		it, _, ok := jsonTypeOf(u.Elem())
		return errcode.ExtraArray, it, ok
	case *types.Array:
		it, _, ok := jsonTypeOf(u.Elem())
		return errcode.ExtraArray, it, ok
	case *types.Map:
		return errcode.ExtraObject, "", true
	case *types.Struct:
		return errcode.ExtraObject, "", true
	}
	return "", "", false
}

func (s *extraScanner) add(code errcode.Code, kind errkind.Kind, keys []keyVal, p token.Pos) {
	for _, kv := range keys {
		t := s.info.TypeOf(kv.val)
		typ, items, ok := jsonTypeOf(t)
		if !ok {
			s.fail(kv.val.Pos(), "cannot tell the JSON type of Extra %q (Go type %s); give it a string, integer, boolean, slice, map or struct value", kv.key, t)
			continue
		}
		s.uses = append(s.uses, extraUse{Code: code, Kind: kind, Field: kv.key, Type: typ, Items: items,
			GoType: types.TypeString(t, nil), Site: s.pos(p)})
	}
}

// scanFunc reads one function's envelope literals (and renderKind's clauses).
func (s *extraScanner) scanFunc(fd *ast.FuncDecl) {
	if fd.Body == nil {
		return
	}
	if (siteFunc{s.file, fd.Name.Name}) == renderKindFunc {
		s.sawRender = true
		s.readRenderKind(fd)
	}
	// The variables assigned a renderKind result: their Extra is renderKind's.
	rendered := map[types.Object]bool{}
	// spec variables → the kind of `spec := kindSpecOf(errkind.X)`.
	specs := map[types.Object]errkind.Kind{}
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		as, ok := n.(*ast.AssignStmt)
		if !ok || len(as.Rhs) != 1 {
			return true
		}
		call, ok := as.Rhs[0].(*ast.CallExpr)
		if !ok {
			return true
		}
		switch calleeName(call) {
		case "renderKind":
			if len(as.Lhs) == 2 {
				if id, ok := as.Lhs[1].(*ast.Ident); ok {
					rendered[s.info.ObjectOf(id)] = true
				}
			}
		case "kindSpecOf":
			if id, ok := as.Lhs[0].(*ast.Ident); ok && len(call.Args) == 1 {
				if k, ok := s.kindOf(call.Args[0]); ok {
					specs[s.info.ObjectOf(id)] = k
				}
			}
		}
		return true
	})
	// The envelope literal handed to writeKindAnswer → its spec argument;
	// every `Extra: v` of a variable, by variable.
	kindAnswer := map[*ast.CompositeLit]ast.Expr{}
	sinks := map[types.Object][]*ast.Ident{}
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		switch x := n.(type) {
		case *ast.CallExpr:
			if calleeName(x) == "writeKindAnswer" && len(x.Args) == 5 {
				if lit, ok := x.Args[4].(*ast.CompositeLit); ok {
					kindAnswer[lit] = x.Args[2]
				}
			}
		case *ast.CompositeLit:
			if x.Type != nil && isEnvelopeType(x.Type) {
				if id, ok := extraOf(x).(*ast.Ident); ok {
					sinks[s.info.ObjectOf(id)] = append(sinks[s.info.ObjectOf(id)], id)
				}
			}
		case *ast.AssignStmt:
			for _, l := range x.Lhs {
				target := l
				if ix, ok := l.(*ast.IndexExpr); ok {
					target = ix.X
				}
				if sel, ok := target.(*ast.SelectorExpr); ok && sel.Sel.Name == "Extra" {
					s.fail(sel.Pos(), "Extra is set on an envelope after it was built; set it in the ErrorResponse literal")
				}
			}
		}
		return true
	})
	read := map[types.Object]bool{}
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		lit, ok := n.(*ast.CompositeLit)
		if !ok || lit.Type == nil || !isEnvelopeType(lit.Type) {
			return true
		}
		extra := extraOf(lit)
		if extra == nil {
			return true
		}
		if id, ok := extra.(*ast.Ident); ok && rendered[s.info.ObjectOf(id)] {
			return true // renderKind's, read there
		}
		var code errcode.Code
		var kind errkind.Kind
		if v, ok := codeField(lit); ok {
			c, ok := s.codeOf(v)
			if !ok {
				s.fail(v.Pos(), "an envelope with Extra whose Code is not an errcode constant")
				return true
			}
			code = c
		} else if spec, ok := kindAnswer[lit]; ok {
			known := false
			if id, isID := spec.(*ast.Ident); isID {
				kind, known = specs[s.info.ObjectOf(id)]
			}
			if !known {
				s.fail(spec.Pos(), "writeKindAnswer with Extra: its spec is not a `kindSpecOf(errkind.X)` variable")
				return true
			}
		} else {
			s.fail(lit.Pos(), "an envelope with Extra whose code cannot be read (no Code, not handed to writeKindAnswer)")
			return true
		}
		switch x := extra.(type) {
		case *ast.CompositeLit:
			if !isAnyMap(x.Type) {
				s.fail(x.Pos(), "an Extra literal that is not a map[string]any")
				return true
			}
			s.add(code, kind, s.litKeys(x), x.Pos())
		case *ast.Ident:
			v := s.info.ObjectOf(x)
			if _, isVar := v.(*types.Var); !isVar || read[v] {
				if !isVar {
					s.fail(x.Pos(), "Extra %s is not a local variable", x.Name)
				}
				return true
			}
			read[v] = true
			keys, ok := s.varKeys(fd.Body, v, sinks[v])
			if !ok {
				s.fail(x.Pos(), "Extra variable %s is never assigned a map[string]any literal", x.Name)
				return true
			}
			s.add(code, kind, keys, x.Pos())
		default:
			s.fail(extra.Pos(), "an Extra that is neither a map[string]any literal nor a variable holding one")
		}
		return true
	})
}

// extraOf is lit's Extra: value, nil when it sets none.
func extraOf(lit *ast.CompositeLit) ast.Expr {
	for _, el := range lit.Elts {
		if kv, ok := el.(*ast.KeyValueExpr); ok {
			if k, ok := kv.Key.(*ast.Ident); ok && k.Name == "Extra" {
				return kv.Value
			}
		}
	}
	return nil
}

// readRenderKind reads renderKind's switch: per case clause, the keys of the
// Extra it returns, for each of the clause's kinds. Every return outside the
// switch, and in a clause without kinds, must return a nil Extra.
func (s *extraScanner) readRenderKind(fd *ast.FuncDecl) {
	var sw *ast.SwitchStmt
	for _, st := range fd.Body.List {
		if x, ok := st.(*ast.SwitchStmt); ok {
			if sw != nil {
				s.fail(x.Pos(), "renderKind has more than one top-level switch")
				return
			}
			sw = x
		}
	}
	if sw == nil {
		s.fail(fd.Pos(), "renderKind has no top-level switch on the kind")
		return
	}
	// Returns outside the switch.
	for _, st := range fd.Body.List {
		if st == sw {
			continue
		}
		ast.Inspect(st, func(n ast.Node) bool {
			if r, ok := n.(*ast.ReturnStmt); ok {
				s.requireNilExtra(r)
			}
			return true
		})
	}
	for _, c := range sw.Body.List {
		cc := c.(*ast.CaseClause)
		var kinds []errkind.Kind
		for _, e := range cc.List {
			k, ok := s.kindOf(e)
			if !ok {
				s.fail(e.Pos(), "a renderKind case that is not an errkind constant")
				continue
			}
			kinds = append(kinds, k)
		}
		clause := &ast.BlockStmt{List: cc.Body}
		// The clause's Extra variables: those assigned a map literal in it,
		// and the returns that hand each over.
		vars := map[types.Object]bool{}
		ast.Inspect(clause, func(n ast.Node) bool {
			if as, ok := n.(*ast.AssignStmt); ok && len(as.Lhs) == len(as.Rhs) {
				for i, l := range as.Lhs {
					if id, ok := l.(*ast.Ident); ok {
						if _, ok := anyMapLit(as.Rhs[i]); ok {
							vars[s.info.ObjectOf(id)] = true
						}
					}
				}
			}
			return true
		})
		sinks := map[types.Object][]*ast.Ident{}
		var keys []keyVal
		ast.Inspect(clause, func(n ast.Node) bool {
			r, ok := n.(*ast.ReturnStmt)
			if !ok {
				return true
			}
			if len(kinds) == 0 {
				s.requireNilExtra(r)
				return true
			}
			if len(r.Results) != 2 {
				s.fail(r.Pos(), "a renderKind return without (text, extra)")
				return true
			}
			switch x := r.Results[1].(type) {
			case *ast.Ident:
				if x.Name == "nil" {
					return true
				}
				v := s.info.ObjectOf(x)
				if !vars[v] {
					s.fail(x.Pos(), "renderKind returns Extra %s, which this clause does not assign a map[string]any literal", x.Name)
					return true
				}
				sinks[v] = append(sinks[v], x)
			case *ast.CompositeLit:
				if !isAnyMap(x.Type) {
					s.fail(x.Pos(), "renderKind returns an Extra literal that is not a map[string]any")
					return true
				}
				keys = append(keys, s.litKeys(x)...)
			default:
				s.fail(x.Pos(), "renderKind returns an Extra that is neither nil, a map[string]any literal nor a variable holding one")
			}
			return true
		})
		for v := range vars {
			k, _ := s.varKeys(clause, v, sinks[v])
			keys = append(keys, k...)
		}
		for _, k := range kinds {
			s.add("", k, keys, cc.Pos())
		}
	}
}

func (s *extraScanner) requireNilExtra(r *ast.ReturnStmt) {
	if len(r.Results) == 2 {
		if id, ok := r.Results[1].(*ast.Ident); ok && id.Name == "nil" {
			return
		}
	}
	s.fail(r.Pos(), "a renderKind return outside a kind's case must return a nil Extra")
}

// scanExtra type-checks every package under internal/ and cmd/ of the module
// at modRoot but internal/errcode and this command, and returns the Extra
// fields their non-test files set, sorted by site and field.
func scanExtra(modRoot string) ([]extraUse, error) {
	codes, err := errcodeNames(modRoot)
	if err != nil {
		return nil, err
	}
	kindNames, err := constNames(modRoot, "internal/errkind", "Kind")
	if err != nil {
		return nil, err
	}
	kinds := map[string]errkind.Kind{}
	for n, v := range kindNames {
		kinds[n] = errkind.Kind(v)
	}
	fset := token.NewFileSet()
	pkgs, err := packages.Load(&packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax | packages.NeedTypes | packages.NeedTypesInfo,
		Dir:  modRoot,
		Fset: fset,
	}, "./internal/...", "./cmd/...")
	if err != nil {
		return nil, err
	}
	s := &extraScanner{fset: fset, codes: codes, kinds: kinds}
	for _, p := range pkgs {
		for _, e := range p.Errors {
			s.errs = append(s.errs, "type-check: "+e.Error())
		}
		if p.PkgPath == errcodePath || strings.HasSuffix(p.PkgPath, "/cmd/gen-error-contract") {
			continue
		}
		s.info = p.TypesInfo
		for _, af := range p.Syntax {
			abs := fset.Position(af.Pos()).Filename
			rel, err := filepath.Rel(modRoot, abs)
			if err != nil {
				return nil, err
			}
			s.file, s.codeLocal, s.kindLocal = filepath.ToSlash(rel), "", ""
			for _, im := range af.Imports {
				ip, _ := strconv.Unquote(im.Path.Value)
				local := ip[strings.LastIndex(ip, "/")+1:]
				if im.Name != nil {
					local = im.Name.Name
				}
				switch ip {
				case errcodePath:
					s.codeLocal = local
				case errkindPath:
					s.kindLocal = local
				}
			}
			for _, d := range af.Decls {
				if fd, ok := d.(*ast.FuncDecl); ok {
					s.scanFunc(fd)
				}
			}
		}
	}
	if !s.sawRender && len(s.errs) == 0 {
		s.errs = append(s.errs, fmt.Sprintf("%s in %s no longer exists; update the Extra scan", renderKindFunc.fn, renderKindFunc.file))
	}
	if len(s.errs) > 0 {
		slices.Sort(s.errs)
		return nil, errors.New("the Extra fields the handlers set cannot all be read:\n  " + strings.Join(s.errs, "\n  "))
	}
	slices.SortFunc(s.uses, func(a, b extraUse) int {
		if c := strings.Compare(a.Site, b.Site); c != 0 {
			return c
		}
		return strings.Compare(a.Field, b.Field)
	})
	return s.uses, nil
}

// constNames reads the constants of type typeName in the package directory
// rel of the module at modRoot: Go name → value.
func constNames(modRoot, rel, typeName string) (map[string]string, error) {
	files, err := filepath.Glob(filepath.Join(modRoot, filepath.FromSlash(rel), "*.go"))
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
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
				if id, ok := vs.Type.(*ast.Ident); !ok || id.Name != typeName {
					continue
				}
				for i, n := range vs.Names {
					v, ok := stringLit(vs.Values[i])
					if !ok {
						return nil, fmt.Errorf("%s.%s is not a string literal", path.Base(rel), n.Name)
					}
					out[n.Name] = v
				}
			}
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("read no %s constants in %s", typeName, rel)
	}
	return out, nil
}
