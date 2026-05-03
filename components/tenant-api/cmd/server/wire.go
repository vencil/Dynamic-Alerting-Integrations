package main

// PR-mode backend bootstrap (ADR-011).
//
// `direct` write-back is the boring default — no platform integration
// at all. The two PR-mode variants (GitHub PRs, GitLab MRs) each need
// to: validate required token / repo / project flags, build the
// platform Client, optionally point it at a self-hosted API endpoint,
// run a startup ValidateToken probe, and create the matching Tracker.
//
// In v2.7.0 this lived inline in main(); the resulting 50+ lines of
// switch-case made main() hard to read. PR-5 extracts it here so
// main() shows the wiring shape (flag-parse → managers → backend →
// router) without a paragraph of GitHub vs GitLab branching in the
// middle.

import (
	"log"
	"os"
	"time"

	gh "github.com/vencil/tenant-api/internal/github"
	gl "github.com/vencil/tenant-api/internal/gitlab"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
)

// prBackendFlags are the CLI-flag values consumed by wirePRBackend.
// Bundling them avoids passing 6 positional args to the helper and
// makes the contract obvious at the call site in main.go.
type prBackendFlags struct {
	Mode           string        // raw value of --write-mode / TA_WRITE_MODE
	GitHubRepo     string        // owner/repo
	GitHubBase     string        // PR target branch
	GitLabProject  string        // group/project or numeric ID
	GitLabBranch   string        // MR target branch
	ReloadInterval time.Duration // tracker poll cadence
}

// wirePRBackend resolves the PR-mode variant, builds the
// corresponding platform.Client + platform.Tracker, and returns the
// normalized WriteMode. Direct mode returns (nil, nil, WriteModeDirect).
//
// On missing required env vars / flags the helper calls log.Fatalf —
// matching pre-PR-5 behavior. Token-validation failures are logged at
// WARN (the deployment may have a deferred secret rotation; PR ops
// will surface the auth failure when actually invoked).
func wirePRBackend(f prBackendFlags) (platform.Client, platform.Tracker, handler.WriteMode) {
	wm := handler.WriteMode(f.Mode)
	switch wm {
	case handler.WriteModePR, handler.WriteModePRGitHub:
		// Normalize "pr" alias → "pr-github" so downstream comparisons
		// don't have to handle both. This was the v2.6.0 behavior;
		// preserving it.
		wm = handler.WriteModePR
		ghToken := os.Getenv("TA_GITHUB_TOKEN")
		if ghToken == "" {
			log.Fatalf("FATAL: TA_GITHUB_TOKEN is required when write-mode=pr/pr-github")
		}
		if f.GitHubRepo == "" {
			log.Fatalf("FATAL: --github-repo (or TA_GITHUB_REPO) is required when write-mode=pr/pr-github")
		}
		ghClient, err := gh.NewClient(ghToken, f.GitHubRepo, f.GitHubBase)
		if err != nil {
			log.Fatalf("FATAL: github client: %v", err)
		}
		if gheURL := os.Getenv("TA_GITHUB_API_URL"); gheURL != "" {
			ghClient.SetBaseURL(gheURL)
		}
		if err := ghClient.ValidateToken(); err != nil {
			log.Printf("WARN: GitHub token validation failed: %v (PR operations may fail)", err)
		}
		log.Printf("tenant-api: GitHub PR write-back mode enabled (repo=%s, base=%s)", f.GitHubRepo, f.GitHubBase)
		return ghClient, gh.NewTracker(ghClient, f.ReloadInterval), wm

	case handler.WriteModePRGitLab:
		glToken := os.Getenv("TA_GITLAB_TOKEN")
		if glToken == "" {
			log.Fatalf("FATAL: TA_GITLAB_TOKEN is required when write-mode=pr-gitlab")
		}
		if f.GitLabProject == "" {
			log.Fatalf("FATAL: --gitlab-project (or TA_GITLAB_PROJECT) is required when write-mode=pr-gitlab")
		}
		glClient, err := gl.NewClient(glToken, f.GitLabProject, f.GitLabBranch)
		if err != nil {
			log.Fatalf("FATAL: gitlab client: %v", err)
		}
		if glURL := os.Getenv("TA_GITLAB_API_URL"); glURL != "" {
			glClient.SetBaseURL(glURL)
		}
		if err := glClient.ValidateToken(); err != nil {
			log.Printf("WARN: GitLab token validation failed: %v (MR operations may fail)", err)
		}
		log.Printf("tenant-api: GitLab MR write-back mode enabled (project=%s, target=%s)", f.GitLabProject, f.GitLabBranch)
		return glClient, gl.NewTracker(glClient, f.ReloadInterval), wm

	default:
		log.Printf("tenant-api: direct write mode (commit-on-write)")
		return nil, nil, handler.WriteModeDirect
	}
}
