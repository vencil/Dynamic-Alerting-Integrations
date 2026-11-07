package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

const maintenanceSeries = `user_state_filter{filter="maintenance"}`

// TestSelectorsMatchingReadsSpellingsAsPrometheusDoes pins that every way of
// writing a selector that reaches the maintenance series is found — the
// spellings a text check misses (#1200 adversarial review) — and that
// selectors which cannot reach it are not.
func TestSelectorsMatchingReadsSpellingsAsPrometheusDoes(t *testing.T) {
	t.Parallel()
	want, err := seriesLabels(maintenanceSeries)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name, expr string
		hits       int
	}{
		{"canonical", `up == 0 unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`, 1},
		{"hex escape", `up unless on(tenant) (user_state_filter{filter="mai\x6etenance"} == 1)`, 1},
		{"single quotes", `up unless on(tenant) (user_state_filter{filter='maintenance'} == 1)`, 1},
		{"backticks", "up unless on(tenant) (user_state_filter{filter=`maintenance`} == 1)", 1},
		{"regex value", `up unless on(tenant) (user_state_filter{filter=~"main.*"} == 1)`, 1},
		{"negative value", `up unless on(tenant) (user_state_filter{filter!="container_crashloop"} == 1)`, 1},
		{"no filter matcher", `up unless on(tenant) user_state_filter`, 1},
		{"other label too", `up unless on(tenant) (user_state_filter{filter="maintenance",tenant="a"} == 1)`, 1},
		{"name by regex", `up unless on(tenant) ({__name__=~"user_state_.*", filter=~"main.*"} == 1)`, 1},
		{"no metric name", `up unless on(tenant) ({filter="maintenance"} == 1)`, 1},
		{"inside a range", `up unless on(tenant) (max_over_time(user_state_filter{filter="maintenance"}[5m]) == 1)`, 1},
		{"two copies", `(a unless on(tenant) (user_state_filter{filter="maintenance"}==1)) or (b unless on(tenant) (user_state_filter{filter="maintenance"}==1))`, 2},
		{"another filter", `x * on(tenant) group_left user_state_filter{filter="container_crashloop"}`, 0},
		{"another metric", `up{job="clickhouse"} == 0`, 0},
		{"name regex that excludes it", `{__name__=~"up|probe_success", filter="maintenance"}`, 0},
		{"negated value", `user_state_filter{filter!="maintenance"}`, 0},
	}
	for _, c := range cases {
		c := c
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			hits, err := selectorsMatching(c.expr, want)
			if err != nil {
				t.Fatal(err)
			}
			if len(hits) != c.hits {
				t.Fatalf("%s: %d selector(s) could select %s, want %d: %+v", c.expr, len(hits), maintenanceSeries, c.hits, hits)
			}
			for _, h := range hits {
				if h.Start < 0 || h.End > len(c.expr) || h.Start >= h.End {
					t.Fatalf("hit %+v outside the expression", h)
				}
			}
		})
	}
}

// TestSelectorPositionsAndSpelling: the offsets locate the selector text in
// the expression, and the spelling is Prometheus' canonical one whatever the
// quoting, so a caller can compare it to one fixed string.
func TestSelectorPositionsAndSpelling(t *testing.T) {
	t.Parallel()
	want, _ := seriesLabels(maintenanceSeries)
	expr := "x\nunless on(tenant)\n(user_state_filter{filter='maintenance'} == 1)"
	hits, err := selectorsMatching(expr, want)
	if err != nil || len(hits) != 1 {
		t.Fatalf("hits %+v, err %v", hits, err)
	}
	if got := expr[hits[0].Start:hits[0].End]; got != `user_state_filter{filter='maintenance'}` {
		t.Fatalf("offsets select %q", got)
	}
	if hits[0].Selector != maintenanceSeries {
		t.Fatalf("spelled %q, want %q", hits[0].Selector, maintenanceSeries)
	}
}

func TestSeriesMustBeEqualityMatchersWithAName(t *testing.T) {
	t.Parallel()
	for _, s := range []string{`user_state_filter{filter=~"m.*"}`, `{filter="maintenance"}`, `user_state_filter{`} {
		if _, err := seriesLabels(s); err == nil {
			t.Errorf("%s accepted as a series", s)
		}
	}
}

func TestRunPromQLSelectors(t *testing.T) {
	t.Parallel()
	in := `["up unless on(tenant) (user_state_filter{filter=\"maintenance\"} == 1)", "up"]`
	var out, errb bytes.Buffer
	rc := run([]string{"promql-selectors", maintenanceSeries, "-"}, strings.NewReader(in), &out, &errb)
	if rc != exitOK {
		t.Fatalf("rc %d, stderr %s", rc, errb.String())
	}
	var got struct {
		Documents [][][]selectorHit `json:"documents"`
	}
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Documents) != 1 || len(got.Documents[0]) != 2 ||
		len(got.Documents[0][0]) != 1 || len(got.Documents[0][1]) != 0 {
		t.Fatalf("output %s", out.String())
	}

	for _, bad := range []struct {
		args []string
		in   string
	}{
		{[]string{"promql-selectors", maintenanceSeries, "-"}, `["up +"]`},
		{[]string{"promql-selectors", maintenanceSeries, "-"}, `{"not": "a list"}`},
		{[]string{"promql-selectors", `{filter="x"}`, "-"}, `["up"]`},
		{[]string{"promql-selectors", maintenanceSeries, "file.yaml"}, `["up"]`},
	} {
		var o, e bytes.Buffer
		if rc := run(bad.args, strings.NewReader(bad.in), &o, &e); rc != exitCallerErr {
			t.Errorf("%v with %s: rc %d, want %d", bad.args, bad.in, rc, exitCallerErr)
		}
	}
}
