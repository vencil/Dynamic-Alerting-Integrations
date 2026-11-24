package config

import (
	"strings"
	"testing"

	promparser "github.com/prometheus/prometheus/promql/parser"
	"gopkg.in/yaml.v3"
)

func caSpec(t *testing.T, y string) CustomAlertSpec {
	t.Helper()
	var specs []CustomAlertSpec
	if err := yaml.Unmarshal([]byte(y), &specs); err != nil {
		t.Fatal(err)
	}
	return specs[0]
}

func caEntry(t *testing.T, status string, sevs []string, y string) CustomAlertEntry {
	t.Helper()
	spec := caSpec(t, y)
	id, err := RecipeID(spec)
	if err != nil && status != CustomAlertRejected {
		t.Fatal(err)
	}
	return CustomAlertEntry{Spec: spec, Status: status, RecipeID: id, Severities: sevs}
}

func packRules(t *testing.T, p CustomAlertPack) map[string]orderedMap {
	t.Helper()
	out := map[string]orderedMap{}
	for _, g := range p.Groups {
		for _, r := range g.get("rules").([]any) {
			rule := r.(orderedMap)
			id := rule.get("record")
			if id == nil {
				id = rule.get("alert")
			}
			key := id.(string)
			if l, ok := rule.get("labels").(orderedMap); ok && rule.get("alert") != nil {
				key += "/" + l.get("severity").(string)
			}
			if rule.get("record") == "custom_recipe_info" {
				key += "/" + rule.get("labels").(orderedMap).get("recipe_id").(string)
			}
			out[key] = rule
		}
	}
	return out
}

const thrSpec = `- {recipe: threshold, name: q, metric: qd, op: ">", window: 5m, threshold: "1"}`

// Served and switched-off entries compile; rejected and duplicate ones do not.
// A shape's severities are the union over every entry, in sorted order.
func TestCompileCustomAlertPack_CompilesServedAndDisabledOnly(t *testing.T) {
	served := caEntry(t, CustomAlertServed, []string{"warning"}, thrSpec)
	off := caEntry(t, CustomAlertDisabled, []string{"critical"}, thrSpec)
	rejected := caEntry(t, CustomAlertRejected, []string{"warning"},
		`- {recipe: rate, name: r, metric: rr, window: 5m, threshold: "1"}`)
	dup := caEntry(t, CustomAlertDuplicated, []string{"warning"},
		`- {recipe: rate, name: r2, metric: rd, window: 5m, threshold: "1"}`)
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{
		{Tenant: "a", Entries: []CustomAlertEntry{served, rejected}},
		{Tenant: "b", Entries: []CustomAlertEntry{off, dup}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.Shapes != 1 {
		t.Fatalf("shapes = %d, want 1 (rejected and duplicate entries compile nothing)", p.Shapes)
	}
	rules := packRules(t, p)
	for _, want := range []string{
		"custom:" + served.RecipeID + ":critical:core", "custom:" + served.RecipeID + ":warning:core",
		"Custom_" + served.RecipeID + "/critical", "Custom_" + served.RecipeID + "/warning",
	} {
		if _, ok := rules[want]; !ok {
			t.Errorf("missing %s; have %v", want, keys(rules))
		}
	}
	// critical sorts before warning.
	var order []string
	for _, g := range p.Groups {
		if g.get("name") == "custom-alerts-normalization" {
			for _, r := range g.get("rules").([]any) {
				order = append(order, r.(orderedMap).get("record").(string))
			}
		}
	}
	if !strings.HasSuffix(order[len(order)-2], ":critical:core") || !strings.HasSuffix(order[len(order)-1], ":warning:core") {
		t.Errorf("core records not in severity order: %v", order)
	}
}

func keys(m map[string]orderedMap) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}

// Two unequal shapes on one recipe_id would emit one shape's selectors for
// both: refused.
func TestCompileCustomAlertPack_RefusesARecipeIDTwoShapesShare(t *testing.T) {
	a := caEntry(t, CustomAlertServed, []string{"warning"}, thrSpec)
	b := caEntry(t, CustomAlertServed, []string{"warning"},
		`- {recipe: threshold, name: q, metric: qd, op: ">", window: 10m, threshold: "1"}`)
	b.RecipeID = a.RecipeID // a forced collision
	_, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "a", Entries: []CustomAlertEntry{a}}, {Tenant: "b", Entries: []CustomAlertEntry{b}}})
	if err == nil || !strings.Contains(err.Error(), "collides between two distinct shapes") {
		t.Fatalf("err = %v, want a collision", err)
	}
	// Equal shapes on one id are one rule, not a collision.
	c := caEntry(t, CustomAlertServed, []string{"critical"}, thrSpec)
	if _, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "a", Entries: []CustomAlertEntry{a}}, {Tenant: "c", Entries: []CustomAlertEntry{c}}}); err != nil {
		t.Fatalf("equal shapes refused: %v", err)
	}
}

// An absence entry's op is in neither its slug nor its rules: two entries on
// one recipe_id that differ only in a stray `op:` are one shape, not a
// collision that would refuse the whole tree.
func TestCompileCustomAlertPack_AbsenceOpIsNotPartOfTheShape(t *testing.T) {
	a := caEntry(t, CustomAlertServed, []string{"warning"},
		`- {recipe: absence, name: hb, metric: hb_total, window: 5m, threshold: "1"}`)
	b := caEntry(t, CustomAlertServed, []string{"warning"},
		`- {recipe: absence, name: hb, metric: hb_total, window: 5m, threshold: "1", op: "<"}`)
	if a.RecipeID != b.RecipeID {
		t.Fatalf("precondition: one recipe_id, got %s / %s", a.RecipeID, b.RecipeID)
	}
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "a", Entries: []CustomAlertEntry{a}}, {Tenant: "b", Entries: []CustomAlertEntry{b}}})
	if err != nil || p.Shapes != 1 {
		t.Fatalf("shapes = %d, err = %v; want one shape", p.Shapes, err)
	}
}

func TestCompileCustomAlertPack_EmptyHasNoGroups(t *testing.T) {
	p, err := CompileCustomAlertPack(nil)
	if err != nil || len(p.Groups) != 0 || p.Shapes != 0 {
		t.Fatalf("p = %+v, err = %v", p, err)
	}
}

// #3483: the threshold record emits a join key only while the replicas agree
// on its (name, mode) — no tie-break, so Prometheus and VictoriaMetrics behave
// the same during a reload. Behaviour is pinned by promtool and vmalert-tool in
// tests/rulepacks/rule-pack-custom-alerts-reload-skew_test.yaml; this pins the
// shape and that the join key is spelled once.
func TestThresholdRecordEmitsAKeyOnlyWhileReplicasAgree(t *testing.T) {
	rec := thresholdRecord("rid", "m")
	if got := rec.get("record"); got != "custom:threshold:rid" {
		t.Fatalf("record = %v", got)
	}
	expr, _ := rec.get("expr").(string)
	rows := `max by(tenant, version, severity, name, mode) ` +
		`(user_threshold{component="custom", metric="m", recipe_id="rid"})`
	want := `label_replace(` + rows +
		` and on(tenant, version, severity) (count by(tenant, version, severity) (` + rows + `) == 1)` +
		`, "version", "default", "version", "^$")`
	if expr != want {
		t.Fatalf("expr =\n%s\nwant\n%s", expr, want)
	}
	if strings.Contains(expr, "topk") {
		t.Errorf("tie-break in the threshold record: %s", expr)
	}
	if n := strings.Count(expr, thresholdJoinKey); n != 4 {
		t.Errorf("join key %q appears %d times, want 4 (both rows, on, count by)", thresholdJoinKey, n)
	}
}

// The emit-time invariant: a template action or backtick the platform did
// not write fails the compile; the platform's own pass.
func TestAssertTemplateSafe(t *testing.T) {
	group := func(v string) []orderedMap {
		return []orderedMap{{{"name", "g"}, {"rules", []any{orderedMap{
			{"alert", "A"}, {"annotations", orderedMap{{"description", v}}}}}}}}
	}
	for _, ok := range []string{"{{ $labels.tenant }}", `value {{ $value | printf "%.2f" }}`, "plain"} {
		if err := assertTemplateSafe(group(ok)); err != nil {
			t.Errorf("%q refused: %v", ok, err)
		}
	}
	// Each of the three markers alone fails it: an opening action, a closing
	// one, a backtick.
	for _, bad := range []string{`{{ query "up" }}`, "{{ x", "x }}", "a `b`", "{{ $labels.x }} }}", `{{ $value | printf "%d" }}`} {
		if err := assertTemplateSafe(group(bad)); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
	// #3483: exactly the three metadata annotation templates pass (alone or
	// inside prose); a near variant — another query, a range form, another
	// label — is template code the compiler did not author, and fails.
	for _, l := range metadataLabels {
		for _, ok := range []string{metadataAnnotation(l), "[" + metadataAnnotation(l) + "] {{ $labels.tenant }}"} {
			if err := assertTemplateSafe(group(ok)); err != nil {
				t.Errorf("%q refused: %v", ok, err)
			}
		}
	}
	for _, bad := range []string{
		"{{ with query (printf `topk(1, up)` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.owner }}{{ end }}{{ end }}",
		"{{ range query (printf `" + metadataQuery + "` $labels.tenant) }}{{ .Labels.owner }}{{ end }}",
		metadataAnnotation("instance"),
		metadataAnnotation("owner") + " {{ query `up` }}",
	} {
		if err := assertTemplateSafe(group(bad)); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

// Selector values are escaped for a PromQL double-quoted string: backslash
// first, then the quote, then newlines.
func TestSelectorEscaping(t *testing.T) {
	sh, err := recipeShape(caSpec(t, `- {recipe: threshold, name: q, metric: m, window: 5m, threshold: "1", `+
		`selectors: {p: 'C:\d"x', n: "a\nb"}, selectors_re: {s: 'api-.*'}}`))
	if err != nil {
		t.Fatal(err)
	}
	if got, want := sh.selector(), `{n="a\nb", p="C:\\d\"x", s=~"api-.*"}`; got != want {
		t.Fatalf("selector = %s, want %s", got, want)
	}
}

// The pack's key order is the order it has always had, and `>` is not
// HTML-escaped by the map itself (an encoder with SetEscapeHTML(false) keeps
// it so; json.Marshal escapes any Marshaler's output again).
func TestOrderedMapMarshal(t *testing.T) {
	b, err := orderedMap{{"record", "r"}, {"expr", "a > b"}, {"labels", orderedMap{{"z", "1"}, {"a", "2"}}}}.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"record":"r","expr":"a > b","labels":{"z":"1","a":"2"}}`; string(b) != want {
		t.Fatalf("got %s, want %s", b, want)
	}
}

// recipeShape and RecipeID agree, and a shape's ID is its recipe_id.
func TestRecipeShapeIDIsRecipeID(t *testing.T) {
	for _, y := range []string{
		thrSpec,
		`- {recipe: forecast, name: f, metric: avail, horizon: 4h, capacity_metric: cap, threshold: "0.1", group_by: [persistentvolumeclaim]}`,
		`- {recipe: slo_burn_rate, name: s, metric: err, denominator_metric: req, objective: "99.9", min_events: 20}`,
		`- {recipe: p99_latency, name: p, metric: lat, window: 5m, quantile: ".95", threshold: "1", selectors: {svc: a}}`,
	} {
		spec := caSpec(t, y)
		id, err := RecipeID(spec)
		if err != nil {
			t.Fatal(err)
		}
		sh, _ := recipeShape(spec)
		if sh.ID() != id {
			t.Errorf("%s: shape ID %s != RecipeID %s", y, sh.ID(), id)
		}
	}
}

// #3309: every compiled alert carries the `maintenance` label, and the label
// says what the rule does: `mute` exactly when the :core record it reads ends
// in maintenanceTail, `page` (the silent-mode sentinel) when nothing does.
// The recurring-maintenance silences select on this label, so a compiled
// alert without it would stop being muted in a recurring window.
func TestCompileCustomAlertPack_MaintenanceLabelMatchesTheRule(t *testing.T) {
	var tenants []TenantCustomAlerts
	for i, y := range []string{
		thrSpec,
		`- {recipe: absence, name: hb, metric: hb_total, window: 5m, threshold: "1"}`,
		`- {recipe: slo_burn_rate, name: s, metric: err, denominator_metric: req, objective: "99.9", min_events: 20}`,
	} {
		e := caEntry(t, CustomAlertServed, []string{"warning", "critical"}, y)
		tenants = append(tenants, TenantCustomAlerts{Tenant: string(rune('a' + i)), Entries: []CustomAlertEntry{e}})
	}
	p, err := CompileCustomAlertPack(tenants)
	if err != nil {
		t.Fatal(err)
	}
	records := map[string]string{}
	var alerts []orderedMap
	for _, g := range p.Groups {
		for _, r := range g.get("rules").([]any) {
			rule := r.(orderedMap)
			if name, ok := rule.get("record").(string); ok {
				records[name] = rule.get("expr").(string)
			} else {
				alerts = append(alerts, rule)
			}
		}
	}
	seen := map[string]int{}
	for _, a := range alerts {
		name := a.get("alert").(string)
		expr := a.get("expr").(string)
		got, _ := a.get("labels").(orderedMap).get("maintenance").(string)
		want := maintenancePage
		if strings.HasSuffix(expr, maintenanceTail) || strings.HasSuffix(records[expr], maintenanceTail) {
			want = maintenanceMute
		}
		if got != want {
			t.Errorf("%s: maintenance=%q, want %q (expr %q)", name, got, want, expr)
		}
		seen[got]++
	}
	// Guard the guard: both values must actually occur, or the loop above
	// proves nothing about one of them.
	if seen[maintenanceMute] == 0 || seen[maintenancePage] == 0 {
		t.Fatalf("label values seen %v; want both %q and %q", seen, maintenanceMute, maintenancePage)
	}
}

// A tenant's `selectors:` pin a label on the tenant's own metric. A label the
// compiled pipeline aggregates BY is a key the recipe depends on: pinning it
// does not filter the data, it breaks the shape (pinning `le` on p99 leaves
// histogram_quantile without its +Inf bucket, so the alert evaluates NaN and
// never fires). Every such key must therefore be in reserved_labels, which
// selectorItems refuses. The keys are read off the compiled rules, not
// listed by hand, so a new recipe that aggregates by a new label turns this
// red until the contract reserves it. group_by values are excluded: the
// tenant chose them, and pinning one is a plain filter. So is the cluster
// label (clusterLabel): every recipe keeps it so that two clusters stay
// apart, and a selector pinning it selects one cluster's series whole — the
// aggregation and the pairing still see every series of that cluster.
func TestCompiledAggregationKeysOnTenantMetricsAreReserved(t *testing.T) {
	specs := []string{
		`- {recipe: threshold, name: a, metric: m_thr, op: ">", window: 5m, threshold: "1"}`,
		`- {recipe: threshold, name: b, metric: m_eq, op: "==", window: 5m, threshold: "1"}`,
		`- {recipe: rate, name: c, metric: m_rate, window: 5m, threshold: "1"}`,
		`- {recipe: ratio, name: d, metric: m_num, denominator_metric: m_den, window: 5m, threshold: "0.1"}`,
		`- {recipe: p99_latency, name: e, metric: m_lat, window: 5m, threshold: "1"}`,
		`- {recipe: absence, name: f, metric: m_abs, window: 5m, threshold: "1"}`,
		`- {recipe: forecast, name: g, metric: m_avail, horizon: 4h, capacity_metric: m_cap, threshold: "0.1", group_by: [persistentvolumeclaim]}`,
		`- {recipe: forecast, name: i, metric: m_raw, op: "<", horizon: 4h, threshold: "10"}`,
		`- {recipe: threshold, name: j, metric: m_thr_g, op: ">", window: 5m, threshold: "1", group_by: [persistentvolumeclaim]}`,
		`- {recipe: p99_latency, name: k, metric: m_lat_g, window: 5m, threshold: "1", group_by: [persistentvolumeclaim]}`,
		`- {recipe: slo_burn_rate, name: h, metric: m_err, denominator_metric: m_req, objective: "99.9", min_events: 20}`,
	}
	tenantMetric := map[string]bool{}
	var entries []CustomAlertEntry
	for _, y := range specs {
		e := caEntry(t, CustomAlertServed, []string{"warning"}, y)
		entries = append(entries, e)
		for _, m := range []string{e.Spec.Metric, e.Spec.DenominatorMetric, e.Spec.CapacityMetric} {
			if m != "" {
				tenantMetric[m] = true
				tenantMetric[m+"_bucket"] = true
			}
		}
	}
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "t", Entries: entries}})
	if err != nil {
		t.Fatal(err)
	}
	reserved := stringSet(contract.ReservedLabels)
	tenantChosen := stringSet(contract.GroupBy.Enum)
	keys := map[string]string{} // label -> a record that aggregates by it
	readsTenantMetric := func(n promparser.Node) bool {
		found := false
		promparser.Inspect(n, func(c promparser.Node, _ []promparser.Node) error {
			if vs, ok := c.(*promparser.VectorSelector); ok && tenantMetric[vs.Name] {
				found = true
			}
			return nil
		})
		return found
	}
	records := 0
	for _, g := range p.Groups {
		for _, r := range g.get("rules").([]any) {
			rule := r.(orderedMap)
			name, ok := rule.get("record").(string)
			if !ok {
				name = rule.get("alert").(string)
			}
			records++
			expr, err := promparser.NewParser(promparser.Options{}).ParseExpr(rule.get("expr").(string))
			if err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			promparser.Inspect(expr, func(n promparser.Node, _ []promparser.Node) error {
				if ag, ok := n.(*promparser.AggregateExpr); ok && !ag.Without && readsTenantMetric(ag.Expr) {
					for _, l := range ag.Grouping {
						keys[l] = name
					}
				}
				// A join over a tenant metric matches on these labels too:
				// pinning one would also change which series pair up.
				// (`ignoring(...)` names labels left OUT of the match, so
				// pinning those is a plain filter; only `on(...)` counts.)
				if be, ok := n.(*promparser.BinaryExpr); ok && be.VectorMatching != nil &&
					be.VectorMatching.On && (readsTenantMetric(be.LHS) || readsTenantMetric(be.RHS)) {
					for _, l := range append(append([]string{}, be.VectorMatching.MatchingLabels...), be.VectorMatching.Include...) {
						keys[l] = name
					}
				}
				return nil
			})
		}
	}
	if records == 0 || len(keys) == 0 {
		t.Fatalf("found %d records and %d aggregation keys; the walk read nothing", records, len(keys))
	}
	for l, rec := range keys {
		if !reserved[l] && !tenantChosen[l] && l != clusterLabel {
			t.Errorf("%s aggregates a tenant metric by %q, which is not in recipe_contract.json reserved_labels: a tenant selector could pin it", rec, l)
		}
	}
}

// ADR-006 "one tenant in several clusters": every metric-side aggregation
// keeps da_cluster, and every pairing of two metric-side operands matches on
// it — the exact text per recipe, so dropping the label from one recipe's
// grouping (or from one side of a ratio) fails here by name. The threshold
// joins stay on(tenant[, version]): thresholds are one per tenant.
func TestCompiledRulesKeepTheClusterPerRecipe(t *testing.T) {
	cases := []struct {
		name, spec, rule string
		want             []string
	}{
		{"threshold", `- {recipe: threshold, name: a, metric: m_thr, op: ">", window: 5m, threshold: "1"}`,
			"custom:metric:", []string{`max by(tenant, version, da_cluster) (m_thr)`}},
		{"threshold group_by", `- {recipe: threshold, name: a, metric: m_thr, op: ">", window: 5m, threshold: "1", group_by: [persistentvolumeclaim]}`,
			"custom:metric:", []string{`max by(tenant, version, da_cluster, persistentvolumeclaim) (m_thr)`}},
		{"rate", `- {recipe: rate, name: a, metric: m_rate, window: 5m, threshold: "1"}`,
			"custom:metric:", []string{`sum by(tenant, version, da_cluster) (rate(m_rate[5m]))`}},
		{"ratio", `- {recipe: ratio, name: a, metric: m_num, denominator_metric: m_den, window: 5m, threshold: "0.1"}`,
			"custom:metric:", []string{
				`sum by(tenant, da_cluster) (rate(m_num[5m]))`,
				`(sum by(tenant, da_cluster) (rate(m_den[5m])) > 0)`}},
		{"p99_latency", `- {recipe: p99_latency, name: a, metric: m_lat, window: 5m, threshold: "1"}`,
			"custom:metric:", []string{`sum by(le, tenant, version, da_cluster) (rate(m_lat_bucket[5m]))`}},
		{"forecast ratio mode", `- {recipe: forecast, name: a, metric: m_avail, horizon: 4h, capacity_metric: m_cap, threshold: "0.1"}`,
			"custom:fcbase:", []string{
				`sum by(tenant, da_cluster) (m_avail)`,
				`(sum by(tenant, da_cluster) (m_cap) > 0)`}},
		{"forecast raw mode", `- {recipe: forecast, name: a, metric: m_raw, op: "<", horizon: 4h, threshold: "10"}`,
			"custom:fcbase:", []string{`max by(tenant, version, da_cluster) (m_raw)`}},
		{"slo ratio window", `- {recipe: slo_burn_rate, name: a, metric: m_err, denominator_metric: m_req, objective: "99.9"}`,
			"custom:metric:", []string{
				`sum by(tenant, da_cluster) (rate(m_err[`,
				`(sum by(tenant, da_cluster) (rate(m_req[`}},
		{"slo bad events", `- {recipe: slo_burn_rate, name: a, metric: m_err, denominator_metric: m_req, objective: "99.9"}`,
			"custom:bad:", []string{`sum by(tenant, da_cluster) (increase(m_err[`}},
		{"equals core", `- {recipe: threshold, name: a, metric: m_eq, op: "==", window: 5m, threshold: "1"}`,
			":core", []string{`max by(tenant, version, da_cluster, name, mode) (`}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := caEntry(t, CustomAlertServed, []string{"warning", "critical"}, tc.spec)
			p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "t", Entries: []CustomAlertEntry{e}}})
			if err != nil {
				t.Fatal(err)
			}
			judged := 0
			for name, rule := range packRules(t, p) {
				if rule.get("record") == nil || !strings.Contains(name, tc.rule) ||
					strings.HasPrefix(name, "custom:threshold:") || strings.HasPrefix(name, "custom_recipe_info") {
					continue
				}
				judged++
				expr := rule.get("expr").(string)
				for _, w := range tc.want {
					if !strings.Contains(expr, w) {
						t.Errorf("%s lacks %q:\n%s", name, w, expr)
					}
				}
				// Not one grouping of the rule names the tenant without the cluster.
				for _, flat := range []string{"by(tenant)", "by(tenant, version)", "by(le, tenant, version)", "by(tenant, version, name, mode)"} {
					if strings.Contains(expr, flat) {
						t.Errorf("%s still groups %q:\n%s", name, flat, expr)
					}
				}
			}
			if judged == 0 {
				t.Fatalf("no %q record in the pack: %v", tc.rule, keys(packRules(t, p)))
			}
		})
	}
}

// The slo core pairs its three operands within one cluster: the long gate,
// the short gate and the bad-event floor. on(tenant) alone would let one
// cluster's long window fire with another cluster's short window.
func TestSloCorePairsItsWindowsPerCluster(t *testing.T) {
	e := caEntry(t, CustomAlertServed, []string{"critical", "warning"},
		`- {recipe: slo_burn_rate, name: a, metric: m_err, denominator_metric: m_req, objective: "99.9"}`)
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "t", Entries: []CustomAlertEntry{e}}})
	if err != nil {
		t.Fatal(err)
	}
	for _, sev := range []string{"critical", "warning"} {
		expr := packRules(t, p)[coreName(e.RecipeID, sev)].get("expr").(string)
		if got := strings.Count(expr, "and on(tenant, da_cluster)\n"); got != 2 {
			t.Errorf("%s core pairs per cluster %d time(s), want 2:\n%s", sev, got, expr)
		}
		if strings.Contains(expr, "and on(tenant)\n") {
			t.Errorf("%s core still pairs on(tenant) alone:\n%s", sev, expr)
		}
		// The threshold join and the maintenance mute stay per tenant; the
		// short gate is many-to-one as well (a tenant's clusters against its
		// one threshold row), so it says group_left too.
		for _, w := range []string{"> on(tenant, version) group_left(name, mode)", "> on(tenant) group_left(name, mode)",
			"> on(tenant, version) group_left\n", "> on(tenant) group_left\n",
			"unless on(tenant)\n(user_state_filter"} {
			if !strings.Contains(expr, w) {
				t.Errorf("%s core lacks %q:\n%s", sev, w, expr)
			}
		}
	}
}

// Absence stays tenant-wide (present in any cluster counts) and says so: the
// pack carries the declaration check_cluster_identity.py reads for exactly
// the absence cores, and for nothing else.
func TestAbsenceCoreIsDeclaredTenantWide(t *testing.T) {
	abs := caEntry(t, CustomAlertServed, []string{"critical"},
		`- {recipe: absence, name: f, metric: m_abs, window: 5m, threshold: "0:critical"}`)
	thr := caEntry(t, CustomAlertServed, []string{"warning"}, thrSpec)
	p, err := CompileCustomAlertPack([]TenantCustomAlerts{{Tenant: "t", Entries: []CustomAlertEntry{abs, thr}}})
	if err != nil {
		t.Fatal(err)
	}
	core := coreName(abs.RecipeID, "critical")
	expr := packRules(t, p)[core].get("expr").(string)
	if !strings.Contains(expr, "unless on(tenant)\n  count by(tenant) (count_over_time(m_abs[5m]) > 0)") {
		t.Errorf("the absence core is not the tenant-wide one:\n%s", expr)
	}
	if len(p.Declarations) != 1 || p.Declarations[core] != absenceTenantWide {
		t.Errorf("declarations = %v, want only %s", p.Declarations, core)
	}
	if !strings.HasPrefix(absenceTenantWide, "da_cluster: tenant-wide by-drops-cluster (tenant) — ") {
		t.Errorf("the declaration is not the form check_cluster_identity.py reads: %q", absenceTenantWide)
	}
}

// A group_by that names the cluster label does not write it twice (the enum
// does not hold it today).
func TestGbSuffixWritesTheClusterOnce(t *testing.T) {
	for in, want := range map[string]string{
		"":                                 ", da_cluster",
		"persistentvolumeclaim":            ", da_cluster, persistentvolumeclaim",
		"da_cluster,persistentvolumeclaim": ", da_cluster, persistentvolumeclaim",
	} {
		var gb []string
		if in != "" {
			gb = strings.Split(in, ",")
		}
		if got := gbSuffix(gb); got != want {
			t.Errorf("gbSuffix(%v) = %q, want %q", gb, got, want)
		}
	}
}
