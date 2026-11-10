package rbac

// HeaderResolver unit tests: the base identity resolution (email / groups /
// provenance) plus the ADR-027 §7 identity-claims seam —
//
//   - HeaderResolver.ClaimHeaders: loading the injected claim headers into
//     VerifiedPrincipal.Claims, including the security assertion that an
//     UNDECLARED header can never be copied onto the principal.
//
// The zero-config invariant (no claim axes declared → byte-identical pre-P2
// principal, Claims nil) is pinned here too.

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
	if err.Error() != "missing identity: the request carries no authenticated caller (oauth2-proxy injected no email header)" {
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

// --- HeaderResolver.ClaimHeaders (ADR-027 §7) ---

// A configured (key, header) pair with a value present loads Claims[key],
// trimmed but otherwise verbatim.
func TestHeaderResolver_ClaimHeaders_LoadsConfiguredClaims(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a")
	req.Header.Set("X-Auth-Request-Org", "  org-alpha  ") // trimmed on load
	req.Header.Set("X-Auth-Request-Region", "region-east")

	h := HeaderResolver{ClaimHeaders: map[string]string{
		"org":    "X-Auth-Request-Org",
		"region": "X-Auth-Request-Region",
	}}
	p, err := h.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	want := map[string]string{"org": "org-alpha", "region": "region-east"}
	if !reflect.DeepEqual(p.Claims, want) {
		t.Errorf("Claims = %v, want %v", p.Claims, want)
	}
}

// The claim value is carried verbatim after trimming — no comma-splitting
// (multi-value semantics belong to P3/P4).
func TestHeaderResolver_ClaimHeaders_ValueNotCommaSplit(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Auth-Request-Org", "org-alpha,org-beta")

	h := HeaderResolver{ClaimHeaders: map[string]string{"org": "X-Auth-Request-Org"}}
	p, err := h.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if got := p.Claims["org"]; got != "org-alpha,org-beta" {
		t.Errorf("Claims[org] = %q, want the verbatim comma-carrying value", got)
	}
}

// An absent or empty(-after-trim) header means the key is NOT present — an
// empty string is never a claim (P3 empty-string match footgun). With no hits
// at all, Claims stays nil so "no claims" has exactly one representation.
func TestHeaderResolver_ClaimHeaders_AbsentOrEmptyHeaderYieldsNoKey(t *testing.T) {
	t.Parallel()
	cfg := map[string]string{
		"org":    "X-Auth-Request-Org",
		"region": "X-Auth-Request-Region",
	}

	// One hit, one empty, → only the hit appears.
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Auth-Request-Org", "org-alpha")
	req.Header.Set("X-Auth-Request-Region", "   ") // whitespace-only = empty
	p, err := HeaderResolver{ClaimHeaders: cfg}.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if !reflect.DeepEqual(p.Claims, map[string]string{"org": "org-alpha"}) {
		t.Errorf("Claims = %v, want only the org hit", p.Claims)
	}
	if _, present := p.Claims["region"]; present {
		t.Error("empty-valued header produced a claim key; empty is NOT a claim")
	}

	// Zero hits → Claims must be nil, not an empty map.
	reqNone := httptest.NewRequest("GET", "/test", nil)
	reqNone.Header.Set("X-Forwarded-Email", "op@example.com")
	pNone, err := HeaderResolver{ClaimHeaders: cfg}.Resolve(reqNone)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if pNone.Claims != nil {
		t.Errorf("Claims = %v, want nil when no declared header carries a value", pNone.Claims)
	}
}

// SECURITY: only DECLARED headers may become claims. A request smuggling an
// arbitrary X-Auth-Request-* header that is not in the declaration must not
// have it copied onto the principal.
func TestHeaderResolver_ClaimHeaders_UndeclaredHeaderNotCopied(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Auth-Request-Org", "org-alpha")
	req.Header.Set("X-Auth-Request-Evil", "smuggled-value") // NOT declared

	h := HeaderResolver{ClaimHeaders: map[string]string{"org": "X-Auth-Request-Org"}}
	p, err := h.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if len(p.Claims) != 1 {
		t.Fatalf("Claims = %v, want exactly the one declared key", p.Claims)
	}
	for k, v := range p.Claims {
		if v == "smuggled-value" {
			t.Errorf("undeclared header value leaked into Claims[%q]", k)
		}
	}
}

// First-value-hijacking backstop: a declared claim header arriving with MORE
// THAN ONE line is refused outright — Header.Get would return the FIRST line,
// so if a hop appended its trusted value instead of strip-and-set, an
// attacker-supplied first line would win. Refusal is fail-closed (the claim
// simply does not load; other claims are unaffected).
func TestHeaderResolver_ClaimHeaders_MultiValueRefused(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	// Simulate append-not-overwrite: attacker line first, trusted line second.
	req.Header.Add("X-Auth-Request-Org", "evil-org")
	req.Header.Add("X-Auth-Request-Org", "legit-org")
	req.Header.Set("X-Auth-Request-Region", "region-east") // single line — loads

	h := HeaderResolver{ClaimHeaders: map[string]string{
		"org":    "X-Auth-Request-Org",
		"region": "X-Auth-Request-Region",
	}}
	p, err := h.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	if _, ok := p.Claims["org"]; ok {
		t.Errorf("Claims[org] = %q, want absent — a multi-line claim header must be refused, not first-value-picked", p.Claims["org"])
	}
	if got := p.Claims["region"]; got != "region-east" {
		t.Errorf("Claims[region] = %q, want region-east (single-line claim must be unaffected by a sibling refusal)", got)
	}
}

// Zero-config invariant: a zero-value HeaderResolver{} (no claim axes) must
// produce a principal IDENTICAL to the pre-P2 shape — Claims nil, every other
// field unchanged — even when claim-looking headers are present on the wire.
func TestHeaderResolver_ZeroConfig_ClaimsNilAndPrincipalUnchanged(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b")
	req.Header.Set("X-Auth-Request-Org", "org-alpha") // present but undeclared

	p, err := HeaderResolver{}.Resolve(req)
	if err != nil {
		t.Fatalf("Resolve returned error: %v", err)
	}
	want := &VerifiedPrincipal{
		Subject:   "op@example.com",
		Email:     "op@example.com",
		Groups:    []string{"team-a", "team-b"},
		Claims:    nil,
		Source:    SourceHumanHopB,
		Assurance: AssuranceHopAttested,
	}
	if !reflect.DeepEqual(p, want) {
		t.Errorf("principal = %+v, want pre-P2-identical %+v", p, want)
	}
}
