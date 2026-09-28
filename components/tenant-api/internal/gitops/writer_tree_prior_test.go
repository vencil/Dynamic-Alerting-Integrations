package gitops

// #2153 T1: scanTree hands the last completed walk to the next
// cfg.ScanDirTree as its prior, so an unchanged file is neither read nor
// parsed again — WritePRBatch's walk before each op included. These tests pin
// both halves — the reuse actually happens, and
// every verdict the #2078 guard derives from a walk stays what a cold walk
// would say — through the walker's own TreeFile.Reused / Parsed flags and the
// per-Writer onTreeScan seam, never through wall-clock timings.

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// ageTree sets every file under dir (outside .git) to an mtime an hour in
// the past, i.e. well beyond cfg.TreeScanMtimeGuard, so a stat-identical
// file is eligible for the walker's mtime fast-path.
func ageTree(t *testing.T, dir string) {
	t.Helper()
	old := time.Now().Add(-time.Hour)
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && d.Name() == ".git" {
			return fs.SkipDir
		}
		if d.Type().IsRegular() {
			return os.Chtimes(p, old, old)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func writeFile(t *testing.T, p, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func mustScan(t *testing.T, w *Writer) *cfg.TreeScan {
	t.Helper()
	s, err := w.scanTree()
	if err != nil {
		t.Fatalf("scanTree: %v", err)
	}
	return s
}

// wantLocated asserts scan attributes tenantID to rel (root-relative), or to
// no file when rel is "".
func wantLocated(t *testing.T, s *cfg.TreeScan, tenantID, rel string) {
	t.Helper()
	got, err := s.Locate(tenantID)
	if rel == "" {
		if !errors.Is(err, cfg.ErrTenantNotFound) {
			t.Errorf("Locate(%s) = %q, %v; want ErrTenantNotFound", tenantID, got, err)
		}
		return
	}
	if err != nil || got != filepath.Join(s.AbsRoot, filepath.FromSlash(rel)) {
		t.Errorf("Locate(%s) = %q, %v; want %s", tenantID, got, err, rel)
	}
}

// The point of the change: a second walk of an unchanged tree reuses every
// file from the prior instead of reading and parsing it again.
func TestTreePrior_SecondScanReusesUnchangedFiles(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "rp-a.yaml"), tenantBody("rp-a"))
	writeFile(t, filepath.Join(dir, "team", "rp-b.yaml"), tenantBody("rp-b"))
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), "defaults: {}\n")
	ageTree(t, dir)
	w := &Writer{configDir: dir}

	first := mustScan(t, w)
	for k, f := range first.Files {
		if f.Reused {
			t.Errorf("cold walk: %s Reused", k)
		}
	}
	second := mustScan(t, w)
	if len(second.Files) != 3 {
		t.Fatalf("second walk kept %d files, want 3", len(second.Files))
	}
	for k, f := range second.Files {
		if !f.Reused || f.Parsed {
			t.Errorf("second walk: %s Reused=%v Parsed=%v, want reused and not parsed", k, f.Reused, f.Parsed)
		}
	}
	wantLocated(t, second, "rp-a", "rp-a.yaml")
	wantLocated(t, second, "rp-b", "team/rp-b.yaml")
}

// A rewrite that keeps size AND mtime (a coarse-mtime filesystem, same
// second) must still be seen. The mtime is set in the future so the file is
// inside cfg.TreeScanMtimeGuard however slowly the test runs; the walker
// re-reads such a file and must re-derive its declarations from the new
// bytes. The first write goes through the Writer itself.
func TestTreePrior_SameStatRewriteAfterWriteIsSeen(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"ss-host.yaml": tenantBody("ss-host")})
	w := NewWriter(dir, dir)
	host := filepath.Join(dir, "ss-host.yaml")
	body := "tenants:\n  ss-host:\n    _silent_mode: \"warning\"\n"
	if _, err := w.Write(context.Background(), "ss-host", "op@example.com", body); err != nil {
		t.Fatalf("Write: %v", err)
	}
	future := time.Now().Add(time.Hour)
	writeFile(t, host, "tenants:\n  ss-aaaa:\n    _silent_mode: \"warning\"\n")
	if err := os.Chtimes(host, future, future); err != nil {
		t.Fatal(err)
	}
	before := mustScan(t, w)
	wantLocated(t, before, "ss-aaaa", "ss-host.yaml")

	// Same length, same mtime, different tenant.
	writeFile(t, host, "tenants:\n  ss-bbbb:\n    _silent_mode: \"warning\"\n")
	if err := os.Chtimes(host, future, future); err != nil {
		t.Fatal(err)
	}
	after := mustScan(t, w)
	if before.Files["ss-host.yaml"].Stat != after.Files["ss-host.yaml"].Stat {
		t.Fatalf("fixture: the rewrite must keep the file's stat")
	}
	wantLocated(t, after, "ss-bbbb", "ss-host.yaml")
	wantLocated(t, after, "ss-aaaa", "")
	if err := w.ensureNotDeclaredElsewhere("ss-bbbb", filepath.Join(dir, "ss-bbbb.yaml")); !errors.Is(err, ErrTenantDeclaredElsewhere) {
		t.Errorf("ss-bbbb now lives in ss-host.yaml: err = %v, want ErrTenantDeclaredElsewhere", err)
	}
}

// git checkout rewrites the files that differ between the two trees and
// leaves the rest alone: the differing file must be judged by its new bytes
// on both switches, while the untouched one keeps coming from the prior.
func TestTreePrior_BranchSwitchIsSeen(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{
		"sw-host.yaml":  "tenants:\n  sw-host:\n    _silent_mode: \"warning\"\n  sw-kkk:\n    _silent_mode: \"warning\"\n",
		"sw-other.yaml": tenantBody("sw-other"),
	})
	gitRun(t, dir, "checkout", "-q", "-b", "feat")
	writeFile(t, filepath.Join(dir, "sw-host.yaml"),
		"tenants:\n  sw-host:\n    _silent_mode: \"warning\"\n  sw-jjj:\n    _silent_mode: \"warning\"\n")
	gitRun(t, dir, "commit", "-q", "-am", "swap the second tenant")
	gitRun(t, dir, "checkout", "-q", "main")
	ageTree(t, dir)
	w := NewWriter(dir, dir)

	onMain := mustScan(t, w)
	wantLocated(t, onMain, "sw-kkk", "sw-host.yaml")

	gitRun(t, dir, "checkout", "-q", "feat")
	onFeat := mustScan(t, w)
	wantLocated(t, onFeat, "sw-jjj", "sw-host.yaml")
	wantLocated(t, onFeat, "sw-kkk", "")
	if f := onFeat.Files["sw-other.yaml"]; !f.Reused {
		t.Errorf("sw-other.yaml was not taken from the prior: the walk was cold")
	}
	if err := w.ensureNotDeclaredElsewhere("sw-jjj", filepath.Join(dir, "sw-jjj.yaml")); !errors.Is(err, ErrTenantDeclaredElsewhere) {
		t.Errorf("on feat: err = %v, want ErrTenantDeclaredElsewhere", err)
	}

	gitRun(t, dir, "checkout", "-q", "main")
	back := mustScan(t, w)
	wantLocated(t, back, "sw-kkk", "sw-host.yaml")
	wantLocated(t, back, "sw-jjj", "")
	if err := w.ensureNotDeclaredElsewhere("sw-jjj", filepath.Join(dir, "sw-jjj.yaml")); err != nil {
		t.Errorf("back on main: sw-jjj is declared nowhere, err = %v", err)
	}
}

// A file removed after the prior was taken is gone from the next walk, and a
// file added after it is read (even with an old mtime — it has no prior
// entry to match).
func TestTreePrior_DeletedAndAddedFiles(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "da-gone.yaml"), tenantBody("da-gone"))
	writeFile(t, filepath.Join(dir, "da-stay.yaml"), tenantBody("da-stay"))
	ageTree(t, dir)
	w := &Writer{configDir: dir}
	wantLocated(t, mustScan(t, w), "da-gone", "da-gone.yaml")

	if err := os.Remove(filepath.Join(dir, "da-gone.yaml")); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(dir, "sub", "da-new.yaml"), tenantBody("da-new"))
	ageTree(t, dir)
	s := mustScan(t, w)
	wantLocated(t, s, "da-gone", "")
	wantLocated(t, s, "da-new", "sub/da-new.yaml")
	wantLocated(t, s, "da-stay", "da-stay.yaml")
	if f := s.Files["sub/da-new.yaml"]; f == nil || f.Reused || !f.Parsed {
		t.Errorf("added file must be parsed, got %+v", f)
	}
}

// The #2078 guard answers the same on a walk served from the prior: a tenant
// declared by another file is refused on every write entry point, and
// released once that file stops declaring it.
func TestTreePrior_DeclaredElsewhereHoldsOnReusedWalk(t *testing.T) {
	host := "tenants:\n  de-host:\n    _silent_mode: \"warning\"\n  de-guest:\n    _silent_mode: \"warning\"\n"
	dir := seedTreeRepo(t, map[string]string{"team/de-host.yaml": host})
	ageTree(t, dir)
	w := NewWriter(dir, dir)
	for i, mode := range allWriteModes {
		if err := writeVia(t, mode, w, "de-guest"); !errors.Is(err, ErrTenantDeclaredElsewhere) {
			t.Errorf("%s: err = %v, want ErrTenantDeclaredElsewhere", mode, err)
		}
		// From the second write on, de-host.yaml's declarations come from
		// the prior: by the mtime fast-path, or — after a PR path's base
		// checkout rewrote it with identical bytes — by the same-hash carry.
		// Either way the file is not parsed again.
		var f *cfg.TreeFile
		if prior := w.treePrior.Load(); prior != nil {
			f = prior.Files["team/de-host.yaml"]
		}
		if i > 0 && (f == nil || f.Parsed) {
			t.Errorf("%s: de-host.yaml was parsed again (a cold walk)", mode)
		}
	}

	// Drop de-guest (different size) with an old mtime: the stat moved, so
	// the file is re-read despite its age.
	writeFile(t, filepath.Join(dir, "team", "de-host.yaml"), tenantBody("de-host"))
	gitRun(t, dir, "commit", "-q", "-am", "drop de-guest")
	ageTree(t, dir)
	if err := writeVia(t, "Write", w, "de-guest"); err != nil {
		t.Errorf("de-guest is declared nowhere now: %v", err)
	}
}

// Only a walk that completed and was handed back becomes the prior. A walk
// abandoned at the timeout must not install its result when it finally
// returns, and a failed walk leaves the prior as it was.
func TestTreePrior_FailedWalkKeepsPrior(t *testing.T) {
	parent := t.TempDir()
	dir := filepath.Join(parent, "conf.d")
	writeFile(t, filepath.Join(dir, "fw-a.yaml"), tenantBody("fw-a"))
	w := &Writer{configDir: dir}
	good := mustScan(t, w)
	if w.treePrior.Load() != good {
		t.Fatalf("a completed walk did not become the prior")
	}
	if err := os.Rename(dir, filepath.Join(parent, "moved")); err != nil {
		t.Fatal(err)
	}
	if _, err := w.scanTree(); err == nil {
		t.Fatalf("walk of a missing configDir succeeded")
	}
	if w.treePrior.Load() != good {
		t.Errorf("a failed walk replaced the prior")
	}
}

// Readers without the writer lock (Diff, DryRunValidate) and writers walk
// concurrently; the prior is shared between them. Run with -race.
func TestTreePrior_ConcurrentWalks(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "cw-stable.yaml"), tenantBody("cw-stable"))
	for i := 0; i < 20; i++ {
		writeFile(t, filepath.Join(dir, "team", "cw-"+strings.Repeat("x", i+1)+".yaml"), tenantBody("cw-"+strings.Repeat("x", i+1)))
	}
	ageTree(t, dir)
	w := &Writer{configDir: dir}
	churn := filepath.Join(dir, "cw-churn.yaml")

	var wg sync.WaitGroup
	var bad atomic.Int32
	stop := make(chan struct{})
	wg.Add(1)
	go func() { // rewrites one file while the walkers run
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			_ = os.WriteFile(churn, []byte(tenantBody("cw-churn")+strings.Repeat("#", i%7)+"\n"), 0o644)
		}
	}()
	var walkers sync.WaitGroup
	for g := 0; g < 8; g++ {
		walkers.Add(1)
		go func() {
			defer walkers.Done()
			for i := 0; i < 15; i++ {
				s, err := w.scanTree()
				if err != nil {
					bad.Add(1)
					continue
				}
				if p, err := s.Locate("cw-stable"); err != nil || filepath.Base(p) != "cw-stable.yaml" {
					bad.Add(1)
				}
				if err := w.ensureNotDeclaredElsewhere("cw-stable", filepath.Join(dir, "cw-stable.yaml")); err != nil {
					bad.Add(1)
				}
			}
		}()
	}
	walkers.Wait()
	close(stop)
	wg.Wait()
	if n := bad.Load(); n != 0 {
		t.Fatalf("%d concurrent walks answered wrongly", n)
	}
}

// batchOp is a PRBatchOp whose merge result is body, whatever is on disk.
func batchOp(id, body string) PRBatchOp {
	return PRBatchOp{TenantID: id, Merge: func([]byte) (string, error) { return body, nil }}
}

// Every op of a batch walks conf.d again, and from the second op on that
// walk is served from the prior: a file no op touched is not parsed again.
func TestTreePriorBatch_LaterOpsWalkFromPrior(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{
		"lo-a.yaml":              tenantBody("lo-a"),
		"team/lo-bystander.yaml": tenantBody("lo-bystander"),
	})
	w := NewWriter(dir, dir)
	// onTreeScan runs as walk n starts, when treePrior is walk n-1. Walk 1 is
	// the cold one; every later walk must carry the untouched file.
	var walks atomic.Int32
	var parsedAgain []int32
	bystanderParsed := func() bool {
		f := w.treePrior.Load().Files["team/lo-bystander.yaml"]
		return f == nil || f.Parsed
	}
	w.onTreeScan = func() {
		if n := walks.Add(1); n >= 3 && bystanderParsed() {
			parsedAgain = append(parsedAgain, n-1)
		}
	}
	if _, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		batchOp("lo-a", "tenants:\n  lo-a:\n    _silent_mode: \"critical\"\n"),
		batchOp("lo-b", tenantBody("lo-b")),
		batchOp("lo-c", tenantBody("lo-c")),
	}, "op@example.com"); err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}
	if n := walks.Load(); n != 3 {
		t.Fatalf("a 3-op batch walked conf.d %d times, want one walk per op", n)
	}
	if bystanderParsed() {
		parsedAgain = append(parsedAgain, walks.Load())
	}
	if len(parsedAgain) > 0 {
		t.Errorf("untouched file parsed again by walk(s) %v", parsedAgain)
	}
}

// assertBatchRefusedAsDuplicate runs [create id, write id again] and wants the
// second op refused as a duplicate and the whole batch aborted.
func assertBatchRefusedAsDuplicate(t *testing.T, dir, id string) {
	t.Helper()
	w := NewWriter(dir, dir)
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		batchOp(id, tenantBody(id)),
		batchOp(id, "tenants:\n  "+id+":\n    _silent_mode: \"critical\"\n"),
	}, "op@example.com")
	var dup *cfg.DuplicateTenantError
	if !errors.Is(err, ErrTenantDeclaredElsewhere) || !errors.As(err, &dup) {
		t.Fatalf("err = %v, want ErrTenantDeclaredElsewhere carrying the duplicate the first op created", err)
	}
	assertCleanOnBase(t, dir, "main", "tenant-api/")
}

// B1 (A): a dangling link elsewhere in conf.d that points at the top-level
// file the first op creates is dropped by the walk before that op, and
// becomes a second declaration the moment the file exists.
func TestTreePriorBatch_DanglingLinkToCreatedFile(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"dl-other.yaml": tenantBody("dl-other")})
	if err := os.MkdirAll(filepath.Join(dir, "team"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join("..", "dl-new.yaml"), filepath.Join(dir, "team", "alias.yaml")); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	gitRun(t, dir, "add", "-A")
	gitRun(t, dir, "commit", "-q", "-m", "dangling alias")
	assertBatchRefusedAsDuplicate(t, dir, "dl-new")
}

// B1 (B): a subdirectory hardlink of the top-level file shares its bytes, so
// writing the tenant into the top-level file declares it twice.
func TestTreePriorBatch_HardlinkOfWrittenFile(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"hl-new.yaml": "tenants: {}\n"})
	if err := os.MkdirAll(filepath.Join(dir, "team"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(filepath.Join(dir, "hl-new.yaml"), filepath.Join(dir, "team", "hl-link.yaml")); err != nil {
		t.Skipf("hardlink unsupported: %v", err)
	}
	gitRun(t, dir, "add", "-A")
	gitRun(t, dir, "commit", "-q", "-m", "hardlink")
	assertBatchRefusedAsDuplicate(t, dir, "hl-new")
}

// An op that keeps a grandfathered co-tenant in its file keeps that
// co-tenant declared there for the ops after it.
func TestTreePriorBatch_KeptCoTenantStillRefused(t *testing.T) {
	both := "tenants:\n  kc-a:\n    _silent_mode: \"warning\"\n  kc-b:\n    _silent_mode: \"warning\"\n"
	dir := seedTreeRepo(t, map[string]string{"kc-a.yaml": both})
	w := NewWriter(dir, dir)
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		batchOp("kc-a", "tenants:\n  kc-a:\n    _silent_mode: \"critical\"\n  kc-b:\n    _silent_mode: \"warning\"\n"),
		batchOp("kc-b", tenantBody("kc-b")),
	}, "op@example.com")
	if !errors.Is(err, ErrTenantDeclaredElsewhere) {
		t.Fatalf("err = %v, want ErrTenantDeclaredElsewhere (kc-b still lives in kc-a.yaml)", err)
	}
	assertCleanOnBase(t, dir, "main", "tenant-api/")
}

// A tenant created by one op may be written again by a later op of the same
// batch: its own new file is the only declaration.
func TestTreePriorBatch_NewTenantTwice(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"nt-x.yaml": tenantBody("nt-x")})
	w := NewWriter(dir, dir)
	second := "tenants:\n  nt-new:\n    _silent_mode: \"critical\"\n"
	res, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		batchOp("nt-new", tenantBody("nt-new")),
		batchOp("nt-new", second),
	}, "op@example.com")
	if err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}
	gitRun(t, dir, "fetch", "-q", "origin", res.BranchName)
	if got := gitOut(t, dir, "show", "FETCH_HEAD:nt-new.yaml"); got != strings.TrimSpace(second) {
		t.Errorf("nt-new.yaml on the branch = %q, want the second op's body", got)
	}
}

// A duplicate that an earlier op resolves is judged afterwards as a single
// declaration elsewhere, not as the duplicate the base tree had.
func TestTreePriorBatch_DuplicateResolvedMidBatch(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{
		"dq-q.yaml":      "tenants:\n  dq-q:\n    _silent_mode: \"warning\"\n  dq-t:\n    _silent_mode: \"warning\"\n",
		"team/dq-t.yaml": tenantBody("dq-t"),
	})
	w := NewWriter(dir, dir)
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		batchOp("dq-q", tenantBody("dq-q")),
		batchOp("dq-t", tenantBody("dq-t")),
	}, "op@example.com")
	if !errors.Is(err, ErrTenantDeclaredElsewhere) {
		t.Fatalf("err = %v, want ErrTenantDeclaredElsewhere (dq-t still lives in team/)", err)
	}
	var dup *cfg.DuplicateTenantError
	if errors.As(err, &dup) {
		t.Errorf("err carries the base tree's duplicate %v; after op 1 dq-t has one declaring file", dup)
	}
}

// An op whose file is a symlink writes through it, into a file the walker
// also reads under its own name: the next op must see both declarations.
func TestTreePriorBatch_WriteThroughSymlinkIsSeen(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"team/sl-shell.yaml": "tenants: {}\n"})
	if err := os.Symlink(filepath.Join("team", "sl-shell.yaml"), filepath.Join(dir, "sl-a.yaml")); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	gitRun(t, dir, "add", "-A")
	gitRun(t, dir, "commit", "-q", "-m", "link")
	gitRun(t, dir, "push", "-q", "origin", "main")
	w := NewWriter(dir, dir)
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		batchOp("sl-a", tenantBody("sl-a")),
		batchOp("sl-a", "tenants:\n  sl-a:\n    _silent_mode: \"critical\"\n"),
	}, "op@example.com")
	var dup *cfg.DuplicateTenantError
	if !errors.Is(err, ErrTenantDeclaredElsewhere) || !errors.As(err, &dup) {
		t.Fatalf("err = %v, want the duplicate the first op's write-through created", err)
	}
}
