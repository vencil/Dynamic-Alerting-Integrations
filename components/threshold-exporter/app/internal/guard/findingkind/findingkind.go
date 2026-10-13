// Package findingkind is the closed set of da-guard finding kinds: the
// `kind` of every finding in a da-guard report, and for each one whether it
// is a routing kind or a threshold kind (#2904, ADR-036 §9).
//
// Why a package of its own, and why a struct. ADR-036 §9 moves da-guard's
// routing checks to the route generator, and
// tests/shared/test_routing_policy_parity.py refuses to let one be deleted
// from Go before the parity matrix pins the generator against it. That gate
// needs the list of every kind da-guard can report, and four earlier ways of
// producing it were each beaten:
//
//  1. a regex over the guard package's source — several names in one
//     declaration, an alias type, a conversion or a literal were invisible;
//  2. an AST rule allowing kinds in one written shape only — syntax cannot
//     see types, so a kind written through an alias type slipped past;
//  3. the set computed by go/types over the package's files — a kind made at
//     run time (a generic conversion, `*(*FindingKind)(&s)`, json.Unmarshal,
//     fmt.Sscan, go:embed) is no constant the type checker records, and
//     go/build's default context does not even open `_arm64.go` or tagged
//     files, while da-tools ships for arm64;
//  4. checking the kinds at Run's exit against a closed list — rejected in
//     review: it judges the kinds a run happened to produce, not the kinds
//     the code can produce.
//
// With a string type the set of kinds is whatever any string can be, so
// every one of those ways had to be found and closed. Here the type closes
// it instead: Kind is a struct with one unexported field, so outside this
// package a Kind can only be the zero value or a copy of one of the vars
// below. No conversion (from a string, a pointer, a generic type parameter
// or a look-alike struct: a field of another package is never identical)
// and no decoder (there is no UnmarshalJSON, and encoding/json cannot set an
// unexported field) yields any other Kind. register, the one constructor, is
// unexported and runs while this package initialises; All() is therefore
// the complete set, and each entry carries its plane and the reason in the
// same call. What is left is the zero value, which marks a finding no kind
// was chosen for: MarshalJSON refuses it, and da-guard's tests assert no
// finding of their fixtures carries it.
//
// Holes this does not close, held by tests instead: a file of this package
// under a build constraint (findingkind_test.go refuses any), `unsafe` or
// cgo in this package or in internal/guard (refused by the same tests), and
// a second Kind{...} literal in this package (only register may write one).
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
	if _, dup := registry[name]; dup {
		panic(fmt.Sprintf("findingkind: %q registered twice", name))
	}
	k := Kind{s: name}
	registry[name] = Entry{Kind: k, Plane: plane, Reason: reason}
	return k
}

// String is the kind's name as it appears in reports; "" for the zero Kind.
func (k Kind) String() string { return k.s }

// IsZero reports whether k is the zero Kind (no kind chosen).
func (k Kind) IsZero() bool { return k.s == "" }

// Plane is the plane k was registered on; "" for the zero Kind.
func (k Kind) Plane() Plane { return registry[k.s].Plane }

// MarshalJSON writes the kind's name as a JSON string, the same bytes the
// string type it replaces produced. The zero Kind is refused: a report that
// carries it would read as a finding of no kind.
func (k Kind) MarshalJSON() ([]byte, error) {
	if k.IsZero() {
		return nil, fmt.Errorf("findingkind: a finding with no kind (the zero Kind) cannot be reported")
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
