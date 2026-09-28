package handler

// #1385: GET /api/v1/tenants/{id} expands the profile the tenant elects —
// from its own file or from a root platform file's `tenants:` entry — as
// /metrics does, below the tenant's own keys and the platform entry's. A
// problem in the profile is a notice naming the file and the profile, never
// a validation_warning.

import (
	"strings"
	"testing"
)

func TestGetTenantExpandsTheElectedProfile(t *testing.T) {
	t.Parallel()
	const defaults = "defaults:\n  mysql_connections: 80\n  redis_memory: 70\n  pg_connections: 100\n"
	const profiles = "profiles:\n  strict:\n    mysql_connections: \"55\"\n    redis_memory: \"56\"\n    pg_connections: \"57\"\n"
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  map[string]float64 // component_metric → warning value
	}{
		{"tenant elects; its own key wins", map[string]string{
			"_defaults.yaml": defaults, "_profiles.yaml": profiles,
			"tx.yaml": "tenants:\n  tx:\n    _profile: strict\n    redis_memory: \"91\"\n"},
			map[string]float64{"mysql_connections": 55, "redis_memory": 91, "pg_connections": 57}},
		{"platform entry elects; its key wins over the profile", map[string]string{
			"_defaults.yaml": defaults, "_profiles.yaml": profiles,
			"_platform.yaml": "tenants:\n  tx:\n    _profile: strict\n    pg_connections: \"66\"\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_memory: \"91\"\n"},
			map[string]float64{"mysql_connections": 55, "redis_memory": 91, "pg_connections": 66}},
		{"profile in the root carrier", map[string]string{
			"_defaults.yaml": defaults + profiles,
			"tx.yaml":        "tenants:\n  tx:\n    _profile: strict\n"},
			map[string]float64{"mysql_connections": 55, "redis_memory": 56, "pg_connections": 57}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			detail := getTenantDetail(t, &Deps{ConfigDir: setupConfigDir(t, tc.files)}, "tx")
			for key, want := range tc.want {
				c, m, _ := strings.Cut(key, "_")
				if got, ok := resolvedValue(detail, c, m, "warning"); !ok || got != want {
					t.Errorf("%s = %v (present %v), want %v", key, got, ok, want)
				}
			}
			if len(detail.Warnings) != 0 || len(detail.Notices) != 0 {
				t.Errorf("warnings %q notices %q, want none", detail.Warnings, detail.Notices)
			}
		})
	}
}

func TestGetTenantProfileProblemIsANoticeNotAWarning(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  strict:\n    mysql_conections_typo: \"5\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: strict\n",
	})
	detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
	if len(detail.Warnings) != 0 {
		t.Errorf("validation_warnings = %q: a profile problem is not what a write of tx.yaml is rejected on", detail.Warnings)
	}
	if len(detail.Notices) != 1 || !strings.Contains(detail.Notices[0], `platform file _profiles.yaml, profile "strict"`) ||
		!strings.Contains(detail.Notices[0], "mysql_conections_typo") || strings.Contains(detail.Notices[0], configDir) {
		t.Errorf("validation_notices = %q, want one naming _profiles.yaml, profile strict and the key (no server path)", detail.Notices)
	}
}
