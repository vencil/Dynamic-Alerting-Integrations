package handler

// #2903: tenant-api's real write path against the REAL generator service
// (scripts/tools/ops/generator_service.py, the code the da-tools image runs),
// started here on a Unix socket over the test's git repository, with a stub
// amtool on its PATH — a pass commits, a finding refuses with nothing
// written. The unit tests (generator_gate_test.go) use a fake service; this
// one proves the two sides agree on the wire.
//
// Needs python3 with PyYAML (the CI tenant-api leg installs it) and a
// da-guard: the generator reads conf.d through `da-guard parse-tree`
// (ADR-036 step 4) — $DA_GUARD_BINARY, else built once from this checkout's
// threshold-exporter source (daGuardBinary). Without them the test skips
// locally and FAILS under CI, so a runner that lost them cannot pass by
// skipping.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/generator"
)

// realGeneratorScript finds scripts/tools/ops/generator_service.py above the
// test's directory.
func realGeneratorScript(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		p := filepath.Join(dir, "scripts", "tools", "ops", "generator_service.py")
		if _, err := os.Stat(p); err == nil {
			return p
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("scripts/tools/ops/generator_service.py not found above the test directory")
		}
		dir = parent
	}
}

var (
	daGuardOnce sync.Once
	daGuardPath string
	daGuardErr  string
)

// daGuardBinary is the da-guard the Python generator runs: $DA_GUARD_BINARY
// when set (a relative path is relative to the repository root — `go test`
// runs in the package directory, so it cannot mean the shell's directory),
// else one `go build` of components/threshold-exporter/app's cmd/da-guard
// per test binary (into a directory removed with the process's temp files).
// requireOrSkip when neither works.
func daGuardBinary(t *testing.T) string {
	t.Helper()
	daGuardOnce.Do(func() {
		root := filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(realGeneratorScript(t)))))
		if p := strings.TrimSpace(os.Getenv("DA_GUARD_BINARY")); p != "" {
			if !filepath.IsAbs(p) {
				p = filepath.Join(root, p)
			}
			daGuardPath = p
			return
		}
		dir, err := os.MkdirTemp("", "da-guard-bin")
		if err != nil {
			daGuardErr = err.Error()
			return
		}
		out := filepath.Join(dir, "da-guard")
		cmd := exec.Command("go", "build", "-buildvcs=false", "-o", out, "./cmd/da-guard")
		cmd.Dir = filepath.Join(root, "components", "threshold-exporter", "app")
		// #3174: the shipped shape (CGO_ENABLED=0, as build.sh / release.yaml).
		// A cgo build gives every OS thread an RLIMIT_STACK-sized pthread
		// stack that counts against the judgement's RLIMIT_DATA, so the
		// generator's memory limit would measure thread count, not the
		// judgement (tests/_da_guard_build.py).
		cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
		if b, err := cmd.CombinedOutput(); err != nil {
			daGuardErr = "go build da-guard: " + err.Error() + ": " + strings.TrimSpace(string(b))
			return
		}
		daGuardPath = out
	})
	if daGuardPath == "" {
		requireOrSkip(t, "no da-guard for the generator's conf.d read: "+daGuardErr)
	}
	return daGuardPath
}

func requireOrSkip(t *testing.T, why string) {
	t.Helper()
	if os.Getenv("CI") != "" {
		t.Fatalf("%s; CI must run this test", why)
	}
	t.Skip(why)
}

// startRealGenerator runs the service over repo (conf.d is its root, as the
// chart mounts it) with policy as --policy, and returns its socket once its
// status endpoint answers.
func startRealGenerator(t *testing.T, repo, policy string) string {
	t.Helper()
	if out, err := exec.Command("python3", "-c", "import yaml").CombinedOutput(); err != nil {
		requireOrSkip(t, "python3 with PyYAML is not available: "+strings.TrimSpace(string(out)))
	}
	script := realGeneratorScript(t)
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "amtool"), []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	sockDir, err := os.MkdirTemp("", "rg")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(sockDir) })
	sock := filepath.Join(sockDir, "g.sock")
	cmd := exec.Command("python3", script, "--socket", sock, "--repo", repo, "--config-subdir", ".",
		"--work-dir", filepath.Join(t.TempDir(), "work"), "--policy", policy, "--warm-ref", "")
	cmd.Env = append(os.Environ(), "PATH="+bin+string(os.PathListSeparator)+os.Getenv("PATH"),
		"DA_GUARD_BINARY="+daGuardBinary(t))
	var logs strings.Builder
	cmd.Stdout, cmd.Stderr = &logs, &logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			_ = cmd.Process.Kill()
			<-done
		}
		if t.Failed() {
			t.Logf("generator service output:\n%s", logs.String())
		}
	})
	client := generator.NewClient(sock)
	deadline := time.Now().Add(30 * time.Second)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		st, err := client.Status(ctx)
		cancel()
		if err == nil && st.Ready {
			return sock
		}
		if time.Now().After(deadline) {
			t.Fatalf("the generator service did not become ready: %v %+v", err, st)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func TestRealGenerator_PassCommitsFindingRefuses(t *testing.T) {
	t.Parallel()
	repo := seedGitTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"t-a.yaml":       "tenants:\n  t-a:\n    mysql_connections: \"70\"\n",
	})
	policy := filepath.Join(t.TempDir(), "policy.yaml")
	if err := os.WriteFile(policy, []byte("allowed_domains:\n  - ok.example.com\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	sock := startRealGenerator(t, repo, policy)
	d := &Deps{Writer: newTestWriter(repo), ConfigDir: repo, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}
	d.Writer.SetValidator(generator.NewGate(generator.NewClient(sock), policy))
	head := func() string { return gitRev(t, repo, "HEAD") }

	// pass: committed.
	before := head()
	w := putTenantAs(t, d, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"72\"\n    _routing:\n      receiver:\n"+
		"        type: webhook\n        url: https://ok.example.com/hook\n", "admins", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("pass: status %d body %s", w.Code, w.Body.String())
	}
	if head() == before {
		t.Fatal("pass: nothing committed")
	}

	// findings: a webhook host outside the deployment policy's
	// allowed_domains — a check only the generator makes.
	before = head()
	w = putTenantAs(t, d, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"73\"\n    _routing:\n      receiver:\n"+
		"        type: webhook\n        url: https://evil.example.org/hook\n", "admins", nil)
	m := genEnvelope(t, w)
	if w.Code != http.StatusUnprocessableEntity || m["code"] != CodeGeneratorFindings || m["findings_compared_with_base"] != true {
		t.Fatalf("findings: status %d body %s", w.Code, w.Body.String())
	}
	fs, _ := m["findings"].([]any)
	var found bool
	for _, f := range fs {
		f := f.(map[string]any)
		if f["kind"] == "receiver_domain_not_allowed" && f["tenant"] == "t-a" && f["related"] == true && f["pre_existing"] == false {
			found = true
		}
	}
	if !found {
		t.Errorf("findings: no related, new receiver_domain_not_allowed for t-a: %s", w.Body.String())
	}
	if head() != before {
		t.Error("findings: a commit was made")
	}
	if out, err := exec.Command("git", "-C", repo, "status", "--porcelain").CombinedOutput(); err != nil || len(out) != 0 {
		t.Errorf("findings: worktree not clean: %v %s", err, out)
	}

	// A batch passes as one judgement.
	resp := runBatch(t, repo, d, `[{"tenant_id":"t-a","patch":{"mysql_connections":"74"}},{"tenant_id":"t-b","patch":{"mysql_connections":"75"}}]`)
	if resp.Status != "completed" {
		t.Errorf("batch: %+v", resp)
	}

	// The deployment policy file tenant-api reads is not the one the
	// generator judged with: refused as retryable, nothing written.
	other := filepath.Join(t.TempDir(), "policy.yaml")
	if err := os.WriteFile(other, []byte("allowed_domains:\n  - other.example.com\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	d.Writer.SetValidator(generator.NewGate(generator.NewClient(sock), other))
	before = head()
	w = putTenantAs(t, d, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"76\"\n", "admins", nil)
	if m := genEnvelope(t, w); w.Code != http.StatusTooManyRequests || m["reason"] != "mismatch" {
		t.Errorf("policy mismatch: status %d body %s", w.Code, w.Body.String())
	}
	if head() != before {
		t.Error("policy mismatch: a commit was made")
	}
}

// #2903 PR-B: tenant-api no longer judges routing or the domain policy
// itself; every such refusal is the real generator's finding (422
// GENERATOR_FINDINGS), never 403 POLICY_VIOLATION, the old 400 receiver /
// routing checks, or a 503 of tenant-api's own policy reader.
func TestRealGenerator_RoutingAndPolicyAreTheGenerators(t *testing.T) {
	t.Parallel()
	const slack = "        type: slack\n        api_url: https://hooks.example.com/x\n"
	repo := seedGitTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: [t-a]\n" +
			"    constraints:\n      forbidden_receiver_types: [slack]\n",
		"t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n",
	})
	policy := filepath.Join(t.TempDir(), "policy.yaml")
	if err := os.WriteFile(policy, []byte("allowed_domains: []\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	sock := startRealGenerator(t, repo, policy)
	d := &Deps{Writer: newTestWriter(repo), ConfigDir: repo, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}
	d.Writer.SetValidator(generator.NewGate(generator.NewClient(sock), policy))

	kinds := func(w *httptest.ResponseRecorder) map[string]bool {
		t.Helper()
		m := genEnvelope(t, w)
		if w.Code != http.StatusUnprocessableEntity || m["code"] != CodeGeneratorFindings {
			t.Fatalf("status %d body %s; want 422 %s", w.Code, w.Body.String(), CodeGeneratorFindings)
		}
		out := map[string]bool{}
		fs, _ := m["findings"].([]any)
		for _, f := range fs {
			out[f.(map[string]any)["kind"].(string)] = true
		}
		return out
	}
	for name, c := range map[string]struct{ body, kind string }{
		"domain policy":         {"tenants:\n  t-a:\n    _routing:\n      receiver:\n" + slack, "domain_policy_violation"},
		"receiver shape":        {"tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n", "missing_receiver_field"},
		"routing not a mapping": {"tenants:\n  t-a:\n    _routing: slack\n", "routing_not_mapping"},
		"group_by element": {"tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n" +
			"        url: https://ok.example.com/x\n      group_by: [\"\"]\n", "routing_group_by_invalid"},
		"matcher value": {"tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n" +
			"        url: https://ok.example.com/x\n      routes:\n        - match: {severity: 1}\n" +
			"          receiver: {type: webhook, url: \"https://ok.example.com/y\"}\n", "routing_value_not_string"},
		"key the generator does not read": {"tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n" +
			"        url: https://ok.example.com/x\n      continue: true\n", "routing_key_ignored"},
	} {
		before := gitRev(t, repo, "HEAD")
		if got := kinds(putTenantAs(t, d, "t-a", c.body, "admins", nil)); !got[c.kind] {
			t.Errorf("%s: findings %v, want %s", name, got, c.kind)
		}
		if gitRev(t, repo, "HEAD") != before {
			t.Errorf("%s: a commit was made", name)
		}
	}
	// A batch `_routing` patch that does not disable is merged and judged.
	w := postTenantBatch(t, d, `[{"tenant_id":"t-a","patch":{"_routing":"slack"}}]`)
	var br struct {
		Results []struct {
			Code string `json:"code"`
		} `json:"results"`
		Findings []map[string]any `json:"findings"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &br)
	batchKinds := map[string]bool{}
	for _, f := range br.Findings {
		batchKinds[f["kind"].(string)] = true
	}
	if w.Code != http.StatusUnprocessableEntity || len(br.Results) != 1 || br.Results[0].Code != CodeGeneratorFindings ||
		!batchKinds["routing_not_mapping"] {
		t.Errorf("batch _routing patch: status %d body %s; want 422, %s with routing_not_mapping",
			w.Code, w.Body.String(), CodeGeneratorFindings)
	}
	// The repo's policy file unreadable: the generator's finding, refusing
	// even a threshold-only write — not tenant-api's 503.
	if err := os.WriteFile(filepath.Join(repo, "_domain_policy.yaml"), []byte("domain_policies: [\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGit(t, repo, "commit", "-qam", "break the policy file")
	if got := kinds(putTenantAs(t, d, "t-a", "tenants:\n  t-a:\n    mysql_connections: \"71\"\n", "admins", nil)); !got["domain_policy_unusable"] {
		t.Errorf("unusable policy file: findings %v, want domain_policy_unusable", got)
	}
}
