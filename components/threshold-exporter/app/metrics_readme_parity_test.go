package main

// README §3.3 Metrics is the one list of what this exporter serves on
// /metrics; docs/api/README*.md link to it instead of keeping a second copy
// (a second copy drifted: four of its six label sets were wrong). This test
// keeps that list honest against the source.
//
// Source side is read with go/ast, not by running the exporter: the Collector
// is unchecked (Describe sends nothing) and most series appear only under a
// config that triggers them, so a scrape cannot enumerate them. What counts:
//   - prometheus.NewDesc("<name>", _, []string{...}, ...) — labels are the
//     literal; a non-literal label argument means the label set is built at
//     scrape time (user_threshold) and only the name is checked.
//   - prometheus.New{Counter,Gauge,Histogram,Summary}[Vec](prometheus.XxxOpts{
//     Name: "<name>"}, []string{...}) — labels from the Vec's literal.
//
// README side: rows of the §3.3 tables whose first cell starts with a
// backticked name. A row declares its labels either as `name{a,b}` in that
// cell or as a `label: a / b` (or `labels:`) clause in the row.
//
// NOT COVERED: metrics whose name is not a string literal at the call site;
// label VALUES; the prose in each row.

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
)

// readmeOnlyMetrics are rows for series this module does not construct:
// promhttp registers its own error counter on the Registry (#2032).
var readmeOnlyMetrics = map[string]bool{
	"promhttp_metric_handler_errors_total": true,
}

type sourceMetric struct {
	labels  []string // sorted; nil when dynamic
	dynamic bool
	pos     string
}

func stringLits(e ast.Expr) ([]string, bool) {
	if id, ok := e.(*ast.Ident); ok && id.Name == "nil" {
		return []string{}, true
	}
	cl, ok := e.(*ast.CompositeLit)
	if !ok {
		return nil, false
	}
	out := []string{}
	for _, el := range cl.Elts {
		bl, ok := el.(*ast.BasicLit)
		if !ok || bl.Kind != token.STRING {
			return nil, false
		}
		s, err := strconv.Unquote(bl.Value)
		if err != nil {
			return nil, false
		}
		out = append(out, s)
	}
	sort.Strings(out)
	return out, true
}

func isPromCall(call *ast.CallExpr, names ...string) (string, bool) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return "", false
	}
	pkg, ok := sel.X.(*ast.Ident)
	if !ok || pkg.Name != "prometheus" {
		return "", false
	}
	for _, n := range names {
		if sel.Sel.Name == n {
			return n, true
		}
	}
	return "", false
}

func optsName(e ast.Expr) (string, bool) {
	cl, ok := e.(*ast.CompositeLit)
	if !ok {
		return "", false
	}
	for _, el := range cl.Elts {
		kv, ok := el.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		if k, ok := kv.Key.(*ast.Ident); ok && k.Name == "Name" {
			if bl, ok := kv.Value.(*ast.BasicLit); ok && bl.Kind == token.STRING {
				s, err := strconv.Unquote(bl.Value)
				return s, err == nil
			}
		}
	}
	return "", false
}

func collectSourceMetrics(t *testing.T) map[string]sourceMetric {
	t.Helper()
	out := map[string]sourceMetric{}
	fset := token.NewFileSet()
	add := func(name string, m sourceMetric) {
		if prev, dup := out[name]; dup {
			t.Fatalf("%s constructed twice (%s, %s); this test assumes one construction per name", name, prev.pos, m.pos)
		}
		out[name] = m
	}
	err := filepath.WalkDir(".", func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path != "." && (d.Name() == "testdata" || d.Name() == "cmd" || strings.HasPrefix(d.Name(), ".")) {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return err
		}
		ast.Inspect(f, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			pos := fset.Position(call.Pos()).String()
			if _, ok := isPromCall(call, "NewDesc"); ok && len(call.Args) >= 3 {
				bl, ok := call.Args[0].(*ast.BasicLit)
				if !ok || bl.Kind != token.STRING {
					return true
				}
				name, _ := strconv.Unquote(bl.Value)
				labels, static := stringLits(call.Args[2])
				add(name, sourceMetric{labels: labels, dynamic: !static, pos: pos})
				return true
			}
			fn, ok := isPromCall(call, "NewCounter", "NewGauge", "NewHistogram", "NewSummary",
				"NewCounterVec", "NewGaugeVec", "NewHistogramVec", "NewSummaryVec")
			if !ok || len(call.Args) == 0 {
				return true
			}
			name, ok := optsName(call.Args[0])
			if !ok {
				return true
			}
			m := sourceMetric{labels: []string{}, pos: pos}
			if strings.HasSuffix(fn, "Vec") {
				labels, static := stringLits(call.Args[1])
				m.labels, m.dynamic = labels, !static
			}
			add(name, m)
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

type readmeRow struct {
	labels   []string // sorted; nil when the row declares none
	declared bool
	line     int
}

var (
	rowNameRe     = regexp.MustCompile("^\\|\\s*`([a-z_][a-z0-9_]*)(\\{([a-z0-9_,\\s]*)\\})?`\\s*\\|")
	labelClauseRe = regexp.MustCompile(`labels?:\s*([^；;）)|]+)`)
	labelTokenRe  = regexp.MustCompile(`^[a-z_][a-z0-9_]*$`)
)

func readmeMetrics(t *testing.T) map[string]readmeRow {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "README.md"))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]readmeRow{}
	in := false
	for i, line := range strings.Split(string(b), "\n") {
		switch {
		case strings.HasPrefix(line, "### 3.3 "):
			in = true
			continue
		case in && (strings.HasPrefix(line, "### ") || strings.HasPrefix(line, "## ")):
			in = false
		}
		if !in {
			continue
		}
		m := rowNameRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		row := readmeRow{line: i + 1}
		var raw []string
		if m[2] != "" {
			raw = strings.Split(m[3], ",")
		} else if c := labelClauseRe.FindStringSubmatch(line); c != nil {
			raw = strings.Split(c[1], "/")
		}
		for _, tok := range raw {
			tok = strings.Trim(strings.TrimSpace(tok), "`")
			if labelTokenRe.MatchString(tok) {
				row.labels = append(row.labels, tok)
			}
		}
		if row.labels != nil {
			row.declared = true
			sort.Strings(row.labels)
		}
		if _, dup := out[m[1]]; dup {
			t.Errorf("README.md:%d: %s listed twice in §3.3", i+1, m[1])
		}
		out[m[1]] = row
	}
	if len(out) == 0 {
		t.Fatal("no metric rows found under README.md `### 3.3 ` — heading or table shape changed; fix this parser, do not skip")
	}
	return out
}

func TestMetricsREADMEMatchesSource(t *testing.T) {
	t.Parallel()
	src := collectSourceMetrics(t)
	doc := readmeMetrics(t)

	if len(src) == 0 {
		t.Fatal("no metric constructions found in source — walker broke; fix it, do not skip")
	}
	for name, m := range src {
		row, ok := doc[name]
		if !ok {
			t.Errorf("%s (%s) is served but missing from README.md §3.3", name, m.pos)
			continue
		}
		if m.dynamic {
			continue
		}
		switch {
		case len(m.labels) == 0 && row.declared:
			t.Errorf("README.md:%d: %s declares labels %v; the source has none", row.line, name, row.labels)
		case len(m.labels) > 0 && strings.Join(m.labels, ",") != strings.Join(row.labels, ","):
			t.Errorf("README.md:%d: %s labels %v; source (%s) has %v — write them as `%s{...}` or a `labels: a / b` clause",
				row.line, name, row.labels, m.pos, m.labels, name)
		}
	}
	for name, row := range doc {
		if _, ok := src[name]; !ok && !readmeOnlyMetrics[name] {
			t.Errorf("README.md:%d: %s is listed in §3.3 but nothing in this module constructs it", row.line, name)
		}
	}
}
