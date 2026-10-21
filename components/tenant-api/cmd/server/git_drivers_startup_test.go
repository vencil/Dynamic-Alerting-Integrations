package main

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// #3175: main refuses to start while the git config it runs under names a
// filter, diff or merge driver program, naming scope and key but not the
// value. The key comes in through GIT_CONFIG_COUNT (scope "command"), in
// the child's environment only; the gitops tests cover every scope and key.
func TestServerRefusesGitDriverConfigAtStartup(t *testing.T) {
	if os.Getenv("GITDRIVER_CRASHER") == "1" {
		os.Args = []string{"tenant-api"}
		main()
		return
	}
	const secret = "SECRET-VALUE-do-not-print"
	// Bounded: without the check main() goes on to serve.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestServerRefusesGitDriverConfigAtStartup$")
	env := []string{"GITDRIVER_CRASHER=1"}
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "TA_") && !strings.HasPrefix(kv, "GIT_CONFIG_COUNT=") &&
			!strings.HasPrefix(kv, "GIT_CONFIG_KEY_") && !strings.HasPrefix(kv, "GIT_CONFIG_VALUE_") {
			env = append(env, kv)
		}
	}
	cmd.Env = append(env,
		"TA_CONFIG_DIR="+t.TempDir(),
		"TA_GENERATOR_SOCKET=/nonexistent/generator.sock",
		"TA_ADDR=127.0.0.1:0",
		"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=filter.x.clean", "GIT_CONFIG_VALUE_0="+secret)
	out, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		t.Fatalf("main() was still running after 30s: it started instead of refusing. Output:\n%s", out)
	}
	if err == nil {
		t.Fatalf("main() exited 0; a driver in the git config must refuse the start. Output:\n%s", out)
	}
	for _, want := range []string{"command filter.x.clean", "#3175"} {
		if !strings.Contains(string(out), want) {
			t.Errorf("startup failure does not mention %q. Got:\n%s", want, out)
		}
	}
	if strings.Contains(string(out), secret) {
		t.Errorf("startup failure prints the value. Got:\n%s", out)
	}
}
