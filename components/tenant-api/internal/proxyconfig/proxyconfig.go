// Package proxyconfig reads the identity headers out of the oauth2-proxy
// structured config file (--alpha-config) that the same pod's oauth2-proxy
// runs with (ADR-027 §2).
//
// tenant-api trusts a header as identity only because oauth2-proxy injects it
// and strips any client-sent copy first. Which headers those are is decided by
// the file oauth2-proxy actually loads, so tenant-api reads the same file
// instead of keeping a list of its own: the two cannot disagree.
//
// Only files whose meaning is unambiguous are accepted; anything else is an
// error and tenant-api refuses to start (ADR-027 §2, §9 item 6):
//
//   - every entry of injectRequestHeaders carries exactly one value, a
//     claimSource naming one session claim, with no prefix, no
//     basicAuthPassword, no secretSource (fixed value), preserveRequestValue
//     not true and InsecureSkipHeaderNormalization not true;
//   - the email claim and the groups claim are each injected by exactly one
//     header; every other claim is an extra claim, injected at most once,
//     and session tokens/timestamps are not claims;
//   - oauth2-proxy expands environment references ($VAR, ${VAR}) before it
//     parses the file and matches field names case-insensitively, so a file
//     holding a '$' or a field name not spelled as oauth2-proxy spells it is
//     refused: tenant-api's reading would otherwise differ from the proxy's;
//   - YAML aliases, merge keys and multi-document files are refused for the
//     same reason.
//
// The parser looks only at the parts that decide identity headers (the top
// level and injectRequestHeaders); the rest of the file (providers, upstreams,
// server) is oauth2-proxy's to validate, which it does strictly at start and
// with --config-test.
package proxyconfig

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// DefaultEmailHeader and DefaultGroupsHeader are oauth2-proxy's conventional
// names for the email and groups headers. The server always reads the names
// the config file uses; these serve the offline `config check` without a
// file, and the TCP listener refuses them in addition to the file's names.
const (
	DefaultEmailHeader  = "X-Forwarded-Email"
	DefaultGroupsHeader = "X-Forwarded-Groups"
)

// Identity says which request header carries each identity attribute.
// Header names are in canonical form (http.CanonicalHeaderKey).
type Identity struct {
	// EmailHeader carries the session's email claim.
	EmailHeader string
	// GroupsHeader carries the session's groups claim (comma-joined by
	// oauth2-proxy when the claim has several values).
	GroupsHeader string
	// ClaimHeaders maps each extra claim name to the header carrying it.
	// nil when the file injects no extra claim.
	ClaimHeaders map[string]string
}

// Default is the Identity used when no config file is given.
func Default() Identity {
	return Identity{EmailHeader: DefaultEmailHeader, GroupsHeader: DefaultGroupsHeader}
}

// Load reads and parses the config file at path.
func Load(path string) (Identity, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Identity{}, fmt.Errorf("oauth2-proxy config: %w", err)
	}
	id, err := Parse(data)
	if err != nil {
		return Identity{}, fmt.Errorf("oauth2-proxy config %s: %w", path, err)
	}
	return id, nil
}

// topLevelFields are the field names of oauth2-proxy's AlphaOptions, spelled
// as its yaml tags spell them (pkg/apis/options/alpha_options.go, v7.15.5).
var topLevelFields = []string{
	"upstreamConfig", "injectRequestHeaders", "injectResponseHeaders",
	"server", "metricsServer", "providers",
}

// headerFields are the field names of options.Header. The capitalised
// InsecureSkipHeaderNormalization is how oauth2-proxy's own yaml tag spells it.
var headerFields = []string{"name", "preserveRequestValue", "InsecureSkipHeaderNormalization", "values"}

// valueFields are the field names of options.HeaderValue.
var valueFields = []string{"claimSource", "secretSource"}

// claimSourceFields are the field names of options.ClaimSource.
var claimSourceFields = []string{"claim", "prefix", "basicAuthPassword"}

// notIdentityClaims are session values oauth2-proxy can inject that are not
// identity attributes: tokens and session timestamps. Carrying one as a claim
// would put a credential into the principal, so they are refused.
var notIdentityClaims = map[string]bool{
	"access_token": true, "id_token": true, "refresh_token": true,
	"created_at": true, "expires_on": true,
}

// claimNameRe is the charset a claim name may use: claim names become
// _rbac.yaml match keys.
var claimNameRe = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

// headerNameRe is the charset an injected header name may use. No '_':
// oauth2-proxy treats '_' and '-' as the same character when it strips a
// client's copy, while Go's request parsing does not, so a name with '_'
// would be read differently by the two.
var headerNameRe = regexp.MustCompile(`^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$`)

// reservedHeaders are request headers that cannot carry identity, with why.
// Hop-by-hop headers are removed by oauth2-proxy's reverse proxy before the
// request reaches tenant-api, so the identity would never arrive (every
// request 401). The rest are sent by ordinary clients — the kubelet's probes
// among them — and the TCP listener answers any request carrying an identity
// header 400 (ADR-027 §2), so naming one of them would fail every probe.
var reservedHeaders = map[string]string{
	"Connection":          "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Keep-Alive":          "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Proxy-Connection":    "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Proxy-Authenticate":  "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Proxy-Authorization": "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Te":                  "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Trailer":             "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Transfer-Encoding":   "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"Upgrade":             "a hop-by-hop header, removed by oauth2-proxy before the request reaches tenant-api",
	"User-Agent":          "ordinary clients and the kubelet's probes send it, and the network listener answers a request carrying an identity header 400",
	"Accept":              "ordinary clients and the kubelet's probes send it, and the network listener answers a request carrying an identity header 400",
	"Accept-Encoding":     "ordinary clients send it, and the network listener answers a request carrying an identity header 400",
	"Content-Length":      "it describes the request body; Go's HTTP server consumes it",
	"Content-Type":        "it describes the request body",
	"Cookie":              "it carries the client's own cookies, including oauth2-proxy's session cookie",
	"Authorization":       "it carries the client's own credentials",
}

// Parse parses the bytes of an oauth2-proxy alpha config file.
func Parse(data []byte) (Identity, error) {
	if i := bytes.IndexByte(data, '$'); i >= 0 {
		return Identity{}, fmt.Errorf("line %d holds '$': oauth2-proxy expands environment references in this file before parsing it, so tenant-api cannot read it as oauth2-proxy does; write the values out", lineOf(data, i))
	}
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var doc yaml.Node
	if err := dec.Decode(&doc); err != nil {
		if errors.Is(err, io.EOF) {
			return Identity{}, errors.New("the file is empty: it injects no email and no groups header")
		}
		return Identity{}, fmt.Errorf("not YAML: %w", err)
	}
	var extra yaml.Node
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return Identity{}, errors.New("the file holds more than one YAML document; oauth2-proxy reads only the first")
	}
	if err := refuseIndirection(&doc); err != nil {
		return Identity{}, err
	}
	root := &doc
	if root.Kind == yaml.DocumentNode && len(root.Content) == 1 {
		root = root.Content[0]
	}
	if root.Kind != yaml.MappingNode {
		return Identity{}, fmt.Errorf("line %d: the top level is not a mapping", root.Line)
	}
	fields, err := mappingFields(root, topLevelFields, "the top level")
	if err != nil {
		return Identity{}, err
	}
	headers, ok := fields["injectRequestHeaders"]
	if !ok {
		return Identity{}, errors.New("injectRequestHeaders is missing: the file injects no email and no groups header")
	}
	return parseHeaders(headers)
}

func parseHeaders(seq *yaml.Node) (Identity, error) {
	if seq.Kind != yaml.SequenceNode {
		return Identity{}, fmt.Errorf("line %d: injectRequestHeaders is not a list", seq.Line)
	}
	var id Identity
	seenHeader := map[string]int{}
	seenClaim := map[string]string{}
	for _, entry := range seq.Content {
		name, claim, err := parseHeader(entry)
		if err != nil {
			return Identity{}, err
		}
		canon := http.CanonicalHeaderKey(name)
		if prev, dup := seenHeader[canon]; dup {
			return Identity{}, fmt.Errorf("line %d: header %q is injected again (first at line %d); oauth2-proxy would join both values into one header", entry.Line, name, prev)
		}
		seenHeader[canon] = entry.Line
		if prev, dup := seenClaim[claim]; dup {
			return Identity{}, fmt.Errorf("line %d: claim %q is injected by %q and by %q; tenant-api needs exactly one header per claim", entry.Line, claim, prev, canon)
		}
		seenClaim[claim] = canon
		switch claim {
		case "email":
			id.EmailHeader = canon
		case "groups":
			id.GroupsHeader = canon
		default:
			if id.ClaimHeaders == nil {
				id.ClaimHeaders = map[string]string{}
			}
			id.ClaimHeaders[claim] = canon
		}
	}
	if id.EmailHeader == "" {
		return Identity{}, errors.New("no header injects the email claim; tenant-api needs it to know who the caller is")
	}
	if id.GroupsHeader == "" {
		return Identity{}, errors.New("no header injects the groups claim; tenant-api needs it to know the caller's roles")
	}
	return id, nil
}

// parseHeader checks one injectRequestHeaders entry and returns its header
// name and the claim it carries.
func parseHeader(entry *yaml.Node) (name, claim string, err error) {
	if entry.Kind != yaml.MappingNode {
		return "", "", fmt.Errorf("line %d: an injectRequestHeaders entry is not a mapping", entry.Line)
	}
	f, err := mappingFields(entry, headerFields, "an injectRequestHeaders entry")
	if err != nil {
		return "", "", err
	}
	nameNode, ok := f["name"]
	if !ok {
		return "", "", fmt.Errorf("line %d: an injectRequestHeaders entry has no name", entry.Line)
	}
	if name, err = stringScalar(nameNode, "name"); err != nil {
		return "", "", err
	}
	if !headerNameRe.MatchString(name) {
		return "", "", fmt.Errorf("line %d: header name %q: use letters, digits and single '-' only (oauth2-proxy and tenant-api normalise other characters differently)", nameNode.Line, name)
	}
	if http.CanonicalHeaderKey(name) == "Host" {
		// Go's server moves Host out of r.Header, so tenant-api could never
		// read a claim carried in it.
		return "", "", fmt.Errorf("line %d: header %q cannot carry identity: Go's HTTP server removes it from the request headers", nameNode.Line, name)
	}
	if why, ok := reservedHeaders[http.CanonicalHeaderKey(name)]; ok {
		return "", "", fmt.Errorf("line %d: header %q cannot carry identity: %s", nameNode.Line, name, why)
	}
	if n, ok := f["preserveRequestValue"]; ok {
		if keep, err := boolScalar(n, "preserveRequestValue"); err != nil {
			return "", "", err
		} else if keep {
			return "", "", fmt.Errorf("line %d: header %q keeps the client's value (preserveRequestValue: true); a client could then send its own identity", n.Line, name)
		}
	}
	if n, ok := f["InsecureSkipHeaderNormalization"]; ok {
		if skip, err := boolScalar(n, "InsecureSkipHeaderNormalization"); err != nil {
			return "", "", err
		} else if skip {
			return "", "", fmt.Errorf("line %d: header %q turns off header-name normalisation; a client's copy spelled differently would then survive the strip", n.Line, name)
		}
	}
	values, ok := f["values"]
	if !ok || values.Kind != yaml.SequenceNode || len(values.Content) != 1 {
		return "", "", fmt.Errorf("line %d: header %q must have exactly one value", entry.Line, name)
	}
	claim, err = parseValue(values.Content[0], name)
	return name, claim, err
}

// parseValue checks a header's single value and returns its claim.
func parseValue(v *yaml.Node, header string) (string, error) {
	if v.Kind != yaml.MappingNode {
		return "", fmt.Errorf("line %d: header %q: the value is not a mapping", v.Line, header)
	}
	f, err := mappingFields(v, valueFields, fmt.Sprintf("header %q's value", header))
	if err != nil {
		return "", err
	}
	if _, ok := f["secretSource"]; ok {
		return "", fmt.Errorf("line %d: header %q has a fixed value (secretSource); identity headers must come from the session", v.Line, header)
	}
	src, ok := f["claimSource"]
	if !ok {
		return "", fmt.Errorf("line %d: header %q: the value has no claimSource", v.Line, header)
	}
	if src.Kind != yaml.MappingNode {
		return "", fmt.Errorf("line %d: header %q: claimSource is not a mapping", src.Line, header)
	}
	cf, err := mappingFields(src, claimSourceFields, fmt.Sprintf("header %q's claimSource", header))
	if err != nil {
		return "", err
	}
	if n, ok := cf["basicAuthPassword"]; ok {
		return "", fmt.Errorf("line %d: header %q turns the claim into basic auth; identity headers carry the claim as it is", n.Line, header)
	}
	if n, ok := cf["prefix"]; ok {
		p, err := stringScalar(n, "prefix")
		if err != nil {
			return "", err
		}
		if p != "" {
			return "", fmt.Errorf("line %d: header %q adds the prefix %q; identity headers carry the claim as it is", n.Line, header, p)
		}
	}
	cn, ok := cf["claim"]
	if !ok {
		return "", fmt.Errorf("line %d: header %q: claimSource names no claim", src.Line, header)
	}
	claim, err := stringScalar(cn, "claim")
	if err != nil {
		return "", err
	}
	if !claimNameRe.MatchString(claim) {
		return "", fmt.Errorf("line %d: header %q: claim name %q: use letters, digits, '_', '.' and '-' only", cn.Line, header, claim)
	}
	if notIdentityClaims[claim] {
		return "", fmt.Errorf("line %d: header %q carries %q, a session token or timestamp, not an identity claim", cn.Line, header, claim)
	}
	return claim, nil
}

// mappingFields returns the fields of a mapping node by name. A field not in
// allowed is refused; one that matches an allowed name only when case is
// ignored is refused as a non-standard spelling (oauth2-proxy would accept it
// and tenant-api would then read the file differently).
func mappingFields(m *yaml.Node, allowed []string, where string) (map[string]*yaml.Node, error) {
	out := make(map[string]*yaml.Node, len(m.Content)/2)
	for i := 0; i+1 < len(m.Content); i += 2 {
		k, v := m.Content[i], m.Content[i+1]
		if k.Kind != yaml.ScalarNode || k.Tag != "!!str" {
			return nil, fmt.Errorf("line %d: %s has a key that is not a string", k.Line, where)
		}
		if _, dup := out[k.Value]; dup {
			return nil, fmt.Errorf("line %d: %s: field %q is written twice; oauth2-proxy refuses that", k.Line, where, k.Value)
		}
		known := false
		for _, a := range allowed {
			if k.Value == a {
				known = true
				break
			}
			if strings.EqualFold(k.Value, a) {
				return nil, fmt.Errorf("line %d: %s: field %q is spelled %q by oauth2-proxy; write it that way", k.Line, where, k.Value, a)
			}
		}
		if !known {
			return nil, fmt.Errorf("line %d: %s: unknown field %q (known: %s)", k.Line, where, k.Value, strings.Join(sorted(allowed), ", "))
		}
		out[k.Value] = v
	}
	return out, nil
}

// refuseIndirection refuses anchors, aliases and merge keys anywhere in the
// document: what a header means must be readable where it is written.
func refuseIndirection(n *yaml.Node) error {
	if n.Kind == yaml.AliasNode {
		return fmt.Errorf("line %d: YAML alias; write the value out", n.Line)
	}
	if n.Anchor != "" {
		return fmt.Errorf("line %d: YAML anchor &%s; write the value out", n.Line, n.Anchor)
	}
	if n.Kind == yaml.MappingNode {
		for i := 0; i < len(n.Content); i += 2 {
			if k := n.Content[i]; k.Tag == "!!merge" {
				return fmt.Errorf("line %d: YAML merge key; write the fields out", k.Line)
			}
		}
	}
	for _, c := range n.Content {
		if err := refuseIndirection(c); err != nil {
			return err
		}
	}
	return nil
}

func stringScalar(n *yaml.Node, field string) (string, error) {
	if n.Kind != yaml.ScalarNode || n.Tag != "!!str" {
		return "", fmt.Errorf("line %d: %s is not a string", n.Line, field)
	}
	return n.Value, nil
}

func boolScalar(n *yaml.Node, field string) (bool, error) {
	if n.Kind != yaml.ScalarNode || n.Tag != "!!bool" {
		return false, fmt.Errorf("line %d: %s is not true or false", n.Line, field)
	}
	var v bool
	if err := n.Decode(&v); err != nil {
		return false, fmt.Errorf("line %d: %s: %w", n.Line, field, err)
	}
	return v, nil
}

func lineOf(data []byte, offset int) int { return bytes.Count(data[:offset], []byte("\n")) + 1 }

func sorted(s []string) []string {
	c := append([]string(nil), s...)
	sort.Strings(c)
	return c
}

// ClaimNames returns the extra claim names, sorted.
func (id Identity) ClaimNames() []string {
	names := make([]string, 0, len(id.ClaimHeaders))
	for k := range id.ClaimHeaders {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}
