// Package github provides a minimal GitHub REST API client for PR-based write-back.
//
// Design (ADR-011):
//   - PR-based write-back mode creates GitHub PRs instead of direct commits.
//   - Uses fine-grained PAT or GitHub App Installation Token for authentication.
//   - Only wraps the endpoints needed: create branch, create PR, list PRs.
//   - Implements platform.Client interface for provider-agnostic handler usage.
package github

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// Compile-time interface assertion: *Client implements platform.Client.
var _ platform.Client = (*Client)(nil)

// Client wraps the GitHub REST API for PR operations.
// Implements platform.Client.
type Client struct {
	token      string
	owner      string
	repo       string
	baseBranch string
	httpClient *http.Client
	baseURL    string // defaults to "https://api.github.com", configurable for GHE
}

// NewClient creates a GitHub API client.
// token is a fine-grained PAT or GitHub App token with contents:write + pull_requests:write.
// repoFullName is "owner/repo" format.
// baseBranch is the target branch for PRs (typically "main").
func NewClient(token, repoFullName, baseBranch string) (*Client, error) {
	parts := strings.SplitN(repoFullName, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return nil, fmt.Errorf("invalid repo format %q, expected owner/repo", repoFullName)
	}
	if baseBranch == "" {
		baseBranch = "main"
	}
	return &Client{
		token:      token,
		owner:      parts[0],
		repo:       parts[1],
		baseBranch: baseBranch,
		httpClient: &http.Client{Timeout: 30 * time.Second},
		baseURL:    "https://api.github.com",
	}, nil
}

// SetBaseURL overrides the GitHub API base URL (for GitHub Enterprise Server).
func (c *Client) SetBaseURL(url string) {
	c.baseURL = strings.TrimRight(url, "/")
}

// ProviderName returns "GitHub".
func (c *Client) ProviderName() string { return "GitHub" }

// ValidateToken checks if the token has valid permissions by calling /user.
// Returns nil if the token is valid, error otherwise.
func (c *Client) ValidateToken() error {
	_, err := c.doRequest("GET", "/user", nil)
	if err != nil {
		return fmt.Errorf("token validation failed: %w", err)
	}
	return nil
}

// CreateBranch creates a new branch from the base branch HEAD.
func (c *Client) CreateBranch(branchName string) error {
	// 1. Get base branch SHA
	resp, err := c.doRequest("GET",
		fmt.Sprintf("/repos/%s/%s/git/ref/heads/%s", c.owner, c.repo, c.baseBranch), nil)
	if err != nil {
		return fmt.Errorf("get base branch: %w", err)
	}
	var ref struct {
		Object struct {
			SHA string `json:"sha"`
		} `json:"object"`
	}
	if err := json.Unmarshal(resp, &ref); err != nil {
		return fmt.Errorf("parse base ref: %w", err)
	}

	// 2. Create new ref
	body := map[string]string{
		"ref": "refs/heads/" + branchName,
		"sha": ref.Object.SHA,
	}
	_, err = c.doRequest("POST",
		fmt.Sprintf("/repos/%s/%s/git/refs", c.owner, c.repo), body)
	if err != nil {
		return fmt.Errorf("create branch: %w", err)
	}
	return nil
}

// CreatePR creates a pull request and returns its metadata as platform.PRInfo.
func (c *Client) CreatePR(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
	payload := map[string]interface{}{
		"title": title,
		"body":  body,
		"head":  headBranch,
		"base":  c.baseBranch,
	}
	resp, err := c.doRequest("POST",
		fmt.Sprintf("/repos/%s/%s/pulls", c.owner, c.repo), payload)
	if err != nil {
		return nil, fmt.Errorf("create PR: %w", err)
	}

	var pr struct {
		Number  int    `json:"number"`
		HTMLURL string `json:"html_url"`
		State   string `json:"state"`
		Title   string `json:"title"`
		Head    struct {
			Ref string `json:"ref"`
		} `json:"head"`
		CreatedAt string `json:"created_at"`
	}
	if err := json.Unmarshal(resp, &pr); err != nil {
		return nil, fmt.Errorf("parse PR response: %w", err)
	}

	// Add labels if provided (best-effort, don't fail the PR creation)
	if len(labels) > 0 {
		labelPayload := map[string]interface{}{"labels": labels}
		_, _ = c.doRequest("POST",
			fmt.Sprintf("/repos/%s/%s/issues/%d/labels", c.owner, c.repo, pr.Number), labelPayload)
	}

	return &platform.PRInfo{
		Number:    pr.Number,
		WebURL:    pr.HTMLURL,
		State:     pr.State,
		Title:     pr.Title,
		HeadRef:   pr.Head.Ref,
		CreatedAt: pr.CreatedAt,
	}, nil
}

// DeleteBranch deletes a feature branch (cleanup after merge/close).
func (c *Client) DeleteBranch(branchName string) error {
	_, err := c.doRequest("DELETE",
		fmt.Sprintf("/repos/%s/%s/git/refs/heads/%s", c.owner, c.repo, branchName), nil)
	if err != nil {
		return fmt.Errorf("delete branch: %w", err)
	}
	return nil
}

// ListOpenPRs returns all open PRs created by tenant-api (filtered by head prefix).
func (c *Client) ListOpenPRs() ([]platform.PRInfo, error) {
	resp, err := c.doRequest("GET",
		fmt.Sprintf("/repos/%s/%s/pulls?state=open&per_page=100", c.owner, c.repo), nil)
	if err != nil {
		return nil, fmt.Errorf("list PRs: %w", err)
	}

	var prs []struct {
		Number  int    `json:"number"`
		HTMLURL string `json:"html_url"`
		State   string `json:"state"`
		Title   string `json:"title"`
		Head    struct {
			Ref string `json:"ref"`
		} `json:"head"`
		CreatedAt string `json:"created_at"`
	}
	if err := json.Unmarshal(resp, &prs); err != nil {
		return nil, fmt.Errorf("parse PRs: %w", err)
	}

	var result []platform.PRInfo
	for _, pr := range prs {
		// Only include PRs created by tenant-api (branch prefix: tenant-api/)
		if !strings.HasPrefix(pr.Head.Ref, "tenant-api/") {
			continue
		}
		info := platform.PRInfo{
			Number:    pr.Number,
			WebURL:    pr.HTMLURL,
			State:     pr.State,
			Title:     pr.Title,
			HeadRef:   pr.Head.Ref,
			CreatedAt: pr.CreatedAt,
		}
		// Extract tenant ID from branch name: tenant-api/{tenantID}/{timestamp}
		parts := strings.SplitN(pr.Head.Ref, "/", 3)
		if len(parts) >= 2 {
			info.TenantID = parts[1]
		}
		result = append(result, info)
	}
	return result, nil
}

// doRequest performs an authenticated GitHub API request.
func (c *Client) doRequest(method, path string, body interface{}) ([]byte, error) {
	var bodyReader io.Reader
	if body != nil {
		jsonBody, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(jsonBody)
	}

	url := c.baseURL + path
	req, err := http.NewRequest(method, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode >= 400 {
		// Sanitize: log the full response for debugging but only expose status code to callers.
		// This prevents leaking internal GitHub error details to API consumers.
		slog.Warn("github API non-2xx",
			"method", method, "path", path, "status", resp.StatusCode, "body", string(respBody))
		return nil, fmt.Errorf("GitHub API %s %s returned %d", method, path, resp.StatusCode)
	}
	return respBody, nil
}
