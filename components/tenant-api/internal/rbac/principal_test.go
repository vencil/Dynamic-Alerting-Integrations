package rbac

import (
	"net/http/httptest"
	"reflect"
	"testing"
)

// HeaderResolver must produce a principal byte-equivalent to the pre-seam
// inline header parse: email as Subject+Email, comma-split groups, and the
// hop-B / hop-attested provenance.
func TestHeaderResolver_Resolve(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b ,, team-c")

	p, err := HeaderResolver{}.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if p.Email != "op@example.com" {
		t.Errorf("Email = %q, want op@example.com", p.Email)
	}
	if p.Subject != "op@example.com" {
		t.Errorf("Subject = %q, want op@example.com (email is the subject on the header path)", p.Subject)
	}
	if p.Source != SourceHumanHopB {
		t.Errorf("Source = %q, want %q", p.Source, SourceHumanHopB)
	}
	if p.Assurance != AssuranceHopAttested {
		t.Errorf("Assurance = %q, want %q", p.Assurance, AssuranceHopAttested)
	}
	// Empty entries (the ",," ) must be dropped, whitespace trimmed.
	want := []string{"team-a", "team-b", "team-c"}
	if !reflect.DeepEqual(p.Groups, want) {
		t.Errorf("Groups = %v, want %v", p.Groups, want)
	}
}

// An empty X-Forwarded-Email is an error — the middleware maps it to 401.
func TestHeaderResolver_EmptyEmailIsError(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	// No X-Forwarded-Email header.
	req.Header.Set("X-Forwarded-Groups", "team-a")

	p, err := HeaderResolver{}.Resolve(req)
	if err == nil {
		t.Fatal("expected an error for missing X-Forwarded-Email, got nil")
	}
	if p != nil {
		t.Errorf("expected nil principal on error, got %+v", p)
	}
	// The error text is surfaced verbatim as the 401 body; keep it stable.
	if err.Error() != "missing identity: X-Forwarded-Email header required" {
		t.Errorf("unexpected error text: %q", err.Error())
	}
}

// A present email with no groups header yields a nil (not empty-non-nil) Groups
// slice — matching parseForwardedGroups and the pre-seam behavior.
func TestHeaderResolver_NoGroups(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "solo@example.com")

	p, err := HeaderResolver{}.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if len(p.Groups) != 0 {
		t.Errorf("Groups = %v, want empty", p.Groups)
	}
}

// HeaderResolver satisfies the IdentityResolver interface (compile+behavior).
func TestHeaderResolver_ImplementsIdentityResolver(t *testing.T) {
	t.Parallel()
	var r IdentityResolver = HeaderResolver{}
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set("X-Forwarded-Email", "x@y.z")
	if _, err := r.Resolve(req); err != nil {
		t.Fatalf("Resolve via interface: %v", err)
	}
}
