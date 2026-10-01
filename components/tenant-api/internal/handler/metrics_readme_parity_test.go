package handler

// components/tenant-api/README.md "Metrics(`/metrics`)" is the one list of what
// tenant-api serves on /metrics. Its previous form was a hand-copied sample of
// seven series while the handler emitted eighteen families; this test keeps the
// table honest.
//
// Source side is read with go/ast, not by scraping: four families only appear
// under a config (human socket, forge write-back, a synced tracker), so one
// scrape cannot enumerate them. What counts, from string literals in this
// package's non-test files:
//   - "# TYPE <name> <type>\n"        → the family and its type
//   - "<name>{<label>=%q} ..."        → its label name
//
// Label VALUES: a row may list them as `值:` followed by backticked values; they
// are compared with the series in testdata/metrics.golden (which
// TestMetricsHandler_Golden keeps equal to the real exposition). A row that
// lists values for a family the golden does not render fails, since nothing
// could check them.
//
// NOT COVERED: the 何時出現 / 用途 prose; families whose name is not a literal.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"testing"
)

var (
	typeLitRe    = regexp.MustCompile(`^# TYPE (tenant_api_\w+) (\w+)\n$`)
	labelLitRe   = regexp.MustCompile(`^(tenant_api_\w+)\{(\w+)=%q\}`)
	goldenLineRe = regexp.MustCompile(`^(tenant_api_\w+)\{(\w+)="([^"]*)"\}`)
	readmeRowRe  = regexp.MustCompile("^\\| `(tenant_api_\\w+)(?:\\{([\\w,]+)\\})?` \\| (\\w+) \\|")
	readmeValsRe = regexp.MustCompile("值:((?:\\s*`[^`]+`\\s*/?)+)")
	backtickRe   = regexp.MustCompile("`([^`]+)`")
)

type metricFamily struct {
	typ    string
	labels []string // sorted
	values []string // sorted; README side only
}

func sourceFamilies(t *testing.T) map[string]metricFamily {
	t.Helper()
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	out := map[string]metricFamily{}
	labels := map[string]map[string]bool{}
	for _, path := range files {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			bl, ok := n.(*ast.BasicLit)
			if !ok || bl.Kind != token.STRING {
				return true
			}
			s, err := strconv.Unquote(bl.Value)
			if err != nil {
				return true
			}
			if m := typeLitRe.FindStringSubmatch(s); m != nil {
				if prev, dup := out[m[1]]; dup {
					t.Fatalf("%s: TYPE line for %s appears twice (types %s, %s)", fset.Position(bl.Pos()), m[1], prev.typ, m[2])
				}
				out[m[1]] = metricFamily{typ: m[2]}
			}
			if m := labelLitRe.FindStringSubmatch(s); m != nil {
				if labels[m[1]] == nil {
					labels[m[1]] = map[string]bool{}
				}
				labels[m[1]][m[2]] = true
			}
			return true
		})
	}
	for name, set := range labels {
		fam, ok := out[name]
		if !ok {
			t.Fatalf("labeled series %s has no # TYPE line in source", name)
		}
		for l := range set {
			fam.labels = append(fam.labels, l)
		}
		sort.Strings(fam.labels)
		out[name] = fam
	}
	return out
}

func goldenLabelValues(t *testing.T) map[string][]string {
	t.Helper()
	b, err := os.ReadFile(metricsGoldenPath)
	if err != nil {
		t.Fatal(err)
	}
	set := map[string]map[string]bool{}
	for _, line := range strings.Split(string(b), "\n") {
		if m := goldenLineRe.FindStringSubmatch(line); m != nil {
			if set[m[1]] == nil {
				set[m[1]] = map[string]bool{}
			}
			set[m[1]][m[3]] = true
		}
	}
	out := map[string][]string{}
	for name, vs := range set {
		for v := range vs {
			out[name] = append(out[name], v)
		}
		sort.Strings(out[name])
	}
	return out
}

func readmeFamilies(t *testing.T) map[string]metricFamily {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "README.md"))
	if err != nil {
		t.Fatal(err)
	}
	const heading = "### Metrics(`/metrics`)"
	text := string(b)
	start := strings.Index(text, heading)
	if start < 0 {
		t.Fatalf("README has no %q section", heading)
	}
	section := text[start+len(heading):]
	if end := strings.Index(section, "\n#"); end >= 0 {
		section = section[:end]
	}
	out := map[string]metricFamily{}
	for _, line := range strings.Split(section, "\n") {
		m := readmeRowRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		fam := metricFamily{typ: strings.ToLower(m[3])}
		if m[2] != "" {
			fam.labels = strings.Split(m[2], ",")
			sort.Strings(fam.labels)
		}
		if vm := readmeValsRe.FindStringSubmatch(line); vm != nil {
			for _, v := range backtickRe.FindAllStringSubmatch(vm[1], -1) {
				fam.values = append(fam.values, v[1])
			}
			sort.Strings(fam.values)
		}
		if _, dup := out[m[1]]; dup {
			t.Fatalf("README lists %s twice", m[1])
		}
		out[m[1]] = fam
	}
	return out
}

func TestMetricsReadmeParity(t *testing.T) {
	src := sourceFamilies(t)
	doc := readmeFamilies(t)
	golden := goldenLabelValues(t)
	// Guard against an empty-vs-empty pass if a regex stops matching.
	if len(src) < 10 || len(doc) == 0 || len(golden) == 0 {
		t.Fatalf("parsed too little: %d source families, %d README rows, %d golden labeled families", len(src), len(doc), len(golden))
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
			t.Errorf("%s is served on /metrics but missing from README", name)
			continue
		case !inSrc:
			t.Errorf("README lists %s but no source file emits it", name)
			continue
		}
		if s.typ != d.typ {
			t.Errorf("%s: README type %q, source %q", name, d.typ, s.typ)
		}
		if !slices.Equal(s.labels, d.labels) {
			t.Errorf("%s: README labels {%s}, source {%s}", name, strings.Join(d.labels, ","), strings.Join(s.labels, ","))
		}
		if d.values == nil {
			continue
		}
		g, ok := golden[name]
		if !ok {
			t.Errorf("%s: README lists label values but metrics.golden renders none to check them against", name)
			continue
		}
		if !slices.Equal(g, d.values) {
			t.Errorf("%s: README values %v, metrics.golden %v", name, d.values, g)
		}
	}
}
