//go:build forge_e2e

package forgee2e

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	gh "github.com/vencil/tenant-api/internal/github"
)

// ghSeeder drives the GitHub REST API directly (raw HTTP, write-scoped PAT)
// for fixtures the platform.Client interface doesn't expose: commit a file
// onto a branch (Contents API → gives the PR a diff) and close a PR (the
// interface has no close method — teardown needs it because the dummy repo
// PERSISTS, unlike GitLab's ephemeral per-test project). The methods under
// test (CreateBranch / CreatePR / ListOpenPRs / DeleteBranch) run via gh.Client.
type ghSeeder struct {
	baseURL string // https://api.github.com (or GHE)
	repo    string // owner/repo
	token   string
	httpc   *http.Client
}

func newGHSeeder(cfg githubCfg) *ghSeeder {
	base := cfg.apiURL
	if base == "" {
		base = "https://api.github.com"
	}
	return &ghSeeder{
		baseURL: strings.TrimRight(base, "/"),
		repo:    cfg.repo,
		token:   cfg.token,
		httpc:   &http.Client{Timeout: 60 * time.Second},
	}
}

// do issues an authenticated GitHub request. Retries transport errors + 5xx
// (and 403 secondary-rate-limit, which GitHub returns under bulk write bursts);
// other 4xx are logic errors → fail. `fatal=false` makes it best-effort (used
// by teardown, which must not fail a passing test).
func (s *ghSeeder) do(t *testing.T, fatal bool, method, path string, body any) ([]byte, int) {
	t.Helper()
	var payload []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("seed marshal: %v", err)
		}
		payload = b
	}
	var last string
	for attempt := 1; attempt <= 4; attempt++ {
		var r io.Reader
		if payload != nil {
			r = bytes.NewReader(payload)
		}
		req, err := http.NewRequest(method, s.baseURL+path, r)
		if err != nil {
			t.Fatalf("seed req: %v", err)
		}
		req.Header.Set("Authorization", "Bearer "+s.token)
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := s.httpc.Do(req)
		if err != nil {
			last = err.Error()
			time.Sleep(time.Duration(attempt) * 2 * time.Second)
			continue
		}
		out, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode < 300 {
			return out, resp.StatusCode
		}
		// PRIMARY rate limit exhausted (the 5000/hr core budget) resets in up to
		// an hour → retrying is futile. Distinguish it from the SECONDARY/abuse
		// limit (transient → retried below) by the header: Remaining==0 means the
		// core budget is gone. Fast-fail with a clear message rather than burning
		// the backoff ladder then dying.
		if resp.StatusCode == 403 && resp.Header.Get("X-RateLimit-Remaining") == "0" {
			msg := fmt.Sprintf("%s %s → 403 PRIMARY rate limit exhausted (X-RateLimit-Remaining=0, Reset=%s) — not retrying",
				method, path, resp.Header.Get("X-RateLimit-Reset"))
			if fatal {
				t.Fatalf("seed %s", msg)
			}
			return out, resp.StatusCode
		}
		// 403 with a rate-limit signal (and Remaining!=0) → SECONDARY/abuse limit,
		// transient → retry. Other 403/4xx → not retryable.
		retryable := resp.StatusCode >= 500 ||
			(resp.StatusCode == 403 && strings.Contains(strings.ToLower(string(out)), "rate limit"))
		if retryable {
			last = fmt.Sprintf("%d: %s", resp.StatusCode, string(out))
			time.Sleep(time.Duration(attempt) * 3 * time.Second)
			continue
		}
		if fatal {
			t.Fatalf("seed %s %s → %d: %s", method, path, resp.StatusCode, string(out))
		}
		return out, resp.StatusCode
	}
	if fatal {
		t.Fatalf("seed %s %s failed after retries: %s", method, path, last)
	}
	return nil, 0
}

// commitFile commits a file onto an existing branch via the Contents API
// (content base64-encoded), so an MR/PR from that branch has a real diff.
func (s *ghSeeder) commitFile(t *testing.T, branch, path, content, msg string) {
	t.Helper()
	s.do(t, true, "PUT", fmt.Sprintf("/repos/%s/contents/%s", s.repo, path),
		map[string]any{
			"message": msg,
			"branch":  branch,
			"content": base64.StdEncoding.EncodeToString([]byte(content)),
		})
}

// closePRBestEffort closes a PR (PATCH state=closed) — best-effort teardown.
func (s *ghSeeder) closePRBestEffort(t *testing.T, number int) {
	t.Helper()
	s.do(t, false, "PATCH", fmt.Sprintf("/repos/%s/pulls/%d", s.repo, number),
		map[string]any{"state": "closed"})
}

// listE2EBranches returns every branch under the tenant-api/ prefix (matching-refs
// API), best-effort. The janitor uses it to sweep PHANTOM branches — ones left
// when a run died after CreateBranch but before CreatePR, so the PR-based sweep
// (ListOpenPRs) never sees them and they'd leak forever.
//
// per_page=100 (the GitHub list-endpoint max) gives ample headroom — realistic
// open-branch counts are tiny. A full page loop is unnecessary unless the
// sandbox ever holds >100 stale branches (the janitor runs every PR/merge, so
// it catches up across runs regardless).
func (s *ghSeeder) listE2EBranches(t *testing.T) []string {
	out, code := s.do(t, false, "GET",
		fmt.Sprintf("/repos/%s/git/matching-refs/heads/%s?per_page=100", s.repo, branchPrefix), nil)
	if code != 200 {
		return nil // best-effort (e.g. no matching refs / transient)
	}
	var refs []struct {
		Ref string `json:"ref"`
	}
	if err := json.Unmarshal(out, &refs); err != nil {
		t.Logf("janitor: parse matching-refs: %v", err)
		return nil
	}
	branches := make([]string, 0, len(refs))
	for _, r := range refs {
		branches = append(branches, strings.TrimPrefix(r.Ref, "refs/heads/"))
	}
	return branches
}

// seedPR creates one tenant-api-prefixed OPEN PR with a real diff: CreateBranch
// off the base (gh.Client) → commit a file (Contents API) → CreatePR (gh.Client).
// Registers best-effort teardown (close PR + delete branch) — critical because
// the dummy repo persists across runs. Returns the branch name.
func seedPR(t *testing.T, cl *gh.Client, s *ghSeeder, tenant string) string {
	t.Helper()
	branch := uniqueBranch(tenant)
	if err := cl.CreateBranch(branch); err != nil {
		t.Fatalf("CreateBranch %s: %v", branch, err)
	}
	s.commitFile(t, branch, "e2e/"+tenant+".txt", "e2e "+runID(), "e2e seed "+tenant)
	pr, err := cl.CreatePR("[tenant-api][e2e] "+tenant, "seed", branch, []string{"tenant-api", "e2e"})
	if err != nil {
		// branch was created but PR failed — still clean up the branch.
		_ = cl.DeleteBranch(branch)
		t.Fatalf("CreatePR %s: %v", branch, err)
	}
	t.Cleanup(func() {
		s.closePRBestEffort(t, pr.Number)
		_ = cl.DeleteBranch(branch)
	})
	return branch
}
