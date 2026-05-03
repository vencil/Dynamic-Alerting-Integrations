package configwatcher

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// testConfig is a tiny payload type for exercising the generic.
type testConfig struct {
	Items map[string]string `yaml:"items"`
}

func parseTestConfig(data []byte) (*testConfig, error) {
	// Minimal hand-rolled "yaml" — line-based key:value to avoid
	// depending on yaml.v3 inside this package's test binary.
	cfg := &testConfig{Items: map[string]string{}}
	for _, line := range splitLines(string(data)) {
		k, v, ok := splitOnce(line, ": ")
		if !ok {
			continue
		}
		cfg.Items[k] = v
	}
	return cfg, nil
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i, c := range s {
		if c == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}

func splitOnce(s, sep string) (string, string, bool) {
	for i := 0; i+len(sep) <= len(s); i++ {
		if s[i:i+len(sep)] == sep {
			return s[:i], s[i+len(sep):], true
		}
	}
	return "", "", false
}

func emptyTestConfig() *testConfig {
	return &testConfig{Items: map[string]string{}}
}

func writeYAML(t *testing.T, dir, body string) string {
	t.Helper()
	p := filepath.Join(dir, "test.yaml")
	if err := os.WriteFile(p, []byte(body), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
	return p
}

func TestNew_EmptyPath(t *testing.T) {
	w, err := New("", "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New(\"\"): %v", err)
	}
	if w.Get() == nil {
		t.Fatal("Get() returned nil for empty-path watcher")
	}
	if got := len(w.Get().Items); got != 0 {
		t.Errorf("expected empty Items, got %d", got)
	}
}

func TestNew_FileMissing_TreatedAsEmpty(t *testing.T) {
	w, err := New(filepath.Join(t.TempDir(), "nope.yaml"), "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("missing file should not error, got: %v", err)
	}
	if got := len(w.Get().Items); got != 0 {
		t.Errorf("expected empty Items for missing file, got %d", got)
	}
}

func TestReload_PicksUpFileChange(t *testing.T) {
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: bar\n")

	w, err := New(path, "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if got := w.Get().Items["foo"]; got != "bar" {
		t.Fatalf("initial foo = %q, want bar", got)
	}

	if err := os.WriteFile(path, []byte("foo: baz\nextra: hello\n"), 0644); err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if err := w.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if got := w.Get().Items["foo"]; got != "baz" {
		t.Errorf("after reload foo = %q, want baz", got)
	}
	if got := w.Get().Items["extra"]; got != "hello" {
		t.Errorf("after reload extra = %q, want hello", got)
	}
}

func TestReload_HashSkipsUnchanged(t *testing.T) {
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: bar\n")

	w, err := New(path, "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	hashBefore := w.LastHash()
	if hashBefore == "" {
		t.Fatal("expected non-empty hash after initial load")
	}
	// Reload clears the hash internally, so this re-reads + re-stores
	// (same content → same hash).
	if err := w.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if hashAfter := w.LastHash(); hashAfter != hashBefore {
		t.Errorf("hash changed across same-content reload: before=%q after=%q",
			hashBefore, hashAfter)
	}
}

func TestOverride_StoresValueAndClearsHash(t *testing.T) {
	w, _ := New("", "test", parseTestConfig, emptyTestConfig)

	override := &testConfig{Items: map[string]string{"injected": "value"}}
	w.Override(override)

	if got := w.Get().Items["injected"]; got != "value" {
		t.Errorf("after Override, Get()['injected'] = %q, want value", got)
	}
	if w.LastHash() != "" {
		t.Error("Override should clear the hash so next disk-load runs")
	}
}

func TestNewForTest_NoIO(t *testing.T) {
	cfg := &testConfig{Items: map[string]string{"a": "1"}}
	w := NewForTest("test", cfg)

	// Get returns the supplied config.
	if got := w.Get().Items["a"]; got != "1" {
		t.Errorf("Get()['a'] = %q, want 1", got)
	}
	// Reload is a no-op (no path).
	if err := w.Reload(); err != nil {
		t.Errorf("Reload on test watcher: %v", err)
	}
	// WatchLoop returns immediately.
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		w.WatchLoop(time.Hour, stop)
		close(done)
	}()
	close(stop)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Error("WatchLoop did not exit promptly on empty-path test watcher")
	}
}

func TestWatchLoop_PicksUpChangeOnTick(t *testing.T) {
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: v1\n")
	w, err := New(path, "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.WatchLoop(20*time.Millisecond, stop)
	}()

	// Rewrite + wait for at least one tick.
	if err := os.WriteFile(path, []byte("foo: v2\n"), 0644); err != nil {
		t.Fatalf("rewrite: %v", err)
	}

	// Poll for up to 1s — flake guard for slow CI.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if w.Get().Items["foo"] == "v2" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	close(stop)
	wg.Wait()

	if got := w.Get().Items["foo"]; got != "v2" {
		t.Errorf("WatchLoop did not pick up change: foo = %q, want v2", got)
	}
}
