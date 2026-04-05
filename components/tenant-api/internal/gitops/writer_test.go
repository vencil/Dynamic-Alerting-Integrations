package gitops

import (
	"strings"
	"testing"
)

func TestValidate(t *testing.T) {
	tests := []struct {
		name     string
		tenantID string
		yaml     string
		wantErrs int // 0 = valid
	}{
		{
			name:     "valid tenant config",
			tenantID: "db-a",
			// ValidateTenantKeys warns on unknown keys not in defaults.
			// Keys starting with _ are reserved keys (silently accepted).
			yaml:     "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
			wantErrs: 0,
		},
		{
			name:     "invalid YAML syntax",
			tenantID: "db-a",
			yaml:     "{{not yaml",
			wantErrs: 1,
		},
		{
			name:     "missing tenant section",
			tenantID: "db-a",
			yaml:     "tenants:\n  db-b:\n    cpu_threshold: \"80\"\n",
			wantErrs: 1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			errs := validate(tt.tenantID, tt.yaml)
			if (len(errs) > 0) != (tt.wantErrs > 0) {
				t.Errorf("validate(%q, ...) returned %d errors %v, want %d",
					tt.tenantID, len(errs), errs, tt.wantErrs)
			}
		})
	}
}

func TestNewWriter(t *testing.T) {
	w := NewWriter("/conf.d", "")
	if w.gitDir != "/conf.d" {
		t.Errorf("gitDir should default to configDir, got %q", w.gitDir)
	}

	w2 := NewWriter("/conf.d", "/repo")
	if w2.gitDir != "/repo" {
		t.Errorf("gitDir should be /repo, got %q", w2.gitDir)
	}
}

func TestDiffNewFile(t *testing.T) {
	// Diff against a non-existent file should show all lines as additions
	w := NewWriter(t.TempDir(), "")
	diff, err := w.Diff("new-tenant", "line1\nline2\n")
	if err != nil {
		t.Fatalf("Diff returned error: %v", err)
	}
	if !strings.Contains(diff, "+line1") {
		t.Errorf("Diff should show additions, got: %s", diff)
	}
}
