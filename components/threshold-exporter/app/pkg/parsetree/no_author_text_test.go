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
		if strings.Contains(bad.Message, canary) {
			t.Errorf("%s: the message carries author text: %q", f.name, bad.Message)
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

// TestSentencesAreConstants: a sentence is made of constants only. The type
// alone holds that much against a string variable (a string does not convert
// to a sentence implicitly); what it cannot hold is an explicit conversion
// `sentence(x)`, so no file of the package may write one.
func TestSentencesAreConstants(t *testing.T) {
	t.Parallel()
	files, err := filepath.Glob("*.go")
	if err != nil || len(files) == 0 {
		t.Fatalf("no Go files: %v", err)
	}
	fset := token.NewFileSet()
	for _, name := range files {
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		f, err := parser.ParseFile(fset, name, src, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if call, ok := n.(*ast.CallExpr); ok {
				if id, ok := call.Fun.(*ast.Ident); ok && id.Name == "sentence" {
					t.Errorf("%s: a conversion to sentence; a refusal's text must be a constant", fset.Position(call.Pos()))
				}
			}
			return true
		})
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
