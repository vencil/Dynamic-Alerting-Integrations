package config

// #3262: isDisabled is a switch over constants (it runs for every value on
// every scrape), and that switch is the one definition of the disabled
// vocabulary. Tests that enumerate the words read them from the switch here
// instead of keeping a second list that could drift from it.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"runtime"
	"strconv"
	"sync"
)

// disabledWords returns isDisabled's case values, in source order, read from
// parse.go next to this file. A case that is neither a string literal nor a
// string constant declared in parse.go panics: the list must be complete or
// not exist. Read on first use, not at init: a test binary re-run as another
// user (testutil.RunUnprivileged) may not be able to read the source.
var disabledWords = sync.OnceValue(func() []string {
	_, self, _, _ := runtime.Caller(0)
	return disabledWordsFromSource(filepath.Join(filepath.Dir(self), "parse.go"))
})

func disabledWordsFromSource(path string) []string {
	f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		panic(err)
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
		panic(path + ": no func isDisabled")
	}
	var words []string
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		cc, ok := n.(*ast.CaseClause)
		if !ok {
			return true
		}
		for _, e := range cc.List {
			switch e := e.(type) {
			case *ast.BasicLit:
				w, err := strconv.Unquote(e.Value)
				if e.Kind != token.STRING || err != nil {
					panic("isDisabled: non-string case " + e.Value)
				}
				words = append(words, w)
			case *ast.Ident:
				w, ok := consts[e.Name]
				if !ok {
					panic("isDisabled: case " + e.Name + " is not a string constant in " + path)
				}
				words = append(words, w)
			default:
				panic("isDisabled: case is not a string literal or constant")
			}
		}
		return true
	})
	if len(words) == 0 {
		panic("isDisabled: no case values found")
	}
	return words
}
