//go:build forge_e2e

package forgee2e

import (
	"os"
	"strings"
	"testing"
)

// TestForgeE2E_GitHub_FullLoop exercises the real round-trip against
// api.github.com (dedicated sandbox repo): CreateBranch/DeleteBranch lifecycle
// + branch→commit→PR→ListOpenPRs (tenant extracted from branch prefix, state
// open). Teardown (close PR + delete branch) runs via seedPR's t.Cleanup — the
// sandbox repo persists, so cleanup is mandatory.
func TestForgeE2E_GitHub_FullLoop(t *testing.T) {
	cfg := loadGitHubCfg(t)
	cl := cfg.clientWithToken(t, cfg.token)
	s := newGHSeeder(cfg)

	// CreateBranch + DeleteBranch directly (platform.Client methods).
	lifecycle := uniqueBranch(e2eTenant("branchlifecycle"))
	if err := cl.CreateBranch(lifecycle); err != nil {
		t.Fatalf("CreateBranch %s: %v", lifecycle, err)
	}
	if err := cl.DeleteBranch(lifecycle); err != nil {
		t.Fatalf("DeleteBranch %s: %v", lifecycle, err)
	}

	tenant := e2eTenant("loop")
	branch := seedPR(t, cl, s, tenant)

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
				t.Errorf("State = %q, want open", pr.State)
			}
		}
	}
	if !found {
		t.Fatalf("seeded PR (%s) not found in ListOpenPRs (%d returned)", branch, len(prs))
	}
}

// TestForgeE2E_GitHub_Janitor sweeps orphaned tenant-api/* state left in the
// sandbox repo by failed/cancelled runs (defence-in-depth beyond per-test
// t.Cleanup, which doesn't run if the job is SIGKILLed). Gated on
// E2E_GITHUB_JANITOR=1 so it runs only when the workflow invokes it.
//
// Branch-primary: closing open PRs alone misses PHANTOM branches — ones created
// when a run died after CreateBranch but before CreatePR (no PR → the PR-based
// sweep never sees them → permanent leak). So we (1) close open PRs + delete
// their head branches, then (2) sweep ALL remaining tenant-api/ branches.
//
// ⚠️ This is a BROAD sweep of the SHARED sandbox — it closes/deletes ALL open
// tenant-api/* PRs+branches, not just one run's. CI runs are serialized by the
// workflow's `concurrency` group so they never overlap. Do NOT run it locally
// while CI may be running, or you'll nuke the live CI run's in-flight PRs.
//
// EXCEPTION: the long-lived >100-PR pagination fixture under fixtureBranchPrefix
// (tenant-api/fixture/, issue #636) is PRESERVED — both the PR-close and branch
// sweeps skip it, so a single one-time seed survives every janitor pass.
func TestForgeE2E_GitHub_Janitor(t *testing.T) {
	if os.Getenv("E2E_GITHUB_JANITOR") != "1" {
		t.Skip("set E2E_GITHUB_JANITOR=1 to run the orphan sweeper")
	}
	cfg := loadGitHubCfg(t)
	cl := cfg.clientWithToken(t, cfg.token)
	s := newGHSeeder(cfg)

	// 1. Close open tenant-api PRs + delete their head branches (skip the fixture).
	prs, err := cl.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs: %v", err)
	}
	closed, kept := 0, 0
	for _, pr := range prs {
		if strings.HasPrefix(pr.HeadRef, fixtureBranchPrefix) {
			kept++
			continue // preserve the pagination fixture (#636)
		}
		s.closePRBestEffort(t, pr.Number)
		_ = cl.DeleteBranch(pr.HeadRef)
		closed++
	}
	// 2. Sweep remaining tenant-api/ branches — the phantoms with no PR (skip the fixture).
	branches := s.listE2EBranches(t)
	swept := 0
	for _, b := range branches {
		if strings.HasPrefix(b, fixtureBranchPrefix) {
			continue // preserve the pagination fixture branches (#636)
		}
		_ = cl.DeleteBranch(b)
		swept++
	}
	t.Logf("janitor closed %d PR(s) + swept %d branch(es); preserved %d fixture PR(s)", closed, swept, kept)
}
