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
	"net/url"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// maxPRPages caps ListOpenPRs pagination at 1000 PRs (10 × per_page=100).
// Mirrors the gitlab client's safety limit so a misbehaving forge that
// keeps returning a "next" link can't spin the loop indefinitely.
const maxPRPages = 10

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
//
// Follows GitHub's Link-header pagination (rel="next") so repos with more
// than per_page open PRs are fully enumerated. Without this the single-page
// fetch silently truncated at 100, so a tenant whose pending PR fell past
// the first page looked PR-less to dedup → a duplicate PR got opened for it.
func (c *Client) ListOpenPRs() ([]platform.PRInfo, error) {
	var result []platform.PRInfo

	path := fmt.Sprintf("/repos/%s/%s/pulls?state=open&per_page=100", c.owner, c.repo)
	for page := 1; ; page++ {
		body, headers, err := c.do("GET", path, nil)
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
		if err := json.Unmarshal(body, &prs); err != nil {
			return nil, fmt.Errorf("parse PRs: %w", err)
		}

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

		next := nextPagePath(headers.Get("Link"))
		if next == "" {
			break
		}
		if page >= maxPRPages {
			slog.Warn("github PR pagination hit safety limit",
				"page", page, "collected", len(result))
			break
		}
		path = next
	}

	return result, nil
}

// nextPagePath extracts the rel="next" target from a GitHub Link header and
// returns it as a base-URL-relative path+query (host stripped). Returning a
// relative path lets doRequest re-attach the configured baseURL, so
// pagination keeps hitting the same host (works for api.github.com, GHE, and
// httptest alike). Returns "" when there is no next page.
func nextPagePath(link string) string {
	if link == "" {
		return ""
	}
	for _, part := range strings.Split(link, ",") {
		seg := strings.TrimSpace(part)
		if !strings.Contains(seg, `rel="next"`) {
			continue
		}
		lo := strings.Index(seg, "<")
		hi := strings.Index(seg, ">")
		if lo < 0 || hi <= lo {
			continue
		}
		u, err := url.Parse(seg[lo+1 : hi])
		if err != nil {
			return ""
		}
		p := u.EscapedPath()
		if u.RawQuery != "" {
			p += "?" + u.RawQuery
		}
		return p
	}
	return ""
}

// doRequest performs an authenticated GitHub API request, discarding
// response headers. Use do() directly when the Link header is needed.
func (c *Client) doRequest(method, path string, body interface{}) ([]byte, error) {
	respBody, _, err := c.do(method, path, body)
	return respBody, err
}

// do performs an authenticated GitHub API request and returns the response
// body and headers. Non-2xx responses become a *platform.APIError carrying
// only the status code — the upstream body is logged for debugging but never
// surfaced to callers, so GitHub error details don't leak through the API.
// A 403 matches errors.Is(err, platform.ErrForbidden) so handlers can map a
// missing-write-scope token to a clean HTTP 403 instead of a 500.
func (c *Client) do(method, path string, body interface{}) ([]byte, http.Header, error) {
	var bodyReader io.Reader
	if body != nil {
		jsonBody, err := json.Marshal(body)
		if err != nil {
			return nil, nil, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(jsonBody)
	}

	reqURL := c.baseURL + path
	req, err := http.NewRequest(method, reqURL, bodyReader)
	if err != nil {
		return nil, nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, nil, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode >= 400 {
		// Sanitize: log the full response for debugging but only expose status code to callers.
		// This prevents leaking internal GitHub error details to API consumers.
		slog.Warn("github API non-2xx",
			"method", method, "path", path, "status", resp.StatusCode, "body", string(respBody))
		return nil, resp.Header, &platform.APIError{
			Provider: "GitHub", Method: method, Path: path, StatusCode: resp.StatusCode,
		}
	}
	return respBody, resp.Header, nil
}
