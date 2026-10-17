//go:build linux

package gitops

// #2939: the special-file, read_error and walk_error shapes of the
// whole-tree verdict (writer_tree_unreadable_test.go has the portable ones),
// and readMerge's own read of a tenant file that is a FIFO. Linux only: the
// read_error shape is a symlink to /proc/self/mem, and the walk_error one a
// directory past PATH_MAX (testutil.UnlistableDeepDir) — the shapes that
// fail for root, which chmod does not. Seams: none.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// mkfifoReleased puts a FIFO at path and, at cleanup, releases any reader a
// failing run left waiting on it (open for write, then close → EOF).
func mkfifoReleased(t *testing.T, path string) {
	t.Helper()
	if err := syscall.Mkfifo(path, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() {
		if f, err := os.OpenFile(path, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
}

// unreadableShapes build one unreadable entry in conf.d that is not the
// written tenant's file and return what the walker records for it.
var unreadableShapes = []struct {
	name  string
	build func(t *testing.T, dir string) cfg.UnreadableFile
}{
	{"fifo", func(t *testing.T, dir string) cfg.UnreadableFile { // (b)
		mkfifoReleased(t, filepath.Join(dir, "stuck.yaml"))
		return cfg.UnreadableFile{RelKey: "stuck.yaml", Reason: cfg.UnreadableSpecialFile}
	}},
	{"read_error", func(t *testing.T, dir string) cfg.UnreadableFile {
		mem, ok := testutil.ProcSelfMem()
		if !ok {
			t.Skip("no /proc/self/mem that fails to read")
		}
		if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
			t.Fatal(err)
		}
		return cfg.UnreadableFile{RelKey: "eio.yaml", Reason: cfg.UnreadableReadError}
	}},
	{"walk_error", func(t *testing.T, dir string) cfg.UnreadableFile { // (d)
		// slack 100: a batch's staging copy (a root ~45 bytes longer) can
		// still hold every directory the serving walk lists.
		return cfg.UnreadableFile{RelKey: testutil.UnlistableDeepDir(t, dir, 100), Reason: cfg.UnreadableWalkError}
	}},
}

// (b) (d) and read_error: every direct write path and preview refuses, at
// once, as a STORE FAULT (D3c: none of these is content git holds), naming
// exactly the entry, and nothing is written. The PR modes judge
// the base git holds, where none of these untracked shapes exists; their
// verdict is pinned with a committed shape (writer_tree_unreadable_test.go),
// and that they accept an untracked one by
// TestTreeUnreadable_PRModesJudgeTheGitBaseNotTheServingTree (#2972).
func TestTreeUnreadable_ShapesRefuseEveryDirectPath(t *testing.T) {
	for _, shape := range unreadableShapes {
		for _, mode := range directTreeModes {
			t.Run(shape.name+"/"+mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{"sh-t.yaml": tenantBody("sh-t")})
				want := shape.build(t, dir)
				head := gitRevParse(t, dir, "HEAD")
				w := NewWriter(dir, dir)
				done := make(chan error, 1)
				go func() { done <- treeVerdictVia(t, mode, w, "sh-t") }()
				select {
				case err := <-done:
					assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{want}, true)
				case <-time.After(30 * time.Second):
					t.Fatal("blocked")
				}
				if got := gitRevParse(t, dir, "HEAD"); got != head {
					t.Errorf("HEAD moved %s → %s", head, got)
				}
				if got, _ := os.ReadFile(filepath.Join(dir, "sh-t.yaml")); string(got) != tenantBody("sh-t") {
					t.Errorf("sh-t.yaml changed: %q", got)
				}
			})
		}
	}
}

// (h) read_error in PR mode: a committed symlink to /proc/self/mem is on the
// fresh base, so WritePR and WritePRBatch refuse like the direct paths.
func TestTreeUnreadable_ReadErrorOnTheBaseRefusesPRModes(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	for _, mode := range []string{"WritePR", "WritePRBatch"} {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"prr-t.yaml": tenantBody("prr-t")})
			if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
				t.Fatal(err)
			}
			gitRun(t, dir, "add", "-A")
			gitRun(t, dir, "commit", "-m", "read_error link")
			err := treeVerdictVia(t, mode, NewWriter(dir, dir), "prr-t")
			assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{{RelKey: "eio.yaml", Reason: cfg.UnreadableReadError}}, true)
		})
	}
}

// D3c-1: the write's OWN tenant file being unreadable in a way that is not a
// dangling symlink — a read_error, or a FIFO — refuses the write for EVERY
// caller (TargetUnreadableError, TENANT_CONFIG_NOT_LOADABLE): the file has
// content nobody can read, and replacing it would erase it. The platform-wide
// writer (WithReplaceUnparseable) is refused too. Every direct path and
// preview agrees, and the PR modes for the read_error shape (a committed
// symlink to /proc/self/mem; a FIFO cannot be committed).
func TestTreeUnreadable_UnreadableTargetIsRefusedForEveryCaller(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	for _, shape := range []struct {
		name, reason string
		build        func(t *testing.T, target string)
		pr           bool
	}{
		{"read_error", cfg.UnreadableReadError, func(t *testing.T, target string) {
			if err := os.Symlink(mem, target); err != nil {
				t.Fatal(err)
			}
		}, true},
		{"fifo", cfg.UnreadableSpecialFile, func(t *testing.T, target string) { mkfifoReleased(t, target) }, false},
	} {
		modes := append(append([]string{}, directTreeModes...), "Write+platform")
		if shape.pr {
			modes = append(modes, "WritePR", "WritePRBatch")
		}
		for _, mode := range modes {
			t.Run(shape.name+"/"+mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{"other-t.yaml": tenantBody("other-t")})
				shape.build(t, filepath.Join(dir, "un-t.yaml"))
				if mode == "WritePR" || mode == "WritePRBatch" {
					gitRun(t, dir, "add", "-A")
					gitRun(t, dir, "commit", "-m", "unreadable tenant file")
				}
				w := NewWriter(dir, dir)
				done := make(chan error, 1)
				go func() {
					if mode == "Write+platform" {
						_, err := w.Write(WithReplaceUnparseable(context.Background()), "un-t", "op@example.com", tenantBody("un-t"))
						done <- err
						return
					}
					done <- treeVerdictVia(t, mode, w, "un-t")
				}()
				var err error
				select {
				case err = <-done:
				case <-time.After(30 * time.Second):
					t.Fatal("blocked")
				}
				var te *TargetUnreadableError
				if !errors.As(err, &te) || te.Reason != shape.reason || te.TenantID != "un-t" {
					t.Fatalf("%s: err = %v, want *TargetUnreadableError (%s)", mode, err, shape.reason)
				}
				if errors.Is(err, ErrTenantTreeUnreadable) || errors.Is(err, ErrTenantStoreFault) {
					t.Errorf("%s: err = %v: the target was also counted against the tree", mode, err)
				}
				if n := strings.Count(err.Error(), "tenant un-t:"); n != 1 {
					t.Errorf("%s: err = %q names the tenant %d times, want once", mode, err, n)
				}
			})
		}
	}
}

// D3c-2: a store fault and a dangling link elsewhere at once — the store
// fault wins (the server must be repaired first), naming both entries; in a
// batch it fails the batch whole rather than per op. The PR modes are not
// here: they judge the base as git holds it (#2972), where neither of these
// untracked entries exists.
func TestTreeUnreadable_StoreFaultWinsOverDanglingLink(t *testing.T) {
	for _, mode := range directTreeModes {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"mx-t.yaml": tenantBody("mx-t")})
			if err := os.Symlink("missing-target.yaml", filepath.Join(dir, "gone.yaml")); err != nil {
				t.Fatal(err)
			}
			mkfifoReleased(t, filepath.Join(dir, "stuck.yaml"))
			w := NewWriter(dir, dir)
			err := treeVerdictVia(t, mode, w, "mx-t")
			assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{
				{RelKey: "gone.yaml", Reason: cfg.UnreadableStatError},
				{RelKey: "stuck.yaml", Reason: cfg.UnreadableSpecialFile},
			}, true)
			if mode == "WriteBatch" { // treeVerdictVia unwraps a per-op refusal; ask the batch itself
				_, berr := w.WriteBatch(context.Background(), []BatchOp{{TenantID: "mx-t",
					Merge: func([]byte) (string, error) { return tenantBody("mx-t"), nil }}}, "op@example.com")
				var rej *BatchRejectedError
				if errors.As(berr, &rej) || !errors.Is(berr, ErrTenantStoreFault) {
					t.Errorf("WriteBatch: %v, want the batch failed whole with the store fault", berr)
				}
			}
		})
	}
}

// The tenant_api_store_unreadable_entries source walks conf.d itself, at
// scrape time (R4-N4): with no write at all, a FIFO, a read_error and an
// unlistable directory each count once — an unresolvable link does not —
// and the count clears by itself once the entry is gone, after the minimum
// interval (a fake clock, no sleep).
func TestWriter_StoreUnreadableWalksAtScrapeTime(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	dir := seedTreeRepo(t, map[string]string{"cnt-t.yaml": tenantBody("cnt-t")})
	w := NewWriter(dir, dir)
	clock := time.Unix(1_000_000, 0)
	w.storeScan.now = func() time.Time { return clock }
	w.storeScan.minInterval = 30 * time.Second
	if sc := w.StoreUnreadable(); !sc.OK || len(sc.Counts) != 0 {
		t.Fatalf("clean tree: %+v; want no store fault", sc)
	}
	fifo := filepath.Join(dir, "stuck.yaml")
	mkfifoReleased(t, fifo)
	if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("missing-target.yaml", filepath.Join(dir, "gone.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("loop.yaml", filepath.Join(dir, "loop.yaml")); err != nil {
		t.Fatal(err)
	}
	testutil.UnlistableDeepDir(t, dir, 100)
	clock = clock.Add(10 * time.Second)
	if got := w.StoreUnreadable().Counts; len(got) != 0 {
		t.Errorf("within the minimum interval: %v, want the cached clean walk", got)
	}
	clock = clock.Add(25 * time.Second)
	got := w.StoreUnreadable().Counts
	want := map[string]int{cfg.UnreadableSpecialFile: 1, cfg.UnreadableReadError: 1, cfg.UnreadableWalkError: 1}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("after the interval, no write: %v, want %v (the unresolvable links not counted)", got, want)
	}
	if err := os.Remove(fifo); err != nil {
		t.Fatal(err)
	}
	clock = clock.Add(31 * time.Second)
	got = w.StoreUnreadable().Counts
	if got[cfg.UnreadableSpecialFile] != 0 || got[cfg.UnreadableReadError] != 1 {
		t.Errorf("after removing the FIFO: %v, want special_file 0 (read_error still 1)", got)
	}
}

// (g) WriteMerged's read of the tenant's OWN file, when that file is a FIFO:
// the Writer refuses it at once (confd.ErrNotRegularFile) instead of
// blocking in the read under the writer lock — it does not rely on the
// handler's earlier refusal. The FIFO is the write's target, so the
// whole-tree verdict does not count it; the read is what must not block.
func TestWriteMerged_TargetFIFODoesNotBlock(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"other-t.yaml": tenantBody("other-t")})
	mkfifoReleased(t, filepath.Join(dir, "fifo-t.yaml"))
	head := gitRevParse(t, dir, "HEAD")
	w := NewWriter(dir, dir)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := w.WriteMerged(ctx, "fifo-t", "op@example.com",
			func([]byte) (string, error) { return tenantBody("fifo-t"), nil })
		done <- err
	}()
	select {
	case err := <-done:
		if !errors.Is(err, confd.ErrNotRegularFile) {
			t.Fatalf("err = %v, want confd.ErrNotRegularFile", err)
		}
	case <-ctx.Done():
		t.Fatal("WriteMerged blocked on the FIFO tenant file")
	}
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Errorf("HEAD moved %s → %s", head, got)
	}
}

// R5-N2: unknown is not zero. While every walk fails the Writer reports
// nothing (OK false); the first success reports counts and its time; a later
// failure keeps both — the time does not move, so its age measures how long
// the gauge has been blind.
func TestWriter_StoreUnreadableUnknownUntilAWalkSucceeds(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"sk-t.yaml": tenantBody("sk-t")})
	w := NewWriter(dir, dir)
	clock := time.Unix(2_000_000, 0)
	w.storeScan.now = func() time.Time { return clock }
	w.storeScan.minInterval = 30 * time.Second
	fail := true
	w.storeScan.scan = func() (map[string]int, error) {
		if fail {
			return nil, errors.New("injected: walk failed")
		}
		return map[string]int{cfg.UnreadableSpecialFile: 2}, nil
	}
	for i := 0; i < 3; i++ {
		if sc := w.StoreUnreadable(); sc.OK || sc.Counts != nil || !sc.LastSuccess.IsZero() {
			t.Fatalf("walk %d failed, yet StoreUnreadable = %+v; want nothing known", i, sc)
		}
		clock = clock.Add(time.Minute)
	}
	fail = false
	first := w.StoreUnreadable()
	if !first.OK || first.Counts[cfg.UnreadableSpecialFile] != 2 || !first.LastSuccess.Equal(clock) {
		t.Fatalf("first success = %+v, want special_file 2 at %v", first, clock)
	}
	fail = true
	clock = clock.Add(5 * time.Minute)
	later := w.StoreUnreadable()
	if !later.OK || later.Counts[cfg.UnreadableSpecialFile] != 2 || !later.LastSuccess.Equal(first.LastSuccess) {
		t.Errorf("after a failed walk = %+v, want the last counts and the timestamp unchanged (%v)", later, first.LastSuccess)
	}
}

// #2972: in PR mode a write and a batch judge the SAME tree — the fresh base
// as git holds it, which is what the service serves once the PR merges. An
// entry the serving working tree holds and git does not (an uncommitted
// FIFO, or one .gitignore hides) is on neither tree, so WritePR and
// WritePRBatch both accept, and both push a branch carrying the body.
func TestTreeUnreadable_PRModesJudgeTheGitBaseNotTheServingTree(t *testing.T) {
	for _, shape := range []struct {
		name  string
		files map[string]string
	}{
		{"untracked", nil},
		{"gitignored", map[string]string{".gitignore": "stuck.yaml\n"}},
	} {
		t.Run(shape.name, func(t *testing.T) {
			got := map[string]error{}
			for _, mode := range []string{"WritePR", "WritePRBatch"} {
				// prb-t is new, so the write changes the tree.
				files := map[string]string{"prb-o.yaml": tenantBody("prb-o")}
				for k, v := range shape.files {
					files[k] = v
				}
				dir := seedTreeRepo(t, files)
				mkfifoReleased(t, filepath.Join(dir, "stuck.yaml"))
				if st := gitOut(t, dir, "ls-files", "--", "stuck.yaml"); st != "" {
					t.Fatalf("fixture: stuck.yaml is tracked: %q", st)
				}
				done := make(chan error, 1)
				go func() { done <- treeVerdictVia(t, mode, NewWriter(dir, dir), "prb-t") }()
				select {
				case got[mode] = <-done:
				case <-time.After(30 * time.Second):
					t.Fatalf("%s: blocked", mode)
				}
				if got[mode] == nil && gitOut(t, dir, "ls-remote", "origin", "refs/heads/tenant-api/*") == "" {
					t.Errorf("%s: accepted, but no branch reached origin", mode)
				}
				if fi, err := os.Lstat(filepath.Join(dir, "stuck.yaml")); err != nil || fi.Mode()&os.ModeNamedPipe == 0 {
					t.Errorf("%s: the serving tree's FIFO was touched (err=%v)", mode, err)
				}
			}
			if got["WritePR"] != nil || got["WritePRBatch"] != nil {
				t.Fatalf("WritePR err = %v, WritePRBatch err = %v; want both nil (the git base is readable)",
					got["WritePR"], got["WritePRBatch"])
			}
		})
	}
}
