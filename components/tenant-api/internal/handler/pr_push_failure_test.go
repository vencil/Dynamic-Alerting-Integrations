package handler

// #1724 handler mapping. A PR-mode write whose push fails — any failure, a
// timeout included — answers 503 GIT_PUSH_FAILED with Retry-After and a
// fixed text, opens no PR/MR, and frees the tenant's claim so a retry can
// succeed. It used to log a WARN and answer 200 pending_review, after asking
// the forge to open a PR/MR on a head origin did not hold as this write.
//
// And a PR-mode write that a git deadline (or the request's context) stops
// elsewhere answers the cancelled-write 503 the direct path gives (#1535),
// not the generic 500.
//
// The faults are git hooks the repo runs through core.hooksPath: a pre-push
// hook that refuses (exit 1, saying where it ran) or sleeps past the git
// timeout, and a pre-commit hook that sleeps past it.

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// hookRepo is a conf.d git repo on main with db-a committed, an empty bare
// origin, and a hooks directory wired in through core.hooksPath.
type hookRepo struct {
	dir, bare, hooks string
}

func newHookRepo(t *testing.T) *hookRepo {
	t.Helper()
	dir := seedGitTree(t, map[string]string{"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"})
	out, err := exec.Command("git", "-C", dir, "remote", "get-url", "origin").Output()
	if err != nil {
		t.Fatalf("origin: %v", err)
	}
	r := &hookRepo{dir: dir, bare: strings.TrimSpace(string(out)), hooks: t.TempDir()}
	runGit(t, dir, "config", "core.hooksPath", r.hooks)
	return r
}

// hook installs (or, with script "", removes) the named hook.
func (r *hookRepo) hook(t *testing.T, name, script string) {
	t.Helper()
	p := filepath.Join(r.hooks, name)
	if script == "" {
		if err := os.Remove(p); err != nil {
			t.Fatal(err)
		}
		return
	}
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+script+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
}

// refusingPrePush rejects every push and prints what an operator would want
// kept out of a client response: the remote it was pushing to.
const refusingPrePush = `echo "hook refused push to $2 (secret-remote-detail)" >&2; exit 1`

// prFixture is PR-mode Deps over r with a counting forge and a claim tracker.
type prFixture struct {
	deps    *Deps
	tracker *mockPlatformTracker
	prs     int
}

func newPRFixture(t *testing.T, r *hookRepo) *prFixture {
	t.Helper()
	f := &prFixture{tracker: &mockPlatformTracker{}}
	f.deps = &Deps{
		Writer: newTestWriter(r.dir), ConfigDir: r.dir, RBAC: adminRBAC(t), WriteMode: WriteModePR,
		PRTracker: f.tracker,
		PRClient: &mockPlatformClient{providerName: "github",
			createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
				f.prs++
				return &platform.PRInfo{Number: 7, WebURL: "https://example.invalid/pr/7", State: "open", HeadRef: head}, nil
			}},
	}
	return f
}

func (f *prFixture) put(t *testing.T, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	rec := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(f.deps), f.deps.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(rec, req)
	return rec
}

func (f *prFixture) batch(t *testing.T, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	rec := httptest.NewRecorder()
	f.deps.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(f.deps)).ServeHTTP(rec, req)
	return rec
}

// assertPushFailed503 checks the GIT_PUSH_FAILED envelope, that it carries
// nothing of the remote or of git's output, and that the repo is left with
// no local PR branch and the serving tree clean on main.
func assertPushFailed503(t *testing.T, rec *httptest.ResponseRecorder, r *hookRepo) {
	t.Helper()
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503; body: %s", rec.Code, rec.Body.String())
	}
	if got := rec.Header().Get("Retry-After"); got != "5" {
		t.Errorf("Retry-After = %q, want 5", got)
	}
	var env ErrorResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v; body: %s", err, rec.Body.String())
	}
	if env.Code != errcode.GitPushFailed || env.Error != msgGitPushFailed || env.RetryAfterS != 5 {
		t.Errorf("envelope = %+v, want code %s, error %q, retry_after_s 5", env, errcode.GitPushFailed, msgGitPushFailed)
	}
	body := rec.Body.String()
	for _, leak := range []string{r.bare, r.dir, "secret-remote-detail", "tenant-api/", "pending_review", "results"} {
		if strings.Contains(body, leak) {
			t.Errorf("body carries %q: %s", leak, body)
		}
	}
	if b, _ := exec.Command("git", "-C", r.dir, "branch", "--list", "tenant-api/*").Output(); len(strings.TrimSpace(string(b))) != 0 {
		t.Errorf("local PR branch left after the failed push:\n%s", b)
	}
	if st := strings.TrimSpace(gitOut(t, r.dir, "status", "--porcelain")); st != "" {
		t.Errorf("serving tree dirty:\n%s", st)
	}
	if cur := strings.TrimSpace(gitOut(t, r.dir, "rev-parse", "--abbrev-ref", "HEAD")); cur != "main" {
		t.Errorf("HEAD on %q, want main", cur)
	}
}

func TestPutTenant_PRMode_PushFailureIs503(t *testing.T) {
	t.Parallel()
	r := newHookRepo(t)
	r.hook(t, "pre-push", refusingPrePush)
	f := newPRFixture(t, r)

	assertPushFailed503(t, f.put(t, "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"), r)
	if f.prs != 0 {
		t.Errorf("PRs opened = %d, want 0 after a failed push", f.prs)
	}
	if f.tracker.claimed["db-a"] {
		t.Error("db-a's claim was not released after the failed push")
	}

	// The retry, once the remote takes pushes again, succeeds.
	r.hook(t, "pre-push", "")
	rec := f.put(t, "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "pending_review") {
		t.Fatalf("retry = %d %s, want 200 pending_review", rec.Code, rec.Body.String())
	}
	if f.prs != 1 {
		t.Errorf("PRs opened = %d, want 1 after the retry", f.prs)
	}
}

func TestBatchTenants_PRMode_PushFailureIs503(t *testing.T) {
	t.Parallel()
	r := newHookRepo(t)
	r.hook(t, "pre-push", refusingPrePush)
	f := newPRFixture(t, r)
	const body = `{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"critical"}}]}`

	assertPushFailed503(t, f.batch(t, body), r)
	if f.prs != 0 {
		t.Errorf("PRs opened = %d, want 0 after a failed push", f.prs)
	}
	if wt := strings.TrimSpace(gitOut(t, r.dir, "worktree", "list", "--porcelain")); strings.Count(wt, "worktree ") != 1 {
		t.Errorf("staging worktree left behind:\n%s", wt)
	}

	r.hook(t, "pre-push", "")
	rec := f.batch(t, body)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "pending_review") {
		t.Fatalf("retry = %d %s, want 200 pending_review", rec.Code, rec.Body.String())
	}
	if f.prs != 1 {
		t.Errorf("PRs opened = %d, want 1 after the retry", f.prs)
	}
}

// A push that hits the git deadline is GIT_PUSH_FAILED, not the generic
// cancelled-write 503: the deadline is the push's.
//
// Not parallel: TENANT_API_GIT_TIMEOUT is process-global (t.Setenv).
func TestPutTenant_PRMode_PushTimeoutIs503PushFailed(t *testing.T) {
	t.Setenv("TENANT_API_GIT_TIMEOUT", "2s")
	r := newHookRepo(t)
	r.hook(t, "pre-push", "exec sleep 30")
	f := newPRFixture(t, r)

	assertPushFailed503(t, f.put(t, "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"), r)
	if f.prs != 0 {
		t.Errorf("PRs opened = %d, want 0", f.prs)
	}
}

// A git command other than the push that hits its deadline on the PR path
// is the cancelled-write 503 (msgWriteCancelled), as on the direct path —
// not the generic 500 it used to be.
//
// Not parallel: TENANT_API_GIT_TIMEOUT is process-global (t.Setenv).
func TestPRMode_GitTimeoutIs503(t *testing.T) {
	t.Setenv("TENANT_API_GIT_TIMEOUT", "2s")
	for _, c := range []struct {
		name string
		run  func(*prFixture, *testing.T) *httptest.ResponseRecorder
	}{
		{"PUT", func(f *prFixture, t *testing.T) *httptest.ResponseRecorder {
			return f.put(t, "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
		}},
		{"batch", func(f *prFixture, t *testing.T) *httptest.ResponseRecorder {
			return f.batch(t, `{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"critical"}}]}`)
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := newHookRepo(t)
			r.hook(t, "pre-commit", "exec sleep 30")
			f := newPRFixture(t, r)
			rec := c.run(f, t)
			if rec.Code != http.StatusServiceUnavailable {
				t.Fatalf("status = %d, want 503; body: %s", rec.Code, rec.Body.String())
			}
			var env ErrorResponse
			if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
				t.Fatal(err)
			}
			if env.Error != msgWriteCancelled {
				t.Errorf("error = %q, want %q", env.Error, msgWriteCancelled)
			}
			if f.prs != 0 {
				t.Errorf("PRs opened = %d, want 0", f.prs)
			}
		})
	}
}

// A PR-mode write whose request is already cancelled when it reaches the
// writer is the same cancelled-write 503, for PUT and batch alike.
func TestPRMode_CancelledRequestIs503(t *testing.T) {
	t.Parallel()
	for _, batch := range []bool{false, true} {
		r := newHookRepo(t)
		f := newPRFixture(t, r)
		var req *http.Request
		if batch {
			req = httptest.NewRequest("POST", "/api/v1/tenants/batch",
				bytes.NewBufferString(`{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"critical"}}]}`))
			req.Header.Set("Content-Type", "application/json")
		} else {
			req = newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
				bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"))
		}
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
		ctx, cancel := context.WithCancel(req.Context())
		cancel()
		req = req.WithContext(ctx)
		rec := httptest.NewRecorder()
		if batch {
			f.deps.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(f.deps)).ServeHTTP(rec, req)
		} else {
			wrapWithRBACMiddleware(PutTenant(f.deps), f.deps.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(rec, req)
		}
		var env ErrorResponse
		_ = json.Unmarshal(rec.Body.Bytes(), &env)
		if rec.Code != http.StatusServiceUnavailable || env.Error != msgWriteCancelled {
			t.Errorf("batch=%t: %d %s, want 503 %q", batch, rec.Code, rec.Body.String(), msgWriteCancelled)
		}
		if f.prs != 0 {
			t.Errorf("batch=%t: PRs opened = %d, want 0", batch, f.prs)
		}
	}
}
