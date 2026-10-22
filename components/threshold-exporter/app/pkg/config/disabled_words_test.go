package config

// #3262: isDisabled is a switch over constants (it runs for every value on
// every scrape), and that switch is the one definition of the disabled
// vocabulary. Tests that enumerate the words read them from the switch here
// instead of keeping a second list that could drift from it.
//
// What catches what:
//   - a word added to or removed from the switch: the list read here changes,
//     so TestDisabledWordsUnchanged and TestSilentModeSchemaEnumsMatchReader
//     go red (FuzzIsDisabled too when the word is one of its seeds);
//   - a word accepted OUTSIDE the switch (an extra `if`, a second switch, a
//     different return): the list read here would not change, so instead
//     disabledWordsFromSource refuses any isDisabled body that is not exactly
//     `switch lower { case ...: return true }; return false` — every test
//     that enumerates the words then fails. The only other net is
//     FuzzIsDisabled's fixed seeds ("no", "nil", "null", "0", "true", ...),
//     which catch just those words; the root package's TestIsDisabled lists
//     "enable", "warning", "80", "", "true", "on" and misses e.g. "no".

import (
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"runtime"
	"strconv"
	"sync"
	"testing"
)

// disabledWordsSource is the file isDisabled is read from: parse.go next to
// this test file, located through the compiled-in source path.
var disabledWordsSource = func() string {
	_, self, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(self), "parse.go")
}()

// readDisabledWords reads isDisabled's case values once, on first use — not
// at init: a binary re-run as another user (testutil.RunUnprivileged), built
// with -trimpath, or moved away from its source cannot read parse.go, and
// that must fail only the tests that need the words, not the whole binary.
var readDisabledWords = sync.OnceValues(func() ([]string, error) {
	return disabledWordsFromSource(disabledWordsSource)
})

// disabledWords returns isDisabled's case values in source order, failing t
// when they cannot be read. Never a skip: a test that cannot see the
// vocabulary has checked nothing.
func disabledWords(t testing.TB) []string {
	t.Helper()
	words, err := readDisabledWords()
	if err != nil {
		t.Fatalf("isDisabled's words: %v", err)
	}
	return words
}

// disabledWordsFromSource parses path and returns isDisabled's case values.
// It refuses an isDisabled that is not exactly one switch on its parameter
// with a single `return true` case clause followed by `return false`, and a
// case that is neither a string literal nor a string constant declared in
// the same file: the list must be complete or not exist.
func disabledWordsFromSource(path string) ([]string, error) {
	f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		return nil, fmt.Errorf("cannot read the parse.go source (built with -trimpath, or the test binary moved away from its source?): %w", err)
	}
	consts := map[string]string{}
	var fn *ast.FuncDecl
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.GenDecl:
			if d.Tok != token.CONST {
				continue
			}
			for _, sp := range d.Specs {
				vs := sp.(*ast.ValueSpec)
				for i, name := range vs.Names {
					if i < len(vs.Values) {
						if lit, ok := vs.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
							consts[name.Name], _ = strconv.Unquote(lit.Value)
						}
					}
				}
			}
		case *ast.FuncDecl:
			if d.Name.Name == "isDisabled" && d.Recv == nil {
				fn = d
			}
		}
	}
	if fn == nil {
		return nil, errors.New(path + ": no func isDisabled")
	}
	shape := errors.New("isDisabled is not `switch <param> { case ...: return true }; return false` — words accepted outside the switch would not be seen")
	params := fn.Type.Params.List
	if len(params) != 1 || len(params[0].Names) != 1 || fn.Body == nil || len(fn.Body.List) != 2 {
		return nil, shape
	}
	sw, ok := fn.Body.List[0].(*ast.SwitchStmt)
	if !ok || sw.Init != nil || !isIdent(sw.Tag, params[0].Names[0].Name) || len(sw.Body.List) != 1 {
		return nil, shape
	}
	if !isReturnOf(fn.Body.List[1], "false") {
		return nil, shape
	}
	cc := sw.Body.List[0].(*ast.CaseClause)
	if len(cc.List) == 0 || len(cc.Body) != 1 || !isReturnOf(cc.Body[0], "true") {
		return nil, shape
	}
	var words []string
	for _, e := range cc.List {
		switch e := e.(type) {
		case *ast.BasicLit:
			w, err := strconv.Unquote(e.Value)
			if e.Kind != token.STRING || err != nil {
				return nil, fmt.Errorf("isDisabled: non-string case %s", e.Value)
			}
			words = append(words, w)
		case *ast.Ident:
			w, ok := consts[e.Name]
			if !ok {
				return nil, fmt.Errorf("isDisabled: case %s is not a string constant in %s", e.Name, path)
			}
			words = append(words, w)
		default:
			return nil, errors.New("isDisabled: a case is not a string literal or constant")
		}
	}
	return words, nil
}

func isIdent(e ast.Expr, name string) bool {
	id, ok := e.(*ast.Ident)
	return ok && id.Name == name
}

func isReturnOf(s ast.Stmt, ident string) bool {
	r, ok := s.(*ast.ReturnStmt)
	return ok && len(r.Results) == 1 && isIdent(r.Results[0], ident)
}
