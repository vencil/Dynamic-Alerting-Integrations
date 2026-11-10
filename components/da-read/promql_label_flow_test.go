package main

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/prometheus/promql/parser"
	"github.com/prometheus/prometheus/promql/promqltest"
)

// labelFlowCases: the left side of the outermost `unless on(tenant)` and
// what `carries` must say about `tenant`. Every case is also EVALUATED by
// Prometheus' engine below, so the static answer is pinned to what the
// engine does, not to this file's reading of engine.go.
var labelFlowCases = []struct {
	name, lhs string
	want      flow
}{
	{"selector", `up == 0`, flowKept},
	{"by tenant", `max by(tenant) (up) == 0`, flowKept},
	{"by other", `max by(job) (up) == 0`, flowDropped},
	{"without tenant (#3678 F2)", `sum without(tenant) (up == 0)`, flowDropped},
	{"without other", `sum without(job) (up == 0)`, flowKept},
	{"plain sum", `sum(up) == 0`, flowDropped},
	{"topk keeps inputs", `topk by(job) (1, up)`, flowKept},
	{"vector", `vector(1)`, flowDropped},
	{"rate", `rate(c[5m]) > 0`, flowKept},
	{"absent with tenant matcher", `absent(nope{tenant="t1"})`, flowKept},
	{"absent without", `absent(nope{job="x"})`, flowDropped},
	{"label_replace other", `label_replace(up, "x", "1", "", "")`, flowKept},
	{"label_replace tenant", `label_replace(up, "tenant", "", "", "")`, flowUnknown},
	{"tenant copied whole from namespace", `label_replace(sum by(namespace) (up), "tenant", "$1", "namespace", "(.*)")`, flowKept},
	{"tenant copied from a non-alias label (CodeRabbit)", `label_replace(sum by(job) (up), "tenant", "$1", "job", "(.*)")`, flowUnknown},
	{"(.*) over a dropped namespace empties tenant", `label_replace(sum by(tenant) (up{tenant="t1"}), "tenant", "$1", "namespace", "(.*)")`, flowDropped},
	{"(.+) over a dropped namespace leaves tenant (CodeRabbit)", `label_replace(sum by(tenant) (up), "tenant", "$1", "namespace", "(.+)")`, flowKept},
	{"absent of an aggregation has no labels (CodeRabbit)", `absent(sum(nope{tenant="t1"}))`, flowDropped},
	{"tenant from a partial regex", `label_replace(up, "tenant", "$1", "job", "j(.*)")`, flowUnknown},
	{"on tenant one-to-one", `up >= on(tenant, job) up`, flowKept},
	{"on other one-to-one", `up >= on(job) up`, flowDropped},
	{"ignoring tenant", `up >= ignoring(tenant) up`, flowDropped},
	{"ignoring other", `up >= ignoring(instance) up`, flowKept},
	{"group_left keeps many side", `up * on(job) group_left max by(job) (up)`, flowKept},
	{"group_left include tenant from one side", `sum by(job) (up) * on(job) group_left(tenant) up`, flowKept},
	{"group_right many side lost it", `max by(job) (up) * on(job) group_right sum by(job) (up)`, flowDropped},
	{"scalar arithmetic", `up * 2`, flowKept},
	{"or with a dropped side (review)", `up or sum(up)`, flowDropped},
	{"or both kept", `up or c`, flowKept},
	{"count_values into tenant (review)", `count_values("tenant", up)`, flowUnknown},
	{"count_values into another label", `count_values("v", up)`, flowDropped},
	{"or both dropped", `sum(up) or vector(1)`, flowDropped},
	{"and keeps left", `sum(up) and on() up`, flowDropped},
	{"paren", `(max by(tenant) (up))`, flowKept},
}

const labelFlowSeries = `
load 1m
  up{tenant="t1", job="j1", instance="i1", ns="t1", namespace="t1"} 0 0 0 0 0 0 0 0 0 0
  up{tenant="t2", job="j2", instance="i2", ns="t2", namespace="t2"} 0 0 0 0 0 0 0 0 0 0
  c{tenant="t1", job="j1"} 0 1 2 3 4 5 6 7 8 9
`

func TestCarriesMatchesTheEngine(t *testing.T) {
	t.Parallel()
	storage := promqltest.LoadedStorage(t, labelFlowSeries)
	engine := promqltest.NewTestEngine(t, false, 5*time.Minute, promqltest.DefaultMaxSamplesPerQuery)
	at := time.Unix(0, 0).Add(9 * time.Minute)
	for _, c := range labelFlowCases {
		// Parenthesised: `and`/`unless` bind tighter than `or`, so a bare
		// `a or b unless …` would test only `b`.
		hits := newFlow(nil).setMatchesOn(mustParse(t, "("+c.lhs+`) unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`))
		if len(hits) != 1 || hits[0].LHS != c.want.String() {
			t.Errorf("%s: got %+v, want one hit with lhs %s", c.name, hits, c.want)
			continue
		}
		if c.want == flowUnknown {
			continue
		}
		q, err := engine.NewInstantQuery(context.Background(), storage, nil, c.lhs, at)
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		res := q.Exec(context.Background())
		if res.Err != nil {
			t.Fatalf("%s: %v", c.name, res.Err)
		}
		vec, err := res.Vector()
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if len(vec) == 0 {
			t.Fatalf("%s: the engine returned nothing; the case proves nothing", c.name)
		}
		// The property the clause relies on: EVERY series keeps a tenant of
		// the input (kept), or some series has none (dropped).
		var lacking, foreign int
		for _, smp := range vec {
			switch v := smp.Metric.Get("tenant"); v {
			case "":
				lacking++
			case "t1", "t2":
			default:
				foreign++
			}
		}
		if c.want == flowKept && (lacking > 0 || foreign > 0) {
			t.Errorf("%s: static kept, but %d series lack tenant and %d carry one not of the input: %v", c.name, lacking, foreign, vec)
		}
		if c.want == flowDropped && lacking == 0 {
			t.Errorf("%s: static dropped, but every series the engine returned has tenant: %v", c.name, vec)
		}
		q.Close()
	}
}

func mustParse(t *testing.T, expr string) parser.Expr {
	t.Helper()
	n, err := ruleParser.ParseExpr(expr)
	if err != nil {
		t.Fatalf("%s: %v", expr, err)
	}
	return n
}

func TestLabelFlowOnlyReportsSetOpsOnTheLabel(t *testing.T) {
	t.Parallel()
	for expr, n := range map[string]int{
		`a unless on(tenant) b`:                       1,
		`a and on(tenant, job) b`:                     1,
		`a unless b`:                                  0,
		`a unless on(job) b`:                          0,
		`a * on(tenant) b`:                            0,
		`(a unless on(tenant) b) unless on(tenant) c`: 2,
	} {
		node, err := ruleParser.ParseExpr(expr)
		if err != nil {
			t.Fatal(err)
		}
		if got := len(newFlow(nil).setMatchesOn(node)); got != n {
			t.Errorf("%s: %d hits, want %d", expr, got, n)
		}
	}
}

func TestPromQLLabelFlowCommand(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	in := `{"expressions": ["sum without(tenant) (up) unless on(tenant) (user_state_filter{filter=\"maintenance\"} == 1)"], "records": {}}`
	if rc := run([]string{"promql-label-flow", "tenant", "-"}, strings.NewReader(in), &stdout, &stderr); rc != exitOK {
		t.Fatalf("rc %d: %s", rc, stderr.String())
	}
	var got struct {
		Documents [][][]setMatch `json:"documents"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Documents) != 1 || len(got.Documents[0]) != 1 || len(got.Documents[0][0]) != 1 ||
		got.Documents[0][0][0].LHS != "dropped" || got.Documents[0][0][0].Op != "unless" {
		t.Fatalf("got %s", stdout.String())
	}
	if rc := run([]string{"promql-label-flow", "tenant", "-"}, strings.NewReader(`{"expressions": ["up +"]}`), &stdout, &stderr); rc != exitCallerErr {
		t.Fatalf("an expression the parser refuses: rc %d, want %d", rc, exitCallerErr)
	}
}

func newFlow(records map[string]string) *flowReader {
	f := &flowReader{label: "tenant", records: map[string][]recordDef{}, visiting: map[string]bool{},
		aliases: map[string]bool{"namespace": true}}
	for name, e := range records {
		n, err := ruleParser.ParseExpr(e)
		if err != nil {
			panic(err)
		}
		f.records[name] = append(f.records[name], recordDef{expr: n})
	}
	return f
}

// TestRecordLabelsOverride: a recording rule's own `labels:` set or remove
// the label on every series it writes (CodeRabbit on #3690).
func TestRecordLabelsOverride(t *testing.T) {
	t.Parallel()
	clause := ` unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`
	for _, c := range []struct {
		lbls map[string]string
		want flow
	}{
		{map[string]string{"tenant": ""}, flowDropped},
		{map[string]string{"tenant": "t1"}, flowKept},
		{map[string]string{"x": "y"}, flowKept},
	} {
		f := newFlow(nil)
		f.records["r:x"] = []recordDef{{expr: mustParse(t, `max by(tenant) (up)`), labels: c.lbls}}
		hits := f.setMatchesOn(mustParse(t, `r:x`+clause))
		if len(hits) != 1 || hits[0].LHS != c.want.String() {
			t.Errorf("labels %v: got %+v, want %s", c.lbls, hits, c.want)
		}
	}
}

// TestFunctionsThatDropALabel: histogram_quantile and histogram_fraction
// drop `le` (CodeRabbit on #3690); for any other label they follow the vector.
func TestFunctionsThatDropALabel(t *testing.T) {
	t.Parallel()
	e := mustParse(t, `histogram_quantile(0.9, rate(b[5m]))`)
	if got := newFlow(nil).forLabel("le").carries(e); got != flowDropped {
		t.Errorf("le: got %s, want dropped", got)
	}
	if got := newFlow(nil).carries(e); got != flowKept {
		t.Errorf("tenant: got %s, want kept", got)
	}
}

// TestRecordsAreFollowed: a record that aggregated tenant away cannot hide
// it from the clause reading it (review of #3678: the F2 shape moved one
// rule away — and every compiled custom alert reads a record).
func TestRecordsAreFollowed(t *testing.T) {
	t.Parallel()
	recs := map[string]string{
		"job:probe_up:min":    `min by(job) (up)`,
		"tenant:probe_up:min": `min by(tenant) (up)`,
		"a:loop":              `b:loop`,
		"b:loop":              `a:loop`,
	}
	clause := ` unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`
	for lhs, want := range map[string]flow{
		`(job:probe_up:min == 0 or up == 0)`: flowDropped,
		`job:probe_up:min == 0`:              flowDropped,
		`tenant:probe_up:min == 0`:           flowKept,
		`up == 0`:                            flowKept,
		`a:loop`:                             flowUnknown,
		`{__name__=~"job:.*"} == 0`:          flowUnknown,
		`{__name__=~"nothing_here.*"} == 0`:  flowKept,
	} {
		hits := newFlow(recs).setMatchesOn(mustParse(t, lhs+clause))
		if len(hits) != 1 || hits[0].LHS != want.String() {
			t.Errorf("%s: got %+v, want %s", lhs, hits, want)
		}
	}
}
