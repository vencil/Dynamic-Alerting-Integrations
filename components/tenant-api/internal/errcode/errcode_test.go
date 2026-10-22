package errcode

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"
)

// constantsOf parses this package's source for every constant declared with
// type typeName: the list a table must cover, read from the source so a new
// constant cannot be forgotten in the table as well as here.
func constantsOf(t *testing.T, typeName string) []string {
	t.Helper()
	paths, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	var out []string
	for _, path := range paths {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				id, ok := vs.Type.(*ast.Ident)
				if !ok {
					// A constant without its type is not in swag's enum: say so
					// rather than skip it.
					if vs.Type == nil {
						t.Errorf("%s: constant %v is declared without a type; write `%v Code = …`",
							path, vs.Names, vs.Names)
					}
					continue
				}
				if id.Name != typeName {
					continue
				}
				for _, v := range vs.Values {
					lit, ok := v.(*ast.BasicLit)
					if !ok {
						t.Fatalf("%s constant %v is not a string literal", typeName, vs.Names)
					}
					s, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatal(err)
					}
					out = append(out, s)
				}
			}
		}
	}
	return out
}

// TestCodeTable_ListsEveryConstantOnce: the table and the declared Code
// constants are the same set — no constant without a remedy, no row for a
// string nobody declared (swag's enum is the constants, the contract is the
// table), no code twice — and every row's remedy is a declared one.
func TestCodeTable_ListsEveryConstantOnce(t *testing.T) {
	t.Parallel()
	declared := constantsOf(t, "Code")
	if len(declared) < 30 {
		t.Fatalf("read %d Code constants; the parser no longer finds them", len(declared))
	}
	shape := regexp.MustCompile(`^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$`)
	seen := map[string]bool{}
	for _, c := range declared {
		if seen[c] {
			t.Errorf("Code %q is declared twice", c)
		}
		seen[c] = true
		if !shape.MatchString(c) {
			t.Errorf("Code %q is not SCREAMING_SNAKE_CASE", c)
		}
	}
	remedies := map[Remedy]bool{}
	for _, r := range Remedies() {
		remedies[r] = true
	}
	rows := map[Code]int{}
	for _, s := range codeTable {
		rows[s.Code]++
		if !seen[string(s.Code)] {
			t.Errorf("codeTable row %q is not a declared Code constant", s.Code)
		}
		if !remedies[s.Remedy] {
			t.Errorf("codeTable row %q: remedy %q is not one of Remedies()", s.Code, s.Remedy)
		}
	}
	for _, c := range declared {
		if rows[Code(c)] != 1 {
			t.Errorf("Code %q has %d codeTable rows, want 1", c, rows[Code(c)])
		}
	}
}

// TestRemedies_ListsEveryConstantOnce: Remedies() is the declared Remedy
// constants, each once — the closed set a client maps.
func TestRemedies_ListsEveryConstantOnce(t *testing.T) {
	t.Parallel()
	declared := constantsOf(t, "Remedy")
	var listed []string
	for _, r := range Remedies() {
		listed = append(listed, string(r))
	}
	if !slices.Equal(declared, listed) {
		t.Errorf("Remedies() = %v, the declared constants are %v", listed, declared)
	}
}

// TestSpecs_IsACopy: the exported listing cannot edit the table.
func TestSpecs_IsACopy(t *testing.T) {
	t.Parallel()
	specs := Specs()
	specs[0].Remedy = ""
	if codeTable[0].Remedy == "" {
		t.Fatal("Specs returned the table itself")
	}
	if _, ok := Lookup(codeTable[0].Code); !ok {
		t.Fatal("Lookup misses the first row")
	}
	if _, ok := Lookup("NO_SUCH_CODE"); ok {
		t.Fatal("Lookup found a code nobody declared")
	}
}
