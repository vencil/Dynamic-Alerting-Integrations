//go:build forge_e2e

package forgee2e

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	gl "github.com/vencil/tenant-api/internal/gitlab"
)

// glSeeder drives the GitLab API directly (raw HTTP, api-scoped token) to set
// up fixtures the platform.Client interface doesn't expose: create/delete
// projects and commit files onto branches (so MRs carry a real diff). The
// methods actually UNDER TEST — CreateBranch / CreatePR / ListOpenPRs /
// DeleteBranch — are still exercised via gl.Client, never here.
type glSeeder struct {
	baseURL string
	token   string
	httpc   *http.Client
}

func newGLSeeder(cfg gitlabCfg) *glSeeder {
	return &glSeeder{
		baseURL: strings.TrimRight(cfg.apiURL, "/"),
		token:   cfg.token,
		httpc:   &http.Client{Timeout: 60 * time.Second},
	}
}

func (s *glSeeder) do(t *testing.T, method, path string, body any) []byte {
	t.Helper()
	var payload []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("seed marshal: %v", err)
		}
		payload = b
	}
	// Retry transport errors + 5xx (GitLab CE under bulk seeding load can
	// transiently 502/503 while Gitaly/Sidekiq churn — #616 round-2 phantom
	// readiness). 4xx are client/logic errors → fail immediately.
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
		req.Header.Set("PRIVATE-TOKEN", s.token)
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
			return out
		}
		if resp.StatusCode >= 500 {
			last = fmt.Sprintf("%d: %s", resp.StatusCode, string(out))
			time.Sleep(time.Duration(attempt) * 2 * time.Second)
			continue
		}
		t.Fatalf("seed %s %s → %d: %s", method, path, resp.StatusCode, string(out))
	}
	t.Fatalf("seed %s %s failed after retries: %s", method, path, last)
	return nil
}

// freshGitLabProject creates an isolated project initialized with a README
// (default branch `main` + one commit, so feature branches can fork off it)
// and registers best-effort teardown. Returns the numeric project id as a
// string (gl.Client accepts a numeric ID as its project path).
func freshGitLabProject(t *testing.T, s *glSeeder, name string) string {
	t.Helper()
	out := s.do(t, "POST", "/api/v4/projects", map[string]any{
		"name":                   name,
		"initialize_with_readme": true,
		"visibility":             "private",
	})
	var p struct {
		ID int `json:"id"`
	}
	if err := json.Unmarshal(out, &p); err != nil {
		t.Fatalf("parse project create: %v", err)
	}
	t.Cleanup(func() {
		req, _ := http.NewRequest("DELETE", fmt.Sprintf("%s/api/v4/projects/%d", s.baseURL, p.ID), nil)
		req.Header.Set("PRIVATE-TOKEN", s.token)
		if resp, err := s.httpc.Do(req); err == nil {
			resp.Body.Close()
		}
	})
	// README commit + repo materialization can lag a beat; a tiny settle
	// avoids a flaky "branch not found" on the first CreateBranch off main.
	time.Sleep(2 * time.Second)
	return fmt.Sprintf("%d", p.ID)
}

// commitFileNewBranch atomically creates `branch` off `startBranch` AND commits
// a file on it in a single Files-API call (the `start_branch` param). This is
// race-free under bulk seeding: the separate CreateBranch-then-commit path hit
// 400 "You can only create or edit files when you are on a branch" around MR
// #36 because the just-created branch hadn't propagated to Gitaly before the
// file commit fired. One atomic server-side op removes that cross-call race.
func (s *glSeeder) commitFileNewBranch(t *testing.T, projectID, branch, startBranch, path, content, msg string) {
	t.Helper()
	s.do(t, "POST",
		fmt.Sprintf("/api/v4/projects/%s/repository/files/%s", projectID, url.PathEscape(path)),
		map[string]any{"branch": branch, "start_branch": startBranch, "content": content, "commit_message": msg})
}

// seedMR creates one tenant-api-prefixed OPEN MR carrying a real diff:
// atomically create-branch-and-commit off main (Files API start_branch) →
// CreatePR (gl.Client). Returns the branch name. CreateBranch/DeleteBranch are
// exercised directly in the full-loop scenario; here we optimise for reliable
// bulk MR seeding (pagination needs >100). startBranch is the project default
// "main" (freshGitLabProject initializes with a README on main).
func seedMR(t *testing.T, cl *gl.Client, s *glSeeder, projectID, tenant string) string {
	t.Helper()
	branch := uniqueBranch(tenant)
	s.commitFileNewBranch(t, projectID, branch, "main", "e2e/"+tenant+".txt", "e2e "+runID(), "e2e seed "+tenant)
	if _, err := cl.CreatePR("[tenant-api][e2e] "+tenant, "seed", branch, []string{"tenant-api", "e2e"}); err != nil {
		t.Fatalf("CreatePR %s: %v", branch, err)
	}
	return branch
}
