package handler

// #2830: a `_metadata` that is written but does not decode (a field of the
// wrong type) reads as empty fields, as threshold-exporter reads it, and is
// marked metadata_incomplete — like an unreadable root platform layer.

import (
	"reflect"
	"sort"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

const undecodableRBACYAML = `groups:
  - name: all-tenants
    tenants: ["*"]
    permissions: [read, write]
  - name: payments-only
    tenants: ["*"]
    domains: ["payments"]
    permissions: [read, write]
  - name: finance-only
    tenants: ["*"]
    domains: ["finance"]
    permissions: [read, write]
`

// undecodableTree: tbad and ttags write `domain: finance` next to a field
// that does not decode; tok is the control.
func undecodableTree() map[string]string {
	return map[string]string{
		"tbad.yaml":  "tenants:\n  tbad:\n    _metadata:\n      domain: finance\n      environment: {a: b}\n",
		"ttags.yaml": "tenants:\n  ttags:\n    _metadata:\n      domain: finance\n      tags: x\n",
		"tok.yaml":   "tenants:\n  tok:\n    _metadata:\n      domain: finance\n",
	}
}

func sortedIDs(rows []TenantSummary) []string {
	ids := idsOf(rows)
	sort.Strings(ids)
	return ids
}

func TestUndecodableMetadataMarkedIncomplete(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, undecodableTree())
	for _, enforce := range []bool{false, true} {
		mgr := newRBACManager(t, undecodableRBACYAML)
		if enforce {
			mgr.EnableMetadataScopeEnforce()
		}
		rows := map[string]TenantSummary{}
		for _, r := range listTenantsAs(t, dir, mgr, "all-tenants") {
			rows[r.ID] = r
		}
		for _, id := range []string{"tbad", "ttags"} {
			r, ok := rows[id]
			if !ok {
				t.Fatalf("enforce=%v: %s missing from LIST for all-tenants: %v", enforce, id, rows)
			}
			if !r.MetadataIncomplete || r.Domain != "" || r.Environment != "" || r.Tags != nil {
				t.Errorf("enforce=%v: %s = %+v, want metadata_incomplete with empty metadata", enforce, id, r)
			}
		}
		if r := rows["tok"]; r.MetadataIncomplete || r.Domain != "finance" {
			t.Errorf("enforce=%v: tok = %+v, want domain finance, metadata_incomplete unset", enforce, r)
		}
		if got := sortedIDs(listTenantsAs(t, dir, mgr, "payments-only")); len(got) != 0 {
			t.Errorf("enforce=%v: LIST for payments-only = %v, want none", enforce, got)
		}
	}
}

func TestSearchUndecodableMetadataMarkedIncomplete(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, undecodableTree())
	for _, enforce := range []bool{false, true} {
		mgr := newRBACManager(t, undecodableRBACYAML)
		if enforce {
			mgr.EnableMetadataScopeEnforce()
		}
		search := func(group, query string) []TenantSummary {
			t.Helper()
			resp, code, body := runSearch(t, dir, mgr, []string{group}, query)
			if code != 200 {
				t.Fatalf("search %q as %s: status %d: %s", query, group, code, body)
			}
			return resp.Items
		}
		for _, it := range search("all-tenants", "") {
			if want := it.ID != "tok"; it.MetadataIncomplete != want {
				t.Errorf("enforce=%v: %s metadata_incomplete = %v, want %v", enforce, it.ID, it.MetadataIncomplete, want)
			}
		}
		for query, want := range map[string][]string{
			"":               {"tbad", "tok", "ttags"},
			"domain=finance": {"tok"},
			"q=tbad":         {"tbad"},
		} {
			if got := sortedIDs(search("all-tenants", query)); !reflect.DeepEqual(got, want) {
				t.Errorf("enforce=%v: search %q as all-tenants = %v, want %v", enforce, query, got, want)
			}
		}
		if got := sortedIDs(search("payments-only", "")); len(got) != 0 {
			t.Errorf("enforce=%v: search as payments-only = %v, want none", enforce, got)
		}
	}
}

// The per-tenant readers (the file on disk, a proposed body) read the empty
// pair for such a `_metadata`. OrgAllowed with that pair is compared, per
// group and mode, with OrgAllowed on ("", "finance") — the pair the earlier
// reader returned for these documents: it is never true where that is false.
func TestUndecodableMetadataScopeMetaReadsEmptyPair(t *testing.T) {
	t.Parallel()
	files := undecodableTree()
	dir := setupConfigDir(t, files)
	earlier := func(string) (string, string) { return "", "finance" }
	for _, id := range []string{"tbad", "ttags"} {
		readers := map[string]ScopeMetaFunc{
			"on-disk":  WriteScopeMeta(dir),
			"proposed": proposedScopeMeta(dir, files[id+".yaml"]),
		}
		for name, meta := range readers {
			if env, domain := meta(id); env != "" || domain != "" {
				t.Errorf("%s read for %s = (%q, %q), want (\"\", \"\")", name, id, env, domain)
			}
			for _, enforce := range []bool{false, true} {
				mgr := newRBACManager(t, undecodableRBACYAML)
				if enforce {
					mgr.EnableMetadataWriteScopeEnforce()
				}
				for _, group := range []string{"all-tenants", "payments-only", "finance-only"} {
					p := &rbac.VerifiedPrincipal{Email: "u@example.com", Groups: []string{group}}
					got := OrgAllowed(mgr, nil, p, id, rbac.PermWrite, meta)
					before := OrgAllowed(mgr, nil, p, id, rbac.PermWrite, earlier)
					if got && !before {
						t.Errorf("%s %s enforce=%v group=%s: OrgAllowed = true, with (\"\", \"finance\") = false", name, id, enforce, group)
					}
				}
			}
		}
	}
}
