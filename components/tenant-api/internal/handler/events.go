package handler

// GET /api/v1/events — the change-event stream, filtered per subscriber
// (#3123).
//
// The route's middleware only checks that the caller may read SOME tenant
// (#2520; before that, read on "*" — which an org-, environment- or
// domain-scoped `tenants: ["*"]` rule passes too), so the route is not an
// isolation barrier: before #3123 every write's tenant id — and every state
// record's entity, federation/<tenant> included — went to every subscriber.

import (
	"net/http"
	"time"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/ws"
)

// ConfigChangeEvent is the event broadcast after a committed write of
// tenantID's conf.d file (gitops.Writer's OnWrite).
func ConfigChangeEvent(tenantID string) ws.Event {
	return ws.Event{
		Type:      "config_change",
		TenantID:  tenantID,
		Timestamp: time.Now(),
		Detail:    "tenant config updated",
		Tenant:    tenantID,
	}
}

// StateChangeEvent is the event broadcast after a write to one of tenant-api's
// own state records (statestore OnWrite). The wire shape is unchanged — the
// entity rides in tenant_id, as before — but the event is now addressed to the
// entity's tenant (statestore.EntityTenant), or to no tenant at all for a
// record that belongs to none (groups, views, the federation policy, the
// AccountID registry), which only a caller who reads every tenant receives.
func StateChangeEvent(entity string) ws.Event {
	tenant, _ := statestore.EntityTenant(entity)
	return ws.Event{
		Type:      "config_change",
		TenantID:  entity,
		Timestamp: time.Now(),
		Detail:    "tenant-api state updated",
		Tenant:    tenant,
	}
}

// EventStream is the GET /api/v1/events handler: hub's stream, each subscriber
// filtered by eventVisible for the request's principal.
//
// The filter is evaluated per event, when the event is broadcast, against the
// RBAC config and tenant→org mapping current AT THAT MOMENT — not frozen when
// the stream opens. An SSE connection lives for hours; a rule removed or
// narrowed by an _rbac.yaml hot-reload (or a tenant moved to another org) must
// stop the flow on the next event, exactly as it changes the next GET, and a
// frozen decision would keep feeding a revoked caller until it reconnects.
// The opposite drift (a grant added) also applies at once, which is harmless.
// The cost is one in-memory RBAC evaluation per event per subscriber; writes
// are rare next to that, and the evaluation does no I/O.
func EventStream(d *Deps, hub *ws.Hub) http.HandlerFunc {
	return hub.Handler(eventFilterFor(d))
}

// eventFilterFor builds each SSE request's subscriber filter: eventVisible for
// the request's principal.
func eventFilterFor(d *Deps) ws.FilterFor {
	return func(r *http.Request) ws.Filter {
		p := rbac.RequestPrincipal(r)
		return func(evt ws.Event) bool { return eventVisible(d, p, evt) }
	}
}

// eventVisible decides one event for one subscriber: an event addressed to a
// tenant goes to callers who may read that tenant (OrgAllowedRead — the
// predicate every list filter uses); a saved-views write goes to callers who
// may GET the views (readsViews — the same bar, #3160); any other event
// addressed to no tenant goes only to callers who read every tenant
// (readsEveryTenant), since there is no tenant to judge it by. Open mode reads
// every tenant and every view, so it keeps receiving everything.
func eventVisible(d *Deps, p *rbac.VerifiedPrincipal, evt ws.Event) bool {
	if d.RBAC == nil {
		return false
	}
	if evt.Tenant == "" {
		if evt.TenantID == viewsEntity {
			return readsViews(d.RBAC, p)
		}
		return readsEveryTenant(d.RBAC, p)
	}
	return OrgAllowedRead(d.RBAC, d.TenantOrg, p, evt.Tenant, rbac.PermRead)
}

// readsEveryTenant reports whether p may read every tenant on every scope
// axis: open mode (no rules, and not the fail-closed-empty state), or a rule
// granting read on the literal "*" with no org-scope, environments or domains
// (rbac.PlatformUnrestricted). It is the bar for anything that is not tied to
// one tenant and so cannot be judged per tenant — a groups / views state event,
// a batch PR whose covered tenants are unknown (#3123).
//
// The bar is read, not admin (cf. tenant_config_derived.go's LoadError, which
// hands out loader text naming arbitrary tenants and file paths): what passes
// here is only the existence of a change or a PR, and a caller with
// unrestricted read can already list every tenant and every PR.
func readsEveryTenant(m *rbac.Manager, p *rbac.VerifiedPrincipal) bool {
	return m != nil && readsPlatform(m, p, m.PlatformUnrestricted)
}

// readsViews reports whether p may read the saved views (GET /views, GET
// /views/{id}, a views change event): open mode, or a rule granting read on
// the literal "*" with no org-scope (rbac.PlatformNonOrgScoped, #3160). A view
// belongs to no tenant and no org, so an org-scoped "*" rule does not reach
// it; environments / domains are not consulted — a view's filters name label
// values, not tenants, and the write bar ignores those axes the same way.
func readsViews(m *rbac.Manager, p *rbac.VerifiedPrincipal) bool {
	return m != nil && readsPlatform(m, p, m.PlatformNonOrgScoped)
}

// readsPlatform is the shape every platform-level READ bar shares: with no
// rules it is open mode's answer (read everything) unless the manager is in
// the fail-closed-empty state (MED-8: a configured _rbac.yaml with zero groups
// denies all); with rules it is bar at read level. The rule predicates
// themselves return false in both zero-group states, so open-mode read access
// has to be decided here, before them.
func readsPlatform(m *rbac.Manager, p *rbac.VerifiedPrincipal,
	bar func(*rbac.VerifiedPrincipal, rbac.Permission) bool) bool {
	if len(m.Get().Groups) == 0 {
		return !m.FailClosedOnEmpty()
	}
	return bar(p, rbac.PermRead)
}
