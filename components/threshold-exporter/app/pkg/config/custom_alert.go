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
	"crypto/sha256"
	"encoding/hex"
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

// forecastCurrentBand mirrors _FORECAST_CURRENT_BAND in
// scripts/tools/dx/custom_alerts/shape.py. The compiler gates a ratio-mode forecast
// on `custom:fcbase < band` (a current-state sanity floor), so a threshold >= band is
// silently neutered (the alert can only fire once current headroom drops below the
// band). These two values MUST stay in lockstep.
const forecastCurrentBand = 0.5

// logCustomAlertError surfaces a malformed custom-alert at ERROR (paired with
// the da_custom_alert_parse_errors gauge so a silent skip is observable).
func logCustomAlertError(tenant, name string, err error) {
	log.Printf("ERROR: tenant=%s: custom alert %q rejected: %v", tenant, name, err)
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
// TRUNCATION, `min_events: 2.5` → 2, silently diverging from Python's
// isinstance(int) reject), raw must be digit-only, and the parsed value must
// be in [1, sloMinEventsMax] (mirrors shape.py::_normalize_min_events).
type strictInt struct {
	raw string
	tag string
}

func (s *strictInt) UnmarshalYAML(n *yaml.Node) error {
	s.raw, s.tag = n.Value, n.Tag
	return nil
}

var (
	customAlertRecipes = map[string]bool{
		"threshold": true, "rate": true, "ratio": true, "absence": true, "p99_latency": true,
		"forecast": true, "slo_burn_rate": true,
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
	// window is a BARE PromQL token interpolated raw into rate(m[<window>]) — it
	// cannot be quote-escaped, so it must be allowlisted to the Go-duration charset
	// (the schema pattern, now enforced imperatively). Mirrors shape.py _WINDOW_RE.
	customAlertWindowRe = regexp.MustCompile(`^([0-9]+(ns|us|µs|ms|s|m|h))+$`)
	// quantile: DECIMAL-float charset only. strconv.ParseFloat accepts Go hex-float
	// literals ("0x1p-1"=0.5) and underscores that CPython float() rejects — this
	// regex pins the accept-set so the Go preflight and the Python compiler can never
	// diverge (a divergence lands a poison commit that wedges the CI drift gate; Phase
	// C hunter finding). Anchored, so MatchString == fullmatch. Mirrors shape.py _QUANTILE_RE.
	customAlertQuantileRe = regexp.MustCompile(`^[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?$`)
	// Go-template metacharacters forbidden in a selector VALUE: the value reaches the
	// alert-annotation template context (recipes.py _alert_rule) where Prometheus
	// evaluates {{ query … }} across the whole TSDB. Reject so tenant data can never
	// BECOME template code (SSTI logic-less). Mirrors shape.py _TEMPLATE_METACHARS.
	customAlertTemplateMetachars = []string{"{{", "}}", "`"}
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
	// permitted slo_burn_rate `slo_period` budget windows (ADR-031). NOT a shape
	// component: the period only scales the burn-rate multipliers derived at
	// resolve time (see sloBurnMultipliers) — it never enters the slug/hash/rule
	// text, so changing it never re-slugs. MUST match shape.py ALLOWED_SLO_PERIOD
	// + the schema enum.
	customAlertSloPeriodValid = map[string]bool{"28d": true, "30d": true}
	// slo_burn_rate per-period burn multipliers (ADR-031 §2, LOCKED): fast = "1h
	// burns 2% of the budget", slow = "6h burns 5%"; M = consumption × period ÷
	// detection window. 30d (720h): fast 0.02×720/1=14.4, slow 0.05×720/6=6;
	// 28d (672h): fast 13.44, slow 5.6. The emitted user_threshold VALUE is
	// M × (1 − objective/100) — critical uses fast, warning uses slow. Pinned on
	// BOTH sides by tests/dx/fixtures/slo_burn_multiplier_vectors.json.
	sloBurnMultipliers = map[string]struct{ fast, slow float64 }{
		"30d": {fast: 14.4, slow: 6},
		"28d": {fast: 13.44, slow: 5.6},
	}
)

// sloMinEventsDefault mirrors shape.py SLO_MIN_EVENTS_DEFAULT (ADR-031 OQ-A):
// the fast-window bad-event absolute floor when min_events is omitted. The
// default IS materialised into the slug (`minev10`) so a later default change
// can never silently re-slug existing shapes.
const sloMinEventsDefault = 10

func customAlertSanitise(s string) string {
	return customAlertSanitiseRe.ReplaceAllString(s, "_")
}

// caHashField length-prefixes one field as "<byte-len>:<utf-8>". Concatenating
// length-prefixed fields is injective (NIST SP 800-185 TupleHash style): it closes the
// delimiter-aliasing collision class where a selector key/value straddles the `_`
// separator (#1008 / F3). Go string len is the UTF-8 byte length, matching Python.
// MUST stay byte-identical to shape.py::_lp.
func caHashField(s string) string {
	return strconv.Itoa(len(s)) + ":" + s
}

// shapeHashSuffix is the #1008/F3 disambiguation suffix for a selector-bearing recipe_id:
// the first 16 hex (64-bit) of SHA-256 over a length-prefixed canonical of the STRUCTURED
// shape identity. Fields are emitted in a FIXED order with an explicit "" for a field not
// applicable to the recipe (no None → matches Python trivially). MUST be byte-identical to
// shape.py::_shape_hash (golden-vector pinned).
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
			quantile = "0.99"
		}
	}
	horizon := ""
	if isForecast {
		horizon = spec.Horizon
	}
	forVal := spec.For
	if forVal == "" {
		forVal = "1m"
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

// rejectTemplateMetachars rejects a selector VALUE that could become Go-template
// code in the annotation sink (see customAlertTemplateMetachars). Mirrors
// shape.py::_reject_template_metachars.
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
// (0,1) (interpolated raw into histogram_quantile). Mirrors shape.py::_validate_quantile.
func validateQuantile(q string) error {
	// Charset gate FIRST — reject Go hex-floats/underscores that ParseFloat accepts
	// but CPython float() does not, keeping both sides' accept-set identical.
	if !customAlertQuantileRe.MatchString(q) {
		return fmt.Errorf("quantile %q must be a decimal number in the open interval (0,1)", q)
	}
	f, err := strconv.ParseFloat(q, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f <= 0 || f >= 1 {
		return fmt.Errorf("quantile %q must be a number in the open interval (0,1)", q)
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
// so the Go/Python accept-sets can never diverge (hex-float / underscore
// poison-commit class). Mirrors shape.py::_validate_objective (isinstance(str)
// → charset regex → float → range).
func validateSloObjective(o taggedScalar) error {
	if o.tag != "" && o.tag != "!!str" {
		return fmt.Errorf("objective must be a quoted YAML string (e.g. \"99.9\"), got a %s "+
			"scalar — a bare value is YAML-dialect-ambiguous and breaks the Go/Python "+
			"lockstep (#1017 quantile class)", o.tag)
	}
	if o.value == "" {
		return fmt.Errorf("slo_burn_rate recipe requires objective (SLO target percentage " +
			"in the open interval (0,100), e.g. \"99.9\"; \"disable\" opts out tri-state)")
	}
	if o.value == "disable" {
		return nil
	}
	if !customAlertQuantileRe.MatchString(o.value) {
		return fmt.Errorf("objective %q must be a decimal number in the open interval (0,100) "+
			"or \"disable\" (decimal charset only — keeps Go/Python accept-sets in lockstep)", o.value)
	}
	f, err := strconv.ParseFloat(o.value, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f <= 0 || f >= 100 {
		return fmt.Errorf("objective %q must be in the OPEN interval (0,100): 100 makes the "+
			"error budget 0 (threshold 0 → always fires), 0 makes it never fire", o.value)
	}
	return nil
}

// sloPeriod validates + normalizes the slo_burn_rate `slo_period` (budget
// window). Empty → default "30d". Mirrors shape.py::_normalize_slo_period.
func sloPeriod(spec CustomAlertSpec) (string, error) {
	p := spec.SloPeriod
	if p == "" {
		p = "30d"
	}
	if !customAlertSloPeriodValid[p] {
		return "", fmt.Errorf("slo_period %q must be one of 28d/30d", p)
	}
	return p, nil
}

// sloMinEventsMax mirrors the schema `maximum` (docs/schemas/
// tenant-config.schema.json min_events) and shape.py::_normalize_min_events's
// upper bound: an absurd floor (e.g. 10^15) silently disables the alert while
// looking configured, and the literal enters the rule text.
const sloMinEventsMax = 1000000

// sloMinEventsRe pins min_events' RAW text to plain decimal digits. This keeps
// `010` legal (base-0 ParseInt reads it as octal 8, matching PyYAML 1.1's
// resolution of the same text) while killing the `0o10`/`0x10`/`1_000` forms
// whose cross-dialect resolution diverges.
var sloMinEventsRe = regexp.MustCompile(`^[0-9]+$`)

// sloMinEvents validates + normalizes the slo_burn_rate `min_events` (the
// fast-window bad-event absolute floor, low-traffic false-positive guard).
// nil (missing) → default 10. Deferred strict-integer contract (see strictInt):
// tag must be !!int (bool/float/string rejected — mirrors Python's
// isinstance(int) + bool-subclass reject), raw must be digit-only, and the
// value must be in [1, sloMinEventsMax] (< 1 would disable the guard — the
// opt-out is objective:"disable"). Mirrors shape.py::_normalize_min_events.
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
	if n64 < 1 {
		return 0, fmt.Errorf("min_events %d must be >= 1 (0 would disable the low-traffic "+
			"guard entirely; use objective:\"disable\" to opt out instead)", n64)
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
	// slo_burn_rate fixes op ">" (a burn RATE exceeding its budget threshold is
	// the only meaningful direction). An explicit different op is a semantic
	// error, not a knob — reject loudly rather than silently slug `gt`.
	// Mirrors shape.py.
	if spec.Recipe == "slo_burn_rate" && op != ">" {
		return "", fmt.Errorf("op %q is not settable for slo_burn_rate (the recipe fixes '>' — "+
			"burn rate exceeding the objective-derived threshold); omit op", op)
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
	switch spec.Recipe {
	case "forecast":
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
	case "slo_burn_rate":
		// ADR-031: the burn windows (1h/5m fast, 6h/30m slow) are FIXED recipe
		// semantics — no `w{window}` slot (a stray authored `window` is ignored,
		// like forecast). `objective` replaces `threshold` (its VALUE rides the
		// data plane via user_threshold; resolveSloBurnRate derives the per-
		// severity burn thresholds from it) — so an authored `threshold` is a
		// declaration error, and objective/slo_period are validated here (ADR-029
		// dual-side duty) but NEVER enter the slug. MUST match shape.py.
		if spec.Threshold != "" {
			return "", fmt.Errorf("slo_burn_rate takes objective (SLO target percentage), not " +
				"threshold — severity is fixed by the recipe (fast→critical, slow→warning), " +
				"so a value:severity threshold has no meaning here")
		}
		if err := validateSloObjective(spec.Objective); err != nil {
			return "", err
		}
		if _, err := sloPeriod(spec); err != nil {
			return "", err
		}
		if spec.DenominatorMetric == "" {
			return "", fmt.Errorf("slo_burn_rate recipe requires denominator_metric (the " +
				"TOTAL-events counter; metric is the BAD-events counter)")
		}
		if err := validateCustomAlertMetric(spec.DenominatorMetric, "denominator_metric"); err != nil {
			return "", err
		}
		parts = append(parts, "den_"+spec.DenominatorMetric)
		// min_events IS a shape component (the compiler writes the literal into
		// the rule text): always emitted, default 10 materialised — the NEW
		// `minev{N}` slot sits between `den_` and `for` (slug-order contract).
		minEv, err := sloMinEvents(spec)
		if err != nil {
			return "", err
		}
		parts = append(parts, "minev"+strconv.Itoa(minEv))
	default:
		parts = append(parts, "w"+spec.Window)
		if spec.Recipe == "p99_latency" {
			q := string(spec.Quantile)
			if q == "" {
				q = "0.99"
			}
			if err := validateQuantile(q); err != nil {
				return "", err
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
	if len(gb) > 0 && (spec.Recipe == "absence" || op == "==" || spec.Recipe == "slo_burn_rate") {
		what := "op \"==\""
		switch spec.Recipe {
		case "absence":
			what = "the absence recipe"
		case "slo_burn_rate":
			// the SLI is a per-tenant aggregate by design — a per-dimension SLO
			// is a distinct declaration, not a group_by knob. Mirrors shape.py.
			what = "the slo_burn_rate recipe"
		}
		return "", fmt.Errorf("group_by (per-dimension eval) is not supported for %s — "+
			"it applies only to value-crossing recipes (ADR-024 Addendum)", what)
	}
	for _, g := range gb {
		parts = append(parts, "gb_"+g)
	}
	// #1008 / F3: recipe_id must be INJECTIVE over the shape identity, but the readable
	// slug is not — customAlertSanitise is lossy AND the `s_{key}_{value}`/`__`-join is
	// ambiguous even with no lossy char ({region_x:1} and {region:x_1} both →
	// `s_region_x_1`). A selector is the only tenant-controlled free-form slug field
	// (window/quantile/metric/for/group_by are charset/enum-bounded), so a selector-bearing
	// recipe carries a disambiguation suffix over the STRUCTURED identity; a no-selector
	// recipe stays byte-identical. MUST stay byte-identical to shape.py::recipe_id.
	slug := customAlertSanitise(strings.Join(parts, "__"))
	if len(spec.Selectors) > 0 || len(spec.SelectorsRe) > 0 {
		slug += "__x" + shapeHashSuffix(spec, op, items, gb)
	}
	return slug, nil
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
	if spec.Recipe == "" || spec.Name == "" || spec.Metric == "" || spec.Threshold == "" {
		return nil, fmt.Errorf("missing required field (recipe/name/metric/threshold)")
	}
	// recipe-aware shaping duration: forecast supplies `horizon` (required + enum
	// validated in RecipeID), every other recipe supplies `window` (empty window
	// → invalid PromQL like rate(m[])).
	if spec.Recipe != "forecast" {
		if spec.Window == "" {
			return nil, fmt.Errorf("missing required field window")
		}
		if !customAlertWindowRe.MatchString(spec.Window) {
			return nil, fmt.Errorf("window %q is not a valid Go duration "+
				"(^([0-9]+(ns|us|µs|ms|s|m|h))+$); it is interpolated raw into rate(…[<window>]) "+
				"— an invalid value is a PromQL injection", spec.Window)
		}
	}
	if !customAlertNameRe.MatchString(spec.Name) {
		return nil, fmt.Errorf("name %q is not a valid identifier", spec.Name)
	}
	rid, err := RecipeID(spec)
	if err != nil {
		return nil, err
	}
	valueStr, severity, err := parseCustomAlertThreshold(spec.Threshold)
	if err != nil {
		return nil, err
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
	value, err := strconv.ParseFloat(valueStr, 64)
	if err != nil {
		return nil, fmt.Errorf("threshold value %q is not numeric: %w", valueStr, err)
	}
	// ParseFloat accepts "NaN"/"Inf" — reject them: a non-finite threshold would
	// emit a nonsensical comparison series (e.g. `metric > NaN` never fires).
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return nil, fmt.Errorf("threshold value %q must be finite", valueStr)
	}
	// forecast ratio mode: the threshold is a headroom-fraction floor
	// (avail/capacity), so it must be in (0, forecastCurrentBand) — a floor >= 1 would
	// fire permanently; a floor >= the current-state band is silently neutered by the band gate (lockstep w/ shape.py).
	if spec.Recipe == "forecast" && spec.CapacityMetric != "" && (value <= 0 || value >= forecastCurrentBand) {
		return nil, fmt.Errorf("forecast ratio-mode threshold %v must be in (0,%v): a floor >= the current-state band is neutered by the band gate", value, forecastCurrentBand)
	}
	mode := spec.Mode
	if mode == "" {
		mode = "page"
	}
	// reject unsupported mode (typo): only page|silent ride the data plane; an
	// unknown value would surface a bogus mode label that S8 routing can't handle.
	if mode != "page" && mode != "silent" {
		return nil, fmt.Errorf("mode %q must be page or silent", mode)
	}
	// `for` must be one of the enum-bounded values (TRK-326): it enters the
	// recipe_id slug, so an out-of-enum value would silently spawn a distinct
	// shape/rule and bloat cardinality. Default 1m (matches schema default).
	forVal := spec.For
	if forVal == "" {
		forVal = "1m"
	}
	if !customAlertForValid[forVal] {
		return nil, fmt.Errorf("for %q is not allowed (one of 0s/1m/5m/15m/30m/1h)", forVal)
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
	if spec.Name == "" || spec.Metric == "" {
		return nil, fmt.Errorf("missing required field (recipe/name/metric)")
	}
	if !customAlertNameRe.MatchString(spec.Name) {
		return nil, fmt.Errorf("name %q is not a valid identifier", spec.Name)
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
	if obj == "disable" {
		return nil, errCustomAlertDisabled
	}
	objective, err := strconv.ParseFloat(obj, 64)
	if err != nil {
		// unreachable after RecipeID's validateSloObjective, but fail loud.
		return nil, fmt.Errorf("objective %q is not numeric: %w", obj, err)
	}
	mode := spec.Mode
	if mode == "" {
		mode = "page"
	}
	if mode != "page" && mode != "silent" {
		return nil, fmt.Errorf("mode %q must be page or silent", mode)
	}
	// `for` must be one of the enum-bounded values (TRK-326) on the slo path
	// too: RecipeID slugs `for{val}` without an enum check, so skipping this
	// here would let an out-of-enum `for` mint a bogus shape the Python
	// compiler (shape.py::_normalize_for inside recipe_id) rejects — a
	// preflight/compiler drift. Same check + message as the non-slo path.
	forVal := spec.For
	if forVal == "" {
		forVal = "1m"
	}
	if !customAlertForValid[forVal] {
		return nil, fmt.Errorf("for %q is not allowed (one of 0s/1m/5m/15m/30m/1h)", forVal)
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
	// Python re-computation in tests/dx/test_compile_custom_alerts.py so the
	// multiplier fixture pins bit-identical float64 values on both sides.
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
	sv, ok := overrides["_custom_alerts"]
	if !ok || strings.TrimSpace(sv.Default) == "" {
		return nil, nil, 0
	}
	var specs []CustomAlertSpec
	if err := yaml.Unmarshal([]byte(sv.Default), &specs); err != nil {
		// whole block unparseable → count as 1 error; the tenant gets NO custom
		// alerts but the rest of its config is unaffected.
		logCustomAlertError(tenant, "<block>", fmt.Errorf("cannot parse _custom_alerts: %w", err))
		return nil, nil, 1
	}
	var out []ResolvedThreshold
	var objectives []ResolvedSloObjective
	errCount := 0
	for _, spec := range specs {
		rts, err := resolveOneCustomAlert(tenant, spec)
		if errors.Is(err, errCustomAlertDisabled) {
			continue // three-state opt-out: no series, NOT an error
		}
		if err != nil {
			logCustomAlertError(tenant, spec.Name, err)
			errCount++
			continue
		}
		out = append(out, rts...)
		// slo_burn_rate: echo the raw objective for the user_slo_objective gauge.
		// Reaching here means resolveSloBurnRate already ParseFloat'ed it (the
		// disable case exited via errCustomAlertDisabled above → no gauge).
		if spec.Recipe == "slo_burn_rate" && len(rts) > 0 {
			if obj, perr := strconv.ParseFloat(spec.Objective.value, 64); perr == nil {
				objectives = append(objectives, ResolvedSloObjective{
					Tenant:    tenant,
					RecipeID:  rts[0].CustomLabels["recipe_id"],
					Objective: obj,
				})
			}
		}
	}
	return out, objectives, errCount
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
