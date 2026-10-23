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

// TestExtra_Declarations (#3272): every envelope field is declared once with
// a known type (Items exactly for an array, Ref only for an object or an
// array of objects) and a Doc; extraFields is sorted by name; every code in
// codeExtra is a code, lists only declared fields, none twice; and every
// declared field is carried by some code. Whether a declared name collides
// with a fixed envelope field is the handler's to say (it owns
// ErrorResponse): TestErrorResponse_ExtraNotAFixedField and the contract
// generator.
func TestExtra_Declarations(t *testing.T) {
	t.Parallel()
	types := map[ExtraType]bool{}
	for _, ty := range ExtraTypes() {
		types[ty] = true
	}
	if got := constantsOf(t, "ExtraType"); len(got) != len(types) {
		t.Errorf("ExtraTypes() lists %d types, %d are declared", len(types), len(got))
	}
	name := regexp.MustCompile(`^[a-z][a-z0-9]*(_[a-z0-9]+)*$`)
	seen := map[string]bool{}
	var names []string
	for _, f := range extraFields {
		if seen[f.Name] {
			t.Errorf("extra field %q is declared twice", f.Name)
		}
		seen[f.Name] = true
		names = append(names, f.Name)
		if !name.MatchString(f.Name) {
			t.Errorf("extra field %q is not snake_case", f.Name)
		}
		if !types[f.Type] {
			t.Errorf("extra field %q: type %q is not one of ExtraTypes()", f.Name, f.Type)
		}
		if (f.Type == ExtraArray) != (f.Items != "") || (f.Items != "" && !types[f.Items]) {
			t.Errorf("extra field %q: type %q items %q — an array needs a known Items, nothing else has one", f.Name, f.Type, f.Items)
		}
		if f.Ref != "" && f.Type != ExtraObject && f.Items != ExtraObject {
			t.Errorf("extra field %q: Ref %q on a %s", f.Name, f.Ref, f.Type)
		}
		// The doc lands in the OpenAPI description and in swag's docs.go,
		// a Go raw string rendered as a text/template.
		if strings.TrimSpace(f.Doc) == "" || strings.ContainsAny(f.Doc, "`\n") || strings.Contains(f.Doc, "{{") {
			t.Errorf("extra field %q: Doc %q must be one non-empty line without a backtick or {{", f.Name, f.Doc)
		}
	}
	if !slices.IsSorted(names) {
		t.Errorf("extraFields is not sorted by name: %v", names)
	}
	carried := map[string]bool{}
	for c, fields := range codeExtra {
		if _, ok := Lookup(c); !ok {
			t.Errorf("codeExtra lists %q, which is not a code", c)
		}
		dup := map[string]bool{}
		for _, f := range fields {
			if !seen[f] {
				t.Errorf("code %s carries %q, which extraFields does not declare", c, f)
			}
			if dup[f] {
				t.Errorf("code %s lists %q twice", c, f)
			}
			dup[f] = true
			carried[f] = true
		}
	}
	for _, f := range names {
		if !carried[f] {
			t.Errorf("extra field %q is declared but no code carries it", f)
		}
	}
	if len(codeExtra[Internal]) != 0 {
		t.Errorf("INTERNAL_ERROR carries %v: its answer withholds everything but the request_id (#1700)", codeExtra[Internal])
	}
}

// TestCarries: what a code declares is carried, nothing else is, and an
// empty or unknown code carries nothing.
func TestCarries(t *testing.T) {
	t.Parallel()
	if !Carries(PendingPR, "pr_number") || Carries(PendingPR, "tenant_id") {
		t.Error("Carries disagrees with codeExtra for PENDING_PR_EXISTS")
	}
	for _, c := range []Code{"", "NO_SUCH_CODE", Internal} {
		if Carries(c, "message") || Carries(c, "error") {
			t.Errorf("code %q carries a field", c)
		}
	}
	specs := Specs()
	for i := range specs {
		if specs[i].Code == PendingPR {
			specs[i].Extra[0] = "mutated"
		}
	}
	if !Carries(PendingPR, "existing_pr_url") {
		t.Fatal("Specs returned codeExtra's own slice")
	}
	if _, ok := LookupExtraField("pr_number"); !ok {
		t.Error("LookupExtraField misses pr_number")
	}
	if _, ok := LookupExtraField("error"); ok {
		t.Error("LookupExtraField found a fixed field")
	}
}
