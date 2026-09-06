package main

// go/ast tripwire for doc-comment ATTACHMENT (#1736, the unexported half).
//
// The defect: Go ends a doc comment at a blank line, so a paragraph that is
// not separated from the next one gets merged and attaches to the FOLLOWING
// declaration — while the declaration it actually describes ends up with no
// doc at all. gofmt, go vet, the compiler and the tests are all blind to it;
// #1730 shipped 3 such sites and #1738 another 10, one of them printing an
// uncallable name on the public godoc face.
//
// Why this is a test and not golangci: `godoclint` (enabled in .golangci.yml
// by #1743) implements the EXPORTED half — its start-with-name rule fires on
// whether the declaration HOLDING the comment is exported, so it is blind
// whenever the swallowing host is unexported, which is what all three #1730
// sites and nine of the ten #1738 sites looked like. Measured on the pre-fix
// trees, godoclint reported 1 of those 13. No golangci linter can express
// "the identifier this paragraph names is a DIFFERENT declaration in this
// package, and that declaration has no doc of its own" — that needs the
// package-wide symbol table this walk builds.
//
// SCOPE is components/** on purpose, not the whole repo. ci.yml's `go` path
// filter lists components/threshold-exporter/** and components/tenant-api/**,
// so what this test READS is inside what TRIGGERS it. ⚠️ That containment is
// accidental, not structural: components/ also holds da-portal, da-tools and
// recipe-preview, and it holds only because those three carry zero .go files
// today. Two of the repo's other Go modules — scripts/tools/ops/bench-canary
// and tests/e2e-bench/receiver — are outside the filter, so scanning them here
// would be a guard a PR can edit without waking (#1399 is that failure shape;
// #1751 tracks their lint coverage). The third, tests/alertmanager-inhibit,
// IS in the filter and has its own test leg — an earlier version of this
// comment wrongly lumped it in with the other two.
//
// ⚠️ THE FALSE-POSITIVE SURFACE IS BIGGER THAN TODAY'S ZERO. Measured: 37
// paragraphs under components/ already name a known declaration and are held
// green ONLY by that declaration happening to carry a doc of its own. Deleting
// or moving a doc in file A can therefore turn file B red, and for the common
// "file-header paragraph names this file's subject" shape the fix message
// below does not apply — such a header is already separated by a blank line,
// which is exactly why it is UNATTACHED. If that starts firing on people who
// did not write the paragraph, this guard is more likely to be deleted than
// fixed; prefer narrowing it (or retiring it) over adding another predicate
// layer.
//
// NOT COVERED. Three clauses bound this walk, and stating them as clauses
// beats listing the dozen shapes that fall outside — adversarial review found
// twelve, all of them consequences of these three:
//
//  1. It reads only the FIRST line of a comment group, and only when that line
//     opens with a bare identifier followed by a space. So a swallowed
//     paragraph is invisible whenever the first line is a directive, a TODO,
//     an article, prose in another language, `name()` with parens, or a
//     `Recv.Name` dotted form — and, most commonly, whenever the two
//     paragraphs are separated by an empty `//` line rather than a blank line,
//     because then they are ONE group and the second paragraph is never read.
//  2. Its symbol table holds only TOP-LEVEL bare names, with a
//     "does it already have a doc" flag. So it cannot see a swallowed struct
//     field or interface method, and it goes quiet whenever the swallowed
//     declaration happens to carry some doc of its own, or shares its bare
//     name with a documented sibling.
//  3. It has no name-history oracle, so a doc naming an identifier that no
//     longer exists (a rename leftover — cmd/da-batchpr/main.go's
//     `exitCodeForSummary` naming `exitCodeForApply`, fixed by hand in #1738)
//     is indistinguishable from ordinary prose.
//
// Widening any of the three means another walk or another predicate layer, and
// the point of this file is one cheap derivable check, not full coverage.
// Tracked in #1736.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// leadingIdent matches the first word of a doc paragraph when it has the shape
// of a Go identifier FOLLOWED BY WHITESPACE — the "// Name verbs …" form the
// Go convention prescribes. Anchored, so "The", "Returns" and "它" simply do
// not match and cost nothing.
//
// The trailing \s is load-bearing, not tidiness: with a bare \b, the prose
// "// Error-mapping coverage for applyPatch" yields "Error", which collides
// with any undocumented Error method in the package. Measured — that was the
// walk's only remaining false positive.
var leadingIdent = regexp.MustCompile(`^([A-Za-z_][A-Za-z0-9_]*)\s`)

// docAttachFinding is one violation: a comment paragraph whose first word
// names some OTHER declaration in the same package, and that declaration has
// no doc comment of its own.
type docAttachFinding struct {
	file   string // repo-relative
	line   int
	names  string // the declaration this paragraph is attached to ("-" when it is attached to nothing)
	orphan string // the declaration the paragraph appears to describe
	shape  string // "MISATTACHED" or "UNATTACHED"
}

// pkgSymbols is one package's top-level declarations: name -> whether that
// declaration carries a doc comment of its own.
type pkgSymbols map[string]bool

// pkgKey identifies a Go package by directory AND package clause, because one
// directory holds two of them whenever an external test package is present.
type pkgKey struct {
	dir string
	pkg string
}

// declNames returns every name a declaration answers to.
//
// Methods answer to their bare name only. An earlier version also registered
// "Recv.Foo", but leadingIdent below can never produce a dotted string, so
// that alias was unreachable — removed rather than made reachable, because
// making it reachable means teaching firstIdent a second spelling, and the
// dotted form is listed under NOT COVERED at the top of this file instead.
func declNames(d ast.Decl) []string {
	switch n := d.(type) {
	case *ast.FuncDecl:
		return []string{n.Name.Name}
	case *ast.GenDecl:
		var out []string
		for _, s := range n.Specs {
			out = append(out, specNames(s)...)
		}
		return out
	}
	return nil
}

func specNames(s ast.Spec) []string {
	switch v := s.(type) {
	case *ast.TypeSpec:
		return []string{v.Name.Name}
	case *ast.ValueSpec:
		var out []string
		for _, id := range v.Names {
			out = append(out, id.Name)
		}
		return out
	}
	return nil
}

// firstIdent returns the identifier a comment group opens with, or "" when the
// group opens with a directive (`//go:generate`, `//nolint:…` — no space after
// the slashes), with the package doc, or with anything that is not shaped like
// a Go identifier.
func firstIdent(cg *ast.CommentGroup) string {
	if cg == nil || len(cg.List) == 0 {
		return ""
	}
	raw := cg.List[0].Text
	if !strings.HasPrefix(raw, "//") {
		return "" // block comment: not the shape this defect takes
	}
	rest := raw[2:]
	if rest != "" && !strings.HasPrefix(rest, " ") && !strings.HasPrefix(rest, "\t") {
		return "" // directive
	}
	text := strings.TrimSpace(rest)
	m := leadingIdent.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	if m[1] == "Package" {
		return ""
	}
	return m[1]
}

// scanDocAttachment walks every non-vendor .go file under root and reports the
// two derivable shapes of the defect:
//
//	MISATTACHED — a declaration's doc opens by naming a DIFFERENT declaration
//	              in the same package, and that one has no doc of its own.
//	UNATTACHED  — a comment group attached to no declaration at all opens by
//	              naming a declaration that has no doc of its own.
//
// The "and that one has no doc of its own" clause is what keeps ordinary prose
// cross-references out: a paragraph may name a sibling freely as long as the
// sibling is documented where it lives. Without it the walk flags the whole
// `// listenUnix …` on `TestListenUnix_…` convention in *_test.go.
func scanDocAttachment(root string) ([]docAttachFinding, int, error) {
	type parsedFile struct {
		rel  string
		file *ast.File
	}
	// Keyed by directory AND package name, not directory alone: one directory
	// can hold two packages (`config` and its external test package
	// `config_test` both live in pkg/config). Merging their symbol tables was
	// measured to invent findings — a paragraph in `package p` naming a helper
	// that only exists in `package p_test` was reported as MISATTACHED.
	byPkg := map[pkgKey][]parsedFile{}
	fset := token.NewFileSet()
	parsed := 0

	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "vendor", "testdata", "node_modules", ".git":
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		f, perr := parser.ParseFile(fset, path, nil, parser.ParseComments)
		if perr != nil {
			// A file this walk cannot parse is a file it cannot vouch for.
			return fmt.Errorf("parse %s: %w", path, perr)
		}
		rel, rerr := filepath.Rel(root, path)
		if rerr != nil {
			rel = path
		}
		key := pkgKey{dir: filepath.Dir(path), pkg: f.Name.Name}
		byPkg[key] = append(byPkg[key], parsedFile{rel: filepath.ToSlash(rel), file: f})
		parsed++
		return nil
	})
	if err != nil {
		return nil, 0, err
	}

	var findings []docAttachFinding
	for _, files := range byPkg {
		syms := pkgSymbols{}
		for _, pf := range files {
			for _, decl := range pf.file.Decls {
				hasDoc := docOf(decl) != nil
				if gd, ok := decl.(*ast.GenDecl); ok && gd.Lparen.IsValid() {
					// Grouped: each spec carries its own doc.
					for _, s := range gd.Specs {
						sdoc := specDoc(s) != nil || hasDoc
						for _, n := range specNames(s) {
							syms[n] = syms[n] || sdoc
						}
					}
					continue
				}
				for _, n := range declNames(decl) {
					syms[n] = syms[n] || hasDoc
				}
			}
		}

		for _, pf := range files {
			attached := map[*ast.CommentGroup]bool{}
			if pf.file.Doc != nil {
				attached[pf.file.Doc] = true
			}
			for _, decl := range pf.file.Decls {
				if doc := docOf(decl); doc != nil {
					attached[doc] = true
					reportIfMisattached(&findings, pf.rel, fset, doc, declNames(decl), syms)
				}
				if gd, ok := decl.(*ast.GenDecl); ok && gd.Lparen.IsValid() {
					for _, s := range gd.Specs {
						if sdoc := specDoc(s); sdoc != nil {
							attached[sdoc] = true
							reportIfMisattached(&findings, pf.rel, fset, sdoc, specNames(s), syms)
						}
					}
				}
			}
			for _, cg := range pf.file.Comments {
				if attached[cg] || insideAnyDecl(pf.file, cg) {
					continue
				}
				id := firstIdent(cg)
				if id == "" {
					continue
				}
				if documented, known := syms[id]; known && !documented {
					findings = append(findings, docAttachFinding{
						file:   pf.rel,
						line:   fset.Position(cg.Pos()).Line,
						names:  "-",
						orphan: id,
						shape:  "UNATTACHED",
					})
				}
			}
		}
	}

	sort.Slice(findings, func(i, j int) bool {
		if findings[i].file != findings[j].file {
			return findings[i].file < findings[j].file
		}
		return findings[i].line < findings[j].line
	})
	return findings, parsed, nil
}

func reportIfMisattached(out *[]docAttachFinding, rel string, fset *token.FileSet, doc *ast.CommentGroup, names []string, syms pkgSymbols) {
	id := firstIdent(doc)
	if id == "" {
		return
	}
	// A declaration this walk cannot name (import specs, whose specNames is
	// nil) can never be shown to be the WRONG host, and reporting it would
	// print an empty "it is attached to".
	if len(names) == 0 {
		return
	}
	for _, n := range names {
		if n == id {
			return
		}
	}
	documented, known := syms[id]
	if !known || documented {
		return
	}
	*out = append(*out, docAttachFinding{
		file:   rel,
		line:   fset.Position(doc.Pos()).Line,
		names:  strings.Join(names, "/"),
		orphan: id,
		shape:  "MISATTACHED",
	})
}

// insideAnyDecl reports whether a comment group sits INSIDE a declaration's
// source range — a function body, a struct's field list, an interface's method
// set. Those comments document things this walk has no symbol table for
// (fields, interface methods, locals), and treating them as file-level
// paragraphs was measured to produce false positives: `// Inc records one
// audit observation` on an interface method was flagged because an unrelated
// concrete `Inc` method elsewhere in the package happens to be undocumented.
//
// ⚠️ This is a COVERAGE TRADE, not a free one. An earlier version of this
// comment claimed the defect "only ever appears BETWEEN top-level
// declarations"; that was measurably wrong — a struct field's doc can be
// swallowed by the next field's exactly the same way, and `go doc -all` then
// prints the merged text on the wrong field. Covering that needs a per-struct
// and per-interface symbol table, which is a second walk; the trade taken here
// is to stay at the top level and say so.
//
// The predicate is deliberately asymmetric: only a group fully inside a
// declaration is suppressed, so a trailing comment that starts inside one and
// ends past its closing brace still reaches the file-level rules.
func insideAnyDecl(f *ast.File, cg *ast.CommentGroup) bool {
	for _, d := range f.Decls {
		if cg.Pos() > d.Pos() && cg.End() < d.End() {
			return true
		}
	}
	return false
}

func docOf(d ast.Decl) *ast.CommentGroup {
	switch n := d.(type) {
	case *ast.FuncDecl:
		return n.Doc
	case *ast.GenDecl:
		return n.Doc
	}
	return nil
}

func specDoc(s ast.Spec) *ast.CommentGroup {
	switch v := s.(type) {
	case *ast.TypeSpec:
		return v.Doc
	case *ast.ValueSpec:
		return v.Doc
	}
	return nil
}

// componentsRoot returns <repo>/components, walking up from this module.
func componentsRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		cand := filepath.Join(dir, "components")
		if st, serr := os.Stat(cand); serr == nil && st.IsDir() {
			return cand
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("components/ not found above the threshold-exporter module")
		}
		dir = parent
	}
}

func TestDocCommentsAttachToTheDeclarationTheyName(t *testing.T) {
	t.Parallel()
	root := componentsRoot(t)

	findings, parsed, err := scanDocAttachment(root)
	if err != nil {
		t.Fatalf("scan %s: %v", root, err)
	}

	// Anti-vacuity, and it guards exactly one axis: corpus SIZE. A walk that
	// parsed nothing reports zero findings and is indistinguishable from a
	// clean tree. The floor sits far below today's count so it pins "the
	// corpus is real", not "the corpus is this exact size" (which every added
	// file would have to bump).
	//
	// The other two axes are guarded elsewhere, and it is worth knowing which
	// is which: a DEAD PREDICATE (leadingIdent stops matching, syms stops
	// being populated) is caught by TestDocAttachScannerDetectsBothShapes
	// below, whose fixture is synthesised and therefore cannot go quiet when
	// the tree gets fixed. Corpus IDENTITY — "we are scanning the right
	// components/, and both modules are in it" — is guarded by nothing here:
	// this floor is an absolute number, so losing the whole threshold-exporter
	// subtree (the smaller of the two) would still clear it.
	const minFiles = 200
	if parsed < minFiles {
		t.Fatalf("scanned only %d .go files under %s — the walk is not seeing the tree "+
			"(a zero-finding result here would be meaningless)", parsed, root)
	}

	if len(findings) > 0 {
		var b strings.Builder
		fmt.Fprintf(&b, "%d doc comment(s) describe a declaration they are not attached to "+
			"(#1736). Go ends a doc comment at a BLANK LINE:\n\n", len(findings))
		for _, f := range findings {
			fmt.Fprintf(&b, "  %s:%d  [%s]\n", f.file, f.line, f.shape)
			fmt.Fprintf(&b, "      this paragraph opens by naming %q, which has no doc of its own;\n", f.orphan)
			fmt.Fprintf(&b, "      it is attached to %s\n", f.names)
		}
		b.WriteString("\nThe fix is to MOVE the paragraph so it sits directly above the declaration\n" +
			"it names — for a const/var GROUP heading, split the group into its own\n" +
			"`const (...)` block so the heading becomes a block-level doc (worked\n" +
			"example: threshold-exporter/app/internal/guard/types.go).\n" +
			"\n" +
			"⛔ Inserting a blank line is NOT the fix, and this message used to say it\n" +
			"was. Measured: inside a const group it turns this test green while\n" +
			"DELETING the paragraph from `go doc -all`; above a top-level declaration\n" +
			"it cannot reach green at all (the paragraph becomes UNATTACHED instead).\n" +
			"\n" +
			"⛔ These also turn it green without fixing anything, so they are not the\n" +
			"way out either: giving the named declaration a stub doc, rewording the\n" +
			"paragraph so it no longer opens with the name, prefixing it with\n" +
			"//nolint:, or deleting the text. In every one of those the godoc output\n" +
			"stays exactly as wrong as it is now.")
		t.Fatal(b.String())
	}
}

// TestDocAttachScannerDetectsBothShapes is the positive control for the test
// above. Without it, "0 findings" cannot be told apart from a walk that looks
// at nothing — and this walk has exactly the shape that fails that way (it
// reports only what it finds).
//
// The fixture is synthesised here rather than taken from the tree so the
// control cannot go vacuous when the tree gets fixed, and it is written to a
// temp dir so it is never itself scanned by the test above.
func TestDocAttachScannerDetectsBothShapes(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	pkg := filepath.Join(dir, "probe")
	if err := os.MkdirAll(pkg, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	src := `package probe

// swallowed is the paragraph that belongs to swallowed(), with no blank line
// after it, so godoc merges it into the next one.
// host does something else entirely.
func host() {}

func swallowed() {}

// alsoOrphaned explains a function that lives further down, but this group is
// attached to no declaration at all.

func alsoOrphaned() {}

// documented has a doc of its own, so naming it from elsewhere is ordinary
// prose and must NOT be reported.
func documented() {}

// mentions documented in passing, which is a legitimate cross-reference.
func mentions() {}

// crossPkgOnly lives in this directory's EXTERNAL test package, so it is a
// different package and must not be reported here.
func namesCrossPkg() {}

//go:generate echo directives must not be read as identifiers
func directiveNeighbour() {}
`
	if err := os.WriteFile(filepath.Join(pkg, "probe.go"), []byte(src), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	// Same directory, DIFFERENT package. crossPkgOnly is undocumented and is
	// named from probe.go above, so a symbol table keyed by directory alone
	// reports probe.go as MISATTACHED — measured, that is what this fixture
	// pins. Born in the same edit as the pkgKey change it guards.
	ext := `package probe_test

func crossPkgOnly() {}
`
	if err := os.WriteFile(filepath.Join(pkg, "probe_ext_test.go"), []byte(ext), 0o600); err != nil {
		t.Fatalf("write external-package fixture: %v", err)
	}

	findings, parsed, err := scanDocAttachment(dir)
	if err != nil {
		t.Fatalf("scan fixture: %v", err)
	}
	if parsed != 2 {
		t.Fatalf("fixture parse count = %d, want 2", parsed)
	}

	got := map[string]string{}
	for _, f := range findings {
		got[f.orphan] = f.shape
	}

	if got["swallowed"] != "MISATTACHED" {
		t.Errorf("MISATTACHED shape not detected: findings = %+v", findings)
	}
	if got["alsoOrphaned"] != "UNATTACHED" {
		t.Errorf("UNATTACHED shape not detected: findings = %+v", findings)
	}
	// Negative controls, in the same run: a documented sibling named from
	// elsewhere, and a directive line, must not be reported. Without these the
	// two assertions above are also satisfied by a scanner that flags
	// everything.
	if _, bad := got["documented"]; bad {
		t.Errorf("false positive on a documented sibling: findings = %+v", findings)
	}
	if _, bad := got["crossPkgOnly"]; bad {
		t.Errorf("false positive across package boundary in one directory: findings = %+v", findings)
	}
	if _, bad := got["directiveNeighbour"]; bad {
		t.Errorf("false positive on a //go: directive: findings = %+v", findings)
	}
	if len(findings) != 2 {
		t.Errorf("want exactly 2 findings, got %d: %+v", len(findings), findings)
	}
}
