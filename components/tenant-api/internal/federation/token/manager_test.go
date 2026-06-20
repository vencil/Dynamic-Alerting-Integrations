package token

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// writeTestKey generates a 2048-bit RSA key, writes it as a PKCS#8 PEM
// file under t.TempDir(), and returns the path plus the key (the key
// is used to verify signatures in assertions).
func writeTestKey(t *testing.T) (string, *rsa.PrivateKey) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}
	path := filepath.Join(t.TempDir(), "fed-key.pem")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatalf("write key: %v", err)
	}
	return path, key
}

// newJSONStore returns a fresh JSON-file RecordStore under t.TempDir().
func newJSONStore(t *testing.T) RecordStore {
	t.Helper()
	st, err := newStore(filepath.Join(t.TempDir(), "store.json"))
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	return st
}

func newTestManager(t *testing.T, ttl time.Duration) *Manager {
	t.Helper()
	keyPath, _ := writeTestKey(t)
	m, err := NewManager(keyPath, newJSONStore(t), ttl)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if m == nil {
		t.Fatal("NewManager returned nil with a key path set")
	}
	return m
}

func TestNewManager_EmptyKeyPathDisablesFeature(t *testing.T) {
	t.Parallel()
	m, err := NewManager("", nil, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if m != nil {
		t.Fatal("expected nil Manager when key path is empty")
	}
}

func TestNewManager_DefaultTTL(t *testing.T) {
	t.Parallel()
	m := newTestManager(t, 0)
	if m.TTL() != DefaultTTL {
		t.Errorf("TTL() = %v, want default %v", m.TTL(), DefaultTTL)
	}
}

func TestNewManager_AcceptsPKCS1Key(t *testing.T) {
	t.Parallel()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	path := filepath.Join(t.TempDir(), "pkcs1.pem")
	der := x509.MarshalPKCS1PrivateKey(key)
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: der})
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatalf("write key: %v", err)
	}
	m, err := NewManager(path, newJSONStore(t), time.Hour)
	if err != nil {
		t.Fatalf("NewManager with PKCS#1 key: %v", err)
	}
	if m == nil {
		t.Fatal("expected a Manager")
	}
}

func TestNewManager_RejectsBadKey(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "bad.pem")
	if err := os.WriteFile(path, []byte("not a pem file"), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}
	if _, err := NewManager(path, newJSONStore(t), time.Hour); err == nil {
		t.Fatal("expected an error for a malformed key file")
	}
}

func TestNewManager_RejectsWeakKey(t *testing.T) {
	t.Parallel()
	key, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}
	path := filepath.Join(t.TempDir(), "weak.pem")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatalf("write key: %v", err)
	}
	if _, err := NewManager(path, newJSONStore(t), time.Hour); err == nil {
		t.Fatal("expected an error for a sub-2048-bit RSA signing key")
	}
}

func TestIssue_SignsVerifiableJWT(t *testing.T) {
	t.Parallel()
	keyPath, key := writeTestKey(t)
	m, err := NewManager(keyPath, newJSONStore(t), time.Hour)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	before := time.Now()
	signed, rec, err := m.Issue("tenant-alpha", "ops@example.com", "grafana pull")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}

	if rec.TenantID != "tenant-alpha" {
		t.Errorf("Record.TenantID = %q, want tenant-alpha", rec.TenantID)
	}
	if rec.IssuedBy != "ops@example.com" {
		t.Errorf("Record.IssuedBy = %q", rec.IssuedBy)
	}
	if rec.Description != "grafana pull" {
		t.Errorf("Record.Description = %q", rec.Description)
	}
	if !strings.HasPrefix(rec.TokenID, "ftk_") {
		t.Errorf("Record.TokenID = %q, want ftk_ prefix", rec.TokenID)
	}
	if d := rec.ExpiresAt.Sub(rec.IssuedAt); d != time.Hour {
		t.Errorf("ExpiresAt-IssuedAt = %v, want 1h", d)
	}

	var claims Claims
	tok, err := jwt.ParseWithClaims(signed, &claims, func(*jwt.Token) (interface{}, error) {
		return &key.PublicKey, nil
	})
	if err != nil {
		t.Fatalf("ParseWithClaims: %v", err)
	}
	if !tok.Valid {
		t.Fatal("parsed token is not valid")
	}
	if claims.TenantID != "tenant-alpha" {
		t.Errorf("claim tenant_id = %q", claims.TenantID)
	}
	if claims.TokenID != rec.TokenID {
		t.Errorf("claim token_id = %q, want %q (Record.TokenID)", claims.TokenID, rec.TokenID)
	}
	if claims.Issuer != "tenant-api" {
		t.Errorf("claim iss = %q, want tenant-api", claims.Issuer)
	}
	if len(claims.Audience) != 1 || claims.Audience[0] != "tenant-federation" {
		t.Errorf("claim aud = %v, want [tenant-federation]", claims.Audience)
	}
	if claims.Subject != "tenant-alpha" {
		t.Errorf("claim sub = %q", claims.Subject)
	}
	// iat is backdated by clockSkewLeeway so a fast signer clock cannot
	// place it in a verifier's future.
	if iat := claims.IssuedAt.Time; iat.After(before) {
		t.Errorf("claim iat = %v, want <= issuance time %v (backdated)", iat, before)
	}
	exp := claims.ExpiresAt.Time
	if exp.Before(before.Add(time.Hour-time.Minute)) || exp.After(time.Now().Add(time.Hour+time.Minute)) {
		t.Errorf("claim exp = %v, want ~1h from issuance", exp)
	}
}

func TestIssueLogs_EmbedsAccountIDAndLogsAudience(t *testing.T) {
	t.Parallel()
	keyPath, key := writeTestKey(t)
	m, err := NewManager(keyPath, newJSONStore(t), time.Hour)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	signed, rec, err := m.IssueLogs("tenant-logs", "ops@example.com", "vmlogs pull", 1042)
	if err != nil {
		t.Fatalf("IssueLogs: %v", err)
	}
	if rec.Capability != CapLogs {
		t.Errorf("Record.Capability = %q, want %q", rec.Capability, CapLogs)
	}
	if rec.AccountID != 1042 {
		t.Errorf("Record.AccountID = %d, want 1042", rec.AccountID)
	}

	var claims Claims
	tok, err := jwt.ParseWithClaims(signed, &claims, func(*jwt.Token) (interface{}, error) {
		return &key.PublicKey, nil
	})
	if err != nil || !tok.Valid {
		t.Fatalf("ParseWithClaims: %v (valid=%v)", err, tok.Valid)
	}
	if claims.AccountID != 1042 {
		t.Errorf("claim account_id = %d, want 1042", claims.AccountID)
	}
	if len(claims.Audience) != 1 || claims.Audience[0] != audienceLogs {
		t.Errorf("claim aud = %v, want [%s]", claims.Audience, audienceLogs)
	}
	if claims.TenantID != "tenant-logs" {
		t.Errorf("claim tenant_id = %q", claims.TenantID)
	}
}

// TestIssueLogs_RejectsZeroAccountID: a logs token with account_id 0 would be
// omitempty'd out of the JWT and route the tenant's logs to the platform
// default partition (AccountID 0). IssueLogs must FAIL LOUD instead, never
// mint a token that reads the wrong partition.
func TestIssueLogs_RejectsZeroAccountID(t *testing.T) {
	t.Parallel()
	keyPath, _ := writeTestKey(t)
	m, err := NewManager(keyPath, newJSONStore(t), time.Hour)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if _, _, err := m.IssueLogs("tenant-logs", "ops@example.com", "", 0); err == nil {
		t.Fatal("IssueLogs with account_id 0 should error, not mint a default-partition token")
	}
}

// TestIssue_MetricsHasNoAccountIDAndMetricsAudience pins the back-compat
// contract: the metrics-plane token carries NO account_id and the original
// audience, so existing callers are byte-for-byte unaffected.
func TestIssue_MetricsHasNoAccountIDAndMetricsAudience(t *testing.T) {
	t.Parallel()
	keyPath, key := writeTestKey(t)
	m, err := NewManager(keyPath, newJSONStore(t), time.Hour)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	signed, rec, err := m.Issue("tenant-metrics", "ops@example.com", "")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	if rec.AccountID != 0 {
		t.Errorf("metrics Record.AccountID = %d, want 0", rec.AccountID)
	}
	if rec.Capability != CapMetrics {
		t.Errorf("metrics Record.Capability = %q, want %q", rec.Capability, CapMetrics)
	}

	var claims Claims
	if _, err := jwt.ParseWithClaims(signed, &claims, func(*jwt.Token) (interface{}, error) {
		return &key.PublicKey, nil
	}); err != nil {
		t.Fatalf("ParseWithClaims: %v", err)
	}
	if claims.AccountID != 0 {
		t.Errorf("metrics claim account_id = %d, want 0 (omitted)", claims.AccountID)
	}
	if len(claims.Audience) != 1 || claims.Audience[0] != audienceMetrics {
		t.Errorf("metrics claim aud = %v, want [%s]", claims.Audience, audienceMetrics)
	}

	// Belt-and-braces: the raw payload must not even carry an account_id key
	// (omitempty), so a metrics verifier sees the pre-ADR-021 shape exactly.
	parts := strings.Split(signed, ".")
	if len(parts) != 3 {
		t.Fatalf("token has %d segments, want 3", len(parts))
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatalf("decode payload: %v", err)
	}
	if strings.Contains(string(payload), "account_id") {
		t.Errorf("metrics token payload contains account_id: %s", payload)
	}
}

func TestIssue_StampsKeyID(t *testing.T) {
	t.Parallel()
	keyPath, key := writeTestKey(t)
	m, err := NewManager(keyPath, newJSONStore(t), time.Hour)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	signed, _, err := m.Issue("tenant-kid", "u@example.com", "")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}

	tok, err := jwt.ParseWithClaims(signed, &Claims{}, func(*jwt.Token) (interface{}, error) {
		return &key.PublicKey, nil
	})
	if err != nil {
		t.Fatalf("ParseWithClaims: %v", err)
	}
	kid, ok := tok.Header["kid"].(string)
	if !ok || kid == "" {
		t.Fatalf("token header kid = %v, want a non-empty string", tok.Header["kid"])
	}
	// The kid is the RFC 7638 thumbprint of the signing key's public half
	// — the same value `da-tools fed-key` independently writes into the
	// JWKS, so the gateway's jwt_authn resolves the key by kid.
	if want := keyID(&key.PublicKey); kid != want {
		t.Errorf("kid = %q, want RFC 7638 thumbprint %q", kid, want)
	}
}

func TestKeyID_StableAndShaped(t *testing.T) {
	t.Parallel()
	_, key := writeTestKey(t)
	kid := keyID(&key.PublicKey)
	// SHA-256 base64url (no padding) is always 43 characters.
	if len(kid) != 43 {
		t.Errorf("keyID = %q (len %d), want a 43-char base64url SHA-256", kid, len(kid))
	}
	// Deterministic — the same key always thumbprints to the same kid.
	if again := keyID(&key.PublicKey); again != kid {
		t.Errorf("keyID not deterministic: %q then %q", kid, again)
	}
	// A different key yields a different kid.
	_, other := writeTestKey(t)
	if keyID(&other.PublicKey) == kid {
		t.Error("two distinct keys produced the same kid")
	}
}

func TestKeyID_RFC7638Vector(t *testing.T) {
	t.Parallel()
	// The published RFC 7638 §3.1 worked example. Pinning keyID to it
	// locks the thumbprint algorithm to the spec — so the kid stays
	// interoperable with the `da-tools fed-key` JWKS regardless of any
	// future refactor of either side.
	const (
		rfcN   = "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
		rfcKid = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"
	)
	nBytes, err := base64.RawURLEncoding.DecodeString(rfcN)
	if err != nil {
		t.Fatalf("decode RFC 7638 modulus: %v", err)
	}
	pub := &rsa.PublicKey{N: new(big.Int).SetBytes(nBytes), E: 65537} // e = AQAB
	if got := keyID(pub); got != rfcKid {
		t.Errorf("keyID(RFC 7638 example) = %q, want %q", got, rfcKid)
	}
}

func TestIssue_RejectsByWrongKey(t *testing.T) {
	t.Parallel()
	m := newTestManager(t, time.Hour)
	signed, _, err := m.Issue("tenant-x", "u@example.com", "")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	otherKey, _ := rsa.GenerateKey(rand.Reader, 2048)
	if _, err := jwt.ParseWithClaims(signed, &Claims{}, func(*jwt.Token) (interface{}, error) {
		return &otherKey.PublicKey, nil
	}); err == nil {
		t.Fatal("expected verification to fail against an unrelated public key")
	}
}

func TestManager_ListGetDelete(t *testing.T) {
	t.Parallel()
	m := newTestManager(t, time.Hour)

	_, recA1, err := m.Issue("tenant-a", "u@example.com", "first")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	_, recA2, err := m.Issue("tenant-a", "u@example.com", "second")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	_, recB1, err := m.Issue("tenant-b", "u@example.com", "")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}

	listA, err := m.List("tenant-a")
	if err != nil {
		t.Fatalf("List(tenant-a): %v", err)
	}
	if len(listA) != 2 {
		t.Fatalf("List(tenant-a) = %d records, want 2", len(listA))
	}
	gotIDs := map[string]bool{listA[0].TokenID: true, listA[1].TokenID: true}
	if !gotIDs[recA1.TokenID] || !gotIDs[recA2.TokenID] {
		t.Errorf("List(tenant-a) missing an issued token: %v", gotIDs)
	}
	if got, err := m.List("tenant-b"); err != nil || len(got) != 1 || got[0].TokenID != recB1.TokenID {
		t.Errorf("List(tenant-b) = (%v, %v), want [%s]", got, err, recB1.TokenID)
	}
	if got, err := m.List("tenant-unknown"); err != nil || len(got) != 0 {
		t.Errorf("List(tenant-unknown) = (%d, %v), want 0", len(got), err)
	}

	got, ok, err := m.Get(recA1.TokenID)
	if err != nil || !ok || got.TenantID != "tenant-a" {
		t.Errorf("Get(%s) = (%+v, %v, %v)", recA1.TokenID, got, ok, err)
	}
	if _, ok, _ := m.Get("ftk_does_not_exist"); ok {
		t.Error("Get of an unknown token reported present")
	}

	deleted, err := m.Delete(recA1.TokenID, recA1.ExpiresAt)
	if err != nil || !deleted {
		t.Fatalf("Delete(%s) = (%v, %v), want (true, nil)", recA1.TokenID, deleted, err)
	}
	if got, err := m.List("tenant-a"); err != nil || len(got) != 1 {
		t.Errorf("List(tenant-a) after delete = (%d, %v), want 1", len(got), err)
	}
	if deleted, _ := m.Delete(recA1.TokenID, recA1.ExpiresAt); deleted {
		t.Error("second Delete of the same token reported a deletion")
	}
}

func TestIssue_EnforcesTokenLimit(t *testing.T) {
	t.Parallel()
	m := newTestManager(t, time.Hour)
	now := time.Now()
	// Seed the store to the cap directly: a loop of Issue calls would
	// trip the mint rate limit long before reaching maxTokensPerTenant.
	for i := 0; i < maxTokensPerTenant; i++ {
		rec := Record{
			TokenID:   fmt.Sprintf("ftk_seed%02d", i),
			TenantID:  "tenant-cap",
			IssuedAt:  now,
			ExpiresAt: now.Add(time.Hour),
		}
		if err := m.store.put(rec); err != nil {
			t.Fatalf("seed put %d: %v", i, err)
		}
	}
	if _, _, err := m.Issue("tenant-cap", "u@example.com", ""); !errors.Is(err, ErrTokenLimitReached) {
		t.Fatalf("Issue past cap = %v, want ErrTokenLimitReached", err)
	}
	// A different tenant is unaffected by another tenant's cap.
	if _, _, err := m.Issue("tenant-other", "u@example.com", ""); err != nil {
		t.Fatalf("Issue for a different tenant: %v", err)
	}
}

func TestIssue_EnforcesMintRateLimit(t *testing.T) {
	t.Parallel()
	m := newTestManager(t, time.Hour)
	for i := 0; i < maxMintsPerWindow; i++ {
		if _, _, err := m.Issue("tenant-fast", "u@example.com", ""); err != nil {
			t.Fatalf("Issue %d within rate limit: %v", i, err)
		}
	}
	if _, _, err := m.Issue("tenant-fast", "u@example.com", ""); !errors.Is(err, ErrMintRateLimited) {
		t.Fatalf("Issue past rate limit = %v, want ErrMintRateLimited", err)
	}
	// A different tenant has its own independent window.
	if _, _, err := m.Issue("tenant-slow", "u@example.com", ""); err != nil {
		t.Fatalf("Issue for a different tenant: %v", err)
	}
}

func TestStore_ListExcludesExpiredButGetReturnsIt(t *testing.T) {
	t.Parallel()
	st, err := newStore(filepath.Join(t.TempDir(), "s.json"))
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	now := time.Now()
	live := Record{TokenID: "ftk_live", TenantID: "t", IssuedAt: now.Add(-time.Hour), ExpiresAt: now.Add(time.Hour)}
	dead := Record{TokenID: "ftk_dead", TenantID: "t", IssuedAt: now.Add(-2 * time.Hour), ExpiresAt: now.Add(-time.Hour)}
	if err := st.put(live); err != nil {
		t.Fatalf("put live: %v", err)
	}
	if err := st.put(dead); err != nil {
		t.Fatalf("put dead: %v", err)
	}

	got, err := st.list("t", now)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(got) != 1 || got[0].TokenID != "ftk_live" {
		t.Errorf("list = %v, want only ftk_live", got)
	}
	if _, ok, _ := st.get("ftk_dead"); !ok {
		t.Error("get should still return an expired-but-unpruned record")
	}
}

func TestStore_Persistence(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "s.json")
	st1, err := newStore(path)
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	now := time.Now()
	rec := Record{TokenID: "ftk_persist", TenantID: "tenant-p", IssuedBy: "u@example.com", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	if err := st1.put(rec); err != nil {
		t.Fatalf("put: %v", err)
	}

	st2, err := newStore(path)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	got, ok, err := st2.get("ftk_persist")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if !ok {
		t.Fatal("record was not reloaded from disk")
	}
	if got.TenantID != "tenant-p" || got.IssuedBy != "u@example.com" {
		t.Errorf("reloaded record = %+v", got)
	}
}

func TestStore_RevokeRemovesRecord(t *testing.T) {
	t.Parallel()
	st, err := newStore(filepath.Join(t.TempDir(), "s.json"))
	if err != nil {
		t.Fatalf("newStore: %v", err)
	}
	now := time.Now()
	rec := Record{TokenID: "ftk_rev", TenantID: "t", IssuedAt: now, ExpiresAt: now.Add(time.Hour)}
	if err := st.put(rec); err != nil {
		t.Fatalf("put: %v", err)
	}
	deleted, err := st.revoke("ftk_rev", rec.ExpiresAt)
	if err != nil || !deleted {
		t.Fatalf("revoke = (%v, %v), want (true, nil)", deleted, err)
	}
	if _, ok, _ := st.get("ftk_rev"); ok {
		t.Error("record should be gone after revoke")
	}
	if deleted, _ := st.revoke("ftk_rev", rec.ExpiresAt); deleted {
		t.Error("second revoke of the same token reported a deletion")
	}
}

func TestNewStore_RemovesStaleTempFile(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "s.json")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte("[]"), 0o600); err != nil {
		t.Fatalf("write stale tmp: %v", err)
	}
	if _, err := newStore(path); err != nil {
		t.Fatalf("newStore: %v", err)
	}
	if _, err := os.Stat(tmp); !os.IsNotExist(err) {
		t.Errorf("stale .tmp file was not removed (stat err = %v)", err)
	}
}
