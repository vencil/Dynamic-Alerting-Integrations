// Package federation implements federation-token issuance for ADR-020
// (Tenant Federation — Label-Injection Proxy over Self-Built Endpoint).
//
// A federation token is a short-lived (default 4h) RS256-signed JWT a
// tenant presents to the label-injection proxy (vmauth / prom-label-proxy)
// to pull its own metrics subset back to tenant-side infrastructure.
// tenant-api is the *signer only*: the proxy and API gateway verify the
// signature with the public half of the key and never call back here, so
// verification is fully stateless.
//
// MVP scope (ADR-020 §Token model):
//   - No server-side revocation list. A leaked token stays valid until
//     its exp claim (≤ TTL). The compensating control is the API-gateway
//     per-token rate limit (sub-issue IV-2b). DELETE removes only the
//     local bookkeeping Record — it does not invalidate the JWT.
//   - Token Records are operational state (machine-written on every
//     issuance, high churn). They persist to a dedicated JSON store,
//     deliberately NOT to the git-backed conf.d directory, so issuance
//     does not generate a commit per token.
package federation

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// DefaultTTL is the federation-token lifetime when none is configured.
// ADR-020 §Token model fixes 4h as the balance point between "short
// enough that the absence of a revocation list is acceptable" and
// "long enough that re-signing is not operationally painful".
const DefaultTTL = 4 * time.Hour

// issuer is the JWT `iss` claim — identifies tenant-api as the signer.
const issuer = "tenant-api"

// tokenIDPrefix namespaces the public token handle. The audit log
// (sub-issue IV-2f) records a token_id_prefix, so a recognisable
// prefix keeps those lines greppable.
const tokenIDPrefix = "ftk_"

// Claims is the JWT payload of a federation token.
//
// TenantID and TokenID are the cross-component contract fixed by
// ADR-020 Wave-0 decision 3: the proxy reads TenantID to inject
// {tenant_id="<X>"} into every selector, and the API gateway reads
// TokenID as the per-token rate-limit key.
type Claims struct {
	TenantID string `json:"tenant_id"`
	TokenID  string `json:"token_id"`
	jwt.RegisteredClaims
}

// Record is the bookkeeping metadata for one issued token. The signed
// JWT itself is never stored — only what GET needs to list issued
// tokens and DELETE needs to identify one. TokenID is the public handle.
type Record struct {
	TokenID     string    `json:"token_id"`
	TenantID    string    `json:"tenant_id"`
	IssuedBy    string    `json:"issued_by"`
	Description string    `json:"description,omitempty"`
	IssuedAt    time.Time `json:"issued_at"`
	ExpiresAt   time.Time `json:"expires_at"`
}

// expired reports whether the record is past its expiry as of now.
func (r Record) expired(now time.Time) bool { return now.After(r.ExpiresAt) }

// Manager signs federation tokens and tracks issued-token Records.
// It is safe for concurrent use: the signing key is immutable after
// construction and the store guards its own state.
type Manager struct {
	key   *rsa.PrivateKey
	ttl   time.Duration
	store *store
}

// NewManager loads the RS256 signing key from keyPath (a PEM file,
// PKCS#1 or PKCS#8) and opens the Record store at storePath. A
// non-positive ttl falls back to DefaultTTL.
//
// When keyPath is empty NewManager returns (nil, nil): the federation
// feature is then disabled and the caller leaves the /federation
// routes unregistered — the same optional-dependency pattern main.go
// uses for the PR tracker.
func NewManager(keyPath, storePath string, ttl time.Duration) (*Manager, error) {
	if keyPath == "" {
		return nil, nil
	}
	key, err := loadPrivateKey(keyPath)
	if err != nil {
		return nil, fmt.Errorf("federation: load signing key: %w", err)
	}
	if ttl <= 0 {
		ttl = DefaultTTL
	}
	st, err := newStore(storePath)
	if err != nil {
		return nil, fmt.Errorf("federation: open token store: %w", err)
	}
	return &Manager{key: key, ttl: ttl, store: st}, nil
}

// NewManagerForTest builds a Manager around an in-memory signing key,
// skipping PEM file loading. The Record store is created at storePath;
// a non-positive ttl falls back to DefaultTTL. Intended for unit tests
// of this package and of the HTTP handlers that depend on it.
func NewManagerForTest(key *rsa.PrivateKey, storePath string, ttl time.Duration) (*Manager, error) {
	if ttl <= 0 {
		ttl = DefaultTTL
	}
	st, err := newStore(storePath)
	if err != nil {
		return nil, err
	}
	return &Manager{key: key, ttl: ttl, store: st}, nil
}

// TTL returns the configured token lifetime.
func (m *Manager) TTL() time.Duration { return m.ttl }

// Issue mints a signed federation JWT for tenantID and persists its
// Record. issuedBy is the operator email (audit trail); description is
// an optional free-text label. The returned token string is the
// compact-serialised JWT — tenant-api does not store it and cannot
// re-display it, so the caller must surface it to the operator once.
func (m *Manager) Issue(tenantID, issuedBy, description string) (string, Record, error) {
	now := time.Now()
	tokenID, err := newTokenID()
	if err != nil {
		return "", Record{}, err
	}
	claims := Claims{
		TenantID: tenantID,
		TokenID:  tokenID,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    issuer,
			Subject:   tenantID,
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(m.ttl)),
		},
	}
	signed, err := jwt.NewWithClaims(jwt.SigningMethodRS256, claims).SignedString(m.key)
	if err != nil {
		return "", Record{}, fmt.Errorf("federation: sign token: %w", err)
	}
	rec := Record{
		TokenID:     tokenID,
		TenantID:    tenantID,
		IssuedBy:    issuedBy,
		Description: description,
		IssuedAt:    now,
		ExpiresAt:   now.Add(m.ttl),
	}
	if err := m.store.put(rec); err != nil {
		return "", Record{}, fmt.Errorf("federation: persist token record: %w", err)
	}
	return signed, rec, nil
}

// List returns the non-expired Records for tenantID, oldest first.
func (m *Manager) List(tenantID string) []Record {
	return m.store.list(tenantID, time.Now())
}

// Get returns the Record for tokenID, or false if no such record
// exists. An expired-but-not-yet-pruned record is still returned —
// callers that care about liveness check ExpiresAt themselves.
func (m *Manager) Get(tokenID string) (Record, bool) {
	return m.store.get(tokenID)
}

// Delete removes the bookkeeping Record for tokenID and reports
// whether a record was present. Per ADR-020 there is no server-side
// revocation: a still-valid JWT keeps working until its exp even
// after its Record is deleted.
func (m *Manager) Delete(tokenID string) (bool, error) {
	return m.store.remove(tokenID)
}

// newTokenID returns a fresh public token handle: tokenIDPrefix plus
// 16 hex characters (8 random bytes).
func newTokenID() (string, error) {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("federation: generate token id: %w", err)
	}
	return tokenIDPrefix + hex.EncodeToString(b), nil
}

// loadPrivateKey reads an RSA private key from a PEM file, accepting
// both PKCS#8 (BEGIN PRIVATE KEY) and PKCS#1 (BEGIN RSA PRIVATE KEY)
// encodings.
func loadPrivateKey(path string) (*rsa.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block found in key file")
	}
	if k, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		rk, ok := k.(*rsa.PrivateKey)
		if !ok {
			return nil, fmt.Errorf("key is %T, want an RSA private key", k)
		}
		return rk, nil
	}
	rk, err := x509.ParsePKCS1PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse RSA private key: %w", err)
	}
	return rk, nil
}
