package customalerts

import "testing"

// RecipeStatus reads the embedded recipe-status.json (generated from the SSOT).
// Every shipped recipe is active today, and an unknown recipe must report active
// (never block on an unknown name).
func TestRecipeStatus_EmbeddedAllActive(t *testing.T) {
	for _, r := range []string{"threshold", "rate", "ratio", "absence", "p99_latency", "forecast"} {
		if got := RecipeStatus(r); got != "active" {
			t.Errorf("RecipeStatus(%q) = %q, want active", r, got)
		}
	}
	if got := RecipeStatus("does_not_exist"); got != "active" {
		t.Errorf("RecipeStatus(unknown) = %q, want active", got)
	}
}

// The inclusive eol guard (ADR-024 §8): freeze GROWTH of eol usage, never
// collateral-block edits to existing/unrelated recipes.
func TestEolExpansionViolations(t *testing.T) {
	statusOf := func(r string) string {
		switch r {
		case "legacy_cpu", "old_ratio":
			return "eol"
		default:
			return "active"
		}
	}
	inst := func(recipe, name string) map[string]any {
		return map[string]any{"recipe": recipe, "name": name}
	}

	cases := []struct {
		name        string
		current     []map[string]any
		next        []map[string]any
		wantBlocked bool
	}{
		{
			name:        "add a new eol instance is blocked",
			current:     []map[string]any{inst("threshold", "a")},
			next:        []map[string]any{inst("threshold", "a"), inst("legacy_cpu", "b")},
			wantBlocked: true,
		},
		{
			name:        "edit an existing eol instance is allowed",
			current:     []map[string]any{inst("legacy_cpu", "b")},
			next:        []map[string]any{inst("legacy_cpu", "b")}, // same recipe+name; params would differ
			wantBlocked: false,
		},
		{
			name:        "remove an eol instance is allowed",
			current:     []map[string]any{inst("legacy_cpu", "b")},
			next:        []map[string]any{},
			wantBlocked: false,
		},
		{
			name:        "rename an existing eol instance is allowed (count unchanged)",
			current:     []map[string]any{inst("legacy_cpu", "b")},
			next:        []map[string]any{inst("legacy_cpu", "b2")},
			wantBlocked: false,
		},
		{
			name:        "add more of an existing eol recipe is blocked",
			current:     []map[string]any{inst("legacy_cpu", "b")},
			next:        []map[string]any{inst("legacy_cpu", "b"), inst("legacy_cpu", "c")},
			wantBlocked: true,
		},
		{
			name:        "swap one eol recipe for another is blocked (the new one grows 0->1)",
			current:     []map[string]any{inst("legacy_cpu", "b")},
			next:        []map[string]any{inst("old_ratio", "b")},
			wantBlocked: true,
		},
		{
			name:        "active recipes are never blocked",
			current:     []map[string]any{},
			next:        []map[string]any{inst("threshold", "a"), inst("rate", "b")},
			wantBlocked: false,
		},
		{
			name:        "no change is allowed",
			current:     []map[string]any{inst("legacy_cpu", "b"), inst("threshold", "a")},
			next:        []map[string]any{inst("legacy_cpu", "b"), inst("threshold", "a")},
			wantBlocked: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			viol := eolExpansionViolations(tc.current, tc.next, statusOf)
			if tc.wantBlocked && len(viol) == 0 {
				t.Errorf("expected a violation, got none")
			}
			if !tc.wantBlocked && len(viol) > 0 {
				t.Errorf("expected no violation, got: %v", viol)
			}
		})
	}
}
