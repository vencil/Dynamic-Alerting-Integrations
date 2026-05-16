package federation

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
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

func newTestManager(t *testing.T, ttl time.Duration) *Manager {
	t.Helper()
	keyPath, _ := writeTestKey(t)
	m, err := NewManager(keyPath, filepath.Join(t.TempDir(), "store.json"), ttl)
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
	m, err := NewManager("", "", 0)
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
	m, err := NewManager(path, filepath.Join(t.TempDir(), "s.json"), time.Hour)
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
	if _, err := NewManager(path, filepath.Join(t.TempDir(), "s.json"), time.Hour); err == nil {
		t.Fatal("expected an error for a malformed key file")
	}
}

func TestIssue_SignsVerifiableJWT(t *testing.T) {
	t.Parallel()
	keyPath, key := writeTestKey(t)
	m, err := NewManager(keyPath, filepath.Join(t.TempDir(), "store.json"), time.Hour)
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
	if claims.Subject != "tenant-alpha" {
		t.Errorf("claim sub = %q", claims.Subject)
	}
	exp := claims.ExpiresAt.Time
	if exp.Before(before.Add(time.Hour-time.Minute)) || exp.After(time.Now().Add(time.Hour+time.Minute)) {
		t.Errorf("claim exp = %v, want ~1h from issuance", exp)
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

	listA := m.List("tenant-a")
	if len(listA) != 2 {
		t.Fatalf("List(tenant-a) = %d records, want 2", len(listA))
	}
	gotIDs := map[string]bool{listA[0].TokenID: true, listA[1].TokenID: true}
	if !gotIDs[recA1.TokenID] || !gotIDs[recA2.TokenID] {
		t.Errorf("List(tenant-a) missing an issued token: %v", gotIDs)
	}
	if got := m.List("tenant-b"); len(got) != 1 || got[0].TokenID != recB1.TokenID {
		t.Errorf("List(tenant-b) = %v, want [%s]", got, recB1.TokenID)
	}
	if got := m.List("tenant-unknown"); len(got) != 0 {
		t.Errorf("List(tenant-unknown) = %d, want 0", len(got))
	}

	got, ok := m.Get(recA1.TokenID)
	if !ok || got.TenantID != "tenant-a" {
		t.Errorf("Get(%s) = (%+v, %v)", recA1.TokenID, got, ok)
	}
	if _, ok := m.Get("ftk_does_not_exist"); ok {
		t.Error("Get of an unknown token reported present")
	}

	deleted, err := m.Delete(recA1.TokenID)
	if err != nil || !deleted {
		t.Fatalf("Delete(%s) = (%v, %v), want (true, nil)", recA1.TokenID, deleted, err)
	}
	if got := m.List("tenant-a"); len(got) != 1 {
		t.Errorf("List(tenant-a) after delete = %d, want 1", len(got))
	}
	if deleted, _ := m.Delete(recA1.TokenID); deleted {
		t.Error("second Delete of the same token reported a deletion")
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

	got := st.list("t", now)
	if len(got) != 1 || got[0].TokenID != "ftk_live" {
		t.Errorf("list = %v, want only ftk_live", got)
	}
	if _, ok := st.get("ftk_dead"); !ok {
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
	got, ok := st2.get("ftk_persist")
	if !ok {
		t.Fatal("record was not reloaded from disk")
	}
	if got.TenantID != "tenant-p" || got.IssuedBy != "u@example.com" {
		t.Errorf("reloaded record = %+v", got)
	}
}
