package customalerts

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"sort"
)

// recipe-status.json is DERIVED from the compiler SSOT (shape.py RECIPE_STATUS)
// by scripts/tools/dx/gen_recipe_status_json.py and embedded at build time, so
// the Go write path never hand-authors a second status map (the split-brain
// trap). A drift gate keeps it in sync with the SSOT.
//
//go:embed recipe-status.json
var recipeStatusJSON []byte

var recipeStatus = mustParseRecipeStatus()

func mustParseRecipeStatus() map[string]string {
	var doc struct {
		Statuses map[string]string `json:"statuses"`
	}
	if err := json.Unmarshal(recipeStatusJSON, &doc); err != nil {
		// The json is a build-time embedded artifact, not runtime input — a parse
		// failure is a generator/build bug, so fail loud at startup.
		panic("customalerts: invalid embedded recipe-status.json: " + err.Error())
	}
	return doc.Statuses
}

// RecipeStatus returns a recipe's lifecycle status (active/deprecated/eol),
// derived from the compiler SSOT via recipe-status.json (ADR-024 §8). An unknown
// recipe reports "active": the compiler's recipe_id is the authority that rejects
// unknown recipes, so this accessor never blocks on an unknown name.
func RecipeStatus(recipe string) string {
	if s, ok := recipeStatus[recipe]; ok {
		return s
	}
	return "active"
}

// EolExpansionViolations enforces the inclusive eol write guard (ADR-024 §8,
// "B2-wide"): a write may keep, edit, rename, or remove existing alerts that use
// an end-of-life recipe, but must not GROW usage of one. For each eol recipe, the
// instance count in next must not exceed the count in current.
//
// This is deliberately NOT "reject any write that touches an eol recipe": that
// full-overlay collateral block would stop a tenant from editing an UNRELATED
// alert during an incident just because a stale eol recipe sits in their config
// (the outage-hostage failure mode). current/next are the tenant's _custom_alerts
// arrays before/after the write. Returns one violation per expanded eol recipe,
// sorted for a deterministic response.
func EolExpansionViolations(current, next []map[string]any) []string {
	return eolExpansionViolations(current, next, RecipeStatus)
}

// eolExpansionViolations is the testable core: statusOf is injected so tests can
// simulate an eol recipe without mutating the embedded (all-active) status map.
func eolExpansionViolations(current, next []map[string]any, statusOf func(string) string) []string {
	cur := eolRecipeCounts(current, statusOf)
	nxt := eolRecipeCounts(next, statusOf)
	var viol []string
	for recipe, n := range nxt {
		if n > cur[recipe] {
			viol = append(viol, fmt.Sprintf(
				"recipe %q is end-of-life (eol): you can keep or edit existing alerts "+
					"using it, but cannot add new ones (have %d, write requests %d)",
				recipe, cur[recipe], n))
		}
	}
	sort.Strings(viol)
	return viol
}

// eolRecipeCounts counts, per eol recipe, how many instances declare it.
func eolRecipeCounts(insts []map[string]any, statusOf func(string) string) map[string]int {
	counts := map[string]int{}
	for _, inst := range insts {
		recipe, _ := inst["recipe"].(string)
		if recipe != "" && statusOf(recipe) == "eol" {
			counts[recipe]++
		}
	}
	return counts
}
