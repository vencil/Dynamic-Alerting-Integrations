package rbac

import "context"

// Listener identifies which server socket accepted a connection. It is a
// CONNECTION-derived trust signal (set by each http.Server's ConnContext at
// accept time), NOT anything the request can carry or influence — unlike a
// header, a caller cannot forge or strip it. ADR-027 §2 uses it to decide the
// identity source: headers are read as identity only on the UDS listener.
type Listener int

const (
	// ListenerTCP is the network :8080 listener (probes and metrics only,
	// ADR-027 §3). It is the ZERO value; ListenerFromContext's bool tells a
	// context with no stamp apart from an explicit TCP one (ResolveIdentity
	// resolves an unstamped request — see there).
	ListenerTCP Listener = iota
	// ListenerUDS is the pod-internal Unix-domain-socket listener (human plane,
	// fronted by the same-pod oauth2-proxy), the only plane identity is read
	// on (ADR-027 §2).
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
// with no listener value returns ListenerTCP (the zero value) and false: the
// bool reports whether a value was actually present, and ResolveIdentity
// keys on it.
func ListenerFromContext(ctx context.Context) (Listener, bool) {
	l, ok := ctx.Value(listenerKey{}).(Listener)
	if !ok {
		return ListenerTCP, false
	}
	return l, true
}
