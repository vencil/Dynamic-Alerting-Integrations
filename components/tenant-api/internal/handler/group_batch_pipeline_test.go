package handler

// #2339: POST /groups/{id}/batch expands into one BatchOperation per member
// and runs the /tenants/batch pipeline. Before the fix it wrote straight to
// the base branch even in PR write-back mode, and skipped the domain-policy
// gate and the patch-value check in both modes.

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

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

const finGroupsYAML = "groups:\n  g-fin:\n    label: Finance\n    members: [t-ok, t-dirty]\n"

// groupBatchFixture seeds batchTree plus a finance group as a git repo and
// returns Deps for the given write mode. In PR mode, *prCalls counts
// CreatePR calls and *labels / *title hold the last call's arguments.
type groupBatchFixture struct {
	configDir string
	deps      *Deps
	prCalls   int
	title     string
	labels    []string
	head      string // the last CreatePR call's head branch
}

func newGroupBatchFixture(t *testing.T, mode WriteMode) *groupBatchFixture {
	t.Helper()
	files := batchTree()
	files["_groups.yaml"] = finGroupsYAML
	return newGroupBatchFixtureOver(t, mode, files)
}

// newGroupBatchFixtureOver is newGroupBatchFixture over files, which must
// carry the g-fin group in `_groups.yaml`.
func newGroupBatchFixtureOver(t *testing.T, mode WriteMode, files map[string]string) *groupBatchFixture {
	t.Helper()
	configDir := seedGitTree(t, files)
	f := &groupBatchFixture{configDir: configDir}
	f.deps = &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Groups: groups.NewManager(configDir), Policy: policy.NewManager(configDir), WriteMode: mode}
	if mode == WriteModePR {
		f.deps.PRClient = &mockPlatformClient{
			providerName: "github",
			createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
				f.prCalls++
				f.title, f.labels, f.head = title, labels, head
				return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
			},
		}
		f.deps.PRTracker = &mockPlatformTracker{}
	}
	return f
}

func (f *groupBatchFixture) post(t *testing.T, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("POST", "/api/v1/groups/g-fin/batch", "id", "g-fin", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	f.deps.RBAC.Middleware(rbac.PermRead, nil)(GroupBatch(f.deps)).ServeHTTP(w, req)
	return w
}

func (f *groupBatchFixture) mainSHA(t *testing.T) string {
	t.Helper()
	out, err := exec.Command("git", "-C", f.configDir, "rev-parse", "main").CombinedOutput()
	if err != nil {
		t.Fatalf("git rev-parse main: %v\n%s", err, out)
	}
	return strings.TrimSpace(string(out))
}

func decodeGroupBatch(t *testing.T, w *httptest.ResponseRecorder) GroupBatchResponse {
	t.Helper()
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp GroupBatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return resp
}

// PR mode: the whole group becomes ONE PR, and the base branch gets no commit.
func TestGroupBatch_PRMode_OpensOnePR(t *testing.T) {
	f := newGroupBatchFixture(t, WriteModePR)
	before := f.mainSHA(t)
	resp := decodeGroupBatch(t, f.post(t, `{"patch":{"cpu_usage_percent":"90"}}`))

	if f.prCalls != 1 {
		t.Errorf("CreatePR calls = %d, want 1", f.prCalls)
	}
	if after := f.mainSHA(t); after != before {
		t.Errorf("base branch moved %s → %s: group batch committed directly in PR mode", before, after)
	}
	if resp.Status != "pending_review" || resp.GroupID != "g-fin" || resp.PRNumber != 9 || resp.PRURL == "" {
		t.Errorf("response = %+v, want pending_review for g-fin with PR 9", resp)
	}
	if len(resp.Results) != 2 || resp.Results[0].Status != "included" || resp.Results[1].Status != "included" {
		t.Errorf("results = %+v, want both members included", resp.Results)
	}
	if !strings.Contains(f.title, "g-fin") {
		t.Errorf("PR title = %q, want it to name the group", f.title)
	}
	hasGroup := false
	for _, l := range f.labels {
		hasGroup = hasGroup || l == "group"
	}
	if !hasGroup {
		t.Errorf("PR labels = %v, want a \"group\" label", f.labels)
	}
}

// Domain policy: finance forbids slack, and team-chat renders a slack
// receiver. The patch is refused for every member in both write modes and
// nothing is written.
func TestGroupBatch_DomainPolicy(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			f := newGroupBatchFixture(t, mode)
			before := f.mainSHA(t)
			okBefore, _ := os.ReadFile(filepath.Join(f.configDir, "t-ok.yaml"))
			resp := decodeGroupBatch(t, f.post(t, `{"patch":{"_routing_profile":"team-chat"}}`))

			if len(resp.Results) != 2 {
				t.Fatalf("results = %+v, want 2", resp.Results)
			}
			for _, r := range resp.Results {
				if r.Status != "error" || !strings.Contains(r.Message, "policy violation") {
					t.Errorf("%s = %+v, want refused for domain policy", r.TenantID, r)
				}
			}
			if f.prCalls != 0 {
				t.Errorf("CreatePR calls = %d, want 0", f.prCalls)
			}
			if after := f.mainSHA(t); after != before {
				t.Errorf("base branch moved %s → %s", before, after)
			}
			okAfter, _ := os.ReadFile(filepath.Join(f.configDir, "t-ok.yaml"))
			if !bytes.Equal(okBefore, okAfter) {
				t.Errorf("refused patch changed t-ok.yaml:\n%s", okAfter)
			}
		})
	}
}

// Patch values go through validatePatchMap, like /tenants/batch.
func TestGroupBatch_PatchValueValidation(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		t.Run(string(mode), func(t *testing.T) {
			f := newGroupBatchFixture(t, mode)
			before := f.mainSHA(t)
			w := f.post(t, `{"patch":{"_timeout_ms":"99999999"}}`)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body: %s", w.Code, w.Body.String())
			}
			var bad ErrorResponse
			if err := json.Unmarshal(w.Body.Bytes(), &bad); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			if bad.Code != CodeInvalidBody || len(bad.Violations) != 1 || bad.Violations[0].Field != `patch["_timeout_ms"]` {
				t.Errorf("response = %+v, want one %s violation on patch[\"_timeout_ms\"]", bad, CodeInvalidBody)
			}
			if f.prCalls != 0 {
				t.Errorf("CreatePR calls = %d, want 0", f.prCalls)
			}
			if after := f.mainSHA(t); after != before {
				t.Errorf("base branch moved %s → %s", before, after)
			}
		})
	}
}
