//go:build forge_e2e

// Package forgee2e holds real-forge end-to-end tests for tenant-api's PR/MR
// write-back (issue #616). Unlike the httptest mocks in internal/github +
// internal/gitlab (deterministic protocol checks owned by #615), these run
// against a REAL forge to catch environment realities a mock cannot:
// pagination Link/header behavior, true permission 403s, and CE-specific API
// differences / rate limits.
//
// Gated behind the `forge_e2e` build tag so `go test ./...` never compiles or
// runs them. Run explicitly:
//
//	go test -tags forge_e2e ./test/forgee2e/... -v
//
// Each provider is env-driven and SKIPS when its env is unset, so a run wired
// for only GitLab (the CE nightly) doesn't fail on missing GitHub config, and
// vice-versa.
package forgee2e

import (
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	gh "github.com/vencil/tenant-api/internal/github"
	gl "github.com/vencil/tenant-api/internal/gitlab"
)

// branchPrefix mirrors the prefix the production write path uses
// (gitops.WritePR → "tenant-api/<tenant>/<ts>"); ListOpenPRs filters on it,
// so E2E resources MUST carry it to be seen by the tracker logic under test.
const branchPrefix = "tenant-api/"

// runID namespaces every resource this run creates (branch name / PR title /
// tenant id) so concurrent CI runs against a shared dummy repo never collide.
// CI sets E2E_RUN_ID = "${{ github.run_id }}-${{ github.run_attempt }}"
// (unique per attempt). The local fallback adds the PID, because a bare
// second-resolution timestamp collides when two runs start in the same second
// (#616 round-2 review note).
func runID() string {
	if id := strings.TrimSpace(os.Getenv("E2E_RUN_ID")); id != "" {
		return id
	}
	return fmt.Sprintf("%s-%d-local", time.Now().UTC().Format("20060102-150405"), os.Getpid())
}

// e2eTenant returns a per-run, per-test tenant id namespaced by runID. The
// runID is embedded in the TENANT segment (not just a branch suffix) so it
// stays unique even under a deterministic per-tenant branch scheme, and so
// two concurrent runs exercising the "same" logical tenant don't collide
// (#616 round-2 review note).
func e2eTenant(name string) string {
	return fmt.Sprintf("e2e-%s-%s", name, runID())
}

// uniqueBranch builds a tenant-api-prefixed branch name for a tenant.
func uniqueBranch(tenant string) string {
	return branchPrefix + tenant + "/" + runID()
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

// --- GitHub provider config ---

type githubCfg struct {
	repo       string // owner/repo of the dedicated dummy repo
	token      string // write-scoped PAT (contents:write + pull_requests:write)
	roToken    string // read-scoped PAT (no write) — for the 403 scenario
	apiURL     string // optional, for GitHub Enterprise Server
	baseBranch string
}

// loadGitHubCfg loads GitHub E2E config, SKIPPING the test when the minimal
// write config is absent. The read-only token is loaded separately by tests
// that need it (it may be unset even when write config is present).
func loadGitHubCfg(t *testing.T) githubCfg {
	t.Helper()
	repo := strings.TrimSpace(os.Getenv("E2E_GITHUB_REPO"))
	token := strings.TrimSpace(os.Getenv("E2E_GITHUB_TOKEN"))
	if repo == "" || token == "" {
		t.Skip("GitHub forge E2E skipped: set E2E_GITHUB_REPO + E2E_GITHUB_TOKEN")
	}
	return githubCfg{
		repo:       repo,
		token:      token,
		roToken:    strings.TrimSpace(os.Getenv("E2E_GITHUB_RO_TOKEN")),
		apiURL:     strings.TrimSpace(os.Getenv("E2E_GITHUB_API_URL")),
		baseBranch: envOr("E2E_GITHUB_BASE", "main"),
	}
}

// clientWithToken builds a GitHub client using the given token (the 403
// scenario passes the read-scoped token).
func (c githubCfg) clientWithToken(t *testing.T, token string) *gh.Client {
	t.Helper()
	cl, err := gh.NewClient(token, c.repo, c.baseBranch)
	if err != nil {
		t.Fatalf("gh.NewClient: %v", err)
	}
	if c.apiURL != "" {
		cl.SetBaseURL(c.apiURL)
	}
	return cl
}

// --- GitLab provider config ---

type gitlabCfg struct {
	token        string // api-scoped token (write)
	roToken      string // read_api-only token — for the 403 scenario
	apiURL       string // self-hosted CE base URL (TA_GITLAB_API_URL analogue)
	targetBranch string
}

// loadGitLabCfg loads GitLab E2E config, SKIPPING when the api token or base
// URL is absent. Against an ephemeral CE, tests create their own isolated
// project (see freshGitLabProject), so only the api-scoped token + CE base URL
// are required.
func loadGitLabCfg(t *testing.T) gitlabCfg {
	t.Helper()
	token := strings.TrimSpace(os.Getenv("E2E_GITLAB_TOKEN"))
	apiURL := strings.TrimSpace(os.Getenv("E2E_GITLAB_API_URL"))
	if token == "" || apiURL == "" {
		t.Skip("GitLab forge E2E skipped: set E2E_GITLAB_TOKEN (api) + E2E_GITLAB_API_URL")
	}
	return gitlabCfg{
		token:        token,
		roToken:      strings.TrimSpace(os.Getenv("E2E_GITLAB_RO_TOKEN")),
		apiURL:       apiURL,
		targetBranch: envOr("E2E_GITLAB_BRANCH", "main"),
	}
}

// clientForProject builds a write-scoped GitLab client pointed at a specific
// project id/path (used after freshGitLabProject creates one).
func (c gitlabCfg) clientForProject(t *testing.T, project string) *gl.Client {
	t.Helper()
	cl, err := gl.NewClient(c.token, project, c.targetBranch)
	if err != nil {
		t.Fatalf("gl.NewClient(%s): %v", project, err)
	}
	if c.apiURL != "" {
		cl.SetBaseURL(c.apiURL)
	}
	return cl
}

// roClientForProject builds a read-scoped GitLab client for a project (403 scenario).
func (c gitlabCfg) roClientForProject(t *testing.T, project string) *gl.Client {
	t.Helper()
	cl, err := gl.NewClient(c.roToken, project, c.targetBranch)
	if err != nil {
		t.Fatalf("gl.NewClient(ro,%s): %v", project, err)
	}
	if c.apiURL != "" {
		cl.SetBaseURL(c.apiURL)
	}
	return cl
}
