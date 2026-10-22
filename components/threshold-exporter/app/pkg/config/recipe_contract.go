package config

// The custom-alert recipe contract (#3229, ADR-024): recipe_contract.json is
// the one place the recipe enums, patterns, bands, lifecycle and SLO
// windows/multipliers are written. The exporter reads it here; tenant-api
// reads it through the exported accessors below; every other copy (schema,
// portal, shape.py's GENERATED block, rule-packs/recipes/*.yaml) is generated
// from it by scripts/tools/dx/gen_recipe_contract.py, whose --check is the
// drift gate.

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

//go:embed recipe_contract.json
var recipeContractJSON []byte

type recipeContractRecipe struct {
	Name     string `json:"name"`
	Status   string `json:"status"`
	Duration string `json:"duration"`
}

type recipeContractField struct {
	Name          string `json:"name"`
	QuotedExample string `json:"quoted_example"`
}

type recipeContractEnum struct {
	Enum    []string `json:"enum"`
	Default string   `json:"default"`
}

type recipeContractInterval struct {
	ExclusiveMinimum float64 `json:"exclusive_minimum"`
	ExclusiveMaximum float64 `json:"exclusive_maximum"`
}

type recipeContract struct {
	Comment   string                 `json:"_comment"`
	Recipes   []recipeContractRecipe `json:"recipes"`
	Lifecycle []string               `json:"lifecycle"`
	Cap       int                    `json:"custom_alert_cap"`
	Fields    []recipeContractField  `json:"fields"`
	Patterns  struct {
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
	For      recipeContractEnum `json:"for"`
	Mode     recipeContractEnum `json:"mode"`
	Horizon  recipeContractEnum `json:"horizon"`
	GroupBy  recipeContractEnum `json:"group_by"`
	Quantile struct {
		Default string `json:"default"`
		recipeContractInterval
	} `json:"quantile"`
	ReservedLabels    []string `json:"reserved_labels"`
	TemplateMetachars []string `json:"template_metachars"`
	Forecast          struct {
		CurrentBand float64 `json:"current_band"`
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
	if len(c.Recipes) == 0 {
		return c, fmt.Errorf("no recipes")
	}
	if c.Cap < 1 {
		return c, fmt.Errorf("custom_alert_cap %d must be >= 1", c.Cap)
	}
	for _, r := range c.Recipes {
		if !contains(c.Lifecycle, r.Status) {
			return c, fmt.Errorf("recipe %q status %q is not one of %v", r.Name, r.Status, c.Lifecycle)
		}
		switch r.Duration {
		case "window", "horizon", "none":
		default:
			return c, fmt.Errorf("recipe %q duration %q is not window/horizon/none", r.Name, r.Duration)
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
