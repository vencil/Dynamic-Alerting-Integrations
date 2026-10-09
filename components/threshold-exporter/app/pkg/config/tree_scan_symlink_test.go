package config

// tree_scan_symlink_test.go — #1969 at the walker: the mtime fast-path must
// judge a symlinked file by its TARGET, and a symlink whose target cannot be
// statted must be dropped (not carried from the prior).
//
// Every link is aged past TreeScanMtimeGuard with testutil.AgeSymlink (the
// link's OWN mtime; os.Chtimes follows links). Without that, the defective
// walker re-reads the young link through the guard and these tests pass on
// the defect. The app-level end-to-end twin (the manager's watch tick and
// its reload) is app/config_symlink_reload_test.go.
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
			"(Windows without the symlink privilege); CI measures them on ubuntu-latest, its only runner", err)
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
				t.Errorf("_defaults.yaml hash unchanged across the ..data swap (Reused=%v): the fast-path carried the old payload", next.Reused("_defaults.yaml"))
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

// TestScanDirTree_SymlinkRetargetToIdenticalStatTarget: the link itself is
// retargeted (new link renamed over it, as `ln -sfn` does) to a file that
// was written earlier with the same size AND the same mtime as the old
// target. The target stat is identical before and after; only the link's
// lstat moved, so a fast-path keyed on the target alone carries the old
// hash. Three rows:
//   - new link fresh: inside the guard, and its lstat differs too.
//   - new link aged: to a different past time; decided by the link-stat
//     comparison alone (the guard is not involved).
//   - link lstat collides, link young: the new link gets the OLD link's
//     exact mtime (and the same size), a coarse-mtime filesystem's view.
//     Every stat equals the prior's, so only the guard can catch it — and
//     only if the guard ages the NEWER of the two mtimes (the link, young)
//     rather than the target's (old). Pins that choice.
func TestScanDirTree_SymlinkRetargetToIdenticalStatTarget(t *testing.T) {
	t.Parallel()
	const (
		v1 = "defaults:\n  cpu_pct: 50\n"
		v2 = "defaults:\n  cpu_pct: 90\n" // same size as v1 on purpose
	)
	for _, mode := range []string{"new link fresh", "new link aged", "link lstat collides, link young"} {
		t.Run(mode, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			same := time.Now().Add(-symlinkFixtureAge).Truncate(time.Second)
			for p, body := range map[string]string{".a/d.yaml": v1, ".b/d.yaml": v2} {
				full := filepath.Join(root, filepath.FromSlash(p))
				agedWrite(t, full, body, 0)
				if err := os.Chtimes(full, same, same); err != nil {
					t.Fatal(err)
				}
			}
			link := filepath.Join(root, "_defaults.yaml")
			agedSymlinkOrSkip(t, filepath.Join(".a", "d.yaml"), link)
			// young is inside the guard but far enough from its edge for the
			// two scans below to finish first.
			young := time.Now().Add(-TreeScanMtimeGuard / 4).Truncate(time.Microsecond)
			if mode == "link lstat collides, link young" {
				testutil.SetSymlinkMtime(t, link, young)
			}

			var logBuf bytes.Buffer
			logger := log.New(&logBuf, "", 0)
			prior, err := ScanDirTree(root, nil, nil, logger)
			if err != nil {
				t.Fatal(err)
			}

			oldLink, err := os.Lstat(link)
			if err != nil {
				t.Fatal(err)
			}
			tmp := filepath.Join(root, ".retarget-tmp")
			if err := os.Symlink(filepath.Join(".b", "d.yaml"), tmp); err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "new link aged":
				testutil.AgeSymlink(t, tmp, symlinkFixtureAge/2)
			case "link lstat collides, link young":
				testutil.SetSymlinkMtime(t, tmp, young)
			}
			if err := os.Rename(tmp, link); err != nil {
				t.Fatal(err)
			}
			// Fixture check: the target stat really is identical.
			if ti, err := os.Stat(link); err != nil || ti.ModTime().UnixNano() != prior.Files["_defaults.yaml"].Stat.ModTime ||
				ti.Size() != prior.Files["_defaults.yaml"].Stat.Size {
				t.Fatalf("fixture: new target stat (%v, %v) differs from the prior's %+v", ti, err, prior.Files["_defaults.yaml"].Stat)
			}
			if mode == "link lstat collides, link young" {
				if li, err := os.Lstat(link); err != nil || li.ModTime().UnixNano() != oldLink.ModTime().UnixNano() || li.Size() != oldLink.Size() {
					t.Fatalf("fixture: new link lstat (%v, %v) must equal the old link's (%v, %v)",
						li, err, oldLink.ModTime(), oldLink.Size())
				}
			}

			next, err := ScanDirTree(root, prior, nil, logger)
			if err != nil {
				t.Fatal(err)
			}
			nf := next.Files["_defaults.yaml"]
			if nf == nil {
				t.Fatalf("_defaults.yaml missing after the retarget; log:\n%s", logBuf.String())
			}
			if nf.Hash == prior.Files["_defaults.yaml"].Hash {
				t.Errorf("_defaults.yaml hash unchanged across the retarget (Reused=%v): the fast-path carried the old target", next.Reused("_defaults.yaml"))
			}
			if string(nf.Data) != v2 {
				t.Errorf("_defaults.yaml Data = %q, want the new target's %q", nf.Data, v2)
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
	if _, ok := next.Files["_defaults.yaml"]; ok {
		t.Errorf("dangling _defaults.yaml kept (Reused=%v): it must be dropped, not carried from the prior", next.Reused("_defaults.yaml"))
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

// TestScanDirTree_KubeletDirSymlinkIsAudible (#1972): the two layouts
// kubelet's AtomicWriter actually produces for a ConfigMap volume.
//
//   - nested: an `items[].path` with a sub-directory (`team-a/x.yaml`) is
//     projected as ONE top-level DIRECTORY link, `team-a -> ..data/team-a`
//     — not as a real `team-a/` holding file links. The walker does not
//     follow it (by design), so the tenant under it is not loaded; before
//     #1972 that happened with no log line at all. It must now WARN, and
//     the WARN must name `team-a`.
//   - flat: every key is a FILE link `key -> ..data/key` next to `..data`.
//     The baseline that must not move: every tenant loads and the logger
//     stays EMPTY — in particular `..data` (itself a directory link) must
//     not trip the new WARN.
//
// Plus the two rows of the target rule (dirLinkTargetIsWalked): a link to a
// real directory INSIDE the root (`alias/current -> team-b`) is silent —
// its tenant loads through the real path — and a link to a directory
// OUTSIDE the root warns.
//
// Seams: the scan logger is a local buffer passed to ScanDirTree.
func TestScanDirTree_KubeletDirSymlinkIsAudible(t *testing.T) {
	t.Parallel()
	const (
		defaults = "defaults:\n  cpu_pct: 50\n"
		tenant   = "tenants:\n  t-sym: {}\n"
	)
	link := func(t *testing.T, target, at string) {
		t.Helper()
		if err := os.Symlink(target, at); err != nil {
			t.Skipf("os.Symlink unavailable here (%v) — symlinked rows cannot be built on this platform "+
				"(Windows without the symlink privilege); CI measures them on ubuntu-latest, its only runner", err)
		}
	}

	t.Run("nested items[].path: directory link warns and names it", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		agedWrite(t, filepath.Join(root, "..v1", "_defaults.yaml"), defaults, 0)
		agedWrite(t, filepath.Join(root, "..v1", "team-a", "x.yaml"), tenant, 0)
		link(t, "..v1", filepath.Join(root, "..data"))
		link(t, filepath.Join("..data", "_defaults.yaml"), filepath.Join(root, "_defaults.yaml"))
		link(t, filepath.Join("..data", "team-a"), filepath.Join(root, "team-a"))

		var logBuf bytes.Buffer
		scan, err := ScanDirTree(root, nil, nil, log.New(&logBuf, "", 0))
		if err != nil {
			t.Fatal(err)
		}
		// The design is unchanged: the link is still not walked.
		if _, ok := scan.Files["team-a/x.yaml"]; ok {
			t.Fatalf("team-a/x.yaml was loaded: the walker followed the directory link (keys=%v)", scan.Keys)
		}
		out := logBuf.String()
		want := filepath.Join(root, "team-a") + " is a symlink to a directory and is not followed"
		if !strings.Contains(out, "WARN: "+want) {
			t.Errorf("no WARN naming the team-a directory link; want a line containing %q, log:\n%s", want, out)
		}
		if strings.Contains(out, "..data") {
			t.Errorf("the ..data link must not be reported (it is how every ConfigMap volume looks); log:\n%s", out)
		}
		if n := strings.Count(out, "\n"); n != 1 {
			t.Errorf("log has %d lines, want exactly 1 (the team-a WARN); log:\n%s", n, out)
		}
	})

	// A link to a real directory INSIDE the root that the walk reaches by
	// itself: its tenants load through the real path, nothing is lost, so
	// a "NOT loaded" WARN would be false (#1972 blind review F1).
	t.Run("alias to an in-root directory: silent, tenant loads", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		agedWrite(t, filepath.Join(root, "alias", "team-b", "x.yaml"), tenant, 0)
		link(t, "team-b", filepath.Join(root, "alias", "current"))

		var logBuf bytes.Buffer
		scan, err := ScanDirTree(root, nil, nil, log.New(&logBuf, "", 0))
		if err != nil {
			t.Fatal(err)
		}
		if f := scan.Files["alias/team-b/x.yaml"]; f == nil || len(f.TenantIDs) != 1 {
			t.Fatalf("alias/team-b/x.yaml not loaded (keys=%v)", scan.Keys)
		}
		if logBuf.Len() != 0 {
			t.Errorf("a link to a directory the walk reaches anyway must not warn; log:\n%s", logBuf.String())
		}
	})

	// A link to a directory OUTSIDE the root: nothing reaches it, warn.
	t.Run("link to a directory outside the root warns", func(t *testing.T) {
		t.Parallel()
		base := t.TempDir()
		root := filepath.Join(base, "root")
		agedWrite(t, filepath.Join(base, "elsewhere", "x.yaml"), tenant, 0)
		agedWrite(t, filepath.Join(root, "_defaults.yaml"), defaults, 0)
		link(t, filepath.Join(base, "elsewhere"), filepath.Join(root, "ext"))

		var logBuf bytes.Buffer
		if _, err := ScanDirTree(root, nil, nil, log.New(&logBuf, "", 0)); err != nil {
			t.Fatal(err)
		}
		want := filepath.Join(root, "ext") + " is a symlink to a directory and is not followed"
		if !strings.Contains(logBuf.String(), "WARN: "+want) {
			t.Errorf("no WARN for the out-of-root directory link; want %q, log:\n%s", want, logBuf.String())
		}
	})

	// A CONFIG-NAMED directory link never reaches the new branch: it is an
	// entry, and phase 2's read fails. docs/troubleshooting names this WARN.
	t.Run("config-named directory link keeps the read-failure WARN", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		agedWrite(t, filepath.Join(root, "real", "x.yaml"), tenant, 0)
		link(t, "real", filepath.Join(root, "t.YAML"))

		var logBuf bytes.Buffer
		if _, err := ScanDirTree(root, nil, nil, log.New(&logBuf, "", 0)); err != nil {
			t.Fatal(err)
		}
		out := logBuf.String()
		if !strings.Contains(out, "WARN: cannot read "+filepath.Join(root, "t.YAML")) || !strings.Contains(out, "is a directory") {
			t.Errorf("want the read-failure WARN for t.YAML; log:\n%s", out)
		}
		if strings.Contains(out, "is not followed") {
			t.Errorf("a config-named link must not get the #1972 line too; log:\n%s", out)
		}
	})

	t.Run("flat keys: file links and ..data load silently", func(t *testing.T) {
		t.Parallel()
		root := t.TempDir()
		agedWrite(t, filepath.Join(root, "..v1", "_defaults.yaml"), defaults, 0)
		agedWrite(t, filepath.Join(root, "..v1", "x.yaml"), tenant, 0)
		link(t, "..v1", filepath.Join(root, "..data"))
		link(t, filepath.Join("..data", "_defaults.yaml"), filepath.Join(root, "_defaults.yaml"))
		link(t, filepath.Join("..data", "x.yaml"), filepath.Join(root, "x.yaml"))

		var logBuf bytes.Buffer
		scan, err := ScanDirTree(root, nil, nil, log.New(&logBuf, "", 0))
		if err != nil {
			t.Fatal(err)
		}
		if logBuf.Len() != 0 {
			t.Errorf("the kubelet flat layout must scan without a single log line; log:\n%s", logBuf.String())
		}
		f := scan.Files["x.yaml"]
		if f == nil || len(f.TenantIDs) != 1 || f.TenantIDs[0] != "t-sym" {
			t.Errorf("x.yaml = %+v, want TenantIDs [t-sym] (keys=%v)", f, scan.Keys)
		}
		if d := scan.Files["_defaults.yaml"]; d == nil || !d.IsDefaults {
			t.Errorf("_defaults.yaml not kept as a defaults carrier (keys=%v)", scan.Keys)
		}
	})
}
