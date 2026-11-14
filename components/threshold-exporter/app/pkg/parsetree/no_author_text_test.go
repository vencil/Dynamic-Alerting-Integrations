package parsetree

// A refusal's message carries no text the file's author wrote (#3703): every
// reader prints it — the route generator's stdout, stderr and findings JSON
// among them — and a key, a value, a tag or an anchor name is not theirs to
// repeat. The canary matrix puts one marker in every place an author writes
// text (a key, a tag, an anchor and its alias, a scalar value) for every Kind
// a file's bytes can produce, one fixture per refusal path where a Kind has
// several, and holds that each fixture gets its Kind (the marker sat where
// the refusal looked) and that its message holds no marker.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const canary = "ZQXCANARY"

// callerKinds are set by the caller's walk (a stat, a read, a directory),
// never from a file's bytes: Parse cannot produce them, so they have no
// fixture here. Their messages are the caller's (cmd/da-guard).
var callerKinds = map[Kind]bool{KindReadFailure: true, KindNotRegularFile: true}

type canaryFixture struct {
	name string // the refusal path
	kind Kind
	data string
}

// canaryFixtures: every Kind but callerKinds, with the marker C in each
// place the file's author controls that the shape allows.
func canaryFixtures() []canaryFixture {
	const C = canary
	join := func(item string, n int) string { return strings.TrimSuffix(strings.Repeat(item+",", n), ",") }
	laughs := func() string {
		// yaml.v3's own alias limit: each level doubles what the last one
		// holds, 9 times over, in a file far under the content bound.
		var b strings.Builder
		b.WriteString(C + "0: &" + C + "0 [" + C + "]\n")
		for i := 1; i < 10; i++ {
			prev := C + string(rune('0'+i-1))
			b.WriteString(C + string(rune('0'+i)) + ": &" + C + string(rune('0'+i)) + " [" + join("*"+prev, 9) + "]\n")
		}
		return b.String()
	}
	return []canaryFixture{
		{"invalid UTF-8", KindInvalidUTF8, C + ": " + C + "\n" + C + ": \xff\n"},
		{"BOM after the start", KindBOMNotAtStart, C + ": &" + C + " " + C + "\uFEFF\n"},
		{"parser: unclosed flow", KindSyntaxError, C + ": [" + C + "\n"},
		{"parser: unknown anchor", KindSyntaxError, C + ": *" + C + "\n"},
		{"parser: %YAML directive refused", KindSyntaxError, "%YAML 2.0\n---\n" + C + ": " + C + "\n"},
		{"parser: nesting deeper than the limit", KindSyntaxError, C + ": " + strings.Repeat("[", 10_001) + C + "\n"},
		{"parser: after the first document", KindSyntaxError, C + ": 1\n---\n" + C + ": [" + C + "\n"},
		{"multiple documents", KindMultipleDocuments, C + ": &" + C + " " + C + "\n---\n" + C + ": 2\n"},
		{"non-specific tag", KindNonSpecificTag, C + ": ! " + C + "\n"},
		{"tag on a scalar", KindTagNotAllowed, C + ": !" + C + " " + C + "\n"},
		{"tag on a mapping", KindTagNotAllowed, C + ": !" + C + " {" + C + ": 1}\n"},
		{"tag on a sequence", KindTagNotAllowed, C + ": !" + C + " [" + C + "]\n"},
		{"plain << as a value", KindTagNotAllowed, C + ": <<\n"},
		{"alias to a plain <<", KindTagNotAllowed, C + ":\n  &" + C + " <<: {" + C + ": 1}\n" + C + "2: [*" + C + "]\n"},
		{"non-scalar key", KindNonScalarKey, "? [" + C + ", &" + C + " " + C + "]\n: " + C + "\n"},
		{"null key", KindNullKey, C + ":\n  ~: " + C + "\n"},
		{"duplicate key", KindDuplicateKey, C + ": 1\n" + C + ": 2\n"},
		{"duplicate key through an alias", KindDuplicateKey, "&" + C + " " + C + ": 1\n*" + C + " : 2\n"},
		{"scalar does not decode as its tag", KindDecodeError, C + ": !!int " + C + "\n"},
		{"null tag on a key's text", KindDecodeError, "!!null " + C + ": 1\n"},
		{"merge value not a mapping", KindDecodeError, C + ":\n  <<: &" + C + " 1\n"},
		{"merge sequence item not a mapping", KindDecodeError, C + ":\n  <<: [&" + C + " 1]\n"},
		{"alias that contains itself", KindDecodeError, C + ": &" + C + " [*" + C + "]\n"},
		{"excessive aliasing", KindDecodeError, laughs()},
		{"content over the byte bound", KindDecodeError, C + ": &" + C + " \"" + C + strings.Repeat("a", 100_000) + "\"\nl: [" + join("*"+C, 300) + "]\n"},
		{"top level not a mapping", KindTopLevelNotMapping, "- &" + C + " " + C + "\n- *" + C + "\n"},
	}
}

func TestNoAuthorTextInAnyMessage(t *testing.T) {
	t.Parallel()
	fixtures := canaryFixtures()
	covered := map[Kind]bool{}
	for _, f := range fixtures {
		covered[f.kind] = true
		if !strings.Contains(f.data, canary) {
			t.Errorf("%s: the fixture holds no canary", f.name)
		}
		_, bad := Parse([]byte(f.data))
		if bad == nil || bad.Kind != f.kind {
			t.Errorf("%s: got %v, want %s — the canary is not where this refusal looks", f.name, bad, f.kind)
			continue
		}
		if strings.Contains(bad.Message(), canary) {
			t.Errorf("%s: the message carries author text: %q", f.name, bad.Message())
		}
		if j := Judge([]byte(f.data)); j == nil || *j != *bad {
			t.Errorf("%s: Judge %v, Parse %v", f.name, j, bad)
		}
	}
	// Every Kind a file's bytes can produce has a fixture: a Kind added to
	// Kinds() without one fails here rather than going unchecked.
	for _, k := range Kinds() {
		if callerKinds[k] {
			continue
		}
		if !covered[k] {
			t.Errorf("kind %s has no canary fixture", k)
		}
	}
	for k := range callerKinds {
		if !contains(Kinds(), k) {
			t.Errorf("callerKinds names %s, which Kinds() does not list", k)
		}
	}
}

func contains(ks []Kind, k Kind) bool {
	for _, x := range ks {
		if x == k {
			return true
		}
	}
	return false
}

// TestSentencesAreConstants: a refusal's text is made of constants only, held
// by the package's syntax as a closed world (every non-test file):
//
//  1. The name sentence appears only in its own package-level declaration
//     `type sentence string` and as a parameter or result type of a function
//     declaration. The type alone holds a string variable off (a string does
//     not convert to a sentence implicitly); this rule holds off every way of
//     naming the type to convert to it — `sentence(x)`, `(sentence)(x)`, an
//     alias or a derived type, a variable's type, a type argument, a literal.
//  2. Exactly one composite literal of Unreadable, inside fail's body.
//  3. No assignment to a field named message.
//
// The rules are not trusted to be right because the package happens to pass
// them: TestNoAuthorTextRulesCatchTheBypasses feeds each a source that breaks
// it and one that does not.
func TestSentencesAreConstants(t *testing.T) {
	t.Parallel()
	names, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{}
	for _, name := range names {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		files[name] = string(src)
	}
	if len(files) == 0 {
		t.Fatal("no non-test Go files")
	}
	for _, v := range noAuthorTextViolations(t, files) {
		t.Error(v)
	}
}

// noAuthorTextViolations runs the three rules over files (name → source), the
// whole of a package, and returns one line per violation, positioned.
func noAuthorTextViolations(t *testing.T, files map[string]string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var parsed []*ast.File
	for name, src := range files {
		f, err := parser.ParseFile(fset, name, src, 0)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		parsed = append(parsed, f)
	}
	var out []string
	out = append(out, sentenceNameViolations(fset, parsed)...)
	out = append(out, unreadableLiteralViolations(fset, parsed)...)
	out = append(out, messageAssignViolations(fset, parsed)...)
	return out
}

// sentenceNameViolations is rule 1: every *ast.Ident named sentence that is
// neither the name of the package-level non-alias TypeSpec nor the whole type
// of a parameter or result field of a FuncDecl.
func sentenceNameViolations(fset *token.FileSet, files []*ast.File) []string {
	const name = "sentence"
	allowed := map[*ast.Ident]bool{}
	for _, f := range files {
		for _, d := range f.Decls {
			switch d := d.(type) {
			case *ast.GenDecl:
				for _, s := range d.Specs {
					if ts, ok := s.(*ast.TypeSpec); ok && ts.Name.Name == name && !ts.Assign.IsValid() && ts.TypeParams == nil {
						if id, ok := ts.Type.(*ast.Ident); ok && id.Name == "string" {
							allowed[ts.Name] = true
						}
					}
				}
			case *ast.FuncDecl:
				for _, fl := range []*ast.FieldList{d.Type.Params, d.Type.Results} {
					if fl == nil {
						continue
					}
					for _, field := range fl.List {
						if id, ok := field.Type.(*ast.Ident); ok && id.Name == name {
							allowed[id] = true
						}
					}
				}
			}
		}
	}
	var out []string
	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			if id, ok := n.(*ast.Ident); ok && id.Name == name && !allowed[id] {
				out = append(out, fmt.Sprintf("%s: the name %s outside its declaration and function signatures; "+
					"a refusal's text must be a constant", fset.Position(id.Pos()), name))
			}
			return true
		})
	}
	return out
}

// unreadableLiteralViolations is rule 2: exactly one composite literal whose
// type is Unreadable, and it lies in the body of the function fail.
func unreadableLiteralViolations(fset *token.FileSet, files []*ast.File) []string {
	var out []string
	count := 0
	for _, f := range files {
		var failBody *ast.BlockStmt
		for _, d := range f.Decls {
			if fd, ok := d.(*ast.FuncDecl); ok && fd.Recv == nil && fd.Name.Name == "fail" {
				failBody = fd.Body
			}
		}
		ast.Inspect(f, func(n ast.Node) bool {
			lit, ok := n.(*ast.CompositeLit)
			if !ok {
				return true
			}
			if id, ok := lit.Type.(*ast.Ident); !ok || id.Name != "Unreadable" {
				return true
			}
			count++
			if failBody == nil || lit.Pos() < failBody.Pos() || lit.End() > failBody.End() {
				out = append(out, fmt.Sprintf("%s: an Unreadable literal outside fail; fail is its one constructor",
					fset.Position(lit.Pos())))
			}
			return true
		})
	}
	if count != 1 {
		out = append(out, fmt.Sprintf("%d Unreadable literals in the package, want exactly 1 (fail's)", count))
	}
	return out
}

// messageAssignViolations is rule 3: no assignment (=, +=, …) whose left side
// is a selector of a field named message.
func messageAssignViolations(fset *token.FileSet, files []*ast.File) []string {
	var out []string
	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			as, ok := n.(*ast.AssignStmt)
			if !ok {
				return true
			}
			for _, lhs := range as.Lhs {
				if sel, ok := lhs.(*ast.SelectorExpr); ok && sel.Sel.Name == "message" {
					out = append(out, fmt.Sprintf("%s: an assignment to message; only fail sets it",
						fset.Position(as.Pos())))
				}
			}
			return true
		})
	}
	return out
}

// TestNoAuthorTextRulesCatchTheBypasses: each rule fails on a source that
// breaks it — among them the three ways a blind review got author text past
// the earlier check (a parenthesised conversion, an alias, a literal outside
// fail) — and passes a source that keeps all three.
func TestNoAuthorTextRulesCatchTheBypasses(t *testing.T) {
	t.Parallel()
	const head = "package p\n\ntype Kind string\n\ntype Unreadable struct {\n\tKind    Kind\n\tmessage string\n}\n\n" +
		"type sentence string\n\nfunc fail(k Kind, s sentence) *Unreadable {\n\treturn &Unreadable{Kind: k, message: string(s)}\n}\n\n" +
		"func kindName(b bool) sentence {\n\tif b {\n\t\treturn \"a mapping\"\n\t}\n\treturn \"a scalar\"\n}\n\n"
	for _, tc := range []struct {
		name string
		body string
		want string // a substring of the one violation expected; "" for none
	}{
		{"legal: constants and a sentence-returning helper",
			"func f(v string, b bool) *Unreadable {\n\t_ = v\n\treturn fail(\"k\", \"refused: \"+kindName(b))\n}\n", ""},
		{"parenthesised conversion",
			"func f(v string) *Unreadable { return fail(\"k\", (sentence)(\"a \"+v)) }\n", "the name sentence"},
		{"plain conversion",
			"func f(v string) *Unreadable { return fail(\"k\", sentence(\"a \"+v)) }\n", "the name sentence"},
		{"type alias",
			"type sent2 = sentence\n\nfunc f(v string) *Unreadable { return fail(\"k\", sent2(\"a \"+v)) }\n", "the name sentence"},
		{"derived type",
			"type sent3 sentence\n\nfunc f(v string) *Unreadable { return fail(\"k\", sentence(sent3(v))) }\n", "the name sentence"},
		{"variable of the type",
			"func f(v string) *Unreadable {\n\tvar s sentence\n\t_ = v\n\treturn fail(\"k\", s)\n}\n", "the name sentence"},
		{"type argument",
			"func as[T ~string](s string) T { return T(s) }\n\nfunc f(v string) *Unreadable { return fail(\"k\", as[sentence](v)) }\n",
			"the name sentence"},
		{"function literal's parameter",
			"func f(v string) *Unreadable { return func(s sentence) *Unreadable { return fail(\"k\", s) }(\"x\") }\n",
			"the name sentence"},
		{"literal outside fail",
			"func f(v string) *Unreadable { return &Unreadable{Kind: \"k\", message: \"a \" + v} }\n", "outside fail"},
		{"assignment to message",
			"func f(v string) *Unreadable {\n\tu := fail(\"k\", \"x\")\n\tu.message = \"a \" + v\n\treturn u\n}\n", "assignment to message"},
		{"append to message",
			"func f(v string) *Unreadable {\n\tu := fail(\"k\", \"x\")\n\tu.message += v\n\treturn u\n}\n", "assignment to message"},
	} {
		got := noAuthorTextViolations(t, map[string]string{"p.go": head + tc.body})
		switch {
		case tc.want == "" && len(got) != 0:
			t.Errorf("%s: want no violation, got %q", tc.name, got)
		case tc.want != "" && (len(got) == 0 || !strings.Contains(strings.Join(got, "\n"), tc.want)):
			t.Errorf("%s: want a violation naming %q, got %q", tc.name, tc.want, got)
		}
	}
	// The count half of rule 2: no literal at all is as wrong as two.
	noLit := "package p\n\ntype Unreadable struct{ message string }\n\nfunc fail() *Unreadable { return new(Unreadable) }\n"
	if got := noAuthorTextViolations(t, map[string]string{"p.go": noLit}); len(got) != 1 || !strings.Contains(got[0], "0 Unreadable literals") {
		t.Errorf("no literal: got %q", got)
	}
	// The declaration itself must be the defined type: an alias of string
	// would make every string a sentence.
	aliasDecl := "package p\n\ntype Unreadable struct{ message string }\n\ntype sentence = string\n\n" +
		"func fail(s sentence) *Unreadable { return &Unreadable{message: s} }\n"
	if got := noAuthorTextViolations(t, map[string]string{"p.go": aliasDecl}); len(got) != 1 || !strings.Contains(got[0], "the name sentence") {
		t.Errorf("alias declaration: got %q", got)
	}
}

// lineOf reads the digits of yaml.v3's fixed `line N:` prefix and nothing
// else: an error that does not start that way gives no line.
func TestLineOfReadsOnlyTheFixedPrefix(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]int{
		"yaml: line 7: did not find expected node content": 7,
		"yaml: line 12: mapping values are not allowed":    12,
	} {
		if got, ok := lineOf(errString(in)); !ok || got != want {
			t.Errorf("%q: %d %v, want %d", in, got, ok, want)
		}
	}
	for _, in := range []string{
		"yaml: unknown anchor 'line 9:' referenced",
		"yaml: anchor 'x' value contains itself",
		"line 3: not yaml.v3's prefix",
		"yaml: line : no digits",
		"yaml: line 12345678901: too long",
	} {
		if got, ok := lineOf(errString(in)); ok {
			t.Errorf("%q: line %d, want none", in, got)
		}
	}
}

type errString string

func (e errString) Error() string { return string(e) }
