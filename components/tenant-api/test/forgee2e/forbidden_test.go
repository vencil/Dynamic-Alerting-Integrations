//go:build forge_e2e

package forgee2e

import (
	"errors"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
)

// TestForgeE2E_GitHub_Forbidden403 verifies #615's create-time 403 handling
// against the REAL GitHub API: a read-scoped token passes ValidateToken
// (/user) yet a PR-create returns 403 — which must surface as
// platform.ErrForbidden (clean, no upstream body leaked), not a generic 5xx.
//
// (The GitLab equivalent lives in gitlab_e2e_test.go, where it seeds a real
// source branch first so a missing-branch 400 can't mask the scope 403.)
func TestForgeE2E_GitHub_Forbidden403(t *testing.T) {
	cfg := loadGitHubCfg(t)
	if cfg.roToken == "" {
		t.Skip("set E2E_GITHUB_RO_TOKEN (read-scoped PAT) to run the GitHub 403 scenario")
	}
	ro := cfg.clientWithToken(t, cfg.roToken)

	// The read-scoped token is still VALID (passes /user) — exactly the gap
	// #615 closes: ValidateToken can't detect a missing write scope; only the
	// create-time call can.
	if err := ro.ValidateToken(); err != nil {
		t.Fatalf("read-scoped token should pass ValidateToken (/user), got: %v", err)
	}

	// GitHub checks token scope before validating the head branch, so a
	// non-existent branch still yields a permission 403 (not a 404/422).
	_, err := ro.CreatePR(
		"[tenant-api][e2e] forbidden probe "+runID(),
		"E2E read-scoped token probe — expected 403.",
		uniqueBranch(e2eTenant("gh-403")),
		[]string{"tenant-api", "e2e"},
	)
	assertForbiddenErr(t, ro.ProviderName(), err)
}

// assertForbiddenErr asserts a create error is a clean platform.ErrForbidden
// with no upstream JSON body leaked into the message (shared by the GitHub +
// GitLab 403 scenarios).
func assertForbiddenErr(t *testing.T, provider string, err error) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected a 403 from %s create with a read-scoped token, got nil", provider)
	}
	if !errors.Is(err, platform.ErrForbidden) {
		t.Fatalf("expected errors.Is(err, platform.ErrForbidden), got: %v", err)
	}
	// Sanitization: status-code-only, never the upstream JSON body (which on
	// GitHub carries message + documentation_url).
	for _, leak := range []string{"documentation_url", "{", "}"} {
		if strings.Contains(err.Error(), leak) {
			t.Errorf("error leaked upstream body (contains %q): %v", leak, err)
		}
	}
}
