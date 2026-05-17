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
// Storage (ADR-020 Posture B):
//   - Token Records are operational state — machine-written on every
//     issuance, high churn. The production store is a shared Kubernetes
//     ConfigMap (configmap_store.go) so tenant-api stays stateless and
//     can run multi-replica. The JSON-file store (store.go) is retained
//     only as the unit-test backend.
//   - DELETE is a real revocation: the token id is written to a revoked
//     set that the API gateway consults (sub-issue IV-2b). A revoked
//     token is rejected within the ConfigMap projected-volume sync
//     window (~1-2 min).
package federation

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
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

// audience is the JWT `aud` claim. Binding every federation token to a
// single audience lets a verifier reject a token replayed against any
// other API that happens to trust the same signing key (cross-service
// replay). ADR-020 Wave-0 decision 3.
const audience = "tenant-federation"

// clockSkewLeeway backdates the JWT `iat` claim. A signer clock a few
// seconds ahead of the verifier would otherwise place `iat` in the
// verifier's future and get a just-issued token rejected (ADR-020;
// Gemini round-7 review). The gateway verifier sets a matching leeway.
const clockSkewLeeway = time.Minute

// tokenIDPrefix namespaces the public token handle. The audit log
// (sub-issue IV-2f) records a token_id_prefix, so a recognisable
// prefix keeps those lines greppable.
const tokenIDPrefix = "ftk_"

// minRSAKeyBits is the smallest RSA modulus accepted for the signing
// key. Below 2048 bits a forged signature becomes computationally
// feasible, so a weak key is a silent vulnerability — it is rejected
// at load time rather than allowed to sign tokens.
const minRSAKeyBits = 2048

// maxTokensPerTenant caps the live federation tokens one tenant may
// hold. It is an abuse guard, not a precise quota — concurrent
// issuance may exceed it slightly, which is acceptable. A tenant with
// this many live 4h tokens is almost certainly misbehaving (an
// issuance loop, or never letting old tokens expire).
const maxTokensPerTenant = 16

// ErrTokenLimitReached is returned by Issue when the tenant already
// holds maxTokensPerTenant live tokens.
var ErrTokenLimitReached = errors.New("federation: tenant federation-token limit reached")

// ErrMintRateLimited is returned by Issue when a tenant mints tokens
// faster than the per-tenant rate limit allows.
var ErrMintRateLimited = errors.New("federation: token mint rate limit exceeded")

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

// RecordStore is the persistence backend for token Records. Two
// implementations exist: the production ConfigMap-backed store
// (configmap_store.go) and the JSON-file store (store.go, unit tests
// only). Every method may touch a network-backed store, so all may
// fail. The interface methods are unexported — it is implemented only
// within this package.
type RecordStore interface {
	put(r Record) error
	get(tokenID string) (Record, bool, error)
	list(tenantID string, now time.Time) ([]Record, error)
	revoke(tokenID string, expiresAt time.Time) (bool, error)
}

// Manager signs federation tokens and tracks issued-token Records.
// It is safe for concurrent use: the signing key is immutable after
// construction and the store guards its own state.
type Manager struct {
	key   *rsa.PrivateKey
	kid   string // RFC 7638 thumbprint of the public key; the JWT `kid`.
	ttl   time.Duration
	store RecordStore
	mints *mintLimiter
}

// NewManager loads the RS256 signing key from keyPath (a PEM file,
// PKCS#1 or PKCS#8) and builds a Manager around the given RecordStore.
// A non-positive ttl falls back to DefaultTTL. The caller constructs
// the store — NewConfigMapStore in production.
//
// When keyPath is empty NewManager returns (nil, nil): the federation
// feature is then disabled and the caller leaves the /federation
// routes unregistered — the same optional-dependency pattern main.go
// uses for the PR tracker.
func NewManager(keyPath string, store RecordStore, ttl time.Duration) (*Manager, error) {
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
	return &Manager{key: key, kid: keyID(&key.PublicKey), ttl: ttl, store: store, mints: newMintLimiter()}, nil
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
	return &Manager{key: key, kid: keyID(&key.PublicKey), ttl: ttl, store: st, mints: newMintLimiter()}, nil
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
	if !m.mints.allow(tenantID, now) {
		return "", Record{}, ErrMintRateLimited
	}
	live, err := m.store.list(tenantID, now)
	if err != nil {
		return "", Record{}, fmt.Errorf("federation: check token limit: %w", err)
	}
	if len(live) >= maxTokensPerTenant {
		return "", Record{}, ErrTokenLimitReached
	}
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
			Audience:  jwt.ClaimStrings{audience},
			IssuedAt:  jwt.NewNumericDate(now.Add(-clockSkewLeeway)),
			ExpiresAt: jwt.NewNumericDate(now.Add(m.ttl)),
		},
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	// Stamp the key id so the gateway verifier selects the right JWKS key
	// by `kid` rather than trying every key (ADR-020 IV-2l).
	tok.Header["kid"] = m.kid
	signed, err := tok.SignedString(m.key)
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
func (m *Manager) List(tenantID string) ([]Record, error) {
	return m.store.list(tenantID, time.Now())
}

// Get returns the Record for tokenID, or false if no such record
// exists. An expired-but-not-yet-pruned record is still returned —
// callers that care about liveness check ExpiresAt themselves.
func (m *Manager) Get(tokenID string) (Record, bool, error) {
	return m.store.get(tokenID)
}

// Delete revokes a token: it removes the bookkeeping Record and adds
// the token id to the revoked set (ADR-020 Posture B). expiresAt is
// the token's JWT expiry — the revoked entry self-prunes once past it.
// Reports whether a record was present.
func (m *Manager) Delete(tokenID string, expiresAt time.Time) (bool, error) {
	return m.store.revoke(tokenID, expiresAt)
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
// encodings. A key smaller than minRSAKeyBits is rejected.
func loadPrivateKey(path string) (*rsa.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block found in key file")
	}
	key, err := parseRSAPrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	if bits := key.N.BitLen(); bits < minRSAKeyBits {
		return nil, fmt.Errorf("RSA signing key is %d-bit, want at least %d-bit", bits, minRSAKeyBits)
	}
	return key, nil
}

// parseRSAPrivateKey decodes DER bytes as an RSA private key, trying
// PKCS#8 first then PKCS#1.
func parseRSAPrivateKey(der []byte) (*rsa.PrivateKey, error) {
	if k, err := x509.ParsePKCS8PrivateKey(der); err == nil {
		rk, ok := k.(*rsa.PrivateKey)
		if !ok {
			return nil, fmt.Errorf("key is %T, want an RSA private key", k)
		}
		return rk, nil
	}
	rk, err := x509.ParsePKCS1PrivateKey(der)
	if err != nil {
		return nil, fmt.Errorf("parse RSA private key: %w", err)
	}
	return rk, nil
}

// keyID returns the RFC 7638 JWK thumbprint of an RSA public key: the
// SHA-256 of the canonical JWK — the required members {e, kty, n} in
// lexicographic order, no whitespace — base64url-encoded. It is stamped
// on every issued token as the `kid` header so the gateway verifier
// selects the matching JWKS key by id. Without it a JWKS holding more
// than one key (as during a rotation) forces the verifier to try each
// key, multiplying the RSA cost of a bad-signature flood. The
// `da-tools fed-key` tool computes the identical thumbprint for the
// JWKS, so signer and verifier agree on the kid by construction.
func keyID(pub *rsa.PublicKey) string {
	// json.Marshal of a map emits keys in lexicographic order with no
	// whitespace — exactly RFC 7638's canonical form.
	canonical, _ := json.Marshal(map[string]string{
		"e":   base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes()),
		"kty": "RSA",
		"n":   base64.RawURLEncoding.EncodeToString(pub.N.Bytes()),
	})
	sum := sha256.Sum256(canonical)
	return base64.RawURLEncoding.EncodeToString(sum[:])
}
