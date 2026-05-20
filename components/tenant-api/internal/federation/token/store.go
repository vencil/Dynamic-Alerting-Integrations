package token

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
//
// MVP constraint: the store is pod-local. With tenant-api scaled past
// one replica, GET listings become per-replica and inconsistent
// (token signing and proxy-side verification are unaffected — both are
// stateless). v2.9.0 ships with replicaCount=1; a multi-replica
// deployment needs a shared store, which is also the prerequisite for
// a future server-side revocation list.
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
	// Drop a stale temp file left by a crash mid-flush (write-temp +
	// rename). It is at most a partial / unrenamed write; the real file
	// is the authoritative state, so the temp is just litter.
	_ = os.Remove(path + ".tmp")
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

// put inserts or replaces a Record and persists the store. Inserting a
// new token id is rejected once the tenant holds maxTokensPerTenant
// live records — the cap check and the insert happen under the same
// mutex, so the check cannot race the insert (the configMapStore makes
// the equivalent guarantee inside its RetryOnConflict closure).
func (s *store) put(r Record) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(time.Now())
	if _, replacing := s.recs[r.TokenID]; !replacing {
		live := 0
		for _, rec := range s.recs {
			if rec.TenantID == r.TenantID {
				live++
			}
		}
		if live >= maxTokensPerTenant {
			return ErrTokenLimitReached
		}
	}
	s.recs[r.TokenID] = r
	return s.flushLocked()
}

// get returns the Record for tokenID. Expired-but-unpruned records are
// still returned; liveness is the caller's concern. The error return
// is always nil — it exists to satisfy the RecordStore interface,
// whose ConfigMap implementation can fail.
func (s *store) get(tokenID string) (Record, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.recs[tokenID]
	return r, ok, nil
}

// list returns the non-expired Records for tenantID, oldest first.
func (s *store) list(tenantID string, now time.Time) ([]Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Record, 0)
	for _, r := range s.recs {
		if r.TenantID == tenantID && !r.expired(now) {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].IssuedAt.Before(out[j].IssuedAt) })
	return out, nil
}

// listAll returns every non-expired Record across all tenants, oldest
// first. Used by the OrphanDetector (#521); the GET listing uses the
// per-tenant list instead.
func (s *store) listAll(now time.Time) ([]Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Record, 0, len(s.recs))
	for _, r := range s.recs {
		if !r.expired(now) {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].IssuedAt.Before(out[j].IssuedAt) })
	return out, nil
}

// revoke removes the Record for tokenID and persists the store,
// reporting whether a record was present. The JSON store is the
// unit-test backend only and has no gateway-facing revoked set, so the
// expiresAt argument is unused here — the production ConfigMap store
// (configmap_store.go) is what writes the revoked set.
func (s *store) revoke(tokenID string, _ time.Time) (bool, error) {
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
