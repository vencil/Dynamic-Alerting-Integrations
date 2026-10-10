// Package credmask hides receiver credentials from callers who may read a
// tenant but not write it (#1560 option d).
//
// A tenant file carries its alert receivers in `_routing`, and a receiver's
// required fields ARE credentials: a Slack / Teams / Rocket.Chat webhook URL
// is the secret itself. GET /tenants/{id}, /effective and /diff are PermRead
// routes, and the shipped `viewers` rule reads every tenant, so before this
// package every viewer read every tenant's receiver credentials verbatim.
//
// Who gets the masked form is the handler's call, made with the same
// predicate as the PUT gate (handler.canSeeCredentials): a caller who sees
// the placeholder can never PUT, so a read-modify-write cannot carry it back
// over the real value. The writer refuses the placeholder anyway
// (gitops.ReceiverPreflight), for a client that reads under one identity and
// writes under another.
//
// ⛔ MATCHED BY KEY NAME, ANYWHERE IN THE DOCUMENT — not by receiver path. A
// receiver sits in at least five places (`_routing.receiver`,
// `overrides[i].receiver`, `routes[i].receiver`, `_routing_defaults`, a
// routing profile), and a path list drifts the day a sixth appears. Masking a
// `url` that is not a credential costs a read-only caller one value; missing
// one that is costs the secret.
//
// ⛔ FAIL-CLOSED. A document this package cannot mask with certainty is
// reported as ErrUnmaskable and the caller withholds it whole:
//   - not YAML, or more than one document;
//   - an anchor, alias or merge key anywhere. `_metadata.owner: &h <secret>`
//     plus `api_url: *h` puts the secret on a line no key-name rule reaches
//     (measured while planning #1560's fix), and following aliases back to
//     their anchors is more code to get right than refusing the shape;
//   - a mapping key that is not a scalar.
//
// The masked document is re-encoded, which normalizes formatting and DROPS
// EVERY COMMENT: a commented-out `# api_url: https://…` is a credential no
// key-name rule can see. A multi-line value becomes the one-line placeholder,
// so its line count does not leak through a diff hunk header either.
package credmask

import (
	"bytes"
	"errors"
	"io"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

// Placeholder replaces every credential value in the masked form. It does not
// start with `*`, `&` or `!` (YAML would read those as an alias, anchor or
// tag) and is always written double-quoted.
const Placeholder = "<masked: write permission required>"

// ErrUnmaskable reports a document whose credentials cannot be located with
// certainty. The caller must not show the document at all.
var ErrUnmaskable = errors.New("document cannot be masked")

// credentialKeys are the mapping keys whose values are masked, compared
// case-insensitively. The tenant-config schema's receiver credentials
// (webhook / slack / teams / rocketchat URLs, the PagerDuty keys, the SMTP
// password, http_config's basic_auth password, bearer token and proxy URL)
// plus the secret-typed fields Alertmanager's own receivers and http_config
// use, so a receiver written in Alertmanager's spelling is covered too.
var credentialKeys = map[string]bool{
	"url":           true,
	"api_url":       true,
	"webhook_url":   true,
	"proxy_url":     true,
	"routing_key":   true,
	"service_key":   true,
	"auth_password": true,
	"auth_secret":   true,
	"password":      true,
	"bearer_token":  true,
	"credentials":   true,
	"client_secret": true,
	"api_key":       true,
	"token":         true,
	"bot_token":     true,
	"user_key":      true,
}

// IsCredentialKey reports whether a mapping key's value is masked.
func IsCredentialKey(key string) bool {
	return credentialKeys[strings.ToLower(key)]
}

// MaskYAML returns data with every credential value replaced by Placeholder,
// re-encoded without comments; no document at all comes back empty. It
// returns ErrUnmaskable for any shape described in the package comment.
func MaskYAML(data []byte) ([]byte, error) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var doc yaml.Node
	if err := dec.Decode(&doc); err != nil {
		if errors.Is(err, io.EOF) {
			// No document. Not data: a file of comments alone is still
			// comments, and a comment can be a commented-out credential.
			return []byte{}, nil
		}
		return nil, ErrUnmaskable
	}
	var extra yaml.Node
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return nil, ErrUnmaskable // a second document, or junk after the first
	}
	if err := maskNode(&doc); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(2)
	if err := enc.Encode(&doc); err != nil {
		return nil, ErrUnmaskable
	}
	if err := enc.Close(); err != nil {
		return nil, ErrUnmaskable
	}
	return buf.Bytes(), nil
}

// maskNode masks n in place and strips its comments.
func maskNode(n *yaml.Node) error {
	if n.Kind == yaml.AliasNode || n.Anchor != "" {
		return ErrUnmaskable
	}
	n.HeadComment, n.LineComment, n.FootComment = "", "", ""
	if n.Kind == yaml.MappingNode {
		for i := 0; i+1 < len(n.Content); i += 2 {
			k, v := n.Content[i], n.Content[i+1]
			if k.Kind != yaml.ScalarNode || k.Anchor != "" || k.Value == "<<" {
				return ErrUnmaskable
			}
			k.HeadComment, k.LineComment, k.FootComment = "", "", ""
			if IsCredentialKey(k.Value) {
				// Walk it first only to refuse an anchor or alias inside;
				// the value itself is replaced whatever its shape.
				if err := maskNode(v); err != nil {
					return err
				}
				*v = yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: Placeholder, Style: yaml.DoubleQuotedStyle}
				continue
			}
			if err := maskNode(v); err != nil {
				return err
			}
		}
		return nil
	}
	for _, c := range n.Content {
		if err := maskNode(c); err != nil {
			return err
		}
	}
	return nil
}

// MaskValue returns a copy of v (a decoded document: maps, slices, scalars)
// with the value of every credential key replaced by Placeholder. v itself is
// not modified.
func MaskValue(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			if IsCredentialKey(k) {
				out[k] = Placeholder
				continue
			}
			out[k] = MaskValue(e)
		}
		return out
	case map[any]any:
		out := make(map[any]any, len(t))
		for k, e := range t {
			if s, ok := k.(string); ok && IsCredentialKey(s) {
				out[k] = Placeholder
				continue
			}
			out[k] = MaskValue(e)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = MaskValue(e)
		}
		return out
	default:
		return v
	}
}

// PlaceholderPaths returns the path of every value in a YAML document that
// decodes to Placeholder — quoted, tagged, block-scalar or reached through an
// alias, since the decoded value is what is compared — sorted. Nil when the
// document does not decode.
func PlaceholderPaths(data []byte) []string {
	var v any
	if yaml.Unmarshal(data, &v) != nil {
		return nil
	}
	var out []string
	var walk func(path string, v any)
	walk = func(path string, v any) {
		switch t := v.(type) {
		case string:
			if t == Placeholder {
				out = append(out, path)
			}
		case map[string]any:
			for k, e := range t {
				walk(join(path, k), e)
			}
		case map[any]any:
			for k, e := range t {
				ks, _ := k.(string)
				walk(join(path, ks), e)
			}
		case []any:
			for i, e := range t {
				walk(path+"["+strconv.Itoa(i)+"]", e)
			}
		}
	}
	walk("", v)
	sort.Strings(out)
	return out
}

func join(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}
