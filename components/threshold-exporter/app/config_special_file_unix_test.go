//go:build unix

package main

// #2939: a FIFO named like a config file used to block the exporter's walk
// forever — the reload tick never returned, so neither the tree nor any
// metric moved again. Now the walk does not open it: it is
// da_config_unreadable_files{reason="special_file"} (and, for a `_defaults`
// file, da_config_defaults_unusable{reason="unreadable"}), and every other
// tenant keeps reloading.
//
// Seams: freshMetrics + SetMetrics and a discarding logger per test
// (newUnusableTreeManager), so the test runs in parallel.

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestTickOnce_FIFO_CountedAsSpecialFileAndDoesNotBlock(t *testing.T) {
	t.Parallel()
	for _, hier := range []bool{false, true} {
		hier := hier
		t.Run(modeName(hier), func(t *testing.T) {
			t.Parallel()
			m, fresh, dir := newUnusableTreeManager(t, hier)
			if err := os.MkdirAll(filepath.Join(dir, "fifo-team"), 0o755); err != nil {
				t.Fatal(err)
			}
			for _, rel := range []string{"tenant-c.yaml", "fifo-team/_defaults.yaml"} {
				p := filepath.Join(dir, filepath.FromSlash(rel))
				if err := syscall.Mkfifo(p, 0o600); err != nil {
					t.Skipf("mkfifo: %v", err)
				}
				// Unblock a reader a failing run left on it.
				t.Cleanup(func() {
					if f, err := os.OpenFile(p, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
						_ = f.Close()
					}
				})
			}
			done := make(chan struct{})
			go func() {
				defer close(done)
				m.tickOnce()
			}()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				t.Fatal("reload tick blocked reading a FIFO")
			}
			g, ok := fresh.unreadableFiles["special_file"]
			if !ok {
				t.Fatalf("da_config_unreadable_files has no special_file series (reasons: %v)", unreadableFileReasons)
			}
			if got := testutil.ToFloat64(g); got != 2 {
				t.Errorf("unreadable_files{special_file} = %v, want 2", got)
			}
			if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonUnreadable); got != 1 {
				t.Errorf("defaults_unusable{unreadable} = %v, want 1 (fifo-team/_defaults.yaml)", got)
			}
			if cfg := m.GetConfig(); cfg == nil || cfg.Tenants["tenant-b"] == nil {
				t.Errorf("tenant-b no longer served after the tick")
			}
		})
	}
}
