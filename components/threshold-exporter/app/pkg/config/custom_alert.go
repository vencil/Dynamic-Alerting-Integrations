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
// The compiled rule pack (S1+S2, CompileCustomAlertPack) joins exactly this
// shape. recipe_id is the SHAPE slug, the one function both the served row and
// the compiled rules take it from (#3229), pinned by
// tests/dx/fixtures/recipe_id_vectors.json: a change to the slug algorithm
// renames every deployed rule and series, so it updates the golden vector in
// the same PR, deliberately.

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"math"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

// errCustomAlertDisabled signals a `threshold: "disable"` three-state opt-out:
// the entry is valid but emits no series (not a parse error → no error gauge).
var errCustomAlertDisabled = errors.New("custom alert disabled")

// customAlertThresholdRefusal is resolveOneCustomAlert's error for a
// `threshold:` the threshold rows' grammar refuses (#3209): a `:<severity>`
// other than warning / critical, or a value half parseThresholdNumber does
// not read — refused is RefusedNonFinite (NaN, ±Inf) or RefusedUnparsed
// (`abc`, `70:high`, `1e999`), the classes RefusedValueAt gives a threshold
// key. The text is the wrapped error's, unchanged. The walk copies refused
// into CustomAlertEntry.Refused, which da-guard reports as the threshold
// rows' value_not_served and served-values writes as custom_alerts[].refused.
// A rejection for any other field (window, name, recipe …) is not one.
type customAlertThresholdRefusal struct {
	refused string
	err     error
}

func (e *customAlertThresholdRefusal) Error() string { return e.err.Error() }
func (e *customAlertThresholdRefusal) Unwrap() error { return e.err }

// forecastCurrentBand is the ratio-mode forecast's current-state band: the
// compiler gates a ratio-mode forecast on `custom:fcbase < band`, so a
// threshold >= band would be silently neutered. From recipe_contract.json.
var forecastCurrentBand = contract.Forecast.CurrentBand

// logCustomAlertError surfaces a malformed custom-alert at ERROR (paired with
// the da_custom_alert_parse_errors gauge so a silent skip is observable).
func logCustomAlertError(logf func(format string, args ...any), tenant, name string, err error) {
	logf("ERROR: tenant=%s: custom alert %q rejected: %v", tenant, name, err)
}

// flexStr accepts a YAML scalar that may be quoted-string OR bare number and
// stores its textual form. NB (#1017): for conf.d input the text seen here has
// ALREADY been re-canonicalised by parse.go's ScheduledValue passthrough
// (Decode→Marshal turns a bare `0.990` into `0.99`) — agreement with Python's
// str(quantile) for bare numbers was that coincidence, not raw-text fidelity,
// and YAML-dialect edge cases (PyYAML reads `95e-2` as a string, yaml.v3 as a
// float) still diverged. The schema now pins quantile to type:string (quoted),
// so both languages read the same authored text; flexStr stays lenient here so
// a legacy bare-number conf.d keeps resolving instead of erroring at runtime.
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
	// slo_burn_rate (ADR-031): objective REPLACES threshold (the SLO target
	// percentage in the open interval (0,100), or "disable" for the tri-state
	// opt-out). objective is STRING-ONLY (schema type:string, the #1017
	// quantile lesson) — taggedScalar defers the tag check to resolve time so
	// a bare-number objective rejects ONLY its own declaration, never the
	// whole block. slo_period scales the exporter-derived burn multipliers
	// only. min_events is a POINTER so a MISSING value (nil → default 10) is
	// distinguishable from an authored 0 (rejected); the strictInt type pins
	// the accept-set to bare YAML integers (see its doc).
	Objective taggedScalar `yaml:"objective"`
	SloPeriod string       `yaml:"slo_period"`
	MinEvents *strictInt   `yaml:"min_events"`
}

// taggedScalar captures a YAML scalar's raw text AND its resolved tag WITHOUT
// ever erroring at unmarshal time (deferred-error pattern): an UnmarshalYAML
// error inside one list element fails the WHOLE []CustomAlertSpec decode, so
// one malformed field would silently drop every other declaration the tenant
// authored (whole-block errCount=1). Capturing (value, tag) here and checking
// the tag at validation time keeps the fail-soft per-declaration semantics.
// tag=="" ⇔ the field was absent (UnmarshalYAML never ran).
type taggedScalar struct {
	value string
	tag   string
}

func (ts *taggedScalar) UnmarshalYAML(n *yaml.Node) error {
	ts.value = n.Value
	ts.tag = n.Tag
	return nil
}

// strictInt captures the min_events scalar for DEFERRED strict-integer
// validation (same never-error pattern as taggedScalar — an unmarshal-time
// error would poison the whole _custom_alerts block instead of dropping just
// the offending declaration). The accept-set is enforced in sloMinEvents:
// tag must be !!int (plain `var v int` would let yaml.v3 coerce a float by
// TRUNCATION, `min_events: 2.5` → 2, a value the schema rejects), raw must be
// digit-only, and the parsed value must be in [1, sloMinEventsMax].
type strictInt struct {
	raw string
	tag string
}

func (s *strictInt) UnmarshalYAML(n *yaml.Node) error {
	s.raw, s.tag = n.Value, n.Tag
	return nil
}

// Every enum, pattern and band below comes from recipe_contract.json (#3229);
// the comments say why each exists, the contract says what it holds.
var (
	customAlertRecipes = func() map[string]bool {
		m := map[string]bool{}
		for _, r := range contract.Recipes {
			m[r.Name] = true
		}
		return m
	}()
	customAlertOpSlug = contract.Op.Slug
	// recipes that may use op "==" (#810): exact match suits integer status/
	// error codes on a RAW gauge; other recipes emit computed floats.
	customAlertEqRecipes = stringSet(contract.Op.EqRecipes)
	// permitted forecast horizons (predict-ahead distance) — enum-bounded, enters
	// the recipe_id slug.
	customAlertHorizonValid = stringSet(contract.Horizon.Enum)
	// the tenant-chosen alert name (the `name` label).
	customAlertAlertNameRe = regexp.MustCompile(contract.Patterns.Name)
	// metric name charset (no colon → cannot reference recording rules; no
	// braces/operators → PromQL injection structurally impossible).
	customAlertMetricRe = regexp.MustCompile(contract.Patterns.Metric)
	// selector label-name charset.
	customAlertLabelRe = regexp.MustCompile(contract.Patterns.Label)
	// non-identifier chars → '_' (deterministic, locale-free). Mirrors _sanitise.
	customAlertSanitiseRe = regexp.MustCompile(`[^a-zA-Z0-9_]`)
	// window is a BARE PromQL token interpolated raw into rate(m[<window>]) — it
	// cannot be quote-escaped, so it must be allowlisted to the Go-duration charset.
	customAlertWindowRe = regexp.MustCompile(contract.Patterns.Window)
	// quantile / objective: DECIMAL-float charset only. strconv.ParseFloat
	// accepts Go hex-float literals ("0x1p-1"=0.5) and underscores; this pins
	// the accept-set to plain decimals. Anchored, so MatchString == fullmatch.
	customAlertQuantileRe = regexp.MustCompile(contract.Patterns.Decimal)
	// Go-template metacharacters forbidden in a selector VALUE: the value reaches the
	// alert-annotation template context where Prometheus evaluates {{ query … }}
	// across the whole TSDB. Reject so tenant data can never BECOME template code.
	customAlertTemplateMetachars = contract.TemplateMetachars
	// reserved labels a tenant may not pin as a selector (would hijack the
	// vectorisation join keys / platform dimensions).
	customAlertReservedLabels = stringSet(contract.ReservedLabels)
	// permitted `for` values — enum-bounded (TRK-326 / #751) so `for` entering
	// the recipe_id slug keeps cardinality a small constant per base-shape.
	customAlertForValid = stringSet(contract.For.Enum)
	// permitted group_by dimensions (ADR-024 §Addendum disk recipes) — bounded so a
	// per-PVC disk-fill alert can fire per volume without unbounding cardinality.
	// Each entry enters the recipe_id slug.
	customAlertGroupByValid = stringSet(contract.GroupBy.Enum)
	// permitted `mode` values: only these ride the data plane.
	customAlertModeValid = stringSet(contract.Mode.Enum)
	// permitted slo_burn_rate `slo_period` budget windows (ADR-031). NOT a shape
	// component: the period only scales the burn-rate multipliers derived at
	// resolve time (see sloBurnMultipliers) — it never enters the slug/hash/rule
	// text, so changing it never re-slugs.
	customAlertSloPeriodValid = stringSet(contract.SloBurnRate.SloPeriod.Enum)
	// slo_burn_rate per-period burn multipliers (ADR-031 §2, LOCKED): fast = "1h
	// burns 2% of the budget", slow = "6h burns 5%"; M = consumption × period ÷
	// detection window. The emitted user_threshold VALUE is
	// M × (1 − objective/100) — critical uses fast, warning uses slow. Pinned by
	// tests/dx/fixtures/slo_burn_multiplier_vectors.json.
	sloBurnMultipliers = func() map[string]struct{ fast, slow float64 } {
		m := map[string]struct{ fast, slow float64 }{}
		for p, byTier := range contract.SloBurnRate.Multipliers {
			m[p] = struct{ fast, slow float64 }{fast: byTier["critical"], slow: byTier["warning"]}
		}
		return m
	}()
)

// sloMinEventsDefault (ADR-031 OQ-A): the fast-window bad-event absolute floor
// when min_events is omitted. The default IS materialised into the slug
// (`minev10`) so a later default change can never silently re-slug existing
// shapes.
var sloMinEventsDefault = contract.SloBurnRate.MinEvents.Default

func customAlertSanitise(s string) string {
	return customAlertSanitiseRe.ReplaceAllString(s, "_")
}

// caHashField length-prefixes one field as "<byte-len>:<utf-8>". Concatenating
// length-prefixed fields is injective (NIST SP 800-185 TupleHash style): it closes the
// delimiter-aliasing collision class where a selector key/value straddles the `_`
// separator (#1008 / F3). Go string len is the UTF-8 byte length.
func caHashField(s string) string {
	return strconv.Itoa(len(s)) + ":" + s
}

// shapeHashSuffix is the #1008/F3 disambiguation suffix for a selector-bearing recipe_id:
// the first 16 hex (64-bit) of SHA-256 over a length-prefixed canonical of the STRUCTURED
// shape identity. Fields are emitted in a FIXED order with an explicit "" for a field not
// applicable to the recipe. Golden-vector pinned (tests/dx/fixtures/recipe_id_vectors.json).
func shapeHashSuffix(spec CustomAlertSpec, op string, items []selectorItem, gb []string) string {
	isForecast := spec.Recipe == "forecast"
	den := spec.DenominatorMetric
	if isForecast {
		den = spec.CapacityMetric
	}
	hashOp := op
	if spec.Recipe == "absence" {
		hashOp = ""
	}
	window := spec.Window
	// slo_burn_rate has NO window slot (fixed burn windows are recipe semantics;
	// a stray authored `window` is ignored — like forecast — so it must not fork
	// the hash). Existing recipes' field list/order is UNCHANGED; min_events needs
	// no hash field (charset-bounded + always visible in the slug as minev{N}).
	if isForecast || spec.Recipe == "slo_burn_rate" {
		window = ""
	}
	quantile := ""
	if spec.Recipe == "p99_latency" {
		quantile = string(spec.Quantile)
		if quantile == "" {
			quantile = contract.Quantile.Default
		}
	}
	horizon := ""
	if isForecast {
		horizon = spec.Horizon
	}
	forVal := spec.For
	if forVal == "" {
		forVal = contract.For.Default
	}
	var b strings.Builder
	for _, f := range []string{spec.Recipe, spec.Metric, hashOp, window, quantile, horizon, den, forVal} {
		b.WriteString(caHashField(f))
	}
	b.WriteString(caHashField("sel"))
	b.WriteString(caHashField(strconv.Itoa(len(items))))
	for _, it := range items {
		b.WriteString(caHashField(it.op))
		b.WriteString(caHashField(it.key))
		b.WriteString(caHashField(it.value))
	}
	b.WriteString(caHashField("gb"))
	b.WriteString(caHashField(strconv.Itoa(len(gb))))
	for _, g := range gb {
		b.WriteString(caHashField(g))
	}
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])[:16]
}

// customAlertGroupBy validates + canonicalises group_by into a sorted, deduped
// slice. Empty → nil, so a recipe without group_by keeps a byte-identical slug
// (existing golden vectors unaffected). Bounded to customAlertGroupByValid; sorted
// for slug determinism.
func customAlertGroupBy(spec CustomAlertSpec) ([]string, error) {
	if len(spec.GroupBy) == 0 {
		return nil, nil
	}
	seen := map[string]bool{}
	var out []string
	for _, label := range spec.GroupBy {
		if !customAlertGroupByValid[label] {
			return nil, fmt.Errorf("group_by label %q must be one of [%s] "+
				"(bounded whitelist, ADR-024 Addendum)", label, strings.Join(contract.GroupBy.Enum, ", "))
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
	if !customAlertMetricRe.MatchString(metric) {
		return fmt.Errorf("%s %q is not a bare metric name (%s); "+
			"label filtering must use selectors/selectors_re, never inline PromQL", field, metric, contract.Patterns.Metric)
	}
	return nil
}

// rejectTemplateMetachars rejects a selector VALUE that could become Go-template
// code in the annotation sink (see customAlertTemplateMetachars;
// assertTemplateSafe is the emit-time backstop).
func rejectTemplateMetachars(value, key string) error {
	for _, mc := range customAlertTemplateMetachars {
		if strings.Contains(value, mc) {
			return fmt.Errorf("selector value for %q contains a Go-template metacharacter %q: "+
				"it would reach the alert-annotation template context where Prometheus evaluates "+
				"{{ … }} at fire time (cross-tenant PromQL injection); selector values may not "+
				"contain {{, }}, or backticks", key, mc)
		}
	}
	return nil
}

// validateQuantile rejects a p99_latency quantile that is not a bare number in
// (0,1) (interpolated raw into histogram_quantile).
func validateQuantile(q string) error {
	// Charset gate FIRST — reject Go hex-floats/underscores that ParseFloat
	// accepts but PromQL and the schema's decimal pattern do not.
	qMin, qMax := contract.Quantile.ExclusiveMinimum, contract.Quantile.ExclusiveMaximum
	if !customAlertQuantileRe.MatchString(q) {
		return fmt.Errorf("quantile %q must be a decimal number in the open interval (%v,%v)", q, qMin, qMax)
	}
	f, err := strconv.ParseFloat(q, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f <= qMin || f >= qMax {
		return fmt.Errorf("quantile %q must be a number in the open interval (%v,%v)", q, qMin, qMax)
	}
	return nil
}

// validateSloObjective rejects a slo_burn_rate `objective` that is not a QUOTED
// YAML STRING carrying a decimal number in the OPEN interval (0,100) or the
// exact tri-state opt-out "disable" (=100 → error budget 0 → threshold 0 →
// always fires; =0 → never fires). STRING-ONLY by tag: a bare !!float/!!int/
// !!bool/!!null has already been resolved by the YAML reader — its text is
// dialect-dependent (the #1017 quantile class), so it is rejected outright
// rather than re-canonicalised. Then reuses the quantile decimal-only charset
// so a hex-float / underscore spelling is refused (the poison-commit class):
// string tag → charset regex → float → range.
func validateSloObjective(o taggedScalar) error {
	obj := contract.SloBurnRate.Objective
	lo, hi, disable := obj.ExclusiveMinimum, obj.ExclusiveMaximum, obj.Disable
	if o.tag != "" && o.tag != "!!str" {
		return fmt.Errorf("objective must be a quoted YAML string (e.g. \"99.9\"), got a %s "+
			"scalar — a bare value is YAML-dialect-ambiguous (#1017 quantile class)", o.tag)
	}
	if o.value == "" {
		return fmt.Errorf("slo_burn_rate recipe requires objective (SLO target percentage "+
			"in the open interval (%v,%v), e.g. \"99.9\"; %q opts out tri-state)", lo, hi, disable)
	}
	if o.value == disable {
		return nil
	}
	if !customAlertQuantileRe.MatchString(o.value) {
		return fmt.Errorf("objective %q must be a decimal number in the open interval (%v,%v) "+
			"or %q (decimal charset only)", o.value, lo, hi, disable)
	}
	f, err := strconv.ParseFloat(o.value, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f <= lo || f >= hi {
		return fmt.Errorf("objective %q must be in the OPEN interval (%v,%v): %v makes the "+
			"error budget 0 (threshold 0 → always fires), %v makes it never fire", o.value, lo, hi, hi, lo)
	}
	return nil
}

// sloPeriod validates + normalizes the slo_burn_rate `slo_period` (budget
// window). Empty → the contract's default.
func sloPeriod(spec CustomAlertSpec) (string, error) {
	p := spec.SloPeriod
	if p == "" {
		p = contract.SloBurnRate.SloPeriod.Default
	}
	if !customAlertSloPeriodValid[p] {
		return "", fmt.Errorf("slo_period %q must be one of %s", p, oneOf(contract.SloBurnRate.SloPeriod.Enum))
	}
	return p, nil
}

// sloMinEventsMin / sloMinEventsMax bound min_events: below the minimum the
// low-traffic guard is off (the opt-out is objective:"disable"); an absurd
// floor (e.g. 10^15) silently disables the alert while looking configured, and
// the literal enters the rule text.
var (
	sloMinEventsMin = int64(contract.SloBurnRate.MinEvents.Minimum)
	sloMinEventsMax = int64(contract.SloBurnRate.MinEvents.Maximum)
)

// sloMinEventsRe pins min_events' RAW text to plain decimal digits. This keeps
// `010` legal (base-0 ParseInt reads it as octal 8, matching PyYAML 1.1's
// resolution of the same text) while killing the `0o10`/`0x10`/`1_000` forms
// whose cross-dialect resolution diverges.
var sloMinEventsRe = regexp.MustCompile(`^[0-9]+$`)

// sloMinEvents validates + normalizes the slo_burn_rate `min_events` (the
// fast-window bad-event absolute floor, low-traffic false-positive guard).
// nil (missing) → default 10. Deferred strict-integer contract (see strictInt):
// tag must be !!int (bool/float/string rejected), raw must be digit-only, and the
// value must be in [1, sloMinEventsMax] (< 1 would disable the guard — the
// opt-out is objective:"disable").
func sloMinEvents(spec CustomAlertSpec) (int, error) {
	if spec.MinEvents == nil {
		return sloMinEventsDefault, nil
	}
	if spec.MinEvents.tag != "!!int" {
		return 0, fmt.Errorf("min_events %q must be a bare YAML integer (not %s) — "+
			"it enters the recipe_id slug as minev{N}", spec.MinEvents.raw, spec.MinEvents.tag)
	}
	if !sloMinEventsRe.MatchString(spec.MinEvents.raw) {
		return 0, fmt.Errorf("min_events %q must be a plain decimal integer "+
			"(no sign/0x/0o/0b/underscore forms — their YAML-dialect resolution diverges)",
			spec.MinEvents.raw)
	}
	n64, err := strconv.ParseInt(spec.MinEvents.raw, 0, 64)
	if err != nil {
		return 0, fmt.Errorf("min_events %q is not a parseable integer: %w", spec.MinEvents.raw, err)
	}
	// Range-check in the int64 domain BEFORE narrowing to int: on a 32-bit
	// platform int(n64) can wrap (e.g. 2^32+5 → 5) and sneak past the bounds
	// (CodeQL go/incorrect-integer-conversion).
	if n64 < sloMinEventsMin {
		return 0, fmt.Errorf("min_events %d must be >= %d (0 would disable the low-traffic "+
			"guard entirely; use objective:\"disable\" to opt out instead)", n64, sloMinEventsMin)
	}
	if n64 > sloMinEventsMax {
		return 0, fmt.Errorf("min_events %d exceeds the maximum %d (schema maximum; an "+
			"absurd floor silently disables the alert while looking configured)", n64, sloMinEventsMax)
	}
	return int(n64), nil
}

type selectorItem struct {
	op, key, value string // op is "=" (selectors) or "=~" (selectors_re)
}

// selectorItems returns the validated (op,key,value) triples sorted by (key, op)
// — deterministic, independent of map iteration order (the slug and the
// selector the compiled rules match on).
func selectorItems(spec CustomAlertSpec) ([]selectorItem, error) {
	var items []selectorItem
	for k, v := range spec.Selectors {
		items = append(items, selectorItem{"=", k, v})
	}
	for k, v := range spec.SelectorsRe {
		items = append(items, selectorItem{"=~", k, v})
	}
	for _, it := range items {
		if !customAlertLabelRe.MatchString(it.key) {
			return nil, fmt.Errorf("selector label %q is not a valid label name", it.key)
		}
		if customAlertReservedLabels[it.key] {
			return nil, fmt.Errorf("selector label %q is reserved and may not be pinned", it.key)
		}
		if err := rejectTemplateMetachars(it.value, it.key); err != nil {
			return nil, err
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

// customAlertSpecFields maps each yaml key of CustomAlertSpec to its field
// index, so the contract's field names address the struct directly (a
// contract field with no struct field is caught by a test).
var customAlertSpecFields = func() map[string]int {
	m := map[string]int{}
	t := reflect.TypeOf(CustomAlertSpec{})
	for i := 0; i < t.NumField(); i++ {
		m[strings.Split(t.Field(i).Tag.Get("yaml"), ",")[0]] = i
	}
	return m
}()

// specFieldSet reports whether the declaration carries field (an authored
// empty string counts as absent, as the hand-written checks treated it).
func specFieldSet(spec CustomAlertSpec, field string) bool {
	i, ok := customAlertSpecFields[field]
	if !ok {
		return false
	}
	switch v := reflect.ValueOf(spec).Field(i).Interface().(type) {
	case string:
		return v != ""
	case flexStr:
		return v != ""
	case taggedScalar:
		return v.value != ""
	case *strictInt:
		return v != nil
	case map[string]string:
		return len(v) > 0
	case []string:
		return len(v) > 0
	}
	return false
}

// checkRecipeFields applies the contract's per-recipe declaration rules:
// base_required + the recipe's required fields present, its forbidden fields
// absent, its fixed op (if any) the one used, and group_by only where the
// recipe takes it. withDataPlane=false skips the data-plane fields (name,
// threshold) the shape slug does not need.
func checkRecipeFields(spec CustomAlertSpec, op string, withDataPlane bool) error {
	r, ok := contractRecipe(spec.Recipe)
	if !ok {
		return fmt.Errorf("unknown recipe %q", spec.Recipe)
	}
	dataPlane := map[string]bool{}
	for _, f := range contract.Fields {
		dataPlane[f.Name] = f.DataPlane
	}
	for _, f := range append(append([]string(nil), contract.BaseRequired...), r.Required...) {
		if !withDataPlane && dataPlane[f] {
			continue
		}
		if !specFieldSet(spec, f) {
			return fmt.Errorf("missing required field %s (the %s recipe requires %s)",
				f, r.Name, strings.Join(append(append([]string(nil), contract.BaseRequired...), r.Required...), "/"))
		}
	}
	forbidden := make([]string, 0, len(r.Forbidden))
	for f := range r.Forbidden {
		forbidden = append(forbidden, f)
	}
	sort.Strings(forbidden)
	for _, f := range forbidden {
		if specFieldSet(spec, f) {
			return fmt.Errorf("%s does not take %s: %s", r.Name, f, r.Forbidden[f])
		}
	}
	if r.FixedOp != "" && op != r.FixedOp {
		return fmt.Errorf("op %q is not settable for %s (the recipe fixes %q); omit op", op, r.Name, r.FixedOp)
	}
	if len(spec.GroupBy) > 0 && !r.GroupBy {
		return fmt.Errorf("group_by (per-dimension eval) is not supported for the %s recipe — "+
			"it applies only to value-crossing recipes (ADR-024 Addendum)", r.Name)
	}
	return nil
}

// customAlertShape is one declaration's rule identity, validated and with the
// contract's defaults applied: everything the recipe_id slug encodes and the
// rule compiler (custom_alert_compile.go) emits from. Two declarations with the
// same recipe_id compile to one vectorised rule, so a recipe_id shared by two
// unequal shapes is a hash collision (CompileCustomAlertPack refuses it).
type customAlertShape struct {
	Recipe, Metric string
	// Op: the authored op or the contract default; empty for absence.
	Op string
	// Window: rate/ratio/p99_latency/threshold/absence only.
	Window string
	// Quantile: p99_latency only (contract default applied).
	Quantile string
	// Denominator: ratio and slo_burn_rate.
	Denominator string
	// Horizon and Capacity: forecast (Capacity set → ratio mode).
	Horizon, Capacity string
	For               string
	// MinEvents: slo_burn_rate only.
	MinEvents int
	Items     []selectorItem
	GroupBy   []string
	// hash: the #1008/F3 disambiguation suffix ("" without selectors).
	hash string
}

// recipeShape validates spec's identity fields and returns its shape. The
// checks run in the order RecipeID always ran them, so its errors are
// unchanged.
func recipeShape(spec CustomAlertSpec) (customAlertShape, error) {
	var sh customAlertShape
	if !customAlertRecipes[spec.Recipe] {
		return sh, fmt.Errorf("unknown recipe %q", spec.Recipe)
	}
	op := spec.Op
	if op == "" {
		op = contract.Op.Default
	}
	// The shape slug needs the identity fields only: the data-plane ones
	// (name, threshold) are checked by the resolve paths.
	if err := checkRecipeFields(spec, op, false); err != nil {
		return sh, err
	}
	if err := validateCustomAlertMetric(spec.Metric, "metric"); err != nil {
		return sh, err
	}
	items, err := selectorItems(spec)
	if err != nil {
		return sh, err
	}
	sh = customAlertShape{Recipe: spec.Recipe, Metric: spec.Metric, Op: op, Items: items}
	// `==` is threshold-recipe-only (#810): exact match suits integer status/
	// error codes on a RAW gauge; other recipes emit computed floats where
	// equality is fragile. This gate runs BEFORE the absence short-circuit so it
	// also rejects absence+"==" (op is meaningless for a presence check) — keeping
	// the imperative gate in lockstep with the JSON-schema if/then editor-guard,
	// so an API/GitOps-accepted spec can't later fail to render in the Portal.
	if op == "==" && !customAlertEqRecipes[spec.Recipe] {
		return sh, fmt.Errorf("op \"==\" is only allowed for the %s recipe "+
			"(raw-gauge status-code match); %s does not support it", oneOf(contract.Op.EqRecipes), spec.Recipe)
	}
	if spec.Recipe == "absence" {
		// A presence check has no comparison: the op is not in its slug nor
		// in any rule it emits, so it is not in its shape either — or two
		// entries on one recipe_id, one with a stray `op:`, would be refused
		// as a collision (CompileCustomAlertPack compares shapes whole).
		sh.Op = ""
	} else if _, ok := customAlertOpSlug[op]; !ok {
		return sh, fmt.Errorf("unknown op %q", op)
	}
	switch spec.Recipe {
	case "forecast":
		// forecast: lookback derives from `horizon` (the compiler's
		// forecastLookbackSeconds), so the tenant supplies `horizon` (enum),
		// NOT a window. capacity_metric present → ratio mode.
		h := spec.Horizon // required by the contract (checkRecipeFields)
		if !customAlertHorizonValid[h] {
			return sh, fmt.Errorf("horizon %q must be one of %s", h, oneOf(contract.Horizon.Enum))
		}
		sh.Horizon = h
		if spec.CapacityMetric != "" {
			if err := validateCustomAlertMetric(spec.CapacityMetric, "capacity_metric"); err != nil {
				return sh, err
			}
			sh.Capacity = spec.CapacityMetric
		}
	case "slo_burn_rate":
		// ADR-031: the burn windows (1h/5m fast, 6h/30m slow) are FIXED recipe
		// semantics — no window (a stray authored `window` is ignored, like
		// forecast). `objective` replaces `threshold`; objective/slo_period are
		// validated here (ADR-029 dual-side duty) but never enter the slug.
		if err := validateSloObjective(spec.Objective); err != nil {
			return sh, err
		}
		if _, err := sloPeriod(spec); err != nil {
			return sh, err
		}
		if err := validateCustomAlertMetric(spec.DenominatorMetric, "denominator_metric"); err != nil {
			return sh, err
		}
		sh.Denominator = spec.DenominatorMetric
		// min_events IS a shape component (the compiler writes the literal into
		// the rule text): default materialised into the slug as minev{N}.
		if sh.MinEvents, err = sloMinEvents(spec); err != nil {
			return sh, err
		}
	default:
		sh.Window = spec.Window
		if spec.Recipe == "p99_latency" {
			q := string(spec.Quantile)
			if q == "" {
				q = contract.Quantile.Default
			}
			if err := validateQuantile(q); err != nil {
				return sh, err
			}
			sh.Quantile = q
		}
		if spec.Recipe == "ratio" {
			if err := validateCustomAlertMetric(spec.DenominatorMetric, "denominator_metric"); err != nil {
				return sh, err
			}
			sh.Denominator = spec.DenominatorMetric
		}
	}
	// `for` is part of the rule identity (a Prometheus `for:` is a static rule
	// attribute, unlike data-plane `mode`): two tenants differing only in `for`
	// are genuinely different rules (TRK-326 / #751).
	sh.For = spec.For
	if sh.For == "" {
		sh.For = contract.For.Default
	}
	// group_by dimensions (ADR-024 §Addendum): per-dimension eval (e.g. per PVC).
	//   FORESIGHT: the "==" rejection is safe ONLY because the whitelist is PVC-only
	//   (error codes aren't per-PVC). If a topology dim (e.g. pod) is whitelisted, a
	//   tenant may legitimately want group_by:[pod] + op:"==" — then relax this AND
	//   thread group_by into the eq core (custom_alert_compile.go eqCoreRecord).
	if sh.GroupBy, err = customAlertGroupBy(spec); err != nil {
		return sh, err
	}
	// Which recipes take group_by at all is the contract's (checkRecipeFields).
	if len(sh.GroupBy) > 0 && op == "==" {
		return sh, fmt.Errorf("group_by (per-dimension eval) is not supported for op \"==\" — " +
			"it applies only to value-crossing recipes (ADR-024 Addendum)")
	}
	// #1008 / F3: recipe_id must be INJECTIVE over the shape identity, but the
	// readable slug is not (customAlertSanitise is lossy, the `__` join is
	// ambiguous). A selector is the only tenant-controlled free-form slug field,
	// so a selector-bearing recipe carries a suffix over the STRUCTURED identity.
	if len(spec.Selectors) > 0 || len(spec.SelectorsRe) > 0 {
		sh.hash = shapeHashSuffix(spec, op, items, sh.GroupBy)
	}
	return sh, nil
}

// ID is the deterministic shape slug (golden vector test).
//
//	SLUG-ORDER CONTRACT: a new slug field goes in a fixed position, and only
//	when present (like gb_) — an always-appended field (like `for`) re-slugs
//	every existing rule, a deliberate breaking migration.
func (sh customAlertShape) ID() string {
	parts := []string{sh.Recipe, sh.Metric}
	for _, it := range sh.Items {
		prefix := "s"
		if it.op == "=~" {
			prefix = "sre"
		}
		parts = append(parts, prefix+"_"+it.key+"_"+it.value)
	}
	if sh.Recipe == "absence" {
		parts = append(parts, "absent")
	} else {
		parts = append(parts, customAlertOpSlug[sh.Op])
	}
	switch sh.Recipe {
	case "forecast":
		parts = append(parts, "h"+sh.Horizon)
		if sh.Capacity != "" {
			parts = append(parts, "den_"+sh.Capacity)
		}
	case "slo_burn_rate":
		parts = append(parts, "den_"+sh.Denominator, "minev"+strconv.Itoa(sh.MinEvents))
	default:
		parts = append(parts, "w"+sh.Window)
		if sh.Recipe == "p99_latency" {
			parts = append(parts, "q"+sh.Quantile)
		}
		if sh.Recipe == "ratio" {
			parts = append(parts, "den_"+sh.Denominator)
		}
	}
	parts = append(parts, "for"+sh.For)
	for _, g := range sh.GroupBy {
		parts = append(parts, "gb_"+g)
	}
	slug := customAlertSanitise(strings.Join(parts, "__"))
	if sh.hash != "" {
		slug += "__x" + sh.hash
	}
	return slug
}

// RecipeID computes the deterministic shape slug (recipeShape + ID).
func RecipeID(spec CustomAlertSpec) (string, error) {
	sh, err := recipeShape(spec)
	if err != nil {
		return "", err
	}
	return sh.ID(), nil
}

// parseCustomAlertThreshold splits "value[:severity]" → (value, severity).
// severity defaults to "warning". The grammar is the threshold rows' one
// (splitThresholdSeverity, #3071).
func parseCustomAlertThreshold(threshold string) (value, severity string, err error) {
	return splitThresholdSeverity(threshold)
}

// ResolvedSloObjective carries one slo_burn_rate declaration's raw objective
// percentage out of resolve time so the collector can publish the
// `user_slo_objective{tenant, recipe_id}` gauge (ADR-031 — the data-plane echo
// of the tenant's SLO target; the derived burn thresholds ride user_threshold).
// objective:"disable" produces NO entry (data-plane opt-out, same as its
// user_threshold rows).
type ResolvedSloObjective struct {
	Tenant    string
	RecipeID  string
	Objective float64
}

// resolveOneCustomAlert validates one spec and builds its user_threshold-bearing
// ResolvedThreshold rows (component="custom"). Every recipe yields exactly one
// row except slo_burn_rate, which fans out to two (fast→critical, slow→warning;
// ADR-031). No version label is set: the rule's normalize layer fills empty
// version → "default" (no-version main path).
func resolveOneCustomAlert(tenant string, spec CustomAlertSpec) ([]ResolvedThreshold, error) {
	// slo_burn_rate (ADR-031) branches early: objective replaces threshold, the
	// recipe has no window, and severity fans out per burn tier — none of the
	// value:severity machinery below applies.
	if spec.Recipe == "slo_burn_rate" {
		return resolveSloBurnRate(tenant, spec)
	}
	if !customAlertRecipes[spec.Recipe] {
		return nil, fmt.Errorf("unknown recipe %q", spec.Recipe)
	}
	op := spec.Op
	if op == "" {
		op = contract.Op.Default
	}
	if err := checkRecipeFields(spec, op, true); err != nil {
		return nil, err
	}
	// A recipe whose params carry `window` interpolates it raw into
	// rate(…[<window>]); one without (forecast's horizon) ignores a stray one.
	if r, _ := contractRecipe(spec.Recipe); contains(r.Params, "window") {
		if !customAlertWindowRe.MatchString(spec.Window) {
			return nil, fmt.Errorf("window %q is not a valid Go duration "+
				"(%s); it is interpolated raw into rate(…[<window>]) "+
				"— an invalid value is a PromQL injection", spec.Window, contract.Patterns.Window)
		}
	}
	if !customAlertAlertNameRe.MatchString(spec.Name) {
		return nil, fmt.Errorf("name %q is not a valid alert name (%s)", spec.Name, contract.Patterns.Name)
	}
	rid, err := RecipeID(spec)
	if err != nil {
		return nil, err
	}
	valueStr, severity, err := parseCustomAlertThreshold(spec.Threshold)
	if err != nil {
		return nil, &customAlertThresholdRefusal{refused: RefusedUnparsed, err: err}
	}
	// Three-state: `threshold: "disable"` (schema-valid thresholdScalar) turns the
	// custom alert OFF by emitting NO user_threshold series — the vectorized rule
	// then simply doesn't fire for this tenant (others sharing the shape are
	// unaffected). This is a clean opt-out, NOT a parse error, so it must not
	// raise da_custom_alert_parse_errors. (Same "absent = disabled" semantics as
	// regular numeric thresholds.)
	if isDisabled(strings.ToLower(strings.TrimSpace(valueStr))) {
		return nil, errCustomAlertDisabled
	}
	// The one threshold-number reader the row resolvers use (#3140): a
	// non-finite number ("NaN"/"Inf", which ParseFloat accepts) would emit a
	// nonsensical comparison series (e.g. `metric > NaN` never fires).
	value, ok := parseThresholdNumber(valueStr)
	if !ok {
		if _, err := strconv.ParseFloat(valueStr, 64); err != nil {
			return nil, &customAlertThresholdRefusal{refused: RefusedUnparsed,
				err: fmt.Errorf("threshold value %q is not numeric: %w", valueStr, err)}
		}
		return nil, &customAlertThresholdRefusal{refused: RefusedNonFinite,
			err: fmt.Errorf("threshold value %q must be finite", valueStr)}
	}
	// forecast ratio mode: the threshold is a headroom-fraction floor
	// (avail/capacity), so it must be in (0, forecastCurrentBand) — a floor >= 1 would
	// fire permanently; a floor >= the current-state band is silently neutered by the band gate (forecastRecords).
	if lo := contract.Forecast.RatioFloorExclusiveMinimum; spec.Recipe == "forecast" && spec.CapacityMetric != "" && (value <= lo || value >= forecastCurrentBand) {
		return nil, fmt.Errorf("forecast ratio-mode threshold %v must be in (%v,%v): a floor >= the current-state band is neutered by the band gate", value, lo, forecastCurrentBand)
	}
	mode := spec.Mode
	if mode == "" {
		mode = contract.Mode.Default
	}
	// reject unsupported mode (typo): only page|silent ride the data plane; an
	// unknown value would surface a bogus mode label that S8 routing can't handle.
	if !customAlertModeValid[mode] {
		return nil, fmt.Errorf("mode %q must be one of %s", mode, oneOf(contract.Mode.Enum))
	}
	// `for` must be one of the enum-bounded values (TRK-326): it enters the
	// recipe_id slug, so an out-of-enum value would silently spawn a distinct
	// shape/rule and bloat cardinality. Default 1m (matches schema default).
	forVal := spec.For
	if forVal == "" {
		forVal = contract.For.Default
	}
	if !customAlertForValid[forVal] {
		return nil, fmt.Errorf("for %q is not allowed (one of %s)", forVal, oneOf(contract.For.Enum))
	}
	return []ResolvedThreshold{{
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
	}}, nil
}

// resolveSloBurnRate validates one slo_burn_rate spec (ADR-031) and fans it out
// to its TWO user_threshold rows sharing one recipe_id:
//
//	critical ← fast burn (1h & 5m):  value = fastM × (1 − objective/100)
//	warning  ← slow burn (6h & 30m): value = slowM × (1 − objective/100)
//
// where the multipliers come from sloBurnMultipliers[slo_period] (30d→14.4/6,
// 28d→13.44/5.6). Severity is decided by the RECIPE (a deliberate departure from
// the value:severity threshold tail — hence no parseCustomAlertThreshold here).
// objective:"disable" is the tri-state opt-out: valid, but emits NO series
// (errCustomAlertDisabled — same semantics as threshold:"disable").
func resolveSloBurnRate(tenant string, spec CustomAlertSpec) ([]ResolvedThreshold, error) {
	op := spec.Op
	if op == "" {
		op = contract.Op.Default
	}
	if err := checkRecipeFields(spec, op, true); err != nil {
		return nil, err
	}
	if !customAlertAlertNameRe.MatchString(spec.Name) {
		return nil, fmt.Errorf("name %q is not a valid alert name (%s)", spec.Name, contract.Patterns.Name)
	}
	// RecipeID carries the rest of the slo validation duty (objective charset +
	// open interval, slo_period enum, denominator_metric, min_events, fixed op,
	// threshold-presence rejection, group_by rejection, selector safety) — same
	// "preflight is at least as strict as the slug" property as other recipes.
	rid, err := RecipeID(spec)
	if err != nil {
		return nil, err
	}
	obj := spec.Objective.value
	if obj == contract.SloBurnRate.Objective.Disable {
		return nil, errCustomAlertDisabled
	}
	objective, err := strconv.ParseFloat(obj, 64)
	if err != nil {
		// unreachable after RecipeID's validateSloObjective, but fail loud.
		return nil, fmt.Errorf("objective %q is not numeric: %w", obj, err)
	}
	mode := spec.Mode
	if mode == "" {
		mode = contract.Mode.Default
	}
	if !customAlertModeValid[mode] {
		return nil, fmt.Errorf("mode %q must be one of %s", mode, oneOf(contract.Mode.Enum))
	}
	// `for` must be one of the enum-bounded values (TRK-326) on the slo path
	// too: RecipeID slugs `for{val}` without an enum check, so skipping this
	// here would let an out-of-enum `for` mint a bogus shape (a `for:` the
	// compiled alert carries verbatim). Same check + message as the non-slo
	// path.
	forVal := spec.For
	if forVal == "" {
		forVal = contract.For.Default
	}
	if !customAlertForValid[forVal] {
		return nil, fmt.Errorf("for %q is not allowed (one of %s)", forVal, oneOf(contract.For.Enum))
	}
	period, err := sloPeriod(spec)
	if err != nil {
		return nil, err
	}
	mult, ok := sloBurnMultipliers[period]
	if !ok {
		// unreachable: sloPeriod only returns keys of sloBurnMultipliers.
		return nil, fmt.Errorf("no burn multipliers for slo_period %q", period)
	}
	// error budget fraction; expression order (M × (1 − obj/100)) MUST match the
	// re-computation in tests/dx/test_compile_custom_alerts.py, which pins the
	// multiplier fixture to the locked multipliers bit-identically.
	budget := 1 - objective/100
	row := func(value float64, severity string) ResolvedThreshold {
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
		}
	}
	return []ResolvedThreshold{
		row(mult.fast*budget, "critical"),
		row(mult.slow*budget, "warning"),
	}, nil
}

// resolveTenantCustomAlerts re-parses a tenant's _custom_alerts YAML string and
// returns the resolved user_threshold rows, the slo_burn_rate objectives for the
// user_slo_objective gauge (ADR-031; nil when the tenant declares none), plus the
// count of malformed entries (for the da_custom_alert_parse_errors gauge —
// fail-loud, never silent-skip).
func resolveTenantCustomAlerts(tenant string, overrides map[string]ScheduledValue) ([]ResolvedThreshold, []ResolvedSloObjective, int) {
	return resolveTenantCustomAlertsLogf(tenant, overrides, log.Printf)
}

// resolveTenantCustomAlertsLogf is resolveTenantCustomAlerts with the
// rejected-alert ERROR sink as a parameter (#2397; nil = silent). The returned
// error count — the da_custom_alert_parse_errors gauge — does not depend on it.
func resolveTenantCustomAlertsLogf(tenant string, overrides map[string]ScheduledValue, logf func(format string, args ...any)) ([]ResolvedThreshold, []ResolvedSloObjective, int) {
	out, objectives, errCount, _ := resolveCustomAlertSpecs(tenant, overrides, logf)
	return out, objectives, errCount
}

// resolveCustomAlertSpecs is resolveTenantCustomAlertsLogf plus the entries
// it dropped as duplicates of an earlier one's series (#2031): each counts as
// a malformed entry and is logged as one.
func resolveCustomAlertSpecs(tenant string, overrides map[string]ScheduledValue, logf func(format string, args ...any)) (
	[]ResolvedThreshold, []ResolvedSloObjective, int, []CustomAlertDuplicate,
) {
	walked, blockErr := walkCustomAlertSpecs(tenant, overrides, logf)
	if blockErr != nil {
		return nil, nil, 1, nil
	}
	var out []ResolvedThreshold
	var objectives []ResolvedSloObjective
	var dups []CustomAlertDuplicate
	errCount := 0
	for _, w := range walked {
		switch w.entry.Status {
		case CustomAlertServed:
			out = append(out, w.rows...)
			if w.objective != nil {
				objectives = append(objectives, *w.objective)
			}
		case CustomAlertRejected:
			if !w.switchedOff {
				errCount++
			}
		case CustomAlertDuplicated:
			dups = append(dups, w.dup)
			errCount++
		}
	}
	return out, objectives, errCount, dups
}

// Statuses of CustomAlertEntry — a closed set.
const (
	// CustomAlertServed: the exporter serves the entry's rows.
	CustomAlertServed = "served"
	// CustomAlertDisabled: switched off (`threshold: disable`, `objective:
	// disable`, …) — no row, by design; given a value it would be served.
	CustomAlertDisabled = "disabled"
	// CustomAlertRejected: the exporter rejects the entry (Reason says why);
	// a switched-off entry that would be rejected given a value is this too.
	CustomAlertRejected = "rejected"
	// CustomAlertDuplicated: an earlier entry of the list already serves its
	// shape and severity (CustomAlertDuplicate); the exporter drops it.
	CustomAlertDuplicated = "duplicate"
)

// CustomAlertEntry is one element of a tenant's built `_custom_alerts` list
// as the exporter's resolver reads it (#3042): the spec as decoded, what the
// exporter does with it, and — for a served or disabled entry — its
// recipe_id and the severities of the rows it serves (would serve, when
// disabled). The custom-alert compiler builds its rules from these.
type CustomAlertEntry struct {
	Index      int
	Spec       CustomAlertSpec
	Status     string
	Reason     string
	RecipeID   string
	Severities []string
	// Refused (#3209), for a rejected entry only: the entry is rejected
	// because the exporter refuses its `threshold:` as a threshold value —
	// RefusedNonFinite or RefusedUnparsed, as RefusedValueAt classes a
	// threshold key's value. Empty for any other rejection, and for an
	// entry switched off (`threshold: disable`).
	Refused string
}

// CustomAlertEntries is CustomAlertEntry for every element of the tenant's
// `_custom_alerts` (overrides: its built map), in declaration order — read by
// the same walk resolveCustomAlertSpecs serves rows from, so the two cannot
// part. A block that does not decode has no entry: blockErr says why (""
// when the block decodes or is absent).
func CustomAlertEntries(tenant string, overrides map[string]ScheduledValue) (entries []CustomAlertEntry, blockErr string) {
	walked, err := walkCustomAlertSpecs(tenant, overrides, nil)
	if err != nil {
		return nil, err.Error()
	}
	entries = make([]CustomAlertEntry, 0, len(walked))
	for _, w := range walked {
		entries = append(entries, w.entry)
	}
	return entries, ""
}

// walkedCustomAlert is one entry of walkCustomAlertSpecs with what serving it
// takes: its rows and user_slo_objective (served), or the earlier entry it
// repeats (duplicate).
type walkedCustomAlert struct {
	entry     CustomAlertEntry
	rows      []ResolvedThreshold
	objective *ResolvedSloObjective
	dup       CustomAlertDuplicate
	// switchedOff: rejected only once switched back on. The exporter serves
	// it no row either way and has always taken it as a valid opt-out — it
	// is not counted on da_custom_alert_parse_errors nor logged.
	switchedOff bool
}

// walkCustomAlertSpecs decodes the tenant's `_custom_alerts` and judges each
// entry in declaration order. A disabled entry is resolved once more with a
// value in place of the switched-off one (disabledProbe): it is disabled when
// that is served, rejected otherwise, so a disabled entry is exactly one that
// switching back on serves. The error is the block's: it does not decode.
func walkCustomAlertSpecs(tenant string, overrides map[string]ScheduledValue, logf func(format string, args ...any)) ([]walkedCustomAlert, error) {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	sv, ok := overrides["_custom_alerts"]
	if !ok || strings.TrimSpace(sv.Default) == "" {
		return nil, nil
	}
	var specs []CustomAlertSpec
	if err := yaml.Unmarshal([]byte(sv.Default), &specs); err != nil {
		// whole block unparseable → count as 1 error; the tenant gets NO custom
		// alerts but the rest of its config is unaffected.
		err = fmt.Errorf("cannot parse _custom_alerts: %w", err)
		logCustomAlertError(logf, tenant, "<block>", err)
		return nil, err
	}
	out := make([]walkedCustomAlert, 0, len(specs))
	var owner map[customSeriesID]int // series → index of the entry that serves it; one entry cannot repeat another
	if len(specs) > 1 {
		owner = make(map[customSeriesID]int, len(specs)+1)
	}
	for i, spec := range specs {
		w := walkedCustomAlert{entry: CustomAlertEntry{Index: i, Spec: spec}}
		rts, err := resolveOneCustomAlert(tenant, spec)
		if errors.Is(err, errCustomAlertDisabled) {
			// three-state opt-out: no series, NOT an error — when switching
			// it back on would be served.
			probe, perr := disabledProbe(tenant, spec)
			if perr != nil {
				w.entry.Status, w.switchedOff = CustomAlertRejected, true
				w.entry.Reason = "switched off; switched back on it would be rejected: " + perr.Error()
			} else {
				w.entry.Status = CustomAlertDisabled
				w.entry.RecipeID, w.entry.Severities = probe[0].CustomLabels["recipe_id"], rowSeverities(probe)
			}
			out = append(out, w)
			continue
		}
		if err != nil {
			logCustomAlertError(logf, tenant, spec.Name, err)
			w.entry.Status, w.entry.Reason = CustomAlertRejected, err.Error()
			var refusal *customAlertThresholdRefusal
			if errors.As(err, &refusal) {
				w.entry.Refused = refusal.refused
			}
			out = append(out, w)
			continue
		}
		w.entry.RecipeID, w.entry.Severities = rts[0].CustomLabels["recipe_id"], rowSeverities(rts)
		// slo_burn_rate: echo the raw objective for the user_slo_objective gauge.
		// Reaching here means resolveSloBurnRate already ParseFloat'ed it (the
		// disable case exited via errCustomAlertDisabled above → no gauge).
		if spec.Recipe == "slo_burn_rate" {
			if v, perr := strconv.ParseFloat(spec.Objective.value, 64); perr == nil {
				w.objective = &ResolvedSloObjective{Tenant: tenant, RecipeID: w.entry.RecipeID, Objective: v}
			}
		}
		if owner != nil {
			if d, dup := customAlertSeriesCollision(rts, w.objective, owner); dup {
				d.Index, d.Name, d.OfName = i, spec.Name, specs[d.Of].Name
				reason := fmt.Sprintf("its series %s is of the shape and severity _custom_alerts[%d] (%q) already serves; only the first is served", d.Series, d.Of, d.OfName)
				logCustomAlertError(logf, tenant, spec.Name, errors.New(reason))
				w.entry.Status, w.entry.Reason, w.dup = CustomAlertDuplicated, reason, d
				w.entry.RecipeID, w.entry.Severities = "", nil
				w.objective = nil
				out = append(out, w)
				continue
			}
			for _, r := range rts {
				owner[customRowID(r)] = i
			}
			if w.objective != nil {
				owner[customSeriesID{objective: true, recipeID: w.objective.RecipeID}] = i
			}
		}
		w.entry.Status, w.rows = CustomAlertServed, rts
		out = append(out, w)
	}
	return out, nil
}

// disabledProbe resolves a switched-off spec with a value in its place — the
// threshold's value half (its severity kept) or the objective — so every
// other check runs. The value is one every recipe accepts (inside the
// forecast ratio band); it enters no recipe_id.
func disabledProbe(tenant string, spec CustomAlertSpec) ([]ResolvedThreshold, error) {
	probe := spec
	if spec.Recipe == "slo_burn_rate" {
		probe.Objective = taggedScalar{value: "99", tag: spec.Objective.tag}
	} else {
		_, severity, err := parseCustomAlertThreshold(spec.Threshold)
		if err != nil {
			return nil, err
		}
		probe.Threshold = strconv.FormatFloat(forecastCurrentBand/2, 'f', -1, 64) + ":" + severity
	}
	return resolveOneCustomAlert(tenant, probe)
}

// Fields is the spec as the resolver decoded it, keyed by its YAML names,
// absent fields left out: every value a string, except selectors /
// selectors_re (string → string), group_by (strings) and a min_events written
// as a YAML integer (int). It is what the resolver judged — not the text as
// written: a YAML `true` selector value is "true", a null is "" (#3042, the
// custom-alert compiler builds its rules from it).
func (s CustomAlertSpec) Fields() map[string]any {
	out := map[string]any{}
	str := func(k, v string) {
		if v != "" {
			out[k] = v
		}
	}
	str("recipe", s.Recipe)
	str("name", s.Name)
	str("metric", s.Metric)
	str("op", s.Op)
	str("window", s.Window)
	str("threshold", s.Threshold)
	str("quantile", string(s.Quantile))
	str("denominator_metric", s.DenominatorMetric)
	str("horizon", s.Horizon)
	str("capacity_metric", s.CapacityMetric)
	str("mode", s.Mode)
	str("for", s.For)
	str("slo_period", s.SloPeriod)
	if s.Selectors != nil {
		out["selectors"] = s.Selectors
	}
	if s.SelectorsRe != nil {
		out["selectors_re"] = s.SelectorsRe
	}
	if s.GroupBy != nil {
		out["group_by"] = s.GroupBy
	}
	if s.Objective.tag != "" {
		out["objective"] = s.Objective.value
	}
	if s.MinEvents != nil {
		if n, err := strconv.Atoi(s.MinEvents.raw); err == nil && s.MinEvents.tag == "!!int" {
			out["min_events"] = n
		} else {
			out["min_events"] = s.MinEvents.raw
		}
	}
	return out
}

// rowSeverities is the severity of each row, in row order.
func rowSeverities(rows []ResolvedThreshold) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.Severity
	}
	return out
}

// CustomAlertDuplicate is one `_custom_alerts` entry the exporter drops
// because an earlier entry of the same list already serves a row of its
// shape and severity, or its user_slo_objective (customSeriesID): the same
// series twice fails the whole Gather (HTTP 500 for every tenant, #2031);
// the same shape and severity under another name or mode fails the shape's
// shared rule for every tenant (#3112). The later entry is dropped, counted
// on da_custom_alert_parse_errors and logged at ERROR.
type CustomAlertDuplicate struct {
	Index  int    // the dropped entry's index in the list
	Name   string // its name
	Of     int    // the index of the entry whose series it repeats
	OfName string // that entry's name
	Series string // the repeated series: its metric name and the labels that collide
}

// CustomAlertDuplicates is the `_custom_alerts` entries of one tenant's
// built map (overrides) the exporter drops as duplicates of an earlier
// entry's series — the exporter's own reading, for da-guard's
// custom_alert_duplicate_series. nil when none.
func CustomAlertDuplicates(tenant string, overrides map[string]ScheduledValue) []CustomAlertDuplicate {
	_, _, _, dups := resolveCustomAlertSpecs(tenant, overrides, nil)
	return dups
}

// customSeriesID is what one tenant may serve once per resolved custom
// alert: a user_threshold row's shape and severity (its metric, severity
// and recipe_id — not its name or mode) or, with objective set, its
// user_slo_objective{recipe_id}.
//
// A row is keyed without name and mode because the compiled pack has ONE
// rule per (recipe_id, severity) for every tenant, joined
// `on(tenant, version) group_left(name, mode)` to
// `max by(tenant, version, severity, name, mode) (user_threshold{…})`: two
// rows of one tenant that differ only in name or mode leave two series on
// that side, and the evaluation fails ("many-to-many matching not allowed")
// for EVERY tenant of the shape, not just this one (#3112). Two rows with
// one label set — the same name too — are the narrower case client_golang
// refuses to gather (#2031). The preflight's other rule (one name per
// tenant) stays the preflight's: an entry only it refuses is still served
// (ValidateTenantCustomAlerts keeps it for tenant-api).
type customSeriesID struct {
	objective                  bool
	metric, severity, recipeID string
}

func customRowID(r ResolvedThreshold) customSeriesID {
	return customSeriesID{metric: r.Metric, severity: r.Severity, recipeID: r.CustomLabels["recipe_id"]}
}

// customAlertSeriesCollision reports whether a series of one resolved
// custom alert (rows, obj) is already in owner, and which entry serves it.
func customAlertSeriesCollision(rows []ResolvedThreshold, obj *ResolvedSloObjective, owner map[customSeriesID]int) (CustomAlertDuplicate, bool) {
	for _, r := range rows {
		if of, taken := owner[customRowID(r)]; taken {
			return CustomAlertDuplicate{Of: of, Series: fmt.Sprintf(
				"user_threshold{component=%q, metric=%q, severity=%q, recipe_id=%q}",
				r.Component, r.Metric, r.Severity, r.CustomLabels["recipe_id"])}, true
		}
	}
	if obj != nil {
		if of, taken := owner[customSeriesID{objective: true, recipeID: obj.RecipeID}]; taken {
			return CustomAlertDuplicate{Of: of, Series: fmt.Sprintf("user_slo_objective{recipe_id=%q}", obj.RecipeID)}, true
		}
	}
	return CustomAlertDuplicate{}, false
}

// CustomAlertRowCount is how many custom-alert rows the exporter serves a
// tenant from the `_custom_alerts` of its built map (overrides): one per
// user_threshold row — a switched-off recipe serves none, an slo_burn_rate
// two, an entry dropped as a duplicate none — counted before the per-tenant
// metric limit (max_metrics_per_tenant) truncates them. The per-tenant cap
// (MaxCustomRecipesDefault) is judged on it by da-guard's
// custom_alert_over_cap and, through served-values' custom_alert_rows,
// validate-config's custom_alert_cap (#3042).
func CustomAlertRowCount(tenant string, overrides map[string]ScheduledValue) int {
	rows, _, _, _ := resolveCustomAlertSpecs(tenant, overrides, nil)
	return len(rows)
}

// MaxCustomRecipesDefault is the per-tenant cap on a tenant's OWN custom-alert
// rows (S4, #3042), judged by tenant-api's write check, da-guard's
// custom_alert_over_cap and validate-config's custom_alert_cap — the compiler
// does not judge it. The number is the recipe contract's custom_alert_cap;
// scripts/tools/_lib_constants.py::MAX_CUSTOM_RECIPES is generated from it.
var MaxCustomRecipesDefault = contract.Cap

// ValidateTenantCustomAlerts is the tenant-api shift-left preflight (ADR §S5):
// the same per-entry judgment the exporter serves with and the compiler
// compiles from, in-process. It validates a tenant's `_custom_alerts` block (as stored in
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
		rts, err := resolveOneCustomAlert(tenant, spec)
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
		// A declaration counts toward the cap per emitted user_threshold row —
		// one for every recipe except slo_burn_rate, whose fixed critical+warning
		// fan-out counts as 2 (ADR-031: the cap tracks data-plane series budget).
		ownCount += len(rts)
		// within-tenant uniqueness — the CI compiler enforces these globally; the
		// preflight catches the within-PUT subset for fast feedback (PUT is a
		// full-overlay, so the body is the tenant's complete own set).
		if prev, dup := nameSeen[spec.Name]; dup {
			violations = append(violations,
				fmt.Sprintf("_custom_alerts[%d]: duplicate name %q (also at [%d]); names must be unique per tenant", i, spec.Name, prev))
		} else {
			nameSeen[spec.Name] = i
		}
		// per-row shape+severity uniqueness: slo_burn_rate registers BOTH its
		// severities, so two slo declarations sharing a shape collide on each.
		for _, rt := range rts {
			sevKey := rt.CustomLabels["recipe_id"] + "|" + rt.Severity
			if prev, dup := sevSeen[sevKey]; dup {
				violations = append(violations,
					fmt.Sprintf("_custom_alerts[%d]: a %s alert with the same shape already exists at [%d] (one per shape+severity)", i, rt.Severity, prev))
			} else {
				sevSeen[sevKey] = i
			}
		}
	}
	if ownCount > maxOwnRecipes {
		violations = append(violations,
			fmt.Sprintf("%d custom-alert recipes exceeds the per-tenant cap (%d); reduce the tenant's own _custom_alerts", ownCount, maxOwnRecipes))
	}
	return violations
}
