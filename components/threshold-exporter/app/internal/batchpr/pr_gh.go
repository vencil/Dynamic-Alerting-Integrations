package batchpr

// PR-2 — production PRClient implementation: shell out to `gh`.
//
// Why `gh` (vs go-github or raw HTTP):
//
//   - `gh` is the CLI customers use day-to-day; it shares auth
//     setup with whatever the human reviewer uses (gh auth status
//     / GH_TOKEN env). One auth surface, one mental model.
//
//   - Mirrors the existing pattern in `.github/workflows/release-
//     attach-bench-baseline.yaml` and the C-12 PR-5 guard workflow,
//     both of which shell out to `gh`. Customers running the
//     migration toolkit will already have `gh` available.
//
//   - Avoids pulling in go-github (large surface, transitive deps).
//     If a future caller wants pure-API, the PRClient interface
//     already accommodates it — they can add a sibling impl
//     (e.g. `pr_rest.go`) without touching apply.go.
//
// Auth note: `gh` reads $GH_TOKEN / $GITHUB_TOKEN / its own
// keyring config. The impl does NOT manage auth; callers ensure
// the environment is set up before constructing this client.

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

// GHPRClient is the production PRClient using `gh pr` subcommands.
type GHPRClient struct {
	// Repo identifies the target. Must match ApplyInput.Repo —
	// the impl passes `--repo <owner>/<name>` so `gh` doesn't need
	// the CWD to be inside the repo.
	Repo Repo

	// run is injected for testability (defaults to defaultRunner).
	// Tests substitute a stub that records args without shelling
	// out. nil = use default.
	run cmdRunner
}

// NewGHPRClient constructs a GHPRClient for the supplied repo.
func NewGHPRClient(repo Repo) *GHPRClient {
	return &GHPRClient{Repo: repo}
}

// OpenPR implements PRClient. Issues
// `gh pr create --repo owner/name --title T --body B --head H --base B`.
//
// `gh pr create` prints the new PR URL to stdout on success. We
// parse the trailing `https://github.com/.../pull/<num>` line to
// recover the PR number — `gh` doesn't expose `--json` on `create`
// directly, so the URL parse is the cleanest stable signal.
func (c *GHPRClient) OpenPR(ctx context.Context, in OpenPRInput) (*PROpened, error) {
	if c.Repo.FullName() == "" {
		return nil, fmt.Errorf("open PR: GHPRClient.Repo missing owner/name")
	}
	args := []string{
		"pr", "create",
		"--repo", c.Repo.FullName(),
		"--title", in.Title,
		"--body", in.Body,
		"--head", in.Head,
		"--base", in.Base,
	}
	out, err := c.runGH(ctx, args...)
	if err != nil {
		return nil, fmt.Errorf("gh pr create: %w", err)
	}
	url := lastPRURL(out)
	if url == "" {
		return nil, fmt.Errorf("gh pr create: could not parse PR URL from output: %q", out)
	}
	num, err := prNumberFromURL(url)
	if err != nil {
		return nil, fmt.Errorf("gh pr create: %w (url=%s)", err, url)
	}
	return &PROpened{Number: num, URL: url}, nil
}

// FindPRByBranch implements PRClient. Issues
// `gh pr list --repo owner/name --head H --state open --json number,url --limit 1`.
//
// `--json` returns a JSON array; an empty array means no matching
// PR (we return (nil, nil) — sentinel for "branch exists but no
// open PR", which Apply() treats as "skip with no recorded PR").
func (c *GHPRClient) FindPRByBranch(ctx context.Context, branch string) (*PROpened, error) {
	if c.Repo.FullName() == "" {
		return nil, fmt.Errorf("find PR: GHPRClient.Repo missing owner/name")
	}
	args := []string{
		"pr", "list",
		"--repo", c.Repo.FullName(),
		"--head", branch,
		"--state", "open",
		"--json", "number,url",
		"--limit", "1",
	}
	out, err := c.runGH(ctx, args...)
	if err != nil {
		return nil, fmt.Errorf("gh pr list: %w", err)
	}
	var rows []struct {
		Number int    `json:"number"`
		URL    string `json:"url"`
	}
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &rows); err != nil {
		return nil, fmt.Errorf("gh pr list: parse JSON: %w (raw: %s)", err, out)
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return &PROpened{Number: rows[0].Number, URL: rows[0].URL}, nil
}

// UpdatePRDescription implements PRClient. Issues
// `gh pr edit <number> --repo owner/name --body <body>`.
func (c *GHPRClient) UpdatePRDescription(ctx context.Context, num int, body string) error {
	if c.Repo.FullName() == "" {
		return fmt.Errorf("update PR: GHPRClient.Repo missing owner/name")
	}
	args := []string{
		"pr", "edit", fmt.Sprintf("%d", num),
		"--repo", c.Repo.FullName(),
		"--body", body,
	}
	if _, err := c.runGH(ctx, args...); err != nil {
		return fmt.Errorf("gh pr edit %d: %w", num, err)
	}
	return nil
}

// runGH wraps the cmdRunner with `gh` as the command name. Working
// directory is irrelevant for `gh pr *` (we always pass `--repo`),
// so we pass `""` as dir — the runner promotes that to "no chdir".
func (c *GHPRClient) runGH(ctx context.Context, args ...string) (string, error) {
	r := c.run
	if r == nil {
		r = defaultRunner{}
	}
	return r.run(ctx, "", "gh", args...)
}

// lastPRURL scans `gh pr create` stdout for the conventional
// `https://github.com/<owner>/<repo>/pull/<num>` line and returns
// the last match (gh writes the new PR URL on its own line).
func lastPRURL(out string) string {
	var found string
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if strings.Contains(line, "/pull/") && strings.HasPrefix(line, "https://") {
			found = line
		}
	}
	return found
}

// prNumberFromURL extracts the trailing `<num>` from a PR URL.
// Accepts `https://github.com/owner/repo/pull/123` and returns 123.
func prNumberFromURL(url string) (int, error) {
	idx := strings.LastIndex(url, "/pull/")
	if idx < 0 {
		return 0, fmt.Errorf("no /pull/ segment in URL %q", url)
	}
	tail := url[idx+len("/pull/"):]
	// `gh` may append `/files`, `?q=...` etc. on some flows; trim
	// at the first non-digit.
	end := len(tail)
	for i, r := range tail {
		if r < '0' || r > '9' {
			end = i
			break
		}
	}
	if end == 0 {
		return 0, fmt.Errorf("URL %q has no numeric PR id after /pull/", url)
	}
	n := 0
	for _, r := range tail[:end] {
		n = n*10 + int(r-'0')
	}
	return n, nil
}

// Compile-time interface assertion: *GHPRClient implements PRClient.
var _ PRClient = (*GHPRClient)(nil)
