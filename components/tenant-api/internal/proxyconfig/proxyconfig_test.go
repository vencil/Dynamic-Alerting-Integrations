package proxyconfig

// ADR-027 §9 item 6: an oauth2-proxy config whose identity headers do not
// have the §2 shape keeps tenant-api from starting. Each testdata file is one
// of the shapes §9 item 6 lists; tests/oauth2-proxy/fixtures_test.go loads
// the same files with oauth2-proxy's own loader, to show what the proxy does
// with them.

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestLoad_ChartShape(t *testing.T) {
	t.Parallel()
	id, err := Load(filepath.Join("testdata", "valid.yaml"))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	want := Identity{
		EmailHeader:  "X-Forwarded-Email",
		GroupsHeader: "X-Forwarded-Groups",
		ClaimHeaders: map[string]string{"org": "X-Auth-Request-Org"},
	}
	if !reflect.DeepEqual(id, want) {
		t.Errorf("Load = %+v, want %+v", id, want)
	}
	if got := id.ClaimNames(); !reflect.DeepEqual(got, []string{"org"}) {
		t.Errorf("ClaimNames = %v", got)
	}
}

// adr-check: ADR-027/9.6b
func TestLoad_RefusesAmbiguousShapes(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"preserve-request-value.yaml":   "keeps the client's value",
		"secret-source.yaml":            "fixed value",
		"prefix.yaml":                   "adds the prefix",
		"basic-auth.yaml":               "basic auth",
		"claim-wrong.yaml":              "no header injects the email claim",
		"missing-email.yaml":            "no header injects the email claim",
		"missing-groups.yaml":           "no header injects the groups claim",
		"env-reference.yaml":            "environment references",
		"nonstandard-field.yaml":        `spelled "injectRequestHeaders"`,
		"nonstandard-nested-field.yaml": `spelled "preserveRequestValue"`,
	}
	for file, want := range cases {
		t.Run(file, func(t *testing.T) {
			t.Parallel()
			_, err := Load(filepath.Join("testdata", file))
			if err == nil {
				t.Fatalf("Load(%s) accepted it", file)
			}
			if !strings.Contains(err.Error(), want) {
				t.Errorf("Load(%s) = %v, want it to say %q", file, err, want)
			}
		})
	}
}

// headersDoc wraps injectRequestHeaders entries in a document with the
// required email and groups headers in front.
func headersDoc(extra string) string {
	return `injectRequestHeaders:
- name: X-Forwarded-Email
  values:
  - claimSource:
      claim: email
- name: X-Forwarded-Groups
  values:
  - claimSource:
      claim: groups
` + extra
}

// adr-check: ADR-027/9.6b
func TestParse_Refusals(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, doc, want string
	}{
		{"empty", "", "file is empty"},
		{"no headers", "server:\n  BindAddress: :4180\n", "injectRequestHeaders is missing"},
		{"not a list", "injectRequestHeaders: {}\n", "not a list"},
		{"two documents", headersDoc("---\nserver: {}\n"), "more than one YAML document"},
		{"unknown top level", "upstreams: []\n" + headersDoc(""), `unknown field "upstreams"`},
		{"unknown header field", headersDoc("- name: X-A\n  value: x\n  values:\n  - claimSource:\n      claim: a\n"), `unknown field "value"`},
		{"duplicate header", headersDoc("- name: x-forwarded-email\n  values:\n  - claimSource:\n      claim: user\n"), "injected again"},
		{"claim twice", headersDoc("- name: X-Other\n  values:\n  - claimSource:\n      claim: email\n"), `claim "email" is injected by`},
		{"two values", headersDoc("- name: X-A\n  values:\n  - claimSource:\n      claim: a\n  - claimSource:\n      claim: b\n"), "exactly one value"},
		{"no values", headersDoc("- name: X-A\n"), "exactly one value"},
		{"both sources", headersDoc("- name: X-A\n  values:\n  - claimSource:\n      claim: a\n    secretSource:\n      value: eA==\n"), "fixed value"},
		{"token claim", headersDoc("- name: X-Token\n  values:\n  - claimSource:\n      claim: access_token\n"), "not an identity claim"},
		{"underscore name", headersDoc("- name: X_Org\n  values:\n  - claimSource:\n      claim: org\n"), "header name"},
		{"skip normalisation", headersDoc("- name: X-Org\n  InsecureSkipHeaderNormalization: true\n  values:\n  - claimSource:\n      claim: org\n"), "normalisation"},
		{"bool as string", headersDoc("- name: X-Org\n  preserveRequestValue: \"false\"\n  values:\n  - claimSource:\n      claim: org\n"), "not true or false"},
		{"alias", "x: &h\n  - claimSource:\n      claim: email\n" + headersDoc(""), "anchor"},
		{"host header", headersDoc("- name: host\n  values:\n  - claimSource:\n      claim: org\n"), "cannot carry identity"},
		{"hop-by-hop header", headersDoc("- name: connection\n  values:\n  - claimSource:\n      claim: org\n"), "hop-by-hop"},
		{"probe header", headersDoc("- name: User-Agent\n  values:\n  - claimSource:\n      claim: org\n"), "probes"},
		{"accept header", headersDoc("- name: accept\n  values:\n  - claimSource:\n      claim: org\n"), "probes"},
		{"duplicate field", headersDoc("- name: X-Org\n  preserveRequestValue: true\n  preserveRequestValue: false\n  values:\n  - claimSource:\n      claim: org\n"), "written twice"},
		{"merge key", "injectRequestHeaders:\n- <<: {name: X-Forwarded-Email}\n  values:\n  - claimSource:\n      claim: email\n", "merge key"},
		{"claim charset", headersDoc("- name: X-Org\n  values:\n  - claimSource:\n      claim: \"o rg\"\n"), "claim name"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			_, err := Parse([]byte(c.doc))
			if err == nil {
				t.Fatalf("Parse accepted:\n%s", c.doc)
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Errorf("Parse = %v, want it to say %q", err, c.want)
			}
		})
	}
}

func TestParse_HeaderNamesAreCanonical(t *testing.T) {
	t.Parallel()
	id, err := Parse([]byte(strings.Replace(headersDoc("- name: x-auth-request-org\n  values:\n  - claimSource:\n      claim: org\n"),
		"X-Forwarded-Email", "x-forwarded-email", 1)))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if id.EmailHeader != "X-Forwarded-Email" || id.ClaimHeaders["org"] != "X-Auth-Request-Org" {
		t.Errorf("Parse = %+v, want canonical header names", id)
	}
}

func TestDefault(t *testing.T) {
	t.Parallel()
	if d := Default(); d.EmailHeader != DefaultEmailHeader || d.GroupsHeader != DefaultGroupsHeader || d.ClaimHeaders != nil {
		t.Errorf("Default = %+v", d)
	}
}

// TestParse_HeaderNamesFollowTheFile: email and groups are read from whatever
// header the file injects them as (ADR-027 §2) — the claim decides, not the
// name. The rate limiter and the request log read the resolved principal, so
// nothing else depends on the conventional names.
func TestParse_HeaderNamesFollowTheFile(t *testing.T) {
	t.Parallel()
	renamed := strings.Replace(headersDoc(""), "X-Forwarded-Email", "X-Auth-Request-Email", 1)
	id, err := Parse([]byte(renamed))
	if err != nil {
		t.Fatalf("Parse refused a renamed email header: %v", err)
	}
	if id.EmailHeader != "X-Auth-Request-Email" || id.GroupsHeader != DefaultGroupsHeader {
		t.Errorf("Parse = %+v, want email from X-Auth-Request-Email, groups from %s", id, DefaultGroupsHeader)
	}

	swapped, err := Load(filepath.Join("testdata", "claim-swapped.yaml"))
	if err != nil {
		t.Fatalf("Load(claim-swapped.yaml): %v", err)
	}
	if swapped.EmailHeader != DefaultGroupsHeader || swapped.GroupsHeader != DefaultEmailHeader {
		t.Errorf("Load(claim-swapped.yaml) = %+v, want email read from %s and groups from %s",
			swapped, DefaultGroupsHeader, DefaultEmailHeader)
	}
}

// TestLoad_TryLocalConfig: try-local and the contract test run oauth2-proxy
// and tenant-api from try-local/oauth2-proxy/oauth2-proxy.yaml (ADR-027 §3).
// A file tenant-api refuses would leave the demo stack without an API.
func TestLoad_TryLocalConfig(t *testing.T) {
	t.Parallel()
	id, err := Load(filepath.Join("..", "..", "..", "..", "try-local", "oauth2-proxy", "oauth2-proxy.yaml"))
	if err != nil {
		t.Fatalf("tenant-api refuses try-local's oauth2-proxy config: %v", err)
	}
	if id.EmailHeader != DefaultEmailHeader || id.GroupsHeader != DefaultGroupsHeader || id.ClaimHeaders != nil {
		t.Errorf("try-local config = %+v, want email/groups on the conventional headers and no claims", id)
	}
}
