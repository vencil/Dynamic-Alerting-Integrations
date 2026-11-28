package config

import (
	"strings"
	"testing"

	"github.com/prometheus/prometheus/promql/parser"
)

// selectorValueCorpus covers each way a selector value can fail to load:
// a `=~` value Prometheus's matcher constructor refuses, and a rune the PromQL
// lexer refuses inside a string literal (either op) — plus accepted shapes on
// both regex paths (the literal-alternation fast path and the parsed one) and
// the characters escapeSelectorValue rewrites. No template metacharacter: the
// gate refuses those for a reason of its own (rejectTemplateMetachars).
var selectorValueCorpus = []string{
	// accepted
	"", ".*", "5..", "a|b|c", "foo|bar.*", "(?i)foo", `\p{Greek}+`, "(?P<x>a)b",
	"a{1000}", `[\d\-]+`, "^anchored$", `\.`, "(a|b)*c", "a\nb", "a\rb", "a\tb",
	"a\x00b", "a\u2028b", `a"b`, `a\\b`, "naïve", "日本",
	// refused by the regex constructor (`=~` only)
	"(unclosed", "unopened)", "[z-a]", "a**", "a{1001}", "(a{1000}){1000}",
	`\8`, `\C`, `\pX`, "(?P<>a)", "x{2,1}", "[[:bogus:]]", "*a", "(?<x", `\`,
	// refused by the lexer (both ops)
	"\uFFFD", "a\uFFFDb", "\xff", "a\xc3",
}

// What the gate accepts must be exactly what Prometheus's parser loads, for
// the selector as the compiler emits it: ParseExpr lexes the literal and
// builds every matcher (compiling a `=~` one), the steps the rule loader runs.
// Accepting more is the bug this gate closes; accepting less refuses a value
// Prometheus would run.
func TestSelectorValues_MatchPrometheusParser(t *testing.T) {
	promParser := parser.NewParser(parser.Options{})
	refused := map[string]int{}
	for _, op := range []string{"=", "=~"} {
		for _, v := range selectorValueCorpus {
			spec := CustomAlertSpec{}
			if op == "=" {
				spec.Selectors = map[string]string{"job": v}
			} else {
				spec.SelectorsRe = map[string]string{"job": v}
			}
			_, gateErr := selectorItems(spec)
			emitted := "m" + customAlertShape{Items: []selectorItem{{op, "job", v}}}.selector()
			_, parseErr := promParser.ParseExpr(emitted)
			if (gateErr == nil) != (parseErr == nil) {
				t.Errorf("%s %q: emitted %s; parser err=%v, gate err=%v", op, v, emitted, parseErr, gateErr)
			}
			if parseErr != nil {
				refused[op]++
			}
		}
	}
	if refused["="] < 4 || refused["=~"] < 15 {
		t.Fatalf("corpus refusals = %v, want >= 4 for = and >= 15 for =~ (corpus undershot)", refused)
	}
}

const badRegexEntry = "      - {recipe: rate, name: r, metric: http_requests_total, " +
	"selectors_re: {job: \"(unclosed\"}, op: \">\", window: 5m, threshold: \"1:warning\"}\n"

// tenant-api's write preflight: one indexed violation naming the field.
func TestSelectorRegex_TenantAPIViolation(t *testing.T) {
	cfg := customAlertsConfig(t, "shop-a", badRegexEntry)
	got := ValidateTenantCustomAlerts("shop-a", cfg.Tenants["shop-a"], 20)
	if len(got) != 1 || !strings.Contains(got[0], "_custom_alerts[0]") ||
		!strings.Contains(got[0], "not a valid regular expression") {
		t.Fatalf("want 1 indexed regex violation, got %v", got)
	}
}

// The exporter serves no row for it and counts one error; the compiler, which
// compiles served and switched-off entries only, sees it rejected.
func TestSelectorRegex_ExporterAndCompilerRefuse(t *testing.T) {
	cfg := customAlertsConfig(t, "t1", badRegexEntry)
	rows, _, errs := resolveTenantCustomAlerts("t1", cfg.Tenants["t1"])
	if errs != 1 || len(rows) != 0 {
		t.Fatalf("exporter: want 1 error + 0 rows, got errs=%d rows=%d", errs, len(rows))
	}
	entries, blockErr := CustomAlertEntries("t1", cfg.Tenants["t1"])
	if blockErr != "" || len(entries) != 1 || entries[0].Status != CustomAlertRejected {
		t.Fatalf("compiler input: want 1 rejected entry, got %+v (block err %q)", entries, blockErr)
	}
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "t1", Entries: entries}})
	if err != nil {
		t.Fatal(err)
	}
	if p.Shapes != 0 {
		t.Fatalf("compiler emitted %d shape(s) for a rejected entry", p.Shapes)
	}
}

// A switched-off entry is refused too: the shape is judged before the
// opt-out, so no rule is kept ready for a regex that could never load.
func TestSelectorRegex_SwitchedOffEntryAlsoRejected(t *testing.T) {
	off := strings.Replace(badRegexEntry, `threshold: "1:warning"`, `threshold: "disable"`, 1)
	cfg := customAlertsConfig(t, "t1", off)
	entries, _ := CustomAlertEntries("t1", cfg.Tenants["t1"])
	if len(entries) != 1 || entries[0].Status != CustomAlertRejected {
		t.Fatalf("want 1 rejected entry, got %+v", entries)
	}
}
