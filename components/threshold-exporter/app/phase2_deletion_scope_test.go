package main

// go/ast guard for the phase-2 deletion scope of ADR-036 §9 (#2766, #2486):
// phase 2 deletes the Go code that imitates PyYAML, and what may be deleted
// is decided by who still uses it. tests/shared/phase2_deletion_scope.json
// declares those users; this test lists them from the source and goes red
// when the two differ, in either direction:
//
//   - every importer of <exporter module>/pkg/routingpolicy and
//     <exporter module>/pkg/pyyamlcompat, as (repo-relative directory,
//     whether the importing file is a _test.go file);
//   - every `.SpacesOnly` selector (a call or a method value) outside
//     third_party/, the vendored yaml.v3 option phase 2 deletes;
//   - the set of third_party/*.patch files on disk vs the table's patches.
//
// Package level, not per symbol (owner decision on #2766, 2026-10-10):
// routingpolicy mixes code to delete with helpers to keep, and that split is
// prose in the table, not rows.
//
// What is read, deliberately wider than one `go build` sees:
//   - BOTH modules, components/threshold-exporter/app and
//     components/tenant-api; a missing module root is a failure, never a skip;
//   - every .go file whatever its build constraints (go/parser does not
//     evaluate them), _test.go files and external `_test` packages included;
//   - not third_party/ (vendored upstream yaml.v3, tests/_vendored_go.py) and
//     not testdata/. Every other directory is read, `.`/`_`-prefixed and
//     vendor/ included: an import there is not compiled today, but reporting
//     it costs a row while missing it costs a broken phase 2.
//
// The import path is compared exactly or as a parent ("<pkg>/sub" counts for
// <pkg>); the binding name (alias, `.`, `_`) is ignored, so every spelling
// of an import is the same use.
//
// ⚠️ NOT GUARDED: a use that does not go through an import or a `SpacesOnly`
// selector — reflection, a copy of the code pasted elsewhere, a generated file
// written at build time; and whether code relies on yaml.v3 keeping Tag "!"
// (the nonspecific-tag patch): the table records which packages are believed
// reliant, nothing here proves it.

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// phase2TablePath is the declared table, repo-relative.
const phase2TablePath = "tests/shared/phase2_deletion_scope.json"

// phase2Modules are the repo-relative roots of the Go modules the scope spans.
// The first is the module that defines the packages below.
var phase2Modules = []string{"components/threshold-exporter/app", "components/tenant-api"}

// phase2Packages are the module-relative packages whose importers are listed.
var phase2Packages = []string{"pkg/pyyamlcompat", "pkg/routingpolicy"}

// phase2Selector is the vendored yaml.v3 option whose uses are listed.
const phase2Selector = "SpacesOnly"

// Allowed dispositions. A package is deleted outright or split (part
// deleted, part kept or moved); a patch is kept, goes with routingpolicy, or
// goes once nothing relies on its behaviour.
var (
	phase2PackageDispositions = map[string]bool{"delete": true, "split": true}
	phase2PatchDispositions   = map[string]bool{
		"keep": true, "delete-with-routingpolicy": true, "delete-when-unrelied": true,
	}
)

type phase2Site struct {
	Dir  string `json:"dir"`
	Test bool   `json:"test"`
}

func (s phase2Site) String() string {
	if s.Test {
		return s.Dir + " (test file)"
	}
	return s.Dir + " (non-test file)"
}

type phase2Package struct {
	Package     string       `json:"package"`
	Category    string       `json:"category"`
	Disposition string       `json:"disposition"`
	Notes       string       `json:"notes"`
	Importers   []phase2Site `json:"importers"`
}

type phase2Patch struct {
	File            string   `json:"file"`
	Disposition     string   `json:"disposition"`
	Reason          string   `json:"reason"`
	BelievedReliant []string `json:"believed_reliant,omitempty"`
}

type phase2Table struct {
	Comment           []string        `json:"_comment"`
	Packages          []phase2Package `json:"packages"`
	SpacesOnlyCallers []phase2Site    `json:"spaces_only_callers"`
	Patches           []phase2Patch   `json:"patches"`
}

func TestPhase2DeletionScopeMatchesTable(t *testing.T) {
	t.Parallel()

	exporterRoot := exporterModuleRoot(t)
	repoRoot, err := phase2RepoRoot(exporterRoot, phase2Modules)
	if err != nil {
		t.Fatal(err)
	}
	modulePath, err := goModulePath(filepath.Join(exporterRoot, "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(phase2TablePath))) // #nosec G304 -- repo test fixture
	if err != nil {
		t.Fatalf("read %s: %v", phase2TablePath, err)
	}
	table, err := parsePhase2Table(data)
	if err != nil {
		t.Fatalf("%s: %v", phase2TablePath, err)
	}
	if problems := validatePhase2Table(table); len(problems) > 0 {
		t.Fatalf("%s is malformed:\n  %s", phase2TablePath, strings.Join(problems, "\n  "))
	}
	// A renamed package would otherwise have no importers and every row would
	// read as stale — red, but for the wrong reason. Say the right one.
	for _, pkg := range phase2Packages {
		if fi, err := os.Stat(filepath.Join(exporterRoot, filepath.FromSlash(pkg))); err != nil || !fi.IsDir() {
			t.Fatalf("%s/%s is gone; update phase2Packages and %s together", phase2Modules[0], pkg, phase2TablePath)
		}
	}

	scan, err := scanPhase2Scope(repoRoot, phase2Modules, modulePath, phase2Packages, phase2Selector)
	if err != nil {
		t.Fatalf("scanning: %v", err)
	}
	for _, mod := range phase2Modules {
		if scan.files[mod] == 0 {
			t.Fatalf("%s: no .go file read; the walk is not looking where the code is", mod)
		}
	}

	problems := comparePhase2Scope(table, scan)
	onDisk, err := listPatchFiles(filepath.Join(exporterRoot, "third_party"))
	if err != nil {
		t.Fatal(err)
	}
	problems = append(problems, comparePhase2Patches(table.Patches, onDisk)...)
	if len(problems) > 0 {
		t.Errorf("phase-2 deletion scope differs from %s (ADR-036 §9). The table is the reviewed "+
			"list of what phase 2 must rewrite before deleting; change it in the same PR as the code:\n  %s",
			phase2TablePath, strings.Join(problems, "\n  "))
	}
}

// phase2RepoRoot derives the repo root from the exporter module root and
// checks that every module in modules has a go.mod there. A module that is not
// found is an error: a guard that skipped it would report its users as absent.
func phase2RepoRoot(exporterRoot string, modules []string) (string, error) {
	want := "/" + modules[0]
	slashed := filepath.ToSlash(exporterRoot)
	if !strings.HasSuffix(slashed, want) {
		return "", fmt.Errorf("exporter module root %s does not end in %s; cannot locate the repo root", exporterRoot, want)
	}
	repoRoot := filepath.FromSlash(strings.TrimSuffix(slashed, want))
	if repoRoot == "" {
		repoRoot = string(filepath.Separator)
	}
	for _, mod := range modules {
		gomod := filepath.Join(repoRoot, filepath.FromSlash(mod), "go.mod")
		if fi, err := os.Stat(gomod); err != nil || fi.IsDir() {
			return "", fmt.Errorf("module %s not found (no %s); this guard reads every module of the "+
				"phase-2 scope and never skips one", mod, gomod)
		}
	}
	return repoRoot, nil
}

// goModulePath returns the `module` path declared in a go.mod file.
func goModulePath(gomod string) (string, error) {
	data, err := os.ReadFile(gomod) // #nosec G304 -- repo go.mod
	if err != nil {
		return "", err
	}
	sc := bufio.NewScanner(bytes.NewReader(data))
	for sc.Scan() {
		line := sc.Text()
		if i := strings.Index(line, "//"); i >= 0 {
			line = line[:i]
		}
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == "module" {
			if p, err := strconv.Unquote(fields[1]); err == nil {
				return p, nil
			}
			return fields[1], nil
		}
	}
	if err := sc.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("%s declares no module path", gomod)
}

func parsePhase2Table(data []byte) (*phase2Table, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var table phase2Table
	if err := dec.Decode(&table); err != nil {
		return nil, err
	}
	if dec.More() {
		return nil, errors.New("trailing data after the JSON object")
	}
	return &table, nil
}

// validatePhase2Table checks the table's own shape; it does not look at code.
func validatePhase2Table(table *phase2Table) []string {
	var problems []string
	bad := func(format string, args ...any) { problems = append(problems, fmt.Sprintf(format, args...)) }

	declared := map[string]bool{}
	for _, p := range table.Packages {
		known := false
		for _, want := range phase2Packages {
			known = known || p.Package == want
		}
		switch {
		case !known:
			bad("packages: %q is not one of %v", p.Package, phase2Packages)
		case declared[p.Package]:
			bad("packages: %q listed twice", p.Package)
		}
		declared[p.Package] = true
		if strings.TrimSpace(p.Category) == "" {
			bad("packages[%s].category is empty: name the ADR-036 §9 row", p.Package)
		}
		if !phase2PackageDispositions[p.Disposition] {
			bad("packages[%s].disposition %q is not one of %v", p.Package, p.Disposition, phase2Keys(phase2PackageDispositions))
		}
		if strings.TrimSpace(p.Notes) == "" {
			bad("packages[%s].notes is empty: say what goes and what stays", p.Package)
		}
		problems = append(problems, validatePhase2Sites("packages["+p.Package+"].importers", p.Importers)...)
	}
	for _, want := range phase2Packages {
		if !declared[want] {
			bad("packages: %q missing", want)
		}
	}

	problems = append(problems, validatePhase2Sites("spaces_only_callers", table.SpacesOnlyCallers)...)

	seen := map[string]bool{}
	withRoutingpolicy := false
	for _, p := range table.Patches {
		if p.File == "" || strings.ContainsAny(p.File, `/\`) || !strings.HasSuffix(p.File, ".patch") {
			bad("patches: %q is not a bare *.patch file name under third_party/", p.File)
		}
		if seen[p.File] {
			bad("patches: %q listed twice", p.File)
		}
		seen[p.File] = true
		if !phase2PatchDispositions[p.Disposition] {
			bad("patches[%s].disposition %q is not one of %v", p.File, p.Disposition, phase2Keys(phase2PatchDispositions))
		}
		if strings.TrimSpace(p.Reason) == "" {
			bad("patches[%s].reason is empty", p.File)
		}
		switch p.Disposition {
		case "delete-when-unrelied":
			if len(p.BelievedReliant) == 0 {
				bad("patches[%s]: delete-when-unrelied needs believed_reliant (the packages thought to rely on it)", p.File)
			}
			for _, r := range p.BelievedReliant {
				if !declared[r] {
					bad("patches[%s].believed_reliant: %q is not a declared package", p.File, r)
				}
			}
		default:
			if len(p.BelievedReliant) > 0 {
				bad("patches[%s]: believed_reliant only applies to delete-when-unrelied", p.File)
			}
		}
		withRoutingpolicy = withRoutingpolicy || p.Disposition == "delete-with-routingpolicy"
	}

	// A patch deleted together with routingpolicy must have no user outside it.
	if withRoutingpolicy {
		home := phase2Modules[0] + "/pkg/routingpolicy"
		for _, s := range table.SpacesOnlyCallers {
			if s.Dir != home && !strings.HasPrefix(s.Dir, home+"/") {
				bad("spaces_only_callers: %s is outside %s, but a patch is marked delete-with-routingpolicy; "+
					"deleting it with routingpolicy would break this caller", s, home)
			}
		}
	}
	return problems
}

func validatePhase2Sites(field string, sites []phase2Site) []string {
	var problems []string
	seen := map[phase2Site]bool{}
	for _, s := range sites {
		if s.Dir == "" || strings.Contains(s.Dir, `\`) || path.Clean(s.Dir) != s.Dir ||
			strings.HasPrefix(s.Dir, "/") || strings.HasPrefix(s.Dir, "..") {
			problems = append(problems, fmt.Sprintf("%s: %q is not a clean repo-relative slash path", field, s.Dir))
			continue
		}
		inModule := false
		for _, mod := range phase2Modules {
			inModule = inModule || s.Dir == mod || strings.HasPrefix(s.Dir, mod+"/")
		}
		if !inModule {
			problems = append(problems, fmt.Sprintf("%s: %s is in none of %v", field, s, phase2Modules))
		}
		if seen[s] {
			problems = append(problems, fmt.Sprintf("%s: %s listed twice", field, s))
		}
		seen[s] = true
	}
	return problems
}

type phase2Scan struct {
	importers map[string]map[phase2Site]bool // module-relative package -> sites
	selectors map[phase2Site]bool
	files     map[string]int // module -> .go files read
}

// scanPhase2Scope parses every .go file of each module (repo-relative) and
// records the importers of modulePath/<pkg> for each pkg and the sites of the
// selector `.<selector>`.
func scanPhase2Scope(repoRoot string, modules []string, modulePath string, packages []string, selector string) (*phase2Scan, error) {
	scan := &phase2Scan{
		importers: map[string]map[phase2Site]bool{},
		selectors: map[phase2Site]bool{},
		files:     map[string]int{},
	}
	for _, pkg := range packages {
		scan.importers[pkg] = map[phase2Site]bool{}
	}
	for _, mod := range modules {
		base := filepath.Join(repoRoot, filepath.FromSlash(mod))
		err := filepath.WalkDir(base, func(p string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if p != base && (d.Name() == "third_party" || d.Name() == "testdata") {
					return filepath.SkipDir
				}
				return nil
			}
			if !strings.HasSuffix(d.Name(), ".go") {
				return nil
			}
			fset := token.NewFileSet()
			file, perr := parser.ParseFile(fset, p, nil, parser.SkipObjectResolution)
			if perr != nil {
				return fmt.Errorf("cannot parse %s, so its uses cannot be listed: %w", p, perr)
			}
			scan.files[mod]++
			rel, rerr := filepath.Rel(repoRoot, filepath.Dir(p))
			if rerr != nil {
				return rerr
			}
			site := phase2Site{Dir: filepath.ToSlash(rel), Test: strings.HasSuffix(d.Name(), "_test.go")}
			for _, spec := range file.Imports {
				imported, uerr := strconv.Unquote(spec.Path.Value)
				if uerr != nil {
					return fmt.Errorf("%s: %w", fset.Position(spec.Pos()), uerr)
				}
				for _, pkg := range packages {
					target := modulePath + "/" + pkg
					if imported == target || strings.HasPrefix(imported, target+"/") {
						scan.importers[pkg][site] = true
					}
				}
			}
			ast.Inspect(file, func(n ast.Node) bool {
				if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == selector {
					scan.selectors[site] = true
				}
				return true
			})
			return nil
		})
		if err != nil {
			return nil, fmt.Errorf("module %s: %w", mod, err)
		}
	}
	return scan, nil
}

func comparePhase2Scope(table *phase2Table, scan *phase2Scan) []string {
	var problems []string
	for _, p := range table.Packages {
		problems = append(problems, diffPhase2Sites("importer of "+p.Package, p.Importers, scan.importers[p.Package])...)
	}
	problems = append(problems, diffPhase2Sites("."+phase2Selector+" use", table.SpacesOnlyCallers, scan.selectors)...)
	return problems
}

func diffPhase2Sites(what string, declared []phase2Site, found map[phase2Site]bool) []string {
	var problems []string
	want := map[phase2Site]bool{}
	for _, s := range declared {
		want[s] = true
		if !found[s] {
			problems = append(problems, fmt.Sprintf("stale row: %s declared at %s, but no file there has it any "+
				"more; delete the row", what, s))
		}
	}
	for s := range found {
		if !want[s] {
			problems = append(problems, fmt.Sprintf("new %s at %s: phase 2 would have one more user to rewrite "+
				"before deleting; remove the use, or add the row so the review sees the scope grow", what, s))
		}
	}
	sort.Strings(problems)
	return problems
}

// listPatchFiles returns the *.patch file names directly in dir.
func listPatchFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("list vendored patches: %w", err)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".patch") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}

func comparePhase2Patches(declared []phase2Patch, onDisk []string) []string {
	var problems []string
	want := map[string]bool{}
	for _, p := range declared {
		want[p.File] = true
	}
	have := map[string]bool{}
	for _, f := range onDisk {
		have[f] = true
		if !want[f] {
			problems = append(problems, fmt.Sprintf("third_party/%s has no row in patches: record whether phase 2 "+
				"keeps or deletes it (tests/ops/test_vendored_yaml_v3.py _PATCHES pins the same set)", f))
		}
	}
	for f := range want {
		if !have[f] {
			problems = append(problems, fmt.Sprintf("patches lists %s, which is not in third_party/; delete the row", f))
		}
	}
	sort.Strings(problems)
	return problems
}

func phase2Keys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// ---- controls: each red path on synthetic sources ----

const synthModulePath = "example.com/exp"

// synthModules mirror phase2Modules' shape under a temp root.
var synthModules = []string{"exp", "api"}

func writeSynthTree(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	all := map[string]string{"exp/go.mod": "module " + synthModulePath + "\n", "api/go.mod": "module example.com/api\n"}
	for k, v := range files {
		all[k] = v
	}
	for rel, src := range all {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestPhase2ScanFindsEverySpellingOfAUse(t *testing.T) {
	t.Parallel()

	const rp = synthModulePath + "/pkg/routingpolicy"
	const pc = synthModulePath + "/pkg/pyyamlcompat"
	imp := func(pkgClause, binding, path string) string {
		return pkgClause + "\n\nimport " + binding + " " + strconv.Quote(path) + "\n"
	}
	h := "api/internal/h"
	cases := []struct {
		name      string
		files     map[string]string
		importers map[string][]phase2Site // package -> sites
		selectors []phase2Site
	}{
		{"plain import", map[string]string{h + "/h.go": imp("package h", "", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"aliased import", map[string]string{h + "/h.go": imp("package h", "pol", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"dot import", map[string]string{h + "/h.go": imp("package h", ".", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"blank import", map[string]string{h + "/h.go": imp("package h", "_", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"file excluded by //go:build ignore", map[string]string{h + "/h.go": imp("//go:build ignore\n\npackage h", "", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"file excluded by its GOOS suffix", map[string]string{h + "/h_plan9.go": imp("package h", "", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"external _test package", map[string]string{"exp/pkg/routingpolicy/x_test.go": imp("package routingpolicy_test", "", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{"exp/pkg/routingpolicy", true}}}, nil},
		{"test and non-test file in one directory are two sites", map[string]string{
			h + "/h.go": imp("package h", "", rp), h + "/h_test.go": imp("package h", "", rp)},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}, {h, true}}}, nil},
		{"a package below it counts for it", map[string]string{h + "/h.go": imp("package h", "", rp+"/sub")},
			map[string][]phase2Site{"pkg/routingpolicy": {{h, false}}}, nil},
		{"pyyamlcompat is listed under its own package", map[string]string{h + "/h.go": imp("package h", "", pc)},
			map[string][]phase2Site{"pkg/pyyamlcompat": {{h, false}}}, nil},
		{"a raw-prefix sibling is another package", map[string]string{h + "/h.go": imp("package h", "", rp+"x")},
			nil, nil},
		{"testdata is not read", map[string]string{h + "/testdata/h.go": imp("package h", "", rp)},
			nil, nil},
		{"third_party is not read", map[string]string{
			"exp/third_party/yaml.v3/y.go": "package yaml\n\nimport _ " + strconv.Quote(rp) + "\n\nfunc f(d *D) { d.SpacesOnly(true) }\n"},
			nil, nil},
		{"SpacesOnly call", map[string]string{h + "/h.go": "package h\n\nfunc f(d interface{ SpacesOnly(bool) }) { d.SpacesOnly(true) }\n"},
			nil, []phase2Site{{h, false}}},
		{"SpacesOnly method value", map[string]string{h + "/h_test.go": "package h\n\nfunc f(d interface{ SpacesOnly(bool) }) { g := d.SpacesOnly; _ = g }\n"},
			nil, []phase2Site{{h, true}}},
		{"SpacesOnly as a bare name or in a comment is not a use", map[string]string{
			h + "/h.go": "package h\n\n// d.SpacesOnly(true)\nvar SpacesOnly = 1\n"},
			nil, nil},
	}
	for _, tc := range cases {
		root := writeSynthTree(t, tc.files)
		scan, err := scanPhase2Scope(root, synthModules, synthModulePath, phase2Packages, phase2Selector)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		for _, pkg := range phase2Packages {
			if got, want := siteList(scan.importers[pkg]), sortSites(tc.importers[pkg]); !equalSites(got, want) {
				t.Errorf("%s: importers of %s = %v, want %v", tc.name, pkg, got, want)
			}
		}
		if got, want := siteList(scan.selectors), sortSites(tc.selectors); !equalSites(got, want) {
			t.Errorf("%s: SpacesOnly sites = %v, want %v", tc.name, got, want)
		}

		// Red path: against a table declaring nothing, every found site is
		// reported as new, naming its directory.
		empty := &phase2Table{Packages: []phase2Package{{Package: "pkg/pyyamlcompat"}, {Package: "pkg/routingpolicy"}}}
		problems := comparePhase2Scope(empty, scan)
		wantN := len(tc.importers["pkg/routingpolicy"]) + len(tc.importers["pkg/pyyamlcompat"]) + len(tc.selectors)
		if len(problems) != wantN {
			t.Errorf("%s: %d problems against an empty table, want %d: %v", tc.name, len(problems), wantN, problems)
		}
		joined := strings.Join(problems, "\n")
		for _, p := range problems {
			if !strings.HasPrefix(p, "new ") {
				t.Errorf("%s: not reported as new: %s", tc.name, p)
			}
		}
		for _, sites := range [][]phase2Site{tc.importers["pkg/routingpolicy"], tc.importers["pkg/pyyamlcompat"], tc.selectors} {
			for _, s := range sites {
				if !strings.Contains(joined, " at "+s.String()+":") {
					t.Errorf("%s: no problem names %s: %v", tc.name, s, problems)
				}
			}
		}
	}
}

func TestPhase2CompareReportsStaleRows(t *testing.T) {
	t.Parallel()

	root := writeSynthTree(t, map[string]string{"api/internal/h/h.go": "package h\n"})
	scan, err := scanPhase2Scope(root, synthModules, synthModulePath, phase2Packages, phase2Selector)
	if err != nil {
		t.Fatal(err)
	}
	table := &phase2Table{
		Packages: []phase2Package{
			{Package: "pkg/pyyamlcompat"},
			{Package: "pkg/routingpolicy", Importers: []phase2Site{{"api/internal/h", false}}},
		},
		SpacesOnlyCallers: []phase2Site{{"exp/pkg/routingpolicy", false}},
	}
	problems := comparePhase2Scope(table, scan)
	if len(problems) != 2 {
		t.Fatalf("want 2 stale rows, got %v", problems)
	}
	for _, p := range problems {
		if !strings.HasPrefix(p, "stale row: ") {
			t.Errorf("not reported as stale: %s", p)
		}
	}
	// The same table matches once the uses exist: the stale verdict came from
	// their absence, not from the rows.
	root = writeSynthTree(t, map[string]string{
		"api/internal/h/h.go":           "package h\n\nimport _ " + strconv.Quote(synthModulePath+"/pkg/routingpolicy") + "\n",
		"exp/pkg/routingpolicy/load.go": "package routingpolicy\n\nfunc f(d interface{ SpacesOnly(bool) }) { d.SpacesOnly(true) }\n",
	})
	if scan, err = scanPhase2Scope(root, synthModules, synthModulePath, phase2Packages, phase2Selector); err != nil {
		t.Fatal(err)
	}
	if problems := comparePhase2Scope(table, scan); len(problems) != 0 {
		t.Errorf("matching tree still reported: %v", problems)
	}
}

func TestPhase2ScanFailsLoudly(t *testing.T) {
	t.Parallel()

	root := writeSynthTree(t, map[string]string{"api/internal/h/h.go": "package h\n\nfunc {\n"})
	if _, err := scanPhase2Scope(root, synthModules, synthModulePath, phase2Packages, phase2Selector); err == nil ||
		!strings.Contains(err.Error(), "cannot parse") {
		t.Errorf("unparseable file: err = %v, want a parse failure", err)
	}
	root = writeSynthTree(t, nil)
	if _, err := scanPhase2Scope(root, []string{"exp", "api", "missing"}, synthModulePath, phase2Packages, phase2Selector); err == nil {
		t.Error("missing module directory scanned without error")
	}

	// Repo-root location: tenant-api missing is an error, not a skip.
	repo := t.TempDir()
	exp := filepath.Join(repo, filepath.FromSlash(phase2Modules[0]))
	for _, d := range []string{exp, filepath.Join(repo, filepath.FromSlash(phase2Modules[1]))} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(exp, "go.mod"), []byte("module x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := phase2RepoRoot(exp, phase2Modules); err == nil || !strings.Contains(err.Error(), phase2Modules[1]) {
		t.Errorf("tenant-api without go.mod: err = %v, want it named", err)
	}
	if err := os.WriteFile(filepath.Join(repo, filepath.FromSlash(phase2Modules[1]), "go.mod"), []byte("module y\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, err := phase2RepoRoot(exp, phase2Modules); err != nil || got != repo {
		t.Errorf("complete layout: got %q, %v; want %q", got, err, repo)
	}
	if _, err := phase2RepoRoot(repo, phase2Modules); err == nil {
		t.Error("an exporter root not at components/threshold-exporter/app was accepted")
	}
	if p, err := goModulePath(filepath.Join(exp, "go.mod")); err != nil || p != "x" {
		t.Errorf("goModulePath = %q, %v", p, err)
	}
}

func validPhase2Table() *phase2Table {
	return &phase2Table{
		Packages: []phase2Package{
			{Package: "pkg/pyyamlcompat", Category: "c", Disposition: "delete", Notes: "n",
				Importers: []phase2Site{{"components/tenant-api/internal/policy", false}}},
			{Package: "pkg/routingpolicy", Category: "c", Disposition: "split", Notes: "n"},
		},
		SpacesOnlyCallers: []phase2Site{{"components/threshold-exporter/app/pkg/routingpolicy", false}},
		Patches: []phase2Patch{
			{File: "a.patch", Disposition: "keep", Reason: "r"},
			{File: "b.patch", Disposition: "delete-when-unrelied", Reason: "r", BelievedReliant: []string{"pkg/pyyamlcompat"}},
			{File: "c.patch", Disposition: "delete-with-routingpolicy", Reason: "r"},
		},
	}
}

func TestPhase2TableValidation(t *testing.T) {
	t.Parallel()

	// Anchor: the baseline is valid, so each case below fails for its own edit.
	if problems := validatePhase2Table(validPhase2Table()); len(problems) != 0 {
		t.Fatalf("baseline table rejected: %v", problems)
	}
	cases := []struct {
		name string
		edit func(*phase2Table)
		want string
	}{
		{"package missing", func(tb *phase2Table) { tb.Packages = tb.Packages[:1] }, `"pkg/routingpolicy" missing`},
		{"unknown package", func(tb *phase2Table) { tb.Packages[0].Package = "pkg/other" }, "is not one of"},
		{"package twice", func(tb *phase2Table) { tb.Packages[1].Package = "pkg/pyyamlcompat" }, "listed twice"},
		{"empty category", func(tb *phase2Table) { tb.Packages[0].Category = " " }, "category is empty"},
		{"bad package disposition", func(tb *phase2Table) { tb.Packages[0].Disposition = "keep" }, "disposition"},
		{"empty notes", func(tb *phase2Table) { tb.Packages[1].Notes = "" }, "notes is empty"},
		{"duplicate importer", func(tb *phase2Table) {
			tb.Packages[0].Importers = append(tb.Packages[0].Importers, tb.Packages[0].Importers[0])
		}, "listed twice"},
		{"importer outside the modules", func(tb *phase2Table) { tb.Packages[0].Importers[0].Dir = "scripts/x" }, "is in none of"},
		{"unclean dir", func(tb *phase2Table) { tb.Packages[0].Importers[0].Dir = "components/tenant-api/./x" }, "not a clean"},
		{"trailing slash", func(tb *phase2Table) { tb.Packages[0].Importers[0].Dir = "components/tenant-api/x/" }, "not a clean"},
		{"backslash", func(tb *phase2Table) { tb.Packages[0].Importers[0].Dir = `components\tenant-api` }, "not a clean"},
		{"patch path", func(tb *phase2Table) { tb.Patches[0].File = "third_party/a.patch" }, "bare *.patch"},
		{"patch twice", func(tb *phase2Table) { tb.Patches[1].File = "a.patch" }, "listed twice"},
		{"bad patch disposition", func(tb *phase2Table) { tb.Patches[0].Disposition = "delete" }, "disposition"},
		{"empty reason", func(tb *phase2Table) { tb.Patches[0].Reason = "" }, "reason is empty"},
		{"unrelied without reliant", func(tb *phase2Table) { tb.Patches[1].BelievedReliant = nil }, "needs believed_reliant"},
		{"reliant not a package", func(tb *phase2Table) { tb.Patches[1].BelievedReliant = []string{"pkg/x"} }, "not a declared package"},
		{"reliant on a kept patch", func(tb *phase2Table) { tb.Patches[0].BelievedReliant = []string{"pkg/pyyamlcompat"} }, "only applies"},
		{"SpacesOnly caller outside routingpolicy", func(tb *phase2Table) {
			tb.SpacesOnlyCallers = append(tb.SpacesOnlyCallers, phase2Site{"components/tenant-api/internal/h", false})
		}, "delete-with-routingpolicy"},
	}
	for _, tc := range cases {
		tb := validPhase2Table()
		tc.edit(tb)
		problems := validatePhase2Table(tb)
		if !strings.Contains(strings.Join(problems, "\n"), tc.want) {
			t.Errorf("%s: problems %v, want one containing %q", tc.name, problems, tc.want)
		}
	}

	for name, src := range map[string]string{
		"unknown field": `{"packages": [], "spaces_only_callers": [], "patches": [], "extra": 1}`,
		"trailing data": `{"packages": []} {}`,
		"wrong type":    `{"packages": {}}`,
	} {
		if _, err := parsePhase2Table([]byte(src)); err == nil {
			t.Errorf("%s: parsed without error", name)
		}
	}
	if _, err := parsePhase2Table([]byte(`{"_comment": ["x"], "packages": [], "spaces_only_callers": [], "patches": []}`)); err != nil {
		t.Errorf("well-formed table rejected: %v", err)
	}
}

func TestPhase2PatchSetComparison(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	for _, name := range []string{"a.patch", "b.patch", "README.md"} {
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(dir, "d.patch"), 0o755); err != nil {
		t.Fatal(err)
	}
	onDisk, err := listPatchFiles(dir)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(onDisk, ",") != "a.patch,b.patch" {
		t.Fatalf("listPatchFiles = %v", onDisk)
	}
	declared := []phase2Patch{{File: "a.patch"}, {File: "b.patch"}}
	if problems := comparePhase2Patches(declared, onDisk); len(problems) != 0 {
		t.Errorf("matching sets reported: %v", problems)
	}
	problems := comparePhase2Patches(declared[:1], onDisk)
	if len(problems) != 1 || !strings.Contains(problems[0], "b.patch has no row") {
		t.Errorf("unlisted patch on disk: %v", problems)
	}
	problems = comparePhase2Patches(append(declared, phase2Patch{File: "c.patch"}), onDisk)
	if len(problems) != 1 || !strings.Contains(problems[0], "c.patch, which is not in third_party") {
		t.Errorf("row without a patch file: %v", problems)
	}
	if _, err := listPatchFiles(filepath.Join(dir, "missing")); err == nil {
		t.Error("missing third_party directory listed without error")
	}
}

func siteList(m map[phase2Site]bool) []phase2Site {
	out := make([]phase2Site, 0, len(m))
	for s := range m {
		out = append(out, s)
	}
	return sortSites(out)
}

func sortSites(s []phase2Site) []phase2Site {
	out := append([]phase2Site(nil), s...)
	sort.Slice(out, func(i, j int) bool { return out[i].String() < out[j].String() })
	return out
}

func equalSites(a, b []phase2Site) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
