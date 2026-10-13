//go:build unix

package config

import (
	"bytes"
	"errors"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// #2939: a config-named entry that is neither a regular file nor a directory
// (a FIFO, a socket, a device — or a symlink to one) is never opened by ANY
// mode of the walk. Before the fix only walkListOnly (parse-tree, #2897)
// refused it; the exporter's walk (walkFull) and the root-only modes
// (da-guard's carrier read, tenant-api's merge core) read it, and reading a
// FIFO with no writer blocks forever — a reload tick, `da-guard
// served-values`, tenant-api's /effective all hung on one `mkfifo`.
//
// Every walk below runs under a deadline, so a regression fails the test
// instead of hanging it.

// specialFileReason is the wire value of the reason, spelled out so the
// test also pins what the metric label and da-guard's JSON say.
const specialFileReason = "special_file"

// mkfifoOrSkip makes a FIFO at p, skipping the test where the platform or
// filesystem cannot. The cleanup opens and closes the write side so a reader
// left blocked on it by a failing run returns.
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

// scanWithin runs walk under a deadline; a walk that blocks fails the test.
func scanWithin(t *testing.T, what string, walk func() (*TreeScan, error)) *TreeScan {
	t.Helper()
	type result struct {
		scan *TreeScan
		err  error
	}
	done := make(chan result, 1)
	go func() {
		s, err := walk()
		done <- result{s, err}
	}()
	select {
	case r := <-done:
		if r.err != nil {
			t.Fatalf("%s: %v", what, r.err)
		}
		return r.scan
	case <-time.After(10 * time.Second):
		t.Fatalf("%s blocked reading a FIFO", what)
	}
	return nil
}

// specialTree is a tree with two readable files and three special entries:
// a FIFO tenant file, a FIFO defaults file one level down, and a symlink
// whose target is a FIFO outside the tree.
func specialTree(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	testutil.WriteTree(t, root, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"t1.yaml":        "tenants:\n  t1:\n    mysql_connections: 70\n",
		"sub/t2.yaml":    "tenants:\n  t2:\n    mysql_connections: 60\n",
	})
	mkfifoOrSkip(t, filepath.Join(root, "stuck.yaml"))
	mkfifoOrSkip(t, filepath.Join(root, "sub", "_defaults.yaml"))
	outside := filepath.Join(t.TempDir(), "pipe.yaml")
	mkfifoOrSkip(t, outside)
	if err := os.Symlink(outside, filepath.Join(root, "link.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	return root
}

var wantSpecialUnreadable = []UnreadableFile{
	{RelKey: "link.yaml", Reason: specialFileReason},
	{RelKey: "stuck.yaml", Reason: specialFileReason},
	{RelKey: "sub/_defaults.yaml", Reason: specialFileReason},
}

// The exporter's walk and parse-tree's walk answer the same: each special
// entry is in Unreadable with special_file, in no other map, and nothing
// blocks. One judgement for both — the parity is the point of the fix.
func TestScanDirTree_SpecialFilesAreNotRead(t *testing.T) {
	t.Parallel()
	root := specialTree(t)
	full := scanWithin(t, "ScanDirTree", func() (*TreeScan, error) { return ScanDirTree(root, nil, nil, discardLogger) })
	list := scanWithin(t, "ScanDirTreeFiles", func() (*TreeScan, error) { return ScanDirTreeFiles(root, discardLogger) })
	for name, s := range map[string]*TreeScan{"ScanDirTree": full, "ScanDirTreeFiles": list} {
		if !reflect.DeepEqual(s.Unreadable, wantSpecialUnreadable) {
			t.Errorf("%s: Unreadable = %+v, want %+v", name, s.Unreadable, wantSpecialUnreadable)
		}
		if s.NotFiles != nil {
			t.Errorf("%s: NotFiles = %+v, want none", name, s.NotFiles)
		}
		if want := []string{"_defaults.yaml", "sub/t2.yaml", "t1.yaml"}; !reflect.DeepEqual(s.Keys, want) {
			t.Errorf("%s: Keys = %v, want %v", name, s.Keys, want)
		}
	}
	if full.Conflict != nil || full.Tenants["t1"] == "" || full.Tenants["t2"] == "" {
		t.Errorf("ScanDirTree: tenants %v (conflict %v), want t1 and t2", full.Tenants, full.Conflict)
	}
}

// A reload tick walks with the previous scan as prior. A file that was a
// regular file on the last tick and is a FIFO now is refused before the
// mtime fast path or the read can reach it.
func TestScanDirTree_FileReplacedByFIFO_WarmScan(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	testutil.WriteTree(t, root, map[string]string{
		"t1.yaml": "tenants:\n  t1:\n    mysql_connections: 70\n",
		"t2.yaml": "tenants:\n  t2:\n    mysql_connections: 60\n",
	})
	ageTree(t, root)
	prior := scanWithin(t, "cold ScanDirTree", func() (*TreeScan, error) { return ScanDirTree(root, nil, nil, discardLogger) })
	p := filepath.Join(root, "t2.yaml")
	if err := os.Remove(p); err != nil {
		t.Fatal(err)
	}
	mkfifoOrSkip(t, p)
	warm := scanWithin(t, "warm ScanDirTree", func() (*TreeScan, error) { return ScanDirTree(root, prior, nil, discardLogger) })
	if want := []UnreadableFile{{RelKey: "t2.yaml", Reason: specialFileReason}}; !reflect.DeepEqual(warm.Unreadable, want) {
		t.Errorf("Unreadable = %+v, want %+v", warm.Unreadable, want)
	}
	if _, ok := warm.Tenants["t2"]; ok {
		t.Errorf("t2 still attributed to %q after its file became a FIFO", warm.Tenants["t2"])
	}
}

// The root-only modes (da-guard's carrier read, tenant-api's merge core)
// use the same judgement: a FIFO `_defaults.yaml` is special_file there too.
func TestRootOnlyScans_SpecialFileIsNotRead(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	testutil.WriteTree(t, root, map[string]string{
		"t1.yaml": "tenants:\n  t1:\n    mysql_connections: 70\n",
	})
	mkfifoOrSkip(t, filepath.Join(root, "_defaults.yaml"))
	want := []UnreadableFile{{RelKey: "_defaults.yaml", Reason: specialFileReason}}
	for name, walk := range map[string]func() (*TreeScan, error){
		"scanRootDefaults": func() (*TreeScan, error) { return scanRootDefaults(root) },
		"scanRootPlatform": func() (*TreeScan, error) { return scanRootPlatform(root) },
	} {
		s := scanWithin(t, name, walk)
		if !reflect.DeepEqual(s.Unreadable, want) {
			t.Errorf("%s: Unreadable = %+v, want %+v", name, s.Unreadable, want)
		}
		if len(s.Files) != 0 {
			t.Errorf("%s: Files = %v, want none", name, s.Keys)
		}
	}
}

// ReadConfFile is the re-read every reader of a kept path makes (a warm scan
// carries files unread). It judges the opened fd with the walk's
// IsSpecialFile, so an entry swapped for a special file after the walk is
// refused (ErrSpecialFile) without blocking; anything else reads as
// os.ReadFile does.
func TestReadConfFile(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	regular := filepath.Join(dir, "t1.yaml")
	body := bytes.Repeat([]byte("a: 1\n"), 300) // past the 512-byte floor
	if err := os.WriteFile(regular, body, 0o600); err != nil {
		t.Fatal(err)
	}
	fifo := filepath.Join(dir, "fifo.yaml")
	mkfifoOrSkip(t, fifo)
	link := filepath.Join(dir, "link.yaml")
	if err := os.Symlink(fifo, link); err != nil {
		t.Skipf("symlink: %v", err)
	}
	special := map[string]string{"fifo": fifo, "link to fifo": link, "char device": os.DevNull}
	// A unix socket fails already at open (ENXIO); short path, sun_path is small.
	if sdir, err := os.MkdirTemp("", "rcf"); err == nil {
		t.Cleanup(func() { _ = os.RemoveAll(sdir) })
		sock := filepath.Join(sdir, "s.yaml")
		if l, lerr := net.Listen("unix", sock); lerr == nil {
			t.Cleanup(func() { _ = l.Close() })
			special["socket"] = sock
		}
	}
	for name, p := range special {
		done := make(chan error, 1)
		go func() {
			_, err := ReadConfFile(p)
			done <- err
		}()
		select {
		case err := <-done:
			if !errors.Is(err, ErrSpecialFile) {
				t.Errorf("%s: err = %v, want ErrSpecialFile", name, err)
			}
		case <-time.After(10 * time.Second):
			t.Fatalf("%s: ReadConfFile blocked", name)
		}
	}
	if got, err := ReadConfFile(regular); err != nil || !bytes.Equal(got, body) {
		t.Errorf("regular file: %d bytes, err %v; want %d bytes", len(got), err, len(body))
	}
	if _, err := ReadConfFile(dir); err == nil || errors.Is(err, ErrSpecialFile) {
		t.Errorf("directory: err = %v, want the read error os.ReadFile gives", err)
	}
	if _, err := ReadConfFile(filepath.Join(dir, "missing.yaml")); !errors.Is(err, fs.ErrNotExist) {
		t.Errorf("missing: err = %v, want fs.ErrNotExist", err)
	}
}
