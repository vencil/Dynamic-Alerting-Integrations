package rbac

// go/ast guard for the dependency direction depguard's `domain-no-handler`
// rule declares in .golangci.yml: the domain packages must not import the HTTP
// handler layer (#1870).
//
// Where it bites: handler already depends on all five packages below, so a
// non-test file in one of them importing handler is an import cycle the
// compiler rejects on its own. What depguard and this test add is the rest —
// a new subpackage under a domain directory, and an external `_test` package.
//
// depguard enforces the same rule in the Go Lint job. This test does not read
// the lint config, so dropping depguard from `linters.enable` (measured: the
// violation then passes golangci and fails here) does not retire the rule.
//
// Parity with depguard on golangci-lint 2.12.2, measured, so this test is not
// the narrower of the two:
//   - deny is a raw string prefix: `internal/handlerx` is rejected, like
//     `internal/handler/federation`.
//   - _test.go files in scope are checked; depguard lints them too.
//   - a `vendor` directory that holds code is a package and is read; its
//     subdirectories, `testdata`, and `.`/`_`-prefixed directories are
//     skipped, as `./...` skips them.
//
// ⛔ Two enforcers, two package lists, deliberately independent: each covers
// what it names, so a package is unguarded only when BOTH omit it. A new
// domain package goes into both. ⚠️ NOT GUARDED: deleting this file together
// with the depguard rule; and the exporter carries a byte-identical copy of
// scanDeniedImports that nothing keeps in step.

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

// Module-relative directories that make up the domain layer. Same set as the
// depguard rule's `files:` globs.
var domainPackages = []string{
	"internal/customalerts",
	"internal/groups",
	"internal/policy",
	"internal/rbac",
	"internal/views",
}

const handlerImportPath = "github.com/vencil/tenant-api/internal/handler"

func TestDomainPackagesDoNotImportHandler(t *testing.T) {
	t.Parallel()

	root := moduleRootForGuard(t)
	violations, scanned, err := scanDeniedImports(root, domainPackages, handlerImportPath)
	if err != nil {
		t.Fatalf("scanning domain packages: %v", err)
	}
	// A renamed or emptied package would otherwise make this pass on nothing.
	for _, scope := range domainPackages {
		if scanned[scope] == 0 {
			t.Fatalf("%s holds no .go file this guard reads; update domainPackages "+
				"(and depguard's `files:`) rather than letting the scope go empty", scope)
		}
	}
	if len(violations) > 0 {
		t.Errorf("domain package imports the HTTP handler layer (%s); move the shared "+
			"piece into the domain side, or mirror it by value as middleware.go does:\n  %s",
			handlerImportPath, strings.Join(violations, "\n  "))
	}
}

// scanDeniedImports parses the imports of every .go file under each scope and
// returns the ones whose path starts with deny (a raw string prefix, as
// depguard matches), plus a per-scope count of files read.
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
				if path != base && (name == "testdata" ||
					strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") ||
					filepath.Base(filepath.Dir(path)) == "vendor") {
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
				if strings.HasPrefix(imported, deny) {
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

	const deny = "example.com/m/internal/handler"
	cases := []struct {
		name  string
		files map[string]string // scope-relative path -> import path
		want  int               // violations
	}{
		{"exact package", map[string]string{"a.go": deny}, 1},
		{"package below it", map[string]string{"a.go": deny + "/federation"}, 1},
		{"raw-prefix sibling, as depguard matches", map[string]string{"a.go": deny + "x"}, 1},
		{"unrelated import", map[string]string{"a.go": "strings"}, 0},
		{"test file", map[string]string{"a_test.go": deny}, 1},
		{"nested subdirectory", map[string]string{"sub/a.go": deny}, 1},
		{"testdata is not loaded by ./...", map[string]string{"testdata/a.go": deny}, 0},
		{"vendor holding code is a package", map[string]string{"vendor/a.go": deny}, 1},
		{"below vendor is not loaded", map[string]string{"vendor/x/a.go": deny}, 0},
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
