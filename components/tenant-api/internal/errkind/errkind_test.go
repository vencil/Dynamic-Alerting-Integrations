package errkind

import (
	"errors"
	"fmt"
	"testing"
)

type kinded struct{ k Kind }

func (e *kinded) Error() string   { return string(e.k) }
func (e *kinded) ErrorKind() Kind { return e.k }

func TestOf(t *testing.T) {
	t.Parallel()
	plain := errors.New("plain")
	cases := []struct {
		name string
		err  error
		want Kind
		ok   bool
	}{
		{"nil", nil, "", false},
		{"no kind", plain, "", false},
		{"typed", &kinded{Conflict}, Conflict, true},
		{"wrapped", fmt.Errorf("ctx: %w", &kinded{GitPushFailed}), GitPushFailed, true},
		{"first of two, errors.As order", fmt.Errorf("%w: %w", plain, fmt.Errorf("x: %w: %w",
			&kinded{WriteOverloaded}, &kinded{Conflict})), WriteOverloaded, true},
	}
	for _, tc := range cases {
		got, ok := Of(tc.err)
		if got != tc.want || ok != tc.ok {
			t.Errorf("%s: Of = (%q, %t), want (%q, %t)", tc.name, got, ok, tc.want, tc.ok)
		}
	}
}
