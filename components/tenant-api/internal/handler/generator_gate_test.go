package handler

// #2903 (ADR-036 §4, §5): every content-changing conf.d write asks the
// generator container before it commits, and its answer decides — tested
// here against a fake service on a Unix socket (testutil.FakeGenerator);
// generator_real_test.go runs the real one.
//
// The write paths (gitops/write_validator.go lists them; WriteMerged has no
// handler and is covered in package gitops):
//
//	PUT /tenants/{id}                 direct (Write, WriteIfUnchanged) and PR (WritePRChecked)
//	PUT /tenants/{id}/custom-alerts   direct (WriteIfUnchanged)
//	POST /tenants/batch               direct sync, direct async (WriteBatch), PR (WritePRBatch)
//	POST /groups/{id}/batch           direct sync, direct async (WriteBatch), PR (WritePRBatch)

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// withFakeGenerator installs a gate on f's Writer that asks a fresh fake.
func withFakeGenerator(t *testing.T, f *aonFixture) *testutil.FakeGenerator {
	t.Helper()
	fake := testutil.StartFakeGenerator(t)
	f.deps.Writer.SetValidator(generator.NewGate(generator.NewClient(fake.Socket), ""))
	return fake
}

// repoState is what a refused write must leave as it was: every ref, and a
// clean worktree.
func repoState(t *testing.T, f *aonFixture) string {
	t.Helper()
	return f.git(t, "for-each-ref", "--format=%(refname) %(objectname)") + "\n--status--\n" +
		f.git(t, "status", "--porcelain", "--untracked-files=all")
}

func putTenantAs(t *testing.T, d *Deps, tenant, body, group string, header map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenant, "id", tenant, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	for k, v := range header {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), d.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

const tAPut = "tenants:\n  t-a:\n    mysql_connections: \"72\"\n"

// gatePath is one write path: run sends one request that would change
// content, and returns the HTTP status and the per-op results (async: the
// task's).
type gatePath struct {
	name string
	mode WriteMode
	run  func(t *testing.T, f *aonFixture) (int, []BatchResult)
}

func gatePaths() []gatePath {
	put := func(header map[string]string) func(t *testing.T, f *aonFixture) (int, []BatchResult) {
		return func(t *testing.T, f *aonFixture) (int, []BatchResult) {
			w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", header)
			f.lastBody = w.Body.String()
			return w.Code, nil
		}
	}
	batch := func(group, body string, async bool) func(t *testing.T, f *aonFixture) (int, []BatchResult) {
		return func(t *testing.T, f *aonFixture) (int, []BatchResult) {
			code, res, _ := f.run(t, group, body, async)
			return code, res
		}
	}
	const tenantOps = `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-b","patch":{"_silent_mode":"critical"}}]}`
	const groupOps = `{"patch":{"_silent_mode":"warning"}}`
	return []gatePath{
		{"direct/PUT", WriteModeDirect, put(nil)},
		{"direct/PUT with base hash", WriteModeDirect, put(map[string]string{
			"X-DA-Base-Hash": cfg.ComputeSourceHash([]byte(aonTree()["t-a.yaml"]))})},
		{"direct/custom-alerts", WriteModeDirect, func(t *testing.T, f *aonFixture) (int, []BatchResult) {
			body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(aonTree()["t-a.yaml"])) +
				`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
			resp := putCustomAlerts(t, f.deps, "t-a", body, "alice@example.com", []string{"admins"})
			raw, _ := io.ReadAll(resp.Body)
			f.lastBody = string(raw)
			return resp.StatusCode, nil
		}},
		{"direct/tenant batch sync", WriteModeDirect, batch("", tenantOps, false)},
		{"direct/tenant batch async", WriteModeDirect, batch("", tenantOps, true)},
		{"direct/group batch sync", WriteModeDirect, batch("g-ok", groupOps, false)},
		{"direct/group batch async", WriteModeDirect, batch("g-ok", groupOps, true)},
		{"pr/PUT", WriteModePR, put(nil)},
		{"pr/tenant batch", WriteModePR, batch("", tenantOps, false)},
		{"pr/group batch", WriteModePR, batch("g-ok", groupOps, false)},
	}
}

// Every path asks, and a refusal leaves the repository as it was: no commit
// on any ref, no branch, a clean worktree, no PR/MR.
func TestGeneratorGate_EveryWritePathIsRefused(t *testing.T) {
	t.Parallel()
	for _, p := range gatePaths() {
		t.Run(p.name, func(t *testing.T) {
			t.Parallel()
			f := newAONFixture(t, p.mode)
			fake := withFakeGenerator(t, f)
			fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
				return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("receiver_invalid", "t-a", "", "refused by the fake"))
			})
			before := repoState(t, f)
			code, results := p.run(t, f)
			if len(fake.Requests()) == 0 {
				t.Fatal("the write never asked the generator")
			}
			if strings.HasSuffix(p.name, "async") {
				if len(results) == 0 || results[0].Code != CodeGeneratorFindings || !strings.Contains(results[0].Message, "refused by the fake") {
					t.Errorf("async results = %+v, want GENERATOR_FINDINGS with the related finding", results)
				}
			} else if code != http.StatusUnprocessableEntity {
				t.Errorf("status = %d, want 422", code)
			}
			if after := repoState(t, f); after != before {
				t.Errorf("the repository changed:\nbefore %s\nafter  %s", before, after)
			}
			if f.prs != 0 {
				t.Errorf("%d PR/MR opened", f.prs)
			}
		})
	}
}

// A pass commits exactly what was judged, on the commit judged.
func TestGeneratorGate_PassCommitsWhatWasJudged(t *testing.T) {
	t.Parallel()
	for _, p := range gatePaths() {
		t.Run(p.name, func(t *testing.T) {
			t.Parallel()
			f := newAONFixture(t, p.mode)
			fake := withFakeGenerator(t, f)
			head := f.git(t, "rev-parse", "main")
			code, results := p.run(t, f)
			if code >= 400 {
				t.Fatalf("status %d, results %+v", code, results)
			}
			for _, r := range results {
				if r.Status == "error" {
					t.Fatalf("results %+v", results)
				}
			}
			reqs := fake.Requests()
			if len(reqs) != 1 {
				t.Fatalf("requests = %d, want 1", len(reqs))
			}
			req := reqs[0]
			if req.BaseCommit != head {
				t.Errorf("base = %s, want the commit the write lands on %s", req.BaseCommit, head)
			}
			if req.DeadlineMS <= int64(generator.MinJudgeTime/time.Millisecond) {
				t.Errorf("deadline_ms = %d", req.DeadlineMS)
			}
			// The committed bytes are the judged ones.
			commit := "main"
			if p.mode == WriteModePR {
				branches := f.batchBranches(t)
				if len(branches) != 1 {
					t.Fatalf("branches %v", branches)
				}
				commit = branches[0]
			} else if f.git(t, "rev-parse", "main~1") != head {
				t.Fatalf("main did not move by one commit")
			}
			for _, w := range req.Writes {
				if got := f.git(t, "show", commit+":"+w.Path) + "\n"; got != string(w.Content) {
					t.Errorf("%s committed\n%q\njudged\n%q", w.Path, got, w.Content)
				}
			}
		})
	}
}

func generatorDirectFixture(t *testing.T, answer func(n int, req testutil.FakeValidateRequest) (int, any)) (*aonFixture, *testutil.FakeGenerator) {
	t.Helper()
	f := newAONFixture(t, WriteModeDirect)
	fake := withFakeGenerator(t, f)
	if answer != nil {
		fake.SetValidate(answer)
	}
	return f, fake
}

func genEnvelope(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
		t.Fatalf("body %s: %v", w.Body.String(), err)
	}
	return m
}

// G4: findings → 422 GENERATOR_FINDINGS; related first; pre_existing from
// the base judgement; unrelated ones only for a caller with write on all
// tenants.
func TestGeneratorGate_FindingsResponse(t *testing.T) {
	t.Parallel()
	answer := func(n int, req testutil.FakeValidateRequest) (int, any) {
		if len(req.Writes) == 0 { // the base judgement
			return 200, testutil.FindingsVerdict(req, "/b", testutil.Finding("old", "t-b", "t-b.yaml", "t-b was already wrong"))
		}
		return 200, testutil.FindingsVerdict(req, "/w",
			testutil.Finding("old", "t-b", "t-b.yaml", "t-b was already wrong"),
			testutil.Finding("new", "t-a", "t-a.yaml", "t-a is now wrong"))
	}
	t.Run("platform caller", func(t *testing.T) {
		t.Parallel()
		f, _ := generatorDirectFixture(t, answer)
		w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", nil)
		m := genEnvelope(t, w)
		if w.Code != http.StatusUnprocessableEntity || m["code"] != CodeGeneratorFindings || m["findings_compared_with_base"] != true {
			t.Fatalf("status %d body %s", w.Code, w.Body.String())
		}
		fs := m["findings"].([]any)
		if len(fs) != 2 {
			t.Fatalf("findings %v", fs)
		}
		first, second := fs[0].(map[string]any), fs[1].(map[string]any)
		if first["message"] != "t-a is now wrong" || first["related"] != true || first["pre_existing"] != false ||
			second["related"] != false || second["pre_existing"] != true {
			t.Errorf("findings %v", fs)
		}
		if _, has := m["unrelated_findings_withheld"]; has {
			t.Errorf("withheld for a platform caller: %s", w.Body.String())
		}
	})
	t.Run("tenant-scoped caller", func(t *testing.T) {
		t.Parallel()
		f, _ := generatorDirectFixture(t, answer)
		f.deps.RBAC = newRBACManager(t, "groups:\n  - name: team\n    tenants: [\"t-a\"]\n    permissions: [read, write]\n")
		w := putTenantAs(t, f.deps, "t-a", tAPut, "team", nil)
		m := genEnvelope(t, w)
		fs, _ := m["findings"].([]any)
		if w.Code != http.StatusUnprocessableEntity || len(fs) != 1 || m["unrelated_findings_withheld"] != float64(1) ||
			strings.Contains(w.Body.String(), "t-b") {
			t.Errorf("status %d body %s", w.Code, w.Body.String())
		}
	})
}

// G4: the rejection reasons and the 503s.
func TestGeneratorGate_RefusalStatuses(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name       string
		answer     func(n int, req testutil.FakeValidateRequest) (int, any)
		status     int
		code       string
		reason     string
		retryAfter bool
		text       string
	}{
		{"busy", rejectWith("busy"), 429, CodeGeneratorRejected, "busy", true, "retry"},
		{"deadline", rejectWith("deadline"), 429, CodeGeneratorRejected, "deadline", true, ""},
		{"git_read", rejectWith("git_read"), 429, CodeGeneratorRejected, "git_read", true, ""},
		{"memory", rejectWith("memory"), 422, CodeGeneratorRejected, "memory", false, ""},
		{"incomplete", rejectWith("incomplete"), 422, CodeGeneratorRejected, "incomplete", false, ""},
		{"413", func(int, testutil.FakeValidateRequest) (int, any) {
			return 413, map[string]any{"error": "PAYLOAD_TOO_LARGE", "message": "big"}
		}, 422, CodeGeneratorRejected, "too_large", false, "split the batch"},
		{"mismatch twice", func(_ int, req testutil.FakeValidateRequest) (int, any) {
			v := testutil.PassVerdict(req, nil)
			v["validated"].(map[string]any)["base_commit"] = strings.Repeat("0", 40)
			return 200, v
		}, 429, CodeGeneratorRejected, "mismatch", true, ""},
		{"validator unavailable", func(int, testutil.FakeValidateRequest) (int, any) {
			return 503, map[string]any{"error": "VALIDATOR_UNAVAILABLE", "reason": "amtool_missing", "message": "amtool is not on PATH"}
		}, 503, CodeValidatorUnavailable, "amtool_missing", true, "server log"},
		{"policy unavailable", func(int, testutil.FakeValidateRequest) (int, any) {
			return 503, map[string]any{"error": "POLICY_UNAVAILABLE", "reason": "policy_unusable", "message": "--policy: bad"}
		}, 503, generator.CodePolicyUnavailable, "policy_unusable", true, "server log"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f, _ := generatorDirectFixture(t, tc.answer)
			before := repoState(t, f)
			w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", nil)
			m := genEnvelope(t, w)
			if w.Code != tc.status || m["code"] != tc.code || m["reason"] != tc.reason ||
				(w.Header().Get("Retry-After") != "") != tc.retryAfter || !strings.Contains(m["error"].(string), tc.text) {
				t.Errorf("status %d, Retry-After %q, body %s", w.Code, w.Header().Get("Retry-After"), w.Body.String())
			}
			if repoState(t, f) != before {
				t.Error("the repository changed")
			}
		})
	}
	// A 503's message is never shown to the client, whatever its reason:
	// the service's internal_error is a Python repr, tenant-api's
	// policy_unreadable an os.ReadFile error with the policy path.
	t.Run("503 messages stay in the log", func(t *testing.T) {
		t.Parallel()
		const secret = "/srv/secret-dir/generator_service.py line 577"
		for name, answer := range map[string]func(int, testutil.FakeValidateRequest) (int, any){
			"internal_error": func(int, testutil.FakeValidateRequest) (int, any) {
				return 503, map[string]any{"error": "VALIDATOR_UNAVAILABLE", "reason": "internal_error",
					"message": "generator service error: OSError(2, '" + secret + "')"}
			},
			"amtool_missing": func(int, testutil.FakeValidateRequest) (int, any) {
				return 503, map[string]any{"error": "VALIDATOR_UNAVAILABLE", "reason": "amtool_missing", "message": secret}
			},
			"policy_unusable": func(int, testutil.FakeValidateRequest) (int, any) {
				return 503, map[string]any{"error": "POLICY_UNAVAILABLE", "reason": "policy_unusable", "message": secret}
			},
			// A reason tenant-api does not know is shown as "unknown".
			"unknown": func(int, testutil.FakeValidateRequest) (int, any) {
				return 503, map[string]any{"error": "VALIDATOR_UNAVAILABLE", "reason": secret, "message": secret}
			},
		} {
			f, _ := generatorDirectFixture(t, answer)
			w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", nil)
			if m := genEnvelope(t, w); w.Code != 503 || m["reason"] != name || strings.Contains(w.Body.String(), "secret-dir") ||
				!strings.Contains(m["error"].(string), "(reason: "+name+")") {
				t.Errorf("%s: status %d body %s", name, w.Code, w.Body.String())
			}
			// The async batch's stored results carry the same text.
			code, results, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`, true)
			for _, r := range results {
				if strings.Contains(r.Message, "secret-dir") {
					t.Errorf("%s: async result (status %d) shows the message: %+v", name, code, r)
				}
			}
		}
		// tenant-api's own copy of the policy file unreadable.
		f := newAONFixture(t, WriteModeDirect)
		fake := testutil.StartFakeGenerator(t)
		missing := filepath.Join(t.TempDir(), "secret-dir", "policy.yaml")
		f.deps.Writer.SetValidator(generator.NewGate(generator.NewClient(fake.Socket), missing))
		w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", nil)
		if m := genEnvelope(t, w); w.Code != 503 || m["reason"] != "policy_unreadable" ||
			strings.Contains(w.Body.String(), "secret-dir") || strings.Contains(w.Body.String(), "no such file") {
			t.Errorf("policy_unreadable: status %d body %s", w.Code, w.Body.String())
		}
	})
	t.Run("unreachable", func(t *testing.T) {
		t.Parallel()
		f := newAONFixture(t, WriteModeDirect)
		sock := filepath.Join(t.TempDir(), "none.sock")
		f.deps.Writer.SetValidator(generator.NewGate(generator.NewClient(sock), ""))
		w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", nil)
		m := genEnvelope(t, w)
		if w.Code != 503 || m["code"] != CodeValidatorUnavailable || m["reason"] != "unreachable" ||
			strings.Contains(w.Body.String(), sock) {
			t.Errorf("status %d body %s", w.Code, w.Body.String())
		}
	})
}

func rejectWith(reason string) func(int, testutil.FakeValidateRequest) (int, any) {
	return func(int, testutil.FakeValidateRequest) (int, any) { return 200, testutil.RejectedVerdict(reason) }
}

// G2: an async batch runs under --write-timeout from when it starts; the
// generator is given what is left of it.
func TestGeneratorGate_AsyncBatchDeadline(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		set  time.Duration
		want time.Duration
	}{{"--write-timeout", 12 * time.Second, 12 * time.Second}, {"default", 0, DefaultWriteTimeout}} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f, fake := generatorDirectFixture(t, nil)
			f.deps.WriteTimeoutDur = tc.set
			code, res, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`, true)
			if code != http.StatusAccepted || len(res) != 1 || res[0].Status != "ok" {
				t.Fatalf("code %d results %+v", code, res)
			}
			ms := fake.Requests()[0].DeadlineMS
			if ms > tc.want.Milliseconds() || ms < tc.want.Milliseconds()-3000 {
				t.Errorf("deadline_ms = %d, want just under %v", ms, tc.want)
			}
		})
	}
}

// G6: the flat `_routing_receiver_type` is a 400 on PUT (and on a batch
// patch, validatePatchMap) before anything else judges it — the generator
// is never asked (the domain policy here would forbid slack).
func TestGeneratorGate_FlatReceiverTypeIs400(t *testing.T) {
	t.Parallel()
	f, fake := generatorDirectFixture(t, nil)
	if err := os.WriteFile(filepath.Join(f.dir, "_domain_policy.yaml"), []byte("domain_policies:\n  d:\n    tenants: [t-a]\n"+
		"    constraints:\n      forbidden_receiver_types: [slack]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	body := "tenants:\n  t-a:\n    mysql_connections: \"72\"\n    _routing_receiver_type: slack\n"
	w := putTenantAs(t, f.deps, "t-a", body, "admins", nil)
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "_routing.receiver.type") {
		t.Errorf("PUT: status %d body %s", w.Code, w.Body.String())
	}
	code, _, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"_routing_receiver_type":"slack"}}]}`, false)
	if code != http.StatusBadRequest {
		t.Errorf("batch: status %d", code)
	}
	if n := len(fake.Requests()); n != 0 {
		t.Errorf("the generator was asked %d time(s)", n)
	}
}

// A batch the generator refuses with findings is a rejected batch (#2900's
// 422 BatchResponse shape): every op refused with GENERATOR_FINDINGS naming
// the findings about its own tenant, and the batch-level findings list. A
// retryable rejection is a 429 ErrorResponse.
func TestGeneratorGate_BatchRefusalShape(t *testing.T) {
	t.Parallel()
	const ops = `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-b","patch":{"_silent_mode":"critical"}}]}`
	findings := func(_ int, req testutil.FakeValidateRequest) (int, any) {
		if len(req.Writes) == 0 {
			return 200, testutil.PassVerdict(req, nil)
		}
		return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("k", "t-b", "t-b.yaml", "t-b is wrong"))
	}
	for _, tc := range []struct {
		name, group string
		mode        WriteMode
	}{{"direct/tenant", "", WriteModeDirect}, {"direct/group", "g-ok", WriteModeDirect},
		{"pr/tenant", "", WriteModePR}, {"pr/group", "g-ok", WriteModePR}} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f := newAONFixture(t, tc.mode)
			fake := withFakeGenerator(t, f)
			fake.SetValidate(findings)
			body := ops
			if tc.group != "" {
				body = `{"patch":{"_silent_mode":"warning"}}`
			}
			code, results, _ := f.run(t, tc.group, body, false)
			if code != http.StatusUnprocessableEntity || len(results) != 2 {
				t.Fatalf("code %d results %+v", code, results)
			}
			for _, r := range results {
				if r.Status != "error" || r.Code != CodeGeneratorFindings {
					t.Errorf("result %+v", r)
				}
				if names := strings.Contains(r.Message, "t-b is wrong"); names != (r.TenantID == "t-b") {
					t.Errorf("%s's message names t-b's finding: %v (%q)", r.TenantID, names, r.Message)
				}
			}
		})
	}
	t.Run("findings list and status", func(t *testing.T) {
		t.Parallel()
		f, _ := generatorDirectFixture(t, findings)
		w := postTenantBatch(t, f.deps, `[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-b","patch":{"_silent_mode":"critical"}}]`)
		m := genEnvelope(t, w)
		fs, _ := m["findings"].([]any)
		if w.Code != http.StatusUnprocessableEntity || m["status"] != batchStatusRejected || len(fs) != 1 ||
			fs[0].(map[string]any)["related"] != true || m["findings_compared_with_base"] != true {
			t.Errorf("status %d body %s", w.Code, w.Body.String())
		}
	})
	t.Run("busy is a 429 envelope", func(t *testing.T) {
		t.Parallel()
		f, _ := generatorDirectFixture(t, rejectWith("busy"))
		w := postTenantBatch(t, f.deps, `[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]`)
		if m := genEnvelope(t, w); w.Code != http.StatusTooManyRequests || m["code"] != CodeGeneratorRejected || m["reason"] != "busy" {
			t.Errorf("status %d body %s", w.Code, w.Body.String())
		}
	})
}

// ADR-036 §4 "驗的和寫的必須是同一份": a .gitattributes clean filter that
// rewrites a tenant file as it is staged makes the commit other bytes than
// the ones judged — every path refuses it (422 GENERATOR_REJECTED reason
// normalized, async: the task's results) and leaves the repository as it
// was. (Package gitops covers `text eol=lf` with a CRLF body.)
func TestGeneratorGate_NormalizedByGitAttributesIsRefused(t *testing.T) {
	t.Parallel()
	for _, p := range gatePaths() {
		t.Run(p.name, func(t *testing.T) {
			t.Parallel()
			f := newAONFixture(t, p.mode)
			fake := withFakeGenerator(t, f)
			// Rewrites exactly what each path's request writes, nothing the
			// seeded files hold (their checkout stays clean).
			f.git(t, "config", "filter.shout.clean", `sed -e s/warning/WARNING/ -e s/critical/CRITICAL/ -e s/queue_high/QUEUE_HIGH/ -e s/72/73/`)
			if err := os.WriteFile(filepath.Join(f.dir, ".gitattributes"), []byte("t-*.yaml filter=shout\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			f.git(t, "add", ".gitattributes")
			f.git(t, "commit", "-q", "-m", "attributes")
			before := repoState(t, f)
			code, results := p.run(t, f)
			if len(fake.Requests()) != 1 {
				t.Fatalf("the generator was asked %d time(s), want 1 (it passes)", len(fake.Requests()))
			}
			if strings.HasSuffix(p.name, "async") || len(results) > 0 {
				if len(results) == 0 {
					t.Fatalf("status %d, no results", code)
				}
				for _, r := range results {
					if r.Status != "error" || r.Code != CodeGeneratorRejected || !strings.Contains(r.Message, "(normalized)") {
						t.Errorf("result %+v, want GENERATOR_REJECTED normalized", r)
					}
				}
			}
			if !strings.HasSuffix(p.name, "async") && code != http.StatusUnprocessableEntity {
				t.Errorf("status = %d, want 422", code)
			}
			if after := repoState(t, f); after != before {
				t.Errorf("the repository changed:\nbefore %s\nafter  %s", before, after)
			}
			if f.prs != 0 {
				t.Errorf("%d PR/MR opened", f.prs)
			}
		})
	}
	t.Run("PUT envelope", func(t *testing.T) {
		t.Parallel()
		f, _ := generatorDirectFixture(t, nil)
		f.git(t, "config", "filter.shout.clean", "sed -e s/72/73/")
		if err := os.WriteFile(filepath.Join(f.dir, ".gitattributes"), []byte("t-*.yaml filter=shout\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		f.git(t, "add", ".gitattributes")
		f.git(t, "commit", "-q", "-m", "attributes")
		w := putTenantAs(t, f.deps, "t-a", tAPut, "admins", nil)
		m := genEnvelope(t, w)
		if w.Code != http.StatusUnprocessableEntity || m["code"] != CodeGeneratorRejected || m["reason"] != generator.ReasonNormalized ||
			w.Header().Get("Retry-After") != "" || strings.Contains(m["error"].(string), "t-a.yaml") {
			t.Errorf("status %d body %s", w.Code, w.Body.String())
		}
	})
}

// #1530 on the generator's refusal of a group batch, direct and PR mode:
// the rejected batch names only the members the caller may read when it is
// answered — in its results and in its findings[] (disclosedFindings: a
// finding naming such a member by tenant, or by file only, is withheld and
// counted). Here a member's org is reassigned while the generator judges
// (the caller could write both members when the batch was authorized).
func TestGeneratorGate_GroupBatchRefusalShowsOnlyVisibleMembers(t *testing.T) {
	t.Parallel()
	for _, mode := range []WriteMode{WriteModeDirect, WriteModePR} {
		t.Run(string(mode), func(t *testing.T) {
			t.Parallel()
			orgs := "tenant_orgs:\n  " + orgEnfTenantIn + ": [" + orgEnfMemberOrg + "]\n  " + orgEnfTenantOut + ": [" + orgEnfMemberOrg + "]\n"
			files := orgEnfGroupMemberFiles(visGroupsYAML)
			files["_tenant_orgs.yaml"] = orgs
			f := newOrgEnfFixture(t, files)
			f.tenantOrg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
				orgEnfTenantIn: {orgEnfMemberOrg}, orgEnfTenantOut: {orgEnfMemberOrg}}})
			d := f.deps()
			if mode == WriteModePR {
				d.WriteMode = WriteModePR
				d.PRClient = &mockPlatformClient{
					createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
						return &platform.PRInfo{Number: 5, WebURL: "https://gh/5", State: "open", HeadRef: headBranch}, nil
					},
				}
				d.PRTracker = &mockPlatformTracker{}
			}
			fake := testutil.StartFakeGenerator(t)
			d.Writer.SetValidator(generator.NewGate(generator.NewClient(fake.Socket), ""))
			fake.SetValidate(func(_ int, req testutil.FakeValidateRequest) (int, any) {
				f.tenantOrg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
					orgEnfTenantIn: {orgEnfMemberOrg}, orgEnfTenantOut: {orgEnfOutsiderOrg}}})
				if len(req.Writes) == 0 {
					return 200, testutil.PassVerdict(req, nil)
				}
				return 200, testutil.FindingsVerdict(req, "/w",
					testutil.Finding("k", orgEnfTenantIn, "", "refused by the fake"),
					testutil.Finding("k", orgEnfTenantOut, "", "hidden-tenant-detail"),
					testutil.Finding("k", "", orgEnfTenantOut+".yaml", "hidden-file-detail"))
			})
			w := visCall(t, f, d, GroupBatch(d), rbac.PermRead, "POST+batch", "g-mixed", `{"patch":{"_silent_mode":"warning"}}`)
			if len(fake.Requests()) == 0 {
				t.Fatalf("the generator was never asked; status %d %s", w.Code, w.Body.String())
			}
			var resp GroupBatchResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			body := w.Body.String()
			if w.Code != http.StatusUnprocessableEntity || strings.Contains(body, orgEnfTenantOut) || strings.Contains(body, "hidden-") {
				t.Errorf("status %d; the response names a member the caller cannot read: %s", w.Code, body)
			}
			if len(resp.Results) != 1 || resp.Results[0].TenantID != orgEnfTenantIn || resp.Results[0].Code != CodeGeneratorFindings {
				t.Errorf("results = %+v, want only %s refused with %s", resp.Results, orgEnfTenantIn, CodeGeneratorFindings)
			}
			if len(resp.Findings) != 1 || resp.Findings[0].Message != "refused by the fake" || resp.UnrelatedFindingsWithheld != 2 {
				t.Errorf("findings = %+v, withheld %d; want only the readable member's, 2 withheld", resp.Findings, resp.UnrelatedFindingsWithheld)
			}
		})
	}
}

// The client text of every generator refusal is one rule
// (generatorRejectedText / generatorUnavailableText): code, the reason as
// generator.ClientReason gives it, a fixed sentence — never the error's
// message. Three shapes whose message carries detail — a mismatch naming
// the file judged otherwise, a 400 invalid_request whose message is a
// Python repr, a rejected verdict with a reason tenant-api does not know
// (shown as "unknown") — on every write path: sync, batch results, async
// task, PR mode.
func TestGeneratorGate_RefusalTextNeverEchoesTheMessage(t *testing.T) {
	t.Parallel()
	const secret = "srv-secret-dir"
	for _, tc := range []struct {
		name   string
		answer func(int, testutil.FakeValidateRequest) (int, any)
		absent []string
		reason string
	}{
		{"mismatch", func(_ int, req testutil.FakeValidateRequest) (int, any) {
			v := testutil.PassVerdict(req, nil)
			if files := v["validated"].(map[string]any)["files"].([]map[string]any); len(files) > 0 {
				files[0]["sha256"] = "00"
			}
			return 200, v
		}, []string{"judged with other content", ".yaml"}, "mismatch"},
		{"invalid_request", func(int, testutil.FakeValidateRequest) (int, any) {
			return 400, map[string]any{"error": "INVALID_REQUEST", "message": "ValueError('/" + secret + "/x')"}
		}, []string{secret, "ValueError"}, "invalid_request"},
		{"unknown reason", func(int, testutil.FakeValidateRequest) (int, any) {
			v := testutil.RejectedVerdict("/" + secret + "/reason")
			v["message"] = "detail from /" + secret
			return 200, v
		}, []string{secret}, "unknown"},
	} {
		for _, p := range gatePaths() {
			t.Run(tc.name+"/"+p.name, func(t *testing.T) {
				t.Parallel()
				f := newAONFixture(t, p.mode)
				fake := withFakeGenerator(t, f)
				fake.SetValidate(tc.answer)
				code, results := p.run(t, f)
				if len(fake.Requests()) == 0 {
					t.Fatal("the write never asked the generator")
				}
				if code < 400 && !strings.HasSuffix(p.name, "async") {
					t.Fatalf("status %d", code)
				}
				for _, a := range tc.absent {
					if strings.Contains(f.lastBody, a) {
						t.Errorf("the response shows %q: %s", a, f.lastBody)
					}
				}
				if !strings.Contains(f.lastBody, tc.reason) {
					t.Errorf("the response does not name reason %q: %s", tc.reason, f.lastBody)
				}
				for _, r := range results {
					if r.Code != CodeGeneratorRejected {
						t.Errorf("result %+v, want %s", r, CodeGeneratorRejected)
					}
				}
			})
		}
	}
}
