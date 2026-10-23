package config

// custom_alert_compile.go — the custom-alert rule compiler (ADR-024 Capability
// B, #741; ADR-031 slo_burn_rate; #3229 step 2).
//
// It compiles the entries the exporter judged (CustomAlertEntries) into ONE
// vectorised rule set per shape (recipe_id), structured like the version-aware
// pilot rule pack:
//
//	recording  custom:threshold:{id}   normalise the threshold side (version→default)
//	recording  custom:metric:{id}      the recipe's metric side (not for absence / ==)
//	recording  custom:{id}:{sev}:core  per-severity exact-or-fallback join + maint suppress
//	alert      Custom_{id}             metadata-enriched, bilingual, {{ $labels.name }} title
//
// Invariants:
//   - vectorised on(tenant[,version]) — no per-tenant fan-out (O(M))
//   - the version graceful-join is emitted unconditionally (absent version is safe)
//   - aggregation uses an explicit by(tenant[,version]) keep-list (never bare sum())
//   - `name` rides group_left(name, mode) so on-call sees the tenant's title
//   - the alertname is the static shape slug; humans read the `name` label
//   - ratio guards division by zero with `(denominator > 0)` → empty vector, no +Inf
//   - absence self-scopes off custom:threshold:{id} (only declaring tenants have it)
//
// Which entries exist, whether each is served, its recipe_id and severities
// are the exporter's (CustomAlertEntries); this file only emits rule text.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// forecastMinSamples is the forecast cold-start data-sufficiency gate: a fresh
// base series with too few samples in the lookback yields wild slopes, so
// predict_linear is gated until at least this many exist (ADR-024 §Forecast).
const forecastMinSamples = 3

// orderedMap is a JSON object that keeps its key order, so the rule pack
// compile_custom_alerts.py serialises from it is byte-stable (record/alert, expr, for,
// labels, annotations — the order the pack has always had).
type orderedMap []orderedKV

type orderedKV struct {
	Key   string
	Value any
}

// MarshalJSON writes the object in insertion order, without HTML escaping
// (`>` stays `>` in an expr).
func (m orderedMap) MarshalJSON() ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, kv := range m {
		if i > 0 {
			buf.WriteByte(',')
		}
		k, err := marshalNoEscape(kv.Key)
		if err != nil {
			return nil, err
		}
		v, err := marshalNoEscape(kv.Value)
		if err != nil {
			return nil, err
		}
		buf.Write(k)
		buf.WriteByte(':')
		buf.Write(v)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

func marshalNoEscape(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// get returns the value at key (nil when absent).
func (m orderedMap) get(key string) any {
	for _, kv := range m {
		if kv.Key == key {
			return kv.Value
		}
	}
	return nil
}

// TenantCustomAlerts is one tenant's entries, as CustomAlertEntries returns them.
type TenantCustomAlerts struct {
	Tenant  string
	Entries []CustomAlertEntry
}

// CustomAlertPack is the compiled rule pack: Groups in pack order, and the
// shape count. Groups marshals to the pack's `groups:` list.
type CustomAlertPack struct {
	Groups []orderedMap
	Shapes int
}

// compiledStatuses: the entries a rule is compiled for — served, and switched
// off (a rule ready for when the tenant switches it back on).
var compiledStatuses = map[string]bool{CustomAlertServed: true, CustomAlertDisabled: true}

// CompileCustomAlertPack compiles the entries the exporter serves or has
// switched off into one rule set per recipe_id, sorted by recipe_id. A
// recipe_id two unequal shapes share (a residual hash collision) is an error:
// one rule would carry one shape's selectors for both.
func CompileCustomAlertPack(tenants []TenantCustomAlerts) (CustomAlertPack, error) {
	shapes := map[string]customAlertShape{}
	severities := map[string]map[string]bool{}
	for _, t := range tenants {
		for _, e := range t.Entries {
			if !compiledStatuses[e.Status] {
				continue
			}
			sh, err := recipeShape(e.Spec)
			if err != nil {
				// The exporter served or accepted it, so its shape validated
				// there; failing here is an internal inconsistency.
				return CustomAlertPack{}, fmt.Errorf("tenant=%s: _custom_alerts[%d] (%q): %w",
					t.Tenant, e.Index, e.Spec.Name, err)
			}
			if prev, ok := shapes[e.RecipeID]; ok && !reflect.DeepEqual(prev, sh) {
				return CustomAlertPack{}, fmt.Errorf("tenant=%s: _custom_alerts[%d] (%q): recipe_id %q "+
					"collides between two distinct shapes (residual hash collision — please report)",
					t.Tenant, e.Index, e.Spec.Name, e.RecipeID)
			}
			shapes[e.RecipeID] = sh
			if severities[e.RecipeID] == nil {
				severities[e.RecipeID] = map[string]bool{}
			}
			for _, s := range e.Severities {
				severities[e.RecipeID][s] = true
			}
		}
	}
	ids := make([]string, 0, len(shapes))
	for id := range shapes {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	var recording, alerts, info []any
	for _, id := range ids {
		sevs := make([]string, 0, len(severities[id]))
		for s := range severities[id] {
			sevs = append(sevs, s)
		}
		sort.Strings(sevs)
		rec, alr := emitShape(id, shapes[id], sevs)
		recording = append(recording, rec...)
		alerts = append(alerts, alr...)
		// D1 (ADR-024 §8): a static lifecycle-info series per shape, so SRE can
		// join recipe usage to recipe status for a tech-debt burn-down.
		info = append(info, orderedMap{
			{"record", "custom_recipe_info"},
			{"expr", "vector(1)"},
			{"labels", orderedMap{
				{"recipe_id", id},
				{"recipe", shapes[id].Recipe},
				{"status", RecipeStatus(shapes[id].Recipe)},
			}},
		})
	}

	var groups []orderedMap
	if len(recording) > 0 {
		groups = append(groups, orderedMap{{"name", "custom-alerts-normalization"}, {"interval", "15s"}, {"rules", recording}})
	}
	if len(info) > 0 {
		// Static metadata on a slow interval, out of the 15s normalize cadence.
		groups = append(groups, orderedMap{{"name", "custom-alerts-info"}, {"interval", "1m"}, {"rules", info}})
	}
	if len(alerts) > 0 {
		// The single global silent sentinel, ahead of the shape alerts (S7/S8).
		groups = append(groups, orderedMap{{"name", "custom-alerts"}, {"rules", append([]any{silentSentinel()}, alerts...)}})
	}
	if err := assertTemplateSafe(groups); err != nil {
		return CustomAlertPack{}, err
	}
	return CustomAlertPack{Groups: groups, Shapes: len(ids)}, nil
}

// silentSentinel is the SINGLE global (tenant-agnostic) silent-mode sentinel
// (#741 S7/S8): it fires once per (tenant, name) whose recipe declares
// mode=silent, straight from the exporter's user_threshold series, so it is
// injected once regardless of shape count. Alertmanager uses it as an inhibit
// SOURCE (equal:[tenant,name]) to suppress that recipe's notification while
// Prometheus keeps evaluating it (ADR-003 sentinel+inhibit). It carries the
// static component="sentinel" routing discriminator (#1095) so the platform's
// sentinel-sinkhole route swallows it instead of notifying humans.
func silentSentinel() orderedMap {
	return orderedMap{
		{"alert", "CustomRecipeSilent"},
		{"expr", `max by(tenant, name) (user_threshold{component="custom", mode="silent"})`},
		{"labels", orderedMap{
			{"severity", "none"},
			{"component", "sentinel"},
			{"tenant", "{{ $labels.tenant }}"},
			{"name", "{{ $labels.name }}"},
		}},
		{"annotations", orderedMap{
			{"summary", "Custom recipe [{{ $labels.name }}] is silent for {{ $labels.tenant }}"},
			{"summary_zh", "{{ $labels.tenant }} 的自訂告警 [{{ $labels.name }}] 處於靜默模式"},
			{"description", "The recipe is still evaluated and visible as an ALERTS " +
				"series (dashboard-only); its notifications are suppressed " +
				"via inhibit, not deleted."},
			{"description_zh", "此自訂告警仍會評估並可於監控面板（ALERTS series）查看，" +
				"通知經 inhibit 抑制而非刪除。"},
		}},
	}
}

// allowedTemplateAction: the platform-authored template actions the compiler
// itself writes into labels/annotations — `{{ $value | printf "%.Nf" }}` and
// `{{ $labels.X }}`. Anything else means tenant data became Go-template code.
var allowedTemplateAction = regexp.MustCompile(
	`\{\{\s*(?:\$value\s*\|\s*printf\s+"%\.[0-9]+f"|\$labels\.[a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}`)

// assertTemplateSafe is the emit-time invariant (F2 defence in depth): no
// generated label/annotation carries a template action or backtick beyond the
// platform allowlist, whichever field would have carried it. selectorItems
// rejects the known selector-value vector; this catches any future field.
func assertTemplateSafe(groups []orderedMap) error {
	for _, g := range groups {
		rules, _ := g.get("rules").([]any)
		for _, r := range rules {
			rule := r.(orderedMap)
			for _, section := range []string{"labels", "annotations"} {
				fields, _ := rule.get(section).(orderedMap)
				for _, kv := range fields {
					val := fmt.Sprint(kv.Value)
					residual := allowedTemplateAction.ReplaceAllString(val, "")
					if strings.Contains(residual, "{{") || strings.Contains(residual, "}}") || strings.Contains(residual, "`") {
						ident := rule.get("alert")
						if ident == nil {
							ident = rule.get("record")
						}
						return fmt.Errorf("emit-time invariant violation: %q in %q contains a "+
							"non-platform Go-template action or backtick — tenant-controlled data "+
							"must never become template code (F2 injection class). Value: %q", kv.Key, ident, val)
					}
				}
			}
		}
	}
	return nil
}

// normVersion normalises an empty/absent version → "default" (the
// version-aware idiom).
func normVersion(expr string) string {
	return `label_replace(` + expr + `, "version", "default", "version", "^$")`
}

// gbSuffix is ", l1, l2" for the bounded group_by dimensions (or ""),
// appended inside a by() label list so the metric-side aggregation keeps each
// dimension (per-PVC disk fill: fire if ANY PVC crosses; ADR-024 §Addendum).
// The threshold record and the on()/group_left join keys are unchanged: the
// extra dimension rides the many side of the join to the alert label.
func gbSuffix(gb []string) string {
	var b strings.Builder
	for _, g := range gb {
		b.WriteString(", " + g)
	}
	return b.String()
}

// escapeSelectorValue escapes a selector value for a PromQL double-quoted
// string: backslash first, then the double quote, then newlines, so a value
// can never break out of the literal.
func escapeSelectorValue(v string) string {
	v = strings.ReplaceAll(v, `\`, `\\`)
	v = strings.ReplaceAll(v, `"`, `\"`)
	return strings.ReplaceAll(v, "\n", `\n`)
}

// selector is the `{k="v", k2=~"v2"}` PromQL selector ("" without selectors).
func (sh customAlertShape) selector() string {
	if len(sh.Items) == 0 {
		return ""
	}
	parts := make([]string, len(sh.Items))
	for i, it := range sh.Items {
		parts[i] = it.key + it.op + `"` + escapeSelectorValue(it.value) + `"`
	}
	return "{" + strings.Join(parts, ", ") + "}"
}

var singleUnitDuration = regexp.MustCompile(`^(\d+)(h|m|s)$`)

// durationSeconds converts a single-unit duration (`4h`, `30m`, `10s`) — the
// forecast horizon enum — to integer seconds.
func durationSeconds(d string) (int, error) {
	m := singleUnitDuration.FindStringSubmatch(d)
	if m == nil {
		return 0, fmt.Errorf("cannot convert duration %q to integer seconds", d)
	}
	n, err := strconv.Atoi(m[1])
	if err != nil {
		return 0, err
	}
	return n * map[string]int{"h": 3600, "m": 60, "s": 1}[m[2]], nil
}

func record(name, expr string) orderedMap {
	return orderedMap{{"record", name}, {"expr", expr}}
}

func thresholdRecord(id, metric string) orderedMap {
	inner := `max by(tenant, version, severity, name, mode) ` +
		`(user_threshold{component="custom", metric="` + metric + `", recipe_id="` + id + `"})`
	return record("custom:threshold:"+id, normVersion(inner))
}

func metricRecord(id string, sh customAlertShape, sel string) orderedMap {
	g := gbSuffix(sh.GroupBy)
	var inner string
	switch sh.Recipe {
	case "threshold":
		inner = "max by(tenant, version" + g + ") (" + sh.Metric + sel + ")"
	case "rate":
		inner = "sum by(tenant, version" + g + ") (rate(" + sh.Metric + sel + "[" + sh.Window + "]))"
	case "ratio":
		inner = "sum by(tenant" + g + ") (rate(" + sh.Metric + sel + "[" + sh.Window + "]))\n" +
			"  /\n" +
			"(sum by(tenant" + g + ") (rate(" + sh.Denominator + sel + "[" + sh.Window + "])) > 0)"
	case "p99_latency":
		inner = "histogram_quantile(" + sh.Quantile + ",\n" +
			"  sum by(le, tenant, version" + g + ") (rate(" + sh.Metric + "_bucket" + sel + "[" + sh.Window + "])))"
	}
	return record("custom:metric:"+id, normVersion(inner))
}

// forecastRecords (ADR-024 §Forecast Recipe): custom:fcbase:{id} is the base
// aggregate (ratio mode: avail/capacity headroom; raw mode: the gauge), and
// custom:metric:{id} is predict_linear over it plus a cold-start gate. The
// lookback is max(2·horizon, 1h), platform-derived (not tenant-settable), and
// every duration is integer seconds so `[lookback]` is never a bad duration.
func forecastRecords(id string, sh customAlertShape, sel string) ([]any, error) {
	hs, err := durationSeconds(sh.Horizon)
	if err != nil {
		return nil, err
	}
	lb := 2 * hs
	if lb < 3600 {
		lb = 3600
	}
	h, l := strconv.Itoa(hs), strconv.Itoa(lb)
	g := gbSuffix(sh.GroupBy)
	base := "custom:fcbase:" + id
	var baseInner, predictInner string
	if sh.Capacity != "" {
		// ratio mode: a headroom ratio (avail/capacity); >0 guard → no +Inf.
		baseInner = "sum by(tenant" + g + ") (" + sh.Metric + sel + ")\n" +
			"  /\n" +
			"(sum by(tenant" + g + ") (" + sh.Capacity + sel + ") > 0)"
		// clamp_min(…, 0): a negative headroom is meaningless and firing is
		// unchanged. `base < band`: "predicted low AND currently low" kills the
		// transient write-burst false positive; the tenant threshold is
		// bounded below the band at load so the band cannot neuter it.
		predictInner = "clamp_min(predict_linear(" + base + "[" + l + "s], " + h + "), 0)\n" +
			"  and\n" +
			base + " < " + strconv.FormatFloat(contract.Forecast.CurrentBand, 'f', -1, 64) + "\n" +
			"  and\n" +
			"count_over_time(" + base + "[" + l + "s]) > " + strconv.Itoa(forecastMinSamples)
	} else {
		// raw mode: an arbitrary gauge — no [0,1] clamp, no band (TRK-327, #936).
		baseInner = "max by(tenant, version" + g + ") (" + sh.Metric + sel + ")"
		predictInner = "predict_linear(" + base + "[" + l + "s], " + h + ")\n" +
			"  and\n" +
			"count_over_time(" + base + "[" + l + "s]) > " + strconv.Itoa(forecastMinSamples)
	}
	return []any{record(base, baseInner), record("custom:metric:"+id, normVersion(predictInner))}, nil
}

// sloTier is the contract's (long window, short window, bad-event factor) for
// a severity (ADR-031 §2).
func sloTier(sev string) (long, short string, factor int, err error) {
	for _, t := range contract.SloBurnRate.Tiers {
		if t.Severity == sev {
			return t.Long, t.Short, t.MinEventsFactor, nil
		}
	}
	return "", "", 0, fmt.Errorf("slo_burn_rate has no tier for severity %q", sev)
}

// sloBurnRecords (ADR-031 §2): the SLI error ratio per burn window and the
// absolute bad-event count per short window. No maintenance `unless` —
// measurement is never suppressed (guardrail #4), only the core is.
func sloBurnRecords(id string, sh customAlertShape, sel string) []any {
	var out []any
	var long, short []string
	for _, t := range contract.SloBurnRate.Tiers {
		long = append(long, t.Long)
		short = append(short, t.Short)
	}
	// Ratio windows per tier (long, short) in tier order; bad counts per short.
	var ratioWins []string
	for i := range contract.SloBurnRate.Tiers {
		ratioWins = append(ratioWins, long[i], short[i])
	}
	for _, w := range ratioWins {
		inner := "sum by(tenant) (rate(" + sh.Metric + sel + "[" + w + "]))\n" +
			"  /\n" +
			"(sum by(tenant) (rate(" + sh.Denominator + sel + "[" + w + "])) > 0)"
		out = append(out, record("custom:metric:"+id+":"+w, normVersion(inner)))
	}
	for _, w := range short {
		out = append(out, record("custom:bad:"+id+":"+w, normVersion("sum by(tenant) (increase("+sh.Metric+sel+"["+w+"]))")))
	}
	return out
}

const maintenanceTail = "unless on(tenant)\n(user_state_filter{filter=\"maintenance\"} == 1)"

// sloCoreRecord (ADR-031 §2): the multi-window AND — long and short ratio
// above the exporter-resolved threshold, and the bad-event floor (min_events ×
// the tier factor, a compile-time literal). Only the long-window gate carries
// group_left(name, mode); `and on(tenant)` keeps left-hand labels.
func sloCoreRecord(id, sev string, minEvents int) (orderedMap, error) {
	long, short, factor, err := sloTier(sev)
	if err != nil {
		return nil, err
	}
	thr := `custom:threshold:` + id + `{severity="` + sev + `"}`
	thrDefault := `custom:threshold:` + id + `{version="default", severity="` + sev + `"}`
	gate := func(win string, carry bool) string {
		gl := ""
		if carry {
			gl = " group_left(name, mode)"
		}
		return "(\n" +
			"  (\n" +
			"    custom:metric:" + id + ":" + win + "\n" +
			"    > on(tenant, version)" + gl + "\n" +
			"      " + thr + "\n" +
			"  )\n" +
			"  or\n" +
			"  (\n" +
			"    (\n" +
			"      custom:metric:" + id + ":" + win + "\n" +
			"      unless on(tenant, version)\n" +
			"        " + thr + "\n" +
			"    )\n" +
			"    > on(tenant)" + gl + "\n" +
			"      " + thrDefault + "\n" +
			"  )\n" +
			")"
	}
	expr := gate(long, true) + "\n" +
		"and on(tenant)\n" +
		gate(short, false) + "\n" +
		"and on(tenant)\n" +
		"(custom:bad:" + id + ":" + short + " > " + strconv.Itoa(minEvents*factor) + ")\n" +
		maintenanceTail
	return record("custom:"+id+":"+sev+":core", expr), nil
}

// eqCoreRecord is the `==` any-match core (#810 + #819): equality has no
// meaningful aggregate (max masks a matching replica), so the RAW per-series
// metric is compared to the per-tenant threshold FIRST, then existence is
// aggregated. The raw metric is inlined (no per-pod recording rule).
func eqCoreRecord(id, sev, metric, sel string) orderedMap {
	raw := normVersion(metric + sel)
	thr := `custom:threshold:` + id + `{severity="` + sev + `"}`
	core := "(\n" +
		"  max by(tenant, version, name, mode) (\n" +
		"    " + raw + "\n" +
		"    == on(tenant, version) group_left(name, mode)\n" +
		"      " + thr + "\n" +
		"  )\n" +
		"  or\n" +
		"  max by(tenant, version, name, mode) (\n" +
		"    (\n" +
		"      " + raw + "\n" +
		"      unless on(tenant, version)\n" +
		"        " + thr + "\n" +
		"    )\n" +
		"    == on(tenant) group_left(name, mode)\n" +
		"      custom:threshold:" + id + `{version="default", severity="` + sev + `"}` + "\n" +
		"  )\n" +
		")"
	return record("custom:"+id+":"+sev+":core", core+"\n"+maintenanceTail)
}

func coreRecord(id string, sh customAlertShape, sev, sel string) orderedMap {
	if sh.Op == "==" {
		return eqCoreRecord(id, sev, sh.Metric, sel)
	}
	thr := `custom:threshold:` + id + `{severity="` + sev + `"}`
	var core string
	if sh.Recipe == "absence" {
		core = "(\n" +
			"  " + thr + "\n" +
			"  unless on(tenant)\n" +
			"  count by(tenant) (count_over_time(" + sh.Metric + sel + "[" + sh.Window + "]) > 0)\n" +
			")"
	} else {
		core = "(\n" +
			"  (\n" +
			"    custom:metric:" + id + "\n" +
			"    " + sh.Op + " on(tenant, version) group_left(name, mode)\n" +
			"      " + thr + "\n" +
			"  )\n" +
			"  or\n" +
			"  (\n" +
			"    (\n" +
			"      custom:metric:" + id + "\n" +
			"      unless on(tenant, version)\n" +
			"        " + thr + "\n" +
			"    )\n" +
			"    " + sh.Op + " on(tenant) group_left(name, mode)\n" +
			"      custom:threshold:" + id + `{version="default", severity="` + sev + `"}` + "\n" +
			"  )\n" +
			")"
	}
	return record("custom:"+id+":"+sev+":core", core+"\n"+maintenanceTail)
}

func alertRule(id string, sh customAlertShape, sev, sel, op string) orderedMap {
	core := "custom:" + id + ":" + sev + ":core"
	expr := "(\n" +
		"  " + core + "\n" +
		"  * on(tenant) group_left(runbook_url, owner, tier)\n" +
		"    tenant_metadata_info\n" +
		")\n" +
		"or\n" +
		"(\n" +
		"  " + core + "\n" +
		"  unless on(tenant) tenant_metadata_info\n" +
		")"
	sevEn, sevZh := "", ""
	if sev == "critical" {
		sevEn, sevZh = "CRITICAL ", "達臨界 "
	}
	descEn := `value {{ $value | printf "%.2f" }} crossed the configured threshold`
	descZh := `值 {{ $value | printf "%.2f" }} 已越過設定閾值`
	if op == "==" {
		descEn = `value {{ $value | printf "%.0f" }} matched the configured code`
		descZh = `值 {{ $value | printf "%.0f" }} 等於設定代碼`
	}
	return orderedMap{
		{"alert", "Custom_" + id},
		{"expr", expr},
		{"for", sh.For},
		{"labels", orderedMap{
			{"severity", sev},
			{"tenant", "{{ $labels.tenant }}"},
			{"recipe", sh.Recipe},
			{"component", "custom"},
			{"mode", "{{ $labels.mode }}"},
		}},
		{"annotations", orderedMap{
			{"summary", sevEn + "Custom alert [{{ $labels.name }}] for {{ $labels.tenant }}"},
			{"summary_zh", "{{ $labels.tenant }} 的自訂告警 [{{ $labels.name }}] " + sevZh + "觸發"},
			{"description", sh.Recipe + " on " + sh.Metric + sel + ": " + descEn},
			{"description_zh", sh.Metric + sel + " 的 " + sh.Recipe + ": " + descZh},
			{"runbook_url", "{{ $labels.runbook_url }}"},
			{"owner", "{{ $labels.owner }}"},
			{"tier", "{{ $labels.tier }}"},
		}},
	}
}

// emitShape emits (recording rules, alert rules) for one shape.
func emitShape(id string, sh customAlertShape, sevs []string) ([]any, []any) {
	sel := sh.selector()
	recording := []any{thresholdRecord(id, sh.Metric)}
	var alerts []any
	if sh.Recipe == "slo_burn_rate" {
		recording = append(recording, sloBurnRecords(id, sh, sel)...)
		for _, sev := range sevs {
			core, err := sloCoreRecord(id, sev, sh.MinEvents)
			if err != nil {
				panic(err) // a served slo severity the contract has no tier for
			}
			recording = append(recording, core)
		}
		for _, sev := range sevs {
			// slo alert = the standard alert + metric_group (so Severity
			// Dedup's inhibit matches fast+slow firing together) + slo_burn
			// (the storm fan-out discriminator, ADR-031 guardrail #3).
			a := alertRule(id, sh, sev, sel, ">")
			labels := a.get("labels").(orderedMap)
			labels = append(labels, orderedKV{"metric_group", "slo_{{ $labels.name }}"}, orderedKV{"slo_burn", "true"})
			a[3].Value = labels
			alerts = append(alerts, a)
		}
		return recording, alerts
	}
	if sh.Recipe == "forecast" {
		fc, err := forecastRecords(id, sh, sel)
		if err != nil {
			panic(err) // the horizon enum is single-unit; a contract bug
		}
		recording = append(recording, fc...)
	} else if sh.Recipe != "absence" && sh.Op != "==" {
		recording = append(recording, metricRecord(id, sh, sel))
	}
	for _, sev := range sevs {
		recording = append(recording, coreRecord(id, sh, sev, sel))
	}
	for _, sev := range sevs {
		alerts = append(alerts, alertRule(id, sh, sev, sel, sh.Op))
	}
	return recording, alerts
}
