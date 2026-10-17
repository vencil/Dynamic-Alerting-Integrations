package confd

import (
	"bytes"
	"errors"
	"io"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// This file is the one definition of WHICH TENANTS tenant-api manages (#3064).
//
// RBAC grants per tenant id, while a write, a hash and a raw read are per
// FILE. The two agree only when the file is the tenant's alone, so tenant-api
// draws the line at the file: a tenant whose file (located by its name, as
// ResolveTenantFile locates it) is "API-shaped" is API-managed — read whole,
// hashed whole and written whole through the API, because everything in it is
// that tenant's. Every other tenant is git-managed: tenant-api shows its
// effective config only and refuses every write, so nothing of a shared file
// can reach a caller through the API, and no write can reach a tenant the
// caller was not authorized for.
//
// ⛔ THIS PREDICATE IS WRITTEN ONCE. GET, every write path (PUT, its /diff and
// /validate previews, PUT .../custom-alerts, the batches, PR mode) and the
// writer under its lock all ask TenantFileManagement; a second copy anywhere
// is the drift #3050 / #3062 came from.
//
// ⛔ IDS ARE WHAT THE EXPORTER'S DECODER READS, not the node's text: a key is
// decoded into a string as the exporter's typed decode into map[string] does
// (a `? !!binary ZGItYQ==` key decodes to "db-a"). The node walk alone is held
// to cfg.ParseTenantFile shape by shape (TestShapeVerdict_AgreesWithTheExporter);
// the exporter's own tenant set is unioned in as well, as defence in depth.

// Git-managed reasons (TenantFileManagement). Fixed strings: they describe
// the file's shape and never name a tenant.
const (
	// GitManagedNotTenantDocument: not YAML, or its root or its `tenants`
	// is not a mapping, or `tenants` is a top-level key twice.
	GitManagedNotTenantDocument = "not_tenant_document"
	// GitManagedSeveralDocuments: a YAML document with content after the
	// first (the exporter reads the first alone).
	GitManagedSeveralDocuments = "several_documents"
	// GitManagedYAMLIndirection: an anchor, an alias, a merge key (<<) or a
	// key that is not an untagged scalar — at the top level or directly
	// under `tenants:`.
	GitManagedYAMLIndirection = "yaml_indirection"
	// GitManagedTenantCount: the file declares several tenants.
	GitManagedTenantCount = "tenant_count"
	// GitManagedIDMismatch: the one tenant it declares is not the file's
	// name (stem).
	GitManagedIDMismatch = "id_mismatch"
)

// TenantFileManagement reports whether data, the content of the tenant file
// whose name's stem is stem, is API-shaped: "" when it is (the tenant is
// API-managed), else the git-managed reason.
//
// API-shaped is:
//
//   - one YAML document: any document after the first is empty (no node, or
//     a null one — a trailing `---`, a comment), as the exporter reads the
//     first document alone;
//   - its root a mapping, and its keys untagged scalars, plain or quoted (no
//     tag — `!!str` included —, not `<<`, not a block scalar), carrying no
//     anchor, with no value that is an alias or carries an anchor;
//   - its `tenants` a mapping with one pair, held to the same rules: an
//     untagged scalar key that decodes to stem, a value that is not an alias
//     and carries no anchor;
//   - cfg.ParseTenantFile, when it decodes the file, declaring stem alone.
//
// A file that declares no tenant at all — empty, comments only, a null root,
// no or an empty `tenants` — is API-shaped too, if its top level keeps the
// rules above: it holds nothing of any tenant, so a write fills it (a shell
// `<id>.yaml` created ahead of the tenant) and nothing in it is another
// tenant's to show.
//
// Anchors and aliases INSIDE the tenant's own section are allowed: the whole
// file is the tenant's, so what they resolve to is the tenant's own content.
// The rules above are only about what declares a tenant: what sits at the
// top level and directly under `tenants:` decides which ids the exporter
// reads, and a merge key, an alias or a tagged key there can declare an id
// its text does not show.
func TenantFileManagement(data []byte, stem string) string {
	ids, reason := walkDeclaredIDs(data)
	if reason != "" {
		return reason
	}
	// Defence in depth: whatever the exporter's own parse declares is
	// declared too. TestShapeVerdict_AgreesWithTheExporter shows, shape by
	// shape, that the walk alone already never calls a file API-shaped that
	// the exporter reads as declaring another id, so on every shape it
	// covers this adds nothing; it stays for a shape that table misses. A
	// file the exporter does not decode is served by no plane
	// (config_error); the walk is what decides for it.
	if c, err := cfg.ParseTenantFile(data); err == nil {
		for id := range c.Tenants {
			ids = append(ids, id)
		}
	}
	return declaredVerdict(ids, stem)
}

// shapeVerdict is TenantFileManagement without the exporter's set: the
// verdict of the node walk alone. Only the differential test calls it — it is
// what that test holds against cfg.ParseTenantFile, which the union in
// TenantFileManagement would otherwise make agree by construction.
func shapeVerdict(data []byte, stem string) string {
	ids, reason := walkDeclaredIDs(data)
	if reason != "" {
		return reason
	}
	return declaredVerdict(ids, stem)
}

// walkDeclaredIDs walks data's YAML nodes: the ids its `tenants:` keys
// decode to (repeats kept), or the git-managed reason its shape alone gives
// (TenantFileManagement's rules other than the id check).
func walkDeclaredIDs(data []byte) (ids []string, reason string) {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, ""
	}
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var docs []*yaml.Node
	for {
		n := new(yaml.Node)
		if err := dec.Decode(n); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return nil, GitManagedNotTenantDocument
		}
		docs = append(docs, n)
	}
	if len(docs) == 0 {
		return nil, "" // comments only: no document, no tenant
	}
	for _, d := range docs[1:] {
		if docRoot(d) != nil {
			return nil, GitManagedSeveralDocuments
		}
	}
	root := docRoot(docs[0])
	if root == nil {
		return nil, "" // an empty first document declares no tenant
	}
	if root.Kind == yaml.AliasNode || root.Anchor != "" {
		return nil, GitManagedYAMLIndirection
	}
	if root.Kind != yaml.MappingNode {
		return nil, GitManagedNotTenantDocument
	}
	var tenants *yaml.Node
	for i := 0; i+1 < len(root.Content); i += 2 {
		k, v := root.Content[i], root.Content[i+1]
		name, ok := plainUntaggedKey(k)
		if !ok || indirect(v) {
			return nil, GitManagedYAMLIndirection
		}
		if name != "tenants" {
			continue
		}
		if tenants != nil {
			return nil, GitManagedNotTenantDocument
		}
		tenants = v
	}
	if tenants != nil && !isNull(tenants) { // absent or null: no tenant
		if tenants.Kind != yaml.MappingNode {
			return nil, GitManagedNotTenantDocument
		}
		for i := 0; i+1 < len(tenants.Content); i += 2 {
			k, v := tenants.Content[i], tenants.Content[i+1]
			id, ok := plainUntaggedKey(k)
			if !ok || indirect(v) {
				return nil, GitManagedYAMLIndirection
			}
			ids = append(ids, id)
		}
	}
	return ids, ""
}

// declaredVerdict is TenantFileManagement's verdict on the ids a file
// declares (repeats allowed — a key written twice is still the one tenant):
// none, or stem alone, is API-shaped.
func declaredVerdict(ids []string, stem string) string {
	distinct := map[string]bool{}
	for _, id := range ids {
		distinct[id] = true
	}
	switch {
	case len(distinct) > 1:
		return GitManagedTenantCount
	case len(distinct) == 1 && !distinct[stem]:
		return GitManagedIDMismatch
	}
	return ""
}

// docRoot is the root node of a decoded document, or nil when the document
// holds nothing: no node, or a null scalar (`~`, an empty `---`).
func docRoot(d *yaml.Node) *yaml.Node {
	if d.Kind != yaml.DocumentNode || len(d.Content) == 0 || isNull(d.Content[0]) {
		return nil
	}
	return d.Content[0]
}

// isNull reports whether n is a null scalar.
func isNull(n *yaml.Node) bool {
	return n.Kind == yaml.ScalarNode && n.Tag == "!!null"
}

// plainUntaggedKey returns the string mapping key k decodes to, and whether
// k is an untagged flow scalar: plain or quoted (a quoted key decodes to its
// text, as the exporter's map[string] decode reads it), no explicit tag, no
// anchor, not `<<`. A tag is what lets a key declare an id its text does not
// show (`!!binary ZGItYQ==` is "db-a"), so every tag is refused, `!!str`
// included.
func plainUntaggedKey(k *yaml.Node) (string, bool) {
	if k.Kind != yaml.ScalarNode || k.Anchor != "" || k.Tag == "!!merge" || k.Value == "<<" {
		return "", false
	}
	if k.Style&^(yaml.DoubleQuotedStyle|yaml.SingleQuotedStyle) != 0 {
		return "", false // tagged, or a block scalar
	}
	var s string
	if err := k.Decode(&s); err != nil {
		return "", false
	}
	return s, true
}

// indirect reports whether v, a value at the top level or directly under
// `tenants:`, is an alias or carries an anchor.
func indirect(v *yaml.Node) bool {
	return v.Kind == yaml.AliasNode || v.Anchor != ""
}
