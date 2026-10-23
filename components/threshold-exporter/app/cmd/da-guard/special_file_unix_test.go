//go:build unix

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// #2939: every da-guard subcommand reads conf.d through the exporter's walk,
// so a FIFO named like a config file blocked all of them except parse-tree
// (`da-guard served-values` was the reproduction: rc 124 under `timeout`).
// Now each one ends, names the FIFO, and exits as it does for any file the
// exporter cannot read (3; parse-tree reports it as data and exits 0).
// Two shapes: a FIFO tenant file plus a link to a FIFO outside the tree
// (the full walk), and a FIFO root `_defaults.yaml` (the root-only walk the
// guard reads its carrier with).
func TestSubcommands_SpecialFileDoesNotBlock(t *testing.T) {
	t.Parallel()
	shapes := map[string]func(t *testing.T, dir string) []string{
		"tenant file and link": func(t *testing.T, dir string) []string {
			mkfifoOrSkip(t, filepath.Join(dir, "stuck.yaml"))
			outside := filepath.Join(t.TempDir(), "pipe.yaml")
			mkfifoOrSkip(t, outside)
			if err := os.Symlink(outside, filepath.Join(dir, "link.yaml")); err != nil {
				t.Skipf("symlink: %v", err)
			}
			return []string{"stuck.yaml", "link.yaml"}
		},
		"root defaults": func(t *testing.T, dir string) []string {
			if err := os.Remove(filepath.Join(dir, "_defaults.yaml")); err != nil {
				t.Fatal(err)
			}
			mkfifoOrSkip(t, filepath.Join(dir, "_defaults.yaml"))
			return []string{"_defaults.yaml"}
		},
	}
	cmds := map[string]struct {
		args []string
		exit int
	}{
		"guard":                 {nil, exitParseFailed},
		"served-values":         {[]string{servedValuesCmd}, exitParseFailed},
		"effective":             {[]string{effectiveCmd}, exitParseFailed},
		"key-refs":              {[]string{keyRefsCmd, "--metric", "mysql_connections"}, exitParseFailed},
		"compile-custom-alerts": {[]string{compileCustomAlertsCmd}, exitParseFailed},
		"parse-tree":            {[]string{parseTreeCmd}, exitOK},
	}
	for shape, build := range shapes {
		for name, c := range cmds {
			shape, build, name, c := shape, build, name, c
			t.Run(shape+"/"+name, func(t *testing.T) {
				t.Parallel()
				tmp := t.TempDir()
				testutil.WriteTree(t, tmp, map[string]string{
					"conf.d/_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
					"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
				})
				dir := filepath.Join(tmp, "conf.d")
				names := build(t, dir)
				var out, errOut bytes.Buffer
				done := make(chan int, 1)
				go func() { done <- run(append(append([]string{}, c.args...), "--config-dir", dir), &out, &errOut) }()
				var code int
				select {
				case code = <-done:
				case <-time.After(15 * time.Second):
					t.Fatal("blocked reading a FIFO")
				}
				if code != c.exit {
					t.Errorf("exit = %d, want %d; stderr=%q", code, c.exit, errOut.String())
				}
				all := out.String() + errOut.String()
				for _, n := range names {
					if !strings.Contains(all, n) {
						t.Errorf("output does not name %s:\nstdout=%s\nstderr=%s", n, out.String(), errOut.String())
					}
				}
			})
		}
	}
}

// mkfifoOrSkip makes a FIFO at p, skipping where the platform cannot; the
// cleanup opens and closes the write side so a reader a failing run left
// blocked on it returns.
func mkfifoOrSkip(t *testing.T, p string) {
	t.Helper()
	if err := syscall.Mkfifo(p, 0o600); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() {
		if f, err := os.OpenFile(p, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
}
