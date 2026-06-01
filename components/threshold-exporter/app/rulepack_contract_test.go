package main

// rulepack_contract_test.go — exporter↔rule-pack metric-label contract (#731).
//
// Root cause of #731: rule-pack threshold-normalization recording rules queried
// `user_threshold{metric="<full conf.d key>"}` while the exporter actually emits
// `user_threshold{component=<prefix>, metric=<stripped>}` (collector.go +
// config.ParseMetricKey split on the first "_"). The two never matched → the
// `tenant:alert_threshold:<key>` recording rule was an empty set → alerts never
// fired (fail-silent). promtool fixtures hid it because they hand-wrote the
// `user_threshold` shape to match the (broken) query rather than the real
// exporter output — an echo chamber.
//
// This test is the anti-echo-chamber backstop. It walks EVERY `user_threshold`
// vector selector in EVERY rule pack via the real Prometheus PromQL AST parser
// (not regex — immune to multi-line / or / unless / group_left layout) and
// asserts each selects on BOTH `component` and `metric` with EXACT (=) matchers.
// For selectors inside a `tenant[_version]:alert_threshold:<K>` recording rule it
// additionally asserts (component, metric) == config.ParseMetricKey(<K>) — using
// the SAME Go function the exporter uses, so there is zero cross-language
// reimplementation that could drift (a Python re-impl of the split would just be
// a new echo chamber — exactly the bug we are killing).
//
// Companion: TestRulePackFixturesCarryComponent guards the promtool fixtures
// from regressing to the component-less shape that hid the original bug.

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/prometheus/prometheus/model/labels"
	promqlparser "github.com/prometheus/prometheus/promql/parser"
	"github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// promParser is a shared PromQL parser (promQLParser is a stateless struct;
// safe to reuse across sequential ParseExpr calls in this test).
var promParser = promqlparser.NewParser(promqlparser.Options{})

// thresholdRecordRe matches a threshold-normalization recording rule name and
// captures the conf.d key suffix <K>. Both the tenant-scoped (`tenant:`) and the
// ADR-024 version-aware (`tenant_version:`) forms are in scope.
var thresholdRecordRe = regexp.MustCompile(`^tenant(?:_version)?:alert_threshold:(.+)$`)

// versionAwareAllowlist is the ADR-024 Phase-1 set of (component/metric)
// identities permitted to carry a dimensional version (mirrors
// pkg/config/resolve.go pilotVersionMetrics). Only these may legitimately have a
// digit-leading or otherwise version-shaped metric in the future; everything
// else must keep version in a `version=` label, never baked into the conf.d key.
var versionAwareAllowlist = map[string]bool{
	"container/cpu":    true,
	"container/memory": true,
}

// permittedThresholdLabels is the closed set of label matchers a user_threshold
// selector may carry. The exporter only emits {tenant, component, metric,
// severity} (+ ADR-024 version, + dimensional custom labels which the
// threshold-normalization rules do not query). ANY other narrowing matcher
// (e.g. a stray `instance="x"`) selects an empty set in prod — the exact
// silent-alert failure class of #731 — yet would otherwise pass the
// component/metric check. So reject unknown matchers outright (review-gate:
// adding a genuinely-needed dimensional label here is a deliberate edit).
var permittedThresholdLabels = map[string]bool{
	"__name__":  true, // implicit metric-name matcher the PromQL parser always adds
	"component": true,
	"metric":    true,
	"severity":  true,
	"version":   true,
	"tenant":    true, // a selector may legitimately pin tenant (none do today)
}

// minimum coverage floors — a green run over an EMPTY set is itself a new echo
// chamber, so the test fails loudly if it discovers far fewer rules than exist
// today (14 packs; ~63 threshold recording rules). These are conservative
// floors, not exact counts, so adding/removing a metric does not false-fail.
const (
	minRulePacks            = 14
	minThresholdSelectors   = 44
	minFixtureUserThreshold = 10
)

type rulePackDoc struct {
	Groups []struct {
		Name  string `yaml:"name"`
		Rules []struct {
			Record string `yaml:"record"`
			Alert  string `yaml:"alert"`
			Expr   string `yaml:"expr"`
		} `yaml:"rules"`
	} `yaml:"groups"`
}

// findRepoRoot walks up from the test working directory until it finds the
// directory that contains both `rule-packs/` and `Makefile` (the repo root in
// both a normal checkout and a git worktree). Robust to the test CWD and never
// silently returns a wrong dir.
func findRepoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if isDir(filepath.Join(dir, "rule-packs")) && isFile(filepath.Join(dir, "Makefile")) {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root (a dir with rule-packs/ + Makefile) walking up from %s", dir)
		}
		dir = parent
	}
}

func isDir(p string) bool  { fi, err := os.Stat(p); return err == nil && fi.IsDir() }
func isFile(p string) bool { fi, err := os.Stat(p); return err == nil && !fi.IsDir() }

// matcherValue returns the value of the named label matcher on a vector
// selector, or "" if absent.
func matcherValue(vs *promqlparser.VectorSelector, name string) string {
	for _, m := range vs.LabelMatchers {
		if m.Name == name {
			return m.Value
		}
	}
	return ""
}

// userThresholdSelectors parses a PromQL expr and returns every user_threshold
// vector selector it contains (via AST, so multi-line / or / unless / group_left
// joins are all handled structurally).
func userThresholdSelectors(t *testing.T, where, expr string) []*promqlparser.VectorSelector {
	t.Helper()
	parsed, err := promParser.ParseExpr(expr)
	if err != nil {
		t.Errorf("%s: rule expr is not valid PromQL: %v\nexpr: %s", where, err, expr)
		return nil
	}
	var out []*promqlparser.VectorSelector
	promqlparser.Inspect(parsed, func(node promqlparser.Node, _ []promqlparser.Node) error {
		if vs, ok := node.(*promqlparser.VectorSelector); ok && vs.Name == "user_threshold" {
			out = append(out, vs)
		}
		return nil
	})
	return out
}

// assertComponentMetric enforces the core contract on one user_threshold
// selector: it must carry BOTH a `component` and a `metric` matcher, each an
// EXACT (=) match. Returns the matched values for the optional ParseMetricKey
// round-trip check. ok=false means a structural violation was already reported.
func assertComponentMetric(t *testing.T, where string, vs *promqlparser.VectorSelector) (component, metric string, ok bool) {
	t.Helper()
	var hasComp, hasMetric bool
	ok = true
	for _, m := range vs.LabelMatchers {
		switch m.Name {
		case "component":
			hasComp = true
			component = m.Value
			if m.Type != labels.MatchEqual {
				t.Errorf("%s: component matcher must be exact (=), got %q in %s", where, m.Type, vs.String())
				ok = false
			}
		case "metric":
			hasMetric = true
			metric = m.Value
			if m.Type != labels.MatchEqual {
				t.Errorf("%s: metric matcher must be exact (=), got %q in %s", where, m.Type, vs.String())
				ok = false
			}
		}
	}
	if !hasComp {
		t.Errorf("%s: user_threshold selector is missing a `component` matcher (the #731 bug shape): %s", where, vs.String())
		ok = false
	}
	if !hasMetric {
		t.Errorf("%s: user_threshold selector is missing a `metric` matcher: %s", where, vs.String())
		ok = false
	}
	// Closed-label guard (review M2): an UNEXPECTED narrowing matcher selects an
	// empty set in prod — the same silent-alert failure class as #731 — but would
	// otherwise sail past the component/metric check. Reject any matcher outside
	// the permitted set.
	for _, m := range vs.LabelMatchers {
		if !permittedThresholdLabels[m.Name] {
			t.Errorf("%s: user_threshold selector carries unexpected matcher %q — the exporter never "+
				"emits it, so this would match an empty set in prod (the #731 failure class); selector %s",
				where, m.Name, vs.String())
			ok = false
		}
	}
	// Forward-looking naming guard (ADR-024): a version belongs in a `version=`
	// label, never baked into the metric key. A digit-leading stripped metric
	// (e.g. conf.d key `redis_6_connected_clients` → metric `6_connected_clients`)
	// is almost certainly such a leak. Allow only the piloted version-aware pair.
	if hasMetric && metric != "" && metric[0] >= '0' && metric[0] <= '9' &&
		!versionAwareAllowlist[component+"/"+metric] {
		t.Errorf("%s: metric %q is digit-leading — a version must live in a `version=` label, "+
			"not in the conf.d key/metric (ADR-024); selector %s", where, metric, vs.String())
		ok = false
	}
	return component, metric, ok
}

func TestRulePackExporterContract(t *testing.T) {
	root := findRepoRoot(t)
	packs, err := filepath.Glob(filepath.Join(root, "rule-packs", "rule-pack-*.yaml"))
	if err != nil {
		t.Fatalf("glob rule packs: %v", err)
	}

	packCount := 0
	thresholdSelectorCount := 0

	for _, packPath := range packs {
		raw, err := os.ReadFile(packPath)
		if err != nil {
			t.Errorf("read %s: %v", packPath, err)
			continue
		}
		var doc rulePackDoc
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			t.Errorf("unmarshal %s: %v", packPath, err)
			continue
		}
		packCount++
		base := filepath.Base(packPath)

		for _, g := range doc.Groups {
			for _, r := range g.Rules {
				if r.Expr == "" {
					continue
				}
				where := base + " [" + g.Name + "] " + ruleID(r.Record, r.Alert)
				for _, vs := range userThresholdSelectors(t, where, r.Expr) {
					component, metric, ok := assertComponentMetric(t, where, vs)
					// If this selector lives in a threshold-normalization recording
					// rule, its (component, metric) must equal the exporter's real
					// split of the record's conf.d key — the load-bearing #731 check.
					if m := thresholdRecordRe.FindStringSubmatch(r.Record); m != nil {
						thresholdSelectorCount++
						if !ok {
							continue
						}
						// The record-name suffix is the conf.d key, EXCEPT the
						// multi-tier `_critical` convention: the exporter treats a
						// `<base>_critical` override as severity=critical of <base>
						// (resolve.go TrimSuffix), emitting metric=ParseMetricKey(<base>)
						// — NOT a literal `..._critical` metric. So mirror that here:
						// a `_critical` record carries the severity in a label, and its
						// metric is the base key's stripped metric.
						baseKey := m[1]
						sev := matcherValue(vs, "severity")
						if strings.HasSuffix(baseKey, "_critical") {
							baseKey = strings.TrimSuffix(baseKey, "_critical")
							if sev != "critical" {
								t.Errorf("%s: record name ends in _critical but selector severity=%q "+
									"(expected \"critical\" — the _critical suffix is a severity marker, not part of the metric)",
									where, sev)
							}
						} else if sev != "" && sev != "warning" {
							// review M1: a warning-tier record that PINS a severity must pin
							// "warning" (else it reads the wrong tier's threshold). A missing
							// severity matcher is allowed — ADR-024 version-aware normalize
							// aggregates `by(..., severity)` and pins severity in the alert.
							t.Errorf("%s: warning-tier record selector pins severity=%q (expected \"warning\" or no severity matcher)",
								where, sev)
						}
						wantC, wantM := config.ParseMetricKey(baseKey)
						if component != wantC || metric != wantM {
							t.Errorf("%s: selector {component=%q, metric=%q} does not match "+
								"ParseMetricKey(%q)={component=%q, metric=%q} — exporter emits the latter, "+
								"so this recording rule would be an empty set (the #731 silent-alert bug)",
								where, component, metric, baseKey, wantC, wantM)
						}
					}
				}
			}
		}
	}

	// Anti-echo-chamber: a green run over an empty/under-discovered set is the
	// exact failure mode #731 is about. Fail loudly if discovery undershoots.
	if packCount < minRulePacks {
		t.Fatalf("discovered only %d rule packs (< %d expected) — contract scan undershot; "+
			"a green run over too few packs is itself a new echo chamber", packCount, minRulePacks)
	}
	if thresholdSelectorCount < minThresholdSelectors {
		t.Fatalf("matched only %d threshold-normalization user_threshold selectors (< %d expected) — "+
			"contract scan undershot", thresholdSelectorCount, minThresholdSelectors)
	}
	t.Logf("contract OK: %d packs, %d threshold-normalization selectors validated", packCount, thresholdSelectorCount)
}

func ruleID(record, alert string) string {
	if record != "" {
		return "record=" + record
	}
	if alert != "" {
		return "alert=" + alert
	}
	return "<anonymous rule>"
}

// --- Fixture guard: promtool input_series must use the real exporter shape ---

type promtoolTestDoc struct {
	Tests []struct {
		InputSeries []struct {
			Series string `yaml:"series"`
		} `yaml:"input_series"`
	} `yaml:"tests"`
}

// TestRulePackFixturesCarryComponent asserts every `user_threshold{...}` series
// in the promtool fixtures carries a `component` (and `metric`) exact matcher.
// This locks the fixtures to the real exporter output shape so they can never
// silently regress to the component-less form that masked #731.
func TestRulePackFixturesCarryComponent(t *testing.T) {
	root := findRepoRoot(t)
	fixtures, err := filepath.Glob(filepath.Join(root, "tests", "rulepacks", "*_test.yaml"))
	if err != nil {
		t.Fatalf("glob fixtures: %v", err)
	}

	seriesChecked := 0
	for _, fx := range fixtures {
		raw, err := os.ReadFile(fx)
		if err != nil {
			t.Errorf("read %s: %v", fx, err)
			continue
		}
		var doc promtoolTestDoc
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			t.Errorf("unmarshal %s: %v", fx, err)
			continue
		}
		base := filepath.Base(fx)
		for _, tc := range doc.Tests {
			for _, s := range tc.InputSeries {
				if s.Series == "" {
					continue
				}
				for _, vs := range userThresholdSelectors(t, base+" series", s.Series) {
					seriesChecked++
					assertComponentMetric(t, base+" series "+s.Series, vs)
				}
			}
		}
	}

	if seriesChecked < minFixtureUserThreshold {
		t.Fatalf("checked only %d user_threshold fixture series (< %d expected) — "+
			"fixture discovery undershot", seriesChecked, minFixtureUserThreshold)
	}
	t.Logf("fixture guard OK: %d user_threshold series carry component+metric", seriesChecked)
}
