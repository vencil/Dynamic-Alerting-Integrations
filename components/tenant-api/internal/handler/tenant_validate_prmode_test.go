package handler

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
)

// TestValidateTenant_PRMode_VerdictMatchesWritePR pins the PR-mode half of
// #2124: in PR mode the dry-run must answer what WritePR answers BEFORE it has
// the fresh base — its body-only pre-flight — and not judge the local tree,
// which in PR mode is only synced at pod start and may lag the base (#1718).
//
// The stale-tree arm is the reviewer's reproduction: at "pod start" the tenant
// lives in shared.yaml; the remote then moves it to its own file. Judged on the
// local tree, the tenant is "declared elsewhere" and the dry-run said invalid —
// while WritePR, which resolves on the fresh base, accepts. That verdict never
// self-heals until the pod restarts.
func TestValidateTenant_PRMode_VerdictMatchesWritePR(t *testing.T) {
	t.Parallel()
	const tid = "parity-pr"
	body := func(v string) string { return "tenants:\n  " + tid + ":\n" + v }

	cases := []struct {
		name      string
		body      string
		wantValid bool
	}{
		{name: "stale_local_tree_moved_remotely", body: body("    _silent_mode: \"warning\"\n"), wantValid: true},
		{name: "duplicate_key_body_only_error", body: body("    _silent_mode: \"warning\"\n    _silent_mode: \"critical\"\n")},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			git := func(dir string, args ...string) {
				t.Helper()
				cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
				if out, err := cmd.CombinedOutput(); err != nil {
					t.Skipf("git %v: %v\n%s", args, err, out)
				}
			}
			write := func(dir, name, content string) {
				t.Helper()
				if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			remote := t.TempDir()
			git(remote, "init", "--bare", "-b", "main")

			author := t.TempDir()
			git(author, "clone", remote, ".")
			git(author, "config", "user.email", "a@a.com")
			git(author, "config", "user.name", "A")
			write(author, "shared.yaml", body("    _silent_mode: \"false\"\n"))
			git(author, "add", "-A")
			git(author, "commit", "-m", "seed: tenant lives in shared.yaml")
			git(author, "push", "origin", "main")

			// The pod's clone, taken at "startup" and never pulled again.
			dir := t.TempDir()
			git(dir, "clone", remote, ".")
			git(dir, "config", "user.email", "t@t.com")
			git(dir, "config", "user.name", "T")

			// The remote moves the tenant into its own file.
			write(author, "shared.yaml", "tenants: {}\n")
			write(author, tid+".yaml", body("    _silent_mode: \"false\"\n"))
			git(author, "add", "-A")
			git(author, "commit", "-m", "move tenant to its own file")
			git(author, "push", "origin", "main")

			wr := newTestWriter(dir)
			d := &Deps{Writer: wr, ConfigDir: dir, WriteMode: WriteModePR,
				PRClient: &mockPlatformClient{}, PRTracker: &mockPlatformTracker{}}
			rec := httptest.NewRecorder()
			ValidateTenant(d)(rec, newRequestWithChiParam("POST",
				"/api/v1/tenants/"+tid+"/validate", "id", tid, bytes.NewBufferString(c.body)))
			if rec.Code != http.StatusOK {
				t.Fatalf("validate status = %d, body: %s", rec.Code, rec.Body.String())
			}
			var vresp ValidateResponse
			if err := json.Unmarshal(rec.Body.Bytes(), &vresp); err != nil {
				t.Fatal(err)
			}

			_, werr := wr.WritePR(context.Background(), tid, "op@example.com", c.body)
			if vresp.Valid != (werr == nil) {
				t.Fatalf("PR-mode dry-run and WritePR disagree: validate.valid=%v warnings=%q, WritePR err=%v",
					vresp.Valid, vresp.Warnings, werr)
			}
			if vresp.Valid != c.wantValid {
				t.Fatalf("validate.valid = %v, want %v (warnings=%q)", vresp.Valid, c.wantValid, vresp.Warnings)
			}
		})
	}
}

// TestValidateTenant_ResolverErrorCarriesNoServerPath: the route needs only read
// permission, so a pre-validation failure other than the reserved-id and
// ambiguous-file refusals must not echo the error's text — the resolver's
// ReadDir error names the absolute configDir.
func TestValidateTenant_ResolverErrorCarriesNoServerPath(t *testing.T) {
	t.Parallel()
	// A regular file where the conf.d dir should be: the resolver's ReadDir
	// fails with ENOTDIR (root in CI ignores permission bits, so an
	// unreadable dir would not fail at all). A MISSING dir is not used: the
	// resolver treats it as "no file yet" and the tree scan fails instead.
	configDir := filepath.Join(t.TempDir(), "confd-is-a-file")
	if err := os.WriteFile(configDir, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	ValidateTenant(&Deps{ConfigDir: configDir})(rec, newRequestWithChiParam("POST",
		"/api/v1/tenants/some-t/validate", "id", "some-t",
		bytes.NewBufferString("tenants:\n  some-t:\n    _silent_mode: \"false\"\n")))
	if rec.Code != http.StatusOK {
		t.Fatalf("validate status = %d, body: %s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), configDir) {
		t.Fatalf("response leaks the server path %q: %s", configDir, rec.Body.String())
	}
	var vresp ValidateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &vresp); err != nil {
		t.Fatal(err)
	}
	if len(vresp.Warnings) != 1 || vresp.Warnings[0] != msgTenantFileUnresolved {
		t.Fatalf("want exactly the fixed unresolved-file text, got valid=%v warnings=%q",
			vresp.Valid, vresp.Warnings)
	}
}
