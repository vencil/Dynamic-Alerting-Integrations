package main

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read promql-pin -`: each PromQL expression with every vector selector
// pinned to one tenant (#4226, migrate-rule's migrated rules) — the
// prom-label-proxy / Prometheus Operator `enforcedNamespaceLabel` way: on
// Prometheus' own parse tree, printed back by Prometheus' own printer, so a
// subquery, `offset`, `@`, a matrix selector, an escaped string or a selector
// written `{__name__="x"}` is rewritten exactly as Prometheus reads it.
//
// Standard input is one JSON object:
//
//	{"tenant": "<id>", "namespace_form": ["<metric>", …], "rename": {"<old>": "<new>", …},
//	 "expressions": ["…", …]}
//
// *rename* (optional) renames the selectors of a metric — the caller's own
// recording rules, moved to names of their own so the migrated copy and the
// legacy one do not write or read each other's series. The pin is judged by
// the name as written.
//
// A selector of a metric named in *namespace_form* (one whose series carry no
// `tenant` label: the caller's `_lib_metric_sources.tenant_presence` decides)
// gets `namespace="<id>", tenant=""`; every other selector `tenant="<id>"`.
// A selector that already matches the label to that value by equality keeps
// it as it is; one that matches it any other way (another value, a regex, a
// negation) is a conflict: a pin cannot be added beside it without changing
// which series it reads. A `tenant="<id>"` selector's own `namespace`
// matcher, if it has one, must be `namespace="<id>"` too (the scrape writes
// the tenant from the namespace): any other is a conflict, never rewritten. Stdout is
//
//	{"documents": [[{"expr": "<pinned>", "selectors": [{"legacy", "pinned"}, …]}
//	                 | {"conflict": "<selector>: <why>"}, …]]}
//
// one entry per expression, in input order. "selectors" are its distinct
// selectors as instant vector selectors (no range, offset or `@`), before and
// after the pin, in the order they appear — what a count of the series each
// reads is asked with (#4227's pre-check: a pin that selects nothing). An
// expression Prometheus' parser refuses refuses the input (rc 2, the refusal document); standard input that
// is not that object is the caller's error.
type pinRequest struct {
	Tenant        string            `json:"tenant"`
	NamespaceForm []string          `json:"namespace_form"`
	Rename        map[string]string `json:"rename"`
	Expressions   []string          `json:"expressions"`
}

type pinAnswer struct {
	Expr      *string       `json:"expr,omitempty"`
	Selectors []pinSelector `json:"selectors,omitempty"`
	Conflict  *string       `json:"conflict,omitempty"`
}

type pinSelector struct {
	Legacy string `json:"legacy"`
	Pinned string `json:"pinned"`
}

// instantSelector is *vs* with *ms* as its matchers and nothing else: no
// range, offset or `@`, printed by Prometheus.
func instantSelector(vs *parser.VectorSelector, ms []*labels.Matcher) string {
	return (&parser.VectorSelector{Name: vs.Name, LabelMatchers: ms}).String()
}

// pinTenantLabel and pinNamespaceLabel: the label the platform isolates by
// and the one kube-state-metrics' series name the tenant with.
const (
	pinTenantLabel    = "tenant"
	pinNamespaceLabel = "namespace"
)

func pinRequestOf(data []byte) (pinRequest, error) {
	var req pinRequest
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		return req, err
	}
	if dec.More() {
		return req, fmt.Errorf("more than one JSON value")
	}
	if req.Tenant == "" {
		return req, fmt.Errorf(`"tenant" is missing or empty`)
	}
	if req.Expressions == nil {
		return req, fmt.Errorf(`"expressions" is missing`)
	}
	return req, nil
}

func readPromQLPin(req pinRequest) ([]byte, error) {
	nsForm := map[string]bool{}
	for _, m := range req.NamespaceForm {
		nsForm[m] = true
	}
	out := make([]pinAnswer, 0, len(req.Expressions))
	for i, e := range req.Expressions {
		node, err := parseExpr(e)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		conflict := ""
		var sels []pinSelector
		seen := map[pinSelector]bool{}
		parser.Inspect(node, func(n parser.Node, _ []parser.Node) error {
			vs, ok := n.(*parser.VectorSelector)
			if !ok || conflict != "" {
				return nil
			}
			want := []*labels.Matcher{labels.MustNewMatcher(labels.MatchEqual, pinTenantLabel, req.Tenant)}
			if nsForm[selectorName(vs)] {
				want = []*labels.Matcher{
					labels.MustNewMatcher(labels.MatchEqual, pinNamespaceLabel, req.Tenant),
					labels.MustNewMatcher(labels.MatchEqual, pinTenantLabel, ""),
				}
			}
			before := vs.String()
			legacy := instantSelector(vs, append([]*labels.Matcher(nil), vs.LabelMatchers...))
			if !nsForm[selectorName(vs)] {
				if why := namespaceAgrees(vs, req.Tenant); why != "" {
					conflict = before + ": " + why
					return nil
				}
			}
			for _, w := range want {
				if why := pinMatcher(vs, w); why != "" {
					conflict = before + ": " + why
					return nil
				}
			}
			renameSelector(vs, req.Rename)
			s := pinSelector{Legacy: legacy, Pinned: instantSelector(vs, vs.LabelMatchers)}
			if !seen[s] {
				seen[s] = true
				sels = append(sels, s)
			}
			return nil
		})
		if conflict != "" {
			c := conflict
			out = append(out, pinAnswer{Conflict: &c})
			continue
		}
		s := node.String()
		out = append(out, pinAnswer{Expr: &s, Selectors: sels})
	}
	return json.Marshal(struct {
		Documents [][]pinAnswer `json:"documents"`
	}{Documents: [][]pinAnswer{out}})
}

// selectorName is a selector's metric name: its name, or its `__name__`
// equality matcher's value ("" for neither).
func selectorName(vs *parser.VectorSelector) string {
	if vs.Name != "" {
		return vs.Name
	}
	for _, m := range vs.LabelMatchers {
		if m.Name == model.MetricNameLabel && m.Type == labels.MatchEqual {
			return m.Value
		}
	}
	return ""
}

// pinMatcher adds *w* to *vs*, or says why it cannot: a matcher on the same
// label that is not *w* itself.
func pinMatcher(vs *parser.VectorSelector, w *labels.Matcher) string {
	for _, m := range vs.LabelMatchers {
		if m.Name != w.Name {
			continue
		}
		if m.Type == w.Type && m.Value == w.Value {
			return ""
		}
		return fmt.Sprintf("it already matches %s with %s, not %s", w.Name, m.String(), w.String())
	}
	vs.LabelMatchers = append(vs.LabelMatchers, w)
	return ""
}

// namespaceAgrees says why *vs*'s own `namespace` matchers disagree with
// *tenant*, or "": every one must be `namespace="<tenant>"`.
func namespaceAgrees(vs *parser.VectorSelector, tenant string) string {
	w := labels.MustNewMatcher(labels.MatchEqual, pinNamespaceLabel, tenant)
	for _, m := range vs.LabelMatchers {
		if m.Name == pinNamespaceLabel && (m.Type != w.Type || m.Value != w.Value) {
			return fmt.Sprintf("it matches %s, not %s: the tenant's series are its namespace's",
				m.String(), w.String())
		}
	}
	return ""
}

// renameSelector moves *vs* to *rename*'s name for its metric, if it has one:
// its name and its `__name__` equality matcher both.
func renameSelector(vs *parser.VectorSelector, rename map[string]string) {
	to, ok := rename[selectorName(vs)]
	if !ok {
		return
	}
	if vs.Name != "" {
		vs.Name = to
	}
	for i, m := range vs.LabelMatchers {
		if m.Name == model.MetricNameLabel && m.Type == labels.MatchEqual {
			vs.LabelMatchers[i] = labels.MustNewMatcher(labels.MatchEqual, model.MetricNameLabel, to)
		}
	}
}
