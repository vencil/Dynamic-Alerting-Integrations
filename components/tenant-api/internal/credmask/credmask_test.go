package credmask

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

func TestMaskYAML_MasksEveryCredentialKeyAtAnyDepth(t *testing.T) {
	t.Parallel()
	in := `tenants:
  t1:
    _routing:
      receiver:
        type: slack
        api_url: https://hooks.slack.com/services/CANARY-1   # CANARY-2 in a line comment
      overrides:
        - alertname: A
          receiver:
            type: webhook
            url: https://hook.example/CANARY-3
            http_config:
              bearer_token: CANARY-4
              basic_auth: {username: u, password: CANARY-5}
              authorization: {credentials: CANARY-6}
              oauth2: {client_id: c, client_secret: CANARY-7}
    # api_url: https://hooks.slack.com/services/CANARY-8 (commented out)
    pd:
      type: pagerduty
      routing_key: >-
        CANARY-9
        CANARY-10
    runbook_url: https://runbooks.example/keep-me
`
	out, err := MaskYAML([]byte(in))
	if err != nil {
		t.Fatalf("MaskYAML: %v", err)
	}
	if strings.Contains(string(out), "CANARY") {
		t.Fatalf("masked output still holds a credential:\n%s", out)
	}
	// api_url, url, http_config (whole), routing_key.
	if n := strings.Count(string(out), Placeholder); n != 4 {
		t.Errorf("placeholder count = %d, want 4 (one per credential field):\n%s", n, out)
	}
	if !strings.Contains(string(out), "https://runbooks.example/keep-me") {
		t.Errorf("a non-credential URL was masked:\n%s", out)
	}
	if strings.Contains(string(out), "#") {
		t.Errorf("a comment survived the mask:\n%s", out)
	}
	// R3: a block-scalar credential becomes ONE line, so a diff hunk cannot
	// count its lines.
	for _, line := range strings.Split(string(out), "\n") {
		if strings.Contains(line, "routing_key") && !strings.Contains(line, Placeholder) {
			t.Errorf("routing_key's placeholder is not on its own key's line: %q", line)
		}
	}
	// The masked document decodes back to the placeholder at every path.
	paths := PlaceholderPaths(out)
	if len(paths) != 4 {
		t.Errorf("PlaceholderPaths(masked) = %v, want 4 paths", paths)
	}
}

// R2 / R4: anything the mask cannot locate credentials in with certainty is
// refused whole, never returned half-masked.
func TestMaskYAML_FailsClosed(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"anchor outside a credential field": "a:\n  owner: &h https://hooks.slack.com/services/CANARY\n  api_url: *h\n",
		"anchor on the credential":          "a:\n  api_url: &h CANARY\n  other: x\n",
		"merge key":                         "base: &b {x: 1}\nt:\n  <<: *b\n",
		"merge key without alias":           "t:\n  <<: {api_url: CANARY}\n",
		"two documents":                     "a: 1\n---\napi_url: CANARY\n",
		"not YAML":                          "a: [unclosed\n",
		"non-scalar key":                    "? [api_url]\n: CANARY\n",
		"binary-tagged key":                 "a:\n  ? !!binary YXBpX3VybA==\n  : https://CANARY\n",
		"custom-tagged key":                 "a:\n  !k api_url: https://CANARY\n",
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			out, err := MaskYAML([]byte(in))
			if !errors.Is(err, ErrUnmaskable) {
				t.Fatalf("MaskYAML = (%q, %v), want ErrUnmaskable", out, err)
			}
			if out != nil {
				t.Errorf("an unmaskable document returned content: %q", out)
			}
		})
	}
}

// http_config is passed to Alertmanager as written and can hold a secret in
// fields no list keeps up with: it is masked whole, as are tls_config and
// header maps wherever they appear.
func TestMaskYAML_MasksContainersWhole(t *testing.T) {
	t.Parallel()
	in := `r:
  type: webhook
  http_config:
    proxy_connect_header: {Proxy-Authorization: ["Basic CANARY-PCH"]}
    http_headers: {X-Api-Key: {secrets: [CANARY-HH]}}
    tls_config: {key: CANARY-TLS}
    oauth2: {client_id: c, endpoint_params: {audience: CANARY-EP}}
email:
  headers: {Authorization: CANARY-EMAIL}
other:
  tls_config: {key: CANARY-TLS2}
`
	out, err := MaskYAML([]byte(in))
	if err != nil {
		t.Fatalf("MaskYAML: %v", err)
	}
	if strings.Contains(string(out), "CANARY") {
		t.Errorf("a secret inside a container survived:\n%s", out)
	}
	if !strings.Contains(string(out), "type: webhook") {
		t.Errorf("a non-credential sibling was lost:\n%s", out)
	}
}

// A file of comments alone has no document, and its comments can be a
// commented-out credential: it comes back empty, not as is.
func TestMaskYAML_CommentOnlyFileComesBackEmpty(t *testing.T) {
	t.Parallel()
	out, err := MaskYAML([]byte("# api_url: https://hooks.slack.com/services/CANARY\n"))
	if err != nil {
		t.Fatalf("MaskYAML: %v", err)
	}
	if strings.Contains(string(out), "CANARY") {
		t.Errorf("comment-only file leaked: %q", out)
	}
}

func TestMaskValue_CopiesAndMasks(t *testing.T) {
	t.Parallel()
	in := map[string]any{
		"_routing": map[string]any{
			"receiver": map[string]any{"type": "slack", "API_URL": "CANARY"},
			"routes":   []any{map[string]any{"receiver": map[string]any{"url": "CANARY"}}},
		},
		"cpu": 80,
	}
	out := MaskValue(in).(map[string]any)
	want := map[string]any{
		"_routing": map[string]any{
			"receiver": map[string]any{"type": "slack", "API_URL": Placeholder},
			"routes":   []any{map[string]any{"receiver": map[string]any{"url": Placeholder}}},
		},
		"cpu": 80,
	}
	if !reflect.DeepEqual(out, want) {
		t.Errorf("MaskValue = %#v\nwant %#v", out, want)
	}
	if in["_routing"].(map[string]any)["receiver"].(map[string]any)["API_URL"] != "CANARY" {
		t.Error("MaskValue modified its input")
	}
}

// The writer refuses the placeholder however it is spelled: the DECODED
// value is compared.
func TestPlaceholderPaths_DecodedSpellings(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"double-quoted": `a: {api_url: "` + Placeholder + `"}`,
		"single-quoted": `a: {api_url: '` + Placeholder + `'}`,
		"str tag":       `a: {api_url: !!str "` + Placeholder + `"}`,
		"block scalar":  "a:\n  api_url: >-\n    " + Placeholder + "\n",
		"via alias":     "x: &p \"" + Placeholder + "\"\na: {api_url: *p}\n",
		"other key":     `a: {channel: "` + Placeholder + `"}`,
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if got := PlaceholderPaths([]byte(in)); len(got) == 0 {
				t.Errorf("PlaceholderPaths(%q) = none, want the placeholder found", in)
			}
		})
	}
	if got := PlaceholderPaths([]byte("a: {api_url: https://x}\n")); got != nil {
		t.Errorf("PlaceholderPaths on a real value = %v, want none", got)
	}
}

// #1560 ③-a: a credential field may hold a Secret reference
// ({secret: {key}} / {secret: {shared, key}}) instead of the value. The
// reference is coordinates, not the secret, but it is masked whole all the
// same: the rule is by key name and fail-closed, a key's text can be the
// credential pasted in the wrong place, and unmasking by shape would be a
// second rule to keep right. The cost is one value a reader cannot see.
func TestMaskYAML_MasksASecretReferenceWhole(t *testing.T) {
	t.Parallel()
	in := `tenants:
  t1:
    _routing:
      receiver:
        type: slack
        api_url: {secret: {key: CANARY-1}}
      routes:
        - match: {severity: critical}
          receiver:
            type: pagerduty
            routing_key:
              secret: {shared: CANARY-2, key: CANARY-3}
`
	out, err := MaskYAML([]byte(in))
	if err != nil {
		t.Fatalf("MaskYAML: %v", err)
	}
	if strings.Contains(string(out), "CANARY") || strings.Contains(string(out), "secret:") {
		t.Fatalf("a Secret reference survived the mask:\n%s", out)
	}
	if n := strings.Count(string(out), Placeholder); n != 2 {
		t.Errorf("placeholder count = %d, want 2:\n%s", n, out)
	}
	v := MaskValue(map[string]any{"api_url": map[string]any{"secret": map[string]any{"key": "CANARY-4"}}})
	if got := v.(map[string]any)["api_url"]; got != Placeholder {
		t.Errorf("MaskValue api_url = %#v, want the placeholder", got)
	}
}

// #1560 ③-c: a Secret reference on an http_config leaf (basic_auth.password,
// a header's secrets item, oauth2.tls_config.key, ...) is masked with the
// http_config container, and a leaf written outside its container is masked
// by its own name — `key` excepted, which only its container masks.
func TestMaskYAML_MasksANestedSecretReference(t *testing.T) {
	t.Parallel()
	in := `tenants:
  t1:
    _routing:
      receiver:
        type: webhook
        url: https://hook.example.com/a
        http_config:
          basic_auth: {username: u, password: {secret: {key: CANARY-1}}}
          http_headers:
            X-Token: {values: [v], secrets: [CANARY-2, {secret: {key: CANARY-3}}]}
          oauth2:
            client_id: c
            token_url: https://token.example.com/t
            client_certificate_key: {secret: {key: CANARY-4}}
            tls_config: {cert: c, key: {secret: {key: CANARY-5}}}
`
	out, err := MaskYAML([]byte(in))
	if err != nil {
		t.Fatalf("MaskYAML: %v", err)
	}
	if strings.Contains(string(out), "CANARY") || strings.Contains(string(out), "secret:") {
		t.Fatalf("a nested Secret reference survived the mask:\n%s", out)
	}
	if n := strings.Count(string(out), Placeholder); n != 2 { // url + http_config
		t.Errorf("placeholder count = %d, want 2:\n%s", n, out)
	}
	for _, key := range []string{"password", "secrets", "client_certificate_key", "client_secret", "credentials", "bearer_token"} {
		if !IsCredentialKey(key) {
			t.Errorf("IsCredentialKey(%q) = false, want true", key)
		}
	}
	if IsCredentialKey("key") {
		t.Errorf("IsCredentialKey(\"key\") = true: `key` is a Secret reference's coordinate, masked by its container")
	}
}
