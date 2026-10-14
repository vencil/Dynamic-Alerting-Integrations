// Package findingkind is the closed set of da-guard finding kinds, each
// registered with its plane — routing or threshold (#2904, ADR-036 §9).
//
// Kind's one unexported field keeps a kind from being made by accident: a
// conversion, a literal or a decoder cannot produce one, so the only Kinds
// are the registered vars below, the zero Kind and Unregistered(). What is
// guaranteed is that every kind da-guard outputs is registered, and so
// classified: the guard package's report output swaps any other kind for
// Unregistered() with severity error (internal/guard/seal.go). Non-goal: a
// kind forged on purpose (unsafe, reflect over unsafe pointers, linkname
// into the registry) — under a registered name it is that kind, under any
// other it becomes Unregistered() at the output.
package findingkind

import (
	"encoding/json"
	"fmt"
	"sort"
)

// Kind is one da-guard finding kind. The zero Kind is no kind: it is what a
// finding carries when no kind was chosen for it.
type Kind struct{ s string }

// Plane says which side of ADR-036 §9 a kind is on.
type Plane string

const (
	// PlaneRouting kinds judge `_routing*` keys, routing profiles, domain
	// policies, receivers / overrides / routes, or which tenants the route
	// generator renders. ADR-036 §9 moves their checks to the generator;
	// each must be pinned in tests/shared/routing_policy_parity_matrix.json
	// before its Go check is deleted.
	PlaneRouting Plane = "routing"
	// PlaneThreshold kinds judge threshold values, defaults files or
	// cardinality, and stay with da-guard.
	PlaneThreshold Plane = "threshold"
)

// Entry is one registered kind with its plane and the reason it is there.
type Entry struct {
	Kind   Kind
	Plane  Plane
	Reason string
}

// registry holds every registered kind by name. It is filled while the
// package initialises (the vars in kinds.go) and only read after that.
var registry = map[string]Entry{}

// register is the only place a non-zero Kind is made. A bad registration is
// a programming error found when the package initialises, so it panics.
func register(name string, plane Plane, reason string) Kind {
	switch {
	case name == "":
		panic("findingkind: register with an empty name")
	case plane != PlaneRouting && plane != PlaneThreshold:
		panic(fmt.Sprintf("findingkind: %q registered on plane %q, neither %q nor %q",
			name, plane, PlaneRouting, PlaneThreshold))
	case reason == "":
		panic(fmt.Sprintf("findingkind: %q registered without a reason", name))
	}
	if name == unregisteredName {
		panic(fmt.Sprintf("findingkind: %q is the sentinel Unregistered returns, never a registered kind", name))
	}
	if _, dup := registry[name]; dup {
		panic(fmt.Sprintf("findingkind: %q registered twice", name))
	}
	k := Kind{s: name}
	registry[name] = Entry{Kind: k, Plane: plane, Reason: reason}
	return k
}

// unregisteredName is the name of the kind Unregistered returns. It is part
// of da-guard's JSON contract (docs/cli-reference.md §guard).
const unregisteredName = "unregistered_finding_kind"

// Unregistered is the kind a guard report's output writes in place of any
// kind that is not registered (see the package comment). It is not in All()
// and has no plane.
func Unregistered() Kind { return Kind{s: unregisteredName} }

// IsRegistered reports whether k is a kind register made: one of All().
// False for the zero Kind and for Unregistered().
func (k Kind) IsRegistered() bool {
	e, ok := registry[k.s]
	return ok && e.Kind == k
}

// String is the kind's name as it appears in reports; "" for the zero Kind.
func (k Kind) String() string { return k.s }

// IsZero reports whether k is the zero Kind (no kind chosen).
func (k Kind) IsZero() bool { return k.s == "" }

// Plane is the plane k was registered on; "" for the zero Kind.
func (k Kind) Plane() Plane { return registry[k.s].Plane }

// MarshalJSON writes the kind's name as a JSON string. Only a registered
// kind or Unregistered() is written; the zero Kind and any other Kind are
// refused, for a caller that marshals a Kind outside a guard report (a
// report swaps them first; see the package comment).
func (k Kind) MarshalJSON() ([]byte, error) {
	switch {
	case k.IsZero():
		return nil, fmt.Errorf("findingkind: a finding with no kind (the zero Kind) cannot be reported")
	case !k.IsRegistered() && k != Unregistered():
		return nil, fmt.Errorf("findingkind: kind %q was not registered and cannot be reported", k.s)
	}
	return json.Marshal(k.s)
}

// All returns every registered kind, sorted by name.
func All() []Entry {
	out := make([]Entry, 0, len(registry))
	for _, e := range registry {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Kind.s < out[j].Kind.s })
	return out
}
