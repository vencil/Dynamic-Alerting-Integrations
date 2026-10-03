package handler

// B2 (#2341) owner ruling: removing what is not there is a no-op, as in
// RFC 7396 — an unset-only op on a tenant that does not exist, or that does
// not carry the key, writes nothing, commits nothing, opens no PR, creates no
// tenant, and is not refused by domain policy. It reports what an unchanged
// op already reports: `ok` in direct mode, and in PR mode the all-no-op
// "completed … No changes to apply" response (gitops.ErrNoChanges). An op
// with neither patch nor unset keeps the pre-B2 tenant-batch no-op; the
// group batch keeps its pre-B2 400 for an empty patch.

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
)

// noOpRun posts ops over files in mode and reports the response, whether a
// PR was opened, and whether HEAD moved.
func noOpRun(t *testing.T, mode WriteMode, files map[string]string, ops string) (resp BatchResponse, prOpened, headMoved bool, configDir string) {
	t.Helper()
	configDir = seedGitTree(t, files)
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: mode}
	if mode == WriteModePR {
		d.PRClient = &mockPlatformClient{providerName: "github",
			createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
				prOpened = true
				return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
			}}
		d.PRTracker = &mockPlatformTracker{}
	}
	before := gitHead(t, configDir)
	resp = runBatch(t, configDir, d, ops)
	return resp, prOpened, gitHead(t, configDir) != before, configDir
}

// assertUnchanged: the existing unchanged report for mode, nothing written.
func assertUnchanged(t *testing.T, mode WriteMode, resp BatchResponse, prOpened, headMoved bool) {
	t.Helper()
	if prOpened || headMoved {
		t.Errorf("no-op wrote something: PR opened %v, HEAD moved %v", prOpened, headMoved)
	}
	if mode == WriteModePR {
		if resp.Status != "completed" || resp.Message != "No changes to apply; no PR/MR created." {
			t.Errorf("PR-mode response = %+v, want the no-changes completion", resp)
		}
		return
	}
	for _, r := range resp.Results {
		if r.Status != "ok" {
			t.Errorf("direct-mode result = %+v, want ok", r)
		}
	}
}

func TestBatchTenants_UnsetNoOp(t *testing.T) {
	// t-off on disk, ENABLED on the violating profile, authored with a
	// 4-space indent: re-encoding it would reflow it into a commit, and
	// judging the unset would refuse it for the routing already on disk.
	files := routingPolicyTree()
	files["_domain_policy.yaml"] = "domain_policies:\n  finance:\n    tenants: [t-off, t-ghost]\n" +
		"    constraints:\n      forbidden_receiver_types: [slack]\n"
	disk := "tenants:\n    t-off:\n        cpu_usage_percent: '85'\n        _routing_profile: team-chat\n"
	files["t-off.yaml"] = disk
	// The defaults route the tenant that does not exist to slack, so judging
	// its unset would refuse it too.
	files["_routing_defaults.yaml"] = "_routing_defaults:\n  receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/d'}\n"
	cases := []struct{ name, ops string }{
		{"tenant does not carry the key", `[{"tenant_id":"t-off","unset":["_routing"]}]`},
		{"tenant does not exist", `[{"tenant_id":"t-ghost","unset":["_routing"]}]`},
		{"both", `[{"tenant_id":"t-ghost","unset":["_routing"]},{"tenant_id":"t-off","unset":["_routing"]}]`},
	}
	for _, mode := range bothModes {
		for _, tc := range cases {
			t.Run(string(mode)+"/"+tc.name, func(t *testing.T) {
				resp, prOpened, headMoved, configDir := noOpRun(t, mode, files, tc.ops)
				for _, r := range resp.Results {
					if r.Status == "error" {
						t.Fatalf("no-op refused: %+v", r)
					}
				}
				assertUnchanged(t, mode, resp, prOpened, headMoved)
				if _, err := os.Stat(filepath.Join(configDir, "t-ghost.yaml")); !os.IsNotExist(err) {
					t.Errorf("unset created t-ghost.yaml (err=%v)", err)
				}
				if b, _ := os.ReadFile(filepath.Join(configDir, "t-off.yaml")); string(b) != disk {
					t.Errorf("t-off.yaml changed:\n%s", b)
				}
			})
		}
		// A no-op op does not hide a real one beside it.
		t.Run(string(mode)+"/beside a real op", func(t *testing.T) {
			resp, prOpened, headMoved, _ := noOpRun(t, mode, files,
				`[{"tenant_id":"t-ghost","unset":["_routing"]},{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"}}]`)
			if mode == WriteModePR && (!prOpened || resp.Status != "pending_review") {
				t.Errorf("PR mode: PR opened %v, response %+v", prOpened, resp)
			}
			if mode == WriteModeDirect && !headMoved {
				t.Errorf("direct mode: the real op did not commit: %+v", resp)
			}
		})
	}
}

// Point 2: an op with neither patch nor unset is the pre-B2 tenant-batch
// no-op (200), also for a tenant that does not exist (no file created).
func TestBatchTenants_EmptyEditIsNoOp(t *testing.T) {
	for _, mode := range bothModes {
		for _, op := range []string{`{"tenant_id":"t-off"}`, `{"tenant_id":"t-off","patch":{},"unset":[]}`, `{"tenant_id":"t-new","patch":{}}`} {
			t.Run(string(mode)+"/"+op, func(t *testing.T) {
				resp, prOpened, headMoved, configDir := noOpRun(t, mode, offTree(offDisabledOK), "["+op+"]")
				assertUnchanged(t, mode, resp, prOpened, headMoved)
				if _, err := os.Stat(filepath.Join(configDir, "t-new.yaml")); !os.IsNotExist(err) {
					t.Errorf("empty op created t-new.yaml (err=%v)", err)
				}
			})
		}
	}
}

// Point 2: the group batch keeps its pre-B2 400 for an empty patch — code
// BAD_REQUEST, message unchanged — and lets an unset-only body through.
func TestGroupBatch_EmptyEditRefused(t *testing.T) {
	for _, mode := range bothModes {
		for _, body := range []string{`{}`, `{"patch":{}}`, `{"patch":{},"unset":[]}`} {
			t.Run(string(mode)+"/"+body, func(t *testing.T) {
				f := newGroupBatchFixture(t, mode)
				w := f.post(t, body)
				var bad ErrorResponse
				_ = json.Unmarshal(w.Body.Bytes(), &bad)
				if w.Code != http.StatusBadRequest || bad.Code != CodeBadRequest || bad.Error != "patch must not be empty" {
					t.Errorf("status %d, body %s; want 400 %s \"patch must not be empty\"", w.Code, w.Body.String(), CodeBadRequest)
				}
			})
		}
		t.Run(string(mode)+"/unset only is accepted", func(t *testing.T) {
			f := newGroupBatchFixture(t, mode)
			if w := f.post(t, `{"unset":["_routing"]}`); w.Code != http.StatusOK {
				t.Errorf("status = %d, want 200; body: %s", w.Code, w.Body.String())
			}
		})
	}
}

// B2 round 3 (B-2): a no-op is not refused for what the file already holds —
// a file with no section for the tenant, or a key the write validator
// rejects. Before the fix, its notices validation turned these into a 400
// (PR mode: the whole batch).
func noOpOddTree() map[string]string {
	files := routingPolicyTree()
	files["t-empty.yaml"] = "tenants: {}\n"
	files["t-odd.yaml"] = "tenants:\n  t-odd:\n    totally_unknown_key: '1'\n"
	files["t-other.yaml"] = tOther
	files["_groups.yaml"] = "groups:\n  g-fin:\n    label: Finance\n    members: [t-odd, t-other]\n"
	return files
}

func TestBatchTenants_NoOpOverFileValidateRejects(t *testing.T) {
	cases := []struct{ name, ops string }{
		{"no section, unset", `[{"tenant_id":"t-empty","unset":["_routing"]}]`},
		{"no section, empty patch", `[{"tenant_id":"t-empty","patch":{}}]`},
		{"key validate rejects, unset", `[{"tenant_id":"t-odd","unset":["_routing"]}]`},
	}
	for _, mode := range bothModes {
		for _, tc := range cases {
			t.Run(string(mode)+"/"+tc.name, func(t *testing.T) {
				resp, prOpened, headMoved, _ := noOpRun(t, mode, noOpOddTree(), tc.ops)
				for _, r := range resp.Results {
					if r.Status == "error" {
						t.Fatalf("no-op refused: %+v", r)
					}
				}
				assertUnchanged(t, mode, resp, prOpened, headMoved)
			})
		}
		t.Run(string(mode)+"/beside a real op", func(t *testing.T) {
			resp, prOpened, headMoved, _ := noOpRun(t, mode, noOpOddTree(),
				`[{"tenant_id":"t-empty","unset":["_routing"]},{"tenant_id":"t-odd","unset":["_routing"]},
				  {"tenant_id":"t-other","patch":{"cpu_usage_percent":"90"}}]`)
			if want := okStatus(mode) + "," + okStatus(mode) + "," + okStatus(mode); statuses(resp.Results) != want {
				t.Errorf("statuses = %s, want %s: %+v", statuses(resp.Results), want, resp)
			}
			if (mode == WriteModePR && !prOpened) || (mode == WriteModeDirect && !headMoved) {
				t.Errorf("the real op did not land: PR %v, HEAD moved %v", prOpened, headMoved)
			}
		})
		t.Run(string(mode)+"/group", func(t *testing.T) {
			f := newGroupBatchFixtureOver(t, mode, noOpOddTree())
			resp := decodeGroupBatch(t, f.post(t, `{"unset":["_routing"]}`))
			for _, r := range resp.Results {
				if r.Status == "error" {
					t.Errorf("member refused: %+v", r)
				}
			}
			if f.prCalls != 0 {
				t.Errorf("an all-no-op group batch opened a PR")
			}
		})
	}
}
