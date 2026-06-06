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
	m, err := parseRecipeStatus(recipeStatusJSON)
	if err != nil {
		// recipe-status.json is a build-time embedded artifact (generated from the
		// SSOT + drift-gated), not runtime input — any invalidity is a build bug,
		// so fail LOUD at startup rather than silently degrade EOL enforcement to
		// an all-"active" default (which would disable the guard).
		panic("customalerts: " + err.Error())
	}
	return m
}

// parseRecipeStatus decodes + validates the embedded status map. It FAILS CLOSED:
// an empty/missing map or an unknown lifecycle value is an error (not a silent
// default), because a degraded map would silently disable the eol-expansion guard.
func parseRecipeStatus(data []byte) (map[string]string, error) {
	var doc struct {
		Statuses map[string]string `json:"statuses"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("invalid embedded recipe-status.json: %w", err)
	}
	if len(doc.Statuses) == 0 {
		return nil, fmt.Errorf("embedded recipe-status.json has empty/missing statuses")
	}
	for recipe, status := range doc.Statuses {
		switch status {
		case "active", "deprecated", "eol":
		default:
			return nil, fmt.Errorf("invalid recipe status %q for recipe %q", status, recipe)
		}
	}
	return doc.Statuses, nil
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
