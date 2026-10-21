package rbac

// go/ast guard for the dependency direction depguard's `domain-no-handler`
// rule declares in .golangci.yml: the domain packages must not import the HTTP
// handler layer (#1870).
//
// Where it bites: handler already depends on most of the packages the rule
// names, so a non-test file in one of them importing handler is an import
// cycle the compiler rejects on its own. What depguard and this test add is
// the rest — a new subpackage under a domain directory, an external `_test`
// package, and a domain package handler does not (yet) import.
//
// depguard enforces the same rule in the Go Lint job. This test runs it
// without golangci-lint: it reads the rule out of .golangci.yml (settings, not
// `linters.enable`), so dropping depguard from `linters.enable` (measured: the
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
// #3226: ONE package list — the rule's `files:` in .golangci.yml, read here
// (it used to be copied by hand, and the copy's prose had already drifted).
// Every entry must be "**/internal/<dir>/**"; any other shape fails the test
// instead of being guessed at, and the rule missing or empty fails it too.
// ⚠️ NOT GUARDED: deleting a package from that one list unguards it for
// both enforcers at once (the price of one list); deleting this file
// together with the depguard rule; and the exporter carries a byte-identical
// copy of scanDeniedImports that nothing keeps in step.

import (
	"fmt"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

const handlerImportPath = "github.com/vencil/tenant-api/internal/handler"

// domainRuleName is the depguard rule whose `files:` names the domain layer.
const domainRuleName = "domain-no-handler"

// domainPackages reads the module-relative directories of depguard's
// domain-no-handler rule from root's .golangci.yml, and checks that the rule
// denies handlerImportPath.
func domainPackages(t *testing.T, root string) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(root, ".golangci.yml"))
	if err != nil {
		t.Fatalf("read .golangci.yml: %v", err)
	}
	pkgs, err := parseDomainRule(raw)
	if err != nil {
		t.Fatalf(".golangci.yml: %v", err)
	}
	return pkgs
}

// parseDomainRule is domainPackages on the file's bytes.
func parseDomainRule(raw []byte) ([]string, error) {
	var cfg struct {
		Linters struct {
			Settings struct {
				Depguard struct {
					Rules map[string]struct {
						Files []string `yaml:"files"`
						Deny  []struct {
							Pkg string `yaml:"pkg"`
						} `yaml:"deny"`
					} `yaml:"rules"`
				} `yaml:"depguard"`
			} `yaml:"settings"`
		} `yaml:"linters"`
	}
	if err := yaml.Unmarshal(raw, &cfg); err != nil {
		return nil, err
	}
	rule, ok := cfg.Linters.Settings.Depguard.Rules[domainRuleName]
	if !ok {
		return nil, fmt.Errorf("no depguard rule %q under linters.settings.depguard.rules", domainRuleName)
	}
	denies := false
	for _, d := range rule.Deny {
		denies = denies || d.Pkg == handlerImportPath
	}
	if !denies {
		return nil, fmt.Errorf("depguard rule %q does not deny %s", domainRuleName, handlerImportPath)
	}
	if len(rule.Files) == 0 {
		return nil, fmt.Errorf("depguard rule %q names no files", domainRuleName)
	}
	var out []string
	for _, f := range rule.Files {
		dir, ok := strings.CutPrefix(f, "**/")
		if ok {
			dir, ok = strings.CutSuffix(dir, "/**")
		}
		if !ok || !strings.HasPrefix(dir, "internal/") || strings.ContainsAny(dir, "*?[!{") {
			return nil, fmt.Errorf("depguard rule %q: files entry %q is not \"**/internal/<dir>/**\"; "+
				"this guard reads only that shape", domainRuleName, f)
		}
		out = append(out, dir)
	}
	return out, nil
}

func TestDomainPackagesDoNotImportHandler(t *testing.T) {
	t.Parallel()

	root := moduleRootForGuard(t)
	scopes := domainPackages(t, root)
	violations, scanned, err := scanDeniedImports(root, scopes, handlerImportPath)
	if err != nil {
		t.Fatalf("scanning domain packages: %v", err)
	}
	// A renamed or emptied package would otherwise make this pass on nothing.
	for _, scope := range scopes {
		if scanned[scope] == 0 {
			t.Fatalf("%s holds no .go file this guard reads; update depguard's `files:` "+
				"rather than letting the scope go empty", scope)
		}
	}
	if len(violations) > 0 {
		t.Errorf("domain package imports the HTTP handler layer (%s); move the shared "+
			"piece into the domain side, or mirror it by value as middleware.go does:\n  %s",
			handlerImportPath, strings.Join(violations, "\n  "))
	}
}

// The rule reader against synthetic configs, both directions: a reader that
// returns nothing would pass the main test's scan just as well.
func TestParseDomainRule(t *testing.T) {
	t.Parallel()
	const head = "linters:\n  settings:\n    depguard:\n      rules:\n"
	deny := "          deny:\n            - pkg: " + handlerImportPath + "\n"
	cases := []struct {
		name string
		yml  string
		want []string // nil: an error
	}{
		{"two dirs", head + "        domain-no-handler:\n          files:\n            - \"**/internal/a/**\"\n" +
			"            - \"**/internal/b/c/**\"\n" + deny, []string{"internal/a", "internal/b/c"}},
		{"rule missing", head + "        other:\n          files:\n            - \"**/internal/a/**\"\n" + deny, nil},
		{"no files", head + "        domain-no-handler:\n" + deny, nil},
		{"does not deny handler", head + "        domain-no-handler:\n          files:\n            - \"**/internal/a/**\"\n" +
			"          deny:\n            - pkg: net/http\n", nil},
		{"negated entry", head + "        domain-no-handler:\n          files:\n            - \"!**/internal/a/x_test.go\"\n" + deny, nil},
		{"glob inside", head + "        domain-no-handler:\n          files:\n            - \"**/internal/a*/**\"\n" + deny, nil},
		{"outside internal", head + "        domain-no-handler:\n          files:\n            - \"**/cmd/**\"\n" + deny, nil},
	}
	for _, tc := range cases {
		got, err := parseDomainRule([]byte(tc.yml))
		if tc.want == nil {
			if err == nil {
				t.Errorf("%s: no error, got %v", tc.name, got)
			}
			continue
		}
		if err != nil || strings.Join(got, ",") != strings.Join(tc.want, ",") {
			t.Errorf("%s: got %v, %v; want %v", tc.name, got, err, tc.want)
		}
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
