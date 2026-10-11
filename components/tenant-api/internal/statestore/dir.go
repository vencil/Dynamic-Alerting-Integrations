package statestore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// dirStore keeps every record as one file named after its key in a plain
// directory. It is for deployments without Kubernetes (try-local's docker
// compose). The directory must not be inside the conf.d git tree — the
// writer would see these files as untracked config.
//
// Writes are serialised twice: mu within this process, and an exclusive
// flock on lockFile across processes (#2884) — `tenant-api state restore`
// runs as a second process beside the server, and a read-transform-write
// that two processes interleave loses one of them.
type dirStore struct {
	dir     string
	onWrite OnWrite
	logger  *slog.Logger
	mu      sync.Mutex
}

// NewDir returns a Store backed by dir, creating it if needed.
func NewDir(dir string, onWrite OnWrite) (Store, error) {
	if dir == "" {
		return nil, errors.New("statestore: state directory is empty")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("statestore: create %s: %w", dir, err)
	}
	return &dirStore{dir: dir, onWrite: onWrite}, nil
}

// lockFile is the file dirStore flocks for a write. Dot files are not
// records: Keys skips them, as it skips the ".tmp-" files of writeAtomic.
const lockFile = ".lock"

func (s *dirStore) Describe() string { return "dir " + s.dir }

func (s *dirStore) log() *slog.Logger {
	if s.logger != nil {
		return s.logger
	}
	return slog.Default()
}

// path maps a key to its file. Keys never contain a path separator (they
// are the constants and SubsetKey of a validated tenant id); refuse one that
// does rather than write outside dir, and a dot file, which is the store's
// own (lockFile, writeAtomic's temp files).
func (s *dirStore) path(key string) (string, error) {
	if key == "" || key != filepath.Base(key) || strings.ContainsAny(key, `/\`) || strings.Contains(key, "..") || strings.HasPrefix(key, ".") {
		return "", fmt.Errorf("statestore: invalid key %q", key)
	}
	return filepath.Join(s.dir, key), nil
}

func (s *dirStore) Get(_ context.Context, key string) ([]byte, bool, error) {
	p, err := s.path(key)
	if err != nil {
		return nil, false, err
	}
	b, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("%w: read %s: %v", ErrUnavailable, p, err)
	}
	return b, true, nil
}

func (s *dirStore) Keys(_ context.Context, prefix string) ([]string, error) {
	ents, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, fmt.Errorf("%w: list %s: %v", ErrUnavailable, s.dir, err)
	}
	var keys []string
	for _, e := range ents {
		n := e.Name()
		if e.Type().IsRegular() && strings.HasPrefix(n, prefix) && !strings.HasPrefix(n, ".") {
			keys = append(keys, n)
		}
	}
	sort.Strings(keys)
	return keys, nil
}

func (s *dirStore) Mutate(ctx context.Context, key string, m Meta, t Transform) (bool, error) {
	p, err := s.path(key)
	if err != nil {
		return false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	unlock, err := lockDir(filepath.Join(s.dir, lockFile))
	if err != nil {
		return false, fmt.Errorf("%w: lock %s: %v", ErrUnavailable, s.dir, err)
	}
	defer unlock()

	cur, exists, err := s.Get(ctx, key)
	if err != nil {
		return false, err
	}
	if !exists {
		cur = nil
	}
	next, err := t(cur)
	if err != nil {
		return false, err
	}
	if next == nil || (cur != nil && bytes.Equal(next, cur)) {
		return false, nil
	}
	if err := writeAtomic(s.dir, p, next); err != nil {
		return false, fmt.Errorf("%w: write %s: %v", ErrUnavailable, p, err)
	}
	audit(s.log(), s.Describe(), key, m)
	if s.onWrite != nil {
		s.onWrite(m.Entity)
	}
	return true, nil
}

// writeAtomic writes data to path via a synced temp file and a rename, so a
// reader never sees a half-written record and a crash keeps the old one.
func writeAtomic(dir, path string, data []byte) error {
	f, err := os.CreateTemp(dir, ".tmp-")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // no-op after a successful rename
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
