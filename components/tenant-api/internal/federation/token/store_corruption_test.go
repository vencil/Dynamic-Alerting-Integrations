package token

// Corruption / IO-failure coverage for the JSON-file Record store (ROI
// refactor R3, E4). Scope note: this store is the unit-test / local-dev
// backend — production uses the ConfigMap-backed store (configmap_store.go).
// The load-bearing behavior verified here: newStore on a
// CORRUPTED store file must FAIL LOUD (return the parse error) — never
// silently start empty, which would drop every live record's listing and
// mask the corruption until an operator wonders where the tokens went.
// (Verified against current behavior: newStore propagates the
// json.Unmarshal error and leaves the file untouched — correct, no bug.)

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestNewStore_CorruptedJSONFailsLoud(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "store.json")
	const garbage = "{{{ not json at all"
	if err := os.WriteFile(path, []byte(garbage), 0o600); err != nil {
		t.Fatal(err)
	}

	if _, err := newStore(path); err == nil {
		t.Fatal("newStore on a corrupted file returned nil error — records silently wiped")
	}
	// The corrupted file must be left in place for forensics — a failed open
	// must not truncate or rewrite it.
	got, err := os.ReadFile(path)
	if err != nil || string(got) != garbage {
		t.Errorf("corrupted store file changed by a failed newStore (err=%v):\n%s", err, got)
	}
}

func TestNewStore_TruncatedJSONFailsLoud(t *testing.T) {
	t.Parallel()
	// A crash mid-write of the FINAL file (not the .tmp) leaves valid-prefix
	// truncated JSON — must also fail loud, same as full garbage.
	path := filepath.Join(t.TempDir(), "store.json")
	if err := os.WriteFile(path, []byte(`[{"token_id":"ftk_x","tenant`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := newStore(path); err == nil {
		t.Fatal("newStore on truncated JSON returned nil error")
	}
}

func TestNewStore_EmptyFileStartsEmpty(t *testing.T) {
	t.Parallel()
	// An empty file is NOT corruption (first boot with a pre-created mount
	// file) — the store starts empty without error.
	path := filepath.Join(t.TempDir(), "store.json")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := newStore(path)
	if err != nil {
		t.Fatalf("newStore on an empty file: %v", err)
	}
	recs, err := s.listAll(time.Now())
	if err != nil || len(recs) != 0 {
		t.Errorf("empty-file store: listAll = %v, %v; want empty, nil", recs, err)
	}
}

func TestNewStore_ReadErrorPropagates(t *testing.T) {
	t.Parallel()
	// A path that exists but cannot be read as a file (it is a directory)
	// must surface the read error — only ENOENT means "start empty".
	dir := t.TempDir()
	if _, err := newStore(dir); err == nil {
		t.Fatal("newStore on a directory path returned nil error")
	}
}

func TestStore_PutFlushErrorPropagates(t *testing.T) {
	t.Parallel()
	// Store path in a directory that does not exist: the temp-file write in
	// flushLocked fails and put must report it (the caller treats the token
	// as not persisted).
	path := filepath.Join(t.TempDir(), "missing-subdir", "store.json")
	s, err := newStore(path) // ENOENT on read → valid empty store
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	now := time.Now()
	err = s.put(Record{TokenID: "ftk_flush", TenantID: "tenant-e4", IssuedAt: now, ExpiresAt: now.Add(time.Hour)})
	if err == nil {
		t.Fatal("put with an unwritable store path returned nil error")
	}
}

func TestStore_RevokeFlushErrorPropagates(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	s, err := newStore(filepath.Join(dir, "store.json"))
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	now := time.Now()
	rec := Record{TokenID: "ftk_rev_e4", TenantID: "tenant-e4", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	if err := s.put(rec); err != nil {
		t.Fatalf("put: %v", err)
	}

	// Break the flush target AFTER the record is stored (in-package test —
	// redirect the path into a directory that does not exist).
	s.path = filepath.Join(dir, "missing-subdir", "store.json")
	ok, err := s.revoke(rec.TokenID, rec.ExpiresAt)
	if err == nil {
		t.Fatal("revoke with an unwritable store path returned nil error")
	}
	if ok {
		t.Error("revoke reported success despite the flush failing")
	}
	if !strings.Contains(err.Error(), "missing-subdir") {
		t.Logf("note: flush error does not name the path: %v", err) // informational only
	}
}
