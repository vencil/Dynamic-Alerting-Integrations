package rbac

import "context"

// Listener identifies which server socket accepted a connection. It is a
// CONNECTION-derived trust signal (set by each http.Server's ConnContext at
// accept time), NOT anything the request can carry or influence — unlike a
// header, a caller cannot forge or strip it. ADR-027 D2-B uses it to bind the
// machine-identity audit to the network TCP listener only (human traffic that
// arrives over the pod-internal Unix socket is a physically-isolated trusted
// hop and is deliberately left out of the machine-identity audit denominator).
type Listener int

const (
	// ListenerTCP is the network :8080 listener (machine/relay plane). It is
	// the ZERO value on purpose: any context that never had a listener stamped
	// (a direct handler test, a future code path that forgets ConnContext)
	// reads as TCP — the fail-safe direction. Mis-attributing a request as TCP
	// only ever keeps it IN the audit denominator / (in a future enforce PR)
	// subject to more scrutiny; it never silently grants the UDS carve-out.
	ListenerTCP Listener = iota
	// ListenerUDS is the pod-internal Unix-domain-socket listener (human plane,
	// fronted by the same-pod oauth2-proxy). Requests here are exempt from the
	// machine-identity audit (see middleware.go / ADR-027 §2.3).
	ListenerUDS
)

// String renders the listener for structured logs / audit reconciliation.
func (l Listener) String() string {
	switch l {
	case ListenerUDS:
		return "uds"
	default:
		return "tcp"
	}
}

type listenerKey struct{}

// WithListener stamps the accepting listener onto ctx. Called from each
// http.Server's ConnContext hook, so every request served by that server
// carries its listener identity before the handler chain runs.
func WithListener(ctx context.Context, l Listener) context.Context {
	return context.WithValue(ctx, listenerKey{}, l)
}

// ListenerFromContext returns the listener stamped by WithListener. A context
// with no listener value returns ListenerTCP (the zero value) — the fail-safe
// default: unknown provenance is treated as the network plane, never as the
// trusted UDS carve-out. The bool reports whether a value was actually present
// (for tests that need to distinguish "defaulted" from "explicitly TCP").
func ListenerFromContext(ctx context.Context) (Listener, bool) {
	l, ok := ctx.Value(listenerKey{}).(Listener)
	if !ok {
		return ListenerTCP, false
	}
	return l, true
}
