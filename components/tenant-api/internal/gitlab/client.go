// Package gitlab provides a minimal GitLab REST API client for MR-based write-back.
//
// Design (ADR-011):
//   - MR-based write-back mode creates GitLab Merge Requests instead of direct commits.
//   - Uses project/group access token or personal access token for authentication.
//   - Only wraps the endpoints needed: create branch, create MR, list MRs.
//   - Mirrors the github.Client interface via platform.Client.
package gitlab

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// Compile-time interface assertion: *Client implements platform.Client.
var _ platform.Client = (*Client)(nil)

// Client wraps the GitLab REST API v4 for MR operations.
type Client struct {
	token        string
	projectPath  string // URL-encoded project path or numeric ID
	targetBranch string
	httpClient   *http.Client
	baseURL      string // defaults to "https://gitlab.com", configurable for self-hosted
}

// NewClient creates a GitLab API client.
// token is a project/group access token or personal access token with api scope.
// projectPath is "group/project" or a numeric project ID.
// targetBranch is the target branch for MRs (typically "main").
func NewClient(token, projectPath, targetBranch string) (*Client, error) {
	if projectPath == "" {
		return nil, fmt.Errorf("project path or ID is required")
	}
	if targetBranch == "" {
		targetBranch = "main"
	}
	return &Client{
		token:        token,
		projectPath:  projectPath,
		targetBranch: targetBranch,
		httpClient:   &http.Client{Timeout: 30 * time.Second},
		baseURL:      "https://gitlab.com",
	}, nil
}

// SetBaseURL overrides the GitLab instance URL (for self-hosted GitLab).
func (c *Client) SetBaseURL(u string) {
	c.baseURL = strings.TrimRight(u, "/")
}

// ProviderName returns "GitLab".
func (c *Client) ProviderName() string { return "GitLab" }

// ValidateToken checks if the token has valid permissions by calling /user.
func (c *Client) ValidateToken() error {
	_, err := c.doRequest("GET", "/api/v4/user", nil)
	if err != nil {
		return fmt.Errorf("token validation failed: %w", err)
	}
	return nil
}

// projectAPI returns the URL-encoded project path for API calls.
func (c *Client) projectAPI() string {
	return url.PathEscape(c.projectPath)
}

// CreateBranch creates a new branch from the target branch HEAD.
func (c *Client) CreateBranch(branchName string) error {
	body := map[string]string{
		"branch": branchName,
		"ref":    c.targetBranch,
	}
	_, err := c.doRequest("POST",
		fmt.Sprintf("/api/v4/projects/%s/repository/branches", c.projectAPI()), body)
	if err != nil {
		return fmt.Errorf("create branch: %w", err)
	}
	return nil
}

// CreatePR creates a merge request and returns its metadata as a platform.PRInfo.
func (c *Client) CreatePR(title, body, sourceBranch string, labels []string) (*platform.PRInfo, error) {
	payload := map[string]interface{}{
		"source_branch": sourceBranch,
		"target_branch": c.targetBranch,
		"title":         title,
		"description":   body,
	}
	if len(labels) > 0 {
		// GitLab API v4 accepts labels as a comma-separated string or array.
		// Use array form for clarity and to avoid issues with labels containing commas.
		payload["labels"] = labels
	}

	resp, err := c.doRequest("POST",
		fmt.Sprintf("/api/v4/projects/%s/merge_requests", c.projectAPI()), payload)
	if err != nil {
		return nil, fmt.Errorf("create MR: %w", err)
	}

	var mr struct {
		IID          int    `json:"iid"`
		WebURL       string `json:"web_url"`
		State        string `json:"state"`
		Title        string `json:"title"`
		SourceBranch string `json:"source_branch"`
		CreatedAt    string `json:"created_at"`
	}
	if err := json.Unmarshal(resp, &mr); err != nil {
		return nil, fmt.Errorf("parse MR response: %w", err)
	}

	return &platform.PRInfo{
		Number:    mr.IID,
		WebURL:    mr.WebURL,
		State:     normalizeState(mr.State),
		Title:     mr.Title,
		HeadRef:   mr.SourceBranch,
		CreatedAt: mr.CreatedAt,
	}, nil
}

// ListOpenPRs returns all open MRs created by tenant-api (filtered by source branch prefix).
// Handles pagination to support projects with >100 open MRs.
func (c *Client) ListOpenPRs() ([]platform.PRInfo, error) {
	var result []platform.PRInfo

	page := 1
	for {
		resp, err := c.doRequest("GET",
			fmt.Sprintf("/api/v4/projects/%s/merge_requests?state=opened&per_page=100&page=%d", c.projectAPI(), page), nil)
		if err != nil {
			return nil, fmt.Errorf("list MRs: %w", err)
		}

		var mrs []struct {
			IID          int    `json:"iid"`
			WebURL       string `json:"web_url"`
			State        string `json:"state"`
			Title        string `json:"title"`
			SourceBranch string `json:"source_branch"`
			CreatedAt    string `json:"created_at"`
		}
		if err := json.Unmarshal(resp, &mrs); err != nil {
			return nil, fmt.Errorf("parse MRs: %w", err)
		}

		for _, mr := range mrs {
			// Only include MRs created by tenant-api (branch prefix: tenant-api/)
			if !strings.HasPrefix(mr.SourceBranch, "tenant-api/") {
				continue
			}
			info := platform.PRInfo{
				Number:    mr.IID,
				WebURL:    mr.WebURL,
				State:     normalizeState(mr.State),
				Title:     mr.Title,
				HeadRef:   mr.SourceBranch,
				CreatedAt: mr.CreatedAt,
			}
			// Extract tenant ID from branch name: tenant-api/{tenantID}/{timestamp}
			parts := strings.SplitN(mr.SourceBranch, "/", 3)
			if len(parts) >= 2 {
				info.TenantID = parts[1]
			}
			result = append(result, info)
		}

		// GitLab returns fewer items than per_page when on the last page
		if len(mrs) < 100 {
			break
		}
		page++

		// Safety limit: 10 pages = 1000 MRs max
		if page > 10 {
			log.Printf("WARN: GitLab MR pagination hit safety limit at page %d (collected %d MRs so far)", page, len(result))
			break
		}
	}

	return result, nil
}

// DeleteBranch deletes a branch after a merge request is merged or closed.
// Used for cleanup of feature branches created by tenant-api.
func (c *Client) DeleteBranch(branchName string) error {
	encodedBranch := url.PathEscape(branchName)
	_, err := c.doRequest("DELETE",
		fmt.Sprintf("/api/v4/projects/%s/repository/branches/%s", c.projectAPI(), encodedBranch), nil)
	if err != nil {
		return fmt.Errorf("delete branch: %w", err)
	}
	return nil
}

// normalizeState maps provider-specific MR states to platform-neutral values.
// GitLab uses "opened" while GitHub uses "open". We normalize to "open".
func normalizeState(state string) string {
	if state == "opened" {
		return "open"
	}
	return state
}

// doRequest performs an authenticated GitLab API request.
func (c *Client) doRequest(method, path string, body interface{}) ([]byte, error) {
	var bodyReader io.Reader
	if body != nil {
		jsonBody, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(jsonBody)
	}

	reqURL := c.baseURL + path
	req, err := http.NewRequest(method, reqURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("PRIVATE-TOKEN", c.token)
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
		// This prevents leaking internal GitLab error details to API consumers.
		log.Printf("WARN: GitLab API %s %s returned %d: %s", method, path, resp.StatusCode, string(respBody))
		return nil, fmt.Errorf("GitLab API %s %s returned %d", method, path, resp.StatusCode)
	}
	return respBody, nil
}
