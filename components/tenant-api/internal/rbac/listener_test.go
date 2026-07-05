package rbac

import (
	"context"
	"testing"
)

// The zero value of Listener is ListenerTCP — a context that never had a
// listener stamped must read as TCP (fail-safe), never as the trusted UDS
// carve-out. This is the invariant the whole D2-B audit-binding leans on.
func TestListener_ZeroValueIsTCP(t *testing.T) {
	t.Parallel()
	var l Listener // zero value
	if l != ListenerTCP {
		t.Fatalf("zero-value Listener = %v, want ListenerTCP (fail-safe default)", l)
	}
}

func TestListenerFromContext_MissingDefaultsToTCP(t *testing.T) {
	t.Parallel()
	l, ok := ListenerFromContext(context.Background())
	if ok {
		t.Error("ListenerFromContext reported a value present on a bare context, want absent")
	}
	if l != ListenerTCP {
		t.Errorf("missing-key listener = %v, want ListenerTCP (fail-safe)", l)
	}
}

func TestWithListener_RoundTrips(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		in   Listener
	}{
		{"tcp", ListenerTCP},
		{"uds", ListenerUDS},
	} {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ctx := WithListener(context.Background(), tc.in)
			got, ok := ListenerFromContext(ctx)
			if !ok {
				t.Fatal("ListenerFromContext reported absent after WithListener stamped a value")
			}
			if got != tc.in {
				t.Errorf("round-trip listener = %v, want %v", got, tc.in)
			}
		})
	}
}

func TestListener_String(t *testing.T) {
	t.Parallel()
	if got := ListenerTCP.String(); got != "tcp" {
		t.Errorf("ListenerTCP.String() = %q, want tcp", got)
	}
	if got := ListenerUDS.String(); got != "uds" {
		t.Errorf("ListenerUDS.String() = %q, want uds", got)
	}
	// An out-of-range value renders as tcp (the fail-safe default label), never
	// panics or an empty string.
	if got := Listener(99).String(); got != "tcp" {
		t.Errorf("Listener(99).String() = %q, want tcp (default label)", got)
	}
}
