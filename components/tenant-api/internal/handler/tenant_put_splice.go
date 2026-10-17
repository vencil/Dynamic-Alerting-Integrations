package handler

// --- PUT /tenants/{id} on a shared conf.d file (#3062) ---------------
//
// One conf.d file may declare several tenants, and PUT /tenants/{id} writes
// the whole file {id} lives in. Its body therefore says nothing about the
// other sections of that file the caller may not write — and committing the
// body verbatim let a caller with write access to {id} alone drop or rewrite
// them.
//
// So a PUT body DECLARES tenant sections, and the write replaces exactly
// those:
//
//   - every tenant the body declares must be one the caller may write
//     (declaredTenantsWritable, in the handler, before anything is written);
//   - the declared sections are spliced, as yaml.v3 nodes, into the file as
//     the writer reads it under its lock (spliceDeclaredSections, a
//     gitops.SpliceFunc): an existing section is replaced whole, a new one
//     is appended (a new section for another tenant is still refused by the
//     writer's addedTenantKeys rule);
//   - everything the body does not declare — other tenants' sections, root
//     keys other than `tenants`, comments, later documents — is kept, and
//     verifySplice checks that it is: the written file, parsed back, holds
//     node for node the same YAML outside the declared sections, defines
//     the same anchors in the same order, every alias outside them still
//     resolves to a node written the same, and each declared section
//     decodes to what the body's does. Anything else is refused, fail
//     closed.
//
// Which tenants a body declares is the set of DECODED keys of its `tenants:`
// mapping: `db-b`, `"db-b"`, `'db-b'`, `!!str db-b`, an alias to a `db-b`
// scalar and `!!binary ZGItYg==` are one id. A merge key (`<<`) there, in the
// body or in the file, is refused: what it declares depends on what it
// merges, so it cannot be spliced.

import (
	"bytes"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/tenant-api/internal/customalerts"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

var (
	// errTenantsMergeKey: a `<<` merge key in a `tenants:` mapping.
	errTenantsMergeKey = errors.New("merge key in the tenants block")
	// errTenantsKey: a `tenants:` key that does not decode to a string (a
	// mapping or sequence key), or an id declared twice.
	errTenantsKey = errors.New("tenants block key is not a single tenant id")
)

// Client texts (fixed: they name no tenant and quote no value).
const (
	msgBodyTenantsMergeKey = "the body's tenants block uses a merge key (<<); a PUT replaces exactly the " +
		"tenant sections its body declares, so declare each section literally"
	msgBodyTenantsKey = "the body's tenants block has a key that is not a tenant id, or declares " +
		"one tenant twice"
	msgDeclaresUnwritableTenant = "insufficient permissions: the body declares a tenant section you may not " +
		"write. A PUT replaces only the tenant sections its body declares and keeps the others " +
		"as they are, so leave out every section that is not yours; nothing was written"
)

// tenantKeyID is the tenant id a `tenants:` key declares: the key decoded as
// a string, an alias followed (#3062).
func tenantKeyID(k *yaml.Node) (string, error) {
	n := k
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	if n == nil || n.Kind != yaml.ScalarNode {
		return "", errTenantsKey
	}
	if n.ShortTag() == "!!merge" {
		return "", errTenantsMergeKey
	}
	// Any scalar a string decodes from is an id here, as for the exporter
	// (a map[string] decode): `~`, a non-UTF-8 `!!binary`. Whether it is a
	// valid tenant id is not this function's question — an undeclared
	// section keeps whatever key it has, and a declared one is authorized
	// (and validated) as the id it decodes to.
	var s string
	if err := n.Decode(&s); err != nil {
		return "", errTenantsKey
	}
	return s, nil
}

// tenantsPairs returns a `tenants:` mapping's pairs with the id each one
// declares. A merge key anywhere in it is errTenantsMergeKey (looked for
// first); a key that is not an id, or an id declared twice, errTenantsKey.
func tenantsPairs(tenants *yaml.Node) (ids []string, ps [][2]*yaml.Node, err error) {
	ps = pairs(tenants)
	for _, p := range ps {
		if _, err := tenantKeyID(p[0]); errors.Is(err, errTenantsMergeKey) {
			return nil, nil, err
		}
	}
	seen := map[string]bool{}
	for _, p := range ps {
		id, err := tenantKeyID(p[0])
		if err != nil {
			return nil, nil, err
		}
		if seen[id] {
			return nil, nil, errTenantsKey
		}
		seen[id] = true
		ids = append(ids, id)
	}
	return ids, ps, nil
}

// rootTenantsIndex is the index in pairs(root) of the root's `tenants` key
// (-1 when there is none). A root key that does not decode, or `tenants`
// written twice, is errTenantsKey; a root merge key errTenantsMergeKey.
func rootTenantsIndex(root *yaml.Node) (int, error) {
	idx := -1
	for i, p := range pairs(root) {
		k := p[0]
		for k != nil && k.Kind == yaml.AliasNode {
			k = k.Alias
		}
		if k == nil || k.Kind != yaml.ScalarNode {
			return -1, errTenantsKey
		}
		if k.ShortTag() == "!!merge" {
			return -1, errTenantsMergeKey
		}
		var s string
		if err := k.Decode(&s); err != nil {
			return -1, errTenantsKey
		}
		if s == "tenants" {
			if idx >= 0 {
				return -1, errTenantsKey
			}
			idx = i
		}
	}
	return idx, nil
}

// bodyTenants reads the `tenants:` mapping of a PUT body's first document.
// found is false when there is none to read — the body does not parse, its
// root is not a mapping, has a merge key or does not decode, or `tenants` is
// absent or not a mapping: the writer's validation refuses every such body
// (validateShape). err is errTenantsMergeKey / errTenantsKey for a mapping
// whose keys do not declare a set of tenant ids.
func bodyTenants(body string) (doc *yaml.Node, ids []string, ps [][2]*yaml.Node, found bool, err error) {
	doc = new(yaml.Node)
	if yaml.NewDecoder(strings.NewReader(body)).Decode(doc) != nil ||
		doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 || doc.Content[0].Kind != yaml.MappingNode {
		return nil, nil, nil, false, nil
	}
	root := doc.Content[0]
	idx, rerr := rootTenantsIndex(root)
	if rerr != nil || idx < 0 {
		return nil, nil, nil, false, nil
	}
	t := root.Content[2*idx+1]
	if t.Kind != yaml.MappingNode {
		return nil, nil, nil, false, nil
	}
	ids, ps, err = tenantsPairs(t)
	return doc, ids, ps, true, err
}

// bodyTenantsRefusal is the client text for bodyTenants' err.
func bodyTenantsRefusal(err error) string {
	if errors.Is(err, errTenantsMergeKey) {
		return msgBodyTenantsMergeKey
	}
	return msgBodyTenantsKey
}

// declaredTenantsWritable reports whether the caller may write every tenant
// the body declares besides tenantID (the handler's own gates judged that
// one) — the same two judgements: the tenant's metadata on disk
// (requireOrgWriteAt) and as the body proposes it (requireOrgWriteProposedAt).
// It writes the refusal: 400 for a tenants block that declares no set of
// ids, 403 with a fixed text — no tenant named, nothing said about whether it
// exists — for a tenant the caller may not write.
func declaredTenantsWritable(rw http.ResponseWriter, r *http.Request, d *Deps, tenantID, body string) bool {
	_, ids, _, found, err := bodyTenants(body)
	if !found {
		return true // the writer refuses the body; nothing is declared
	}
	if err != nil {
		WriteJSONError(rw, r, http.StatusBadRequest, bodyTenantsRefusal(err))
		return false
	}
	if d.RBAC == nil {
		return true // test-only state, see requireOrgWriteWithMeta
	}
	p := rbac.RequestPrincipal(r)
	onDisk := WriteScopeMeta(d.ConfigDir)
	proposed := proposedScopeMeta(d.ConfigDir, body)
	for _, id := range ids {
		if id == tenantID {
			continue
		}
		if !OrgAllowed(d.RBAC, d.TenantOrg, p, id, rbac.PermWrite, onDisk) ||
			!OrgAllowed(d.RBAC, d.TenantOrg, p, id, rbac.PermWrite, proposed) {
			WriteJSONErrorWithCode(rw, r, http.StatusForbidden, CodeForbidden, msgDeclaresUnwritableTenant)
			return false
		}
	}
	return true
}

// putSpliceError refuses a PUT whose declared sections cannot be written into
// the current file without changing content the body does not declare.
// Detail names a place only as describePath does.
type putSpliceError struct {
	TenantID string
	Detail   string
}

func (e *putSpliceError) Error() string {
	return fmt.Sprintf("tenant %s: its config file holds content this body does not declare, and writing "+
		"the body's tenant sections into it would change that content (config_error: %s; first difference: "+
		"%s); nothing was written. A PUT replaces only the tenant sections its body declares",
		e.TenantID, configErrorNotRoundTrippable, e.Detail)
}

// writePutSpliceError answers a putSpliceError like a refused partial write
// (writeTenantFileNotLoadable): 409 TENANT_CONFIG_NOT_LOADABLE.
func writePutSpliceError(rw http.ResponseWriter, r *http.Request, err error) bool {
	var e *putSpliceError
	if !errors.As(err, &e) {
		return false
	}
	WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
		Error: e.Error(),
		Code:  CodeTenantConfigNotLoadable,
		Extra: map[string]any{"tenant_id": e.TenantID, "config_error": configErrorNotRoundTrippable},
	})
	return true
}

// putSplicer is the gitops.SpliceFunc a PUT hands the writer.
func putSplicer(tenantID, body string, names gitops.TenantNames) gitops.SpliceFunc {
	return func(base []byte) (string, error) {
		return spliceDeclaredSections(base, body, tenantID, names)
	}
}

// spliceDeclaredSections writes body's declared tenant sections into base
// (see the file comment). An empty base, or one holding nothing but sections
// the body declares, is replaced by the body as it is. A base whose tenants
// cannot be read is gitops.ErrSpliceBaseUnparseable.
func spliceDeclaredSections(base []byte, body, tenantID string, names gitops.TenantNames) (string, error) {
	_, bodyIDs, bodyPairs, found, err := bodyTenants(body)
	if !found {
		return "", fmt.Errorf("%w: YAML must contain a tenants mapping with tenants.%s", gitops.ErrValidation, tenantID)
	}
	if err != nil {
		return "", fmt.Errorf("%w: %s", gitops.ErrValidation, bodyTenantsRefusal(err))
	}
	declared := make(map[string]bool, len(bodyIDs))
	byID := make(map[string][2]*yaml.Node, len(bodyIDs))
	for i, id := range bodyIDs {
		declared[id] = true
		byID[id] = bodyPairs[i]
	}
	if len(bytes.TrimSpace(base)) == 0 {
		return body, nil
	}
	refuse := func(detail string) error { return &putSpliceError{TenantID: tenantID, Detail: detail} }

	docs, err := parseAllDocs(base)
	if err != nil {
		return "", gitops.ErrSpliceBaseUnparseable
	}
	if len(docs) == 0 {
		return body, nil // comments only
	}
	doc := docs[0]
	if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return "", gitops.ErrSpliceBaseUnparseable
	}
	root := doc.Content[0]
	if root.Kind == yaml.ScalarNode && root.ShortTag() == "!!null" && len(docs) == 1 {
		return body, nil // `~`: nothing to keep
	}
	if root.Kind != yaml.MappingNode {
		return "", gitops.ErrSpliceBaseUnparseable
	}
	idx, err := rootTenantsIndex(root)
	if errors.Is(err, errTenantsMergeKey) {
		return "", refuse("a merge key (<<) at the root of the file")
	}
	if err != nil {
		return "", gitops.ErrSpliceBaseUnparseable
	}
	var tenants *yaml.Node
	var baseIDs []string
	var basePairs [][2]*yaml.Node
	if idx >= 0 {
		tenants = root.Content[2*idx+1]
		switch {
		case tenants.Kind == yaml.AliasNode:
			return "", refuse("the file's tenants block is an alias")
		case tenants.Kind == yaml.MappingNode:
			baseIDs, basePairs, err = tenantsPairs(tenants)
			if errors.Is(err, errTenantsMergeKey) {
				return "", refuse("a merge key (<<) in the file's tenants block")
			}
			if err != nil {
				return "", gitops.ErrSpliceBaseUnparseable
			}
		case tenants.Kind == yaml.ScalarNode && tenants.ShortTag() == "!!null":
			tenants = nil // `tenants:` with nothing under it
		default:
			return "", gitops.ErrSpliceBaseUnparseable
		}
	}

	// Nothing outside the declared sections: the body is the file.
	if len(docs) == 1 && len(root.Content) == 2 && idx == 0 {
		keep := false
		for _, id := range baseIDs {
			if !declared[id] {
				keep = true
				break
			}
		}
		if !keep {
			return body, nil
		}
	}

	// The splice, on the base's own nodes.
	var content []*yaml.Node
	used := map[string]bool{}
	for i, p := range basePairs {
		if bp, ok := byID[baseIDs[i]]; ok {
			content = append(content, bp[0], bp[1])
			used[baseIDs[i]] = true
			continue
		}
		content = append(content, p[0], p[1])
	}
	for _, id := range bodyIDs {
		if !used[id] {
			content = append(content, byID[id][0], byID[id][1])
		}
	}
	switch {
	case tenants != nil:
		tenants.Content = content
	case idx >= 0: // `tenants:` null
		root.Content[2*idx+1] = &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: content}
	default:
		root.Content = append(root.Content,
			&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "tenants"},
			&yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: content})
	}

	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	// conf.d's 2-space convention, as mergePatchYAML: a file authored with
	// another indent reflows; comments survive.
	enc.SetIndent(2)
	for _, d := range docs {
		customalerts.DropImplicitTags(d)
		if err := enc.Encode(d); err != nil {
			return "", refuse("the file could not be written back")
		}
	}
	if err := enc.Close(); err != nil {
		return "", refuse("the file could not be written back")
	}
	if err := verifySplice(base, buf.Bytes(), body, declared, tenantID, names); err != nil {
		return "", err
	}
	return buf.String(), nil
}

// verifySplice returns a *putSpliceError unless merged keeps base's written
// YAML everywhere outside the tenants.<declared> sections — the partial-write
// invariant (verifyPartialWrite), the replaced unit being a whole declared
// section — and each declared section of merged decodes to what body's does.
// Not relaxed for the splice: the anchors are compared over the whole file,
// and an alias outside the declared sections must resolve, in merged, to a
// node written as its target in base is.
func verifySplice(base, merged []byte, body string, declared map[string]bool, tenantID string, names gitops.TenantNames) error {
	refuse := func(detail string) error { return &putSpliceError{TenantID: tenantID, Detail: detail} }
	at := func(path []string) error { return refuse(describePath(path, tenantID, names)) }
	bDocs, err := parseAllDocs(base)
	if err != nil {
		return refuse("the current file does not parse")
	}
	mDocs, err := parseAllDocs(merged)
	if err != nil {
		return refuse("the written file would not parse")
	}
	if len(bDocs) != len(mDocs) || len(bDocs) == 0 {
		return refuse(fmt.Sprintf("the number of YAML documents (%d, would be %d)", len(bDocs), len(mDocs)))
	}
	if !reflect.DeepEqual(anchorsOf(bDocs), anchorsOf(mDocs)) {
		return refuse("the anchors the file defines (the write would remove, add or reorder one)")
	}
	c := &nodeCompare{tenantID: tenantID, seen: map[[2]*yaml.Node]bool{}}
	for i := 1; i < len(bDocs); i++ {
		if path, ok := c.equal(bDocs[i], mDocs[i], nil, false); !ok {
			return at(path)
		}
	}
	bd, md := bDocs[0], mDocs[0]
	if !sameNode(bd, md) || len(bd.Content) != 1 || len(md.Content) != 1 {
		return refuse("the first document")
	}
	bRoot, mRoot := bd.Content[0], md.Content[0]
	if bRoot.Kind != yaml.MappingNode || !sameNode(bRoot, mRoot) {
		return refuse("the root of the file")
	}
	bi, berr := rootTenantsIndex(bRoot)
	mi, merr := rootTenantsIndex(mRoot)
	if berr != nil || merr != nil || mi < 0 {
		return refuse("the root of the file")
	}
	// The root keys other than `tenants`, in order.
	without := func(ps [][2]*yaml.Node, skip int) [][2]*yaml.Node {
		out := make([][2]*yaml.Node, 0, len(ps))
		for i, p := range ps {
			if i != skip {
				out = append(out, p)
			}
		}
		return out
	}
	bp, mp := without(pairs(bRoot), bi), without(pairs(mRoot), mi)
	if len(bp) != len(mp) {
		return refuse("the root of the file")
	}
	for i := range bp {
		path := []string{bp[i][0].Value}
		if p, ok := c.eq(bp[i][0], mp[i][0], path, 0); !ok {
			return at(p)
		}
		if p, ok := c.eq(bp[i][1], mp[i][1], path, 0); !ok {
			return at(p)
		}
	}
	mT := pairs(mRoot)[mi][1]
	if mT.Kind != yaml.MappingNode {
		return refuse("the file's tenants block")
	}
	mIDs, mPairs, err := tenantsPairs(mT)
	if err != nil {
		return refuse("the file's tenants block")
	}
	undeclared := func(ids []string, ps [][2]*yaml.Node) (outIDs []string, out [][2]*yaml.Node) {
		for i, id := range ids {
			if !declared[id] {
				outIDs, out = append(outIDs, id), append(out, ps[i])
			}
		}
		return outIDs, out
	}
	_, mKept := undeclared(mIDs, mPairs)
	var bKeptIDs []string
	var bKept [][2]*yaml.Node
	if bi >= 0 {
		bTKey, bT := pairs(bRoot)[bi][0], pairs(bRoot)[bi][1]
		if p, ok := c.eq(bTKey, pairs(mRoot)[mi][0], []string{"tenants"}, 0); !ok {
			return at(p)
		}
		if bT.Kind == yaml.MappingNode {
			if !sameNode(bT, mT) {
				return refuse("the file's tenants block")
			}
			bIDs, bPairs, err := tenantsPairs(bT)
			if err != nil {
				return refuse("the file's tenants block")
			}
			bKeptIDs, bKept = undeclared(bIDs, bPairs)
		}
	}
	if len(bKept) != len(mKept) {
		return refuse("the tenant sections the body does not declare")
	}
	for i := range bKept {
		path := []string{"tenants", bKeptIDs[i]}
		if p, ok := c.eq(bKept[i][0], mKept[i][0], path, 0); !ok {
			return at(p)
		}
		if p, ok := c.eq(bKept[i][1], mKept[i][1], path, 0); !ok {
			return at(p)
		}
	}
	// Each declared section, decoded, is the body's.
	bodyDoc, bodyIDs, bodyPairs, _, err := bodyTenants(body)
	if err != nil || bodyDoc == nil {
		return refuse("the body's tenants block")
	}
	got := map[string]*yaml.Node{}
	for i, id := range mIDs {
		if declared[id] {
			got[id] = mPairs[i][1]
		}
	}
	for i, id := range bodyIDs {
		path := []string{"tenants", id}
		m, ok := got[id]
		if !ok {
			return at(path)
		}
		var want, have any
		if bodyPairs[i][1].Decode(&want) != nil || m.Decode(&have) != nil || !reflect.DeepEqual(want, have) {
			return at(path)
		}
	}
	return nil
}
