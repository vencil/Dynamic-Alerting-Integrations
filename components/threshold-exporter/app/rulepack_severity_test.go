package main

// rulepack_severity_test.go — the severities the rule packs select on
// user_threshold are config.ThresholdSeverities(), exactly (#3071).
//
// The exporter serves a threshold row only with a severity in that set (a
// value with any other suffix is unreadable: pkg/config/threshold_value.go).
// That is right only while the set is what the threshold rules compare: a
// severity a rule selects but the exporter refuses could never fire, and one
// the exporter serves but no rule selects is the silent row #3071 removed.
// So every user_threshold `severity` matcher in every shipped copy of the
// rule packs — rule-packs/, the operator PrometheusRule CRs, the k8s
// ConfigMaps — is collected through the PromQL AST and the two sets are held
// equal in both directions.

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/prometheus/model/labels"
	"github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// ruleExprs returns every rule `expr` string in a decoded YAML document,
// descending into a string that is itself a rule file (a ConfigMap's data).
func ruleExprs(v any) []string {
	var out []string
	switch x := v.(type) {
	case map[string]any:
		for k, e := range x {
			if s, ok := e.(string); ok && k == "expr" {
				out = append(out, s)
				continue
			}
			out = append(out, ruleExprs(e)...)
		}
	case []any:
		for _, e := range x {
			out = append(out, ruleExprs(e)...)
		}
	case string:
		if strings.Contains(x, "groups:") {
			var inner any
			if err := yaml.Unmarshal([]byte(x), &inner); err == nil {
				out = append(out, ruleExprs(inner)...)
			}
		}
	}
	return out
}

func TestRulePackSeveritySelectorsAreTheThresholdSeverities(t *testing.T) {
	root := findRepoRoot(t)
	var files []string
	// Each family's declared rule-file globs (`.yaml` and `.yml`; see
	// globDeclared); a family that matches nothing is fatal.
	for _, family := range [][]string{
		{"rule-packs/rule-pack-*.yaml", "rule-packs/rule-pack-*.yml"},
		{"operator-manifests/da-rule-pack-*.yaml", "operator-manifests/da-rule-pack-*.yml"},
		{"k8s/03-monitoring/configmap-rules-*.yaml", "k8s/03-monitoring/configmap-rules-*.yml"},
	} {
		got := globDeclared(t, root, family...)
		if len(got) == 0 {
			t.Fatalf("glob %v: no files — the scan would pass over nothing", family)
		}
		files = append(files, got...)
	}

	selected := map[string]int{}
	perDir := map[string]int{}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var doc any
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		rel, _ := filepath.Rel(root, f)
		for _, expr := range ruleExprs(doc) {
			for _, vs := range userThresholdSelectors(t, rel, expr) {
				for _, m := range vs.LabelMatchers {
					if m.Name != "severity" {
						continue
					}
					if m.Type != labels.MatchEqual {
						t.Errorf("%s: user_threshold severity matcher must be exact (=): %s", rel, vs.String())
						continue
					}
					selected[m.Value]++
					perDir[filepath.Dir(rel)]++
				}
			}
		}
	}
	for _, dir := range []string{"rule-packs", "operator-manifests", "k8s/03-monitoring"} {
		if perDir[dir] == 0 {
			t.Errorf("%s: no user_threshold severity selector found — the scan undershot", dir)
		}
	}
	var got []string
	for s := range selected {
		got = append(got, s)
	}
	want := config.ThresholdSeverities()
	sort.Strings(got)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("rule packs select user_threshold severities %v (counts %v); the exporter serves %v "+
			"(config.ThresholdSeverities) — a severity in one and not the other is an alert that never fires",
			got, selected, want)
	}
	t.Logf("severity selectors: %v over %d files", selected, len(files))
}
