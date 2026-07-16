package batchpr

// Tests for the gh-shell-out PRClient impl. We don't actually
// invoke `gh` here — instead inject a stub cmdRunner that records
// args. Two responsibilities to cover:
//
//   1. Argument construction matches `gh` CLI conventions
//      (`pr create`, `pr list --json ...`, `pr edit`).
//   2. Output parsing: lastPRURL + prNumberFromURL handle the
//      shapes `gh pr create` actually emits in the wild.

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// stubRunner records every (dir, name, args) call and replays a
// scripted response based on the last positional arg matching a
// key. Tests prepare the responses map then assert call args.
type stubRunner struct {
	calls     []stubCall
	responses map[string]stubResponse // key = first arg after `pr`
}

type stubCall struct {
	dir  string
	name string
	args []string
}

type stubResponse struct {
	stdout string
	err    error
}

func (s *stubRunner) run(_ context.Context, dir, name string, args ...string) (string, error) {
	s.calls = append(s.calls, stubCall{dir: dir, name: name, args: append([]string(nil), args...)})
	if len(args) >= 2 {
		key := args[0] + " " + args[1] // "pr create" / "pr list" / "pr edit"
		if r, ok := s.responses[key]; ok {
			return r.stdout, r.err
		}
	}
	return "", nil
}

func newStubRunner() *stubRunner {
	return &stubRunner{responses: map[string]stubResponse{}}
}

func TestGHPRClient_OpenPR_ParsesURL(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr create"] = stubResponse{
		stdout: "Creating pull request for da-tools/c10/base-abc into main in vencil/Dynamic-Alerting-Integrations\n\nhttps://github.com/vencil/Dynamic-Alerting-Integrations/pull/130\n",
	}
	c := &GHPRClient{Repo: Repo{Owner: "vencil", Name: "Dynamic-Alerting-Integrations", BaseBranch: "main"}, run: stub}
	got, err := c.OpenPR(context.Background(), OpenPRInput{
		Title: "[Base] Import",
		Body:  "Body",
		Head:  "da-tools/c10/base-abc",
		Base:  "main",
	})
	if err != nil {
		t.Fatalf("OpenPR: %v", err)
	}
	if got.Number != 130 {
		t.Errorf("Number = %d, want 130", got.Number)
	}
	if !strings.HasSuffix(got.URL, "/pull/130") {
		t.Errorf("URL = %q", got.URL)
	}
	// Verify args.
	if len(stub.calls) != 1 {
		t.Fatalf("calls = %d, want 1", len(stub.calls))
	}
	args := stub.calls[0].args
	wantContains := []string{"pr", "create", "--repo", "vencil/Dynamic-Alerting-Integrations", "--head", "da-tools/c10/base-abc", "--base", "main"}
	for _, w := range wantContains {
		if !contains(args, w) {
			t.Errorf("args missing %q: %v", w, args)
		}
	}
}

func TestGHPRClient_OpenPR_NoURLInOutput_Errors(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr create"] = stubResponse{stdout: "no URL printed at all"}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	_, err := c.OpenPR(context.Background(), OpenPRInput{Head: "h", Base: "main"})
	if err == nil || !strings.Contains(err.Error(), "could not parse PR URL") {
		t.Errorf("err = %v, want parse-PR-URL", err)
	}
}

func TestGHPRClient_OpenPR_RunErrorPropagates(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr create"] = stubResponse{err: errors.New("simulated")}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	_, err := c.OpenPR(context.Background(), OpenPRInput{Head: "h", Base: "main"})
	if err == nil || !strings.Contains(err.Error(), "simulated") {
		t.Errorf("err = %v", err)
	}
}

func TestGHPRClient_FindPRByBranch_HappyPath(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	rows := []map[string]any{{"number": 42, "url": "https://github.com/o/r/pull/42"}}
	body, _ := json.Marshal(rows)
	stub.responses["pr list"] = stubResponse{stdout: string(body)}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	got, err := c.FindPRByBranch(context.Background(), "feat/x")
	if err != nil {
		t.Fatalf("FindPRByBranch: %v", err)
	}
	if got == nil || got.Number != 42 {
		t.Errorf("got = %+v, want number=42", got)
	}
}

func TestGHPRClient_FindPRByBranch_EmptyArrayReturnsNilNil(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr list"] = stubResponse{stdout: "[]"}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	got, err := c.FindPRByBranch(context.Background(), "feat/x")
	if err != nil || got != nil {
		t.Errorf("got = (%+v, %v), want (nil, nil)", got, err)
	}
}

func TestGHPRClient_FindPRByBranch_ParseError(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr list"] = stubResponse{stdout: "not-json"}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	_, err := c.FindPRByBranch(context.Background(), "feat/x")
	if err == nil || !strings.Contains(err.Error(), "parse JSON") {
		t.Errorf("err = %v", err)
	}
}

func TestGHPRClient_UpdatePRDescription(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	if err := c.UpdatePRDescription(context.Background(), 7, "new body"); err != nil {
		t.Fatalf("UpdatePRDescription: %v", err)
	}
	if len(stub.calls) != 1 {
		t.Fatalf("calls = %d", len(stub.calls))
	}
	args := stub.calls[0].args
	for _, w := range []string{"pr", "edit", "7", "--repo", "o/r", "--body", "new body"} {
		if !contains(args, w) {
			t.Errorf("args missing %q: %v", w, args)
		}
	}
}

func TestGHPRClient_MissingRepoErrors(t *testing.T) {
	t.Parallel()
	c := &GHPRClient{Repo: Repo{}, run: newStubRunner()}
	if _, err := c.OpenPR(context.Background(), OpenPRInput{}); err == nil {
		t.Errorf("OpenPR with missing repo should error")
	}
	if _, err := c.FindPRByBranch(context.Background(), "x"); err == nil {
		t.Errorf("FindPRByBranch with missing repo should error")
	}
	if err := c.UpdatePRDescription(context.Background(), 1, "x"); err == nil {
		t.Errorf("UpdatePRDescription with missing repo should error")
	}
}

func TestLastPRURL(t *testing.T) {
	t.Parallel()
	cases := []struct {
		in, want string
	}{
		{"", ""},
		{"https://github.com/o/r/pull/42\n", "https://github.com/o/r/pull/42"},
		{"prefix\nhttps://github.com/o/r/pull/42\n", "https://github.com/o/r/pull/42"},
		// Multiple URLs — last wins.
		{"first https://github.com/o/r/pull/1\nhttps://github.com/o/r/pull/9\n", "https://github.com/o/r/pull/9"},
		// URL not at start of line: skipped (we want the bare line).
		{"some text https://github.com/o/r/pull/3\n", ""},
	}
	for _, c := range cases {
		if got := lastPRURL(c.in); got != c.want {
			t.Errorf("lastPRURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestPRNumberFromURL(t *testing.T) {
	t.Parallel()
	cases := []struct {
		url    string
		want   int
		expErr bool
	}{
		{"https://github.com/o/r/pull/42", 42, false},
		{"https://github.com/o/r/pull/12345", 12345, false},
		{"https://github.com/o/r/pull/42/files", 42, false},
		{"https://github.com/o/r/pull/42?q=foo", 42, false},
		{"https://github.com/o/r", 0, true},
		{"https://github.com/o/r/pull/", 0, true},
		{"random text", 0, true},
	}
	for _, c := range cases {
		t.Run(c.url, func(t *testing.T) {
			got, err := prNumberFromURL(c.url)
			if c.expErr {
				if err == nil {
					t.Errorf("expected error for %q; got %d", c.url, got)
				}
				return
			}
			if err != nil {
				t.Errorf("unexpected err for %q: %v", c.url, err)
			}
			if got != c.want {
				t.Errorf("prNumberFromURL(%q) = %d, want %d", c.url, got, c.want)
			}
		})
	}
}

// --- GetPR: gh pr view + state normalisation ----------------------------
//
// pr_gh.go:144-172. The MERGED/CLOSED distinction is what lets Refresh()
// tell "landed" from "abandoned", so it gets its own assertions.

func TestGHPRClient_GetPR_HappyPath(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr view"] = stubResponse{
		stdout: `{"number":77,"state":"MERGED","headRefName":"da-tools/c10/t-abc","url":"https://github.com/o/r/pull/77"}` + "\n",
	}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}

	got, err := c.GetPR(context.Background(), 77)
	if err != nil {
		t.Fatalf("GetPR: %v", err)
	}
	if got.Number != 77 || got.State != PRStateMerged || got.HeadBranch != "da-tools/c10/t-abc" {
		t.Errorf("GetPR = %+v, want number=77 state=merged head=da-tools/c10/t-abc", got)
	}
	if len(stub.calls) != 1 {
		t.Fatalf("calls = %d, want 1", len(stub.calls))
	}
	args := stub.calls[0].args
	for _, w := range []string{"pr", "view", "77", "--repo", "o/r", "--json", "number,state,headRefName,url"} {
		if !contains(args, w) {
			t.Errorf("args missing %q: %v", w, args)
		}
	}
}

func TestGHPRClient_GetPR_RunErrorPropagates(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr view"] = stubResponse{err: errors.New("gh: Not Found (HTTP 404)")}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	_, err := c.GetPR(context.Background(), 9)
	if err == nil || !strings.Contains(err.Error(), "gh pr view 9") {
		t.Errorf("err = %v, want wrapped gh-pr-view failure", err)
	}
}

func TestGHPRClient_GetPR_ParseError(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["pr view"] = stubResponse{stdout: "not-json"}
	c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
	_, err := c.GetPR(context.Background(), 9)
	if err == nil || !strings.Contains(err.Error(), "parse JSON") {
		t.Errorf("err = %v, want parse-JSON failure", err)
	}
}

// --- CommentPR ----------------------------------------------------------
//
// pr_gh.go:176-189.

func TestGHPRClient_CommentPR(t *testing.T) {
	t.Parallel()

	t.Run("issues_pr_comment", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
		if err := c.CommentPR(context.Background(), 12, "rebase had conflicts"); err != nil {
			t.Fatalf("CommentPR: %v", err)
		}
		if len(stub.calls) != 1 {
			t.Fatalf("calls = %d, want 1", len(stub.calls))
		}
		args := stub.calls[0].args
		for _, w := range []string{"pr", "comment", "12", "--repo", "o/r", "--body", "rebase had conflicts"} {
			if !contains(args, w) {
				t.Errorf("args missing %q: %v", w, args)
			}
		}
	})

	t.Run("run_error_propagates", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["pr comment"] = stubResponse{err: errors.New("gh: rate limited")}
		c := &GHPRClient{Repo: Repo{Owner: "o", Name: "r", BaseBranch: "main"}, run: stub}
		if err := c.CommentPR(context.Background(), 12, "x"); err == nil || !strings.Contains(err.Error(), "gh pr comment 12") {
			t.Errorf("err = %v, want wrapped gh-pr-comment failure", err)
		}
	})
}

// GetPR / CommentPR join the missing-repo guard the other methods
// already test in TestGHPRClient_MissingRepoErrors.
func TestGHPRClient_GetPRCommentPR_MissingRepoErrors(t *testing.T) {
	t.Parallel()
	c := &GHPRClient{Repo: Repo{}, run: newStubRunner()}
	if _, err := c.GetPR(context.Background(), 1); err == nil {
		t.Errorf("GetPR with missing repo should error")
	}
	if err := c.CommentPR(context.Background(), 1, "x"); err == nil {
		t.Errorf("CommentPR with missing repo should error")
	}
}

// --- normalisePRState -----------------------------------------------------
//
// pr_gh.go:195-206. gh emits upper-case; unknown/future states must fall
// through to PRStateUnknown (Refresh()'s conservative skip branch), never
// to a known state.

func TestNormalisePRState(t *testing.T) {
	t.Parallel()
	cases := []struct {
		in   string
		want PRState
	}{
		{"OPEN", PRStateOpen},
		{"CLOSED", PRStateClosed},
		{"MERGED", PRStateMerged},
		// Case / whitespace tolerance.
		{"open", PRStateOpen},
		{" Merged\n", PRStateMerged},
		// Unknown / future / empty → unknown, never a known state.
		{"DRAFT", PRStateUnknown},
		{"", PRStateUnknown},
		{"LOCKED", PRStateUnknown},
	}
	for _, tc := range cases {
		if got := normalisePRState(tc.in); got != tc.want {
			t.Errorf("normalisePRState(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// --- helper ---

func contains(args []string, want string) bool {
	for _, a := range args {
		if a == want {
			return true
		}
	}
	return false
}
