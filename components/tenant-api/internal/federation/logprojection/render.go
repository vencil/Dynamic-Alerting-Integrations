package logprojection

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

// The placeholders a template carries (helm/vector renders the template; this
// package fills it). The template holds every line of VRL and every routing
// condition; Render only puts data where these stand.
const (
	// PlaceholderAccountMap stands, inside a string, for the tenant_id →
	// AccountID map as a JSON object (a VRL object literal):
	// {"tenant-a":1000,"tenant-b":1001}.
	PlaceholderAccountMap = "__DA_ACCOUNT_MAP__"
	// PlaceholderVisibleMap stands, inside a string, for the tenant_id →
	// selected fields map of the tenants that made a selection, as a JSON
	// object: {"tenant-b":["status","path"]}.
	PlaceholderVisibleMap = "__DA_VISIBLE_MAP__"
	// PlaceholderAccountID, in the KEY of a mapping entry, makes that entry
	// one per projected tenant: the entry is repeated, in AccountID order,
	// with the placeholder replaced by the tenant's AccountID in the key and
	// in every string beneath it. With no projected tenant the entry is
	// dropped. Anywhere else it is an error.
	PlaceholderAccountID = "__DA_ACCOUNT_ID__"
)

// placeholderPrefix starts every placeholder; one left in the output is an
// error (a misspelt placeholder would otherwise reach Vector as text).
const placeholderPrefix = "__DA_"

// Render fills ps into the template (a YAML document). It refuses a template
// that does not carry PlaceholderAccountMap and a per-tenant entry, so a
// template from another chart, or a broken one, never reaches Vector as a
// configuration without any tenant routing in it.
func Render(template []byte, ps []Projection) ([]byte, error) {
	var doc yaml.Node
	if err := yaml.Unmarshal(template, &doc); err != nil {
		return nil, fmt.Errorf("template: %w", err)
	}
	if doc.Kind != yaml.DocumentNode || len(doc.Content) != 1 || doc.Content[0].Kind != yaml.MappingNode {
		return nil, fmt.Errorf("template: not one YAML mapping")
	}
	accountMap, visibleMap, err := dataMaps(ps)
	if err != nil {
		return nil, err
	}
	r := &renderer{accountMap: accountMap, visibleMap: visibleMap, ps: ps}
	if err := r.walk(doc.Content[0]); err != nil {
		return nil, err
	}
	if !r.sawAccountMap {
		return nil, fmt.Errorf("template: no %s", PlaceholderAccountMap)
	}
	if !r.sawPerTenant {
		return nil, fmt.Errorf("template: no entry whose key holds %s", PlaceholderAccountID)
	}
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(2)
	if err := enc.Encode(&doc); err != nil {
		return nil, fmt.Errorf("render: %w", err)
	}
	if err := enc.Close(); err != nil {
		return nil, fmt.Errorf("render: %w", err)
	}
	out := buf.Bytes()
	if i := bytes.Index(out, []byte(placeholderPrefix)); i >= 0 {
		end := i + len(placeholderPrefix) + 32
		if end > len(out) {
			end = len(out)
		}
		return nil, fmt.Errorf("template: placeholder left unfilled near %q", string(out[i:end]))
	}
	return out, nil
}

// dataMaps is the two JSON objects. Tenant ids are DNS labels and the fields
// a fixed vocabulary, but they are encoded, never pasted.
func dataMaps(ps []Projection) (accountMap, visibleMap string, err error) {
	acct := make(map[string]uint32, len(ps))
	vis := map[string][]string{}
	for _, p := range ps {
		acct[p.TenantID] = p.AccountID
		if p.VisibleFields != nil {
			vis[p.TenantID] = append([]string{}, *p.VisibleFields...)
		}
	}
	a, err := json.Marshal(acct)
	if err != nil {
		return "", "", err
	}
	v, err := json.Marshal(vis)
	if err != nil {
		return "", "", err
	}
	return string(a), string(v), nil
}

type renderer struct {
	accountMap, visibleMap      string
	ps                          []Projection
	sawAccountMap, sawPerTenant bool
}

// walk fills n in place. A per-tenant entry is expanded where it stands.
func (r *renderer) walk(n *yaml.Node) error {
	switch n.Kind {
	case yaml.MappingNode:
		var out []*yaml.Node
		for i := 0; i+1 < len(n.Content); i += 2 {
			k, v := n.Content[i], n.Content[i+1]
			if strings.Contains(k.Value, PlaceholderAccountID) {
				r.sawPerTenant = true
				for _, p := range r.ps {
					id := strconv.FormatUint(uint64(p.AccountID), 10)
					kk, vv := clone(k), clone(v)
					if err := replaceID(kk, id); err != nil {
						return err
					}
					if err := replaceID(vv, id); err != nil {
						return err
					}
					if err := r.walk(vv); err != nil {
						return err
					}
					out = append(out, kk, vv)
				}
				continue
			}
			if err := r.scalar(k); err != nil {
				return err
			}
			if err := r.walk(v); err != nil {
				return err
			}
			out = append(out, k, v)
		}
		n.Content = out
	case yaml.SequenceNode:
		for _, c := range n.Content {
			if err := r.walk(c); err != nil {
				return err
			}
		}
	case yaml.ScalarNode:
		return r.scalar(n)
	case yaml.AliasNode:
		return fmt.Errorf("template: line %d: an alias cannot be filled", n.Line)
	}
	return nil
}

func (r *renderer) scalar(n *yaml.Node) error {
	if n.Kind != yaml.ScalarNode {
		return nil
	}
	if strings.Contains(n.Value, PlaceholderAccountID) {
		return fmt.Errorf("template: line %d: %s outside a per-tenant entry", n.Line, PlaceholderAccountID)
	}
	if strings.Contains(n.Value, PlaceholderAccountMap) {
		r.sawAccountMap = true
		n.Value = strings.ReplaceAll(n.Value, PlaceholderAccountMap, r.accountMap)
	}
	n.Value = strings.ReplaceAll(n.Value, PlaceholderVisibleMap, r.visibleMap)
	return nil
}

// replaceID puts id where PlaceholderAccountID stands in every string of n.
func replaceID(n *yaml.Node, id string) error {
	if n.Kind == yaml.AliasNode {
		return fmt.Errorf("template: line %d: an alias cannot be filled", n.Line)
	}
	n.Value = strings.ReplaceAll(n.Value, PlaceholderAccountID, id)
	for _, c := range n.Content {
		if err := replaceID(c, id); err != nil {
			return err
		}
	}
	return nil
}

func clone(n *yaml.Node) *yaml.Node {
	c := *n
	c.Content = make([]*yaml.Node, len(n.Content))
	for i, ch := range n.Content {
		c.Content[i] = clone(ch)
	}
	return &c
}
