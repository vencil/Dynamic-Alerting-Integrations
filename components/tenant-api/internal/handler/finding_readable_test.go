package handler

import (
	"testing"

	"github.com/vencil/tenant-api/internal/generator"
)

// TestFindingReadable_FileNamesItsTenantByTheOneRule pins that a file-only
// generator finding is withheld by the tenant its file is named after, as the
// one name rule (confdname.TenantNamedBy) reads the name (#3206). Before, a
// hand-written `TrimSuffix(".yaml") / TrimSuffix(".yml")` read `Secret.YAML`
// as no tenant at all — every other plane reads it as tenant `Secret` — so a
// finding on that file reached a caller who may not see `Secret`.
func TestFindingReadable_FileNamesItsTenantByTheOneRule(t *testing.T) {
	t.Parallel()
	shown := func(id string) bool { return id == "visible" }
	for _, tc := range []struct {
		file string
		want bool
	}{
		{"visible.yaml", true},
		{"Secret.yaml", false},
		{"Secret.YAML", false}, // the extension in any case
		{"team/Secret.yml", false},
		{"Secret.yml.yaml", false}, // tenant "Secret.yml", not "Secret"
		{"_defaults.yaml", true},   // a platform file names no tenant
		{"notes.txt", true},        // not a tenant file name
	} {
		file := tc.file
		got := findingReadable(shown, generator.Finding{File: &file})
		if got != tc.want {
			t.Errorf("findingReadable(file %q) = %v, want %v", tc.file, got, tc.want)
		}
	}
}
