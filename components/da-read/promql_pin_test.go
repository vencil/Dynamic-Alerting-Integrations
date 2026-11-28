package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func pinOne(t *testing.T, tenant string, nsForm []string, expr string) pinAnswer {
	t.Helper()
	req, _ := json.Marshal(pinRequest{Tenant: tenant, NamespaceForm: nsForm, Expressions: []string{expr}})
	var stdout, stderr bytes.Buffer
	if rc := run([]string{"promql-pin", "-"}, bytes.NewReader(req), &stdout, &stderr); rc != exitOK {
		t.Fatalf("rc %d: %s", rc, stderr.String())
	}
	var doc struct {
		Documents [][]pinAnswer `json:"documents"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	return doc.Documents[0][0]
}

func TestPinRewritesEverySelectorAsPrometheusPrintsIt(t *testing.T) {
	for _, c := range []struct{ in, want string }{
		{`up == 0`, `up{tenant="db-a"} == 0`},
		{`rate(x[5m] offset 1m)`, `rate(x{tenant="db-a"}[5m] offset 1m)`},
		{`max_over_time(x[1h:5m])`, `max_over_time(x{tenant="db-a"}[1h:5m])`},
		{`x @ 100`, `x{tenant="db-a"} @ 100.000`},
		{`absent(x{job="j"})`, `absent(x{job="j",tenant="db-a"})`},
		{`{__name__="x"}`, `{__name__="x",tenant="db-a"}`},
		{`a / on(instance) b`, `a{tenant="db-a"} / on (instance) b{tenant="db-a"}`},
		// an equality to the tenant already there is kept, not doubled
		{`x{tenant="db-a"}`, `x{tenant="db-a"}`},
	} {
		got := pinOne(t, "db-a", nil, c.in)
		if got.Expr == nil || *got.Expr != c.want {
			t.Errorf("%s: got %+v, want %s", c.in, got, c.want)
		}
	}
}

func TestPinUsesTheNamespaceFormForTheMetricsNamed(t *testing.T) {
	got := pinOne(t, "db-a", []string{"kube_pod_info"},
		`kube_pod_info{namespace="db-a"} * on(pod) group_left mysql_up`)
	want := `kube_pod_info{namespace="db-a",tenant=""} * on (pod) group_left () mysql_up{tenant="db-a"}`
	if got.Expr == nil || *got.Expr != want {
		t.Fatalf("got %+v, want %s", got, want)
	}
	// by __name__ too
	got = pinOne(t, "db-a", []string{"kube_pod_info"}, `{__name__="kube_pod_info"}`)
	if got.Expr == nil || *got.Expr != `{__name__="kube_pod_info",namespace="db-a",tenant=""}` {
		t.Fatalf("got %+v", got)
	}
}

func TestPinRefusesAMatcherItWouldChangeTheMeaningOf(t *testing.T) {
	for _, c := range []struct {
		in     string
		nsForm []string
	}{
		{`x{tenant="db-b"}`, nil},
		{`x{tenant=~"db-.*"}`, nil},
		{`x{tenant!="db-b"}`, nil},
		{`kube_pod_info{namespace="db-b"}`, []string{"kube_pod_info"}},
		{`kube_pod_info{tenant="db-a"}`, []string{"kube_pod_info"}},
	} {
		got := pinOne(t, "db-a", c.nsForm, c.in)
		if got.Conflict == nil || got.Expr != nil || !strings.Contains(*got.Conflict, "it already matches") {
			t.Errorf("%s: got %+v, want a conflict", c.in, got)
		}
	}
}

func TestPinRefusesWhatTheParserRefusesAndTheCallersBadRequest(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if rc := run([]string{"promql-pin", "-"},
		strings.NewReader(`{"tenant":"a","expressions":["rate(x[5m]"]}`), &stdout, &stderr); rc != exitCallerErr ||
		!strings.Contains(stdout.String(), "refused") {
		t.Fatalf("parse error: rc %d, stdout %q", rc, stdout.String())
	}
	for _, bad := range []string{`[]`, `{"expressions":["x"]}`, `{"tenant":"a"}`,
		`{"tenant":"a","expressions":[],"extra":1}`, `{"tenant":"a","expressions":[]} {}`} {
		stdout.Reset()
		stderr.Reset()
		if rc := run([]string{"promql-pin", "-"}, strings.NewReader(bad), &stdout, &stderr); rc != exitCallerErr ||
			stdout.Len() != 0 {
			t.Errorf("%s: rc %d, stdout %q", bad, rc, stdout.String())
		}
	}
}

func TestPinListsEachSelectorAsAnInstantSelectorBeforeAndAfter(t *testing.T) {
	got := pinOne(t, "db-a", []string{"kube_pod_info"},
		`rate(x{job="a"}[5m] offset 1m) > on(pod) group_left kube_pod_info @ 100 or rate(x{job="a"}[5m])`)
	if got.Expr == nil || len(got.Selectors) != 2 {
		t.Fatalf("got %+v", got)
	}
	want := []pinSelector{
		{`x{job="a"}`, `x{job="a",tenant="db-a"}`},
		{`kube_pod_info`, `kube_pod_info{namespace="db-a",tenant=""}`},
	}
	for i, w := range want {
		if got.Selectors[i] != w {
			t.Errorf("selector %d: got %+v, want %+v", i, got.Selectors[i], w)
		}
	}
}

func TestPinRefusesAnotherNamespaceBesideTheTenantPin(t *testing.T) {
	for _, e := range []string{`x{namespace="db-b"}`, `x{namespace=~"db-.*"}`} {
		got := pinOne(t, "db-a", nil, e)
		if got.Conflict == nil {
			t.Errorf("%s: got %+v, want a conflict", e, got)
		}
	}
	got := pinOne(t, "db-a", nil, `x{namespace="db-a"}`)
	if got.Expr == nil || *got.Expr != `x{namespace="db-a",tenant="db-a"}` {
		t.Errorf("got %+v", got)
	}
}

func TestPinRenamesARecordAndJudgesItByTheNameAsWritten(t *testing.T) {
	req, _ := json.Marshal(pinRequest{Tenant: "db-a", NamespaceForm: []string{"kube_pod_info"},
		Rename:      map[string]string{"job:x:sum": "migrated:db_a:job:x:sum", "kube_pod_info": "k:y"},
		Expressions: []string{`job:x:sum > 1 and {__name__="job:x:sum"} and kube_pod_info`}})
	var stdout, stderr bytes.Buffer
	if rc := run([]string{"promql-pin", "-"}, bytes.NewReader(req), &stdout, &stderr); rc != exitOK {
		t.Fatalf("rc %d: %s", rc, stderr.String())
	}
	var doc struct {
		Documents [][]pinAnswer `json:"documents"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	got := doc.Documents[0][0]
	want := `migrated:db_a:job:x:sum{tenant="db-a"} > 1 and {__name__="migrated:db_a:job:x:sum",tenant="db-a"} and k:y{namespace="db-a",tenant=""}`
	if got.Expr == nil || *got.Expr != want {
		t.Fatalf("got %+v\nwant %s", got, want)
	}
	if got.Selectors[0] != (pinSelector{`job:x:sum`, `migrated:db_a:job:x:sum{tenant="db-a"}`}) {
		t.Errorf("selector 0: %+v", got.Selectors[0])
	}
}
