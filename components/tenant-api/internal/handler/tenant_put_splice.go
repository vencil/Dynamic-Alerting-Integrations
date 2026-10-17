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
//     (declaredTenantsWritable, in the handler, before anything is written;
//     in PR mode judged again on the fresh base, declaredOnBase; the same
//     judgement answers POST /validate);
//   - the declared sections are spliced, as yaml.v3 nodes, into the file as
//     the writer reads it under its lock (spliceDeclaredSections, a
//     gitops.SpliceFunc): an existing section is replaced whole, a new one
//     is appended (a new section for another tenant is still refused by the
//     writer's addedTenantKeys rule);
//   - everything the body does not declare — other tenants' sections, root
//     keys other than `tenants`, comments, later documents — is kept, and
//     verifySplice checks that it is: the written file, parsed back, holds
//     node for node the same YAML outside the declared sections, every
//     alias outside them still resolves to a node written the same, and each
//     declared section decodes to what the body's does. Anything else is
//     refused, fail closed;
//   - for a caller who may not read every tenant of the file (#3050's
//     fileShown), that value comparison would be an oracle on the hidden
//     sections, so a declared section — the body's, or the one it replaces —
//     holding any anchor or alias is refused outright, whatever the values
//     (swapsAnchorsOrAliases, in spliceDeclaredSections);
//   - a tenant the body declares besides {id} that another conf.d file also
//     declares (#2078's duplicate) is refused under the writer's lock, 409
//     TENANT_DECLARED_ELSEWHERE (gitops: ensureDeclaredSectionsNotElsewhere);
//   - a file whose sections cannot be told apart (it does not parse, a merge
//     key or an alias declares what is in `tenants:`) is #2405's: replaced
//     whole by the body for a caller with write permission on all tenants,
//     refused for anyone else;
//   - POST /diff previews the splice (previewPut).
//
// Which tenants a body declares is the set of DECODED keys of its `tenants:`
// mapping: `db-b`, `"db-b"`, `'db-b'`, `!!str db-b`, an alias to a `db-b`
// scalar and `!!binary ZGItYg==` are one id. A merge key (`<<`) there is
// refused in the body (400): what it declares depends on what it merges.

import (
	"bytes"
	"errors"
	"fmt"
	"math"
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

// declaredGate is what the local gates judged one tenant a PUT body declares
// besides {id} on, for a PR-mode write to judge it again on the fresh base
// (putTenantPRMode's check), as {id}'s gatePre / gatePost are.
type declaredGate struct {
	id        string
	pre, post writeGate
}

// declaredTenantsJudge is the verdict on every tenant the body declares
// besides tenantID (the handler's own gates judged that one): the same two
// judgements as for {id} — the tenant's metadata as the file holds it now
// (declaredScopeMeta: the file {id} lives in, which holds the section) and
// as the body proposes it (proposedScopeMeta). status is 0 when the body
// may be written, 400 for a tenants block that declares no set of ids, 403
// for a tenant the caller may not write; msg is then the client text —
// fixed, naming no tenant and saying nothing about whether it exists. gates
// are the judged pairs, for the PR-mode re-judgement.
func declaredTenantsJudge(r *http.Request, d *Deps, tenantID, body string) (gates []declaredGate, status int, msg string) {
	_, ids, _, found, err := bodyTenants(body)
	if !found {
		return nil, 0, "" // the writer refuses the body; nothing is declared
	}
	if err != nil {
		return nil, http.StatusBadRequest, bodyTenantsRefusal(err)
	}
	if d.RBAC == nil {
		return nil, 0, "" // test-only state, see requireOrgWriteWithMeta
	}
	p := rbac.RequestPrincipal(r)
	onDisk := declaredScopeMeta(d.ConfigDir, tenantID)
	proposed := proposedScopeMeta(d.ConfigDir, body)
	for _, id := range ids {
		if id == tenantID {
			continue
		}
		pre, decoded := onDisk(id)
		if !decoded && !mayWriteUndecodableMetadata(d, p) {
			return nil, http.StatusForbidden, msgDeclaresUnwritableTenant
		}
		orgs := gateOrgs(d, id)
		g := declaredGate{id: id,
			pre:  writeGate{pre, orgs},
			post: writeGate{scopePairOf(proposed, id), orgs}}
		if !OrgAllowed(d.RBAC, d.TenantOrg, p, id, rbac.PermWrite, fixedScopeMeta(g.pre.pair)) ||
			!OrgAllowed(d.RBAC, d.TenantOrg, p, id, rbac.PermWrite, fixedScopeMeta(g.post.pair)) {
			return nil, http.StatusForbidden, msgDeclaresUnwritableTenant
		}
		gates = append(gates, g)
	}
	return gates, 0, ""
}

// declaredTenantsWritable is declaredTenantsJudge for PUT: it writes the
// refusal (403 FORBIDDEN, or 400) and returns false, or returns the gates.
func declaredTenantsWritable(rw http.ResponseWriter, r *http.Request, d *Deps, tenantID, body string) ([]declaredGate, bool) {
	gates, status, msg := declaredTenantsJudge(r, d, tenantID, body)
	switch status {
	case 0:
		return gates, true
	case http.StatusForbidden:
		WriteJSONErrorWithCode(rw, r, status, CodeForbidden, msg)
	default:
		WriteJSONError(rw, r, status, msg)
	}
	return nil, false
}

// errDeclaredUnwritableOnBase: a PR-mode PUT whose body declares a tenant
// the caller may not write on the fresh base (#2857 for #3062's declared
// tenants). Answered as declaredTenantsWritable's 403.
var errDeclaredUnwritableOnBase = errors.New("the body declares a tenant the caller may not write on the latest base branch")

// declaredOnBase judges each declared tenant again on the fresh base
// configDir, as putTenantPRMode's check judges {id}: its metadata there
// before the write (freshDeclaredWriteScope; skipped when that file cannot
// be read for it, the post-state then counting the org axis) and as the body
// proposes it there.
func declaredOnBase(d *Deps, p *rbac.VerifiedPrincipal, configDir, tenantID, body string, gates []declaredGate) error {
	for _, g := range gates {
		pre, judged, decoded := freshDeclaredWriteScope(configDir, tenantID, g.id)
		if !decoded && !mayWriteUndecodableMetadata(d, p) {
			return errDeclaredUnwritableOnBase
		}
		if judged {
			if ok, err := freshScopeAllowed(d, p, configDir, g.id, g.pre, pre, true); err != nil || !ok {
				return orFreshScopeError(err, errDeclaredUnwritableOnBase)
			}
		}
		post := scopePairOf(proposedScopeMeta(configDir, body), g.id)
		if ok, err := freshScopeAllowed(d, p, configDir, g.id, g.post, post, !judged); err != nil || !ok {
			return orFreshScopeError(err, errDeclaredUnwritableOnBase)
		}
	}
	return nil
}

// mayWriteUndecodableMetadata: a declared tenant whose `_metadata` in the
// file does not decode has labels nobody can read, so its write scope on the
// metadata axis cannot be judged. It is refused (fail closed, alike on the
// local tree and on the fresh base) unless the caller may write every tenant
// with no scope at all (rbac.PlatformUnrestricted: no org-scope,
// environments or domains), whose verdict no label could change — the one
// caller who can still repair such a section through the API.
func mayWriteUndecodableMetadata(d *Deps, p *rbac.VerifiedPrincipal) bool {
	return d.RBAC == nil || d.RBAC.PlatformUnrestricted(p, rbac.PermWrite)
}

// writeDeclaredUnwritableOnBase answers errDeclaredUnwritableOnBase.
func writeDeclaredUnwritableOnBase(rw http.ResponseWriter, r *http.Request, err error) bool {
	if !errors.Is(err, errDeclaredUnwritableOnBase) {
		return false
	}
	WriteJSONErrorWithCode(rw, r, http.StatusForbidden, CodeForbidden, msgDeclaresUnwritableTenant)
	return true
}

// putSpliceError refuses a PUT whose declared sections cannot be written into
// the current file without changing content the body does not declare.
// Detail says where only to a caller the file is shown to (fileShown, #3050:
// one who may read every tenant it declares), and then names a place in a
// tenant section only as describePath does; any other caller gets
// msgSpliceDetailWithheld, which says nothing about how the file is written.
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

// msgSpliceDetailWithheld is putSpliceError's Detail for a caller the
// current file is not shown to (#3050): which anchors, aliases or blocks it
// holds is its content.
const msgSpliceDetailWithheld = "withheld: the file declares a tenant you may not read"

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

// fileShownFunc is fileShown for one request and tenant, on the bytes given:
// whether a refusal may describe how those bytes are written.
type fileShownFunc func(data []byte) bool

// putFileShown is the fileShownFunc of r's caller for tenantID.
func putFileShown(r *http.Request, d *Deps, tenantID string) fileShownFunc {
	return func(data []byte) bool { return fileShown(r, d, data, tenantID) }
}

// putSplicer is the gitops.SpliceFunc a PUT hands the writer.
func putSplicer(tenantID, body string, names gitops.TenantNames, shown fileShownFunc) gitops.SpliceFunc {
	return func(base []byte) (string, error) {
		return spliceDeclaredSections(base, body, tenantID, names, shown)
	}
}

// spliceDeclaredSections writes body's declared tenant sections into base
// (see the file comment). An empty base, or one holding nothing but sections
// the body declares, is replaced by the body as it is.
//
// A base it cannot splice into is gitops.ErrSpliceBaseUnparseable, one
// answer for every such shape (#2405: replaced whole by the body only for a
// caller with write permission on all tenants): it does not parse, its
// first document's root is not a mapping or has a key that does not decode,
// `tenants` is written twice, is an alias or neither a mapping nor null, or
// the root or `tenants:` has a merge key (`<<`) or a key that is not one
// tenant id. Through a merge key or an alias a section is declared by
// another node, so no section of such a file can be replaced on its own.
//
// shown decides, on base, whether a refusal may say where the difference is
// (putSpliceError).
func spliceDeclaredSections(base []byte, body, tenantID string, names gitops.TenantNames, shown fileShownFunc) (string, error) {
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
	isShown := &spliceShown{base: base, shown: shown}
	refuse := spliceRefusal(tenantID, isShown)

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
	if err != nil { // a merge key, a key that does not decode, `tenants` twice
		return "", gitops.ErrSpliceBaseUnparseable
	}
	var tenants *yaml.Node
	var baseIDs []string
	var basePairs [][2]*yaml.Node
	if idx >= 0 {
		tenants = root.Content[2*idx+1]
		switch {
		case tenants.Kind == yaml.MappingNode:
			baseIDs, basePairs, err = tenantsPairs(tenants)
			if err != nil { // a merge key, a key that is not one tenant id
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

	// #3062: anchors and aliases in what the splice swaps, for a caller the
	// file is not shown to — a STRUCTURAL rule, not a value comparison.
	//
	// An alias binds to the nearest anchor of its name before it, so an
	// anchor the body brings in, or one it takes away with a section it
	// replaces, can rebind an alias the body does not declare — to the body's
	// node, or to an earlier one in a section the caller may not read. Every
	// value comparison verifySplice could run on such a rebinding (does the
	// alias still resolve to what it did?) compares the caller's value with
	// the hidden one: 200 / 409 would tell a caller who may not read the file
	// whether its guess of a hidden value, or of an anchor's name, is right.
	// For a caller the file is withheld from (#3050's fileShown), a value
	// comparison here is an oracle whatever it compares.
	//
	// So such a caller's write is refused, with the one withheld 409, as soon
	// as a declared section — the body's, or the base's it replaces, its key
	// included — holds any anchor or alias: decided on the body and on the
	// sections the caller writes alone, never on hidden content. Without
	// them the splice cannot change the binding of any alias it does not
	// declare, and verifySplice's verdict does not depend on a hidden value.
	// A caller who may read every tenant of the file has no hidden value to
	// learn and keeps the value comparison. fileShown is asked only here and
	// on a refusal (spliceShown), so a write with no anchor or alias in what
	// it swaps never asks it.
	if swapsAnchorsOrAliases(bodyPairs, basePairs, baseIDs, declared) && !isShown.get() {
		return "", refuse("")
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
	if err := verifySplice(base, buf.Bytes(), body, declared, tenantID, names, refuse); err != nil {
		return "", err
	}
	return buf.String(), nil
}

// spliceShown is fileShown for one splice's base, asked at most once and
// only when needed: on a refusal, or when the anchor rule needs it. Each ask
// walks the caller's read permission over every tenant the file declares
// (OrgAllowedRead), which in shadow mode counts a would-deny for an
// unlabeled one — a successful write must not count reads it never made. A
// nil shown shows nothing.
type spliceShown struct {
	base         []byte
	shown        fileShownFunc
	asked, value bool
}

func (s *spliceShown) get() bool {
	if !s.asked {
		s.asked = true
		s.value = s.shown != nil && s.shown(s.base)
	}
	return s.value
}

// spliceRefusal builds a splice's *putSpliceError: its detail as given when
// the base may be described to the caller, msgSpliceDetailWithheld
// otherwise.
func spliceRefusal(tenantID string, shown *spliceShown) func(detail string) error {
	return func(detail string) error {
		if !shown.get() {
			detail = msgSpliceDetailWithheld
		}
		return &putSpliceError{TenantID: tenantID, Detail: detail}
	}
}

// swapsAnchorsOrAliases reports whether a section the splice swaps holds an
// anchor or an alias anywhere, its key included: one the body declares, or
// one of the base's that a declared section replaces.
func swapsAnchorsOrAliases(bodyPairs, basePairs [][2]*yaml.Node, baseIDs []string, declared map[string]bool) bool {
	for _, p := range bodyPairs {
		if hasAnchorOrAlias(p[0]) || hasAnchorOrAlias(p[1]) {
			return true
		}
	}
	for i, p := range basePairs {
		if declared[baseIDs[i]] && (hasAnchorOrAlias(p[0]) || hasAnchorOrAlias(p[1])) {
			return true
		}
	}
	return false
}

// hasAnchorOrAlias reports whether n or a node under it is an alias or
// defines an anchor. An alias is not followed: being one is enough.
func hasAnchorOrAlias(n *yaml.Node) bool {
	if n == nil {
		return false
	}
	if n.Anchor != "" || n.Kind == yaml.AliasNode {
		return true
	}
	for _, c := range n.Content {
		if hasAnchorOrAlias(c) {
			return true
		}
	}
	return false
}

// verifySplice returns refuse's error unless merged keeps base's written
// YAML everywhere outside the tenants.<declared> sections — the partial-write
// invariant (verifyPartialWrite), the replaced unit being a whole declared
// section — and each declared section of merged decodes to what body's does.
//
// Both files are compared as PARSED BACK, so what a reader of merged gets is
// what is compared: an alias outside the declared sections must resolve, in
// merged, to a node written as its target in base is, wherever that target
// now is (an anchor the body drops, redefines or changes the value of is
// caught there, or merged does not parse). Unlike verifyPartialWrite, the
// set of anchors the file defines is NOT compared: a body may add, drop or
// rename anchors inside its own sections (a whole section is its to write),
// and every effect that has on content it does not declare is an alias
// outside them resolving differently, which the node comparison refuses.
// That comparison runs only for a caller the file is shown to: for anyone
// else a declared section with an anchor or alias never gets here
// (swapsAnchorsOrAliases), since comparing a rebound alias with what it
// resolved to would answer whether the caller guessed a hidden value.
func verifySplice(base, merged []byte, body string, declared map[string]bool, tenantID string, names gitops.TenantNames, refuse func(string) error) error {
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
		if bodyPairs[i][1].Decode(&want) != nil || m.Decode(&have) != nil || !decodedEqual(want, have) {
			return at(path)
		}
	}
	return nil
}

// decodedEqual is reflect.DeepEqual for two yaml.v3 decodes, except that a
// NaN (`.nan`) equals a NaN: the same section decoded twice is equal. A map
// key that is NaN cannot be looked up, so such a map is unequal (refused,
// fail closed).
func decodedEqual(a, b any) bool {
	return valuesEqual(reflect.ValueOf(a), reflect.ValueOf(b))
}

func valuesEqual(a, b reflect.Value) bool {
	if !a.IsValid() || !b.IsValid() {
		return a.IsValid() == b.IsValid()
	}
	if a.Type() != b.Type() {
		return false
	}
	switch a.Kind() {
	case reflect.Interface, reflect.Pointer:
		if a.IsNil() || b.IsNil() {
			return a.IsNil() == b.IsNil()
		}
		return valuesEqual(a.Elem(), b.Elem())
	case reflect.Float32, reflect.Float64:
		x, y := a.Float(), b.Float()
		return x == y || (math.IsNaN(x) && math.IsNaN(y))
	case reflect.Slice, reflect.Array:
		if a.Kind() == reflect.Slice && a.IsNil() != b.IsNil() {
			return false
		}
		if a.Len() != b.Len() {
			return false
		}
		for i := 0; i < a.Len(); i++ {
			if !valuesEqual(a.Index(i), b.Index(i)) {
				return false
			}
		}
		return true
	case reflect.Map:
		if a.IsNil() != b.IsNil() || a.Len() != b.Len() {
			return false
		}
		for _, k := range a.MapKeys() {
			bv := b.MapIndex(k)
			if !bv.IsValid() || !valuesEqual(a.MapIndex(k), bv) {
				return false
			}
		}
		return true
	default:
		return reflect.DeepEqual(a.Interface(), b.Interface())
	}
}
