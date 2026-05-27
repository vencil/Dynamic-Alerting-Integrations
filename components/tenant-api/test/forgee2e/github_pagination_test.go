//go:build forge_e2e

package forgee2e

import (
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	gh "github.com/vencil/tenant-api/internal/github"
)

// fixtureTarget is how many long-lived open PRs the pagination fixture maintains.
// >100 forces GitHub's 100-per-page max to spill onto a 2nd page, so ListOpenPRs
// must follow the Link rel="next" header to enumerate them all (the #615 bug:
// a single-page fetch silently truncated at 100). 105 gives a small margin.
const fixtureTarget = 105

// TestForgeE2E_GitHub_SeedPaginationFixture creates the long-lived >100-PR
// pagination fixture ONCE. Gated on E2E_GITHUB_SEED_FIXTURE=1 so the normal
// per-PR / nightly run never bulk-mutates the sandbox (issue #636 — a per-run
// 105-PR seed trips GitHub's ~80-content/min secondary rate limit, and the
// production gh.Client doesn't retry).
//
// All creation goes through the RETRYING ghSeeder (createBranchRaw / commitFile /
// createPRRaw), so a secondary-limit 403 backs off and recovers instead of failing.
// Idempotent: it counts existing fixture PRs and creates only the shortfall, so
// re-running tops up (and a partially-completed seed is resumable). The fixtures
// are intentionally NOT registered for t.Cleanup — they must PERSIST (the janitor
// skips fixtureBranchPrefix) so the read-only pagination test always has >100.
func TestForgeE2E_GitHub_SeedPaginationFixture(t *testing.T) {
	if os.Getenv("E2E_GITHUB_SEED_FIXTURE") != "1" {
		t.Skip("set E2E_GITHUB_SEED_FIXTURE=1 to (re)seed the >100-PR pagination fixture (#636)")
	}
	cfg := loadGitHubCfg(t)
	cl := cfg.clientWithToken(t, cfg.token)
	s := newGHSeeder(cfg)

	existing := countFixturePRs(t, cl)
	t.Logf("fixture pre-count: %d open (target %d)", existing, fixtureTarget)
	if existing >= fixtureTarget {
		t.Logf("fixture already at/above target — nothing to seed")
		return
	}

	sha := s.baseSHA(t)
	stamp := runID()
	for i := existing; i < fixtureTarget; i++ {
		branch := fmt.Sprintf("%s%s-%03d", fixtureBranchPrefix, stamp, i)
		s.createBranchRaw(t, branch, sha)
		s.commitFile(t, branch, fmt.Sprintf("fixture/%s-%03d.txt", stamp, i),
			"pagination fixture "+stamp, "fixture "+branch)
		num := s.createPRRaw(t, fmt.Sprintf("[tenant-api][fixture] pagination %s #%03d", stamp, i), branch)
		t.Logf("seeded fixture PR #%d (%s)", num, branch)
		// Each fixture is 3 content ops (ref + commit + PR); GitHub's secondary
		// limit is ~80 content/min, so ~2.5s/fixture keeps us at ~72/min — under
		// the limit (≈105×2.5s ≈ 4–5 min, well within the 20 m job cap). The
		// seeder's do() still retries a secondary-limit 403 as a backstop.
		time.Sleep(2500 * time.Millisecond)
	}

	if got := countFixturePRs(t, cl); got <= 100 {
		t.Fatalf("after seeding, fixture PR count = %d, want >100", got)
	} else {
		t.Logf("pagination fixture ready: %d open fixture PRs", got)
	}
}

// TestForgeE2E_GitHub_Pagination validates that ListOpenPRs follows GitHub's real
// Link rel="next" pagination: with the >100-PR fixture present it must enumerate
// every open PR (spilling past the 100-per-page max onto page 2) EXACTLY ONCE.
// Read-only — no per-run mutation, so no rate-limit fight.
//
// SKIPS until the fixture is seeded (see SeedPaginationFixture), so it is
// non-blocking on a sandbox that hasn't been provisioned yet — same posture as
// the rest of Track 1 before its secrets existed.
func TestForgeE2E_GitHub_Pagination(t *testing.T) {
	cfg := loadGitHubCfg(t)
	cl := cfg.clientWithToken(t, cfg.token)

	prs, err := cl.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs: %v", err)
	}

	fixtures := 0
	seen := make(map[int]int, len(prs))
	for _, pr := range prs {
		seen[pr.Number]++
		if strings.HasPrefix(pr.HeadRef, fixtureBranchPrefix) {
			fixtures++
		}
	}
	if fixtures <= 100 {
		t.Skipf("pagination fixture not seeded (%d fixture PRs, need >100) — run SeedPaginationFixture with E2E_GITHUB_SEED_FIXTURE=1 first", fixtures)
	}

	// >100 returned proves ListOpenPRs spilled past the 100-per-page max onto a
	// 2nd page; no Number seen twice proves it followed rel="next" without
	// re-fetching page 1 (the dedup the #615 fix guarantees).
	if len(prs) <= 100 {
		t.Errorf("ListOpenPRs returned %d PRs, want >100 (pagination must span >1 page)", len(prs))
	}
	for num, n := range seen {
		if n > 1 {
			t.Errorf("PR #%d returned %d times — pagination re-fetched a page (dedup failure)", num, n)
		}
	}
	t.Logf("pagination OK: %d open PRs (%d fixtures) enumerated, each exactly once", len(prs), fixtures)
}

// countFixturePRs returns the number of open PRs whose head branch is under the
// fixture prefix (via the production ListOpenPRs — read-only, no rate-limit risk).
func countFixturePRs(t *testing.T, cl *gh.Client) int {
	t.Helper()
	prs, err := cl.ListOpenPRs()
	if err != nil {
		t.Fatalf("ListOpenPRs: %v", err)
	}
	n := 0
	for _, pr := range prs {
		if strings.HasPrefix(pr.HeadRef, fixtureBranchPrefix) {
			n++
		}
	}
	return n
}
