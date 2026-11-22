package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

	promparser "github.com/prometheus/prometheus/promql/parser"
	"github.com/prometheus/prometheus/util/features"
)

// upstreamFeatureCases reads the linked Prometheus' own
// cmd/prometheus/main.go and returns, per option setFeatureListOptions'
// switch handles, the parser.Options fields its case sets
// (c.parserOpts.<Field> = …) — read as Go syntax, so a comment or a log
// message naming an option cannot stand in for a case.
func upstreamFeatureCases(t *testing.T) map[string][]string {
	t.Helper()
	out, err := exec.Command("go", "list", "-m", "-f", "{{.Dir}}", "github.com/prometheus/prometheus").Output()
	if err != nil {
		t.Fatalf("go list -m github.com/prometheus/prometheus: %v", err)
	}
	path := filepath.Join(strings.TrimSpace(string(out)), "cmd", "prometheus", "main.go")
	file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	var fn *ast.FuncDecl
	for _, d := range file.Decls {
		if f, ok := d.(*ast.FuncDecl); ok && f.Name.Name == "setFeatureListOptions" {
			fn = f
		}
	}
	if fn == nil {
		t.Fatalf("%s has no setFeatureListOptions: read how this Prometheus release takes --enable-feature", path)
	}
	cases := map[string][]string{}
	ast.Inspect(fn, func(n ast.Node) bool {
		cc, ok := n.(*ast.CaseClause)
		if !ok {
			return true
		}
		var names []string
		for _, e := range cc.List {
			lit, ok := e.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				t.Fatalf("%s: a case of setFeatureListOptions is not a string literal", path)
			}
			s, _ := strconv.Unquote(lit.Value)
			names = append(names, s)
		}
		fields := []string{}
		for _, stmt := range cc.Body {
			ast.Inspect(stmt, func(m ast.Node) bool {
				as, ok := m.(*ast.AssignStmt)
				if !ok {
					return true
				}
				for _, lhs := range as.Lhs {
					sel, ok := lhs.(*ast.SelectorExpr)
					if !ok {
						continue
					}
					if inner, ok := sel.X.(*ast.SelectorExpr); ok && inner.Sel.Name == "parserOpts" {
						fields = append(fields, sel.Sel.Name)
					}
				}
				return true
			})
		}
		for _, name := range names {
			cases[name] = fields
		}
		return true
	})
	if len(cases) == 0 {
		t.Fatalf("%s: setFeatureListOptions handles no option: its shape changed", path)
	}
	return cases
}

// TestFeatureTableIsPrometheusOwn: featureTable has exactly the options the
// linked Prometheus handles, and an option is "parser" exactly when
// Prometheus sets a parser option for it — that very field.
func TestFeatureTableIsPrometheusOwn(t *testing.T) {
	cases := upstreamFeatureCases(t)
	for name, fields := range cases {
		fb, ok := featureTable[name]
		if !ok {
			t.Errorf("--enable-feature=%s: Prometheus handles it, featureTable has no entry: decide how it bears on what da-read reads", name)
			continue
		}
		switch {
		case len(fields) > 1:
			t.Errorf("--enable-feature=%s sets several parser options %v: featureTable holds one", name, fields)
		case len(fields) == 1 && (fb.bearing != bearingParser || fb.field != fields[0]):
			t.Errorf("--enable-feature=%s sets parser.Options.%s; featureTable says %q/%q", name, fields[0], fb.bearing, fb.field)
		case len(fields) == 0 && fb.bearing == bearingParser:
			t.Errorf("--enable-feature=%s sets no parser option; featureTable says parser", name)
		}
	}
	for name := range featureTable {
		if _, ok := cases[name]; !ok {
			t.Errorf("featureTable has --enable-feature=%s, which this Prometheus does not handle", name)
		}
	}
}

// TestParserOptionsSetTheNamedField: each "parser" option sets exactly the
// field its entry names, and nothing else does.
func TestParserOptionsSetTheNamedField(t *testing.T) {
	for name, fb := range featureTable {
		opts, err := parserOptions([]string{name})
		if fb.bearing != bearingParser {
			if err == nil {
				t.Errorf("parserOptions(%s) accepted a %s option", name, fb.bearing)
			}
			continue
		}
		if err != nil {
			t.Fatalf("parserOptions(%s): %v", name, err)
		}
		v := reflect.ValueOf(opts)
		var set []string
		for i := 0; i < v.NumField(); i++ {
			if !v.Field(i).IsZero() {
				set = append(set, v.Type().Field(i).Name)
			}
		}
		if !reflect.DeepEqual(set, []string{fb.field}) {
			t.Errorf("parserOptions(%s) set %v, want [%s]", name, set, fb.field)
		}
	}
	if _, err := parserOptions([]string{"no-such-option"}); err == nil {
		t.Error("parserOptions accepted an option Prometheus does not have")
	}
}

// registered is what a parser reports it accepts (RegisterFeatures: every
// keyword, operator, aggregation and function, each enabled or not, and the
// unquoted-UTF-8-names switch) — how a parser built from each parser option
// differs from the zero one.
func registered(p promparser.Parser) map[string]map[string]bool {
	r := features.NewRegistry()
	p.RegisterFeatures(r)
	return r.Get()
}

// TestRuleParserIsBuiltFromDeployedFeatures: deployedFeatures holds only
// parser options, and ruleParser accepts exactly what a parser built from
// them does — every option, not one probe.
func TestRuleParserIsBuiltFromDeployedFeatures(t *testing.T) {
	want, err := parserOptions(deployedFeatures)
	if err != nil {
		t.Fatalf("deployedFeatures: %v", err)
	}
	if got, exp := registered(ruleParser), registered(promparser.NewParser(want)); !reflect.DeepEqual(got, exp) {
		t.Errorf("ruleParser is not the parser deployedFeatures %v builds", deployedFeatures)
	}
	// The comparison can tell the options apart: each parser option changes
	// what a parser reports.
	zero := registered(promparser.NewParser(promparser.Options{}))
	for name, fb := range featureTable {
		if fb.bearing != bearingParser {
			continue
		}
		if reflect.DeepEqual(registered(promparser.NewParser(mustParserOptions([]string{name}))), zero) {
			t.Errorf("a parser built with --enable-feature=%s reports what the zero parser does", name)
		}
	}
}

func TestReadPrometheusFeatures(t *testing.T) {
	out, err := readPrometheusFeatures([]string{"promql-binop-fill-modifiers,old-ui", "promql-delayed-name-removal", "nope"})
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		Documents []struct {
			Features map[string]string `json:"features"`
			Parser   []string          `json:"parser"`
		} `json:"documents"`
	}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"promql-binop-fill-modifiers": bearingParser,
		"old-ui":                      bearingNotRead,
		"promql-delayed-name-removal": bearingNotModelled,
		"nope":                        bearingUnknown,
	}
	if len(got.Documents) != 1 || !reflect.DeepEqual(got.Documents[0].Features, want) {
		t.Errorf("features = %+v, want %v", got.Documents, want)
	}
	deployed := append([]string{}, deployedFeatures...)
	sort.Strings(deployed)
	if got.Documents[0].Parser == nil || !reflect.DeepEqual(append([]string{}, got.Documents[0].Parser...), deployed) {
		t.Errorf("parser = %#v, want deployedFeatures %v (an empty array, never null)", got.Documents[0].Parser, deployed)
	}
}
