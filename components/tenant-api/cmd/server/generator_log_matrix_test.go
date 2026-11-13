package main

// #1560 ⑤-b: what the route generator container sends never reaches the
// platform log — the main guard, by behaviour. A fake generator puts a
// canary in every field of its answers, structured-looking ones included
// (they are the container's strings too); each answer is sent through the
// real router, as a caller who sees the whole tree and as one who may write
// a single tenant, and the log must hold no canary. Its route list is held
// to the router, so a new route that asks the generator gets a row.
//
// Not t.Parallel() at the top: it swaps the process-global default slog
// logger and the /metrics generator source; its subtests run in parallel
// inside a group that ends before both are restored.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops/gitopstest"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// generatorRoutes: every route that asks the generator container, method +
// chi pattern → how; every write route is here or in noGeneratorRoutes.
var generatorRoutes = map[string]string{
	"PUT /api/v1/tenants/{id}/":              "a content-changing PUT, direct and PR mode (Writer.Write* → Gate.ValidateWrites)",
	"PUT /api/v1/tenants/{id}/custom-alerts": "direct mode (WriteIfUnchanged); PR mode answers 501 before it",
	"POST /api/v1/tenants/batch":             "direct sync and async (WriteBatch), PR mode (WritePRBatch)",
	"POST /api/v1/groups/{id}/batch":         "one op per member through the /tenants/batch pipeline",
	"POST /api/v1/tenants/{id}/validate":     "direct mode: judgePreview asks as PUT would (#3016)",
	"GET /metrics":                           "the container's /v1/status and /metrics at scrape time",
}

// noGeneratorRoutes: the write routes that do not ask it, and why.
var noGeneratorRoutes = map[string]string{
	"PUT /api/v1/tenants/{id}/federation":                   "federation state, not conf.d (statestore)",
	"POST /api/v1/tenants/{id}/diff":                        "a diff of the body against the file; judges nothing",
	"POST /api/v1/tenants/{id}/recipe-preview":              "asks the recipe-preview container (ADR-027 §5), not the generator; writes nothing",
	"POST /api/v1/tenants/simulate":                         "always 501 SIMULATE_NOT_PROVIDED; reads nothing",
	"PUT /api/v1/groups/{id}/":                              "group records (statestore), not conf.d",
	"DELETE /api/v1/groups/{id}/":                           "group records (statestore), not conf.d",
	"PUT /api/v1/views/{id}/":                               "saved views (statestore)",
	"DELETE /api/v1/views/{id}/":                            "saved views (statestore)",
	"POST /api/v1/federation/tokens/":                       "token records",
	"DELETE /api/v1/federation/tokens/{id}":                 "token records",
	"PUT /api/v1/federation/policy/":                        "the federation whitelist (statestore)",
	"POST /api/v1/federation/accounts/backfill":             "federation account records",
	"POST /api/v1/audit/tenants/{id}/access-report/dry-run": "a what-if over a candidate _rbac.yaml",
}

func TestGeneratorRoutesCoverEveryWriteRoute(t *testing.T) {
	t.Parallel()
	writeMethods := map[string]bool{"PUT": true, "POST": true, "DELETE": true, "PATCH": true}
	seen := map[string]bool{}
	var unclassified []string
	err := chi.Walk(buildRouterAllRoutes(t), func(method, route string, _ http.Handler, _ ...func(http.Handler) http.Handler) error {
		key := method + " " + route
		_, gen := generatorRoutes[key]
		_, no := noGeneratorRoutes[key]
		switch {
		case gen && no:
			t.Errorf("%s is in both generatorRoutes and noGeneratorRoutes", key)
		case gen || no:
			seen[key] = true
		case writeMethods[method]:
			unclassified = append(unclassified, key)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(unclassified)
	if len(unclassified) > 0 {
		t.Errorf("write route(s) neither in generatorRoutes nor noGeneratorRoutes: say whether each asks "+
			"the generator container (#1560 ⑤-b: a route that does gets a row of the log matrix):\n  %s",
			strings.Join(unclassified, "\n  "))
	}
	for _, m := range []map[string]string{generatorRoutes, noGeneratorRoutes} {
		for key := range m {
			if !seen[key] {
				t.Errorf("stale entry %q: no such registered route", key)
			}
		}
	}
	// Every generator route has its rows in the matrix, and only those.
	covered := map[string]bool{"GET /metrics": true} // its own row: logMatrixMetrics
	for _, p := range logMatrixPaths() {
		if _, ok := generatorRoutes[p.route]; !ok {
			t.Errorf("matrix path %s: route %q is not in generatorRoutes", p.name, p.route)
		}
		covered[p.route] = true
	}
	for key := range generatorRoutes {
		if !covered[key] {
			t.Errorf("generator route %s has no row in the log matrix", key)
		}
	}
}

// matrixTree: two tenants, a platform file, and a group of each size.
func matrixTree() map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"t-a.yaml":       "tenants:\n  t-a:\n    mysql_connections: \"70\"\n",
		"t-b.yaml":       "tenants:\n  t-b:\n    mysql_connections: \"75\"\n",
		"_groups.yaml": "groups:\n" +
			"  g-ok:\n    label: OK\n    members: [t-a, t-b]\n" +
			"  g-one:\n    label: One\n    members: [t-a]\n",
	}
}

// The callers: one who sees the whole tree (write on "*"), one who may write
// t-a only.
const (
	callerTree = "admins"
	callerOne  = "t-a-writers"
	matrixRBAC = "groups:\n" +
		"  - name: admins\n    tenants: [\"*\"]\n    permissions: [read, write, admin]\n" +
		"  - name: t-a-writers\n    tenants: [t-a]\n    permissions: [read, write]\n"
)

type matrixFixture struct {
	dir    string
	deps   *handler.Deps
	router http.Handler
	fake   *testutil.FakeGenerator
}

// matrixPRClient / matrixTracker: a forge that opens every PR asked.
type matrixPRClient struct{}

func (matrixPRClient) ValidateToken() error                    { return nil }
func (matrixPRClient) CreateBranch(string) error               { return nil }
func (matrixPRClient) DeleteBranch(string) error               { return nil }
func (matrixPRClient) SetBaseURL(string)                       {}
func (matrixPRClient) ProviderName() string                    { return "github" }
func (matrixPRClient) ListOpenPRs() ([]platform.PRInfo, error) { return nil, nil }
func (matrixPRClient) CreatePR(string, string, string, []string) (*platform.PRInfo, error) {
	return &platform.PRInfo{Number: 7, WebURL: "https://example/pr/7", State: "open"}, nil
}

type matrixTracker struct{ routeStubTracker }

func (matrixTracker) ClaimTenant(string) bool { return true }

func runGit(t *testing.T, args ...string) {
	t.Helper()
	if out, err := exec.Command("git", args...).CombinedOutput(); err != nil {
		t.Skipf("git %v: %v\n%s", args, err, out)
	}
}

func newMatrixFixture(t *testing.T, mode handler.WriteMode) *matrixFixture {
	t.Helper()
	dir := t.TempDir()
	for name, content := range matrixTree() {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for _, args := range [][]string{
		{"init"}, {"config", "user.email", "t@t.com"}, {"config", "user.name", "T"},
		{"add", "."}, {"commit", "-m", "seed"}, {"branch", "-M", "main"},
	} {
		runGit(t, append([]string{"-C", dir}, args...)...)
	}
	// A PR-mode write pushes its branch (#1724).
	bare := t.TempDir()
	runGit(t, "init", "--bare", "-q", bare)
	runGit(t, "-C", dir, "remote", "add", "origin", bare)

	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", matrixRBAC)
	rbacMgr, err := rbac.NewManager(rbacFile, nil)
	if err != nil {
		t.Fatal(err)
	}
	state, err := statestore.NewDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	tasks := async.NewManager(1)
	t.Cleanup(func() { _ = tasks.Close() })
	fake := testutil.StartFakeGenerator(t)
	writer := gitopstest.NewWriter(dir, "")
	writer.SetValidator(generator.NewGate(generator.NewClient(fake.Socket), ""))
	deps := &handler.Deps{ConfigDir: dir, Writer: writer, State: state, RBAC: rbacMgr,
		Groups: groups.NewDirManager(dir), Tasks: tasks, WriteMode: mode,
		WriteTimeoutDur: 20 * time.Second}
	if mode == handler.WriteModePR {
		deps.PRClient = matrixPRClient{}
		deps.PRTracker = matrixTracker{}
	}
	router := buildRouter(routerDeps{Deps: deps, RBAC: rbacMgr, Identity: rbacMgr.HeaderResolver(),
		Events:    func(http.ResponseWriter, *http.Request) {},
		RateLimit: func(next http.Handler) http.Handler { return next }})
	return &matrixFixture{dir: dir, deps: deps, router: router, fake: fake}
}

// send serves one request as caller; an async answer (202) is followed to
// its task, whose JSON is returned as the body.
func (f *matrixFixture) send(t *testing.T, method, path, body, caller string) (int, string) {
	t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewBufferString(body))
	if strings.HasPrefix(body, "{") {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", caller)
	w := httptest.NewRecorder()
	f.router.ServeHTTP(w, req)
	if w.Code != http.StatusAccepted {
		return w.Code, w.Body.String()
	}
	var acc struct {
		TaskID string `json:"task_id"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &acc); err != nil || acc.TaskID == "" {
		t.Fatalf("202 without task_id: %s", w.Body.String())
	}
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if task, ok := f.deps.Tasks.Get(acc.TaskID); ok &&
			(task.Status == async.TaskCompleted || task.Status == async.TaskFailed) {
			raw, _ := json.Marshal(task)
			return w.Code, string(raw)
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("task %s did not finish", acc.TaskID)
	return 0, ""
}

// matrixPath is one way a write reaches the generator: route is its
// generatorRoutes key; run sends it as caller.
type matrixPath struct {
	name, route string
	mode        handler.WriteMode
	async       bool
	run         func(t *testing.T, f *matrixFixture, caller string) (int, string)
}

func logMatrixPaths() []matrixPath {
	const put = "tenants:\n  t-a:\n    mysql_connections: \"72\"\n"
	tenantOps := func(caller string) string {
		if caller == callerOne {
			return `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}}]}`
		}
		return `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-b","patch":{"_silent_mode":"critical"}}]}`
	}
	group := func(caller string) string {
		if caller == callerOne {
			return "g-one"
		}
		return "g-ok"
	}
	putRun := func(t *testing.T, f *matrixFixture, caller string) (int, string) {
		return f.send(t, "PUT", "/api/v1/tenants/t-a", put, caller)
	}
	tenantBatch := func(q string) func(t *testing.T, f *matrixFixture, caller string) (int, string) {
		return func(t *testing.T, f *matrixFixture, caller string) (int, string) {
			return f.send(t, "POST", "/api/v1/tenants/batch"+q, tenantOps(caller), caller)
		}
	}
	groupBatch := func(q string) func(t *testing.T, f *matrixFixture, caller string) (int, string) {
		return func(t *testing.T, f *matrixFixture, caller string) (int, string) {
			return f.send(t, "POST", "/api/v1/groups/"+group(caller)+"/batch"+q, `{"patch":{"_silent_mode":"warning"}}`, caller)
		}
	}
	return []matrixPath{
		{"direct/PUT", "PUT /api/v1/tenants/{id}/", handler.WriteModeDirect, false, putRun},
		{"direct/custom-alerts", "PUT /api/v1/tenants/{id}/custom-alerts", handler.WriteModeDirect, false,
			func(t *testing.T, f *matrixFixture, caller string) (int, string) {
				body := `{"base_hash":"` + cfg.ComputeSourceHash([]byte(matrixTree()["t-a.yaml"])) +
					`","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
				return f.send(t, "PUT", "/api/v1/tenants/t-a/custom-alerts", body, caller)
			}},
		{"direct/tenant batch sync", "POST /api/v1/tenants/batch", handler.WriteModeDirect, false, tenantBatch("")},
		{"direct/tenant batch async", "POST /api/v1/tenants/batch", handler.WriteModeDirect, true, tenantBatch("?async=true")},
		{"direct/group batch sync", "POST /api/v1/groups/{id}/batch", handler.WriteModeDirect, false, groupBatch("")},
		{"direct/group batch async", "POST /api/v1/groups/{id}/batch", handler.WriteModeDirect, true, groupBatch("?async=true")},
		{"direct/validate", "POST /api/v1/tenants/{id}/validate", handler.WriteModeDirect, false,
			func(t *testing.T, f *matrixFixture, caller string) (int, string) {
				return f.send(t, "POST", "/api/v1/tenants/t-a/validate", put, caller)
			}},
		{"pr/PUT", "PUT /api/v1/tenants/{id}/", handler.WriteModePR, false, putRun},
		{"pr/tenant batch", "POST /api/v1/tenants/batch", handler.WriteModePR, false, tenantBatch("")},
		{"pr/group batch", "POST /api/v1/groups/{id}/batch", handler.WriteModePR, false, groupBatch("")},
	}
}

// The canaries. Test strings, not credentials.
const canary = "CANARY"

func canaryOf(field string) string { return canary + "-" + field }

// kvFinding is one key verdict finding in the service's shape.
func kvFinding(kind, key, file string, tenants []string, reason string, blocks bool) map[string]any {
	return map[string]any{"severity": "error", "kind": kind, "key": key, "file": file,
		"tenants": tenants, "reason": reason, "blocks": blocks}
}

// otherKVFindings are the key verdict findings a single-tenant caller may
// not see (withheld > 0): a platform file's, another tenant's in the
// written file.
func otherKVFindings() []map[string]any {
	return []map[string]any{
		kvFinding("unread", "_"+canaryOf("kv-key-platform"), "_defaults.yaml",
			[]string{canaryOf("kv-tenant-platform")}, canaryOf("kv-reason-platform"), false),
		kvFinding("disable_noop", "_"+canaryOf("kv-key-other"), "t-a.yaml",
			[]string{"t-zz-" + strings.ToLower(canary)}, canaryOf("kv-reason-other"), false),
		kvFinding("unread", "_"+canaryOf("kv-key-nofile"), "", nil, canaryOf("kv-reason-nofile"), false),
	}
}

// baseNotJudged answers the write with findings and the judgement of the
// base alone (no writes) with status and body.
func baseNotJudged(status int, body map[string]any) func(int, testutil.FakeValidateRequest) (int, any) {
	return func(_ int, req testutil.FakeValidateRequest) (int, any) {
		if len(req.Writes) == 0 {
			return status, body
		}
		return 200, testutil.FindingsVerdict(req, "/w", testutil.Finding("route", "t-a", "t-a.yaml", "m"))
	}
}

// matrixAnswer is one answer of the fake, with what it must lead to.
type matrixAnswer struct {
	name   string
	answer func(n int, req testutil.FakeValidateRequest) (int, any)
	// passes: the write succeeds (2xx; /validate: judged) — the key verdict
	// advice path.
	passes bool
}

func logMatrixAnswers() []matrixAnswer {
	withKV := func(req testutil.FakeValidateRequest, kv map[string]any) map[string]any {
		v := testutil.PassVerdict(req, nil)
		v["message"] = canaryOf("verdict-message")
		v["stderr_tail"] = canaryOf("verdict-stderr")
		v["key_verdict"] = kv
		return v
	}
	return []matrixAnswer{
		{name: "findings", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			own := testutil.Finding("credential_plaintext", "t-a", "t-a.yaml", "t-a: "+canaryOf("finding-message"))
			own["field"] = "tenants.t-a._routing." + canaryOf("finding-field")
			own["policy"] = canaryOf("finding-policy")
			other := testutil.Finding("route", canaryOf("finding-tenant"), canaryOf("finding-file")+".yaml",
				canaryOf("finding-message-other"))
			odd := testutil.Finding(canaryOf("finding-kind"), "t-a", "t-a.yaml", "m")
			odd["severity"] = canaryOf("finding-severity")
			v := testutil.FindingsVerdict(req, "/w/"+canaryOf("config-dir"), own, other, odd)
			v["stderr_tail"] = "WARN: " + canaryOf("stderr-tail") + "\n"
			v["message"] = canaryOf("verdict-message")
			return 200, v
		}},
		{name: "findings, the base not judged: 503", answer: baseNotJudged(503, map[string]any{
			"error": canaryOf("base-503-code"), "reason": canaryOf("base-503-reason"), "message": canaryOf("base-503-message")})},
		{name: "findings, the base not judged: 400", answer: baseNotJudged(400, map[string]any{
			"error": canaryOf("base-400-code"), "message": canaryOf("base-400-message")})},
		{name: "findings, the base not judged: rejected", answer: baseNotJudged(200, map[string]any{
			"result": "rejected", "reason": canaryOf("base-rejected-reason"), "message": canaryOf("base-rejected-message"),
			"rc": nil, "findings": nil, "stderr_tail": canaryOf("base-rejected-stderr"), "validated": nil})},
		{name: "key verdict advice: withheld and truncated", passes: true,
			answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
				odd := kvFinding(canaryOf("kv-kind"), "_k", "_defaults.yaml", []string{"t-zz"}, "r", false)
				odd["severity"] = canaryOf("kv-severity")
				findings := append(otherKVFindings(), odd, kvFinding("unread", "_"+canaryOf("kv-key-own"), "t-a.yaml",
					[]string{"t-a"}, canaryOf("kv-reason-own"), false))
				return 200, withKV(req, map[string]any{"status": "complete", "tenants": 2, "seconds": 0.1,
					"message": canaryOf("kv-message"), "truncated": 3, "findings": findings})
			}},
		{name: "key verdict blocks", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			findings := append(otherKVFindings(), kvFinding("unread", "_"+canaryOf("kv-key-block"), "t-a.yaml",
				[]string{"t-a"}, canaryOf("kv-reason-block"), true))
			return 200, withKV(req, map[string]any{"status": "complete", "tenants": 2, "seconds": 0.1,
				"message": canaryOf("kv-message"), "truncated": 2, "findings": findings})
		}},
		{name: "key verdict not completed, unknown reason", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			return 200, withKV(req, map[string]any{"status": "not_completed", "reason": canaryOf("kv-reason"),
				"message": canaryOf("kv-message"), "tenants": 0, "seconds": 0.0, "findings": []any{}, "truncated": 0})
		}},
		{name: "key verdict status outside the vocabulary", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			return 200, withKV(req, map[string]any{"status": canaryOf("kv-status"), "reason": "memory",
				"message": canaryOf("kv-message"), "tenants": 0, "seconds": 0.0, "findings": []any{}, "truncated": 0})
		}},
		{name: "judged another base commit", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			v := testutil.PassVerdict(req, nil)
			v["validated"].(map[string]any)["base_commit"] = canaryOf("base-commit")
			return 200, v
		}},
		{name: "result outside the vocabulary", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			v := testutil.PassVerdict(req, nil)
			v["result"] = canaryOf("result")
			return 200, v
		}},
		{name: "key verdict not completed, memory", answer: func(_ int, req testutil.FakeValidateRequest) (int, any) {
			return 200, withKV(req, map[string]any{"status": "not_completed", "reason": "memory",
				"message": canaryOf("kv-message"), "tenants": 0, "seconds": 0.0, "findings": []any{}, "truncated": 0})
		}},
		{name: "rejected, memory", answer: func(int, testutil.FakeValidateRequest) (int, any) {
			v := testutil.RejectedVerdict("memory")
			v["message"] = canaryOf("rejected-message")
			v["stderr_tail"] = canaryOf("rejected-stderr")
			return 200, v
		}},
		{name: "rejected, unknown reason", answer: func(int, testutil.FakeValidateRequest) (int, any) {
			v := testutil.RejectedVerdict("memory")
			v["reason"] = canaryOf("rejected-reason")
			v["message"] = canaryOf("rejected-message")
			return 200, v
		}},
		{name: "unavailable", answer: func(int, testutil.FakeValidateRequest) (int, any) {
			return 503, map[string]any{"error": canaryOf("unavailable-code"), "reason": canaryOf("unavailable-reason"),
				"message": canaryOf("unavailable-message")}
		}},
		{name: "unavailable, known reason", answer: func(int, testutil.FakeValidateRequest) (int, any) {
			return 503, map[string]any{"error": generator.CodeValidatorUnavailable, "reason": generator.ReasonDaGuardMissing,
				"message": canaryOf("unavailable-message")}
		}},
		{name: "too large", answer: func(int, testutil.FakeValidateRequest) (int, any) {
			return 413, map[string]any{"error": canaryOf("413-error"), "message": canaryOf("413-message")}
		}},
		{name: "bad request", answer: func(int, testutil.FakeValidateRequest) (int, any) {
			return 400, map[string]any{"error": canaryOf("400-error"), "reason": canaryOf("400-reason"),
				"message": canaryOf("400-message")}
		}},
	}
}

// syncLogBuffer is a log sink the parallel subtests share.
type syncLogBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncLogBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncLogBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func TestGeneratorLogMatrix_NoGeneratorTextInTheLog(t *testing.T) {
	logs := &syncLogBuffer{}
	orig := slog.Default()
	defer slog.SetDefault(orig)
	slog.SetDefault(slog.New(slog.NewJSONHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})))
	// The Writer refuses a host git config that names a driver (#3175; a
	// git-lfs host has filter.lfs.*).
	restoreGitEnv := matrixGitEnv(t)
	defer restoreGitEnv()

	rows := 0
	t.Run("writes", func(t *testing.T) {
		for _, a := range logMatrixAnswers() {
			for _, p := range logMatrixPaths() {
				for _, caller := range []string{callerTree, callerOne} {
					rows++
					t.Run(a.name+"/"+p.name+"/"+caller, func(t *testing.T) {
						t.Parallel()
						f := newMatrixFixture(t, p.mode)
						f.fake.SetValidate(a.answer)
						code, body := p.run(t, f, caller)
						// Positive control: the write reached the generator,
						// and nothing before it refused the caller.
						if len(f.fake.Requests()) == 0 {
							t.Fatalf("the generator was never asked (status %d): %s", code, body)
						}
						if code == http.StatusUnauthorized || code == http.StatusForbidden || code == http.StatusNotFound {
							t.Fatalf("refused before the generator: status %d: %s", code, body)
						}
						if !a.passes {
							return
						}
						if code >= 300 {
							t.Fatalf("a passing verdict answered %d: %s", code, body)
						}
						// The advice path ran: the caller who sees the tree
						// is told about the findings and the ones not sent;
						// the other is told only that there are some — the
						// branch that logs (withheld > 0).
						if p.async {
							return // the task keeps per-op results, not the advice
						}
						if caller == callerTree {
							if !strings.Contains(body, canaryOf("kv-key-platform")) || !strings.Contains(body, "the generator did not send") {
								t.Errorf("the tree-visible caller is not shown the key verdict: %s", body)
							}
						} else if !strings.Contains(body, "you cannot view") || strings.Contains(body, canaryOf("kv-key-platform")) {
							t.Errorf("the single-tenant caller is not told of withheld findings (or is shown them): %s", body)
						}
					})
				}
			}
		}
	})

	// GET /metrics reads the container's status and /metrics at scrape time.
	t.Run("metrics", func(t *testing.T) { logMatrixMetrics(t) })

	// The generator's errors wrapped with %w, as a write path may hand them
	// over: a wrapper's Error() carries the container's message.
	t.Run("wrapped", func(t *testing.T) {
		for _, e := range []error{
			&generator.RejectedError{Reason: generator.ReasonMemory, Message: canaryOf("wrapped-rejected")},
			&generator.UnavailableError{Code: generator.CodeValidatorUnavailable, Reason: generator.ReasonInternalError,
				Message: canaryOf("wrapped-unavailable")},
		} {
			req := httptest.NewRequest("PUT", "/api/v1/tenants/t-a", nil)
			w := httptest.NewRecorder()
			if !handler.WriteKindError(w, req, fmt.Errorf("write: %w", e), "", errkind.GeneratorRejected,
				errkind.GeneratorRejectedRetryable, errkind.ValidatorUnavailable, errkind.PolicyUnavailable) {
				t.Fatalf("%T was not answered as the generator's", e)
			}
		}
	})

	out := logs.String()
	if strings.Contains(out, canary) {
		var lines []string
		for _, l := range strings.Split(out, "\n") {
			if strings.Contains(l, canary) {
				lines = append(lines, l)
			}
		}
		t.Errorf("the log quotes the generator's text (%d line(s)):\n%s", len(lines), strings.Join(lines, "\n"))
	}
	// Positive control: the matrix logged its refusals and advice, by their
	// structured fields.
	for _, want := range []string{"generator: write refused with findings", "key verdict: findings withheld from the caller",
		"generator: write refused, key verdict not completed", "write refused: the generator gave no verdict",
		"write refused: the generator container cannot judge", "batch op refused: the generator gave no verdict",
		"generator status not read", "generator: base not judged for the pre-existing marks",
		// The container's "structured" strings, by their closed form.
		`"problem":"base_commit_differs"`, `"status":"other"`, `"other=1"`} {
		if !strings.Contains(out, want) {
			t.Errorf("no %q line in the log", want)
		}
	}
	t.Logf("%d write rows (answers × paths × callers) and the /metrics rows", rows)
}

// matrixGitEnv applies testutil.DisableGitAutoMaintenance and returns what
// puts the environment back.
func matrixGitEnv(t *testing.T) func() {
	t.Helper()
	saved := map[string]*string{}
	for _, k := range []string{"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"} {
		if v, ok := os.LookupEnv(k); ok {
			saved[k] = &v
		} else {
			saved[k] = nil
		}
	}
	cleanup, err := testutil.DisableGitAutoMaintenance()
	if err != nil {
		t.Fatal(err)
	}
	return func() {
		for k, v := range saved {
			if v == nil {
				_ = os.Unsetenv(k)
			} else {
				_ = os.Setenv(k, *v)
			}
		}
		cleanup()
	}
}

// logMatrixMetrics: the status and /metrics answers carry canaries.
func logMatrixMetrics(t *testing.T) {
	f := newMatrixFixture(t, handler.WriteModeDirect)
	handler.SetGeneratorSource(generator.NewGate(generator.NewClient(f.fake.Socket), ""))
	t.Cleanup(func() { handler.SetGeneratorSource(nil) })
	for _, c := range []struct {
		name    string
		status  func() (int, any)
		metrics func() (int, string)
	}{
		{"status 503, metrics 500",
			func() (int, any) {
				return 503, map[string]any{"error": canaryOf("status-code"), "reason": canaryOf("status-reason"),
					"message": canaryOf("status-message")}
			},
			func() (int, string) { return 500, canaryOf("metrics-500") }},
		{"status not ready with a policy error",
			func() (int, any) {
				return 200, map[string]any{"policy": map[string]any{"configured": true, "available": false,
					"error": canaryOf("policy-error")}, "da_read_available": true, "da_guard_available": true, "ready": false}
			},
			nil},
		{"status not JSON",
			func() (int, any) { return 200, canaryOf("status-not-an-object") },
			nil},
	} {
		f.fake.SetStatus(c.status)
		f.fake.SetMetrics(c.metrics)
		code, body := f.send(t, "GET", "/metrics", "", "")
		if code != http.StatusOK || !strings.Contains(body, "tenant_api_policy_available 0") {
			t.Errorf("%s: /metrics = %d, want 200 with tenant_api_policy_available 0:\n%s", c.name, code, body)
		}
	}
}

// noGeneratorRequests: one request per noGeneratorRoutes entry, keyed by it.
var noGeneratorRequests = map[string]struct{ method, path, body string }{
	"PUT /api/v1/tenants/{id}/federation":                   {"PUT", "/api/v1/tenants/t-a/federation", `{"metrics":["up"]}`},
	"POST /api/v1/tenants/{id}/diff":                        {"POST", "/api/v1/tenants/t-a/diff", "tenants:\n  t-a:\n    mysql_connections: \"72\"\n"},
	"POST /api/v1/tenants/{id}/recipe-preview":              {"POST", "/api/v1/tenants/t-a/recipe-preview", `{"recipe":{"recipe":"threshold","metric":"m","threshold":"1:warning"},"scenario":{"value":5}}`},
	"POST /api/v1/tenants/simulate":                         {"POST", "/api/v1/tenants/simulate", "tenants:\n  t-a:\n    mysql_connections: \"72\"\n"},
	"PUT /api/v1/groups/{id}/":                              {"PUT", "/api/v1/groups/g-ok", `{"label":"OK2","members":["t-a","t-b"]}`},
	"DELETE /api/v1/groups/{id}/":                           {"DELETE", "/api/v1/groups/g-one", ""},
	"PUT /api/v1/views/{id}/":                               {"PUT", "/api/v1/views/v1", `{"label":"x","filters":{"a":"b"}}`},
	"DELETE /api/v1/views/{id}/":                            {"DELETE", "/api/v1/views/v1", ""},
	"POST /api/v1/federation/tokens/":                       {"POST", "/api/v1/federation/tokens", `{"tenant_id":"t-a"}`},
	"DELETE /api/v1/federation/tokens/{id}":                 {"DELETE", "/api/v1/federation/tokens/x", ""},
	"PUT /api/v1/federation/policy/":                        {"PUT", "/api/v1/federation/policy", `{"whitelist":[]}`},
	"POST /api/v1/federation/accounts/backfill":             {"POST", "/api/v1/federation/accounts/backfill", ""},
	"POST /api/v1/audit/tenants/{id}/access-report/dry-run": {"POST", "/api/v1/audit/tenants/t-a/access-report/dry-run", matrixRBAC},
}

// noGeneratorRoutes is a claim, so each entry is sent through the router
// and the fake generator must stay unasked (whatever the route answers).
// Not t.Parallel(): matrixGitEnv sets the process environment.
func TestNoGeneratorRoutesNeverAsk(t *testing.T) {
	restore := matrixGitEnv(t)
	defer restore()
	for key := range noGeneratorRoutes {
		if _, ok := noGeneratorRequests[key]; !ok {
			t.Errorf("noGeneratorRoutes entry %q has no request in noGeneratorRequests", key)
		}
	}
	for key, c := range noGeneratorRequests {
		if _, ok := noGeneratorRoutes[key]; !ok {
			t.Errorf("noGeneratorRequests entry %q is not in noGeneratorRoutes", key)
		}
		for _, mode := range []handler.WriteMode{handler.WriteModeDirect, handler.WriteModePR} {
			f := newMatrixFixture(t, mode)
			code, _ := f.send(t, c.method, c.path, c.body, callerTree)
			if n := len(f.fake.Requests()); n > 0 {
				t.Errorf("%s (%s, mode %v → %d) asked the generator %d time(s)", key, c.path, mode, code, n)
			}
		}
	}
}
