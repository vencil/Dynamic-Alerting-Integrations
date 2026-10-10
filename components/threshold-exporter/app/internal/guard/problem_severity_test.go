package guard

// problem_severity_test.go — binds the merge-key corpus test's emulation of
// "da-guard blocks on this Problem" (blocksDaGuard in
// pkg/routingpolicy/merge_key_corpus_test.go: every Problem kind fails the
// run except routing_profiles_unusable) to the real severity mapping,
// platformProblemFindings. That package cannot import this one, so the
// emulation is a copy; if a Problem kind ever becomes a warning here, the
// corpus test would hold da-guard to "blocks" while da-guard lets the file
// through — a looser Go reader hidden as an equal one (ADR-036 step 2).
// The kinds are read from routingpolicy's own source (every `Problem*`
// string constant), so a new kind is covered without editing this test.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// routingPolicyProblemKinds returns every `Problem*` string constant
// declared in pkg/routingpolicy's non-test sources: name → value.
func routingPolicyProblemKinds(t *testing.T) map[string]string {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	dir := filepath.Join(filepath.Dir(thisFile), "..", "..", "pkg", "routingpolicy")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read %s: %v", dir, err)
	}
	fset := token.NewFileSet()
	var files []*ast.File
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, e.Name()), nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", e.Name(), err)
		}
		files = append(files, f)
	}
	kinds := map[string]string{}
	for _, f := range files {
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, name := range vs.Names {
					if !strings.HasPrefix(name.Name, "Problem") || i >= len(vs.Values) {
						continue
					}
					lit, ok := vs.Values[i].(*ast.BasicLit)
					if !ok || lit.Kind != token.STRING {
						continue
					}
					v, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatalf("%s: %v", name.Name, err)
					}
					kinds[name.Name] = v
				}
			}
		}
	}
	return kinds
}

func TestPlatformProblemSeverityMatchesCorpusEmulation(t *testing.T) {
	kinds := routingPolicyProblemKinds(t)
	if kinds["ProblemDomainPolicyUnusable"] != routingpolicy.ProblemDomainPolicyUnusable ||
		kinds["ProblemRoutingProfilesUnusable"] != routingpolicy.ProblemRoutingProfilesUnusable {
		t.Fatalf("Problem kinds read from source look wrong: %v", kinds)
	}
	for name, kind := range kinds {
		got := platformProblemFindings([]routingpolicy.Problem{{Kind: kind, Message: "m"}})
		if len(got) != 1 {
			t.Fatalf("%s: %d findings, want 1", name, len(got))
		}
		// What blocksDaGuard (merge_key_corpus_test.go) assumes.
		want := SeverityError
		if kind == routingpolicy.ProblemRoutingProfilesUnusable {
			want = SeverityWarn
		}
		if got[0].Severity != want {
			t.Errorf("%s (%q): platformProblemFindings severity %q, the corpus test's blocksDaGuard assumes %q"+
				" — update both together", name, kind, got[0].Severity, want)
		}
	}
}
