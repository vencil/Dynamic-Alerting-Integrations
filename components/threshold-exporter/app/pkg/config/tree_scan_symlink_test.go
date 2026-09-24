package config

// tree_scan_symlink_test.go — #1969 at the walker: the mtime fast-path must
// judge a symlinked file by its TARGET, and a symlink whose target cannot be
// statted must be dropped (not carried from the prior).
//
// Every link is aged past TreeScanMtimeGuard with testutil.AgeSymlink (the
// link's OWN mtime; os.Chtimes follows links). Without that, the defective
// walker re-reads the young link through the guard and these tests pass on
// the defect. The app-level end-to-end twin (the manager's watch tick and
// IncrementalLoad) is app/config_symlink_reload_test.go.
//
// Seams: none — t.TempDir() trees; the scan logger is a local buffer.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

const symlinkFixtureAge = time.Hour

func agedWrite(t *testing.T, path, content string, age time.Duration) {
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

func agedSymlinkOrSkip(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("os.Symlink unavailable here (%v) — symlinked rows cannot be built on this platform "+
			"(Windows without the symlink privilege); they are measured on Linux/macOS CI", err)
	}
	testutil.AgeSymlink(t, link, symlinkFixtureAge)
}

// TestScanDirTree_ConfigMapDataSwapChangesHash: the kubelet layout
// (`..v1/`, `..v2/`, `..data -> ..v1`, `key -> ..data/key`). After the
// `..data` swap the prior-fed scan must report the new bytes' hash for the
// swapped keys. Both payload variants: written at swap time (young), and
// pre-staged older than the guard with the same size (decided by the target
// stat comparison alone).
func TestScanDirTree_ConfigMapDataSwapChangesHash(t *testing.T) {
	t.Parallel()
	const (
		defaultsV1 = "defaults:\n  cpu_pct: 50\n"
		defaultsV2 = "defaults:\n  cpu_pct: 90\n" // same size as V1 on purpose
		tenant     = "tenants:\n  t-sym: {}\n"
	)
	for _, prestaged := range []bool{false, true} {
		name := "new payload written at swap"
		if prestaged {
			name = "pre-staged aged payload, same size"
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			agedWrite(t, filepath.Join(root, "..v1", "_defaults.yaml"), defaultsV1, 2*symlinkFixtureAge)
			agedWrite(t, filepath.Join(root, "..v1", "t.yaml"), tenant, 2*symlinkFixtureAge)
			if prestaged {
				agedWrite(t, filepath.Join(root, "..v2", "_defaults.yaml"), defaultsV2, symlinkFixtureAge)
				agedWrite(t, filepath.Join(root, "..v2", "t.yaml"), tenant, symlinkFixtureAge)
			}
			agedSymlinkOrSkip(t, "..v1", filepath.Join(root, "..data"))
			agedSymlinkOrSkip(t, filepath.Join("..data", "_defaults.yaml"), filepath.Join(root, "_defaults.yaml"))
			agedSymlinkOrSkip(t, filepath.Join("..data", "t.yaml"), filepath.Join(root, "t.yaml"))

			var logBuf bytes.Buffer
			logger := log.New(&logBuf, "", 0)
			prior, err := ScanDirTree(root, nil, nil, logger)
			if err != nil {
				t.Fatal(err)
			}
			if got := len(prior.Files); got != 2 {
				t.Fatalf("prior scan kept %d files, want 2 (_defaults.yaml, t.yaml); keys=%v", got, prior.Keys)
			}

			if !prestaged {
				agedWrite(t, filepath.Join(root, "..v2", "_defaults.yaml"), defaultsV2, 0)
				agedWrite(t, filepath.Join(root, "..v2", "t.yaml"), tenant, 0)
			}
			tmp := filepath.Join(root, "..data_tmp")
			if err := os.Symlink("..v2", tmp); err != nil {
				t.Fatal(err)
			}
			if err := os.Rename(tmp, filepath.Join(root, "..data")); err != nil {
				t.Fatal(err)
			}

			next, err := ScanDirTree(root, prior, nil, logger)
			if err != nil {
				t.Fatal(err)
			}
			pf, nf := prior.Files["_defaults.yaml"], next.Files["_defaults.yaml"]
			if nf == nil {
				t.Fatalf("_defaults.yaml missing after the swap; log:\n%s", logBuf.String())
			}
			if nf.Hash == pf.Hash {
				t.Errorf("_defaults.yaml hash unchanged across the ..data swap (Reused=%v): the fast-path carried the old payload", nf.Reused)
			}
			if string(nf.Data) != defaultsV2 {
				t.Errorf("_defaults.yaml Data = %q, want the new payload %q", nf.Data, defaultsV2)
			}
			if next.Composite == prior.Composite {
				t.Errorf("composite hash unchanged across the ..data swap")
			}
			// t.yaml's bytes are identical in both payloads: its hash must not
			// move (the swap is not a blanket "everything changed").
			if next.Files["t.yaml"] == nil || next.Files["t.yaml"].Hash != prior.Files["t.yaml"].Hash {
				t.Errorf("t.yaml hash moved although its bytes did not")
			}
		})
	}
}

// TestScanDirTree_DanglingSymlinkedCarrierIsDropped is CodeRabbit's case on
// PR #1968: `_defaults.yaml` is a symlink whose target disappears, next to a
// readable `_defaults.yml`. With a prior that saw the target, the scan must
// drop the dangling `.yaml` (WARN) so DefaultsCarriers selects `.yml` —
// not carry the prior hash and keep an unreadable file selected.
//
// Two rows. "ordinary": the dangling link's own lstat differs from the
// vanished target's stat, so the entry would also fall out through the
// phase-2 read failure. "lstat collides with the vanished target": the
// link's own mtime is set to the target's and the link text is as long as
// the target's bytes, so the link's lstat EQUALS the prior's FileStat — the
// row that only the phase-1 drop (os.Stat failure ⇒ drop) keeps from
// carrying the prior hash. Without it the drop would be pinned by nothing
// but log text.
func TestScanDirTree_DanglingSymlinkedCarrierIsDropped(t *testing.T) {
	t.Parallel()
	for _, collide := range []bool{false, true} {
		name := "ordinary"
		if collide {
			name = "lstat collides with the vanished target"
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			testDanglingSymlinkedCarrier(t, collide)
		})
	}
}

func testDanglingSymlinkedCarrier(t *testing.T, collide bool) {
	t.Helper()
	root := t.TempDir()
	const content = "defaults:\n  cpu_pct: 50\n"
	target := filepath.Join(".store", "d.yaml")
	if collide {
		// Link text length == target size, so the link's lstat Size matches.
		n := len(content) - len(filepath.Join(".store", ".yaml"))
		target = filepath.Join(".store", strings.Repeat("x", n)+".yaml")
		if len(target) != len(content) {
			t.Fatalf("fixture: link text %d bytes, target %d bytes — must be equal", len(target), len(content))
		}
	}
	agedWrite(t, filepath.Join(root, target), content, symlinkFixtureAge)
	agedWrite(t, filepath.Join(root, "_defaults.yml"), "defaults:\n  cpu_pct: 70\n", symlinkFixtureAge)
	link := filepath.Join(root, "_defaults.yaml")
	agedSymlinkOrSkip(t, target, link)
	if collide {
		when := time.Now().Add(-symlinkFixtureAge).Truncate(time.Second)
		if err := os.Chtimes(filepath.Join(root, target), when, when); err != nil {
			t.Fatal(err)
		}
		testutil.SetSymlinkMtime(t, link, when)
		li, lerr := os.Lstat(link)
		ti, terr := os.Stat(link)
		if lerr != nil || terr != nil || li.ModTime().UnixNano() != ti.ModTime().UnixNano() || li.Size() != ti.Size() {
			t.Fatalf("fixture: link lstat (%v, %v) must equal target stat (%v, %v)", li, lerr, ti, terr)
		}
	}

	var logBuf bytes.Buffer
	logger := log.New(&logBuf, "", 0)
	prior, err := ScanDirTree(root, nil, nil, logger)
	if err != nil {
		t.Fatal(err)
	}
	// Control: while the target exists, `.yaml` wins (SelectDefaultsCarriers).
	if got := filepath.Base(prior.DefaultsCarriers().ByDir[prior.AbsRoot]); got != "_defaults.yaml" {
		t.Fatalf("with the target present the carrier is %q, want _defaults.yaml (fixture broken)", got)
	}

	if err := os.Remove(filepath.Join(root, target)); err != nil {
		t.Fatal(err)
	}

	next, err := ScanDirTree(root, prior, nil, logger)
	if err != nil {
		t.Fatal(err)
	}
	if f, ok := next.Files["_defaults.yaml"]; ok {
		t.Errorf("dangling _defaults.yaml kept (Reused=%v): it must be dropped, not carried from the prior", f.Reused)
	}
	if got := filepath.Base(next.DefaultsCarriers().ByDir[next.AbsRoot]); got != "_defaults.yml" {
		t.Errorf("carrier after the target vanished = %q, want _defaults.yml", got)
	}
	if !strings.Contains(logBuf.String(), "WARN") || !strings.Contains(logBuf.String(), "_defaults.yaml") {
		t.Errorf("no WARN naming the dangling _defaults.yaml; log:\n%s", logBuf.String())
	}

	// The root-only mode (tenant-api's MergeTenantWithRootDefaults) goes
	// through the same enumeration and must agree.
	rootOnly, err := scanRootDefaults(root)
	if err != nil {
		t.Fatal(err)
	}
	if got := filepath.Base(rootOnly.DefaultsCarriers().ByDir[rootOnly.AbsRoot]); got != "_defaults.yml" {
		t.Errorf("root-only carrier = %q, want _defaults.yml", got)
	}
}
