package main

// README §3.1 Endpoints and §3.2 旗標 / 環境變數 are the one list of the paths
// this exporter serves and the flags and environment variables it reads;
// docs/api/README*.md link to §3.1 instead of keeping a second copy (a second
// copy drifted: it lacked /api/v1/tenants/simulate). This test keeps both
// tables honest against the source.
//
// Source side is read with go/ast over this package's non-test files
// (http.ServeMux cannot list its routes):
//   - paths: the string literal passed to Handle / HandleFunc in buildMux;
//   - flags: flag.<Type>Var(&v, "<name>", <default>, _) — name, bound
//     variable, and a default that is a literal, N*time.<Unit>, or a constant
//     defined that way;
//   - env vars: the string literal passed to os.Getenv / os.LookupEnv. One
//     shaped `if v := os.Getenv("X"); v != "" { <var> = v }` is paired with
//     the flag bound to <var>; any other read has no flag.
//
// README side: §3.1 rows whose first cell is a backticked path; §3.2 rows of
// `| 旗標 | 環境變數 | 預設 | 說明 |`, where 旗標 is `-name` or —, 環境變數 is
// `NAME` or —, and 預設 is `value`, a bare duration/bool, (auto) for an empty
// string, or — on a row with no flag. A row in either section that does not
// parse fails the test.
//
// NOT COVERED: the Method column (most handlers accept any method); the prose
// in each row; the auto-detect order note under §3.2; defaults of variables
// that have no flag.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// opsEnvNotInReadme: variables this package reads that the §3.2 table leaves
// out on purpose. Each must still be read somewhere, or the entry fails.
var opsEnvNotInReadme = map[string]string{
	"GOMEMLIMIT": "applied by the Go runtime; main.go only logs it. Sizing guidance is in docs/integration/deployment-sizing.md",
}

type opsFlag struct {
	bound string // name of the variable passed as &v
	def   string // normalized default; see opsDefault
	isDur bool
	dur   time.Duration
	pos   string
}

type opsEnv struct {
	flagVar string // variable the value is assigned to; "" when not paired
	pos     string
}

var opsTimeUnits = map[string]time.Duration{
	"Nanosecond": time.Nanosecond, "Microsecond": time.Microsecond, "Millisecond": time.Millisecond,
	"Second": time.Second, "Minute": time.Minute, "Hour": time.Hour,
}

func opsStrLit(e ast.Expr) (string, bool) {
	bl, ok := e.(*ast.BasicLit)
	if !ok || bl.Kind != token.STRING {
		return "", false
	}
	s, err := strconv.Unquote(bl.Value)
	return s, err == nil
}

// opsDuration evaluates N*time.<Unit>.
func opsDuration(e ast.Expr) (time.Duration, bool) {
	be, ok := e.(*ast.BinaryExpr)
	if !ok || be.Op != token.MUL {
		return 0, false
	}
	bl, ok := be.X.(*ast.BasicLit)
	if !ok || bl.Kind != token.INT {
		return 0, false
	}
	n, err := strconv.ParseInt(bl.Value, 0, 64)
	if err != nil {
		return 0, false
	}
	sel, ok := be.Y.(*ast.SelectorExpr)
	if !ok {
		return 0, false
	}
	if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "time" {
		return 0, false
	}
	unit, ok := opsTimeUnits[sel.Sel.Name]
	if !ok {
		return 0, false
	}
	return time.Duration(n) * unit, true
}

func opsSelectorCall(call *ast.CallExpr) (pkg, name string) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return "", ""
	}
	id, ok := sel.X.(*ast.Ident)
	if !ok {
		return "", sel.Sel.Name
	}
	return id.Name, sel.Sel.Name
}

func opsParsePackage(t *testing.T) (*token.FileSet, []*ast.File) {
	t.Helper()
	paths, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	var files []*ast.File
	for _, p := range paths {
		if strings.HasSuffix(p, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, p, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, f)
	}
	return fset, files
}

func opsSourceRoutes(t *testing.T, fset *token.FileSet, files []*ast.File) map[string]string {
	t.Helper()
	out := map[string]string{}
	found := false
	for _, f := range files {
		for _, decl := range f.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Name.Name != "buildMux" {
				continue
			}
			found = true
			ast.Inspect(fn, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				if _, name := opsSelectorCall(call); name != "Handle" && name != "HandleFunc" {
					return true
				}
				pos := fset.Position(call.Pos()).String()
				path, ok := opsStrLit(call.Args[0])
				if !ok {
					t.Fatalf("%s: route pattern is not a string literal; this test cannot read it", pos)
				}
				out[path] = pos
				return true
			})
		}
	}
	if !found {
		t.Fatal("buildMux not found; the endpoint half of this test reads its Handle / HandleFunc calls")
	}
	return out
}

func opsSourceFlagsAndEnv(t *testing.T, fset *token.FileSet, files []*ast.File) (map[string]opsFlag, map[string]opsEnv) {
	t.Helper()
	consts := map[string]time.Duration{}
	for _, f := range files {
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				if len(vs.Names) == 1 && len(vs.Values) == 1 {
					if d, ok := opsDuration(vs.Values[0]); ok {
						consts[vs.Names[0].Name] = d
					}
				}
			}
		}
	}

	flags := map[string]opsFlag{}
	env := map[string]opsEnv{}
	addEnv := func(name string, e opsEnv) {
		if prev, dup := env[name]; dup && prev.flagVar != e.flagVar {
			t.Fatalf("%s read at %s and %s with different flag pairings", name, prev.pos, e.pos)
		}
		env[name] = e
	}
	for _, f := range files {
		paired := map[*ast.CallExpr]bool{}
		ast.Inspect(f, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.IfStmt:
				// if v := os.Getenv("X"); v != "" { target = v }
				as, ok := x.Init.(*ast.AssignStmt)
				if !ok || len(as.Lhs) != 1 || len(as.Rhs) != 1 {
					return true
				}
				call, ok := as.Rhs[0].(*ast.CallExpr)
				if !ok {
					return true
				}
				if pkg, name := opsSelectorCall(call); pkg != "os" || name != "Getenv" {
					return true
				}
				v, ok := as.Lhs[0].(*ast.Ident)
				name, ok2 := opsStrLit(call.Args[0])
				if !ok || !ok2 || len(x.Body.List) != 1 {
					return true
				}
				body, ok := x.Body.List[0].(*ast.AssignStmt)
				if !ok || len(body.Lhs) != 1 || len(body.Rhs) != 1 {
					return true
				}
				target, ok := body.Lhs[0].(*ast.Ident)
				rhs, ok2 := body.Rhs[0].(*ast.Ident)
				if !ok || !ok2 || rhs.Name != v.Name {
					return true
				}
				paired[call] = true
				addEnv(name, opsEnv{flagVar: target.Name, pos: fset.Position(call.Pos()).String()})
			case *ast.CallExpr:
				pkg, fn := opsSelectorCall(x)
				pos := fset.Position(x.Pos()).String()
				switch {
				case pkg == "os" && (fn == "Getenv" || fn == "LookupEnv"):
					if paired[x] {
						return true
					}
					name, ok := opsStrLit(x.Args[0])
					if !ok {
						t.Fatalf("%s: environment variable name is not a string literal", pos)
					}
					addEnv(name, opsEnv{pos: pos})
				case pkg == "flag" && strings.HasSuffix(fn, "Var") && len(x.Args) == 4:
					ue, ok := x.Args[0].(*ast.UnaryExpr)
					if !ok || ue.Op != token.AND {
						t.Fatalf("%s: flag.%s first argument is not &<var>", pos, fn)
					}
					bound, ok := ue.X.(*ast.Ident)
					name, ok2 := opsStrLit(x.Args[1])
					if !ok || !ok2 {
						t.Fatalf("%s: flag.%s name or target is not a literal", pos, fn)
					}
					fl := opsFlag{bound: bound.Name, pos: pos}
					switch d := x.Args[2].(type) {
					case *ast.BasicLit:
						s, ok := opsStrLit(d)
						if !ok {
							s = d.Value
						}
						fl.def = s
					case *ast.Ident:
						if c, ok := consts[d.Name]; ok {
							fl.isDur, fl.dur = true, c
						} else {
							fl.def = d.Name // true / false
						}
					default:
						dur, ok := opsDuration(d)
						if !ok {
							t.Fatalf("%s: cannot evaluate the default of -%s; extend this test", pos, name)
						}
						fl.isDur, fl.dur = true, dur
					}
					if _, dup := flags[name]; dup {
						t.Fatalf("flag -%s defined twice", name)
					}
					flags[name] = fl
				case pkg == "flag" && fn != "Parse" && fn != "Usage" && fn != "Args" && fn != "NArg" && fn != "Arg":
					t.Fatalf("%s: flag.%s is not a shape this test reads; extend it", pos, fn)
				}
			}
			return true
		})
	}
	return flags, env
}

func opsReadmeSection(t *testing.T, heading string) ([]string, int) {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "README.md"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(b), "\n")
	for i, l := range lines {
		if l == heading {
			end := i + 1
			for end < len(lines) && !strings.HasPrefix(lines[end], "#") {
				end++
			}
			return lines[i+1 : end], i + 2
		}
	}
	t.Fatalf("README has no %q section", heading)
	return nil, 0
}

// opsTableRows yields the body rows of the markdown tables in a section,
// skipping each table's header and separator.
func opsTableRows(lines []string, firstLine int) (rows [][]string, lineNos []int) {
	inTable := false
	for i, l := range lines {
		if !strings.HasPrefix(l, "|") {
			inTable = false
			continue
		}
		if !inTable {
			inTable = true // header row
			continue
		}
		if strings.HasPrefix(l, "|--") || strings.HasPrefix(l, "| --") {
			continue
		}
		cells := strings.Split(strings.Trim(l, "|"), " | ")
		for j := range cells {
			cells[j] = strings.TrimSpace(cells[j])
		}
		rows = append(rows, cells)
		lineNos = append(lineNos, firstLine+i)
	}
	return rows, lineNos
}

var (
	opsPathCellRe = regexp.MustCompile("^`(/[^`]*)`$")
	opsFlagCellRe = regexp.MustCompile("^`-([a-z0-9-]+)`$")
	opsEnvCellRe  = regexp.MustCompile("^`([A-Z][A-Z0-9_]*)`$")
	opsDefCellRe  = regexp.MustCompile("^(?:`([^`]*)`|([0-9a-z.]+)|(\\(auto\\))|(—))$")
)

func TestEndpointsReadmeParity(t *testing.T) {
	fset, files := opsParsePackage(t)
	src := opsSourceRoutes(t, fset, files)
	lines, first := opsReadmeSection(t, "### 3.1 Endpoints")
	rows, lineNos := opsTableRows(lines, first)
	doc := map[string]int{}
	for i, cells := range rows {
		m := opsPathCellRe.FindStringSubmatch(cells[0])
		if m == nil {
			t.Fatalf("README line %d: first cell %q is not a backticked path", lineNos[i], cells[0])
		}
		if _, dup := doc[m[1]]; dup {
			t.Fatalf("README lists %s twice", m[1])
		}
		doc[m[1]] = lineNos[i]
	}
	// Guard against an empty-vs-empty pass if a matcher stops matching.
	if len(src) < 5 || len(doc) < 5 {
		t.Fatalf("parsed too little: %d routes in buildMux, %d README rows", len(src), len(doc))
	}
	for p, pos := range src {
		if _, ok := doc[p]; !ok {
			t.Errorf("%s is registered at %s but missing from README §3.1", p, pos)
		}
	}
	for p, line := range doc {
		if _, ok := src[p]; !ok {
			t.Errorf("README line %d lists %s but buildMux does not register it", line, p)
		}
	}
}

func TestFlagsEnvReadmeParity(t *testing.T) {
	fset, files := opsParsePackage(t)
	flags, env := opsSourceFlagsAndEnv(t, fset, files)
	lines, first := opsReadmeSection(t, "### 3.2 旗標 / 環境變數")
	rows, lineNos := opsTableRows(lines, first)

	byVar := map[string]string{}
	for name, fl := range flags {
		byVar[fl.bound] = name
	}
	docFlags := map[string]int{}
	docEnv := map[string]int{}
	for i, cells := range rows {
		ln := lineNos[i]
		if len(cells) != 4 {
			t.Fatalf("README line %d: expected 旗標 | 環境變數 | 預設 | 說明, got %d cells", ln, len(cells))
		}
		flagName, envName := "", ""
		if cells[0] != "—" {
			m := opsFlagCellRe.FindStringSubmatch(cells[0])
			if m == nil {
				t.Fatalf("README line %d: 旗標 cell %q must be `-name` or —", ln, cells[0])
			}
			flagName = m[1]
		}
		if cells[1] != "—" {
			m := opsEnvCellRe.FindStringSubmatch(cells[1])
			if m == nil {
				t.Fatalf("README line %d: 環境變數 cell %q must be `NAME` or —", ln, cells[1])
			}
			envName = m[1]
		}
		if flagName == "" && envName == "" {
			t.Fatalf("README line %d: row names neither a flag nor a variable", ln)
		}
		dm := opsDefCellRe.FindStringSubmatch(cells[2])
		if dm == nil {
			t.Fatalf("README line %d: 預設 %q must be `value`, a bare duration/bool, (auto) or —; explanations belong in 說明", ln, cells[2])
		}

		if envName != "" {
			if _, dup := docEnv[envName]; dup {
				t.Fatalf("README lists %s twice", envName)
			}
			docEnv[envName] = ln
			if note, ok := opsEnvNotInReadme[envName]; ok {
				t.Errorf("README line %d lists %s, which opsEnvNotInReadme leaves out (%s); keep one of the two", ln, envName, note)
			}
			e, ok := env[envName]
			switch {
			case !ok:
				t.Errorf("README line %d lists %s but no source file reads it", ln, envName)
			case e.flagVar == "" && flagName != "":
				t.Errorf("README line %d pairs %s with -%s, but %s does not assign it to that flag's variable", ln, envName, flagName, e.pos)
			case e.flagVar != "" && byVar[e.flagVar] != flagName:
				t.Errorf("README line %d pairs %s with %q, source %s assigns it to the variable of -%s", ln, envName, cells[0], e.pos, byVar[e.flagVar])
			}
		}

		if flagName == "" {
			if dm[4] == "" {
				t.Errorf("README line %d: a row without a flag has no default to state; write —", ln)
			}
			continue
		}
		if _, dup := docFlags[flagName]; dup {
			t.Fatalf("README lists -%s twice", flagName)
		}
		docFlags[flagName] = ln
		fl, ok := flags[flagName]
		if !ok {
			t.Errorf("README line %d lists -%s but no flag.*Var defines it", ln, flagName)
			continue
		}
		var got string
		switch {
		case dm[1] != "":
			got = dm[1]
		case dm[2] != "":
			got = dm[2]
		case dm[3] != "":
			got = ""
		default:
			t.Errorf("README line %d: -%s has a default; — is only for rows without a flag", ln, flagName)
			continue
		}
		if fl.isDur {
			d, err := time.ParseDuration(got)
			if err != nil || d != fl.dur {
				t.Errorf("-%s: README line %d default %q, source %s (%s)", flagName, ln, cells[2], fl.dur, fl.pos)
			}
		} else if got != fl.def {
			t.Errorf("-%s: README line %d default %q, source %q (%s)", flagName, ln, cells[2], fl.def, fl.pos)
		}
	}

	if len(flags) < 5 || len(docFlags) < 5 || len(env) < 3 {
		t.Fatalf("parsed too little: %d flags, %d README flag rows, %d variables", len(flags), len(docFlags), len(env))
	}
	names := make([]string, 0, len(flags))
	for n := range flags {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		if _, ok := docFlags[n]; !ok {
			t.Errorf("-%s is defined at %s but missing from README §3.2", n, flags[n].pos)
		}
	}
	for n, e := range env {
		_, inDoc := docEnv[n]
		_, exempt := opsEnvNotInReadme[n]
		if !inDoc && !exempt {
			t.Errorf("%s is read at %s but missing from README §3.2 (or list it in opsEnvNotInReadme with a reason)", n, e.pos)
		}
	}
	for n := range opsEnvNotInReadme {
		if _, ok := env[n]; !ok {
			t.Errorf("%s is listed in opsEnvNotInReadme but no source file reads it; remove the entry", n)
		}
	}
}
