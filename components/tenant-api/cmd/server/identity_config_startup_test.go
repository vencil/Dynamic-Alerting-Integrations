package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ADR-027 §2, §9 item 6: an oauth2-proxy config file of the wrong shape stops
// the start. proxyconfig's own tests pin which shapes are refused; this pins
// that main acts on the refusal — it exits before opening the API's Unix
// socket instead of logging and serving with headers it cannot attribute.
//
// The child runs main() itself (re-exec of the test binary, as
// TestWirePRBackendFatalOnUnknownMode does) with every flag the identity
// plane needs, so the only thing wrong is the config file.

const identityStartupChild = "IDENTITY_STARTUP_CHILD"

func runMainWithProxyConfig(t *testing.T, fixture string) (out string, exited bool, sock string) {
	t.Helper()
	dir := t.TempDir()
	sock = filepath.Join(dir, "human.sock")
	cfg, err := filepath.Abs(filepath.Join("..", "..", "internal", "proxyconfig", "testdata", fixture))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cfg); err != nil {
		t.Fatalf("fixture %s: %v", fixture, err)
	}
	// A _rbac.yaml that does not parse: the RBAC load right after the
	// identity load is fatal, so even the control run below stops there,
	// on any machine, instead of going on to serve.
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	if err := os.WriteFile(rbacFile, []byte("groups: [unclosed\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestMainRefusesAnAmbiguousOAuth2ProxyConfig$")
	// No TA_* from the parent: an unknown one would stop the start first
	// (checkTAEnv), for the wrong reason.
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "TA_") {
			cmd.Env = append(cmd.Env, kv)
		}
	}
	cmd.Env = append(cmd.Env,
		identityStartupChild+"=1",
		"IDENTITY_STARTUP_ARGS="+strings.Join([]string{
			"--human-socket=" + sock,
			"--generator-socket=" + filepath.Join(dir, "generator.sock"),
			"--oauth2-proxy-config=" + cfg,
			"--config-dir=" + dir,
			"--addr=127.0.0.1:0",
			"--rbac=" + rbacFile,
		}, "\x1f"))
	b, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		t.Fatalf("main with %s did not exit within 30s; it must stop at the identity or RBAC load. Output:\n%s", fixture, b)
	}
	return string(b), err != nil, sock
}

// adr-check: ADR-027/9.6a
func TestMainRefusesAnAmbiguousOAuth2ProxyConfig(t *testing.T) {
	if os.Getenv(identityStartupChild) == "1" {
		os.Args = append([]string{"tenant-api"}, strings.Split(os.Getenv("IDENTITY_STARTUP_ARGS"), "\x1f")...)
		main()
		return // main returning is what the parent reports
	}
	t.Parallel()

	out, exited, sock := runMainWithProxyConfig(t, "missing-email.yaml")
	if !exited {
		t.Fatalf("tenant-api started with an oauth2-proxy config that names no email header; it must refuse. Output:\n%s", out)
	}
	if !strings.Contains(out, "FATAL: --oauth2-proxy-config:") {
		t.Errorf("the refusal does not name --oauth2-proxy-config. Output:\n%s", out)
	}
	// It must STOP there: a refusal that only logs and carries on would reach
	// the identity line and RBAC init (which the malformed _rbac.yaml then
	// stops, hiding the bug behind a non-zero exit).
	for _, later := range []string{"identity headers from", "rbac"} {
		if strings.Contains(strings.ToLower(out), strings.ToLower(later)) {
			t.Errorf("main went on past the refused config (output names %q). Output:\n%s", later, out)
		}
	}
	if _, err := os.Stat(sock); err == nil {
		t.Errorf("the API socket %s exists: main opened the listener before refusing the config", sock)
	}

	// The control: the same start with a well-formed file gets past the
	// identity load (whatever stops it later), so the refusal above is the
	// config's and not an earlier check's.
	out, exited, _ = runMainWithProxyConfig(t, "valid.yaml")
	if !exited {
		t.Fatalf("main with a malformed _rbac.yaml exited 0. Output:\n%s", out)
	}
	if strings.Contains(out, "--oauth2-proxy-config") {
		t.Errorf("a well-formed config is refused too — the check above proves nothing. Output:\n%s", out)
	}
	if !strings.Contains(out, "identity headers from") {
		t.Errorf("with a well-formed config main never reached the identity load. Output:\n%s", out)
	}
}
