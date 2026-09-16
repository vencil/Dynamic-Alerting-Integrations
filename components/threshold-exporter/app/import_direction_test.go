package main

// go/ast guard for the dependency direction depguard's `logic-no-nethttp` rule
// declares in .golangci.yml: the pure-logic packages reused by the CLI tools
// (da-parser / da-guard) must not import net/http (#1870).
//
// depguard enforces the same rule in the Go Lint job. This test exists so the
// rule survives the lint config being edited: dropping depguard from
// `linters.enable`, emptying its `rules`, narrowing `files:`, or switching
// `linters.default` all leave depguard silent while the import graph is left
// unguarded.
//
// Parity with depguard, measured on golangci-lint 2.12.2 so this test is not
// the narrower of the two:
//   - deny is a path-segment prefix: `net/http/httptest` is rejected as well
//     as `net/http`; `net/url` is not.
//   - _test.go files in scope are checked; depguard lints them too.
//   - directories `./...` never loads (testdata, vendor, `.`/`_` prefix) are
//     skipped, as golangci skips them.
//
// ⛔ Two enforcers, two package lists, deliberately independent: each covers
// what it names, so a package is unguarded only when BOTH omit it. A new
// logic package goes into both.

import (
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// Module-relative directories of the CLI-reusable logic layer. Same set as the
// depguard rule's `files:` globs.
var logicPackages = []string{
	"internal/guard",
	"internal/parser",
}

const netHTTPImportPath = "net/http"

func TestLogicPackagesDoNotImportNetHTTP(t *testing.T) {
	t.Parallel()

	root := exporterModuleRoot(t)
	violations, scanned, err := scanDeniedImports(root, logicPackages, netHTTPImportPath)
	if err != nil {
		t.Fatalf("scanning logic packages: %v", err)
	}
	// A renamed or emptied package would otherwise make this pass on nothing.
	for _, scope := range logicPackages {
		if scanned[scope] == 0 {
			t.Fatalf("%s holds no .go file this guard reads; update logicPackages "+
				"(and depguard's `files:`) rather than letting the scope go empty", scope)
		}
	}
	if len(violations) > 0 {
		t.Errorf("CLI-reusable logic package imports %s; keep HTTP at the server edge so "+
			"da-parser / da-guard can import these packages:\n  %s",
			netHTTPImportPath, strings.Join(violations, "\n  "))
	}
}

// exporterModuleRoot walks up from the test's working directory to go.mod.
func exporterModuleRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found above the threshold-exporter package directory")
		}
		dir = parent
	}
}

// scanDeniedImports parses the imports of every .go file under each scope and
// returns the ones naming deny or a package below it, plus a per-scope count
// of files read.
func scanDeniedImports(root string, scopes []string, deny string) ([]string, map[string]int, error) {
	var violations []string
	scanned := make(map[string]int, len(scopes))
	for _, scope := range scopes {
		base := filepath.Join(root, filepath.FromSlash(scope))
		err := filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				name := d.Name()
				if path != base && (name == "testdata" || name == "vendor" ||
					strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_")) {
					return filepath.SkipDir
				}
				return nil
			}
			if !strings.HasSuffix(path, ".go") {
				return nil
			}
			fset := token.NewFileSet()
			file, perr := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
			if perr != nil {
				return perr
			}
			scanned[scope]++
			for _, spec := range file.Imports {
				imported, uerr := strconv.Unquote(spec.Path.Value)
				if uerr != nil {
					return uerr
				}
				if imported == deny || strings.HasPrefix(imported, deny+"/") {
					violations = append(violations, fset.Position(spec.Pos()).String()+" imports "+imported)
				}
			}
			return nil
		})
		if err != nil {
			return nil, nil, err
		}
	}
	sort.Strings(violations)
	return violations, scanned, nil
}

// The scanner against synthetic trees, both directions: a guard that reports
// nothing passes the main test just as well as one that is right.
func TestScanDeniedImportsReadsWhatDepguardReads(t *testing.T) {
	t.Parallel()

	const deny = "net/http"
	cases := []struct {
		name  string
		files map[string]string // scope-relative path -> import path
		want  int               // violations
	}{
		{"exact package", map[string]string{"a.go": deny}, 1},
		{"package below it", map[string]string{"a.go": deny + "/httptest"}, 1},
		{"shared raw prefix, different segment", map[string]string{"a.go": deny + "x"}, 0},
		{"test file", map[string]string{"a_test.go": deny}, 1},
		{"testdata is not loaded by ./...", map[string]string{"testdata/a.go": deny}, 0},
	}
	for _, tc := range cases {
		root := t.TempDir()
		for rel, imp := range tc.files {
			path := filepath.Join(root, "d", filepath.FromSlash(rel))
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			src := "package d\n\nimport _ " + strconv.Quote(imp) + "\n"
			if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		got, _, err := scanDeniedImports(root, []string{"d"}, deny)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if len(got) != tc.want {
			t.Errorf("%s: %d violations, want %d: %v", tc.name, len(got), tc.want, got)
		}
	}
}
