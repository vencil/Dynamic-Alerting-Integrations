package main

// components/tenant-api/README.md "### 環境變數" is the one list of the
// environment variables tenant-api reads. Its previous form omitted fourteen of
// them and stated `(空)` for two branch variables whose default is "main"; this
// test keeps the tables honest against the source.
//
// Source side is read with go/ast over this module's non-test files
// (cmd/server and internal/**, minus internal/testutil). A variable is any
// string literal named TA_*, TENANT_API_* or GIT_COMMITTER_* passed to:
//   - envOrDefault(name, "<lit>")                → default <lit>
//   - envBool(name)                              → default false
//   - parseDurationOrDefault(os.Getenv(name), d) → default d, where d is 0,
//     N*time.<Unit>, or a constant defined that way
//   - os.Getenv / os.LookupEnv(name) anywhere else → default resolved below
//
// Defaults the call site does not state are resolved by:
//   - defaultByResolver: the exported parser the server feeds the value to,
//     called with "" (what an unset variable reads as);
//   - defaultByConst: the constant the reading function falls back to. The
//     test checks that function really references the constant;
//   - uncheckedDefaults: neither applies; the reason is written down.
// A variable read with a bare os.Getenv that is in none of these fails, so a
// new variable cannot ship with an unchecked default by accident.
//
// README side: every table row in the section whose first cell is backticked
// names separated by " / ". The default cell is " / "-separated tokens, each
// `value` or (空) for the empty string; one token applies to every name in the
// row. Anything else in that cell fails: explanations go in the 說明 column.
//
// NOT COVERED: the 說明 prose; flags that have no environment variable.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
)

var envNameRe = regexp.MustCompile(`^(TA_|TENANT_API_|GIT_COMMITTER_)[A-Z0-9_]+$`)

// defaultByResolver: variables whose call site passes "" (or nothing) and whose
// real default comes from the parser main.go hands the value to.
var defaultByResolver = map[string]func() string{
	"TA_RATE_LIMIT_PER_MIN": func() string {
		cfg, _ := handler.RateLimitConfigFromEnv("")
		return strconv.Itoa(cfg.RequestsPerMinute)
	},
	"TA_MAX_BODY_BYTES": func() string {
		n, _ := handler.MaxBodyBytesFromEnv("")
		return strconv.FormatInt(n, 10)
	},
	"TA_MAX_BATCH_BODY_BYTES": func() string {
		n, _ := handler.MaxBatchBodyBytesFromEnv("")
		return strconv.FormatInt(n, 10)
	},
	"TA_MAX_TENANT_DOC_BYTES": func() string {
		n, _ := gitops.TenantDocBytesFromEnv("")
		return strconv.FormatInt(n, 10)
	},
}

// defaultByConst: variable → the constant its (unexported) reader returns when
// the variable is unset.
var defaultByConst = map[string]string{
	"TA_WRITE_QUEUE_DEPTH":   "defaultWriteQueueDepth",
	"TA_GIT_FETCH_TIMEOUT":   "defaultGitFetchTimeout",
	"TENANT_API_GIT_TIMEOUT": "defaultGitTimeout",
}

var uncheckedDefaults = map[string]string{
	"TA_LOG_LEVEL":        "configureLogger's switch falls through to slog.LevelInfo; no value to read",
	"TA_GITHUB_TOKEN":     "bare os.Getenv in wire.go, no fallback; empty is rejected in PR mode",
	"TA_GITHUB_API_URL":   "bare os.Getenv in wire.go; empty keeps the client's built-in github.com URL",
	"TA_GITLAB_TOKEN":     "bare os.Getenv in wire.go, no fallback; empty is rejected in MR mode",
	"TA_GITLAB_API_URL":   "bare os.Getenv in wire.go; empty keeps the client's built-in gitlab.com URL",
	"GIT_COMMITTER_NAME":  "bare os.Getenv in gitops/writer.go; empty falls back to the author",
	"GIT_COMMITTER_EMAIL": "bare os.Getenv in gitops/writer.go; empty falls back to the author",
}

type envKind int

const (
	kindString envKind = iota
	kindDuration
	kindBare // read by a bare os.Getenv / os.LookupEnv
)

type envSource struct {
	kind envKind
	def  string          // kindString
	dur  time.Duration   // kindDuration
	fns  []*ast.FuncDecl // enclosing functions, for kindBare
	pos  string
}

type constVal struct {
	dur   time.Duration
	isDur bool
	n     int64
}

func strLit(e ast.Expr) (string, bool) {
	bl, ok := e.(*ast.BasicLit)
	if !ok || bl.Kind != token.STRING {
		return "", false
	}
	s, err := strconv.Unquote(bl.Value)
	return s, err == nil
}

func intLit(e ast.Expr) (int64, bool) {
	bl, ok := e.(*ast.BasicLit)
	if !ok || bl.Kind != token.INT {
		return 0, false
	}
	n, err := strconv.ParseInt(strings.ReplaceAll(bl.Value, "_", ""), 0, 64)
	return n, err == nil
}

var timeUnits = map[string]time.Duration{
	"Nanosecond": time.Nanosecond, "Microsecond": time.Microsecond, "Millisecond": time.Millisecond,
	"Second": time.Second, "Minute": time.Minute, "Hour": time.Hour,
}

// evalConst evaluates the constant shapes this module uses for env defaults:
// an int literal, or <int literal> * time.<Unit>.
func evalConst(e ast.Expr) (constVal, bool) {
	if n, ok := intLit(e); ok {
		return constVal{n: n}, true
	}
	be, ok := e.(*ast.BinaryExpr)
	if !ok || be.Op != token.MUL {
		return constVal{}, false
	}
	n, ok := intLit(be.X)
	if !ok {
		return constVal{}, false
	}
	sel, ok := be.Y.(*ast.SelectorExpr)
	if !ok {
		return constVal{}, false
	}
	if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "time" {
		return constVal{}, false
	}
	unit, ok := timeUnits[sel.Sel.Name]
	if !ok {
		return constVal{}, false
	}
	return constVal{dur: time.Duration(n) * unit, isDur: true}, true
}

func isCallTo(call *ast.CallExpr, pkg, name string) bool {
	switch f := call.Fun.(type) {
	case *ast.Ident:
		return pkg == "" && f.Name == name
	case *ast.SelectorExpr:
		id, ok := f.X.(*ast.Ident)
		return ok && id.Name == pkg && f.Sel.Name == name
	}
	return false
}

func moduleSourceFiles(t *testing.T) []string {
	t.Helper()
	var files []string
	for _, root := range []string{".", filepath.Join("..", "..", "internal")} {
		err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if d.Name() == "testutil" || d.Name() == "testdata" {
					return filepath.SkipDir
				}
				return nil
			}
			if strings.HasSuffix(path, ".go") && !strings.HasSuffix(path, "_test.go") {
				files = append(files, path)
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	return files
}

func collectEnvSources(t *testing.T) (map[string]envSource, map[string]constVal) {
	t.Helper()
	fset := token.NewFileSet()
	var parsed []*ast.File
	for _, path := range moduleSourceFiles(t) {
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		parsed = append(parsed, f)
	}

	// Constants first: a duration default may name one from another package.
	consts := map[string]constVal{}
	ambiguous := map[string]bool{}
	for _, f := range parsed {
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				if len(vs.Names) != 1 || len(vs.Values) != 1 {
					continue
				}
				v, ok := evalConst(vs.Values[0])
				if !ok {
					continue
				}
				name := vs.Names[0].Name
				if prev, dup := consts[name]; dup && prev != v {
					ambiguous[name] = true
				}
				consts[name] = v
			}
		}
	}
	for name := range ambiguous {
		delete(consts, name)
	}

	durationOf := func(e ast.Expr) (time.Duration, bool) {
		var name string
		switch x := e.(type) {
		case *ast.Ident:
			name = x.Name
		case *ast.SelectorExpr:
			name = x.Sel.Name
		default:
			v, ok := evalConst(e)
			if !ok || (!v.isDur && v.n != 0) {
				return 0, false
			}
			return v.dur, true
		}
		v, ok := consts[name]
		if !ok || !v.isDur {
			return 0, false
		}
		return v.dur, true
	}

	out := map[string]envSource{}
	add := func(name string, src envSource) {
		prev, dup := out[name]
		switch {
		case !dup:
			out[name] = src
		case prev.kind == kindBare && src.kind == kindBare:
			// Re-read for logging and the like: same variable, no default stated.
			prev.fns = append(prev.fns, src.fns...)
			out[name] = prev
		default:
			t.Fatalf("%s read at %s and %s, at least one with a stated default; this test assumes one such site per variable", name, prev.pos, src.pos)
		}
	}
	for _, f := range parsed {
		for _, decl := range f.Decls {
			fn, _ := decl.(*ast.FuncDecl)
			handled := map[*ast.CallExpr]bool{}
			ast.Inspect(decl, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || handled[call] || len(call.Args) == 0 {
					return true
				}
				pos := fset.Position(call.Pos()).String()
				switch {
				case isCallTo(call, "", "envOrDefault") && len(call.Args) == 2:
					name, ok1 := strLit(call.Args[0])
					def, ok2 := strLit(call.Args[1])
					if ok1 && ok2 && envNameRe.MatchString(name) {
						add(name, envSource{kind: kindString, def: def, pos: pos})
					}
				case isCallTo(call, "", "envBool"):
					if name, ok := strLit(call.Args[0]); ok && envNameRe.MatchString(name) {
						add(name, envSource{kind: kindString, def: "false", pos: pos})
					}
				case isCallTo(call, "", "parseDurationOrDefault") && len(call.Args) == 2:
					inner, ok := call.Args[0].(*ast.CallExpr)
					if !ok || !isCallTo(inner, "os", "Getenv") {
						return true
					}
					name, ok := strLit(inner.Args[0])
					if !ok || !envNameRe.MatchString(name) {
						return true
					}
					handled[inner] = true
					d, ok := durationOf(call.Args[1])
					if !ok {
						t.Fatalf("%s: cannot evaluate the default of %s; extend evalConst or list it in uncheckedDefaults", pos, name)
					}
					add(name, envSource{kind: kindDuration, dur: d, pos: pos})
				case isCallTo(call, "os", "Getenv") || isCallTo(call, "os", "LookupEnv"):
					if name, ok := strLit(call.Args[0]); ok && envNameRe.MatchString(name) {
						add(name, envSource{kind: kindBare, fns: []*ast.FuncDecl{fn}, pos: pos})
					}
				}
				return true
			})
		}
	}
	return out, consts
}

func funcsReference(fns []*ast.FuncDecl, ident string) bool {
	found := false
	for _, fn := range fns {
		if fn == nil {
			continue
		}
		ast.Inspect(fn, func(n ast.Node) bool {
			if id, ok := n.(*ast.Ident); ok && id.Name == ident {
				found = true
			}
			return !found
		})
	}
	return found
}

type readmeEnv struct {
	def  string
	line int
}

var (
	readmeEnvNameRe = regexp.MustCompile("^`([A-Z0-9_]+)`$")
	readmeEnvDefRe  = regexp.MustCompile("^(?:`([^`]*)`|\\(空\\))$")
)

func readmeEnvTable(t *testing.T) map[string]readmeEnv {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "README.md"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(b), "\n")
	start := -1
	for i, l := range lines {
		if l == "### 環境變數" {
			start = i + 1
			break
		}
	}
	if start < 0 {
		t.Fatal("README has no \"### 環境變數\" section")
	}
	out := map[string]readmeEnv{}
	for i := start; i < len(lines) && !strings.HasPrefix(lines[i], "#"); i++ {
		l := lines[i]
		if !strings.HasPrefix(l, "| `") {
			continue
		}
		cells := strings.Split(strings.Trim(l, "|"), " | ")
		if len(cells) < 3 {
			t.Fatalf("README line %d: expected 變數 | 預設 | 說明, got %q", i+1, l)
		}
		var names []string
		for _, tok := range strings.Split(strings.TrimSpace(cells[0]), " / ") {
			m := readmeEnvNameRe.FindStringSubmatch(tok)
			if m == nil {
				t.Fatalf("README line %d: name cell token %q is not a backticked variable", i+1, tok)
			}
			names = append(names, m[1])
		}
		var defs []string
		for _, tok := range strings.Split(strings.TrimSpace(cells[1]), " / ") {
			m := readmeEnvDefRe.FindStringSubmatch(tok)
			if m == nil {
				t.Fatalf("README line %d: default token %q must be `value` or (空); explanations belong in 說明", i+1, tok)
			}
			defs = append(defs, m[1])
		}
		if len(defs) != 1 && len(defs) != len(names) {
			t.Fatalf("README line %d: %d names but %d defaults", i+1, len(names), len(defs))
		}
		for j, n := range names {
			d := defs[0]
			if len(defs) > 1 {
				d = defs[j]
			}
			if _, dup := out[n]; dup {
				t.Fatalf("README lists %s twice", n)
			}
			out[n] = readmeEnv{def: d, line: i + 1}
		}
	}
	return out
}

func TestEnvReadmeParity(t *testing.T) {
	src, consts := collectEnvSources(t)
	doc := readmeEnvTable(t)
	// Guard against an empty-vs-empty pass if a matcher stops matching.
	if len(src) < 20 || len(doc) < 20 {
		t.Fatalf("parsed too little: %d source variables, %d README rows", len(src), len(doc))
	}

	names := map[string]bool{}
	for n := range src {
		names[n] = true
	}
	for n := range doc {
		names[n] = true
	}
	sorted := make([]string, 0, len(names))
	for n := range names {
		sorted = append(sorted, n)
	}
	sort.Strings(sorted)

	for _, name := range sorted {
		s, inSrc := src[name]
		d, inDoc := doc[name]
		switch {
		case !inDoc:
			t.Errorf("%s is read at %s but missing from the README table", name, s.pos)
			continue
		case !inSrc:
			t.Errorf("README line %d lists %s but no source file reads it", d.line, name)
			continue
		}

		var want string
		var wantDur time.Duration
		isDur := false
		switch {
		case defaultByResolver[name] != nil:
			want = defaultByResolver[name]()
		case defaultByConst[name] != "":
			c := defaultByConst[name]
			if s.kind != kindBare || !funcsReference(s.fns, c) {
				t.Errorf("%s: defaultByConst names %s, but the function reading it at %s does not reference that constant", name, c, s.pos)
				continue
			}
			v, ok := consts[c]
			if !ok {
				t.Errorf("%s: constant %s not found or not evaluable", name, c)
				continue
			}
			if v.isDur {
				wantDur, isDur = v.dur, true
			} else {
				want = strconv.FormatInt(v.n, 10)
			}
		case uncheckedDefaults[name] != "":
			if s.kind != kindBare {
				t.Errorf("%s is in uncheckedDefaults but its default is now readable at %s; remove the entry", name, s.pos)
			}
			continue
		case s.kind == kindString:
			want = s.def
		case s.kind == kindDuration:
			wantDur, isDur = s.dur, true
		default:
			t.Errorf("%s is read with a bare os.Getenv at %s; add it to defaultByResolver, defaultByConst or uncheckedDefaults", name, s.pos)
			continue
		}

		if isDur {
			got, err := time.ParseDuration(d.def)
			if err != nil || got != wantDur {
				t.Errorf("%s: README line %d default %q, source %s", name, d.line, d.def, wantDur)
			}
			continue
		}
		if d.def != want {
			t.Errorf("%s: README line %d default %q, source %q", name, d.line, d.def, want)
		}
	}

	for _, m := range []map[string]string{defaultByConst, uncheckedDefaults} {
		for name := range m {
			if _, ok := src[name]; !ok {
				t.Errorf("%s is listed in this test but no source file reads it; remove the entry", name)
			}
		}
	}
	for name := range defaultByResolver {
		if _, ok := src[name]; !ok {
			t.Errorf("%s is listed in defaultByResolver but no source file reads it; remove the entry", name)
		}
	}
}
