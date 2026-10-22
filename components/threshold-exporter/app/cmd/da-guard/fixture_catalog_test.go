package main

// Go half of tests/shared/confd_fixture_catalog.json (#3206): the trees the
// behaviour matrix (tests/shared/test_confd_behaviour_matrix.py) runs every
// conf.d tool on. Go owns the catalogue's expectations: this file requires
// every (role, problem kind) pair parse-tree can write to have a row, and
// every pair a row expects to be one parse-tree can write;
// fixture_catalog_unix_test.go builds each recipe and requires parse-tree's
// entries to be exactly the expected ones, as uid 65534 (a mode bit really
// denies). A kind or a role added to Go therefore turns this red until the
// catalogue — and with it the matrix — has a tree for it.

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// fixtureCatalogEnv names a copy of the catalogue to read instead of the
// checkout's: the run as uid 65534 cannot read a checkout in a 0700 home.
const fixtureCatalogEnv = "VIBE_CONFD_FIXTURE_CATALOG"

type catalogOp struct {
	Op      string `json:"op"`
	Path    string `json:"path"`
	PathHex string `json:"path_hex"`
	Text    string `json:"text"`
	B64     string `json:"b64"`
	Target  string `json:"target"`
	Via     string `json:"via"`
	Length  int    `json:"length"`
	Mode    string `json:"mode"`
}

type catalogEntry struct {
	Path string  `json:"path"`
	Role string  `json:"role"`
	Kind *string `json:"kind"`
}

type catalogRow struct {
	ID     string      `json:"id"`
	Matrix string      `json:"matrix"`
	Note   string      `json:"note"`
	Base   bool        `json:"base"`
	Ops    []catalogOp `json:"ops"`
	Expect struct {
		Refused bool           `json:"refused"`
		Entries []catalogEntry `json:"entries"`
	} `json:"expect"`
}

type fixtureCatalog struct {
	Comment     []string       `json:"_comment"`
	Root        string         `json:"root"`
	Base        []catalogOp    `json:"base"`
	BaseEntries []catalogEntry `json:"base_entries"`
	Rows        []catalogRow   `json:"rows"`
}

func fixtureCatalogPath() string {
	if p := os.Getenv(fixtureCatalogEnv); p != "" {
		return p
	}
	_, thisFile, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "confd_fixture_catalog.json")
}

func loadFixtureCatalog(t *testing.T) fixtureCatalog {
	t.Helper()
	raw, err := os.ReadFile(fixtureCatalogPath())
	if err != nil {
		t.Fatalf("read the fixture catalogue: %v", err)
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	var c fixtureCatalog
	if err := dec.Decode(&c); err != nil {
		t.Fatalf("the fixture catalogue: %v", err)
	}
	return c
}

// roleKind is one (role, problem kind) pair of an entry; Kind "" = no
// problem.
type roleKind struct {
	Role config.Role
	Kind parsetree.Kind
}

func (rk roleKind) String() string {
	if rk.Kind == "" {
		return string(rk.Role) + "/-"
	}
	return string(rk.Role) + "/" + string(rk.Kind)
}

// constsWithPrefix is every string constant of package config whose name
// starts with prefix, read from the package's source — so a reason added to
// the walk's closed sets is enumerated here without anyone listing it.
func constsWithPrefix(t *testing.T, prefix string) []string {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	dir := filepath.Join(filepath.Dir(thisFile), "..", "..", "pkg", "config")
	names, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("list pkg/config: %v", err)
	}
	fset := token.NewFileSet()
	var files []*ast.File
	for _, de := range names {
		n := de.Name()
		if de.IsDir() || !strings.HasSuffix(n, ".go") || strings.HasSuffix(n, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, n), nil, 0)
		if err != nil {
			t.Fatalf("parse pkg/config/%s: %v", n, err)
		}
		files = append(files, f)
	}
	if len(files) == 0 {
		t.Fatalf("no Go file in %s", dir)
	}
	var out []string
	for _, f := range files {
		for _, d := range f.Decls {
			gd, ok := d.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, s := range gd.Specs {
				vs := s.(*ast.ValueSpec)
				for i, n := range vs.Names {
					if !strings.HasPrefix(n.Name, prefix) || i >= len(vs.Values) {
						continue
					}
					lit, ok := vs.Values[i].(*ast.BasicLit)
					if !ok || lit.Kind != token.STRING {
						continue
					}
					v, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatalf("%s: %v", n.Name, err)
					}
					out = append(out, v)
				}
			}
		}
	}
	sort.Strings(out)
	return out
}

// emittableRoleKinds is every (role, problem kind) pair tree mode can write,
// derived from the very functions walkEntries and parseTreeOf use: each
// walk record reason (config's Unreadable* and NotFile* constants) through
// its Role() and unreadableKind / KindNotRegularFile, the root that cannot
// be listed, a walked directory, a kept file, and a kept file with each
// content kind — every parsetree.Kinds() no walk record writes.
func emittableRoleKinds(t *testing.T) map[roleKind]bool {
	t.Helper()
	unreadable := constsWithPrefix(t, "Unreadable")
	notFile := constsWithPrefix(t, "NotFile")
	if len(unreadable) < 4 || len(notFile) < 2 {
		t.Fatalf("the walk's reasons collapsed: Unreadable* %v, NotFile* %v", unreadable, notFile)
	}
	out := map[roleKind]bool{
		{config.RoleConfig, ""}:                          true, // TreeScan.Keys
		{config.RoleDir, ""}:                             true, // TreeScan.Dirs
		{config.RootWalkRole, parsetree.KindReadFailure}: true, // TreeScan.RootWalkErr
	}
	walkKinds := map[parsetree.Kind]bool{}
	for _, r := range unreadable {
		for _, missing := range []bool{false, true} {
			u := config.UnreadableFile{Reason: r, Missing: missing}
			out[roleKind{u.Role(), unreadableKind(r)}] = true
			walkKinds[unreadableKind(r)] = true
		}
	}
	for _, r := range notFile {
		out[roleKind{config.NotFile{Reason: r}.Role(), parsetree.KindNotRegularFile}] = true
	}
	walkKinds[parsetree.KindNotRegularFile] = true
	for _, k := range parsetree.Kinds() {
		if !walkKinds[k] {
			out[roleKind{config.RoleConfig, k}] = true // parseTreeOf: parsetree.Parse
		}
	}
	for rk := range out {
		known := false
		for _, r := range config.Roles() {
			known = known || rk.Role == r
		}
		if !known {
			t.Fatalf("%v: a role outside config.Roles()", rk)
		}
	}
	return out
}

func catalogRoleKinds(c fixtureCatalog) map[roleKind][]string {
	out := map[roleKind][]string{}
	add := func(id string, e catalogEntry) {
		k := parsetree.Kind("")
		if e.Kind != nil {
			k = parsetree.Kind(*e.Kind)
		}
		rk := roleKind{config.Role(e.Role), k}
		out[rk] = append(out[rk], id)
	}
	for _, row := range c.Rows {
		if row.Expect.Refused {
			continue
		}
		if row.Base {
			for _, e := range c.BaseEntries {
				add(row.ID, e)
			}
		}
		for _, e := range row.Expect.Entries {
			add(row.ID, e)
		}
	}
	return out
}

// TestFixtureCatalog_CoversEveryRoleAndKind: the catalogue has a tree for
// every (role, problem kind) pair parse-tree can write, and expects no pair
// it cannot.
func TestFixtureCatalog_CoversEveryRoleAndKind(t *testing.T) {
	c := loadFixtureCatalog(t)
	want := emittableRoleKinds(t)
	got := catalogRoleKinds(c)
	var missing, unknown []string
	for rk := range want {
		if len(got[rk]) == 0 {
			missing = append(missing, rk.String())
		}
	}
	for rk, ids := range got {
		if !want[rk] {
			unknown = append(unknown, rk.String()+" "+strings.Join(ids, ","))
		}
	}
	sort.Strings(missing)
	sort.Strings(unknown)
	if len(missing) > 0 {
		t.Errorf("parse-tree can write %v, and no catalogue row has a tree for it: add one "+
			"(the behaviour matrix runs every tool on every row)", missing)
	}
	if len(unknown) > 0 {
		t.Errorf("catalogue rows expect pairs parse-tree cannot write: %v", unknown)
	}
	ids := map[string]bool{}
	matrix := map[string]bool{"control": true, "flagged": true, "refused": true, "go_only": true}
	for _, row := range c.Rows {
		if ids[row.ID] {
			t.Errorf("row %q twice", row.ID)
		}
		ids[row.ID] = true
		if !matrix[row.Matrix] {
			t.Errorf("row %q: matrix %q is not one of control, flagged, refused, go_only",
				row.ID, row.Matrix)
		}
		if (row.Matrix == "refused") != row.Expect.Refused {
			t.Errorf("row %q: matrix %q and refused %v disagree", row.ID, row.Matrix, row.Expect.Refused)
		}
	}
}
