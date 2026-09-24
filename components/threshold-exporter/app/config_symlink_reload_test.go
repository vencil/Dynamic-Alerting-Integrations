package main

// config_symlink_reload_test.go — #1969: a symlinked conf.d (the K8s
// ConfigMap volume layout, and any hand-made `key -> elsewhere` link) must
// hot-reload when the link's TARGET changes, through the entry points a
// production watch tick uses.
//
// The defect: the walker's mtime fast-path compared the directory entry's
// lstat, i.e. the LINK's own mtime+size. A ConfigMap update swaps `..data`
// and leaves every `key -> ..data/key` link untouched; an in-place edit of a
// link target touches no link either. Both matched the prior, the prior hash
// was carried, and nothing reloaded.
//
// ⛔ Anti-vacuity, two halves:
//   - every link is AGED past config.TreeScanMtimeGuard (testutil.AgeSymlink
//     sets the link's OWN mtime; os.Chtimes follows links and cannot). A
//     young link is re-read by the guard on the defective walker too, and the
//     test would pass on the bug.
//   - the "plain file" layout is the CONTROL: it reloaded before the fix and
//     must keep reloading. It is the row that proves the harness can see a
//     reload at all.
//
// Coverage of the two watch paths (ConfigManager.tickOnce, what WatchLoop
// runs per tick): a tree with a `_defaults.yaml` makes Load() enable the
// hierarchical plane, so the tick is detectChange (per-file hash compare) →
// diffAndReload's hierarchical pipeline; a tree without one stays flat, so
// the tick is detectChange (composite hash) → diffAndReload →
// incrementalLoadFrom. IncrementalLoad is also driven directly (its own
// scan). Each row asserts which plane it is on, so a fixture that silently
// changed plane cannot pass for the other.
//
// Seams: NewConfigManagerWithDebounce(root, 0) makes the tick's debounced
// reload synchronous (no timer); SetMetrics(fresh) + SetLogger(buffer) per
// the test-map seam table. No sleeps: the guard window is escaped by aging
// mtimes into the past, the way config_tree_scan_test.go does.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// symlinkReloadTenant is a neutral tenant id for these fixtures.
const symlinkReloadTenant = "t-sym"

// symlinkPlane is which ConfigManager plane a fixture drives.
type symlinkPlane struct {
	name         string
	hierarchical bool
	// valueFile is the conf.d key whose bytes carry cpu_pct.
	valueFile string
	// files is every conf.d key of the fixture → content for a cpu_pct value.
	files func(cpu string) map[string]string
	// served reads the value the manager serves ("" when absent).
	served func(m *ConfigManager) string
}

func symlinkPlanes() []symlinkPlane {
	return []symlinkPlane{
		{
			name:         "hierarchical (_defaults.yaml carries the value)",
			hierarchical: true,
			valueFile:    "_defaults.yaml",
			files: func(cpu string) map[string]string {
				return map[string]string{
					"_defaults.yaml": "defaults:\n  cpu_pct: " + cpu + "\n",
					"t.yaml":         "tenants:\n  " + symlinkReloadTenant + ": {}\n",
				}
			},
			served: func(m *ConfigManager) string {
				v, ok := m.GetConfig().Defaults["cpu_pct"]
				if !ok {
					return ""
				}
				return strconv.FormatFloat(v, 'f', -1, 64)
			},
		},
		{
			name:         "flat (tenant file carries the value)",
			hierarchical: false,
			valueFile:    "t.yaml",
			files: func(cpu string) map[string]string {
				return map[string]string{
					"t.yaml": "tenants:\n  " + symlinkReloadTenant + ":\n    cpu_pct: \"" + cpu + "\"\n",
				}
			},
			served: func(m *ConfigManager) string {
				return m.GetConfig().Tenants[symlinkReloadTenant]["cpu_pct"].Default
			},
		},
	}
}

// writeAgedAt writes content and sets the file's mtime to now-age.
func writeAgedAt(t *testing.T, path, content string, age time.Duration) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-age)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
}

// symlinkOrSkip creates link -> target (target as given, relative allowed)
// or skips where symlinks are unavailable (Windows without the privilege).
func symlinkOrSkip(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("os.Symlink unavailable here (%v) — symlinked conf.d rows cannot be built on this platform "+
			"(Windows without the symlink privilege); they are measured on Linux/macOS CI", err)
	}
}

// symlinkLayout builds a conf.d tree at root holding plane.files("50") and
// returns the mutation that makes it serve plane.files("90").
type symlinkLayout struct {
	name  string
	build func(t *testing.T, root string, p symlinkPlane) (mutate func())
}

func symlinkLayouts() []symlinkLayout {
	return []symlinkLayout{
		{
			// CONTROL: no symlink anywhere. Reloaded before #1969 too.
			name: "CONTROL plain files, edited in place",
			build: func(t *testing.T, root string, p symlinkPlane) func() {
				for k, v := range p.files("50") {
					writeAgedAt(t, filepath.Join(root, k), v, treeScanFixtureAge)
				}
				return func() {
					if err := os.WriteFile(filepath.Join(root, p.valueFile), []byte(p.files("90")[p.valueFile]), 0o600); err != nil {
						t.Fatal(err)
					}
				}
			},
		},
		{
			// kubelet: ..<ts>/ payload dirs, ..data -> current, key -> ..data/key.
			// The new payload is written at swap time (fresh mtime), as kubelet does.
			name: "configmap ..data swap, new payload written at swap",
			build: func(t *testing.T, root string, p symlinkPlane) func() {
				for k, v := range p.files("50") {
					writeAgedAt(t, filepath.Join(root, "..v1", k), v, treeScanFixtureAge)
				}
				symlinkOrSkip(t, "..v1", filepath.Join(root, "..data"))
				for k := range p.files("50") {
					link := filepath.Join(root, k)
					symlinkOrSkip(t, filepath.Join("..data", k), link)
					testutil.AgeSymlink(t, link, treeScanFixtureAge)
				}
				return func() {
					for k, v := range p.files("90") {
						if err := os.MkdirAll(filepath.Join(root, "..v2"), 0o755); err != nil {
							t.Fatal(err)
						}
						if err := os.WriteFile(filepath.Join(root, "..v2", k), []byte(v), 0o600); err != nil {
							t.Fatal(err)
						}
					}
					swapDataLink(t, root, "..v2")
				}
			},
		},
		{
			// Same kubelet layout, but the new payload is OLDER than the guard
			// and differs from the old one only in mtime (same size: "50" vs
			// "90"). This row is decided by the target stat comparison alone,
			// not by the guard re-reading a young file.
			name: "configmap ..data swap, pre-staged aged payload",
			build: func(t *testing.T, root string, p symlinkPlane) func() {
				for k, v := range p.files("50") {
					writeAgedAt(t, filepath.Join(root, "..v1", k), v, 2*treeScanFixtureAge)
				}
				for k, v := range p.files("90") {
					writeAgedAt(t, filepath.Join(root, "..v2", k), v, treeScanFixtureAge)
				}
				symlinkOrSkip(t, "..v1", filepath.Join(root, "..data"))
				for k := range p.files("50") {
					link := filepath.Join(root, k)
					symlinkOrSkip(t, filepath.Join("..data", k), link)
					testutil.AgeSymlink(t, link, treeScanFixtureAge)
				}
				return func() { swapDataLink(t, root, "..v2") }
			},
		},
		{
			// A hand-made link into a hidden store; the TARGET is rewritten.
			name: "symlink target edited in place",
			build: func(t *testing.T, root string, p symlinkPlane) func() {
				for k, v := range p.files("50") {
					if k == p.valueFile {
						writeAgedAt(t, filepath.Join(root, ".store", k), v, treeScanFixtureAge)
						link := filepath.Join(root, k)
						symlinkOrSkip(t, filepath.Join(".store", k), link)
						testutil.AgeSymlink(t, link, treeScanFixtureAge)
						continue
					}
					writeAgedAt(t, filepath.Join(root, k), v, treeScanFixtureAge)
				}
				return func() {
					if err := os.WriteFile(filepath.Join(root, ".store", p.valueFile), []byte(p.files("90")[p.valueFile]), 0o600); err != nil {
						t.Fatal(err)
					}
				}
			},
		},
	}
}

// swapDataLink is kubelet's atomic payload switch: ln -s <dir> ..data_tmp;
// rename ..data_tmp ..data.
func swapDataLink(t *testing.T, root, dir string) {
	t.Helper()
	tmp := filepath.Join(root, "..data_tmp")
	if err := os.Symlink(dir, tmp); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, filepath.Join(root, "..data")); err != nil {
		t.Fatal(err)
	}
}

// symlinkReloadDriver is one production entry point that turns a changed
// tree into a served config.
type symlinkReloadDriver struct {
	name string
	run  func(t *testing.T, m *ConfigManager)
}

func symlinkReloadDrivers() []symlinkReloadDriver {
	return []symlinkReloadDriver{
		{
			// What WatchLoop runs on each tick. detectChange is asserted on
			// its own first so a red row says WHICH half missed the change.
			name: "watch tick (detectChange + diffAndReload)",
			run: func(t *testing.T, m *ConfigManager) {
				t.Helper()
				changed, _, err := m.detectChange()
				if err != nil {
					t.Fatalf("detectChange: %v", err)
				}
				if !changed {
					t.Errorf("detectChange() = false after the mutation; want true")
				}
				m.tickOnce()
			},
		},
		{
			name: "IncrementalLoad",
			run: func(t *testing.T, m *ConfigManager) {
				t.Helper()
				if err := m.IncrementalLoad(); err != nil {
					t.Fatalf("IncrementalLoad: %v", err)
				}
			},
		},
	}
}

func TestSymlinkedConfD_HotReloadsOnTargetChange(t *testing.T) {
	t.Parallel()
	for _, p := range symlinkPlanes() {
		for _, l := range symlinkLayouts() {
			for _, d := range symlinkReloadDrivers() {
				t.Run(p.name+"/"+l.name+"/"+d.name, func(t *testing.T) {
					t.Parallel()
					root := t.TempDir()
					mutate := l.build(t, root, p)

					fresh, _ := freshMetrics(t)
					var logBuf bytes.Buffer
					m := NewConfigManagerWithDebounce(root, 0)
					m.SetMetrics(fresh)
					m.SetLogger(log.New(&logBuf, "", 0))
					t.Cleanup(m.Close)
					if err := m.Load(); err != nil {
						t.Fatalf("Load: %v", err)
					}
					if got := m.hierarchy.enabled; got != p.hierarchical {
						t.Fatalf("hierarchical plane = %v after Load; fixture expects %v (the row would test the other watch path)", got, p.hierarchical)
					}
					if got := p.served(m); got != "50" {
						t.Fatalf("served cpu_pct before mutation = %q, want 50 (fixture broken)\nlog:\n%s", got, logBuf.String())
					}

					mutate()
					d.run(t, m)

					if got := p.served(m); got != "90" {
						t.Errorf("served cpu_pct after mutation = %q, want 90 (disk holds 90)\nlog:\n%s", got, logBuf.String())
					}
				})
			}
		}
	}
}
