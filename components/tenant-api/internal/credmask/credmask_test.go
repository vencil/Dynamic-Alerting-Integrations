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
	if n := strings.Count(string(out), Placeholder); n != 7 {
		t.Errorf("placeholder count = %d, want 7 (one per credential field):\n%s", n, out)
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
	if len(paths) != 7 {
		t.Errorf("PlaceholderPaths(masked) = %v, want 7 paths", paths)
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
