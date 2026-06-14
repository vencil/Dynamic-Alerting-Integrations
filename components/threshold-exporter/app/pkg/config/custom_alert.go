package config

// Custom Alerts data-plane emission (ADR-024 能力 B, #741 S3a).
//
// A tenant declares `_custom_alerts` (a list) in conf.d; the directory scanner
// stores the list as a YAML string in ScheduledValue.Default (parse.go). Here we
// re-parse it and emit one user_threshold series per declaration:
//
//	user_threshold{component="custom", metric=<metric>, severity=<sev>,
//	               recipe_id=<slug>, name=<name>, mode=<page|silent>} = <value>
//
// The compiled rule pack (S1+S2) joins exactly this shape. recipe_id is the
// SHAPE slug — it must be byte-identical to the Python compiler's
// scripts/tools/dx/custom_alerts/shape.py::recipe_id (cross-language contract,
// pinned by tests/dx/fixtures/recipe_id_vectors.json). Any change to the slug
// algorithm MUST update Go + Python + the golden vector in the same PR, or every
// on(tenant) group_left join silently matches the empty set (the #731 class).

import (
	"errors"
	"fmt"
	"log"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

// errCustomAlertDisabled signals a `threshold: "disable"` three-state opt-out:
// the entry is valid but emits no series (not a parse error → no error gauge).
var errCustomAlertDisabled = errors.New("custom alert disabled")

// logCustomAlertError surfaces a malformed custom-alert at ERROR (paired with
// the da_custom_alert_parse_errors gauge so a silent skip is observable).
func logCustomAlertError(tenant, name string, err error) {
	log.Printf("ERROR: tenant=%s: custom alert %q rejected: %v", tenant, name, err)
}

// flexStr accepts a YAML scalar that may be quoted-string OR bare number and
// stores its raw textual form — so `quantile: 0.99` and `quantile: "0.99"` both
// yield "0.99", matching Python's str(quantile) in the slug contract.
type flexStr string

func (f *flexStr) UnmarshalYAML(n *yaml.Node) error {
	*f = flexStr(n.Value)
	return nil
}

// CustomAlertSpec is one tenant-authored custom alert declaration. Field set +
// yaml tags mirror docs/schemas/tenant-config.schema.json customAlertInstance.
type CustomAlertSpec struct {
	Recipe            string            `yaml:"recipe"`
	Name              string            `yaml:"name"`
	Metric            string            `yaml:"metric"`
	Op                string            `yaml:"op"`
	Window            string            `yaml:"window"`
	Threshold         string            `yaml:"threshold"`
	Quantile          flexStr           `yaml:"quantile"`
	DenominatorMetric string            `yaml:"denominator_metric"`
	Horizon           string            `yaml:"horizon"`         // forecast: predict-ahead distance
	CapacityMetric    string            `yaml:"capacity_metric"` // forecast ratio mode: denominator
	Selectors         map[string]string `yaml:"selectors"`
	SelectorsRe       map[string]string `yaml:"selectors_re"`
	Mode              string            `yaml:"mode"`
	For               string            `yaml:"for"`
	GroupBy           []string          `yaml:"group_by"` // bounded per-dimension eval (e.g. per PVC)
}

var (
	customAlertRecipes = map[string]bool{
		"threshold": true, "rate": true, "ratio": true, "absence": true, "p99_latency": true,
		"forecast": true,
	}
	// op → slug. Keep in sync with shape.py OP_SLUG.
	customAlertOpSlug = map[string]string{">": "gt", ">=": "ge", "<": "lt", "<=": "le", "==": "eq"}
	// permitted forecast horizons (predict-ahead distance) — enum-bounded, enters
	// the recipe_id slug. MUST match shape.py ALLOWED_HORIZON + the schema enum.
	customAlertHorizonValid = map[string]bool{
		"1h": true, "2h": true, "4h": true, "12h": true, "24h": true, "48h": true,
	}
	// metric/label name charset (no colon → cannot reference recording rules;
	// no braces/operators → PromQL injection structurally impossible). Mirrors
	// shape.py _METRIC_RE / _LABEL_RE.
	customAlertNameRe = regexp.MustCompile(`^[a-zA-Z_][a-zA-Z0-9_]*$`)
	// non-identifier chars → '_' (deterministic, locale-free). Mirrors _sanitise.
	customAlertSanitiseRe = regexp.MustCompile(`[^a-zA-Z0-9_]`)
	// reserved labels a tenant may not pin as a selector (would hijack the
	// vectorisation join keys / platform dimensions). Mirrors RESERVED_LABELS.
	customAlertReservedLabels = map[string]bool{
		"tenant": true, "version": true, "__name__": true, "severity": true,
		"recipe": true, "recipe_id": true, "name": true, "mode": true,
	}
	// permitted `for` values — enum-bounded (TRK-326 / #751) so `for` entering
	// the recipe_id slug keeps cardinality a small constant per base-shape (no
	// O(M)→O(N) blow-up). MUST match the schema enum in
	// docs/schemas/tenant-config.schema.json.
	customAlertForValid = map[string]bool{
		"0s": true, "1m": true, "5m": true, "15m": true, "30m": true, "1h": true,
	}
	// permitted group_by dimensions (ADR-024 §Addendum disk recipes) — bounded so a
	// per-PVC disk-fill alert can fire per volume (a small full disk is not masked
	// by a large empty one in a by(tenant) sum) without unbounding cardinality.
	// Each entry enters the recipe_id slug. MUST match shape.py ALLOWED_GROUP_BY +
	// the schema enum.
	customAlertGroupByValid = map[string]bool{"persistentvolumeclaim": true}
)

func customAlertSanitise(s string) string {
	return customAlertSanitiseRe.ReplaceAllString(s, "_")
}

// customAlertGroupBy validates + canonicalises group_by into a sorted, deduped
// slice. Empty → nil, so a recipe without group_by keeps a byte-identical slug
// (existing golden vectors unaffected). Bounded to customAlertGroupByValid; sorted
// for cross-language slug determinism. MUST match shape.py::_normalize_group_by.
func customAlertGroupBy(spec CustomAlertSpec) ([]string, error) {
	if len(spec.GroupBy) == 0 {
		return nil, nil
	}
	seen := map[string]bool{}
	var out []string
	for _, label := range spec.GroupBy {
		if !customAlertGroupByValid[label] {
			return nil, fmt.Errorf("group_by label %q must be one of [persistentvolumeclaim] "+
				"(bounded whitelist, ADR-024 Addendum)", label)
		}
		if !seen[label] {
			seen[label] = true
			out = append(out, label)
		}
	}
	sort.Strings(out)
	return out, nil
}

func validateCustomAlertMetric(metric, field string) error {
	if !customAlertNameRe.MatchString(metric) {
		return fmt.Errorf("%s %q is not a bare metric name (^[a-zA-Z_][a-zA-Z0-9_]*$); "+
			"label filtering must use selectors/selectors_re, never inline PromQL", field, metric)
	}
	return nil
}

type selectorItem struct {
	op, key, value string // op is "=" (selectors) or "=~" (selectors_re)
}

// selectorItems returns the validated (op,key,value) triples sorted by (key, op)
// — deterministic, independent of map iteration order (cross-language slug
// contract). Mirrors shape.py _selector_items.
func selectorItems(spec CustomAlertSpec) ([]selectorItem, error) {
	var items []selectorItem
	for k, v := range spec.Selectors {
		items = append(items, selectorItem{"=", k, v})
	}
	for k, v := range spec.SelectorsRe {
		items = append(items, selectorItem{"=~", k, v})
	}
	for _, it := range items {
		if !customAlertNameRe.MatchString(it.key) {
			return nil, fmt.Errorf("selector label %q is not a valid label name", it.key)
		}
		if customAlertReservedLabels[it.key] {
			return nil, fmt.Errorf("selector label %q is reserved and may not be pinned", it.key)
		}
	}
	sort.SliceStable(items, func(i, j int) bool {
		if items[i].key != items[j].key {
			return items[i].key < items[j].key
		}
		return items[i].op < items[j].op
	})
	return items, nil
}

// RecipeID computes the deterministic shape slug. MUST stay byte-identical to
// scripts/tools/dx/custom_alerts/shape.py::recipe_id (golden vector test).
func RecipeID(spec CustomAlertSpec) (string, error) {
	if !customAlertRecipes[spec.Recipe] {
		return "", fmt.Errorf("unknown recipe %q", spec.Recipe)
	}
	if err := validateCustomAlertMetric(spec.Metric, "metric"); err != nil {
		return "", err
	}
	items, err := selectorItems(spec)
	if err != nil {
		return "", err
	}

	parts := []string{spec.Recipe, spec.Metric}
	for _, it := range items {
		prefix := "s"
		if it.op == "=~" {
			prefix = "sre"
		}
		parts = append(parts, prefix+"_"+it.key+"_"+it.value)
	}
	op := spec.Op
	if op == "" {
		op = ">"
	}
	// `==` is threshold-recipe-only (#810): exact match suits integer status/
	// error codes on a RAW gauge; other recipes emit computed floats where
	// equality is fragile. This gate runs BEFORE the absence short-circuit so it
	// also rejects absence+"==" (op is meaningless for a presence check) — keeping
	// the imperative gate in lockstep with the JSON-schema if/then editor-guard,
	// so an API/GitOps-accepted spec can't later fail to render in the Portal.
	// Mirrors shape.py.
	if op == "==" && spec.Recipe != "threshold" {
		return "", fmt.Errorf("op \"==\" is only allowed for the threshold recipe "+
			"(raw-gauge status-code match); %s does not support it", spec.Recipe)
	}
	if spec.Recipe == "absence" {
		parts = append(parts, "absent")
	} else {
		slug, ok := customAlertOpSlug[op]
		if !ok {
			return "", fmt.Errorf("unknown op %q", op)
		}
		parts = append(parts, slug)
	}
	if spec.Recipe == "forecast" {
		// forecast: lookback derives from `horizon` (the compiler computes
		// max(2·horizon,1h)), so the tenant supplies `horizon` (enum), NOT a
		// window — `h{horizon}` takes the `w{window}` slot. capacity_metric
		// present → ratio mode → `den_` slot (raw mode omits it). MUST match
		// shape.py recipe_id's forecast branch.
		h := spec.Horizon
		if h == "" {
			return "", fmt.Errorf("forecast recipe requires horizon (one of 1h/2h/4h/12h/24h/48h)")
		}
		if !customAlertHorizonValid[h] {
			return "", fmt.Errorf("horizon %q must be one of 1h/2h/4h/12h/24h/48h", h)
		}
		parts = append(parts, "h"+h)
		if spec.CapacityMetric != "" {
			if err := validateCustomAlertMetric(spec.CapacityMetric, "capacity_metric"); err != nil {
				return "", err
			}
			parts = append(parts, "den_"+spec.CapacityMetric)
		}
	} else {
		parts = append(parts, "w"+spec.Window)
		if spec.Recipe == "p99_latency" {
			q := string(spec.Quantile)
			if q == "" {
				q = "0.99"
			}
			parts = append(parts, "q"+q)
		}
		if spec.Recipe == "ratio" {
			if err := validateCustomAlertMetric(spec.DenominatorMetric, "denominator_metric"); err != nil {
				return "", err
			}
			parts = append(parts, "den_"+spec.DenominatorMetric)
		}
	}
	// `for` enters the slug — it is part of the rule identity (Prometheus `for:`
	// is a control-plane STATIC rule attribute, unlike data-plane `mode` which
	// rides group_left). Two tenants sharing every other param but a different
	// `for` are genuinely different rules; without `for` in the slug the
	// vectorized rule silently froze to one tenant's `for` (TRK-326 / #751).
	// Always emitted; default 1m. MUST stay byte-identical to shape.py::recipe_id.
	forVal := spec.For
	if forVal == "" {
		forVal = "1m"
	}
	parts = append(parts, "for"+forVal)

	// group_by dimensions (ADR-024 §Addendum): per-dimension eval (e.g. per PVC).
	// Only for value-crossing recipes — reject for absence (a per-tenant presence
	// check) and op "==" (exact code match isn't per-PVC), keeping the eq/absence
	// cores per-tenant. Appended LAST and only when present → no group_by keeps a
	// byte-identical slug. MUST stay byte-identical to shape.py::recipe_id.
	//   SLUG-ORDER CONTRACT: a NEW slug field added later MUST go in the SAME
	//   position in shape.py::recipe_id (the golden vector enforces parity). Keep
	//   new fields only-when-present like gb_ (an ALWAYS-appended field — like
	//   `for` — re-slugs every existing rule, a deliberate breaking migration).
	//   FORESIGHT: the "==" rejection is safe ONLY because the whitelist is PVC-only
	//   (error codes aren't per-PVC). If a topology dim (e.g. pod) is whitelisted, a
	//   tenant may legitimately want group_by:[pod] + op:"==" — then relax this AND
	//   thread group_by into the eq-core aggregation (recipes.py::_eq_core_record).
	gb, err := customAlertGroupBy(spec)
	if err != nil {
		return "", err
	}
	if len(gb) > 0 && (spec.Recipe == "absence" || op == "==") {
		what := "op \"==\""
		if spec.Recipe == "absence" {
			what = "the absence recipe"
		}
		return "", fmt.Errorf("group_by (per-dimension eval) is not supported for %s — "+
			"it applies only to value-crossing recipes (ADR-024 Addendum)", what)
	}
	for _, g := range gb {
		parts = append(parts, "gb_"+g)
	}
	return customAlertSanitise(strings.Join(parts, "__")), nil
}

// parseCustomAlertThreshold splits "value[:severity]" → (value, severity).
// severity defaults to "warning"; mirrors shape.py parse_threshold.
func parseCustomAlertThreshold(threshold string) (value, severity string, err error) {
	raw := strings.TrimSpace(threshold)
	if i := strings.LastIndex(raw, ":"); i >= 0 {
		value = strings.TrimSpace(raw[:i])
		severity = strings.ToLower(strings.TrimSpace(raw[i+1:]))
	} else {
		value, severity = raw, "warning"
	}
	if severity != "warning" && severity != "critical" {
		return "", "", fmt.Errorf("threshold severity %q must be warning or critical", severity)
	}
	return value, severity, nil
}

// resolveOneCustomAlert validates one spec and builds its user_threshold-bearing
// ResolvedThreshold (component="custom"). No version label is set: the rule's
// normalize layer fills empty version → "default" (no-version main path).
func resolveOneCustomAlert(tenant string, spec CustomAlertSpec) (ResolvedThreshold, error) {
	if spec.Recipe == "" || spec.Name == "" || spec.Metric == "" || spec.Threshold == "" {
		return ResolvedThreshold{}, fmt.Errorf("missing required field (recipe/name/metric/threshold)")
	}
	// recipe-aware shaping duration: forecast supplies `horizon` (required + enum
	// validated in RecipeID), every other recipe supplies `window` (empty window
	// → invalid PromQL like rate(m[])).
	if spec.Recipe != "forecast" && spec.Window == "" {
		return ResolvedThreshold{}, fmt.Errorf("missing required field window")
	}
	if !customAlertNameRe.MatchString(spec.Name) {
		return ResolvedThreshold{}, fmt.Errorf("name %q is not a valid identifier", spec.Name)
	}
	rid, err := RecipeID(spec)
	if err != nil {
		return ResolvedThreshold{}, err
	}
	valueStr, severity, err := parseCustomAlertThreshold(spec.Threshold)
	if err != nil {
		return ResolvedThreshold{}, err
	}
	// Three-state: `threshold: "disable"` (schema-valid thresholdScalar) turns the
	// custom alert OFF by emitting NO user_threshold series — the vectorized rule
	// then simply doesn't fire for this tenant (others sharing the shape are
	// unaffected). This is a clean opt-out, NOT a parse error, so it must not
	// raise da_custom_alert_parse_errors. (Same "absent = disabled" semantics as
	// regular numeric thresholds.)
	if isDisabled(strings.ToLower(strings.TrimSpace(valueStr))) {
		return ResolvedThreshold{}, errCustomAlertDisabled
	}
	value, err := strconv.ParseFloat(valueStr, 64)
	if err != nil {
		return ResolvedThreshold{}, fmt.Errorf("threshold value %q is not numeric: %w", valueStr, err)
	}
	// ParseFloat accepts "NaN"/"Inf" — reject them: a non-finite threshold would
	// emit a nonsensical comparison series (e.g. `metric > NaN` never fires).
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return ResolvedThreshold{}, fmt.Errorf("threshold value %q must be finite", valueStr)
	}
	// forecast ratio mode: the threshold is a headroom-fraction floor
	// (avail/capacity), so it must be in (0,1) — a floor >= 1 with op "<" would
	// fire permanently (the ratio is always <= 1).
	if spec.Recipe == "forecast" && spec.CapacityMetric != "" && (value <= 0 || value >= 1) {
		return ResolvedThreshold{}, fmt.Errorf("forecast ratio-mode threshold %v must be in (0,1)", value)
	}
	mode := spec.Mode
	if mode == "" {
		mode = "page"
	}
	// reject unsupported mode (typo): only page|silent ride the data plane; an
	// unknown value would surface a bogus mode label that S8 routing can't handle.
	if mode != "page" && mode != "silent" {
		return ResolvedThreshold{}, fmt.Errorf("mode %q must be page or silent", mode)
	}
	// `for` must be one of the enum-bounded values (TRK-326): it enters the
	// recipe_id slug, so an out-of-enum value would silently spawn a distinct
	// shape/rule and bloat cardinality. Default 1m (matches schema default).
	forVal := spec.For
	if forVal == "" {
		forVal = "1m"
	}
	if !customAlertForValid[forVal] {
		return ResolvedThreshold{}, fmt.Errorf("for %q is not allowed (one of 0s/1m/5m/15m/30m/1h)", forVal)
	}
	return ResolvedThreshold{
		Tenant:    tenant,
		Metric:    spec.Metric,
		Component: "custom",
		Value:     value,
		Severity:  severity,
		CustomLabels: map[string]string{
			"recipe_id": rid,
			"name":      spec.Name,
			"mode":      mode,
		},
	}, nil
}

// resolveTenantCustomAlerts re-parses a tenant's _custom_alerts YAML string and
// returns the resolved user_threshold rows plus the count of malformed entries
// (for the da_custom_alert_parse_errors gauge — fail-loud, never silent-skip).
func resolveTenantCustomAlerts(tenant string, overrides map[string]ScheduledValue) ([]ResolvedThreshold, int) {
	sv, ok := overrides["_custom_alerts"]
	if !ok || strings.TrimSpace(sv.Default) == "" {
		return nil, 0
	}
	var specs []CustomAlertSpec
	if err := yaml.Unmarshal([]byte(sv.Default), &specs); err != nil {
		// whole block unparseable → count as 1 error; the tenant gets NO custom
		// alerts but the rest of its config is unaffected.
		logCustomAlertError(tenant, "<block>", fmt.Errorf("cannot parse _custom_alerts: %w", err))
		return nil, 1
	}
	var out []ResolvedThreshold
	errCount := 0
	for _, spec := range specs {
		rt, err := resolveOneCustomAlert(tenant, spec)
		if errors.Is(err, errCustomAlertDisabled) {
			continue // three-state opt-out: no series, NOT an error
		}
		if err != nil {
			logCustomAlertError(tenant, spec.Name, err)
			errCount++
			continue
		}
		out = append(out, rt)
	}
	return out, errCount
}

// MaxCustomRecipesDefault mirrors the Python loader's MAX_CUSTOM_RECIPES_DEFAULT
// (S4) — the per-tenant OWN-recipe cap. The CI compiler is the authority; this is
// the tenant-api shift-left preflight's matching value (ADR §S5). KEEP IN SYNC
// with scripts/tools/dx/custom_alerts/loader.py::MAX_CUSTOM_RECIPES_DEFAULT.
const MaxCustomRecipesDefault = 20

// ValidateTenantCustomAlerts is the tenant-api shift-left preflight (ADR §S5):
// the Go-native, in-process equivalent of the Python compiler's per-tenant
// validation. It validates a tenant's `_custom_alerts` block (as stored in
// `overrides` by the parse.go SequenceNode passthrough) and returns human-readable
// violation strings (nil if all valid).
//
// Reuses resolveOneCustomAlert for per-recipe spec validity (so the preflight is,
// by construction, at least as strict as the exporter), plus within-tenant
// name / (recipe_id, severity) uniqueness and the OWN-recipe cap. STATELESS w.r.t.
// the rest of the conf.d tree: cross-inheritance collisions and compiler template
// bugs are the CI compiler's authority (ADR §S5 OQ-S5-1 — the local disk is not
// the authoritative global SOT). `threshold: "disable"` (three-state opt-out) is
// VALID and does not count toward the cap.
func ValidateTenantCustomAlerts(tenant string, overrides map[string]ScheduledValue, maxOwnRecipes int) []string {
	sv, ok := overrides["_custom_alerts"]
	if !ok || strings.TrimSpace(sv.Default) == "" {
		return nil
	}
	var specs []CustomAlertSpec
	if err := yaml.Unmarshal([]byte(sv.Default), &specs); err != nil {
		return []string{fmt.Sprintf("_custom_alerts is not a valid recipe list: %v", err)}
	}
	var violations []string
	nameSeen := map[string]int{} // name → 0-based index of first occurrence
	sevSeen := map[string]int{}  // "recipe_id|severity" → index
	ownCount := 0
	for i, spec := range specs {
		rt, err := resolveOneCustomAlert(tenant, spec)
		if errors.Is(err, errCustomAlertDisabled) {
			continue // three-state opt-out: valid, not counted toward the cap
		}
		if err != nil {
			label := spec.Name
			if label == "" {
				label = "<unnamed>"
			}
			violations = append(violations, fmt.Sprintf("_custom_alerts[%d] (%s): %v", i, label, err))
			continue
		}
		ownCount++
		// within-tenant uniqueness — the CI compiler enforces these globally; the
		// preflight catches the within-PUT subset for fast feedback (PUT is a
		// full-overlay, so the body is the tenant's complete own set).
		if prev, dup := nameSeen[spec.Name]; dup {
			violations = append(violations,
				fmt.Sprintf("_custom_alerts[%d]: duplicate name %q (also at [%d]); names must be unique per tenant", i, spec.Name, prev))
		} else {
			nameSeen[spec.Name] = i
		}
		sevKey := rt.CustomLabels["recipe_id"] + "|" + rt.Severity
		if prev, dup := sevSeen[sevKey]; dup {
			violations = append(violations,
				fmt.Sprintf("_custom_alerts[%d]: a %s alert with the same shape already exists at [%d] (one per shape+severity)", i, rt.Severity, prev))
		} else {
			sevSeen[sevKey] = i
		}
	}
	if ownCount > maxOwnRecipes {
		violations = append(violations,
			fmt.Sprintf("%d custom-alert recipes exceeds the per-tenant cap (%d); reduce the tenant's own _custom_alerts", ownCount, maxOwnRecipes))
	}
	return violations
}
