package handler

// #3140: a threshold written as a non-finite number is refused by every
// write path with the same 400 and the same reason — PUT /tenants/{id}
// (the parity table's non_finite_threshold rows), POST /tenants/batch and
// POST /groups/{id}/batch, in both write modes. Before, the batch paths
// answered it 200 in direct mode (WriteBatch's validate never ran the
// predicate) and 500 INTERNAL_ERROR in PR mode (the pre-flight's
// ErrValidation fell through runBatchPR to its generic 500).
//
// A value the tenant's file already holds is not the batch's: a batch that
// writes another key goes through, as it did before #3140.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

const nonFiniteGroup = "g-parity"

// batchEndpoint is one of the batch write paths.
type batchEndpoint struct {
	name string
	pr   bool
	// group: POST /groups/{id}/batch over a group of {id} alone; else
	// POST /tenants/batch with one op for {id}.
	group bool
}

var batchEndpoints = []batchEndpoint{
	{name: "tenants_batch/direct"},
	{name: "tenants_batch/pr", pr: true},
	{name: "groups_batch/direct", group: true},
	{name: "groups_batch/pr", pr: true, group: true},
}

// nonFiniteTree is {id}'s own file plus the root defaults that declare
// mysql_connections and a group of {id} alone.
func nonFiniteTree(file string) map[string]string {
	files := map[string]string{parityIn + ".yaml": file,
		"_groups.yaml": "groups:\n  " + nonFiniteGroup + ":\n    label: Parity\n    members: [" + parityIn + "]\n"}
	for k, v := range parityThresholdDefaults {
		files[k] = v
	}
	return files
}

// serveBatch sends patch for {id} to ep on a fresh git-backed copy of files
// and returns the answer and whether the serving branch moved.
func serveBatch(t *testing.T, files map[string]string, ep batchEndpoint, patch map[string]string) (*httptest.ResponseRecorder, bool) {
	t.Helper()
	dir := setupConfigDir(t, files)
	initGitRepo(t, dir)
	if out, err := exec.Command("git", "-C", dir, "branch", "-M", "main").CombinedOutput(); err != nil {
		t.Fatalf("git branch -M main: %v\n%s", err, out)
	}
	addBareOrigin(t, dir)
	before := gitHead(t, dir)
	mgr := newRBACManager(t, parityRBAC)
	d := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: mgr,
		Groups: groups.NewDirManager(dir), WriteMode: WriteModeDirect}
	if ep.pr {
		d.WriteMode, d.PRClient, d.PRTracker = WriteModePR, &mockPlatformClient{}, &mockPlatformTracker{}
	}
	p, err := json.Marshal(patch)
	if err != nil {
		t.Fatal(err)
	}
	var req *http.Request
	var h http.HandlerFunc
	if ep.group {
		req = newRequestWithChiParam("POST", "/api/v1/groups/"+nonFiniteGroup+"/batch", "id", nonFiniteGroup,
			bytes.NewBufferString(`{"patch":`+string(p)+`}`))
		h = GroupBatch(d)
	} else {
		req = httptest.NewRequest("POST", "/api/v1/tenants/batch",
			bytes.NewBufferString(`{"operations":[{"tenant_id":"`+parityIn+`","patch":`+string(p)+`}]}`))
		h = BatchTenants(d)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "parity@example.com")
	// The batch routes ask read on every tenant ("*") before the handler.
	req.Header.Set("X-Forwarded-Groups", "platform")
	w := httptest.NewRecorder()
	mgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)
	return w, gitHead(t, dir) != before
}

// batchViolationReason is the reason of the one violation a 400 names.
func batchViolationReason(t *testing.T, w *httptest.ResponseRecorder) string {
	t.Helper()
	var env struct {
		Code       string      `json:"code"`
		Violations []Violation `json:"violations"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil || len(env.Violations) != 1 {
		t.Fatalf("400 body %s: want one violation (%v)", w.Body.String(), err)
	}
	if !strings.HasSuffix(env.Violations[0].Field, `["mysql_connections"]`) {
		t.Errorf("violation field = %q, want the patch key", env.Violations[0].Field)
	}
	return env.Violations[0].Reason
}

func TestNonFiniteThreshold_BatchPathsRefuseAsPut(t *testing.T) {
	t.Parallel()
	own := "tenants:\n" + paritySec(parityIn, "warning")
	for _, value := range []string{"NaN", "-Inf:critical", "+Inf", "infinity"} {
		for _, ep := range batchEndpoints {
			t.Run(value+"/"+ep.name, func(t *testing.T) {
				t.Parallel()
				// PUT's answer for the same value, on the same tree.
				put := parityServe(t, parityCase{file: own, extra: parityThresholdDefaults, groups: "in-team",
					body: "tenants:\n  " + parityIn + ":\n    mysql_connections: \"" + value + "\"\n"}, ep.pr, endpointPut)
				if put.Code != http.StatusBadRequest {
					t.Fatalf("precondition: PUT %q = %d %s, want 400", value, put.Code, put.Body.String())
				}
				w, moved := serveBatch(t, nonFiniteTree(own), ep, map[string]string{"mysql_connections": value})
				if w.Code != http.StatusBadRequest {
					t.Fatalf("%s %q = %d %s, want PUT's 400", ep.name, value, w.Code, w.Body.String())
				}
				if moved {
					t.Errorf("%s %q moved the serving branch", ep.name, value)
				}
				reason := batchViolationReason(t, w)
				// PUT's text is "validation failed: <key>: <reason>".
				putText, _ := parityEnvelope(t, put)["error"].(string)
				if !strings.HasSuffix(putText, "mysql_connections: "+reason) {
					t.Errorf("%s reason %q is not PUT's refusal %q", ep.name, reason, putText)
				}
			})
		}
	}
}

// The finite twin of every row above goes through, and so does a batch
// writing another key of a tenant whose file already holds a non-finite
// value: the refusal is about what the batch writes.
func TestNonFiniteThreshold_BatchPathsAcceptWhatTheyDoNotWrite(t *testing.T) {
	t.Parallel()
	own := "tenants:\n" + paritySec(parityIn, "warning")
	carried := "tenants:\n  " + parityIn + ":\n    _silent_mode: \"warning\"\n    mysql_connections: \"NaN\"\n"
	cases := []struct {
		name, file string
		patch      map[string]string
	}{
		{"finite_value", own, map[string]string{"mysql_connections": "70"}},
		{"carried_non_finite", carried, map[string]string{"_silent_mode": "critical"}},
	}
	for _, c := range cases {
		for _, ep := range batchEndpoints {
			t.Run(c.name+"/"+ep.name, func(t *testing.T) {
				t.Parallel()
				w, moved := serveBatch(t, nonFiniteTree(c.file), ep, c.patch)
				if w.Code != http.StatusOK {
					t.Fatalf("%s = %d %s, want 200", ep.name, w.Code, w.Body.String())
				}
				if moved == ep.pr {
					t.Errorf("%s: serving branch moved = %v, want %v (PR mode writes a branch, direct the base)", ep.name, moved, !ep.pr)
				}
			})
		}
	}
}
