package handler

// #2913 — a tenant document whose mapping holds a non-scalar key (`? [a]`,
// `? {k: v}`) beside a merge key `<<`. yaml.v3's typed decode panicked on it
// ("hash of unhashable type"). A PUT body of that shape reached the typed
// decode in validateShape; a batch patch reached it through the tenant's
// current file (checkPartialWriteBase → ParseTenantFile), and in an async
// batch that ran on a task-manager worker with no recover, so tenant-api
// died. The vendored yaml.v3 now answers an error, so both are ordinary
// refusals: PUT answers 400 "invalid YAML", a batch op answers its
// TENANT_CONFIG_NOT_LOADABLE error, and nothing is written.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

const unhashableTenant = "t-2913"

// unhashableMergeKeyDocs are the three shapes measured on #2913.
var unhashableMergeKeyDocs = map[string]string{
	"sequence key, then merge of an alias": "tenants:\n  " + unhashableTenant + ":\n    base: &b {x: 1}\n    ? [a]\n    : 1\n    <<: *b\n",
	"sequence key, then merge of a list":   "tenants:\n  " + unhashableTenant + ":\n    ? [a]\n    : 1\n    <<: &y [{x: 1}]\n",
	"merge, then mapping key":              "tenants:\n  " + unhashableTenant + ":\n    <<: {x: 1}\n    ? {k: v}\n    : 1\n",
}

// failOnPanic runs f and turns a panic in it into a t.Fatalf of the calling
// test. Without it, a regression (the vendored yaml.v3 patch gone) panics the
// test goroutine and ends the whole test binary, so every test in the package
// reports nothing instead of this one failing and naming the shape.
func failOnPanic(t *testing.T, what string, f func()) {
	t.Helper()
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("%s panicked: %v", what, r)
		}
	}()
	f()
}

// The whole-file PUT, in both write modes (putReceiverDoc), and once more in
// direct mode with a domain policy present, so the policy's own reading of
// the body (judgePutBody) is on the path too.
func TestPutTenant_UnhashableMergeKeyIsInvalidYAML(t *testing.T) {
	for name, doc := range unhashableMergeKeyDocs {
		for _, pr := range []bool{false, true} {
			var code int
			var resp, written string
			failOnPanic(t, fmt.Sprintf("%s (pr=%v) PUT", name, pr), func() {
				code, resp, written = putReceiverDoc(t, doc, pr)
			})
			if code != http.StatusBadRequest || written != "" || !strings.Contains(resp, "invalid YAML") {
				t.Errorf("%s (pr=%v): status %d, written %q; want 400 invalid YAML, no write; body: %s",
					name, pr, code, written, resp)
			}
		}

		dir := seedGitTree(t, map[string]string{
			"_defaults.yaml":      "defaults:\n  cpu_usage_percent: 80\n",
			"_domain_policy.yaml": policyListing(unhashableTenant),
		})
		rb := adminRBAC(t)
		d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rb, WriteMode: WriteModeDirect,
			Policy: policy.NewManager(dir)}
		req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+unhashableTenant, "id", unhashableTenant,
			bytes.NewBufferString(doc))
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
		w := httptest.NewRecorder()
		failOnPanic(t, name+" (domain policy present) PUT", func() {
			wrapWithRBACMiddleware(PutTenant(d), rb, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
		})
		if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "invalid YAML") {
			t.Errorf("%s (domain policy present): status %d; want 400 invalid YAML; body: %s", name, w.Code, w.Body.String())
		}
		if _, err := os.Stat(filepath.Join(dir, unhashableTenant+".yaml")); !os.IsNotExist(err) {
			t.Errorf("%s (domain policy present): the tenant file was written (stat err %v)", name, err)
		}
	}
}

// A batch patch on a tenant whose CURRENT file has the shape (written out of
// band — the API refuses it, see above): sync and async alike, the op is
// refused with TENANT_CONFIG_NOT_LOADABLE and the file is left alone; the
// async task finishes (completed, with the op's error), it does not take the
// worker down.
func TestBatchTenants_UnhashableMergeKeyInCurrentFileIsRefused(t *testing.T) {
	const reqBody = `{"operations":[{"tenant_id":"` + unhashableTenant + `","patch":{"_silent_mode":"warning"}}]}`
	for name, doc := range unhashableMergeKeyDocs {
		for _, async_ := range []bool{false, true} {
			dir := seedGitTree(t, map[string]string{
				"_defaults.yaml":           "defaults:\n  cpu_usage_percent: 80\n",
				unhashableTenant + ".yaml": doc,
			})
			rb := adminRBAC(t)
			mgr := async.NewManager(1)
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rb, WriteMode: WriteModeDirect, Tasks: mgr}
			path := "/api/v1/tenants/batch"
			if async_ {
				path += "?async=true"
			}
			req := httptest.NewRequest("POST", path, bytes.NewBufferString(reqBody))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Forwarded-Email", "alice@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			w := httptest.NewRecorder()
			failOnPanic(t, fmt.Sprintf("%s (async=%v) batch", name, async_), func() {
				rb.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
			})

			var status, code, msg string
			if async_ {
				var resp struct {
					TaskID string `json:"task_id"`
				}
				if w.Code != http.StatusAccepted || json.Unmarshal(w.Body.Bytes(), &resp) != nil {
					t.Fatalf("%s async: status %d, body %s", name, w.Code, w.Body.String())
				}
				task := pollUntilTerminal(t, mgr, resp.TaskID, 10*time.Second)
				if task.Status != async.TaskCompleted || len(task.Results) != 1 {
					t.Fatalf("%s async: task %+v, want completed with one result", name, task)
				}
				status, code, msg = task.Results[0].Status, task.Results[0].Code, task.Results[0].Message
			} else {
				var resp BatchResponse
				if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &resp) != nil || len(resp.Results) != 1 {
					t.Fatalf("%s sync: status %d, body %s", name, w.Code, w.Body.String())
				}
				status, code, msg = resp.Results[0].Status, resp.Results[0].Code, resp.Results[0].Message
			}
			if status != "error" || code != CodeTenantConfigNotLoadable {
				t.Errorf("%s (async=%v): result %s/%s %q, want error/%s", name, async_, status, code, msg, CodeTenantConfigNotLoadable)
			}
			if got, _ := os.ReadFile(filepath.Join(dir, unhashableTenant+".yaml")); string(got) != doc {
				t.Errorf("%s (async=%v): the current file changed:\n%s", name, async_, got)
			}
			_ = mgr.Close()
		}
	}
}
