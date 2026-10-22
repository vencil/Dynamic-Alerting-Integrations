package handler

// #3175: a git config naming a filter / diff / merge driver refuses every
// write and preview. The answer is the server's: a fixed text (500
// GIT_DRIVER_CONFIGURED for the write and /diff since #3226 — a generic
// INTERNAL_ERROR before — valid: false for /validate), the scope and key
// only in the log. Which keys and scopes count is pinned in gitops. A
// config that cannot be read to check it is not that refusal (#3226): 503,
// retry.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
)

func TestGitDriverConfig_WriteAndPreviewAnswers(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	if out, err := exec.Command("git", "-C", dir, "config", "filter.x.clean", "cat").CombinedOutput(); err != nil {
		t.Fatalf("git config: %v\n%s", err, out)
	}
	head := gitHead(t, dir)
	before, err := os.ReadFile(filepath.Join(dir, "t-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
	const body = "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"

	noKey := func(what string, w *httptest.ResponseRecorder) {
		t.Helper()
		if strings.Contains(w.Body.String(), "filter.x") {
			t.Errorf("%s body names the key: %s", what, w.Body.String())
		}
	}
	for name, w := range map[string]*httptest.ResponseRecorder{
		"PUT":   wmPut(d, body, ""),
		"/diff": wmServe(d, DiffTenant(d), newRequestWithChiParam("POST", "/api/v1/tenants/t-a/diff", "id", "t-a", bytes.NewBufferString(body))),
	} {
		if w.Code != http.StatusInternalServerError {
			t.Errorf("%s status = %d, want 500; body: %s", name, w.Code, w.Body.String())
			continue
		}
		var env ErrorResponse
		if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
			t.Fatal(err)
		}
		if env.Code != errcode.GitDriverConfigured || env.Error != msgGitDriverConfigured {
			t.Errorf("%s envelope = %+v, want %s with msgGitDriverConfigured", name, env, errcode.GitDriverConfigured)
		}
		if ra := w.Header().Get("Retry-After"); ra != "" {
			t.Errorf("%s Retry-After = %q: an operator must remove the key, a retry cannot pass", name, ra)
		}
		noKey(name, w)
	}

	vw := wmServe(d, ValidateTenant(d), newRequestWithChiParam("POST", "/api/v1/tenants/t-a/validate", "id", "t-a", bytes.NewBufferString(body)))
	var vr ValidateResponse
	if err := json.Unmarshal(vw.Body.Bytes(), &vr); err != nil {
		t.Fatalf("/validate: %v; body: %s", err, vw.Body.String())
	}
	if vr.Valid || len(vr.Warnings) != 1 || vr.Warnings[0] != msgGitDriverConfigured {
		t.Errorf("/validate = %+v, want valid: false with msgGitDriverConfigured", vr)
	}
	noKey("/validate", vw)

	bw := wmBatch(d, "")
	if bw.Code != http.StatusInternalServerError || !strings.Contains(bw.Body.String(), string(errcode.GitDriverConfigured)) {
		t.Errorf("batch = %d %s, want 500 %s", bw.Code, bw.Body.String(), errcode.GitDriverConfigured)
	}
	noKey("batch", bw)

	if gitHead(t, dir) != head {
		t.Error("HEAD moved")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml")); string(got) != string(before) {
		t.Errorf("t-a.yaml changed: %q", got)
	}
}

// #3226: a git config git cannot read is refused like a driver — nothing
// can be shown clean — but answered as what it is: 503 with Retry-After,
// a fixed text, never GIT_DRIVER_CONFIGURED (an operator has no key to
// remove, and the next check may read it). The direct write and the batch
// both; nothing is written.
func TestGitConfigUnreadable_WriteAnswers(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	head := gitHead(t, dir)
	before, err := os.ReadFile(filepath.Join(dir, "t-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	// An include git cannot parse: every `git config` read of the repository
	// fails (exit 128), the commands the write would run included.
	bad := filepath.Join(t.TempDir(), "bad.gitconfig")
	if err := os.WriteFile(bad, []byte("[[[ not a config line\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("git", "-C", dir, "config", "include.path", bad).CombinedOutput(); err != nil {
		t.Fatalf("git config: %v\n%s", err, out)
	}
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
	const body = "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"

	pw := wmPut(d, body, "")
	if pw.Code != http.StatusServiceUnavailable {
		t.Fatalf("PUT status = %d, want 503; body: %s", pw.Code, pw.Body.String())
	}
	var env ErrorResponse
	if err := json.Unmarshal(pw.Body.Bytes(), &env); err != nil {
		t.Fatal(err)
	}
	if env.Code != errcode.Upstream || env.Error != msgGitConfigUnreadable || env.RetryAfterS != 5 {
		t.Errorf("PUT envelope = %+v, want %s, msgGitConfigUnreadable, retry_after_s 5", env, errcode.Upstream)
	}
	if ra := pw.Header().Get("Retry-After"); ra != "5" {
		t.Errorf("PUT Retry-After = %q, want 5", ra)
	}
	if strings.Contains(pw.Body.String(), bad) {
		t.Errorf("PUT body names the config file: %s", pw.Body.String())
	}

	bw := wmBatch(d, "")
	if bw.Code != http.StatusServiceUnavailable || strings.Contains(bw.Body.String(), string(errcode.GitDriverConfigured)) {
		t.Errorf("batch = %d %s, want 503, not %s", bw.Code, bw.Body.String(), errcode.GitDriverConfigured)
	}

	// Readable again, so the checks below can run git.
	if err := os.WriteFile(bad, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if gitHead(t, dir) != head {
		t.Error("HEAD moved")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml")); string(got) != string(before) {
		t.Errorf("t-a.yaml changed: %q", got)
	}
}
