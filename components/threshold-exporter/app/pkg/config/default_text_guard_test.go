package config

// Guard for #3638: no site may recover structure from the text of
// ScheduledValue.Default except structureOf.
//
// ScheduledValue.Default holds a structured (mapping / sequence) value as a
// re-serialised YAML string. Stage 1 froze the readers that got the
// structure back from that text; stage 2 moved each onto structureOf
// (silent_mode, state_value, metadata, custom_alert, routing) and the frozen
// list is now empty. What remains is the one decoder and the listed matches
// that are not about structure.
//
// The test parses every non-test .go file of package config with go/parser
// (syntax only — no go/types: golang.org/x/tools is not a dependency of this
// module) and flags two shapes:
//
//	(A) "redecode": a call yaml.Unmarshal(X, ...) whose first argument is a
//	    conversion []byte(E), for any E; or yaml.NewDecoder(strings.NewReader(E)).
//	    Converting to []byte at the call site means E is string-typed, i.e. a
//	    string is being decoded as YAML. Whole-document decodes of data read
//	    from disk pass a []byte variable (data, b, f.data, ...) and are not
//	    matched — that is the syntactic rule that keeps them out, by design.
//	(B) "sniff": a call strings.Contains / strings.HasPrefix / strings.Index
//	    whose second argument is a string literal matching ^[A-Za-z_]*:$
//	    ("expires:", "target:", ":") — guessing a YAML key from text.
//
// "yaml" and "strings" are matched by the file's import of gopkg.in/yaml.v3
// and "strings" (alias-aware). A site is identified by (file, enclosing
// top-level func — "Recv.Method" for methods, "<file scope>" outside any
// func, shape), never by line number, so unrelated edits do not break it.
//
// The two lists below record how many sites each key has. The test fails when
//   - a key has MORE sites than recorded, or a key is not recorded at all:
//     a new site — decode from structure instead (see #3638);
//   - a key has FEWER sites than recorded (including zero): the entry is
//     stale and must be lowered / deleted.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

type defaultTextSite struct {
	file, fn, shape string
}

// defaultTextTheDecoder is the one site stage 2 converges on, and stays:
// structureOf decodes the text of a value ScheduledValue.structured marks
// as a mapping or sequence — never a scalar's (#3638). A reader asks it,
// not the text. Counted, with stale entries refused, as described above.
var defaultTextTheDecoder = map[defaultTextSite]int{
	{"structured_value.go", "structureOf", "redecode"}: 1,
}

// defaultTextNotStructure lists matches of the shapes that are NOT about the
// structure of Default, so stage 2 does not remove them. Same count/stale
// rules as defaultTextTheDecoder; keep it short and justify each entry.
var defaultTextNotStructure = map[defaultTextSite]int{
	// ":" on a scalar threshold string: detects the `<value>:<severity>`
	// suffix of `disable:critical` (#3042), not a YAML key.
	{"threshold_value.go", "thresholdRowDisabled", "sniff"}: 1,
}

var yamlKeySniffLiteral = regexp.MustCompile(`^[A-Za-z_]*:$`)

func TestDefaultTextGuard(t *testing.T) {
	got, err := scanDefaultTextSites(".")
	if err != nil {
		t.Fatal(err)
	}
	expected := map[defaultTextSite]int{}
	for _, m := range []map[defaultTextSite]int{defaultTextTheDecoder, defaultTextNotStructure} {
		for k, n := range m {
			expected[k] += n
		}
	}
	var problems []string
	for k, n := range got {
		want := expected[k]
		if n > want {
			problems = append(problems, fmt.Sprintf(
				"NEW %s site in %s func %s (%d found, %d recorded): do not recover structure "+
					"from the text of ScheduledValue.Default — decode from structure instead (#3638)",
				k.shape, k.file, k.fn, n, want))
		}
	}
	for k, want := range expected {
		if n := got[k]; n < want {
			problems = append(problems, fmt.Sprintf(
				"STALE entry {%q, %q, %q}: %d recorded, %d found — lower or delete it "+
					"(#3638)", k.file, k.fn, k.shape, want, n))
		}
	}
	sort.Strings(problems)
	for _, p := range problems {
		t.Error(p)
	}
}

func scanDefaultTextSites(dir string) (map[defaultTextSite]int, error) {
	paths, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	fset := token.NewFileSet()
	out := map[defaultTextSite]int{}
	scanned := 0
	for _, p := range paths {
		if strings.HasSuffix(p, "_test.go") {
			continue
		}
		src, err := os.ReadFile(p)
		if err != nil {
			return nil, err
		}
		f, err := parser.ParseFile(fset, p, src, parser.SkipObjectResolution)
		if err != nil {
			return nil, err
		}
		if f.Name.Name != "config" {
			continue
		}
		scanned++
		yamlName, stringsName := importNames(f)
		base := filepath.Base(p)
		record := func(fn, shape string) { out[defaultTextSite{base, fn, shape}]++ }
		visit := func(fn string, n ast.Node) {
			ast.Inspect(n, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				switch {
				case yamlName != "" && isPkgCall(call, yamlName, "Unmarshal") &&
					len(call.Args) >= 1 && isByteConversion(call.Args[0]):
					record(fn, "redecode")
				case yamlName != "" && stringsName != "" && isPkgCall(call, yamlName, "NewDecoder") &&
					len(call.Args) == 1 && isPkgCallExpr(call.Args[0], stringsName, "NewReader"):
					record(fn, "redecode")
				case stringsName != "" && (isPkgCall(call, stringsName, "Contains") ||
					isPkgCall(call, stringsName, "HasPrefix") || isPkgCall(call, stringsName, "Index")) &&
					len(call.Args) == 2 && isYAMLKeyLiteral(call.Args[1]):
					record(fn, "sniff")
				}
				return true
			})
		}
		for _, d := range f.Decls {
			if fd, ok := d.(*ast.FuncDecl); ok {
				visit(funcDeclName(fd), fd)
			} else {
				visit("<file scope>", d)
			}
		}
	}
	if scanned == 0 {
		// Fail closed: scanning nothing must not read as "no sites".
		return nil, fmt.Errorf("scanned no non-test files of package config in %q", dir)
	}
	return out, nil
}

func importNames(f *ast.File) (yamlName, stringsName string) {
	for _, imp := range f.Imports {
		path, _ := strconv.Unquote(imp.Path.Value)
		name := ""
		if imp.Name != nil {
			name = imp.Name.Name
		}
		switch path {
		case "gopkg.in/yaml.v3":
			if name == "" {
				name = "yaml"
			}
			yamlName = name
		case "strings":
			if name == "" {
				name = "strings"
			}
			stringsName = name
		}
	}
	return yamlName, stringsName
}

func isPkgCall(call *ast.CallExpr, pkg, fn string) bool {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != fn {
		return false
	}
	id, ok := sel.X.(*ast.Ident)
	return ok && id.Name == pkg
}

func isPkgCallExpr(e ast.Expr, pkg, fn string) bool {
	call, ok := e.(*ast.CallExpr)
	return ok && isPkgCall(call, pkg, fn)
}

// isByteConversion matches []byte(E).
func isByteConversion(e ast.Expr) bool {
	call, ok := e.(*ast.CallExpr)
	if !ok || len(call.Args) != 1 {
		return false
	}
	at, ok := call.Fun.(*ast.ArrayType)
	if !ok || at.Len != nil {
		return false
	}
	id, ok := at.Elt.(*ast.Ident)
	return ok && id.Name == "byte"
}

func isYAMLKeyLiteral(e ast.Expr) bool {
	lit, ok := e.(*ast.BasicLit)
	if !ok || lit.Kind != token.STRING {
		return false
	}
	s, err := strconv.Unquote(lit.Value)
	return err == nil && yamlKeySniffLiteral.MatchString(s)
}

func funcDeclName(fd *ast.FuncDecl) string {
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return fd.Name.Name
	}
	t := fd.Recv.List[0].Type
	if s, ok := t.(*ast.StarExpr); ok {
		t = s.X
	}
	if ix, ok := t.(*ast.IndexExpr); ok {
		t = ix.X
	}
	if id, ok := t.(*ast.Ident); ok {
		return id.Name + "." + fd.Name.Name
	}
	return fd.Name.Name
}
