package federation

import (
	"encoding/json"
	"errors"
	"os"
	"sort"
	"sync"
	"time"
)

// store is the JSON-file-backed Record store. Federation token records
// are operational state, not GitOps configuration (ADR-020 Wave-0
// decision 4): they are machine-written on every issuance and must not
// generate a git commit each time. The whole file is rewritten under a
// mutex on each mutation — one tenant holds at most a handful of live
// 4h tokens, so the rewrite cost is negligible.
//
// A pod restart that loses the file costs only the GET listing: the
// signed JWTs remain valid (verification is stateless) until they
// expire. For production the store path should point at a mounted
// volume; see main.go's --federation-store flag.
type store struct {
	path string
	mu   sync.Mutex
	recs map[string]Record // keyed by TokenID
}

// newStore opens the Record store at path. A missing file is not an
// error — the store starts empty and the file is created on first
// mutation.
func newStore(path string) (*store, error) {
	s := &store{path: path, recs: make(map[string]Record)}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return s, nil
	}
	var recs []Record
	if err := json.Unmarshal(data, &recs); err != nil {
		return nil, err
	}
	for _, r := range recs {
		s.recs[r.TokenID] = r
	}
	return s, nil
}

// put inserts or replaces a Record and persists the store.
func (s *store) put(r Record) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(time.Now())
	s.recs[r.TokenID] = r
	return s.flushLocked()
}

// get returns the Record for tokenID. Expired-but-unpruned records are
// still returned; liveness is the caller's concern.
func (s *store) get(tokenID string) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.recs[tokenID]
	return r, ok
}

// list returns the non-expired Records for tenantID, oldest first.
func (s *store) list(tenantID string, now time.Time) []Record {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Record, 0)
	for _, r := range s.recs {
		if r.TenantID == tenantID && !r.expired(now) {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].IssuedAt.Before(out[j].IssuedAt) })
	return out
}

// remove deletes the Record for tokenID and persists the store. It
// reports whether a record was present.
func (s *store) remove(tokenID string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.recs[tokenID]; !ok {
		return false, nil
	}
	delete(s.recs, tokenID)
	if err := s.flushLocked(); err != nil {
		return false, err
	}
	return true, nil
}

// pruneLocked drops expired Records. The caller must hold s.mu.
// Expired records carry no security weight — the JWT is already past
// its exp — so pruning only keeps the file and GET listing tidy.
func (s *store) pruneLocked(now time.Time) {
	for id, r := range s.recs {
		if r.expired(now) {
			delete(s.recs, id)
		}
	}
}

// flushLocked writes the current Record set to disk via a temp file +
// rename so a crash mid-write cannot truncate the store. The caller
// must hold s.mu.
func (s *store) flushLocked() error {
	recs := make([]Record, 0, len(s.recs))
	for _, r := range s.recs {
		recs = append(recs, r)
	}
	sort.Slice(recs, func(i, j int) bool { return recs[i].IssuedAt.Before(recs[j].IssuedAt) })
	data, err := json.MarshalIndent(recs, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}
