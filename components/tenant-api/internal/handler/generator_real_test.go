package handler

// #2903: tenant-api's real write path against the REAL generator service
// (scripts/tools/ops/generator_service.py, the code the da-tools image runs),
// started here on a Unix socket over the test's git repository, with a stub
// amtool on its PATH — a pass commits, a finding refuses with nothing
// written. The unit tests (generator_gate_test.go) use a fake service; this
// one proves the two sides agree on the wire.
//
// Needs python3 with PyYAML (the CI tenant-api leg installs it). Without
// them the test skips locally and FAILS under CI, so a runner that lost
// them cannot pass by skipping.

import (
	"context"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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
	cmd.Env = append(os.Environ(), "PATH="+bin+string(os.PathListSeparator)+os.Getenv("PATH"))
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
