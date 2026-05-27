//go:build forge_e2e

package forgee2e

import (
	"fmt"
	"testing"
)

// TestForgeE2E_GitLab_Forbidden403 verifies #615's create-time 403 against a
// REAL GitLab CE: a read_api-only token passes /user but creating an MR is
// rejected with 403 → platform.ErrForbidden (clean). A real source branch is
// seeded first (via the api token) so a "branch not found" 400 can't mask the
// scope 403 we're actually asserting.
func TestForgeE2E_GitLab_Forbidden403(t *testing.T) {
	cfg := loadGitLabCfg(t)
	if cfg.roToken == "" {
		t.Skip("set E2E_GITLAB_RO_TOKEN (read_api-only token) to run the GitLab 403 scenario")
	}
	s := newGLSeeder(cfg)
	project := freshGitLabProject(t, s, "e2e-403-"+runID())

	api := cfg.clientForProject(t, project)
	branch := uniqueBranch(e2eTenant("gl-403"))
	if err := api.CreateBranch(branch); err != nil {
		t.Fatalf("seed branch: %v", err)
	}

	ro := cfg.roClientForProject(t, project)
	if err := ro.ValidateToken(); err != nil {
		t.Fatalf("read_api token should pass ValidateToken (/user), got: %v", err)
	}
	_, err := ro.CreatePR("[tenant-api][e2e] forbidden probe "+runID(), "expected 403", branch, []string{"tenant-api", "e2e"})
	assertForbiddenErr(t, ro.ProviderName(), err)
}

// TestForgeE2E_GitLab_FullLoop exercises the real round-trip against GitLab CE:
// seed a tenant-api-prefixed MR (branch + diff + MR) → it must surface via
// ListOpenPRs with the tenant extracted from the branch prefix and state open.
func TestForgeE2E_GitLab_FullLoop(t *testing.T) {
	cfg := loadGitLabCfg(t)
	s := newGLSeeder(cfg)
	project := freshGitLabProject(t, s, "e2e-fullloop-"+runID())
	cl := cfg.clientForProject(t, project)

	// Exercise CreateBranch + DeleteBranch directly against the real CE
	// (seedMR uses the atomic Files-API path, so these methods are covered here).
	lifecycle := uniqueBranch(e2eTenant("branchlifecycle"))
	if err := cl.CreateBranch(lifecycle); err != nil {
		t.Fatalf("CreateBranch %s: %v", lifecycle, err)
	}
	if err := cl.DeleteBranch(lifecycle); err != nil {
		t.Fatalf("DeleteBranch %s: %v", lifecycle, err)
	}

	tenant := e2eTenant("loop")
	branch := seedMR(t, cl, s, project, tenant)

	prs, err := cl.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs: %v", err)
	}
	found := false
	for _, pr := range prs {
		if pr.HeadRef == branch {
			found = true
			if pr.TenantID != tenant {
				t.Errorf("TenantID = %q, want %q", pr.TenantID, tenant)
			}
			if pr.State != "open" {
				t.Errorf("State = %q, want open (normalized from GitLab 'opened')", pr.State)
			}
		}
	}
	if !found {
		t.Fatalf("seeded MR (%s) not found in ListOpenPRs (%d returned)", branch, len(prs))
	}

	// Cleanup the source branch (project itself is torn down via t.Cleanup).
	if err := cl.DeleteBranch(branch); err != nil {
		t.Logf("DeleteBranch %s: %v (non-fatal)", branch, err)
	}
}

// TestForgeE2E_GitLab_Pagination is the marquee #615 check against a REAL
// server: seed >100 tenant-api-prefixed open MRs so ListOpenPRs MUST follow
// pagination (per_page=100), and assert every one is fetched exactly once
// (none dropped past page 1, none duplicated). Heavy — only runs under the
// forge_e2e tag with a live forge.
func TestForgeE2E_GitLab_Pagination(t *testing.T) {
	cfg := loadGitLabCfg(t)
	s := newGLSeeder(cfg)
	project := freshGitLabProject(t, s, "e2e-pagination-"+runID())
	cl := cfg.clientForProject(t, project)

	const n = 105 // > per_page=100 → forces a second page
	seeded := make(map[string]bool, n)
	for i := 0; i < n; i++ {
		tenant := e2eTenant(fmt.Sprintf("pg%03d", i))
		seedMR(t, cl, s, project, tenant)
		seeded[tenant] = true
	}

	prs, err := cl.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs: %v", err)
	}
	got := make(map[string]int, n)
	for _, pr := range prs {
		if seeded[pr.TenantID] {
			got[pr.TenantID]++
		}
	}
	if len(got) != n {
		t.Fatalf("expected %d distinct seeded tenants fetched across pages, got %d (total returned=%d)", n, len(got), len(prs))
	}
	for tenant, count := range got {
		if count != 1 {
			t.Errorf("tenant %s appeared %d times across pages (want 1 — dedup/pagination bug)", tenant, count)
		}
	}
}
