package config

// number_reader_guard_test.go — nothing in this package reads a text as a
// float except the functions listed here (#3457 review).
//
// The grammar of a threshold number is parseThresholdNumber's: the recipe
// contract's pattern, then strconv.ParseFloat, then finite. Tests of that
// function do not show that every path which serves a threshold calls it — a
// resolver with its own `strconv.ParseFloat` would serve `1_000` as 1000
// while every reader test stays green. cmd/da-guard's
// TestThresholdMatrix_EveryServingPath holds the paths that exist today to
// the matrix by what they serve; this test holds the next one, structurally:
// a float parser may be named only inside the functions of floatTextReaders,
// each of which says what it reads. Adding a reader means adding it here,
// with its reason, in the same change.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// floatParsers is the functions that read a text as a floating-point number,
// by import path.
var floatParsers = map[string][]string{
	"strconv":  {"ParseFloat", "ParseComplex"},
	"fmt":      {"Sscan", "Sscanf", "Sscanln", "Fscan", "Fscanf", "Fscanln"},
	"math/big": {"ParseFloat"},
}

// floatTextReaders is every function of this package that may name one of
// floatParsers, and what it reads.
var floatTextReaders = map[string]string{
	// The threshold number.
	"parseThresholdNumber": "THE reader of a threshold number: the contract's pattern first, then ParseFloat, then finite",
	"nonFiniteNumberText":  "classifies a text parseThresholdNumber already refused: does it name NaN or ±Inf",
	"ReadThresholdText":    "the NaN / ±Inf a refused text names or, for a text in the grammar, overflows to (da-guard threshold-values)",
	// Not thresholds: each is behind its own pattern of the recipe contract.
	"validateQuantile":     "a custom alert's quantile, after customAlertQuantileRe (patterns.decimal)",
	"validateSloObjective": "a custom alert's SLO objective, after the same decimal pattern",
	"resolveSloBurnRate":   "the SLO objective validateSloObjective accepted",
	"walkCustomAlertSpecs": "the SLO objective resolveSloBurnRate already read, echoed for the user_slo_objective gauge",
}

func TestOnlyTheListedFunctionsReadAFloatFromText(t *testing.T) {
	t.Parallel()
	_, self, _, _ := runtime.Caller(0)
	dir := filepath.Dir(self)
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("cannot list the package source (built with -trimpath, or moved away from it?): %v", err)
	}
	fset := token.NewFileSet()
	used := map[string]bool{}
	files := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, 0)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		files++
		// The local name each parser package is imported under in this file.
		local := map[string][]string{}
		for _, imp := range f.Imports {
			path, _ := strconv.Unquote(imp.Path.Value)
			funcs, watched := floatParsers[path]
			if !watched {
				continue
			}
			as := path[strings.LastIndex(path, "/")+1:]
			if imp.Name != nil {
				as = imp.Name.Name
			}
			if as == "." || as == "_" {
				t.Errorf("%s imports %q as %q: its float parsers could be named without the package", name, path, as)
				continue
			}
			local[as] = funcs
		}
		report := func(owner string, n ast.Node) {
			ast.Inspect(n, func(n ast.Node) bool {
				sel, ok := n.(*ast.SelectorExpr)
				if !ok {
					return true
				}
				pkg, ok := sel.X.(*ast.Ident)
				if !ok {
					return true
				}
				for _, fn := range local[pkg.Name] {
					if sel.Sel.Name != fn {
						continue
					}
					if _, listed := floatTextReaders[owner]; listed {
						used[owner] = true
						continue
					}
					t.Errorf("%s: %s names %s.%s. A threshold number is read by parseThresholdNumber alone; "+
						"if this reads something else, list the function in floatTextReaders with what it reads",
						fset.Position(sel.Pos()), owner, pkg.Name, fn)
				}
				return true
			})
		}
		for _, d := range f.Decls {
			if fn, ok := d.(*ast.FuncDecl); ok {
				report(fn.Name.Name, fn)
			} else {
				report("(package level of "+name+")", d)
			}
		}
	}
	if files < 20 {
		t.Fatalf("only %d source files read: the guard looked at too little", files)
	}
	var stale []string
	for fn := range floatTextReaders {
		if !used[fn] {
			stale = append(stale, fn)
		}
	}
	sort.Strings(stale)
	if len(stale) > 0 {
		t.Errorf("floatTextReaders lists %v, which no longer read a float from text: remove them", stale)
	}
}
