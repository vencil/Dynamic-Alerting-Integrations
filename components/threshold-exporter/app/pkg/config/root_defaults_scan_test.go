package config

// root_defaults_scan_test.go — the walker's root-only mode (scanRootDefaults,
// #1674 round 2) must pick the SAME root carrier, with the same bytes, as a
// full ScanDirTree, on the trees where the two could plausibly differ: case
// variants, a dangling carrier (dropped by the read, not by the name), hidden
// entries, and carriers below the root (which a root-only walk must not see
// but which must not change the root answer either). Plus the cost it exists
// for, as a benchmark against the full scan it replaced.
//
// Seams: none — t.TempDir() trees; logs discarded by scanRootDefaults.

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func rootWrite(t testing.TB, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestRootOnlyScanSelectsWhatTheFullScanSelects(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		build func(t *testing.T, root string)
		want  string // basename of the selected root carrier; "" = none
	}{
		{"case variants of both extensions", func(t *testing.T, root string) {
			rootWrite(t, filepath.Join(root, "_DEFAULTS.YML"), "defaults:\n  a: 1\n")
			rootWrite(t, filepath.Join(root, "_Defaults.yaml"), "defaults:\n  a: 2\n")
			rootWrite(t, filepath.Join(root, "_defaults.YAML"), "defaults:\n  a: 3\n")
		}, "_defaults.YAML"},
		{"dangling .yaml loses to a readable .yml", func(t *testing.T, root string) {
			rootWrite(t, filepath.Join(root, "_defaults.yml"), "defaults:\n  a: 1\n")
			if err := os.Symlink(filepath.Join(root, "missing.yaml"), filepath.Join(root, "_defaults.yaml")); err != nil {
				t.Skipf("symlinks unavailable here: %v", err)
			}
		}, "_defaults.yml"},
		{"hidden entries are not candidates", func(t *testing.T, root string) {
			rootWrite(t, filepath.Join(root, "._defaults.yaml"), "defaults:\n  a: 9\n")
			rootWrite(t, filepath.Join(root, ".hidden", "_defaults.yaml"), "defaults:\n  a: 8\n")
			rootWrite(t, filepath.Join(root, "_defaults.yml"), "defaults:\n  a: 1\n")
		}, "_defaults.yml"},
		{"subtree carriers do not move the root answer", func(t *testing.T, root string) {
			rootWrite(t, filepath.Join(root, "sub", "_defaults.yaml"), "defaults:\n  a: 7\n")
			rootWrite(t, filepath.Join(root, "sub", "t.yaml"), "tenants:\n  t-s: {}\n")
			rootWrite(t, filepath.Join(root, "_DEFAULTS.YML"), "defaults:\n  a: 1\n")
		}, "_DEFAULTS.YML"},
		{"no root carrier", func(t *testing.T, root string) {
			rootWrite(t, filepath.Join(root, "sub", "_defaults.yaml"), "defaults:\n  a: 7\n")
			rootWrite(t, filepath.Join(root, "_profiles.yaml"), "profiles: {}\n")
		}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			tc.build(t, root)

			full, err := ScanDirTree(root, nil, nil, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatal(err)
			}
			rootOnly, err := scanRootDefaults(root)
			if err != nil {
				t.Fatal(err)
			}
			fullPick := full.DefaultsCarriers().ByDir[full.AbsRoot]
			onlyPick := rootOnly.DefaultsCarriers().ByDir[rootOnly.AbsRoot]
			if fullPick != onlyPick {
				t.Fatalf("root-only picked %q, full scan picked %q", onlyPick, fullPick)
			}
			if got := filepath.Base(onlyPick); (tc.want == "" && onlyPick != "") || (tc.want != "" && got != tc.want) {
				t.Fatalf("picked %q, want %q", onlyPick, tc.want)
			}
			// The root-only walk holds nothing but root carriers.
			for k := range rootOnly.Files {
				if strings.Contains(k, "/") || !strings.HasPrefix(strings.ToLower(k), "_defaults.y") {
					t.Errorf("root-only scan kept %q — it must keep root carriers only", k)
				}
			}
			if tc.want != "" {
				_, data, ok := rootDefaultsCarrier(root)
				want, _ := os.ReadFile(fullPick)
				if !ok || string(data) != string(want) {
					t.Errorf("rootDefaultsCarrier bytes = %q (ok=%v), want the full scan's pick %q", data, ok, want)
				}
			}
		})
	}
}

// buildMergeBenchTree is the reviewer's tree: 999 tenant files and a
// 100-key root `_defaults.yaml`.
func buildMergeBenchTree(b *testing.B) string {
	b.Helper()
	dir := b.TempDir()
	var d strings.Builder
	d.WriteString("defaults:\n")
	for i := 0; i < 100; i++ {
		fmt.Fprintf(&d, "  metric_%03d: %d\n", i, 50+i)
	}
	rootWrite(b, filepath.Join(dir, "_defaults.yaml"), d.String())
	for i := 0; i < 999; i++ {
		rootWrite(b, filepath.Join(dir, fmt.Sprintf("tenant-%03d.yaml", i)),
			fmt.Sprintf("tenants:\n  tenant-%03d:\n    metric_001: \"70\"\n", i))
	}
	return dir
}

func BenchmarkMergeTenantWithRootDefaults_1000Files(b *testing.B) {
	dir := buildMergeBenchTree(b)
	body := []byte("tenants:\n  tenant-001:\n    metric_001: \"70\"\n")
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if merged := MergeTenantWithRootDefaults(dir, "tenant-001", body); len(merged.Defaults) != 100 {
			b.Fatalf("Defaults = %d keys, want 100", len(merged.Defaults))
		}
	}
}
