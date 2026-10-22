package config

// The custom-alert recipe contract (#3229, ADR-024): recipe_contract.json is
// the one place the recipe enums, patterns, bands, per-recipe fields,
// lifecycle and SLO windows/multipliers are written. The exporter reads it here; tenant-api
// reads it through the exported accessors below; every other copy (schema,
// portal, shape.py's GENERATED block, rule-packs/recipes/*.yaml) is generated
// from it by scripts/tools/dx/gen_recipe_contract.py, whose --check is the
// drift gate.

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"sort"
	"strings"
)

//go:embed recipe_contract.json
var recipeContractJSON []byte

// A field tagged optional:"true" may be absent or empty; every other string,
// slice and map in the contract must be present and non-empty (checked by
// requireNonEmpty, so a dropped key cannot zero a validator).
type recipeContractRecipe struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	// Params are the fields a declaration of this recipe may carry (besides
	// recipe and name); Required the ones it must, on top of base_required.
	Params   []string `json:"params"`
	Required []string `json:"required"`
	// Forbidden maps a field the recipe refuses to the hint its error gives.
	Forbidden map[string]string `json:"forbidden" optional:"true"`
	// FixedOp is the only op the recipe takes ("" = any of op.enum).
	FixedOp string `json:"fixed_op" optional:"true"`
	// GroupBy says whether group_by (per-dimension eval) applies.
	GroupBy bool `json:"group_by"`
}

type recipeContractField struct {
	Name          string `json:"name"`
	QuotedExample string `json:"quoted_example" optional:"true"`
	// DataPlane marks a value that rides the user_threshold series rather
	// than the rule identity: RecipeID (the shape slug) does not need it.
	DataPlane bool `json:"data_plane"`
}

type recipeContractEnum struct {
	Enum    []string `json:"enum"`
	Default string   `json:"default"`
}

// recipeContractValues is an enum with no default (the field is required, or
// absent means "none").
type recipeContractValues struct {
	Enum []string `json:"enum"`
}

type recipeContractInterval struct {
	ExclusiveMinimum float64 `json:"exclusive_minimum"`
	ExclusiveMaximum float64 `json:"exclusive_maximum"`
}

type recipeContract struct {
	Comment      string                 `json:"_comment" optional:"true"`
	BaseRequired []string               `json:"base_required"`
	Recipes      []recipeContractRecipe `json:"recipes"`
	Lifecycle    []string               `json:"lifecycle"`
	Cap          int                    `json:"custom_alert_cap"`
	Fields       []recipeContractField  `json:"fields"`
	Patterns     struct {
		Name    string `json:"name"`
		Metric  string `json:"metric"`
		Label   string `json:"label"`
		Window  string `json:"window"`
		Decimal string `json:"decimal"`
	} `json:"patterns"`
	Op struct {
		Enum      []string          `json:"enum"`
		Default   string            `json:"default"`
		Slug      map[string]string `json:"slug"`
		EqRecipes []string          `json:"eq_recipes"`
	} `json:"op"`
	For      recipeContractEnum   `json:"for"`
	Mode     recipeContractEnum   `json:"mode"`
	Horizon  recipeContractValues `json:"horizon"`
	GroupBy  recipeContractValues `json:"group_by"`
	Quantile struct {
		Default string `json:"default"`
		recipeContractInterval
	} `json:"quantile"`
	ReservedLabels    []string `json:"reserved_labels"`
	TemplateMetachars []string `json:"template_metachars"`
	Forecast          struct {
		CurrentBand float64 `json:"current_band"`
		// RatioFloorExclusiveMinimum is the lower bound of a ratio-mode
		// threshold; current_band is its (exclusive) upper bound.
		RatioFloorExclusiveMinimum float64 `json:"ratio_floor_exclusive_minimum"`
	} `json:"forecast"`
	SloBurnRate struct {
		Objective struct {
			recipeContractInterval
			Disable string `json:"disable"`
		} `json:"objective"`
		SloPeriod recipeContractEnum `json:"slo_period"`
		MinEvents struct {
			Default int `json:"default"`
			Minimum int `json:"minimum"`
			Maximum int `json:"maximum"`
		} `json:"min_events"`
		Tiers []struct {
			Severity        string `json:"severity"`
			Long            string `json:"long"`
			Short           string `json:"short"`
			MinEventsFactor int    `json:"min_events_factor"`
		} `json:"tiers"`
		Multipliers map[string]map[string]float64 `json:"multipliers"`
	} `json:"slo_burn_rate"`
}

// contract is the parsed recipe_contract.json. It is a build-time embedded
// artifact, not runtime input, so an invalid one is a build bug: fail loud at
// startup rather than run with a degraded validator.
var contract = mustParseRecipeContract(recipeContractJSON)

func mustParseRecipeContract(data []byte) recipeContract {
	c, err := parseRecipeContract(data)
	if err != nil {
		panic("config: recipe_contract.json: " + err.Error())
	}
	return c
}

// parseRecipeContract decodes the contract and checks the cross-references a
// typo would silently break (an unknown field is rejected; every enum default
// must be in its enum; every lifecycle status must be one of lifecycle; every
// slo_period must have both tier multipliers; every op must have a slug).
func parseRecipeContract(data []byte) (recipeContract, error) {
	var c recipeContract
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return c, err
	}
	if err := requireNonEmpty(reflect.ValueOf(c), "contract"); err != nil {
		return c, err
	}
	if c.Cap < 1 {
		return c, fmt.Errorf("custom_alert_cap %d must be >= 1", c.Cap)
	}
	fieldNames := map[string]bool{}
	for _, f := range c.Fields {
		fieldNames[f.Name] = true
	}
	for _, f := range c.BaseRequired {
		if !fieldNames[f] {
			return c, fmt.Errorf("base_required %q is not a field", f)
		}
	}
	for _, r := range c.Recipes {
		if !contains(c.Lifecycle, r.Status) {
			return c, fmt.Errorf("recipe %q status %q is not one of %v", r.Name, r.Status, c.Lifecycle)
		}
		for _, f := range r.Params {
			if !fieldNames[f] {
				return c, fmt.Errorf("recipe %q param %q is not a field", r.Name, f)
			}
		}
		for _, f := range r.Required {
			if !contains(r.Params, f) {
				return c, fmt.Errorf("recipe %q requires %q, which is not one of its params", r.Name, f)
			}
		}
		for f := range r.Forbidden {
			if !fieldNames[f] || contains(r.Params, f) {
				return c, fmt.Errorf("recipe %q forbids %q, which is not a field or is one of its params", r.Name, f)
			}
		}
		if r.FixedOp != "" && !contains(c.Op.Enum, r.FixedOp) {
			return c, fmt.Errorf("recipe %q fixed_op %q is not in %v", r.Name, r.FixedOp, c.Op.Enum)
		}
	}
	for _, iv := range []struct {
		what   string
		lo, hi float64
	}{
		{"quantile", c.Quantile.ExclusiveMinimum, c.Quantile.ExclusiveMaximum},
		{"objective", c.SloBurnRate.Objective.ExclusiveMinimum, c.SloBurnRate.Objective.ExclusiveMaximum},
		{"forecast ratio floor", c.Forecast.RatioFloorExclusiveMinimum, c.Forecast.CurrentBand},
	} {
		if !(iv.lo < iv.hi) {
			return c, fmt.Errorf("%s interval (%v,%v) is empty", iv.what, iv.lo, iv.hi)
		}
	}
	for _, e := range []struct {
		what string
		enum recipeContractEnum
	}{{"for", c.For}, {"mode", c.Mode}, {"slo_period", c.SloBurnRate.SloPeriod}} {
		if !contains(e.enum.Enum, e.enum.Default) {
			return c, fmt.Errorf("%s default %q is not in %v", e.what, e.enum.Default, e.enum.Enum)
		}
	}
	if !contains(c.Op.Enum, c.Op.Default) {
		return c, fmt.Errorf("op default %q is not in %v", c.Op.Default, c.Op.Enum)
	}
	for _, op := range c.Op.Enum {
		if c.Op.Slug[op] == "" {
			return c, fmt.Errorf("op %q has no slug", op)
		}
	}
	// sloBurnMultipliers reads the critical (fast) and warning (slow) tiers by
	// name, so the tiers must be exactly those two.
	var tiers []string
	for _, t := range c.SloBurnRate.Tiers {
		tiers = append(tiers, t.Severity)
	}
	if strings.Join(tiers, ",") != "critical,warning" {
		return c, fmt.Errorf("slo_burn_rate tiers %v must be [critical warning]", tiers)
	}
	for _, p := range c.SloBurnRate.SloPeriod.Enum {
		for _, t := range c.SloBurnRate.Tiers {
			if _, ok := c.SloBurnRate.Multipliers[p][t.Severity]; !ok {
				return c, fmt.Errorf("slo_period %q has no %s multiplier", p, t.Severity)
			}
		}
	}
	md := c.SloBurnRate.MinEvents
	if md.Default < md.Minimum || md.Default > md.Maximum {
		return c, fmt.Errorf("min_events default %d is outside [%d, %d]", md.Default, md.Minimum, md.Maximum)
	}
	for _, p := range []string{c.Patterns.Name, c.Patterns.Metric, c.Patterns.Label, c.Patterns.Window, c.Patterns.Decimal} {
		if _, err := regexp.Compile(p); err != nil || p == "" {
			return c, fmt.Errorf("pattern %q does not compile: %v", p, err)
		}
	}
	return c, nil
}

// requireNonEmpty reports the first string, slice or map under v that is
// empty and not tagged optional:"true" — a key dropped from the contract, or
// written as null / "" / [] / {}, decodes to that zero value.
func requireNonEmpty(v reflect.Value, path string) error {
	switch v.Kind() {
	case reflect.String:
		if v.Len() == 0 {
			return fmt.Errorf("%s is missing or empty", path)
		}
	case reflect.Slice, reflect.Map:
		if v.Len() == 0 {
			return fmt.Errorf("%s is missing or empty", path)
		}
		if v.Kind() == reflect.Slice {
			for i := 0; i < v.Len(); i++ {
				if err := requireNonEmpty(v.Index(i), fmt.Sprintf("%s[%d]", path, i)); err != nil {
					return err
				}
			}
			return nil
		}
		iter := v.MapRange()
		for iter.Next() {
			if err := requireNonEmpty(iter.Value(), fmt.Sprintf("%s.%v", path, iter.Key())); err != nil {
				return err
			}
		}
	case reflect.Struct:
		t := v.Type()
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			if f.Tag.Get("optional") == "true" {
				continue
			}
			name := strings.Split(f.Tag.Get("json"), ",")[0]
			if f.Anonymous {
				name = ""
			}
			sub := path
			if name != "" {
				sub = path + "." + name
			}
			if err := requireNonEmpty(v.Field(i), sub); err != nil {
				return err
			}
		}
	}
	return nil
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func stringSet(xs []string) map[string]bool {
	m := make(map[string]bool, len(xs))
	for _, x := range xs {
		m[x] = true
	}
	return m
}

// oneOf renders an enum for an error message: "1h/2h/4h".
func oneOf(xs []string) string { return strings.Join(xs, "/") }

// contractRecipe returns the contract's entry for recipe.
func contractRecipe(recipe string) (recipeContractRecipe, bool) {
	for _, r := range contract.Recipes {
		if r.Name == recipe {
			return r, true
		}
	}
	return recipeContractRecipe{}, false
}

// RecipeStatus returns a recipe's lifecycle status (one of the contract's
// lifecycle values). An unknown recipe reports "active": RecipeID is the
// authority that rejects unknown recipes, so this accessor never blocks on an
// unknown name.
func RecipeStatus(recipe string) string {
	for _, r := range contract.Recipes {
		if r.Name == recipe {
			return r.Status
		}
	}
	return "active"
}

// RecipeFieldOrder returns the canonical order of a recipe declaration's keys
// (recipe → identity → metric(s) → params → routing).
func RecipeFieldOrder() []string {
	out := make([]string, 0, len(contract.Fields))
	for _, f := range contract.Fields {
		out = append(out, f.Name)
	}
	return out
}

// RecipeQuotedTextFields returns the recipe fields whose TEXT is part of the
// contract and so must be a quoted YAML string (schema type: string), each
// mapped to the example a violation message quotes, plus the field names
// sorted for a deterministic report.
func RecipeQuotedTextFields() (map[string]string, []string) {
	m := map[string]string{}
	var names []string
	for _, f := range contract.Fields {
		if f.QuotedExample != "" {
			m[f.Name] = f.QuotedExample
			names = append(names, f.Name)
		}
	}
	sort.Strings(names)
	return m, names
}
